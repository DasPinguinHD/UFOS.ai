"""/v2/* HTTP routes of the Strategy Platform (SPEC §4).

All routes are added to main.py's aiohttp app and therefore sit behind its
``api_auth`` middleware (session token + origin check). Two layers, like
fund/api.py, so the handlers are testable without aiohttp:

- ``V2Api``: one ``async`` method per route taking ``(query, body, match)``
  and returning ``(status, payload)``;
- ``register(app)``: adds the aiohttp routes (aiohttp imported lazily).

Stage 2 (SPEC §7.11): /v2/dsl/operators, /v2/universes, /v2/strategies/* —
lifecycle in ``lifecycle/registry.py``; spec errors -> 400 with the
ValidationResult ``{error, ok, errors: [{path, code, message, line?, column?}],
warnings, facts}``; unknown strategy/version -> 404 ``{error}``; conflicts
(stale base version, read-only library strategy, refused transition) -> 409
``{error, current_version?}``.

Stage 3 (SPEC §8.5, notes §8.11): /v2/backtests (create 202 / list / detail /
cancel) on ``backtest/jobs.py`` — one job at a time (409 ``{error: "A backtest
is already running.", run_id}``), unknown run -> 404 ``{error}``.

Stage 4 (SPEC §9.4, §9.5, V2 notes): /v2/validations (create 202 / list / detail /
exceptions / cancel), /v2/strategies/{id}/holdout (status / open 202), /v2/settings/gates
(GET / PUT) on ``validation/`` — same single-job runner and 409 rule as backtests;
``HoldoutWindowViolation`` -> 400 ``{error, kind, errors: {start|end}, holdout_start}``.

Robustness (SPEC §10.4, R2 notes): ``auto_resume`` in POST /v2/backtests and POST /v2/validations;
POST /v2/backtests/{id}/resume and POST /v2/validations/{id}/resume {auto_resume?} -> 202 {run_id,
resumed_from} (409 {error} when the run cannot be resumed / the basis changed / busy); run lists and
details carry ``resumed_from, resume_count, error_kind, auto_resume, resumable`` and ``status``
"waiting" while an auto-resume waits for the connection; GET/PUT /v2/settings/network {adaptive,
fixed?} (``data.network``, PUT audited ``network_settings_changed``).

Explain (SPEC §11.2, X2 notes §11.6): POST/GET /v2/backtests/{id}/explanations and
/v2/validations/{id}/explanations ({model: standard|premium, language: en|de} -> 200 explanation; errors
{error, code, …} with 402 budget, 409 not done / no attribution / busy, 502 bad_json / llm_error, 504
timeout), GET/PUT /v2/settings/explain {daily_budget_usd} (PUT audited ``explain_settings_changed``).

Strategy Studio + memory (SPEC §13, notes §13.5): /v2/studio/sessions (create / list / detail), POST
/v2/studio/sessions/{id}/messages {text, model}, POST /v2/studio/sessions/{id}/drafts/{message_id}/cancel,
GET /v2/studio/operator-requests, GET/PUT /v2/settings/ideation (audited ``ideation_settings_changed``),
GET /v2/memory — handlers in ``research/studio.py``; errors {error, code, …} as the explain routes.

Discovery loop (SPEC §16, notes §16.12): /v2/loops (create 202 / list / estimate / detail), POST
/v2/loops/{id}/cancel|pause|resume, POST /v2/loops/{id}/finalists/{variant_id}/approve|reject, GET/PUT
/v2/settings/loop — handlers in ``discovery/loop.py``; errors {error, code, …}; busy 409 bodies of the job
routes add ``loop_id`` when a loop's job holds the slot.

Stage 7 (SPEC §17.10, payloads §17.11): /v2/portfolio/*, /v2/oms/*, /v2/incubation* — handlers in
``trading_routes.TradingRoutesMixin``; refused requests {error, code, …} (``execution.errors.TradingError``).

Errors: ``InvalidRequest`` -> 400, other ``UserFacingError`` -> 503 ``{error: user_message, kind}``; invalid
input -> 400 ``{error, errors}``; anything else -> 500 with a generic message
(details only in backend.log). Key VALUES never pass through this API.

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import re
from typing import Mapping, Optional, Tuple

from .. import secrets
from ..core import fixture
from ..core.errors import UserFacingError
from ..storage import db as storage

logger = logging.getLogger(__name__)

NO_BODY = object()
KEY_ACTIONS = ("set", "removed", "imported")
TESTABLE_SOURCES = ("alpaca",)
DATA_LAYER_MODULE = "strategy_platform.data.registry"


def _bad(errors: dict, message: str = "invalid input") -> Tuple[int, dict]:
    return 400, {"error": message, "errors": errors}


def user_error(ex: UserFacingError) -> Tuple[int, dict]:
    from ..core.errors import InvalidRequest
    status = 400 if isinstance(ex, InvalidRequest) else 503     # the caller's request vs. a source failure
    return status, {"error": ex.user_message, "kind": type(ex).__name__}


def _json_safe(obj):
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def dumps(obj) -> str:
    """Strict JSON (no NaN — the .NET client rejects it)."""
    return json.dumps(_json_safe(obj), allow_nan=False, ensure_ascii=False, default=str)


def _load_registry():
    """strategy_platform.data.registry, or None while the data layer is not
    installed yet (development-order guard for POST /v2/data/test only — not a
    data fallback). A missing dependency INSIDE the registry is re-raised."""
    import importlib
    try:
        return importlib.import_module(DATA_LAYER_MODULE)
    except ModuleNotFoundError as ex:
        if ex.name in (DATA_LAYER_MODULE, "strategy_platform.data"):
            return None
        raise


# --- stage 2 helpers (SPEC §7.11) -------------------------------------------------------------

# Review fix 2026-10-09 (SPEC §9.9): the preview's held-weight history reaches into the locked holdout window
# without counting as a trial or raising seen_until — 250 days of weights + public prices would reveal about
# half of the window's performance. The route therefore returns the current signal row + at most the last
# 5 sessions (strategy.preview itself still accepts up to its own MAX_PREVIEW_DAYS for internal callers).
MAX_PREVIEW_DAYS = 5
DEFAULT_PREVIEW_DAYS = 5
# Request bodies: the spec text (≤ MAX_TEXT_BYTES) travels JSON-escaped inside the body; .NET's
# System.Text.Json escapes non-ASCII as \uXXXX (up to 6 bytes per character), so the body limit is
# 4 × MAX_TEXT_BYTES + 4 KB (main.py passes it to aiohttp as client_max_size). The text itself is
# checked against MAX_TEXT_BYTES after decoding.
def max_body_bytes() -> int:
    from ..strategy.limits import MAX_TEXT_BYTES
    # stage 5b (SPEC §14.7): a pasted paper text (≤ 200,000 chars) may arrive \uXXXX-escaped (6 bytes per char)
    return max(4 * MAX_TEXT_BYTES, 6 * 200_000) + 4096


def _type_name(t) -> str:
    names = {str: "a string", int: "an integer", bool: "true/false", type(None): "null", dict: "an object"}
    if isinstance(t, tuple):
        return " or ".join(names.get(x, x.__name__) for x in t)
    return names.get(t, t.__name__)


def _is_type(value, t) -> bool:
    ts = t if isinstance(t, tuple) else (t,)
    if isinstance(value, bool) and bool not in ts:
        return False                     # JSON true/false is not an integer
    return isinstance(value, ts)


def _check_body(body, required: dict = None, optional: dict = None) -> dict:
    """{field: message} for a JSON-object body with typed required/optional fields; unknown -> error."""
    required, optional = required or {}, optional or {}
    if not isinstance(body, dict):
        return {"_": "expected a JSON object {" + ", ".join(list(required) + [f"{k}?" for k in optional]) + "}"}
    errors = {k: "unknown field" for k in sorted(set(body) - set(required) - set(optional))}
    for name, t in required.items():
        if name not in body:
            errors[name] = "required"
        elif not _is_type(body[name], t):
            errors[name] = f"expected {_type_name(t)}"
    for name, t in optional.items():
        if name in body and not _is_type(body[name], t):
            errors[name] = f"expected {_type_name(t)}"
    return errors


def _loop_busy(run_id) -> dict:
    """409 busy bodies (SPEC §16.8): ``loop_id`` when the active job belongs to a running discovery loop."""
    try:
        from ..discovery import loop
        lid = loop.loop_of_run(run_id)
    except Exception:     # noqa: BLE001 — the 409 itself must not fail
        lid = None
    return {"loop_id": lid} if lid else {}


def _text_too_large(text: str):
    from ..strategy.limits import MAX_TEXT_BYTES
    size = len(text.encode("utf-8", "surrogatepass"))
    if size > MAX_TEXT_BYTES:
        return 400, {"error": f"The spec text is too large ({size} bytes; at most {MAX_TEXT_BYTES}).",
                     "errors": {"text": f"at most {MAX_TEXT_BYTES} bytes"}}
    return None


def _version_param(raw):
    if not isinstance(raw, str) or not raw.isdigit() or len(raw) > 9:
        return None
    v = int(raw)
    return v if v >= 1 else None


def _spec_result(result) -> dict:
    """ValidationResult -> {ok, errors, warnings, facts} (JSON-safe)."""
    from ..lifecycle import registry as lc
    d = lc.validation_dict(result)
    return {"ok": bool(d.get("ok")), "errors": list(d.get("errors") or []), "warnings": list(d.get("warnings") or []),
            "facts": dict(d.get("facts") or {})}


def dsl_fields():
    from ..strategy import dsl
    for name in ("FIELDS", "BAR_FIELDS"):
        value = getattr(dsl, name, None)
        if value:
            return tuple(value)
    raise RuntimeError("strategy.dsl exposes no FIELDS")


def universes_payload() -> list:
    from ..strategy import universe
    return universe.universes()


def _strategy_row_out(row: dict) -> dict:
    return {k: row[k] for k in ("id", "name", "origin", "category", "state", "current_version", "created_at",
                                "updated_at", "state_changed_at", "library_key")}


def _strategy_summary_fields(row: dict, spec: dict, envelope: dict, validation: dict) -> dict:
    return {"content_hash": row.get("content_hash") or envelope.get("content_hash"),
            "tags": list(spec.get("tags") or []), "ai_generated": envelope.get("ai_generated", False),
            "external_text": bool(envelope.get("external_text")), "valid": bool(validation.get("ok")),
            **fixture.spec_fields(spec)}           # sandbox fixture label (SPEC "Sandbox profile"; absent otherwise)


def _list_strategies(filters: dict) -> list:
    from ..lifecycle import registry as lc
    out = []
    for r in storage.strategies_list(**filters):
        spec = lc._loads(r["spec_json"], "spec")
        envelope = lc._loads(r["envelope_json"], "envelope")
        validation = lc.validate_spec(spec)           # current rules (a stricter validator can flag old specs)
        item = _strategy_row_out(r)
        item.update(_strategy_summary_fields(r, spec, envelope, validation))
        item.update(lc.family_chips(r, spec))            # stage 4: family_id, contaminated, holdout_failed_until, trial_count
        item["has_exception"] = has_exception(r["id"], r["current_version"])
        out.append(item)
    return out


def _strategy_detail(strategy_id) -> dict:
    from ..lifecycle import registry as lc
    row = lc.get(strategy_id)
    versions = storage.strategy_versions_list(row["id"])
    current = versions[-1] if versions else None
    spec = lc._loads(current["spec_json"], "spec") if current else {}
    envelope = lc._loads(current["envelope_json"], "envelope") if current else {}
    strategy = _strategy_row_out(row)
    strategy.update(_strategy_summary_fields({"content_hash": current and current["content_hash"]}, spec, envelope,
                                             lc.validate_spec(spec) if current else {}))
    strategy["description"] = spec.get("description", "")
    strategy["library"] = lc.library_metadata(row["library_key"])
    strategy.update(lc.family_chips(row, spec if current else None))     # stage 4 (SPEC §9.7 chips)
    strategy["has_exception"] = has_exception(row["id"], row["current_version"])
    return {
        "strategy": strategy,
        "versions": [{"version": v["version"], "content_hash": v["content_hash"], "created_at": v["created_at"],
                      "origin": (lc._loads(v["envelope_json"], "envelope") or {}).get("origin")}
                     for v in versions],
        "transitions": [{"id": t["id"], "ts": t["ts"], "version": t["version"], "from_state": t["from_state"],
                         "to_state": t["to_state"], "actor": t["actor"], "reason": t["reason"],
                         "evidence": lc._loads(t["evidence_json"], "evidence") if t["evidence_json"] else None}
                        for t in storage.strategy_transitions_list(row["id"])],
        "allowed_transitions": lc.allowed_transitions(row),
    }


def spec_summary(spec: dict) -> dict:
    """Readable one-liners of a stored spec (strategy.summary, B1)."""
    from ..strategy import summary
    return summary.summary_texts(spec)


def _version_detail(strategy_id, version: int) -> dict:
    from ..lifecycle import registry as lc
    from ..strategy import schema
    row = lc.get(strategy_id)
    v = lc.get_version(strategy_id, version)
    return {"spec": v["spec"], "spec_text": schema.pretty_json(v["spec"]), "envelope": v["envelope"],
            "validation": _spec_result(lc.validate_spec(v["spec"])), "summary": spec_summary(v["spec"]),
            "library": lc.library_metadata(row["library_key"])}


def _export(strategy_id, version: int) -> dict:
    from ..lifecycle import registry as lc
    from ..strategy import schema
    v = lc.get_version(strategy_id, version)
    spec = dict(v["spec"])
    spec["provenance"] = {"exported_from": {"strategy_id": strategy_id, "version": version,
                                            "content_hash": v["content_hash"]}}
    slug = re.sub(r"[^a-z0-9]+", "-", str(spec.get("name", "strategy")).lower()).strip("-")[:60] or "strategy"
    return {"file_name": f"{slug}-v{version}.json", "text": schema.pretty_json(spec)}


class PreviewDataError(UserFacingError):
    """The preview failed on the LOADED data (duplicate/malformed bars, values the evaluator rejects).
    The spec was validated before, so this is not the caller's request error -> 503 with the message."""


