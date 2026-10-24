"""Backtest run cards (SPEC §8.4, notes §8.10) — hedgefund.db table
``backtest_runs`` (storage v5).

Life of a card: ``create`` (status ``queued``: strategy, version, content
hash, spec snapshot, request, seed) -> ``mark_running`` -> ``finish``
(``done``: engine version, range, data snapshot, licences, cost model,
metrics, compact daily curve, trades summary, warnings, stress) | ``fail``
| ``cancel``. These status transitions are the ONLY updates; a finished
card is immutable and no card can be deleted (DB triggers, like
``strategy_versions``).

Stage 4 (SPEC §9.1/§9.2, V1 notes §9.8): every card has a ``kind``
(``backtest`` | ``validation`` | ``holdout``; ``holdout`` cards are created only
by the holdout job). ``create`` of a ``backtest``/``validation`` card refuses an
explicit request start/end inside the locked holdout window
(``validation.families.HoldoutWindowViolation``, HTTP 400) before anything is
stored. ``finish`` sets ``trial_number`` = the family's done-card count
including this card and raises the family's ``seen_until`` to the card's
``range.end`` — in the same transaction as the status change.

Error contract (§5.1): ``get`` -> None when unknown, lists -> ``[]``;
storage failures -> ``StorageUnavailable``; invalid arguments ->
``ValueError``; ``create`` for an unknown strategy/version -> ``NotFound``;
a transition from the wrong status (or of an unknown id) -> ``StatusConflict``.

Curve compaction: money rounded to cents, exposures / drawdown to 1e-6.

Resumable runs (SPEC §10.3, R2 notes): columns ``resumed_from``,
``resume_count``, ``error_kind`` (connection | interrupted | basis | error,
set by ``fail``), ``auto_resume``. ``create_resumed`` makes the NEW card of a
manual resume (the failed card stays as it is); ``resume_refusal`` says why a
card cannot be resumed (409 text); ``mark_waiting`` / ``mark_resumed`` note
the auto-resume state on a running card. ``CheckpointStore``: the
tamper-evident checkpoints of a run chain; ``ensure_basis`` writes/compares
the basis of the first start (``BasisChanged``).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import json
import math
import uuid
from typing import Any, Dict, List, Optional

from ..core import fixture
from ..core.errors import UserFacingError
from ..storage import db

ACTOR = "engine"
REQUEST_KEYS = ("start", "end", "benchmark", "capital", "stress", "history_from")   # history_from: 2026-10-09
SURVIVORSHIP_MESSAGE = ("The universe is today's symbol list: symbols that were delisted or merged before "
                        "today are missing from the history (survivorship bias — results can look better than "
                        "they would have been).")
JSON_COLUMNS = ("spec_json", "request_json", "range_json", "data_json", "licences_json", "cost_model_json",
                "metrics_json", "curve_json", "trades_json", "warnings_json", "stress_json",
                "attribution_json")                 # attribution_json: v9, SPEC §11.1 (X1, 2026-10-14)


class NotFound(Exception):
    """The strategy or version of a new run card does not exist."""


class StatusConflict(Exception):
    """The run card is not in the status the transition starts from (or does not exist)."""



class BasisChanged(UserFacingError):
    """A resumed run would not compute on the basis of its first start (SPEC §10.3) — not resumable."""

    def __init__(self, what: str):
        super().__init__(f"The basis changed ({what}) — start a new run.")
        self.what = what


class ResumeRefused(Exception):
    """The run cannot be resumed (HTTP 409) — ``message``."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _dumps(value) -> Optional[str]:
    if value is None:
        return None
    # allow_nan=False: a NaN/inf would be invalid JSON for the WPF client — surfaced, never hidden
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _loads(text, what: str):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise db.StorageUnavailable(f"the stored {what} of a backtest run card is unreadable") from None


def new_id() -> str:
    return "bt-" + uuid.uuid4().hex[:12]


def _field(result, key: str):
    return result.get(key) if isinstance(result, dict) else getattr(result, key)


