"""Lifecycle registry — strategies, immutable versions, transitions, library
seeding (SPEC §7.6, §7.10). Storage: hedgefund.db v2 (``storage/db.py``).

- Only VALID specs are stored (``validator.validate`` with 0 errors). The
  stored spec is the default-filled spec (``schema.with_defaults``) WITHOUT
  its ``provenance`` block: provenance claims in a text never set the
  envelope — they are kept verbatim (≤ ``MAX_CLAIMED_BYTES``) in
  ``envelope.claimed`` and shown as "claimed by the file" only.
- Versions are insert-only (DB triggers refuse UPDATE/DELETE). Editing
  creates version N+1 (optimistic concurrency on ``base_version``); a
  strategy above ``draft`` returns to ``draft`` (its evidence belonged to the
  old version). Library strategies are read-only.
- Stage-2 transition rules: user may move ``idea ↔ draft``, any state →
  ``retired``, ``retired → draft``. Stage 3 (SPEC §8.4): ``draft →
  backtested`` when a ``done`` backtest run card of the CURRENT version
  exists (``backtest/runcard.py``; its id is the transition's evidence);
  higher states need evidence of later stages; ``live``/``scaled`` also the
  live guard (stage 8). Stage 4 (SPEC §9.5, V1 notes §9.8): ``backtested →
  candidate`` needs the LATEST done validation run card (kind ``validation``)
  of the current version to pass (``validation.report.effective_passed(card)``
  — the raw ``metrics_json.validation.passed`` merged with the gate exceptions);
  ``candidate → validated`` needs the latest done holdout run card (kind
  ``holdout``) of the current version with ``metrics_json.holdout.passed`` true
  (no exception possible); a family with ``holdout_failed_at`` set cannot move
  to ``validated``/``incubation``. Every strategy belongs to a family
  (``family_id``: own id, or the parent's family for duplicates and verified
  imports of an export — ``db.family_parent_of`` — or the family of an
  existing strategy with the same logic hash, ``db.logic_hash_of``). A reason (1..500 chars) is required; every change writes a
  transition row and an audit row in the same transaction.
- Library seeding (``seed_library``): idempotent by ``library_key`` +
  content hash; a changed library spec becomes a new version (actor system).
  The outcome is kept in ``seed_status()`` (surfaced by the API) — a failure
  is never hidden.

Errors: ``SpecInvalid`` (carries the ValidationResult dict), ``NotFound``,
``Conflict`` (409: stale base version, read-only library strategy, refused
transition), ``ValueError`` for bad arguments; storage failures raise
``StorageUnavailable``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..core.errors import UserFacingError
from ..storage import db

logger = logging.getLogger(__name__)

STATES = ("idea", "draft", "backtested", "candidate", "validated", "incubation", "live", "scaled", "retired")
ORIGINS = ("library", "user", "import", "duplicate", "studio", "paper", "loop")   # studio: §13 (5a), paper: §14 (5b), loop: §16 (6)
EVIDENCE_STATES = ("backtested", "candidate", "validated", "incubation", "live", "scaled")
LIVE_STATES = ("live", "scaled")
MAX_REASON_LEN = 500
MAX_CLAIMED_BYTES = 16384
MAX_FILE_NAME_LEN = 120
READ_ONLY_MESSAGE = "Library strategies are read-only — duplicate them first."
NEEDS_RUN_CARD = ("needs evidence from a later stage — stage 4 allows draft → backtested → candidate → "
                  "validated")
ONLY_FROM_BACKTESTED = "only a backtested strategy can become a candidate"
ONLY_FROM_CANDIDATE = "only a candidate can become validated"
NEEDS_PASSING_VALIDATION = "needs a passing validation run of version {version} — run Validate…"
LATEST_VALIDATION_FAILED = ("the latest validation run of version {version} ({run_id}) did not pass all gates "
                            "G1–G8 — fix the strategy, add exceptions for failed gates or validate again")
VALIDATION_RUN_NOT_EVIDENCE = ("validation run {run_id} is not the latest finished validation run of version "
                               "{version} of this strategy")
NEEDS_PASSING_HOLDOUT = "needs a passing holdout run of version {version} — {availability}"
HOLDOUT_AVAILABLE_NOW = "the holdout can be opened now (once per strategy family)"
HOLDOUT_AVAILABLE_FROM = "the holdout can be opened from {date}: {reason}"
LATEST_HOLDOUT_FAILED = "the holdout run {run_id} of version {version} did not pass"
HOLDOUT_RUN_NOT_EVIDENCE = ("holdout run {run_id} is not the latest finished holdout run of version {version} of "
                            "this strategy")
HOLDOUT_FAILED_LOCK = ("the strategy family failed the holdout on {date} — locked until {until} (≥ 12 new unseen "
                       "months), then one re-test on those months only")
NEEDS_FINISHED_BACKTEST = "needs a finished backtest of version {version}"
ONLY_FROM_DRAFT = "only a draft can become backtested"
RUN_NOT_EVIDENCE = "backtest {run_id} is not a finished backtest of version {version} of this strategy"
NEEDS_LIVE_GUARD = ("live trading is off — the live guard comes with stage 8 and a live step only on your explicit "
                    "go (stage 12)")
# stage 7 (SPEC §17.2): incubation only through an incubation run (its length is fixed at the start)
INCUBATION_VIA_RUN = "start an incubation run (Portfolio & Trading → Incubation) — its length is fixed then"
ONLY_VALIDATED_INCUBATES = "only a validated strategy can start an incubation run"
INCUBATION_ABORT = "abort its incubation run (Portfolio & Trading → Incubation) to move it back to validated"
NOT_IN_STAGE_2 = ("not allowed in stage 2 — only idea ↔ draft, any state → retired and retired → draft "
                  "can be set by hand")


class SpecInvalid(Exception):
    """The spec text/dict did not validate; ``validation`` is the ValidationResult dict."""

    def __init__(self, validation: dict):
        super().__init__("the strategy spec is invalid")
        self.validation = validation


class NotFound(Exception):
    pass


class Conflict(Exception):
    """409: the request conflicts with the stored state; ``message`` is user-facing."""

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


# --- B1 contract (strategy.limits / schema / validator / alphas) ------------------------------

def _limits():
    from ..strategy import limits
    return limits


def _schema():
    from ..strategy import schema
    return schema


def _validator():
    from ..strategy import validator
    return validator


def validation_dict(result) -> dict:
    """ValidationResult (dict, dataclass or object with to_dict/as_dict) -> plain dict."""
    if isinstance(result, dict):
        return result
    for attr in ("to_dict", "as_dict", "dict"):
        fn = getattr(result, attr, None)
        if callable(fn):
            return fn()
    import dataclasses
    if dataclasses.is_dataclass(result):
        return dataclasses.asdict(result)
    return {"ok": bool(getattr(result, "ok")), "errors": list(getattr(result, "errors", [])),
            "warnings": list(getattr(result, "warnings", [])), "facts": dict(getattr(result, "facts", {}) or {})}


def validate_text(text: str) -> dict:
    return validation_dict(_validator().validate_text(text))


def validate_spec(spec: dict) -> dict:
    return validation_dict(_validator().validate(spec))


def library_entries() -> List[dict]:
    """The alpha library as [{key, spec, metadata}] (adapter over strategy.alphas)."""
    from ..strategy import alphas
    raw = getattr(alphas, "LIBRARY", None)
    if raw is None and callable(getattr(alphas, "library", None)):
        raw = alphas.library()
    if raw is None:
        raise RuntimeError("strategy.alphas has no LIBRARY")
    out = []
    for e in raw:
        if isinstance(e, dict):
            spec = e.get("spec")
            meta = e.get("metadata") if "metadata" in e else e.get("meta")
        elif isinstance(e, (tuple, list)) and len(e) == 2:
            spec, meta = e
        else:
            spec, meta = getattr(e, "spec"), getattr(e, "metadata", None) or getattr(e, "meta", None)
        meta = dict(meta or {})
        key = meta.get("key") or (e.get("key") if isinstance(e, dict) else getattr(e, "key", None))
        if not isinstance(key, str) or not key:
            raise RuntimeError("an alpha library entry has no key")
        meta.setdefault("key", key)
        out.append({"key": key, "spec": spec, "metadata": meta})
    return out


def library_metadata(library_key: Optional[str]) -> Optional[dict]:
    """Metadata of a library entry; None when the strategy is no library strategy or the key is no
    longer in the library. Any other failure (library not importable, …) propagates."""
    if not library_key:
        return None
    for e in library_entries():
        if e["key"] == library_key:
            return e["metadata"]
    return None


# --- helpers --------------------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return "s-" + uuid.uuid4().hex[:12]


def clean_file_name(name) -> Optional[str]:
    """Base name only (any directory part removed), control chars dropped, ≤ 120 chars."""
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("file_name: expected a string")
    base = re.split(r"[\\/]", name)[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    if base in ("", ".", ".."):
        return None
    if len(base) > MAX_FILE_NAME_LEN:
        root, ext = os.path.splitext(base)
        ext = ext if len(ext) <= 10 else ""
        base = root[:MAX_FILE_NAME_LEN - len(ext)] + ext
    return base


def _split_provenance(spec: dict):
    """(spec without provenance, claimed dict or None). Claims too large -> SpecInvalid."""
    spec = dict(spec)
    claimed = spec.pop("provenance", None)
    if claimed in ({}, None):
        return spec, None
    text = json.dumps(claimed, ensure_ascii=False, sort_keys=True)
    if len(text.encode("utf-8")) > MAX_CLAIMED_BYTES:
        raise SpecInvalid({"ok": False, "errors": [{"path": "$.provenance", "code": "limit_exceeded",
                                                    "message": f"The provenance block is larger than "
                                                               f"{MAX_CLAIMED_BYTES} bytes."}],
                           "warnings": [], "facts": {}})
    return spec, claimed


def _prepare(spec: dict):
    """Validated, default-filled spec without provenance -> (spec, claimed, hash, validation)."""
    schema = _schema()
    if not isinstance(spec, dict):
        raise SpecInvalid({"ok": False, "errors": [{"path": "$", "code": "bad_type",
                                                    "message": "The spec must be a JSON object."}],
                           "warnings": [], "facts": {}})
    validation = validate_spec(spec)
    if not validation.get("ok") or validation.get("errors"):
        raise SpecInvalid(validation)
    stored, claimed = _split_provenance(schema.with_defaults(spec))
    return stored, claimed, schema.content_hash(stored), validation


def _parse(text: str) -> dict:
    validation = validate_text(text)
    if not validation.get("ok") or validation.get("errors"):
        raise SpecInvalid(validation)
    return _limits().parse_spec_text(text)


def _version_row(strategy_id, version, spec, chash, envelope, validation, created_at) -> dict:
    return {"strategy_id": strategy_id, "version": version, "spec_json": _schema().canonical_json(spec),
            "content_hash": chash, "envelope_json": json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            "validation_json": json.dumps(validation, ensure_ascii=False, sort_keys=True, default=str),
            "created_at": created_at}


def _envelope(strategy_id, version, chash, origin, created_at, created_by, *, parent=None, ai_generated=False,
              external_text=False, claimed=None, source_file=None, ai_provenance=None) -> dict:
    env = {"strategy_id": strategy_id, "version": version, "content_hash": chash, "origin": origin,
           "created_at": created_at, "created_by": created_by, "parent": parent, "ai_generated": ai_generated,
           "external_text": bool(external_text), "claimed": claimed, "source_file": source_file}
    if ai_provenance is not None:            # Strategy Studio drafts (SPEC §13.1): model, prompt hash, session
        env["ai_provenance"] = ai_provenance
    return env


def _loads(text, what: str):
    """Stored JSON -> value; unreadable -> StorageUnavailable (never a made-up default:
    a lost envelope would silently drop the sticky external_text / ai_generated flags)."""
    try:
        return json.loads(text)
    except (TypeError, ValueError) as ex:
        raise db.StorageUnavailable(f"a stored strategy record ({what}) is unreadable",
                                    detail=str(ex)) from None


# --- create / version / duplicate ----------------------------------------------------------------

def _version_hash(strategy_id: str, version: int) -> Optional[str]:
    v = db.strategy_version_get(strategy_id, int(version))
    return v["content_hash"] if v else None


def _family_for(sid: str, envelope: dict, stored: dict) -> str:
    """Family of a new strategy (SPEC §9.1, §9.8): the family of the strategy it was created from
    (duplicate parent, or the verified ``claimed.exported_from`` of an import); else the family of the
    oldest strategy having a version with the same logic hash (``db.logic_hash_of``: copy-pasted spec,
    import with the provenance stripped — review 2026-10-09); else its own id."""
    src = db.family_parent_of(sid, envelope, _version_hash)
    if src is not None:
        row = db.strategy_get(src)
        if row is not None:
            if not row.get("family_id"):
                raise db.StorageUnavailable("a strategy has no family (schema v6 data missing)", detail=src)
            return row["family_id"]
    twins = db.families_with_logic_hash(db.logic_hash_of(stored))
    return twins[0] if twins else sid


def _create(spec: dict, origin: str, actor: str, *, state: str, library_key: Optional[str] = None,
            parent=None, ai_generated: Any = False, external_text: bool = False, source_file=None,
            reason: str, ai_provenance=None) -> dict:
    stored, claimed, chash, validation = _prepare(spec)
    sid = _new_id()
    ts = now_iso()
    envelope = _envelope(sid, 1, chash, origin, ts, "system" if actor == "system" else "user", parent=parent,
                         ai_generated=ai_generated, external_text=external_text, claimed=claimed,
                         source_file=source_file, ai_provenance=ai_provenance)
    strategy = {"id": sid, "name": stored["name"], "origin": origin, "category": stored.get("category") or "other",
                "state": state, "current_version": 1, "created_at": ts, "updated_at": ts, "state_changed_at": ts,
                "library_key": library_key, "family_id": _family_for(sid, envelope, stored)}
    transition = {"ts": ts, "strategy_id": sid, "version": 1, "from_state": None, "to_state": state,
                  "actor": actor, "reason": reason, "evidence_json": None}
    audit = (actor, "strategy_created", {"id": sid, "version": 1, "origin": origin, "content_hash": chash,
                                         "library_key": library_key,
                                         "file_name": (source_file or {}).get("name")})
    db.strategy_create(strategy, _version_row(sid, 1, stored, chash, envelope, validation, ts), transition, audit)
    logger.info("Strategy %s created (%s, %s)", sid, origin, chash[:12])
    return {"id": sid, "version": 1}


def create_from_text(text: str, origin: str, *, file_name=None, file_sha256=None) -> dict:
    """User strategy (origin user) or imported file (origin import) -> {id, version}."""
    if origin not in ("user", "import"):
        raise ValueError("origin: user or import")
    spec = _parse(text)
    source_file = None
    if origin == "import":
        raw = text.encode("utf-8")
        source_file = {"name": clean_file_name(file_name), "sha256": hashlib.sha256(raw).hexdigest(),
                       "bytes": len(raw)}
        if file_sha256 and file_sha256 != source_file["sha256"]:
            source_file["sha256_claimed"] = file_sha256      # the client's hash of the file bytes (not trusted)
    return _create(spec, origin, "user", state="draft",
                   ai_generated=("unknown" if origin == "import" else False),
                   external_text=(origin == "import"), source_file=source_file,
                   reason="imported from a file" if origin == "import" else "created")


def _require(strategy_id: str) -> dict:
    if not isinstance(strategy_id, str) or not re.fullmatch(r"s-[0-9a-f]{12}", strategy_id):
        raise NotFound(strategy_id)
    row = db.strategy_get(strategy_id)
    if row is None:
        raise NotFound(strategy_id)
    return row


def new_version(strategy_id: str, text: str, base_version: int) -> dict:
    """Saves an edited spec as version N+1 -> {id, version, unchanged}."""
    row = _require(strategy_id)
    if row["origin"] == "library":
        raise Conflict(READ_ONLY_MESSAGE)
    if int(base_version) != int(row["current_version"]):
        raise Conflict(f"The strategy was changed meanwhile (current version {row['current_version']}, "
                       f"your edit is based on version {base_version}) — reload it.",
                       current_version=row["current_version"])
    return _add_version(row, _parse(text), base_version)


def _add_version(row: dict, spec: dict, base_version: int, *, ai_provenance=None) -> dict:
    """Version N+1 of ``row`` from a parsed spec (shared by new_version and the Studio)."""
    strategy_id = row["id"]
    stored, claimed, chash, validation = _prepare(spec)
    current = db.strategy_version_get(strategy_id, row["current_version"])
    if current and current["content_hash"] == chash:
        return {"id": strategy_id, "version": row["current_version"], "unchanged": True}
    cur_env = _loads(current["envelope_json"], "envelope") if current else {}
    version = int(row["current_version"]) + 1
    ts = now_iso()
    envelope = _envelope(strategy_id, version, chash, row["origin"], ts, "user",
                         parent={"strategy_id": strategy_id, "version": row["current_version"]},
                         ai_generated=(True if ai_provenance is not None else cur_env.get("ai_generated", False)),
                         external_text=bool(cur_env.get("external_text")),     # stays external once imported
                         claimed=claimed, source_file=None, ai_provenance=ai_provenance)
    new_state, transition = None, None
    if row["state"] not in ("idea", "draft"):
        new_state = "draft"
        transition = {"ts": ts, "strategy_id": strategy_id, "version": version, "from_state": row["state"],
                      "to_state": "draft", "actor": "system",
                      "reason": f"new version {version} — the evidence belonged to version {version - 1}",
                      "evidence_json": None}
    audit = ("user", "strategy_version_created", {"id": strategy_id, "version": version, "content_hash": chash})
    try:
        db.strategy_add_version(strategy_id, base_version,
                                _version_row(strategy_id, version, stored, chash, envelope, validation, ts),
                                name=stored["name"], category=stored.get("category") or "other",
                                new_state=new_state, transition=transition, audit=audit)
    except db.StaleVersion as ex:
        raise Conflict(f"The strategy was changed meanwhile (current version {ex.current}) — reload it.",
                       current_version=ex.current) from None
    return {"id": strategy_id, "version": version, "unchanged": False}


def create_from_studio(spec: dict, ai_provenance: dict) -> dict:
    """A Strategy Studio draft (SPEC §13.1): origin ``studio``, state draft, ``ai_generated: true`` and the
    AI provenance in the envelope -> {id, version}. The spec's own ``provenance`` block is dropped (an AI
    claim is not provenance). ``SpecInvalid`` for an invalid spec."""
    spec = {k: v for k, v in dict(spec).items() if k != "provenance"}
    return _create(spec, "studio", "user", state="draft", ai_generated=True, ai_provenance=dict(ai_provenance),
                   reason="created by the Strategy Studio (AI draft)")


def create_from_paper(spec: dict, ai_provenance: dict) -> dict:
    """A paper -> strategy draft (SPEC §14.2): origin ``paper``, state draft, ``ai_generated: true``; the
    envelope's ``ai_provenance`` carries ``paper {id, sha256, title}``. -> {id, version}."""
    spec = {k: v for k, v in dict(spec).items() if k != "provenance"}
    return _create(spec, "paper", "user", state="draft", ai_generated=True, ai_provenance=dict(ai_provenance),
                   reason="built from a paper (AI draft, user-confirmed extraction)")