def _preview(strategy_id, version, days: int) -> dict:
    from ..lifecycle import registry as lc
    from ..strategy import preview as preview_mod
    row = lc.get(strategy_id)
    v = lc.get_version(strategy_id, version or row["current_version"])
    try:
        return preview_mod.preview(v["spec"], as_of=None, days=days)
    except UserFacingError:
        raise                       # InvalidRequest -> 400, source failures -> 503 (user_error)
    except (ValueError, TypeError) as ex:
        # panel/evaluate/portfolio raise ValueError (EvaluationError) for bad source data after validation
        logger.warning("preview of %s failed on its data: %s", strategy_id, ex)
        raise PreviewDataError(f"The signal preview failed on the loaded market data: {str(ex)[:300]}",
                               detail=type(ex).__name__) from None


# --- stage 3 helpers (SPEC §8.5, notes §8.11) --------------------------------------------------

BT_BODY_OPTIONAL = {"version": int, "start": str, "end": str, "benchmark": str, "capital": (int, float),
                    "stress": bool, "history_from": str, "auto_resume": bool}      # auto_resume: SPEC §10.4
BT_LIST_LIMIT_DEFAULT = 50
BT_LIST_LIMIT_MAX = 200
BT_NOT_FOUND = "Backtest not found."
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _iso_date(value: str):
    from datetime import date
    if not _DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _backtest_request(body) -> Tuple[dict, dict]:
    """(errors, normalised request) for POST /v2/backtests."""
    from ..backtest import engine
    from ..core.instruments import instrument
    from ..data import seam
    errors = _check_body(body, required={"strategy_id": str}, optional=BT_BODY_OPTIONAL)
    if errors:
        return errors, {}
    if not _ID_RE.fullmatch(body["strategy_id"]):
        errors["strategy_id"] = "a strategy id"
    if "version" in body and body["version"] < 1:
        errors["version"] = "a version number ≥ 1"
    dates = {}
    for name in ("start", "end"):
        if name in body:
            d = _iso_date(body[name])
            if d is None:
                errors[name] = "an ISO date YYYY-MM-DD"
            else:
                dates[name] = d
    if "start" in dates and dates["start"] < seam.EARLIEST_START:
        errors["start"] = f"on or after {seam.EARLIEST_START.isoformat()}"
    if "start" in dates and "end" in dates and dates["start"] >= dates["end"] and "start" not in errors:
        errors["start"] = "before end"
    if "history_from" in body and body["history_from"] not in engine.HISTORY_FROM_CHOICES:
        errors["history_from"] = "one of " + ", ".join(engine.HISTORY_FROM_CHOICES)
    benchmark = None
    if "benchmark" in body:
        try:
            benchmark = instrument(body["benchmark"]).symbol
        except ValueError:
            errors["benchmark"] = "a supported symbol (e.g. SPY)"
    capital = None
    if "capital" in body:
        try:
            capital = float(body["capital"])
        except OverflowError:
            capital = math.inf
        if not (math.isfinite(capital) and engine.CAPITAL_MIN <= capital <= engine.CAPITAL_MAX):
            errors["capital"] = f"{engine.CAPITAL_MIN:,.0f} … {engine.CAPITAL_MAX:,.0f}"
    if errors:
        return errors, {}
    request = {"start": body.get("start"), "end": body.get("end"), "benchmark": benchmark,
               "capital": capital, "stress": bool(body.get("stress", False)),
               "history_from": body.get("history_from")}
    return {}, request


def _tiingo_key_check(strategy_id: str, version, request: dict) -> None:
    """Tiingo key checked at submit time (no run card) -> SecretMissing (503) ONLY when the run loads
    equity data before the seam (explicit start incl. warm-up, else history_from "2000" — load start
    < 2016-01-04; user decision 2026-10-06: from the
    seam on prices AND volume come from Alpaca SIP, Tiingo serves only the years before). Unknown
    strategy/version -> lifecycle ``NotFound`` (404). The key value is not kept."""
    from ..backtest import engine
    from ..data.providers import tiingo
    from ..lifecycle import registry as lc
    row = lc.get(strategy_id)
    v = lc.get_version(strategy_id, version if version is not None else row["current_version"])
    if request.get("start") is None and request.get("history_from") in (None, engine.DEFAULT_HISTORY_FROM):
        return                                    # default: loading from the seam
    if engine.needs_pre_seam_equity(v["spec"], request.get("start"), request.get("benchmark"),
                                    request.get("history_from")):
        tiingo.require_key()


RESUME_BODY_OPTIONAL = {"auto_resume": bool}


def _resume_fields(row: dict, successors: dict, mgr) -> dict:
    """SPEC §10.4 fields of a run list row / detail: {resumed_from, resume_count, error_kind, auto_resume,
    resumable} (+ the caller sets status "waiting" via ``_status``)."""
    from ..backtest import runcard
    return {"resumed_from": row.get("resumed_from"), "resume_count": int(row.get("resume_count") or 0),
            "error_kind": row.get("error_kind"), "auto_resume": bool(row.get("auto_resume")),
            "resumable": runcard.resumable(row, successors.get(row["id"]))}


def _status(row: dict, mgr) -> str:
    """The card status, or "waiting" while its job waits for the connection (auto-resume)."""
    if row.get("status") == "running" and mgr is not None and mgr.is_waiting(row["id"]):
        return "waiting"
    return row.get("status")


def _successors_of(rows) -> dict:
    from ..backtest import runcard
    failed = [r["id"] for r in rows if r.get("status") == "failed"]
    return runcard.successors(failed) if failed else {}


def _strategy_names(ids) -> dict:
    out = {}
    for sid in set(ids):
        row = storage.strategy_get(sid)
        out[sid] = row["name"] if row else None
    return out


def _bt_list(strategy_id, limit: int) -> list:
    from ..backtest import jobs, runcard
    rows = runcard.list_runs(strategy_id, limit=limit)
    names = _strategy_names(r["strategy_id"] for r in rows)
    succ = _successors_of(rows)
    mgr = jobs.manager()
    return [dict({"id": r["id"], "created_at": r["created_at"], "started_at": r["started_at"],
                  "finished_at": r["finished_at"], "status": _status(r, mgr), "error": r["error"],
                  "strategy_id": r["strategy_id"], "strategy_name": names.get(r["strategy_id"]),
                  "version": r["version"], "request": r["request"], "range": r["range"],
                  "key_metrics": r["key_metrics"], "kind": r.get("kind", "backtest")},    # kind: stage 4
                 **_resume_fields(r, succ, mgr), **{k: r[k] for k in ("fixture", "fixture_note") if k in r})
            for r in rows]


def _seam_entry(adjustment: str, info: dict):
    seam = (info or {}).get("seam")
    if not seam:
        return None
    return {"adjustment": adjustment, "ok": seam.get("ok"), "factor": seam.get("factor"),
            "checked_at": seam.get("checked_at"), "stats": seam.get("stats")}


def _data_section(data: dict) -> list:
    """Run card data snapshot -> [{symbol, asset_class, role, excluded, prices, volume, dividends, seam}]."""
    out = []
    for symbol in sorted(data or {}):
        e = data[symbol] or {}
        price_info = e.get("split") or e.get("bars") or {}
        ranges = [{"source": src, "first": rg.get("first"), "last": rg.get("last"), "rows": rg.get("rows")}
                  for src, rg in sorted((price_info.get("ranges") or {}).items())]
        if not ranges and price_info.get("sources"):
            ranges = [{"source": src, "first": price_info.get("first"), "last": price_info.get("last"),
                       "rows": price_info.get("rows")} for src in price_info["sources"]]
        vol = e.get("volume")
        actions = e.get("corporate_actions")
        seams = [s for s in (_seam_entry("all", e.get("all")), _seam_entry("split", e.get("split"))) if s]
        out.append({"symbol": symbol, "asset_class": e.get("asset_class"), "role": e.get("role"),
                    "excluded": bool(e.get("excluded")),
                    "prices": {"sources": list(price_info.get("sources") or []), "ranges": ranges},
                    "volume": None if not vol else {"source": vol.get("source"), "first": vol.get("first"),
                                                    "last": vol.get("last"), "rows": vol.get("rows")},
                    "dividends": None if not actions else actions.get("dividends"),
                    "seam": seams})
    return out


BT_DETAIL_FIELDS = ("id", "status", "created_at", "started_at", "finished_at", "error", "strategy_id", "version",
                    "content_hash", "engine_version", "seed", "trial_number", "request", "range", "metrics",
                    "curve", "trades", "warnings", "stress", "licences", "cost_model", "spec",
                    "kind",       # kind (backtest | validation | holdout): stage 4
                    "resumed_from", "resume_count", "error_kind", "auto_resume",         # SPEC §10.4
                    "attribution")                       # SPEC §11.1 (X1): null + attribution_unavailable