def normalise_request(request: Optional[dict]) -> dict:
    request = dict(request or {})
    unknown = sorted(set(request) - set(REQUEST_KEYS))
    if unknown:
        raise ValueError(f"request: unknown field(s) {', '.join(unknown)}")
    return {k: request.get(k) for k in REQUEST_KEYS}


# --- writes ------------------------------------------------------------------------------------------

def create(strategy_id: str, version: Optional[int] = None, request: Optional[dict] = None, *,
           seed: Optional[int] = None, run_id: Optional[str] = None, kind: str = "backtest",
           auto_resume: bool = False) -> dict:
    """A new ``queued`` card for ``strategy_id`` at ``version`` (None = current) -> the card.
    ``seed`` defaults to ``stress.MC_SEED`` when ``request.stress`` is true (else NULL).
    ``kind`` backtest | validation | holdout; backtest/validation: an explicit start/end inside the
    holdout window -> ``HoldoutWindowViolation`` (nothing stored)."""
    from . import stress
    if kind not in db.RUN_KINDS:
        raise ValueError(f"kind: one of {', '.join(db.RUN_KINDS)}")
    row = db.strategy_get(strategy_id)
    if row is None:
        raise NotFound(f"Strategy {strategy_id} does not exist.")
    v = int(row["current_version"]) if version is None else version
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("version: an integer")
    ver = db.strategy_version_get(strategy_id, v)
    if ver is None:
        raise NotFound(f"Strategy {strategy_id} has no version {v}.")
    req = normalise_request(request)
    if kind != "holdout" and (req.get("start") is not None or req.get("end") is not None):
        from ..validation import families
        families.check_request_dates(_loads(ver["spec_json"], "spec"), req.get("start"), req.get("end"))
    if seed is None and req.get("stress"):
        seed = stress.MC_SEED
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ValueError("seed: an integer")
    now = db.utc_now_iso()
    rid = run_id or new_id()
    card = {"id": rid, "created_at": now, "updated_at": now, "strategy_id": strategy_id, "version": v,
            "content_hash": ver["content_hash"], "status": "queued", "spec_json": ver["spec_json"],
            "request_json": _dumps(req), "seed": seed, "trial_number": None, "kind": kind,
            "auto_resume": 1 if (auto_resume and kind != "holdout") else 0}
    db.backtest_run_insert(card, (ACTOR, "backtest_queued", {"run_id": rid, "strategy_id": strategy_id,
                                                             "version": v, "kind": kind}))
    return get(rid)


def _transition(run_id: str, frm: str, to: str, fields: dict, action: str) -> dict:
    ok = db.backtest_run_transition(run_id, frm, to, fields, (ACTOR, action, {"run_id": run_id, "status": to}))
    if not ok:
        cur = db.backtest_run_get(run_id)
        raise StatusConflict(f"Backtest {run_id} " + ("does not exist." if cur is None else
                                                       f"is {cur['status']}, not {frm}."))
    return get(run_id)


def mark_running(run_id: str) -> dict:
    return _transition(run_id, "queued", "running", {"started_at": db.utc_now_iso()}, "backtest_started")


def finish(run_id: str, result, metrics: dict, *, stress: Optional[dict] = None,
           attribution: Optional[dict] = None) -> dict:
    """running -> done with the payload built from the engine result (``build_payload``); sets
    ``trial_number`` and raises the family's seen_until to ``range.end`` (one transaction).
    ``attribution`` (SPEC §11.1): ``backtest.attribution.compute`` output, stored in ``attribution_json``
    (None = the column stays NULL — the API then reports why)."""
    fields = build_payload(result, metrics, stress, attribution=attribution)
    fields["finished_at"] = db.utc_now_iso()
    rng = _field(result, "range") or {}
    seen = rng.get("end")
    if seen is None:
        raise ValueError("result: range.end is missing")
    trial = db.backtest_run_finish(run_id, fields, seen_until=seen,
                                   audit=(ACTOR, "backtest_finished", {"run_id": run_id, "status": "done"}))
    if trial is None:
        cur = db.backtest_run_get(run_id)
        raise StatusConflict(f"Backtest {run_id} " + ("does not exist." if cur is None else
                                                       f"is {cur['status']}, not running."))
    return get(run_id)