def new_studio_version(strategy_id: str, spec: dict, base_version: int, ai_provenance: dict) -> dict:
    """Version N+1 of a Studio strategy from an AI draft -> {id, version, unchanged}."""
    row = _require(strategy_id)
    if row["origin"] == "library":
        raise Conflict(READ_ONLY_MESSAGE)
    if int(base_version) != int(row["current_version"]):
        raise Conflict(f"The strategy was changed meanwhile (current version {row['current_version']}) — the "
                       "draft is based on an older version.", current_version=row["current_version"])
    spec = {k: v for k, v in dict(spec).items() if k != "provenance"}
    return _add_version(row, spec, base_version, ai_provenance=dict(ai_provenance))


def create_from_loop(spec: dict, ai_provenance: dict, parent: Optional[dict] = None) -> dict:
    """A discovery-loop variant (SPEC §16.1): origin ``loop``, state draft, ``ai_generated: true`` + AI provenance.
    ``parent`` = {strategy_id, version} of the variant it derives from — the parent link puts it into the
    parent's family (``_family_for``; no family bypass); None only for the root of an idea seed (own family,
    unless the logic hash matches an existing strategy). The sticky ``external_text`` flag is inherited.
    -> {id, version}."""
    spec = {k: v for k, v in dict(spec).items() if k != "provenance"}
    external = False
    if parent is not None:
        pv = db.strategy_version_get(parent["strategy_id"], int(parent["version"]))
        if pv is None:
            raise NotFound(parent["strategy_id"])
        external = bool(_loads(pv["envelope_json"], "envelope").get("external_text"))
        parent = {"strategy_id": parent["strategy_id"], "version": int(parent["version"])}
    reason = (f"created by the discovery loop {ai_provenance.get('loop_id')} (AI variant"
              + (f" of {parent['strategy_id']} v{parent['version']})" if parent else ", idea seed)"))
    return _create(spec, "loop", "user", state="draft", parent=parent, ai_generated=True, external_text=external,
                   ai_provenance=dict(ai_provenance), reason=reason)