def _bt_detail(run_id: str, progress) -> Optional[dict]:
    from ..backtest import metrics as metrics_mod
    from ..backtest import runcard
    from ..data import seam
    card = runcard.get(run_id)
    if card is None:
        return None
    out = {k: card.get(k) for k in BT_DETAIL_FIELDS}
    out["strategy_name"] = _strategy_names([card["strategy_id"]]).get(card["strategy_id"])
    out["key_metrics"] = metrics_mod.summary(card["metrics"]) if card.get("metrics") is not None else None
    out["warnings"] = list(card.get("warnings") or [])
    out["data"] = _data_section(card.get("data"))
    out["seam_date"] = seam.SEAM_DATE.isoformat()
    out["progress"] = progress if card["status"] in ("queued", "running") else None
    out["attribution_unavailable"] = _attribution_reason(card)
    _add_resume(out, card)
    out.update(fixture.card_fields(card))          # sandbox fixture label (SPEC "Sandbox profile"; absent otherwise)
    return out


def _attribution_reason(card: dict) -> Optional[str]:
    """SPEC §11.1/§11.4: why ``attribution`` is null (None when present) — never a guessed value."""
    from ..backtest import attribution
    return attribution.unavailable_reason(card)


def _add_resume(out: dict, card: dict) -> None:
    from ..backtest import jobs
    mgr = jobs.manager()
    out.update(_resume_fields(card, _successors_of([card]), mgr))
    out["status"] = _status(card, mgr)



# --- stage 4 helpers (SPEC §9.4, §9.5; V2 notes) ---------------------------------------------------

VAL_BODY_OPTIONAL = {"version": int, "history_from": str, "auto_resume": bool}        # auto_resume: SPEC §10.4
EXC_BODY = {"gate": str, "reason": str}
VAL_NOT_FOUND = "Validation run not found."
VAL_KINDS = ("validation", "holdout")


def holdout_violation(ex) -> Optional[Tuple[int, dict]]:
    """``families.HoldoutWindowViolation`` -> 400 ``{error, kind, errors: {start|end: message},
    holdout_start}`` (None for other errors)."""
    from ..validation import families
    if not isinstance(ex, families.HoldoutWindowViolation):
        return None
    return 400, {"error": ex.user_message, "kind": "HoldoutWindowViolation",
                 "errors": {ex.what: ex.user_message}, "holdout_start": ex.holdout_start.isoformat()}


def _report_of(card: dict):
    """Merged report of a done validation card / the stored report of a holdout card (else None)."""
    from ..validation import report
    if card.get("status") != "done":
        return None
    if card.get("kind") == "holdout":
        return ((card.get("metrics") or {}).get("holdout") or {}).get("report")
    return report.effective(card)


def _val_detail(run_id: str, progress) -> Optional[dict]:
    from ..backtest import metrics as metrics_mod
    from ..backtest import runcard
    card = runcard.get(run_id)
    if card is None or card.get("kind") not in VAL_KINDS:
        return None
    rep = _report_of(card)
    out = {"id": card["id"], "kind": card["kind"], "status": card["status"], "created_at": card["created_at"],
            "started_at": card.get("started_at"), "finished_at": card.get("finished_at"), "error": card.get("error"),
            "strategy_id": card["strategy_id"],
            "strategy_name": _strategy_names([card["strategy_id"]]).get(card["strategy_id"]),
            "version": card["version"], "content_hash": card.get("content_hash"),
            "trial_number": card.get("trial_number"), "request": card.get("request"), "range": card.get("range"),
            "progress": progress if card["status"] in ("queued", "running") else None,
            "key_metrics": metrics_mod.summary(card["metrics"]) if card.get("metrics") is not None else None,
            "passed": None if rep is None else bool(rep.get("passed")), "report": rep,
            "warnings": list(card.get("warnings") or [])}
    # SPEC §11.1 (X1): the base (training) run's attribution / the holdout window's + the report's gate facts
    from ..backtest import attribution
    out["attribution"] = attribution.with_gate_facts(card.get("attribution"), rep)
    out["attribution_unavailable"] = _attribution_reason(card)
    _add_resume(out, card)
    out.update(fixture.card_fields(card))          # sandbox fixture label (SPEC "Sandbox profile"; absent otherwise)
    return out


def has_exception(strategy_id: str, version) -> bool:
    """SPEC §9.7 chip: the latest done validation run of ``version`` has at least one gate exception."""
    from ..backtest import runcard
    latest = runcard.latest_done(strategy_id, int(version), kind="validation")
    return bool(latest and storage.validation_exceptions_list(latest["id"]))


def _val_list(strategy_id, limit: int) -> list:
    """Validation AND holdout runs (each row has ``kind``), newest first. Reads the list columns only (no
    spec/data/curve/stress — review 2026-10-09); ``passed`` from metrics_json with the exceptions merged."""
    import json as _json
    from ..backtest import metrics as metrics_mod
    from ..validation import report

    def dec(text, what):
        if text is None:
            return None
        try:
            return _json.loads(text)
        except ValueError:
            raise storage.StorageUnavailable(f"the stored {what} of a run card is unreadable") from None

    rows = storage.backtest_runs_list(strategy_id, limit=limit, kind="validation") + \
        storage.backtest_runs_list(strategy_id, limit=limit, kind="holdout")
    rows = sorted(rows, key=lambda r: (r["created_at"], r["id"]), reverse=True)[:limit]
    names = _strategy_names(r["strategy_id"] for r in rows)
    from ..backtest import jobs
    succ = _successors_of(rows)
    mgr = jobs.manager()
    out = []
    for r in rows:
        metrics = dec(r["metrics_json"], "metrics")
        passed = exceptions = None
        if r["status"] == "done" and metrics:
            if r["kind"] == "holdout":
                block = metrics.get("holdout") or {}
                passed, exceptions = (bool(block["passed"]) if "passed" in block else None), 0
            else:
                rep = (metrics.get("validation") or {}).get("report")
                if rep is not None:
                    merged = report.merge(rep, report.exceptions_for(r["id"]))
                    passed, exceptions = bool(merged["passed"]), len(merged["exceptions"])
        out.append({"id": r["id"], "kind": r["kind"], "created_at": r["created_at"], "started_at": r["started_at"],
                    "finished_at": r["finished_at"], "status": _status(r, mgr), "error": r["error"],
                    "strategy_id": r["strategy_id"], "strategy_name": names.get(r["strategy_id"]),
                    "version": r["version"], "trial_number": r.get("trial_number"),
                    "request": dec(r["request_json"], "request"), "range": dec(r["range_json"], "range"),
                    "key_metrics": metrics_mod.summary(metrics) if metrics is not None else None,
                    "passed": passed, "exceptions": exceptions, **_resume_fields(r, succ, mgr),
                    **(fixture.fields() if fixture.of_metrics(metrics) else {})})      # sandbox fixture label
    return out


def _holdout_status(strategy_id: str) -> dict:
    """GET /v2/strategies/{id}/holdout: families.holdout_eligibility + whether a passing validation run of
    the current version exists (the button needs both)."""
    from ..backtest import runcard
    from ..lifecycle import registry as lc
    from ..validation import families, report
    row = lc.get(strategy_id)
    out = dict(families.holdout_eligibility(strategy_id))
    version = int(row["current_version"])
    latest = runcard.latest_done(strategy_id, version, kind="validation")
    card = runcard.get(latest["id"]) if latest else None
    passed = bool(card and report.effective_passed(card))
    out["version"] = version
    out["training_end"] = families.lock_for_kind(out["universe_kind"])["training_end"].isoformat()
    out["validation_run_id"] = card["id"] if card else None
    out["validation_passed"] = passed
    out["can_open"] = bool(out["eligible"] and passed)
    if not out["eligible"]:
        out["blocked_reason"] = out.get("reason")
    elif not passed:
        out["blocked_reason"] = (f"needs a passing validation run of version {version} (all gates G1–G8 pass or "
                                 "have an exception)")
    else:
        out["blocked_reason"] = None
    return out


def _put_network_settings(adaptive: bool, fixed) -> dict:
    """data.network.set_settings + audit ``network_settings_changed`` {old, new} (SPEC §10.4)."""
    from ..data import network
    old = network.get_settings()            # does not raise for invalid stored values (repair path)
    new = network.set_settings(adaptive, fixed)     # validates only the NEW values; fixed None = keep stored
    old_detail = {"adaptive": old["adaptive"], "fixed": old["fixed"]}
    if old.get("stored_invalid"):
        old_detail.update({"stored_invalid": True, "stored": old["stored"]})
    storage.add_audit("user", "network_settings_changed",
                      {"old": old_detail, "new": {"adaptive": new["adaptive"], "fixed": new["fixed"]}})
    return new


from .trading_routes import TRADING_ROUTES, TradingRoutesMixin  # noqa: E402 — stage 7 (SPEC §17.10)
from .broker_routes import BROKER_ROUTES, BrokerRoutesMixin  # noqa: E402 — stage 8 (SPEC §18.9)
from .monitoring_routes import MONITORING_ROUTES, MonitoringRoutesMixin, read_csv_body  # noqa: E402 — stage 9 (SPEC §19.10)
from .options_archive_routes import OPTIONS_ARCHIVE_ROUTES, OptionsArchiveRoutesMixin  # noqa: E402 — OS1 (SPEC §21.11)
from .council_routes import COUNCIL_ROUTES, CouncilRoutesMixin  # noqa: E402 — S10-C (SPEC §22.11)