def finish_holdout(run_id: str, result, metrics: dict, *, family_event: dict, stress: Optional[dict] = None,
                   attribution: Optional[dict] = None) -> dict:
    """running -> done for a ``holdout`` card AND its immutable holdout event in ONE transaction
    (review 2026-10-09: a crash can no longer burn the holdout without a visible result): trial_number,
    seen_until = max(…, range.end, window end), the holdout_events row and holdout_failed_at (set on
    'fail', cleared on 'pass'). ``family_event`` = {window: {start, end}, result: 'pass' | 'fail'}.
    -> the card + ``holdout_event_id``."""
    from ..validation import families
    if not isinstance(family_event, dict) or set(family_event) != {"window", "result"}:
        raise ValueError("family_event: {window: {start, end}, result: pass | fail}")
    cur = db.backtest_run_get(run_id)
    if cur is None or cur["status"] != "running":
        raise StatusConflict(f"Backtest {run_id} " + ("does not exist." if cur is None else
                                                       f"is {cur['status']}, not running."))
    if cur["kind"] != "holdout":
        raise ValueError("finish_holdout: the run card is not a holdout run")
    event = families.holdout_event_row(cur["strategy_id"], int(cur["version"]), family_event["window"],
                                       family_event["result"], run_id)
    fields = build_payload(result, metrics, stress, attribution=attribution)
    fields["finished_at"] = db.utc_now_iso()
    seen = (_field(result, "range") or {}).get("end")
    if seen is None:
        raise ValueError("result: range.end is missing")
    trial = db.backtest_run_finish(run_id, fields, seen_until=seen, holdout_event=event,
                                   audit=(ACTOR, "holdout_finished", {"run_id": run_id, "status": "done"}))
    if trial is None:
        now = db.backtest_run_get(run_id)
        raise StatusConflict(f"Backtest {run_id} " + ("does not exist." if now is None else
                                                       f"is {now['status']}, not running."))
    card = get(run_id)
    ev = [e for e in db.holdout_events_list(event["family_id"]) if e["run_id"] == run_id]
    card["holdout_event_id"] = ev[-1]["id"]
    return card


def fail(run_id: str, message: str, error_kind: Optional[str] = "error") -> dict:
    """queued|running -> failed with the user-facing ``message`` and ``error_kind`` (db.ERROR_KINDS;
    "connection" makes the card resumable, SPEC §10.3)."""
    return _end(run_id, "failed", message, "backtest_failed", error_kind=error_kind)


def cancel(run_id: str, message: str = "The backtest was cancelled.") -> dict:
    """queued|running -> cancelled."""
    return _end(run_id, "cancelled", message, "backtest_cancelled")


def _end(run_id: str, to: str, message: str, action: str, error_kind: Optional[str] = None) -> dict:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message: required")
    cur = db.backtest_run_get(run_id)
    if cur is None or cur["status"] not in ("queued", "running"):
        raise StatusConflict(f"Backtest {run_id} " + ("does not exist." if cur is None else
                                                       f"is already {cur['status']}."))
    fields = {"error": message.strip()[:2000], "finished_at": db.utc_now_iso()}
    if to == "failed":
        fields["error_kind"] = error_kind
    elif to == "cancelled":
        fields["error_kind"] = None         # a waiting run that is cancelled is not resumable
    return _transition(run_id, cur["status"], to, fields, action)


# --- payload -----------------------------------------------------------------------------------------

def _r(x, nd):
    return None if x is None or not math.isfinite(float(x)) else round(float(x), nd)