def new_loop_version(strategy_id: str, spec: dict, base_version: int, ai_provenance: dict) -> dict:
    """Version N+1 of a loop-created strategy (refinement, search point, finalist promotion) -> {id, version,
    unchanged}. Only strategies of origin ``loop`` — the loop never adds versions to the user's strategies."""
    row = _require(strategy_id)
    if row["origin"] != "loop":
        raise Conflict("The discovery loop only adds versions to the strategies it created.")
    if int(base_version) != int(row["current_version"]):
        raise Conflict(f"The strategy was changed meanwhile (current version {row['current_version']}).",
                       current_version=row["current_version"])
    spec = {k: v for k, v in dict(spec).items() if k != "provenance"}
    return _add_version(row, spec, base_version, ai_provenance=dict(ai_provenance))


def duplicate(strategy_id: str, name: Optional[str] = None) -> dict:
    """Copies the current version as a new draft strategy (origin duplicate)."""
    row = _require(strategy_id)
    current = db.strategy_version_get(strategy_id, row["current_version"])
    spec = _loads(current["spec_json"], "spec")
    env = _loads(current["envelope_json"], "envelope")
    if name is None:
        name = ("Copy of " + spec.get("name", ""))[:_limits().MAX_NAME_LEN]
    spec["name"] = name
    return _create(spec, "duplicate", "user", state="draft",
                   parent={"strategy_id": strategy_id, "version": row["current_version"]},
                   ai_generated=env.get("ai_generated", False), external_text=bool(env.get("external_text")),
                   reason=f"duplicated from {strategy_id} v{row['current_version']}")