class V2Api(TradingRoutesMixin, BrokerRoutesMixin, MonitoringRoutesMixin, OptionsArchiveRoutesMixin,
            CouncilRoutesMixin):
    """Route handlers (see module docstring)."""

    async def _guarded(self, fn, *args):
        """Runs a blocking call in a thread; maps errors to responses."""
        try:
            return None, await asyncio.to_thread(fn, *args)
        except UserFacingError as ex:
            logger.warning("/v2: %s", ex)
            return user_error(ex), None

    # GET /v2/settings/keys
    async def keys(self, query: Mapping, body, match: Mapping):
        err, keys = await self._guarded(secrets.status)
        if err:
            return err
        return 200, {"keys": keys}

    # POST /v2/settings/keys/changed {name, action}
    async def keys_changed(self, query: Mapping, body, match: Mapping):
        if not isinstance(body, dict):
            return _bad({"_": "expected a JSON object {name, action}"})
        errors = {}
        if "value" in body:
            # the backend never accepts key values (they live in the Credential Manager only)
            errors["value"] = "key values are never sent to the backend"
        for extra in sorted(set(body) - {"name", "action", "value"}):
            errors[extra] = "unknown field"
        name, action = body.get("name"), body.get("action")
        if isinstance(name, str) and name in secrets.RETIRED_SECRETS:      # S10-K: a retired key name
            if action != "removed":
                errors["name"] = f"{name} is retired — only action \"removed\" is accepted"
        elif not isinstance(name, str) or (name not in secrets.SECRETS and name not in secrets.OTHER_MODULE_SECRETS):
            # type check first (unhashable -> 400); S10-E: the GM / LSA names of the shared catalog are audited too
            errors["name"] = f"one of {', '.join(list(secrets.SECRETS) + list(secrets.OTHER_MODULE_SECRETS))}"
        if not isinstance(action, str) or action not in KEY_ACTIONS:
            errors["action"] = f"one of {', '.join(KEY_ACTIONS)}"
        if errors:
            return _bad(errors)
        err, audit_id = await self._guarded(storage.add_audit, "user", f"key_{action}", {"name": name})
        if err:
            return err
        logger.info("Key %s %s in the Credential Manager (audit #%s)", name, action, audit_id)
        return 200, {"ok": True, "audit_id": audit_id}

    # GET /v2/data/sources
    async def data_sources(self, query: Mapping, body, match: Mapping):
        err, rows = await self._guarded(storage.data_sources_list)
        if err:
            return err
        out = []
        for r in rows:
            licence = ({"terms": r["licence"], "note": r["licence_note"]}
                       if r["licence"] is not None or r["licence_note"] is not None else None)
            out.append({"data_type": r["data_type"], "source": r["source"], "status": r["status"],
                        "last_ok_at": r["last_ok_at"], "last_error_at": r["last_error_at"],
                        "message": r["message"], "licence": licence})
        return 200, {"sources": out}

    # POST /v2/data/test {source}
    async def data_test(self, query: Mapping, body, match: Mapping):
        if not isinstance(body, dict):
            return _bad({"_": "expected a JSON object {source}"})
        errors = {extra: "unknown field" for extra in sorted(set(body) - {"source"})}
        source = body.get("source")
        if source not in TESTABLE_SOURCES:
            errors["source"] = f"one of {', '.join(TESTABLE_SOURCES)}"
        if errors:
            return _bad(errors)
        registry = _load_registry()
        if registry is None:
            return 503, {"error": "data layer not installed", "kind": "DataLayerNotInstalled"}
        err, result = await self._guarded(registry.test_source, source)
        if err:
            return err
        if not isinstance(result, dict):
            logger.error("registry.test_source returned %s, expected a dict", type(result).__name__)
            return 500, {"error": "internal error in the data layer (see backend.log)"}
        return 200, result


    # ------------------------------------------------------------------ stage 2 (SPEC §7.11)

    async def _call(self, fn, *args, **kwargs):
        """Runs a blocking lifecycle call in a thread -> (error response or None, result)."""
        from ..lifecycle import registry as lc
        try:
            return None, await asyncio.to_thread(fn, *args, **kwargs)
        except lc.SpecInvalid as ex:
            return (400, {"error": "The strategy spec is invalid.", **_spec_result(ex.validation)}), None
        except lc.NotFound:
            return (404, {"error": "Strategy or version not found."}), None
        except lc.Conflict as ex:
            return (409, {"error": ex.message, **ex.extra}), None
        except ValueError as ex:
            return _bad({"_": str(ex)[:300]}), None
        except UserFacingError as ex:
            logger.warning("/v2: %s", ex)
            return user_error(ex), None

    # GET /v2/dsl/operators
    async def dsl_operators(self, query: Mapping, body, match: Mapping):
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        from ..strategy import dsl, features
        return 200, {"operators": dsl.catalogue(), "fields": list(dsl_fields()), "ext": features.catalogue()}

    # GET /v2/universes
    async def universes(self, query: Mapping, body, match: Mapping):
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        return 200, {"universes": universes_payload()}

    # GET /v2/strategies?state=&origin=&category=
    async def strategies_list(self, query: Mapping, body, match: Mapping):
        from ..lifecycle import registry as lc
        errors = {k: "unknown parameter" for k in sorted(set(query) - {"state", "origin", "category"})}
        filters = {}
        for name, allowed in (("state", lc.STATES), ("origin", lc.ORIGINS), ("category", None)):
            value = query.get(name)
            if value in (None, ""):
                continue
            if allowed is not None and value not in allowed:
                errors[name] = f"one of {', '.join(allowed)}"
            elif allowed is None and (len(value) > 32 or not re.fullmatch(r"[a-z0-9_]+", value)):
                errors[name] = "a category name"
            else:
                filters[name] = value
        if errors:
            return _bad(errors)
        err, rows = await self._call(_list_strategies, filters)
        if err:
            return err
        return 200, {"strategies": rows, "library_status": lc.seed_status()}

    # GET /v2/strategies/{id}
    async def strategy_get(self, query: Mapping, body, match: Mapping):
        err, payload = await self._call(_strategy_detail, match.get("id"))
        return err or (200, payload)

    # GET /v2/strategies/{id}/versions/{v}
    async def strategy_version(self, query: Mapping, body, match: Mapping):
        v = _version_param(match.get("v"))
        if v is None:
            return 404, {"error": "Strategy or version not found."}
        err, payload = await self._call(_version_detail, match.get("id"), v)
        return err or (200, payload)

    # GET /v2/strategies/{id}/versions/{v}/export
    async def strategy_export(self, query: Mapping, body, match: Mapping):
        v = _version_param(match.get("v"))
        if v is None:
            return 404, {"error": "Strategy or version not found."}
        err, payload = await self._call(_export, match.get("id"), v)
        return err or (200, payload)

    # POST /v2/strategies/validate {text}
    async def strategy_validate(self, query: Mapping, body, match: Mapping):
        errors = _check_body(body, required={"text": str})
        if errors:
            return _bad(errors)
        too_big = _text_too_large(body["text"])
        if too_big:
            return too_big
        from ..lifecycle import registry as lc
        err, result = await self._call(lc.validate_text, body["text"])
        return err or (200, _spec_result(result))

    # POST /v2/strategies {text, origin, file_name?, file_sha256?}
    async def strategy_create(self, query: Mapping, body, match: Mapping):
        errors = _check_body(body, required={"text": str, "origin": str},
                             optional={"file_name": (str, type(None)), "file_sha256": (str, type(None))})
        if not errors:
            if body["origin"] not in ("user", "import"):
                errors["origin"] = "one of user, import"
            sha = body.get("file_sha256")
            if sha is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
                errors["file_sha256"] = "64 hex characters"
            if body.get("file_name") is not None and len(body["file_name"]) > 4096:
                errors["file_name"] = "too long"
            if body["origin"] == "user" and (body.get("file_name") is not None or sha is not None):
                errors["origin"] = "file_name / file_sha256 only with origin import"
        if errors:
            return _bad(errors)
        too_big = _text_too_large(body["text"])
        if too_big:
            return too_big
        from ..lifecycle import registry as lc
        sha = body.get("file_sha256")
        err, result = await self._call(lc.create_from_text, body["text"], body["origin"],
                                       file_name=body.get("file_name"), file_sha256=sha.lower() if sha else None)
        return err or (201, result)

    # PUT /v2/strategies/{id} {text, base_version}
    async def strategy_update(self, query: Mapping, body, match: Mapping):
        errors = _check_body(body, required={"text": str, "base_version": int})
        if not errors and body["base_version"] < 1:
            errors["base_version"] = "a version number ≥ 1"
        if errors:
            return _bad(errors)
        too_big = _text_too_large(body["text"])
        if too_big:
            return too_big
        from ..lifecycle import registry as lc
        err, result = await self._call(lc.new_version, match.get("id"), body["text"], body["base_version"])
        return err or (200, result)

    # POST /v2/strategies/{id}/duplicate {name?}
    async def strategy_duplicate(self, query: Mapping, body, match: Mapping):
        if body is NO_BODY:
            body = {}
        errors = _check_body(body, optional={"name": (str, type(None))})
        if errors:
            return _bad(errors)
        from ..lifecycle import registry as lc
        err, result = await self._call(lc.duplicate, match.get("id"), body.get("name"))
        return err or (201, result)

    # POST /v2/strategies/{id}/transition {to, reason}
    async def strategy_transition(self, query: Mapping, body, match: Mapping):
        from ..lifecycle import registry as lc
        # fix1023: the reason is an OPTIONAL note (missing / null / "" allowed)
        errors = _check_body(body, required={"to": str}, optional={"reason": (str, type(None))})
        if not errors:
            if body["to"] not in lc.STATES:
                errors["to"] = f"one of {', '.join(lc.STATES)}"
            if len((body.get("reason") or "").strip()) > lc.MAX_REASON_LEN:
                errors["reason"] = f"at most {lc.MAX_REASON_LEN} characters"
        if errors:
            return _bad(errors)
        err, row = await self._call(lc.transition, match.get("id"), body["to"], body.get("reason") or "")
        if err:
            return err
        return 200, {"strategy": _strategy_row_out(row), "allowed_transitions": lc.allowed_transitions(row)}

    # POST /v2/strategies/{id}/preview {version?, days?}
    async def strategy_preview(self, query: Mapping, body, match: Mapping):
        if body is NO_BODY:
            body = {}
        errors = _check_body(body, optional={"version": int, "days": int})
        if not errors:
            if "version" in body and body["version"] < 1:
                errors["version"] = "a version number ≥ 1"
            if "days" in body and not (1 <= body["days"] <= MAX_PREVIEW_DAYS):
                errors["days"] = f"1..{MAX_PREVIEW_DAYS}"
        if errors:
            return _bad(errors)
        err, result = await self._call(_preview, match.get("id"), body.get("version"), body.get("days", DEFAULT_PREVIEW_DAYS))
        if err:
            return err
        if not isinstance(result, dict):
            logger.error("preview returned %s, expected a dict", type(result).__name__)
            return 500, {"error": "internal error in the Strategy Platform (see backend.log)"}
        return 200, result


    # ------------------------------------------------------------------ stage 3 (SPEC §8.5)

    # POST /v2/backtests {strategy_id, version?, start?, end?, benchmark?, capital?, stress?, history_from?}
    async def backtest_create(self, query: Mapping, body, match: Mapping):
        from ..backtest import jobs, runcard
        from ..lifecycle import registry as lc
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors, request = _backtest_request(body)
        if errors:
            return _bad(errors)
        mgr = jobs.manager()
        if mgr.active_run_id() is not None:
            return 409, {"error": jobs.BUSY_MESSAGE, "run_id": mgr.active_run_id(), **_loop_busy(mgr.active_run_id())}
        try:
            await asyncio.to_thread(_tiingo_key_check, body["strategy_id"], body.get("version"), request)
            run_id = await asyncio.to_thread(lambda: mgr.submit(body["strategy_id"], body.get("version"), request,
                                                                auto_resume=body.get("auto_resume", False)))
        except jobs.JobBusy as ex:
            return 409, {"error": jobs.BUSY_MESSAGE, "run_id": ex.run_id, **_loop_busy(ex.run_id)}
        except (runcard.NotFound, lc.NotFound):
            return 404, {"error": "Strategy or version not found."}
        except ValueError as ex:
            return _bad({"_": str(ex)[:300]})
        except UserFacingError as ex:
            hv = holdout_violation(ex)
            if hv:
                return hv
            logger.warning("/v2/backtests: %s", ex)
            return user_error(ex)
        return 202, {"run_id": run_id}

    # GET /v2/backtests?strategy_id=&limit=
    async def backtests_list(self, query: Mapping, body, match: Mapping):
        errors = {k: "unknown parameter" for k in sorted(set(query) - {"strategy_id", "limit"})}
        sid = query.get("strategy_id") or None
        if sid is not None and not _ID_RE.fullmatch(sid):
            errors["strategy_id"] = "a strategy id"
        limit = BT_LIST_LIMIT_DEFAULT
        raw = query.get("limit")
        if raw not in (None, ""):
            if not raw.isdigit() or len(raw) > 4 or not 1 <= int(raw) <= BT_LIST_LIMIT_MAX:
                errors["limit"] = f"1..{BT_LIST_LIMIT_MAX}"
            else:
                limit = int(raw)
        if errors:
            return _bad(errors)
        err, rows = await self._guarded(_bt_list, sid, limit)
        return err or (200, {"runs": rows})

    # GET /v2/backtests/{id}
    async def backtest_get(self, query: Mapping, body, match: Mapping):
        from ..backtest import jobs
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        run_id = match.get("id") or ""
        if not _ID_RE.fullmatch(run_id):
            return 404, {"error": BT_NOT_FOUND}
        progress = jobs.manager().progress(run_id)
        err, card = await self._guarded(_bt_detail, run_id, progress)
        if err:
            return err
        if card is None:
            return 404, {"error": BT_NOT_FOUND}
        return 200, card

    # POST /v2/backtests/{id}/cancel
    async def backtest_cancel(self, query: Mapping, body, match: Mapping):
        from ..backtest import jobs, runcard
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        if body is not NO_BODY and body != {}:
            return _bad({"_": "no body expected"})
        run_id = match.get("id") or ""
        if not _ID_RE.fullmatch(run_id):
            return 404, {"error": BT_NOT_FOUND}
        if jobs.manager().cancel(run_id):
            return 202, {"run_id": run_id, "status": "cancelling"}
        err, card = await self._guarded(runcard.get, run_id)
        if err:
            return err
        if card is None:
            return 404, {"error": BT_NOT_FOUND}
        if card["status"] in ("queued", "running"):
            # a card without a live job (should not exist after recover_interrupted): end it now
            try:
                await asyncio.to_thread(runcard.cancel, run_id, jobs.CANCELLED_MESSAGE)
            except runcard.StatusConflict:
                pass
            card = await asyncio.to_thread(runcard.get, run_id)
            if card["status"] == "cancelled":
                return 200, {"run_id": run_id, "status": "cancelled"}
        return 409, {"error": f"The backtest is already {card['status']}.", "status": card["status"]}


    # ------------------------------------------------------------------ stage 4 (SPEC §9.4, §9.5)

    # POST /v2/validations {strategy_id, version?, history_from?}
    async def validation_create(self, query: Mapping, body, match: Mapping):
        from ..backtest import engine, jobs, runcard
        from ..lifecycle import registry as lc
        from ..validation import jobs as vjobs
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = _check_body(body, required={"strategy_id": str}, optional=VAL_BODY_OPTIONAL)
        if not errors:
            if not _ID_RE.fullmatch(body["strategy_id"]):
                errors["strategy_id"] = "a strategy id"
            if "version" in body and body["version"] < 1:
                errors["version"] = "a version number ≥ 1"
            if "history_from" in body and body["history_from"] not in engine.HISTORY_FROM_CHOICES:
                errors["history_from"] = "one of " + ", ".join(engine.HISTORY_FROM_CHOICES)
        if errors:
            return _bad(errors)
        mgr = jobs.manager()
        if mgr.active_run_id() is not None:
            return 409, {"error": vjobs.BUSY_MESSAGE, "run_id": mgr.active_run_id(), **_loop_busy(mgr.active_run_id())}
        request = {"start": None, "history_from": body.get("history_from"), "benchmark": None}
        try:
            await asyncio.to_thread(_tiingo_key_check, body["strategy_id"], body.get("version"), request)
            run_id = await asyncio.to_thread(lambda: vjobs.service().submit_validation(
                body["strategy_id"], body.get("version"), body.get("history_from"),
                auto_resume=body.get("auto_resume", False)))
        except jobs.JobBusy as ex:
            return 409, {"error": vjobs.BUSY_MESSAGE, "run_id": ex.run_id, **_loop_busy(ex.run_id)}
        except (runcard.NotFound, lc.NotFound):
            return 404, {"error": "Strategy or version not found."}
        except ValueError as ex:
            return _bad({"_": str(ex)[:300]})
        except UserFacingError as ex:
            hv = holdout_violation(ex)
            if hv:
                return hv
            logger.warning("/v2/validations: %s", ex)
            return user_error(ex)
        return 202, {"run_id": run_id}

    # GET /v2/validations?strategy_id=&limit=
    async def validations_list(self, query: Mapping, body, match: Mapping):
        errors = {k: "unknown parameter" for k in sorted(set(query) - {"strategy_id", "limit"})}
        sid = query.get("strategy_id") or None
        if sid is not None and not _ID_RE.fullmatch(sid):
            errors["strategy_id"] = "a strategy id"
        limit = BT_LIST_LIMIT_DEFAULT
        raw = query.get("limit")
        if raw not in (None, ""):
            if not raw.isdigit() or len(raw) > 4 or not 1 <= int(raw) <= BT_LIST_LIMIT_MAX:
                errors["limit"] = f"1..{BT_LIST_LIMIT_MAX}"
            else:
                limit = int(raw)
        if errors:
            return _bad(errors)
        err, rows = await self._guarded(_val_list, sid, limit)
        return err or (200, {"runs": rows})

    # GET /v2/validations/{id}  (validation and holdout cards)
    async def validation_get(self, query: Mapping, body, match: Mapping):
        from ..backtest import jobs
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        run_id = match.get("id") or ""
        if not _ID_RE.fullmatch(run_id):
            return 404, {"error": VAL_NOT_FOUND}
        progress = jobs.manager().progress(run_id)
        err, card = await self._guarded(_val_detail, run_id, progress)
        if err:
            return err
        if card is None:
            return 404, {"error": VAL_NOT_FOUND}
        return 200, card

    # POST /v2/validations/{id}/exceptions {gate, reason}
    async def validation_exception(self, query: Mapping, body, match: Mapping):
        from ..validation import report
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        run_id = match.get("id") or ""
        if not _ID_RE.fullmatch(run_id):
            return 404, {"error": VAL_NOT_FOUND}
        errors = _check_body(body, required=EXC_BODY)
        if errors:
            return _bad(errors)
        try:
            await asyncio.to_thread(report.add_exception, run_id, body["gate"], body["reason"], "user")
        except report.ExceptionRefused as ex:
            if ex.status == 400:
                return _bad({ex.field: ex.message}, ex.message)
            return ex.status, {"error": ex.message}
        except UserFacingError as ex:
            logger.warning("/v2/validations exceptions: %s", ex)
            return user_error(ex)
        err, card = await self._guarded(_val_detail, run_id, None)
        return err or (200, card)

    # POST /v2/validations/{id}/cancel  (same as POST /v2/backtests/{id}/cancel)
    async def validation_cancel(self, query: Mapping, body, match: Mapping):
        return await self.backtest_cancel(query, body, match)

    # GET /v2/strategies/{id}/holdout
    async def holdout_get(self, query: Mapping, body, match: Mapping):
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        err, payload = await self._call(_holdout_status, match.get("id"))
        return err or (200, payload)

    # POST /v2/strategies/{id}/holdout {confirm}
    async def holdout_open(self, query: Mapping, body, match: Mapping):
        from ..backtest import jobs, runcard
        from ..lifecycle import registry as lc
        from ..validation import jobs as vjobs
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = _check_body(body, required={"confirm": str})
        if errors:
            return _bad(errors)
        mgr = jobs.manager()
        if mgr.active_run_id() is not None:
            return 409, {"error": vjobs.BUSY_MESSAGE, "run_id": mgr.active_run_id(), **_loop_busy(mgr.active_run_id())}
        try:
            run_id = await asyncio.to_thread(vjobs.service().submit_holdout, match.get("id"), body["confirm"])
        except jobs.JobBusy as ex:
            return 409, {"error": vjobs.BUSY_MESSAGE, "run_id": ex.run_id, **_loop_busy(ex.run_id)}
        except vjobs.HoldoutRefused as ex:
            return 409, {"error": ex.message, **ex.extra}
        except (runcard.NotFound, lc.NotFound):
            return 404, {"error": "Strategy or version not found."}
        except ValueError as ex:
            msg = str(ex)[:300]
            field = "confirm" if msg.startswith("confirm") else "_"
            return _bad({field: msg}, "The confirmation does not match the strategy name." if field == "confirm"
                        else "invalid input")
        except UserFacingError as ex:
            logger.warning("/v2/strategies/holdout: %s", ex)
            return user_error(ex)
        return 202, {"run_id": run_id}

    # ------------------------------------------------------------------ robustness (SPEC §10.4)

    async def _resume(self, query: Mapping, body, match: Mapping, *, kinds, not_found: str, busy: str):
        from ..backtest import jobs, runcard
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        if body is NO_BODY:
            body = {}
        errors = _check_body(body, optional=RESUME_BODY_OPTIONAL)
        if errors:
            return _bad(errors)
        run_id = match.get("id") or ""
        if not _ID_RE.fullmatch(run_id):
            return 404, {"error": not_found}
        err, card = await self._guarded(runcard.get, run_id)
        if err:
            return err
        if card is None or (kinds is not None and card.get("kind") not in kinds):
            return 404, {"error": not_found}
        mgr = jobs.manager()
        if mgr.active_run_id() is not None:
            return 409, {"error": busy, "run_id": mgr.active_run_id()}
        try:
            new_id = await asyncio.to_thread(lambda: jobs.resume(run_id, auto_resume=body.get("auto_resume")))
        except jobs.JobBusy as ex:
            return 409, {"error": busy, "run_id": ex.run_id, **_loop_busy(ex.run_id)}
        except runcard.ResumeRefused as ex:
            return 409, {"error": ex.message}
        except runcard.NotFound:
            return 404, {"error": not_found}
        except UserFacingError as ex:
            logger.warning("/v2 resume: %s", ex)
            return user_error(ex)
        return 202, {"run_id": new_id, "resumed_from": run_id}

    # POST /v2/backtests/{id}/resume {auto_resume?}  (any non-holdout run card; holdout -> 409)
    async def backtest_resume(self, query: Mapping, body, match: Mapping):
        from ..backtest import jobs
        return await self._resume(query, body, match, kinds=None, not_found=BT_NOT_FOUND, busy=jobs.BUSY_MESSAGE)

    # POST /v2/validations/{id}/resume {auto_resume?}
    async def validation_resume(self, query: Mapping, body, match: Mapping):
        from ..validation import jobs as vjobs
        return await self._resume(query, body, match, kinds=VAL_KINDS, not_found=VAL_NOT_FOUND,
                                  busy=vjobs.BUSY_MESSAGE)

    # GET /v2/settings/network
    async def network_settings_get(self, query: Mapping, body, match: Mapping):
        from ..data import network
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        err, payload = await self._guarded(network.get_settings)
        return err or (200, payload)

    # PUT /v2/settings/network {adaptive, fixed?}  (audited)
    async def network_settings_put(self, query: Mapping, body, match: Mapping):
        from ..data import network
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = _check_body(body, required={"adaptive": bool}, optional={"fixed": int})
        if not errors and "fixed" in body and not network.MIN_CONCURRENCY <= body["fixed"] <= network.MAX_CONCURRENCY:
            errors["fixed"] = f"a whole number {network.MIN_CONCURRENCY}..{network.MAX_CONCURRENCY}"
        if errors:
            return _bad(errors)
        err, payload = await self._guarded(_put_network_settings, body["adaptive"], body.get("fixed"))
        return err or (200, payload)

    # GET /v2/settings/gates
    async def gate_settings_get(self, query: Mapping, body, match: Mapping):
        from ..validation import gates
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        err, payload = await self._guarded(gates.get_settings)
        return err or (200, payload)

    # PUT /v2/settings/gates {preset, overrides?}
    async def gate_settings_put(self, query: Mapping, body, match: Mapping):
        from ..validation import gates
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = gates.check_settings(body)
        if errors:
            return _bad(errors)
        err, payload = await self._guarded(gates.put_settings, body, "user")
        return err or (200, payload)

    # --- SPEC §11.2 (X2): AI explanations of a run's attribution ---------------------------------

    async def _explain_call(self, fn, *args):
        """Runs an explain function (sync in a thread, or awaited); ExplainError -> (status, {error, code,
        …}), other UserFacingError -> user_error."""
        from ..backtest import explain
        try:
            if inspect.iscoroutinefunction(fn):
                return None, await fn(*args)
            return None, await asyncio.to_thread(fn, *args)
        except explain.ExplainError as ex:
            logger.warning("/v2 %s: %s (%s)", getattr(fn, "__name__", "call"), ex.user_message, ex.code)
            return (ex.status, ex.payload()), None
        except UserFacingError as ex:
            logger.warning("/v2 %s: %s", getattr(fn, "__name__", "call"), ex)
            return user_error(ex), None

    async def _explanations_create(self, query: Mapping, body, match: Mapping, route_kind: str):
        from ..backtest import explain
        not_found = VAL_NOT_FOUND if route_kind == "validation" else BT_NOT_FOUND
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        run_id = match.get("id") or ""
        if not _ID_RE.fullmatch(run_id):
            return 404, {"error": not_found, "code": "not_found"}
        errors = _check_body(body, required={"model": str, "language": str})
        if not errors:
            if body["model"] not in explain.TIERS:
                errors["model"] = f"one of {', '.join(explain.TIERS)}"
            if body["language"] not in explain.LANGUAGES:
                errors["language"] = f"one of {', '.join(explain.LANGUAGES)}"
        if errors:
            return _bad(errors)
        err, payload = await self._explain_call(explain.create, run_id, route_kind, body["model"], body["language"])
        return err or (200, payload)

    async def _explanations_list(self, query: Mapping, body, match: Mapping, route_kind: str):
        from ..backtest import explain
        not_found = VAL_NOT_FOUND if route_kind == "validation" else BT_NOT_FOUND
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        run_id = match.get("id") or ""
        if not _ID_RE.fullmatch(run_id):
            return 404, {"error": not_found, "code": "not_found"}
        err, payload = await self._explain_call(explain.list_for, run_id, route_kind)
        return err or (200, payload)

    # POST /v2/backtests/{id}/explanations {model: standard|premium, language: en|de}
    async def backtest_explanation_create(self, query: Mapping, body, match: Mapping):
        return await self._explanations_create(query, body, match, "backtest")

    # GET /v2/backtests/{id}/explanations
    async def backtest_explanations_list(self, query: Mapping, body, match: Mapping):
        return await self._explanations_list(query, body, match, "backtest")

    # POST /v2/validations/{id}/explanations {model, language}  (validation and holdout cards)
    async def validation_explanation_create(self, query: Mapping, body, match: Mapping):
        return await self._explanations_create(query, body, match, "validation")

    # GET /v2/validations/{id}/explanations
    async def validation_explanations_list(self, query: Mapping, body, match: Mapping):
        return await self._explanations_list(query, body, match, "validation")

    # GET /v2/settings/explain
    async def explain_settings_get(self, query: Mapping, body, match: Mapping):
        from ..backtest import explain
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        err, payload = await self._explain_call(explain.get_settings)
        return err or (200, payload)

    # PUT /v2/settings/explain {daily_budget_usd}  (audited explain_settings_changed)
    async def explain_settings_put(self, query: Mapping, body, match: Mapping):
        from ..backtest import explain
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = explain.check_settings(body)
        if errors:
            return _bad(errors)
        err, payload = await self._explain_call(explain.put_settings, body, "user")
        return err or (200, payload)

    # --- SPEC §13 (stage 5a): Strategy Studio + memory -------------------------------------------------

    @staticmethod
    def _limit_param(query: Mapping, default: int, maximum: int):
        raw = query.get("limit")
        if raw is None:
            return default, None
        if not isinstance(raw, str) or not raw.isdigit() or not 1 <= int(raw) <= maximum:
            return None, {"limit": f"an integer 1..{maximum}"}
        return int(raw), None

    # POST /v2/studio/sessions {title?}
    async def studio_session_create(self, query: Mapping, body, match: Mapping):
        from ..research import studio
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        body = {} if body is NO_BODY else body
        errors = _check_body(body, optional={"title": str})
        if not errors and "title" in body and (not body["title"].strip() or len(body["title"]) > studio.TITLE_MAX):
            errors["title"] = f"1..{studio.TITLE_MAX} characters"
        if errors:
            return _bad(errors)
        title = body["title"].strip() if "title" in body else None
        err, payload = await self._explain_call(studio.create_session, title)
        return err or (200, payload)

    # GET /v2/studio/sessions?limit=
    async def studio_sessions_list(self, query: Mapping, body, match: Mapping):
        from ..research import studio
        extra = {k: "unknown parameter" for k in query if k != "limit"}
        limit, lerr = self._limit_param(query, 50, 200)
        if extra or lerr:
            return _bad(dict(extra, **(lerr or {})))
        err, payload = await self._explain_call(studio.list_sessions, limit)
        return err or (200, payload)

    # GET /v2/studio/sessions/{id}
    async def studio_session_get(self, query: Mapping, body, match: Mapping):
        from ..research import studio
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        sid = match.get("id") or ""
        if not _ID_RE.fullmatch(sid):
            return 404, {"error": studio.NOT_FOUND_MESSAGE, "code": "not_found"}
        err, payload = await self._explain_call(studio.get_session, sid)
        return err or (200, payload)

    # POST /v2/studio/sessions/{id}/messages {text, model}
    async def studio_message_create(self, query: Mapping, body, match: Mapping):
        from ..research import prompts, studio
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        sid = match.get("id") or ""
        if not _ID_RE.fullmatch(sid):
            return 404, {"error": studio.NOT_FOUND_MESSAGE, "code": "not_found"}
        errors = _check_body(body, required={"text": str, "model": str})
        if not errors:
            if not body["text"].strip() or len(body["text"]) > prompts.USER_TEXT_MAX:
                errors["text"] = f"1..{prompts.USER_TEXT_MAX:,} characters"
            if body["model"] not in studio.TIERS:
                errors["model"] = f"one of {', '.join(studio.TIERS)}"
        if errors:
            return _bad(errors)
        err, payload = await self._explain_call(studio.send_message, sid, body["text"], body["model"])
        return err or (200, payload)

    # POST /v2/studio/sessions/{id}/drafts/{message_id}/cancel  (no body)
    async def studio_draft_cancel(self, query: Mapping, body, match: Mapping):
        from ..research import studio
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        if body is not NO_BODY and body != {}:
            return _bad({"_": "no body expected"})
        sid, mid = match.get("id") or "", match.get("message_id") or ""
        if not _ID_RE.fullmatch(sid) or not mid.isdigit() or len(mid) > 12:
            return 404, {"error": "Draft not found.", "code": "not_found"}
        err, payload = await self._explain_call(studio.cancel_draft, sid, int(mid))
        return err or (200, payload)

    # POST /v2/studio/sessions/{id}/drafts/{message_id}/save {confirm: true}  (SPEC §13.5 review M1)
    async def studio_draft_save(self, query: Mapping, body, match: Mapping):
        from ..research import studio
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        if body is NO_BODY or body is None:
            body = {}
        errors = _check_body(body, optional={"confirm": bool})        # fix1023: confirm accepted and ignored
        if errors:
            return _bad(errors)
        sid, mid = match.get("id") or "", match.get("message_id") or ""
        if not _ID_RE.fullmatch(sid) or not mid.isdigit() or len(mid) > 12:
            return 404, {"error": "Draft not found.", "code": "not_found"}
        err, payload = await self._explain_call(studio.save_draft, sid, int(mid))
        return err or (200, payload)

    # GET /v2/studio/operator-requests?session_id=
    async def studio_operator_requests(self, query: Mapping, body, match: Mapping):
        from ..research import studio
        extra = {k: "unknown parameter" for k in query if k != "session_id"}
        sid = query.get("session_id")
        if sid is not None and not _ID_RE.fullmatch(sid):
            extra["session_id"] = "a session id"
        if extra:
            return _bad(extra)
        err, payload = await self._explain_call(studio.operator_requests, sid)
        return err or (200, payload)

    # GET /v2/settings/ideation
    async def ideation_settings_get(self, query: Mapping, body, match: Mapping):
        from ..research import studio
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        err, payload = await self._explain_call(studio.get_settings)
        return err or (200, payload)

    # PUT /v2/settings/ideation {monthly_budget_usd, daily_cap_usd}  (audited ideation_settings_changed)
    async def ideation_settings_put(self, query: Mapping, body, match: Mapping):
        from ..research import studio
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = studio.check_settings(body)
        if errors:
            return _bad(errors)
        err, payload = await self._explain_call(studio.put_settings, body, "user")
        return err or (200, payload)

    # GET /v2/memory?family_id=&strategy_id=&limit=
    async def memory_list(self, query: Mapping, body, match: Mapping):
        from ..research import studio
        extra = {k: "unknown parameter" for k in query if k not in ("family_id", "strategy_id", "limit")}
        for k in ("family_id", "strategy_id"):
            if query.get(k) is not None and not _ID_RE.fullmatch(query[k]):
                extra[k] = "an id"
        limit, lerr = self._limit_param(query, 200, 500)
        if extra or lerr:
            return _bad(dict(extra, **(lerr or {})))
        err, payload = await self._explain_call(studio.memory_list, query.get("family_id"), query.get("strategy_id"),
                                                limit)
        return err or (200, payload)


    # --- SPEC §14 (stage 5b): papers -> strategy + replication, investment committee -----------------------

    # POST /v2/papers  (multipart part "file" = PDF | {text, title?} | {url})
    async def papers_create(self, query: Mapping, body, match: Mapping):
        from ..research import paper_strategy, papers
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        if isinstance(body, dict) and "_pdf" in body:
            err, p = await self._explain_call(papers.add_pdf, body["_pdf"], body.get("_filename"))
        elif isinstance(body, dict) and "_multipart_error" in body:
            return 400, {"error": body["_multipart_error"], "code": body.get("_code", "pdf_invalid")}
        else:
            if not isinstance(body, dict) or (("text" in body) == ("url" in body)):
                return _bad({"_": "expected multipart/form-data with a part 'file', or a JSON object {text, title?} "
                                  "or {url}"})
            if "url" in body:
                errors = _check_body(body, required={"url": str})
                if errors:
                    return _bad(errors)
                err, p = await self._explain_call(papers.add_url, body["url"])
            else:
                errors = _check_body(body, required={"text": str}, optional={"title": str})
                if not errors and (not body["text"].strip() or len(body["text"]) > papers.MAX_TEXT_CHARS):
                    errors["text"] = f"1..{papers.MAX_TEXT_CHARS:,} characters"
                if not errors and "title" in body and len(body["title"]) > papers.TITLE_MAX:
                    errors["title"] = f"at most {papers.TITLE_MAX} characters"
                if errors:
                    return _bad(errors)
                err, p = await self._explain_call(papers.add_text, body["text"], body.get("title"))
        if err:
            return err
        err, out = await self._explain_call(paper_strategy.paper_out, p)
        return err or (200, out)

    # GET /v2/papers?limit=
    async def papers_list(self, query: Mapping, body, match: Mapping):
        from ..research import paper_strategy
        extra = {k: "unknown parameter" for k in query if k != "limit"}
        limit, lerr = self._limit_param(query, 50, 200)
        if extra or lerr:
            return _bad(dict(extra, **(lerr or {})))
        err, payload = await self._explain_call(paper_strategy.list_papers, limit)
        return err or (200, payload)

    def _paper_id(self, match: Mapping):
        pid = match.get("id") or ""
        return pid if _ID_RE.fullmatch(pid) else None

    # GET /v2/papers/{id}
    async def paper_get(self, query: Mapping, body, match: Mapping):
        from ..research import paper_strategy
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        pid = self._paper_id(match)
        if pid is None:
            return 404, {"error": "Paper not found.", "code": "not_found"}
        err, payload = await self._explain_call(paper_strategy.get_paper, pid)
        return err or (200, payload)

    # POST /v2/papers/{id}/extract {model}
    async def paper_extract(self, query: Mapping, body, match: Mapping):
        from ..research import paper_strategy, studio
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = _check_body(body, required={"model": str})
        if not errors and body["model"] not in studio.TIERS:
            errors["model"] = "one of standard, premium"
        if errors:
            return _bad(errors)
        pid = self._paper_id(match)
        if pid is None:
            return 404, {"error": "Paper not found.", "code": "not_found"}
        err, payload = await self._explain_call(paper_strategy.extract, pid, body["model"])
        return err or (200, payload)

    # POST /v2/papers/{id}/confirm {extraction_id, model, sample_start, sample_end, reported_sharpe, …}
    async def paper_confirm(self, query: Mapping, body, match: Mapping):
        from ..research import paper_strategy
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = paper_strategy.check_confirm_body(body)
        if errors:
            return _bad(errors)
        pid = self._paper_id(match)
        if pid is None:
            return 404, {"error": "Paper not found.", "code": "not_found"}
        err, payload = await self._explain_call(paper_strategy.confirm, pid, body)
        return err or (200, payload)

    # GET /v2/papers/{id}/replication
    async def paper_replication(self, query: Mapping, body, match: Mapping):
        from ..research import paper_strategy
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        pid = self._paper_id(match)
        if pid is None:
            return 404, {"error": "Paper not found.", "code": "not_found"}
        err, payload = await self._explain_call(paper_strategy.get_replication, pid)
        return err or (200, payload)

    @staticmethod
    def _committee_target(src: Mapping, errors: dict, *, from_query: bool):
        sid = src.get("strategy_id")
        if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
            errors["strategy_id"] = "a strategy id"
        version = src.get("version")
        if from_query and version is not None:
            version = _version_param(version)
            if version is None:
                errors["version"] = "a version number ≥ 1"
        elif version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
            errors["version"] = "a version number ≥ 1"
        vid = src.get("validation_run_id")
        if vid is not None and (not isinstance(vid, str) or not _ID_RE.fullmatch(vid)):
            errors["validation_run_id"] = "a run id"
        if src.get("model") not in ("standard", "premium"):
            errors["model"] = "one of standard, premium"
        return sid, version, vid, src.get("model")

    # POST /v2/committee {strategy_id, version?, validation_run_id?, model}
    async def committee_create(self, query: Mapping, body, match: Mapping):
        from ..research import committee
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = _check_body(body, required={"strategy_id": str, "model": str},
                             optional={"version": int, "validation_run_id": str})
        if errors:
            return _bad(errors)
        sid, version, vid, model = self._committee_target(body, errors, from_query=False)
        if errors:
            return _bad(errors)
        err, payload = await self._explain_call(committee.start, sid, version, vid, model, "manual")
        return err or (202, {"run": payload})

    # GET /v2/committee/estimate?strategy_id=&version=&validation_run_id=&model=
    async def committee_estimate(self, query: Mapping, body, match: Mapping):
        from ..research import committee
        errors = {k: "unknown parameter" for k in query if k not in ("strategy_id", "version", "validation_run_id",
                                                                     "model")}
        sid, version, vid, model = self._committee_target(query, errors, from_query=True)
        if errors:
            return _bad(errors)
        err, payload = await self._explain_call(committee.estimate, sid, version, vid, model)
        return err or (200, payload)

    # GET /v2/committee?strategy_id=&validation_run_id=&limit=
    async def committee_list(self, query: Mapping, body, match: Mapping):
        from ..research import committee
        errors = {k: "unknown parameter" for k in query if k not in ("strategy_id", "validation_run_id", "limit")}
        for k in ("strategy_id", "validation_run_id"):
            if query.get(k) is not None and not _ID_RE.fullmatch(query[k]):
                errors[k] = "an id"
        limit, lerr = self._limit_param(query, 50, 200)
        if errors or lerr:
            return _bad(dict(errors, **(lerr or {})))
        err, payload = await self._explain_call(committee.list_runs, query.get("strategy_id"),
                                                query.get("validation_run_id"), limit)
        return err or (200, payload)

    # GET /v2/committee/{id}
    async def committee_get(self, query: Mapping, body, match: Mapping):
        from ..research import committee
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        rid = match.get("id") or ""
        if not _ID_RE.fullmatch(rid):
            return 404, {"error": "Committee run not found.", "code": "not_found"}
        err, payload = await self._explain_call(committee.get_run, rid)
        return err or (200, payload)

    # POST /v2/committee/{id}/cancel  (no body)
    async def committee_cancel(self, query: Mapping, body, match: Mapping):
        from ..research import committee
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        if body is not NO_BODY and body != {}:
            return _bad({"_": "no body expected"})
        rid = match.get("id") or ""
        if not _ID_RE.fullmatch(rid):
            return 404, {"error": "Committee run not found.", "code": "not_found"}
        err, payload = await self._explain_call(committee.cancel, rid)
        return err or (200, {"run": payload})

    # GET /v2/settings/committee
    async def committee_settings_get(self, query: Mapping, body, match: Mapping):
        from ..research import committee
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        err, payload = await self._explain_call(committee.get_settings)
        return err or (200, payload)

    # PUT /v2/settings/committee {auto_after_validation, model}  (audited committee_settings_changed)
    async def committee_settings_put(self, query: Mapping, body, match: Mapping):
        from ..research import committee
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = committee.check_settings(body)
        if errors:
            return _bad(errors)
        err, payload = await self._explain_call(committee.put_settings, body, "user")
        return err or (200, payload)

    # --- SPEC §16 (stage 6): discovery loop ------------------------------------------------------------------

    @staticmethod
    def _loop_id(match: Mapping):
        lid = match.get("id") or ""
        return lid if _ID_RE.fullmatch(lid) and lid.startswith("lr-") else None

    # POST /v2/loops {seed, variants?, generations?, search_points?, model?, history_from?, auto_resume?}
    async def loop_create(self, query: Mapping, body, match: Mapping):
        from ..discovery import loop
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = loop.check_config(body)
        if errors:
            return _bad(errors)
        err, payload = await self._explain_call(loop.start, body)
        return err or (202, payload)

    # GET /v2/loops?limit=
    async def loops_list(self, query: Mapping, body, match: Mapping):
        from ..discovery import loop
        extra = {k: "unknown parameter" for k in query if k != "limit"}
        limit, lerr = self._limit_param(query, 50, 200)
        if extra or lerr:
            return _bad(dict(extra, **(lerr or {})))
        err, payload = await self._explain_call(loop.list_loops, limit)
        return err or (200, payload)

    # GET /v2/loops/estimate?strategy_id=&idea=&variants=&generations=&search_points=&model=
    async def loop_estimate(self, query: Mapping, body, match: Mapping):
        from ..discovery import loop
        allowed = ("strategy_id", "idea", "variants", "generations", "search_points", "model", "history_from")
        errors = {k: "unknown parameter" for k in query if k not in allowed}
        hist = query.get("history_from")
        if hist is not None and hist not in ("2016", "2000"):
            errors["history_from"] = "2016 or 2000"
        vals = {}
        for k, lo, hi, d in (("variants", loop.VARIANTS_MIN, loop.VARIANTS_MAX, loop.VARIANTS_DEFAULT),
                             ("generations", loop.GENERATIONS_MIN, loop.GENERATIONS_MAX, loop.GENERATIONS_DEFAULT),
                             ("search_points", loop.SEARCH_MIN, loop.SEARCH_MAX, loop.SEARCH_DEFAULT)):
            raw = query.get(k)
            if raw is None:
                vals[k] = d
            elif not raw.isdigit() or not lo <= int(raw) <= hi:
                errors[k] = f"an integer {lo}..{hi}"
            else:
                vals[k] = int(raw)
        sid = query.get("strategy_id")
        if sid is not None and not _ID_RE.fullmatch(sid):
            errors["strategy_id"] = "a strategy id"
        model = query.get("model", "standard")
        if model not in ("standard", "premium"):
            errors["model"] = "one of standard, premium"
        idea = query.get("idea", "false")
        if idea not in ("true", "false"):
            errors["idea"] = "true or false"
        if errors:
            return _bad(errors)
        err, payload = await self._explain_call(loop.estimate, sid, vals["variants"], vals["generations"],
                                                vals["search_points"], model, idea == "true", hist)
        return err or (200, payload)

    # GET /v2/loops/{id}?events_after=
    async def loop_get(self, query: Mapping, body, match: Mapping):
        from ..discovery import loop
        errors = {k: "unknown parameter" for k in query if k != "events_after"}
        after = query.get("events_after")
        if after is not None and (not after.isdigit() or len(after) > 12):
            errors["events_after"] = "an event id (integer)"
        if errors:
            return _bad(errors)
        lid = self._loop_id(match)
        if lid is None:
            return 404, {"error": loop.NOT_FOUND_MESSAGE, "code": "not_found"}
        err, payload = await self._explain_call(loop.get_loop, lid, int(after) if after is not None else None)
        return err or (200, payload)

    async def _loop_action(self, query: Mapping, body, match: Mapping, fn_name: str):
        from ..discovery import loop
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        if body is not NO_BODY and body != {}:
            return _bad({"_": "no body expected"})
        lid = self._loop_id(match)
        if lid is None:
            return 404, {"error": loop.NOT_FOUND_MESSAGE, "code": "not_found"}
        err, payload = await self._explain_call(getattr(loop, fn_name), lid)
        return err or (200, payload)

    # POST /v2/loops/{id}/cancel | /pause | /resume  (no body)
    async def loop_cancel(self, query: Mapping, body, match: Mapping):
        return await self._loop_action(query, body, match, "cancel")

    async def loop_pause(self, query: Mapping, body, match: Mapping):
        return await self._loop_action(query, body, match, "pause")

    async def loop_resume(self, query: Mapping, body, match: Mapping):
        return await self._loop_action(query, body, match, "resume")

    # POST /v2/loops/{id}/finalists/{variant_id}/approve (no body) | /reject {reason?}
    async def _loop_finalist(self, query: Mapping, body, match: Mapping, approve: bool):
        from ..discovery import loop
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        lid = self._loop_id(match)
        vid = match.get("variant_id") or ""
        if lid is None or not _ID_RE.fullmatch(vid) or not vid.startswith("lv-"):
            return 404, {"error": "Finalist not found.", "code": "not_found"}
        if approve:
            if body is not NO_BODY and body != {}:
                return _bad({"_": "no body expected"})
            err, payload = await self._explain_call(loop.approve, lid, vid)
        else:
            body = {} if body is NO_BODY else body
            errors = _check_body(body, optional={"reason": str})
            if not errors and "reason" in body and len(body["reason"]) > 500:
                errors["reason"] = "at most 500 characters"
            if errors:
                return _bad(errors)
            reason = body.get("reason", "").strip() or None
            err, payload = await self._explain_call(loop.reject, lid, vid, reason)
        return err or (200, payload)

    async def loop_finalist_approve(self, query: Mapping, body, match: Mapping):
        return await self._loop_finalist(query, body, match, True)

    async def loop_finalist_reject(self, query: Mapping, body, match: Mapping):
        return await self._loop_finalist(query, body, match, False)

    # GET /v2/settings/loop
    async def loop_settings_get(self, query: Mapping, body, match: Mapping):
        from ..discovery import loop
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        err, payload = await self._explain_call(loop.get_settings)
        return err or (200, payload)

    # PUT /v2/settings/loop {autonomous?, monthly_usd?, daily_usd?, per_run_usd?}  (audited)
    async def loop_settings_put(self, query: Mapping, body, match: Mapping):
        from ..discovery import loop
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        errors = loop.check_settings(body)
        if errors:
            return _bad(errors)
        err, payload = await self._explain_call(loop.put_settings, body, "user")
        return err or (200, payload)


