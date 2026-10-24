"""Validation report (SPEC §9.3 step 5, §9.4) and its exceptions.

Stored report (inside the immutable run card, ``metrics_json.validation``
for kind ``validation``; ``metrics_json.holdout`` for kind ``holdout``):

``{passed, version, report: {version, passed, mode, gates: [{gate, name,
status, value, threshold, explanation, details}], folds, grid, pbo, dsr,
stress, trial_count, thresholds: {preset, overrides, effective}, training,
ic, holdout, exceptions: [], gate_facts: [{gate, name, status, value,
threshold, facts: [{id, label, value, unit, display}]}]}}`` (``gate_facts``:
SPEC §11.1 / §11.5, ``backtest.attribution.gate_facts``; recomputed by
``merge`` after exceptions so statuses match the merged gates).

``passed`` in the card is the RAW result (every G1–G8 ``pass``). Exceptions
(a written reason ≥ 30 characters per failed G1–G8; never H) are stored
separately — insert-only table ``validation_exceptions`` (db v6, V1) via
``db.validation_exception_add`` — because the card cannot change, and are
merged on read (``effective``): a failed gate with an exception gets
status ``exception`` + ``exception {id, ts, reason, actor}``; ``passed`` is
recomputed (all G1–G8 ``pass`` or ``exception``). An exception belongs to
ONE validation run (a new run needs new exceptions). Lifecycle
(``backtested -> candidate``) calls ``effective_passed(card)``.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import copy
from typing import List, Optional

REPORT_VERSION = "sp-validation/4.2"      # 4.2 (2026-10-14, SPEC §11.1): + gate_facts
MIN_REASON_CHARS = 30
MAX_REASON_CHARS = 2000
OK_STATUSES = ("pass", "exception")


class ExceptionRefused(Exception):
    """The exception cannot be added (HTTP 400 for input, 409 for state) — ``status`` + message."""

    def __init__(self, message: str, status: int = 400, field: str = "_"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.field = field


TRAINING_GATES = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")


def passed_of(gates: List[dict]) -> bool:
    """True only when EVERY gate G1–G8 is present and pass/exception (a missing gate -> not passed)."""
    by = {g.get("gate"): g for g in gates}
    return all(k in by and by[k].get("status") in OK_STATUSES for k in TRAINING_GATES)


def build(*, gates: List[dict], mode, folds, grid, pbo, dsr, stress, trial_count, thresholds: dict, training: dict,
          ic: dict, holdout: Optional[dict]) -> dict:
    """The validation report (module docstring)."""
    from ..backtest import attribution
    rep = {"version": REPORT_VERSION, "passed": passed_of(gates), "mode": mode, "gates": gates, "folds": folds,
           "grid": grid, "pbo": pbo, "dsr": dsr, "stress": stress, "trial_count": trial_count,
           "thresholds": thresholds, "training": training, "ic": ic, "holdout": holdout, "exceptions": []}
    rep["gate_facts"] = attribution.gate_facts(rep)
    return rep


def stored_block(rep: dict) -> dict:
    """metrics_json.validation / metrics_json.holdout block."""
    return {"passed": rep["passed"], "version": rep["version"], "report": rep}


def _validation_block(card: dict) -> Optional[dict]:
    m = (card or {}).get("metrics") or {}
    return m.get("validation")


def exceptions_for(run_id: str) -> List[dict]:
    from ..storage import db
    return list(db.validation_exceptions_list(run_id))


def merge(rep: dict, exceptions: List[dict]) -> dict:
    """A copy of ``rep`` with the exceptions applied and ``passed`` recomputed."""
    out = copy.deepcopy(rep)
    by_gate = {}
    for e in exceptions:
        by_gate.setdefault(e["gate"], e)          # one per gate (db rule); first wins
    for g in out.get("gates") or []:
        e = by_gate.get(g["gate"])
        if e is not None and g["gate"] != "H" and g["status"] == "fail":
            g["status"] = "exception"
            g["exception"] = {"id": e.get("id"), "ts": e.get("ts"), "reason": e.get("reason"), "actor": e.get("actor")}
    out["exceptions"] = [{"id": e.get("id"), "ts": e.get("ts"), "gate": e.get("gate"), "reason": e.get("reason"),
                          "actor": e.get("actor")} for e in exceptions]
    out["passed_raw"] = rep.get("passed")
    out["passed"] = passed_of(out.get("gates") or [])
    if "gate_facts" in rep:                       # reports from 4.2 on (older reports stay as stored)
        from ..backtest import attribution
        out["gate_facts"] = attribution.gate_facts(out)
    return out


def effective(card: dict) -> Optional[dict]:
    """The report of a done validation card with its exceptions merged, or None (no report)."""
    block = _validation_block(card)
    if not block or not block.get("report"):
        return None
    return merge(block["report"], exceptions_for(card["id"]))


def effective_passed(card: dict) -> bool:
    """True when the validation card's G1–G8 all pass or have an exception (lifecycle, SPEC §9.5)."""
    if not card or card.get("status") != "done":
        return False
    rep = effective(card)
    return bool(rep and rep.get("passed"))


def add_exception(run_id: str, gate: str, reason: str, actor: str = "user") -> dict:
    """Adds an exception to a FAILED G1–G8 of a done validation card -> the merged report.
    ``ExceptionRefused`` (400 invalid input / gate not failed, 404 unknown run, 409 already excepted)."""
    from ..backtest import runcard
    from ..storage import db
    if not isinstance(gate, str) or gate not in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "H"):
        raise ExceptionRefused("gate: one of G1 … G8", field="gate")
    if gate == "H":
        raise ExceptionRefused("The holdout gate H can never be overridden.", field="gate")
    if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON_CHARS:
        raise ExceptionRefused(f"reason: a written reason of at least {MIN_REASON_CHARS} characters",
                               field="reason")
    reason = reason.strip()
    if len(reason) > MAX_REASON_CHARS:
        raise ExceptionRefused(f"reason: at most {MAX_REASON_CHARS} characters", field="reason")
    card = runcard.get(run_id)
    if card is None or card.get("kind", "validation") != "validation":
        raise ExceptionRefused("Validation run not found.", status=404)
    if card["status"] != "done" or not _validation_block(card):
        raise ExceptionRefused(f"The validation run is {card['status']} — exceptions need a finished report.",
                               status=409)
    rep = effective(card)
    g = next((x for x in rep["gates"] if x["gate"] == gate), None)
    if g is None:
        raise ExceptionRefused(f"The report has no gate {gate}.", field="gate")
    if g["status"] == "exception":
        raise ExceptionRefused(f"Gate {gate} already has an exception in this validation run.", status=409)
    if g["status"] != "fail":
        raise ExceptionRefused(f"Gate {gate} did not fail — only failed gates can get an exception.", field="gate")
    try:
        db.validation_exception_add(run_id, gate, reason, actor=actor,
                                    audit=(actor, "validation_exception", {"run_id": run_id, "gate": gate,
                                                                           "reason": reason}))
    except ValueError as ex:
        msg = str(ex)
        raise ExceptionRefused(msg[:300], status=409 if "already" in msg.lower() else 400) from None
    return effective(runcard.get(run_id))