# --- transitions ------------------------------------------------------------------------------------

def _evidence_run(row: dict, run_id: Optional[str] = None) -> Optional[dict]:
    """The ``done`` run card backing ``draft → backtested`` for the strategy's current version:
    ``run_id`` when given (and valid), else the newest one; None when there is none."""
    from ..backtest import runcard
    version = int(row["current_version"])
    if run_id is not None:
        card = runcard.get(run_id) if isinstance(run_id, str) and run_id.strip() else None
        ok = card is not None and card["status"] == "done" and card["strategy_id"] == row["id"] \
            and int(card["version"]) == version
        return card if ok else None
    return runcard.latest_done(row["id"], version)


def _latest_kind_card(row: dict, kind: str) -> Optional[dict]:
    """The newest done run card of ``kind`` of the strategy's current version (full, decoded) or None."""
    from ..backtest import runcard
    latest = runcard.latest_done(row["id"], int(row["current_version"]), kind=kind)
    return runcard.get(latest["id"]) if latest else None


def _validation_passed(card: dict) -> bool:
    # V2 contract (SPEC §9.8): the raw metrics_json.validation.passed merged with the gate exceptions
    from ..validation import report
    return report.effective_passed(card) is True


def _holdout_passed(card: dict) -> bool:
    return ((card.get("metrics") or {}).get("holdout") or {}).get("passed") is True