ROUTES = (
    ("GET", "/v2/settings/keys", "keys", False),
    ("POST", "/v2/settings/keys/changed", "keys_changed", True),
    ("GET", "/v2/data/sources", "data_sources", False),
    ("POST", "/v2/data/test", "data_test", True),
    # stage 2 (SPEC §7.11)
    ("GET", "/v2/dsl/operators", "dsl_operators", False),
    ("GET", "/v2/universes", "universes", False),
    ("GET", "/v2/strategies", "strategies_list", False),
    ("POST", "/v2/strategies/validate", "strategy_validate", True),
    ("POST", "/v2/strategies", "strategy_create", True),
    ("GET", "/v2/strategies/{id}", "strategy_get", False),
    ("PUT", "/v2/strategies/{id}", "strategy_update", True),
    ("POST", "/v2/strategies/{id}/duplicate", "strategy_duplicate", True),
    ("POST", "/v2/strategies/{id}/transition", "strategy_transition", True),
    ("POST", "/v2/strategies/{id}/preview", "strategy_preview", True),
    ("GET", "/v2/strategies/{id}/versions/{v}", "strategy_version", False),
    ("GET", "/v2/strategies/{id}/versions/{v}/export", "strategy_export", False),
    # stage 3 (SPEC §8.5)
    ("POST", "/v2/backtests", "backtest_create", True),
    ("GET", "/v2/backtests", "backtests_list", False),
    ("GET", "/v2/backtests/{id}", "backtest_get", False),
    ("POST", "/v2/backtests/{id}/cancel", "backtest_cancel", True),
    # stage 4 (SPEC §9.4, §9.5)
    ("POST", "/v2/validations", "validation_create", True),
    ("GET", "/v2/validations", "validations_list", False),
    ("GET", "/v2/validations/{id}", "validation_get", False),
    ("POST", "/v2/validations/{id}/exceptions", "validation_exception", True),
    ("POST", "/v2/validations/{id}/cancel", "validation_cancel", True),
    ("GET", "/v2/strategies/{id}/holdout", "holdout_get", False),
    ("POST", "/v2/strategies/{id}/holdout", "holdout_open", True),
    ("GET", "/v2/settings/gates", "gate_settings_get", False),
    ("PUT", "/v2/settings/gates", "gate_settings_put", True),
    # robustness (SPEC §10.4)
    ("POST", "/v2/backtests/{id}/resume", "backtest_resume", True),
    ("POST", "/v2/validations/{id}/resume", "validation_resume", True),
    ("GET", "/v2/settings/network", "network_settings_get", False),
    ("PUT", "/v2/settings/network", "network_settings_put", True),
    # explain (SPEC §11.2, X2)
    ("POST", "/v2/backtests/{id}/explanations", "backtest_explanation_create", True),
    ("GET", "/v2/backtests/{id}/explanations", "backtest_explanations_list", False),
    ("POST", "/v2/validations/{id}/explanations", "validation_explanation_create", True),
    ("GET", "/v2/validations/{id}/explanations", "validation_explanations_list", False),
    ("GET", "/v2/settings/explain", "explain_settings_get", False),
    ("PUT", "/v2/settings/explain", "explain_settings_put", True),
    # Strategy Studio + memory (SPEC §13, stage 5a)
    ("POST", "/v2/studio/sessions", "studio_session_create", True),
    ("GET", "/v2/studio/sessions", "studio_sessions_list", False),
    ("GET", "/v2/studio/sessions/{id}", "studio_session_get", False),
    ("POST", "/v2/studio/sessions/{id}/messages", "studio_message_create", True),
    ("POST", "/v2/studio/sessions/{id}/drafts/{message_id}/cancel", "studio_draft_cancel", True),
    ("POST", "/v2/studio/sessions/{id}/drafts/{message_id}/save", "studio_draft_save", True),
    ("GET", "/v2/studio/operator-requests", "studio_operator_requests", False),
    ("GET", "/v2/settings/ideation", "ideation_settings_get", False),
    ("PUT", "/v2/settings/ideation", "ideation_settings_put", True),
    ("GET", "/v2/memory", "memory_list", False),
    # papers + committee (SPEC §14, stage 5b)
    ("POST", "/v2/papers", "papers_create", "paper"),
    ("GET", "/v2/papers", "papers_list", False),
    ("GET", "/v2/papers/{id}", "paper_get", False),
    ("POST", "/v2/papers/{id}/extract", "paper_extract", True),
    ("POST", "/v2/papers/{id}/confirm", "paper_confirm", True),
    ("GET", "/v2/papers/{id}/replication", "paper_replication", False),
    ("POST", "/v2/committee", "committee_create", True),
    ("GET", "/v2/committee/estimate", "committee_estimate", False),
    ("GET", "/v2/committee", "committee_list", False),
    ("GET", "/v2/committee/{id}", "committee_get", False),
    ("POST", "/v2/committee/{id}/cancel", "committee_cancel", True),
    ("GET", "/v2/settings/committee", "committee_settings_get", False),
    ("PUT", "/v2/settings/committee", "committee_settings_put", True),
    # discovery loop (SPEC §16, stage 6)
    ("POST", "/v2/loops", "loop_create", True),
    ("GET", "/v2/loops", "loops_list", False),
    ("GET", "/v2/loops/estimate", "loop_estimate", False),
    ("GET", "/v2/loops/{id}", "loop_get", False),
    ("POST", "/v2/loops/{id}/cancel", "loop_cancel", True),
    ("POST", "/v2/loops/{id}/pause", "loop_pause", True),
    ("POST", "/v2/loops/{id}/resume", "loop_resume", True),
    ("POST", "/v2/loops/{id}/finalists/{variant_id}/approve", "loop_finalist_approve", True),
    ("POST", "/v2/loops/{id}/finalists/{variant_id}/reject", "loop_finalist_reject", True),
    ("GET", "/v2/settings/loop", "loop_settings_get", False),
    ("PUT", "/v2/settings/loop", "loop_settings_put", True),
) + TRADING_ROUTES     # stage 7: /v2/portfolio/*, /v2/oms/*, /v2/incubation* (SPEC §17.10)
ROUTES = ROUTES + BROKER_ROUTES      # stage 8: /v2/brokers/* (SPEC §18.9)
ROUTES = ROUTES + MONITORING_ROUTES  # stage 9: /v2/monitoring/*, /v2/shadow/* (SPEC §19.10)
ROUTES = ROUTES + OPTIONS_ARCHIVE_ROUTES   # OS1: /v2/options-archive/* (SPEC §21.11, read-only + audit)
ROUTES = ROUTES + COUNCIL_ROUTES   # S10-C: /v2/council/* (SPEC §22.11)