def compact_curve(result) -> dict:
    eq, bm = _field(result, "equity"), _field(result, "benchmark")
    lmv, smv = _field(result, "long_mv"), _field(result, "short_mv")
    gross, net = [], []
    for e, lo, sh in zip(eq, lmv, smv):
        if e is None or lo is None or sh is None or float(e) <= 0:
            gross.append(None)
            net.append(None)
        else:
            gross.append(_r((float(lo) + float(sh)) / float(e), 6))
            net.append(_r((float(lo) - float(sh)) / float(e), 6))
    return {"dates": list(_field(result, "dates")), "equity": [_r(x, 2) for x in eq],
            "benchmark": [_r(x, 2) for x in bm], "gross_exposure": gross, "net_exposure": net,
            "drawdown_pct": [_r(x, 6) for x in _field(result, "drawdown_pct")]}


# Market Research MR8 (market_research/SPEC.md §12): the card keeps the fills themselves (chart entry/exit
# markers), compact rows [ts, side, qty, fill_price] per symbol, chronological, at most CARD_FILLS_MAX in all
# (later fills dropped, "fills_truncated": true). Additive key; cards finished before have none.
CARD_FILLS_MAX = 10000


def _card_fills(fills) -> tuple:
    out: Dict[str, list] = {}
    for f in fills[:CARD_FILLS_MAX]:
        out.setdefault(f["symbol"], []).append([f.get("ts"), f.get("side"), _r(float(f.get("qty") or 0.0), 8),
                                                _r(float(f.get("fill_price") or 0.0), 6)])
    return {k: out[k] for k in sorted(out)}, len(fills) > CARD_FILLS_MAX


def trades_summary(result) -> dict:
    fills = _field(result, "fills") or []
    orders = _field(result, "orders") or []
    by: Dict[str, dict] = {}
    for f in fills:
        s = by.setdefault(f["symbol"], {"fills": 0, "buys": 0, "sells": 0, "notional": 0.0, "fees": 0.0})
        s["fills"] += 1
        s["buys" if f["side"] in ("buy", "cover") else "sells"] += 1
        s["notional"] += abs(float(f.get("notional") or 0.0))
        s["fees"] += float(f.get("fees") or 0.0)
    for s in by.values():
        s["notional"], s["fees"] = round(s["notional"], 2), round(s["fees"], 2)
    status: Dict[str, int] = {}
    for o in orders:
        status[str(o.get("status"))] = status.get(str(o.get("status")), 0) + 1
    card_fills, truncated = _card_fills(fills)
    return {"fills": len(fills), "orders": len(orders), "orders_by_status": status,
            "first_fill": fills[0]["ts"] if fills else None, "last_fill": fills[-1]["ts"] if fills else None,
            "by_symbol": {k: by[k] for k in sorted(by)}, "margin_events": len(_field(result, "events") or []),
            "stats": dict(_field(result, "stats") or {}),
            "fills_by_symbol": card_fills, "fills_truncated": truncated}


def licences_of(data: dict) -> dict:
    """{source: {licence, note}} for every source named in the data snapshot (data/licences.py)."""
    from ..data import licences
    sources = set()
    for entry in (data or {}).values():
        for key in ("all", "split", "bars", "corporate_actions"):
            info = entry.get(key) or {}
            sources.update(s for s in info.get("sources") or [] if s)
        vol = entry.get("volume") or {}
        if vol.get("source"):
            sources.add(vol["source"])
    return {s: licences.licence_for(s) for s in sorted(sources)}


def build_payload(result, metrics: dict, stress: Optional[dict] = None, *, attribution: Optional[dict] = None) -> dict:
    """Column values of a finished card (JSON encoded)."""
    data = _field(result, "data") or {}
    warnings = list(_field(result, "warnings") or [])
    if not any(w.get("code") == "survivorship" for w in warnings):
        warnings.insert(0, {"code": "survivorship", "message": SURVIVORSHIP_MESSAGE})
    return {"engine_version": _field(result, "engine_version"), "range_json": _dumps(_field(result, "range")),
            "data_json": _dumps(data), "licences_json": _dumps(licences_of(data)),
            "cost_model_json": _dumps({"costs": _field(result, "costs"),
                                       "broker_params": _field(result, "broker_params"),
                                       "benchmark": (_field(result, "benchmark_info") or {}).get("symbol")}),
            "metrics_json": _dumps(metrics), "curve_json": _dumps(compact_curve(result)),
            "trades_json": _dumps(trades_summary(result)), "warnings_json": _dumps(warnings),
            "stress_json": _dumps(stress), "attribution_json": _dumps(attribution)}