def _stage4_evidence(row: dict, to: str, run_id: Optional[str]):
    """(card, None) when the stage-4 evidence for ``to`` (candidate | validated) holds, else (None, why)."""
    version = int(row["current_version"])
    kind = "validation" if to == "candidate" else "holdout"
    card = _latest_kind_card(row, kind)
    if run_id is not None and (card is None or card["id"] != run_id):
        tmpl = VALIDATION_RUN_NOT_EVIDENCE if kind == "validation" else HOLDOUT_RUN_NOT_EVIDENCE
        return None, tmpl.format(run_id=run_id, version=version)
    if kind == "validation":
        if card is None:
            return None, NEEDS_PASSING_VALIDATION.format(version=version)
        if not _validation_passed(card):
            return None, LATEST_VALIDATION_FAILED.format(version=version, run_id=card["id"])
        return card, None
    if card is None:
        from ..validation import families
        el = families.holdout_eligibility(row["id"])
        availability = HOLDOUT_AVAILABLE_NOW if el["eligible"] else \
            HOLDOUT_AVAILABLE_FROM.format(date=el["eligible_from"], reason=el["reason"])
        return None, NEEDS_PASSING_HOLDOUT.format(version=version, availability=availability)
    if not _holdout_passed(card):
        return None, LATEST_HOLDOUT_FAILED.format(run_id=card["id"], version=version)
    return card, None