async def read_paper_body(request):
    """POST /v2/papers: multipart/form-data with ONE part "file" (PDF, read in chunks, capped at 20 MB) ->
    {"_pdf": bytes, "_filename": str}; a too large / malformed upload -> {"_multipart_error", "_code"};
    anything else -> ``read_body`` (JSON)."""
    from ..research import papers
    ctype = (request.headers.get("Content-Type") or "").lower()
    if not ctype.startswith("multipart/form-data"):
        return await read_body(request)
    try:
        reader = await request.multipart()
        part = await reader.next()
        if part is None or getattr(part, "name", None) != "file":
            return {"_multipart_error": "expected one part named 'file' (the PDF)", "_code": "pdf_invalid"}
        data = bytearray()
        while True:
            chunk = await part.read_chunk(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > papers.MAX_PDF_BYTES:
                return {"_multipart_error": f"The PDF is larger than {papers.MAX_PDF_BYTES // (1024 * 1024)} MB.",
                        "_code": "too_large"}
        if await reader.next() is not None:
            return {"_multipart_error": "expected exactly one part named 'file'", "_code": "pdf_invalid"}
        return {"_pdf": bytes(data), "_filename": getattr(part, "filename", None)}
    except (ValueError, AssertionError, RuntimeError):
        return {"_multipart_error": "malformed multipart upload", "_code": "pdf_invalid"}


async def read_body(request):
    """Parsed JSON body, NO_BODY when empty; raises ValueError on bad JSON."""
    if not request.can_read_body:
        return NO_BODY
    text = await request.text()
    if not text.strip():
        return NO_BODY
    return json.loads(text)


# Sandbox profile (SPEC "Sandbox profile"): the main state payloads carry "mode": "normal" | "sandbox".
MODE_PATHS = frozenset({"/v2/portfolio/state", "/v2/oms/runtime", "/v2/incubation", "/v2/monitoring/overview",
                        "/v2/brokers", "/v2/council/state"})


def register(app) -> V2Api:
    """Adds the /v2/* routes to an aiohttp app (behind its middlewares)."""
    from aiohttp import web  # lazy: importable without aiohttp

    api = V2Api()
    too_large = getattr(web, "HTTPRequestEntityTooLarge", ())   # 413 from aiohttp's client_max_size

    import sandbox_profile

    def make(name: str, reads_body: bool, path: str = ""):
        handler = getattr(api, name)

        async def route(request):
            body = NO_BODY
            if reads_body:
                try:
                    body = await (read_paper_body(request) if reads_body == "paper" else
                                  read_csv_body(request) if reads_body == "csv" else read_body(request))
                except too_large:
                    return web.json_response({"error": "request body too large",
                                              "errors": {"_": f"at most {max_body_bytes()} bytes"}}, status=400)
                except ValueError:
                    return web.json_response({"error": "invalid JSON body", "errors": {"_": "invalid JSON body"}},
                                             status=400)
            try:
                status, payload = await handler(request.query, body, request.match_info)
            except UserFacingError as ex:
                status, payload = user_error(ex)
            except Exception:
                logger.exception("/v2 route %s failed", name)
                status, payload = 500, {"error": "internal error in the Strategy Platform (see backend.log)"}
            if path in MODE_PATHS and status == 200 and isinstance(payload, dict):
                payload = sandbox_profile.stamp(payload)           # "mode": normal | sandbox
            return web.json_response(payload, status=status, dumps=dumps)

        route.__name__ = f"v2_{name}"
        return route

    for method, path, name, reads_body in ROUTES:
        app.router.add_route(method, path, make(name, reads_body, path))
    return api