# --- reads -------------------------------------------------------------------------------------------

def _decode(row: dict) -> dict:
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if k in JSON_COLUMNS:
            out[k[:-5]] = _loads(v, k[:-5])
        else:
            out[k] = v
    return out


def get(run_id: str) -> Optional[dict]:
    """The full card (JSON decoded: spec, request, range, data, licences, cost_model, metrics, curve,
    trades, warnings, stress, attribution) or None."""
    row = db.backtest_run_get(run_id)
    if row is None:
        return None
    out = _decode(row)
    out["auto_resume"] = bool(out.get("auto_resume"))
    out.update(fixture.card_fields(out))          # sandbox fixture label (SPEC "Sandbox profile"; absent otherwise)
    if out.get("stress"):
        from . import stress as stress_mod
        stress_mod.with_summaries(out["stress"])        # click-test 2026-10-22 (stored cards too)
    return out


def list_runs(strategy_id: Optional[str] = None, *, version: Optional[int] = None, status: Optional[str] = None,
              limit: int = 50, kind: Optional[str] = None) -> List[dict]:
    """Newest first: {id, created_at, updated_at, started_at, finished_at, strategy_id, version,
    content_hash, status, request, range, key_metrics, error, kind, trial_number, resumed_from,
    resume_count, error_kind, auto_resume}."""
    from . import metrics as metrics_mod
    out = []
    for row in db.backtest_runs_list(strategy_id, version=version, status=status, limit=limit, kind=kind):
        d = _decode({k: v for k, v in row.items() if k != "metrics_json"})
        d["auto_resume"] = bool(d.get("auto_resume"))
        m = _loads(row["metrics_json"], "metrics") if row["metrics_json"] is not None else None
        d["key_metrics"] = metrics_mod.summary(m) if m is not None else None
        d.update(fixture.card_fields({"metrics": m}))      # sandbox fixture label (absent otherwise)
        out.append(d)
    return out


def latest_done(strategy_id: str, version: int, *, kind: Optional[str] = None) -> Optional[dict]:
    """The newest ``done`` card (list shape) of that strategy version (of ``kind``; None = any), or None."""
    rows = list_runs(strategy_id, version=int(version), status="done", limit=1, kind=kind)
    return rows[0] if rows else None


# --- resumable runs (SPEC §10.3, R2 notes) ------------------------------------------------------------

HOLDOUT_NOT_RESUMABLE = ("A holdout run cannot be resumed: a failed holdout run is final — re-running the window "
                         "with knowledge of a partial result would contaminate it (SPEC §10.3). The family's "
                         "holdout was not recorded, so it can be opened again.")
ERROR_KIND_TEXT = {"interrupted": "the backend stopped during the run", "basis": "its basis changed",
                   "error": "an error that waiting cannot fix", None: "an unknown error"}


def resumable(card: dict, successor: Optional[str] = None) -> bool:
    """Cheap check for lists/details (no basis check): failed, error kind connection, not a holdout run,
    not resumed yet."""
    return (card.get("status") == "failed" and card.get("error_kind") == "connection"
            and card.get("kind") != "holdout" and successor is None)


def successors(run_ids) -> Dict[str, str]:
    return db.backtest_run_successors(list(run_ids))


def mark_waiting(run_id: str) -> bool:
    """Auto-resume: the running card waits for the connection (error_kind 'connection' — a backend
    restart now leaves it resumable)."""
    return db.backtest_run_note(run_id, error_kind="connection",
                                audit=(ACTOR, "backtest_waiting", {"run_id": run_id}))


def mark_resumed(run_id: str) -> bool:
    """Auto-resume: the connection is back — resume_count + 1, error_kind cleared."""
    return db.backtest_run_note(run_id, error_kind=None, resume_count_increment=True,
                                audit=(ACTOR, "backtest_auto_resumed", {"run_id": run_id}))