def _failed_lock(row: dict) -> Optional[str]:
    """HOLDOUT_FAILED_LOCK text when the strategy's family failed the holdout (lock standing), else None."""
    from ..validation import families
    failed_at = families.family_state(row["id"])["holdout_failed_at"]      # incl. logic twins (§9.8)
    if not failed_at:
        return None
    chips = families.family_chips(row)
    return HOLDOUT_FAILED_LOCK.format(date=failed_at[:10], until=chips["holdout_failed_until"])


def transition_check(row: dict, to: str, *, run_id: Optional[str] = None) -> Optional[str]:
    """None when the user may move ``row`` to ``to``, else the reason why not."""
    frm = row["state"]
    if to not in STATES:
        return f"unknown state {to!r}"
    if row.get("origin") == "library":
        return READ_ONLY_MESSAGE
    if to == frm:
        return f"the strategy is already {frm}"
    if to in LIVE_STATES:
        return NEEDS_LIVE_GUARD
    if to in EVIDENCE_STATES:
        if to == "backtested":
            if frm != "draft":
                return ONLY_FROM_DRAFT
            if _evidence_run(row, run_id) is None:
                if run_id is not None:
                    return RUN_NOT_EVIDENCE.format(run_id=run_id, version=row["current_version"])
                return NEEDS_FINISHED_BACKTEST.format(version=row["current_version"])
            return None
        if to == "candidate":
            if frm != "backtested":
                return ONLY_FROM_BACKTESTED
            return _stage4_evidence(row, to, run_id)[1]
        if to in ("validated", "incubation"):
            if to == "validated" and frm == "incubation":
                return INCUBATION_ABORT
            if to == "validated" and frm != "candidate":
                return ONLY_FROM_CANDIDATE
            lock = _failed_lock(row)
            if lock is not None:
                return lock
            if to == "incubation" and frm != "validated":
                return ONLY_VALIDATED_INCUBATES
            if to == "validated":
                return _stage4_evidence(row, to, run_id)[1]
            return INCUBATION_VIA_RUN
        return NEEDS_RUN_CARD
    if to == "retired":
        return None
    if (frm, to) in (("idea", "draft"), ("draft", "idea"), ("retired", "draft")):
        return None
    return NOT_IN_STAGE_2


def allowed_transitions(row: dict) -> List[dict]:
    out = []
    for to in STATES:
        if to == row["state"]:
            continue
        why = transition_check(row, to)
        out.append({"to": to, "allowed": why is None, "reason_if_not": why})
    return out


def transition(strategy_id: str, to: str, reason: str, *, actor: str = "user",
               run_id: Optional[str] = None) -> dict:
    """Moves a strategy -> the updated strategy row. Refused -> Conflict(message).
    ``draft → backtested``: evidence = ``run_id`` (must be a done run card of the current
    version) or, when None, the newest such card. ``backtested → candidate`` / ``candidate →
    validated``: evidence = the latest done validation / holdout card of the current version
    (``run_id``, when given, must be that card); evidence_json {run_id, version, kind}."""
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason: a text (optional note)")
    reason = (reason or "").strip()             # fix1023: an optional note ("" allowed)
    if len(reason) > MAX_REASON_LEN:
        raise ValueError(f"reason: at most {MAX_REASON_LEN} characters")
    row = _require(strategy_id)
    why = transition_check(row, to, run_id=run_id)
    if why is not None:
        raise Conflict(f"Can't move the strategy from {row['state']} to {to}: {why}.")
    evidence = None
    if to == "backtested":
        run = _evidence_run(row, run_id)
        evidence = {"run_id": run["id"], "version": int(row["current_version"])}
    elif to in ("candidate", "validated"):
        card, _why = _stage4_evidence(row, to, run_id)
        evidence = {"run_id": card["id"], "version": int(row["current_version"]), "kind": card["kind"]}
    ts = now_iso()
    t = {"ts": ts, "strategy_id": strategy_id, "version": row["current_version"], "from_state": row["state"],
         "to_state": to, "actor": actor, "reason": reason,
         "evidence_json": None if evidence is None else json.dumps(evidence, sort_keys=True)}
    audit = (actor, "strategy_transition", {"id": strategy_id, "version": row["current_version"],
                                           "from": row["state"], "to": to, "evidence": evidence})
    # expected_version: a new version saved meanwhile invalidates the evidence -> refused, not stored
    if not db.strategy_set_state(strategy_id, row["state"], t, audit, expected_version=row["current_version"]):
        raise Conflict("The strategy's state changed meanwhile — reload it.")
    if row["state"] == "incubation":
        # stage 7 (SPEC §17.2): leaving incubation (e.g. → retired) ends its incubation run
        from ..live import incubation
        incubation.on_left_incubation(strategy_id, f"strategy moved to {to}" + (f": {reason}" if reason else ""),
                                      actor)
    return db.strategy_get(strategy_id)


def incubation_transition(strategy_id: str, to: str, reason: str, *, actor: str, run_id: str) -> dict:
    """Stage 7 (SPEC §17.2): ``validated → incubation`` (an incubation run starts) and ``incubation →
    validated`` (its run was aborted) — ONLY for live/incubation.py (the generic route refuses both).
    Raises Conflict when the strategy is not in the expected state (or got a new version meanwhile)."""
    frm = {"incubation": "validated", "validated": "incubation"}.get(to)
    if frm is None:
        raise ValueError("incubation_transition: to must be incubation or validated")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason: a text (optional note)")
    reason = reason or ""                       # fix1023: an optional note
    row = _require(strategy_id)
    if row["state"] != frm:
        raise Conflict(f"Can't move the strategy from {row['state']} to {to}: it is not {frm}.")
    ts = now_iso()
    evidence = {"incubation_id": run_id, "version": int(row["current_version"])}
    t = {"ts": ts, "strategy_id": strategy_id, "version": row["current_version"], "from_state": frm, "to_state": to,
         "actor": actor, "reason": reason.strip()[:MAX_REASON_LEN], "evidence_json": json.dumps(evidence, sort_keys=True)}
    audit = (actor, "strategy_transition", {"id": strategy_id, "version": row["current_version"], "from": frm, "to": to,
                                           "evidence": evidence})
    if not db.strategy_set_state(strategy_id, frm, t, audit, expected_version=row["current_version"]):
        raise Conflict("The strategy's state changed meanwhile — reload it.")
    return db.strategy_get(strategy_id)