class CheckpointStore:
    """Checkpoints of a run chain (the run, the run it resumed, …; SPEC §10.3). ``get(step)`` -> the
    payload of the NEWEST row of that step, verified: sha256 over run_id, step_key and payload must
    match, else None + a ``checkpoint_mismatch`` warning (the caller recomputes the step — no older row
    is used instead). ``put(step, payload)`` inserts a row for THIS run (insert-only table)."""

    def __init__(self, run_id: str, chain: Optional[List[str]] = None):
        self.run_id = run_id
        self.chain = list(chain) if chain is not None else db.backtest_run_chain(run_id)
        self._rows: Dict[str, dict] = {}
        for r in db.checkpoints_list(self.chain):
            self._rows[r["step_key"]] = r          # oldest first -> the newest wins
        self._bad: set = set()
        self.warnings: List[dict] = []
        self.reused: List[str] = []

    @staticmethod
    def digest(run_id: str, step_key: str, payload_json: str) -> str:
        import hashlib
        return hashlib.sha256(f"{run_id}\n{step_key}\n{payload_json}".encode("utf-8")).hexdigest()

    def get(self, step_key: str):
        row = self._rows.get(step_key)
        if row is None or step_key in self._bad:
            return None
        if self.digest(row["run_id"], row["step_key"], row["payload_json"]) != row["sha256"]:
            self._bad.add(step_key)
            self.warnings.append({"code": "checkpoint_mismatch", "step": step_key, "run_id": row["run_id"],
                                  "message": f"The checkpoint '{step_key}' of run {row['run_id']} failed its "
                                             "integrity check (sha256) — the step was recomputed."})
            return None
        try:
            payload = json.loads(row["payload_json"])
        except ValueError:
            self._bad.add(step_key)
            self.warnings.append({"code": "checkpoint_mismatch", "step": step_key, "run_id": row["run_id"],
                                  "message": f"The checkpoint '{step_key}' of run {row['run_id']} is unreadable "
                                             "— the step was recomputed."})
            return None
        if row["run_id"] != self.run_id or step_key not in self.reused:
            self.reused.append(step_key)
        return payload

    def put(self, step_key: str, payload) -> None:
        from ..validation.parallel import canonical_json
        text = canonical_json(payload)
        sha = self.digest(self.run_id, step_key, text)
        rid = db.checkpoint_insert(self.run_id, step_key, text, sha)
        self._rows[step_key] = {"id": rid, "run_id": self.run_id, "step_key": step_key, "payload_json": text,
                                "sha256": sha}
        self._bad.discard(step_key)


def current_basis(card: dict) -> dict:
    """The basis a run of ``card`` would compute on NOW: engine + validation report version, strategy
    version + content hash, request, the
    resolved end date (request end, else the training end = holdout start − 1 session) and the locked
    holdout window of the spec's universe."""
    from ..validation import families
    spec = card["spec"]
    req = card.get("request") or {}
    from ..validation import report
    from . import engine
    lock = families.lock_for_kind(families.universe_kind(spec))
    end = req.get("end") or lock["training_end"].isoformat()
    return {"engine_version": engine.ENGINE_VERSION, "report_version": report.REPORT_VERSION,
            "strategy_id": card["strategy_id"], "version": int(card["version"]),
            "content_hash": card["content_hash"], "kind": card.get("kind", "backtest"), "request": req,
            "resolved_end": str(end),
            "holdout_window": {"start": lock["start"].isoformat(), "end": lock["end"].isoformat()}}


def basis_problem(stored: Optional[dict], now: dict) -> Optional[str]:
    """Why ``now`` differs from the ``stored`` basis (None = identical)."""
    if stored is None:
        return "the record of the first start is missing or failed its integrity check"
    for key, what in (("engine_version", "engine"), ("report_version", "validation report")):
        if stored.get(key) != now[key]:            # review 2026-10-10: results of two versions never mix
            return f"the {what} was updated ({stored.get(key)} → {now[key]})"
    if (stored.get("strategy_id"), stored.get("version"), stored.get("content_hash")) != \
            (now["strategy_id"], now["version"], now["content_hash"]):
        return "the strategy version or its content hash differs"
    if stored.get("request") != now["request"]:
        return "the request differs"
    sw, nw = stored.get("holdout_window") or {}, now["holdout_window"]
    if sw != nw:
        return (f"the locked holdout window moved from {sw.get('start')} … {sw.get('end')} to "
                f"{nw['start']} … {nw['end']}")
    if stored.get("resolved_end") != now["resolved_end"]:
        return f"the end date resolved at the first start was {stored.get('resolved_end')}, now {now['resolved_end']}"
    return None


def ensure_basis(card: dict, store: CheckpointStore) -> dict:
    """First attempt: writes the ``basis`` checkpoint. A later attempt (auto-resume, resumed card): the
    stored basis must equal the current one, else ``BasisChanged``. Also checks that the stored version
    still has the card's content hash. -> the basis."""
    ver = db.strategy_version_get(card["strategy_id"], int(card["version"]))
    if ver is None or ver["content_hash"] != card["content_hash"]:
        raise BasisChanged("the strategy version or its content hash differs")
    now = current_basis(card)
    if card.get("resumed_from") is None and "basis" not in store._rows:
        store.put("basis", now)
        return now
    why = basis_problem(store.get("basis"), now)
    if why:
        raise BasisChanged(why)
    return now


def resume_refusal(card: Optional[dict], *, check_basis: bool = True) -> Optional[str]:
    """None when ``card`` may be resumed now, else the reason (HTTP 409 text, SPEC §10.3)."""
    if card is None:
        return "Run not found."
    if card.get("kind") == "holdout":
        return HOLDOUT_NOT_RESUMABLE
    if card.get("status") != "failed":
        return f"Only a failed run can be resumed — this run is {card.get('status')}."
    if card.get("error_kind") != "connection":
        return ("Only a run that failed because the connection was lost can be resumed — this run failed "
                f"because of {ERROR_KIND_TEXT.get(card.get('error_kind'), card.get('error_kind'))}.")
    succ = db.backtest_run_successors([card["id"]]).get(card["id"])
    if succ:
        return f"This run was already resumed as {succ}."
    if check_basis:
        ver = db.strategy_version_get(card["strategy_id"], int(card["version"]))
        if ver is None or ver["content_hash"] != card["content_hash"]:
            return "The basis changed (the strategy version or its content hash differs) — start a new run."
        store = CheckpointStore(card["id"])
        why = basis_problem(store.get("basis"), current_basis(card))
        if why:
            return f"The basis changed ({why}) — start a new run."
    return None


def create_resumed(run_id: str, *, auto_resume: Optional[bool] = None) -> dict:
    """The NEW queued card of a manual resume of the failed card ``run_id`` (``ResumeRefused`` when it
    cannot be resumed): same strategy, version, content hash, spec, request, seed and kind;
    ``resumed_from`` = run_id, ``resume_count`` = its count + 1, ``auto_resume`` = the given flag or the
    old card's. Its trial number is set when it finishes (the failed card never counted)."""
    prev = get(run_id)
    why = resume_refusal(prev)
    if why:
        raise ResumeRefused(why)
    flag = bool(prev.get("auto_resume")) if auto_resume is None else bool(auto_resume)
    raw = db.backtest_run_get(run_id)
    now = db.utc_now_iso()
    rid = new_id()
    row = {"id": rid, "created_at": now, "updated_at": now, "strategy_id": raw["strategy_id"],
           "version": int(raw["version"]), "content_hash": raw["content_hash"], "status": "queued",
           "spec_json": raw["spec_json"], "request_json": raw["request_json"], "seed": raw["seed"],
           "trial_number": None, "kind": raw["kind"], "resumed_from": run_id,
           "resume_count": int(raw["resume_count"] or 0) + 1, "auto_resume": 1 if flag else 0}
    db.backtest_run_insert(row, ("user", "backtest_resumed", {"run_id": rid, "resumed_from": run_id,
                                                             "kind": raw["kind"], "auto_resume": flag}))
    return get(rid)