# --- reads ----------------------------------------------------------------------------------------

def family_chips(row: dict, spec: Optional[dict] = None) -> dict:
    """{family_id, contaminated, holdout_failed_until, trial_count} for a strategy row (SPEC §9.7)."""
    from ..validation import families
    return families.family_chips(row, spec)


def get(strategy_id: str) -> dict:
    """The strategy row + family chips (family_id, contaminated, holdout_failed_until, trial_count)."""
    row = _require(strategy_id)
    return dict(row, **family_chips(row))


def get_version(strategy_id: str, version: int) -> dict:
    _require(strategy_id)
    v = db.strategy_version_get(strategy_id, int(version))
    if v is None:
        raise NotFound(f"{strategy_id} v{version}")
    return {"spec": _loads(v["spec_json"], "spec"), "envelope": _loads(v["envelope_json"], "envelope"),
            "validation": _loads(v["validation_json"], "validation"), "content_hash": v["content_hash"],
            "created_at": v["created_at"], "version": v["version"]}


# --- library seeding --------------------------------------------------------------------------------

_seed_lock = threading.Lock()
_seed_status: Dict[str, Any] = {"ok": None, "at": None, "message": "library not seeded yet", "created": 0,
                                "updated": 0, "unchanged": 0, "errors": []}


def seed_status() -> dict:
    return dict(_seed_status, errors=list(_seed_status["errors"]))


def _set_status(**kw) -> None:
    _seed_status.update(kw)


def seed_library(entries: Optional[List[dict]] = None) -> dict:
    """Seeds/updates the library strategies (idempotent). Returns and stores
    the status {ok, at, message, created, updated, unchanged, errors}. An
    invalid entry is reported in ``errors`` (ok False) — the others are still
    seeded; a storage failure raises (status records it)."""
    with _seed_lock:
        created = updated = unchanged = 0
        errors: List[dict] = []
        try:
            items = library_entries() if entries is None else entries
            for e in items:
                key = e["key"]
                try:
                    stored, _claimed, chash, validation = _prepare(e["spec"])
                except SpecInvalid as ex:
                    errs = ex.validation.get("errors") or []
                    errors.append({"key": key, "message": "; ".join(f"{x.get('path')}: {x.get('message')}"
                                                                    for x in errs[:3]) or "invalid spec"})
                    continue
                row = db.strategy_by_library_key(key)
                if row is None:
                    _create(e["spec"], "library", "system", state="idea", library_key=key,
                            reason="seeded from the alpha library")
                    created += 1
                    continue
                current = db.strategy_version_get(row["id"], row["current_version"])
                if current and current["content_hash"] == chash:
                    unchanged += 1
                    continue
                version = int(row["current_version"]) + 1
                ts = now_iso()
                envelope = _envelope(row["id"], version, chash, "library", ts, "system",
                                     parent={"strategy_id": row["id"], "version": row["current_version"]})
                db.strategy_add_version(
                    row["id"], row["current_version"],
                    _version_row(row["id"], version, stored, chash, envelope, validation, ts),
                    name=stored["name"], category=stored.get("category") or "other", new_state=None,
                    transition=None, audit=("system", "strategy_version_created",
                                            {"id": row["id"], "version": version, "content_hash": chash,
                                             "library_key": key}))
                updated += 1
        except Exception as ex:
            message = ex.user_message if isinstance(ex, UserFacingError) else f"{type(ex).__name__}: {ex}"
            _set_status(ok=False, at=now_iso(), message=f"Library seeding failed: {message}", created=created,
                        updated=updated, unchanged=unchanged, errors=errors)
            raise
        ok = not errors
        message = (f"Library seeded: {created} new, {updated} updated, {unchanged} unchanged"
                   + ("" if ok else f"; {len(errors)} invalid entr{'y' if len(errors) == 1 else 'ies'} skipped"))
        _set_status(ok=ok, at=now_iso(), message=message, created=created, updated=updated, unchanged=unchanged,
                    errors=errors)
        if not ok:
            logger.error("%s: %s", message, errors)
        else:
            logger.info("%s", message)
        return seed_status()
