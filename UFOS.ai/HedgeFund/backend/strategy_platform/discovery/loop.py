"""Discovery loop orchestrator (SPEC §16.1, §16.5–§16.10).

One loop at a time per backend, in its own daemon thread. The orchestrator never runs a backtest itself: every
backtest / validation is a normal job on the ONE job slot (``backtest.jobs.manager()``), submitted only when the
slot is free, no Studio draft / paper replication waits and ``YIELD_SECONDS`` passed since the loop's previous
job ended (§16.8). Every completed step is written insert-only (``loop_events``, ``loop_variants``,
``loop_ai_messages``) BEFORE the next starts; the state is a fold over the events, so the orchestrator is
re-entrant: a resume (after pause, budget pause, crash or connection loss) rebuilds its position and never
recomputes a stored step (an LLM call cut off by a crash is repeated — its booked spend stays booked).

Generation 1: slot 1 = the seed, slots 2..N = AI mutations of the seed. Generation g ≥ 2 from the selection of
g − 1: elites carried (no new backtest), "refine" variants get diagnosis + refinement (a new version of a
loop-created strategy; refining the user's seed creates a new strategy — the loop never adds versions to the
user's strategies), culled slots are refilled with children of the elites (new strategies). Every variant
lives in the seed's strategy family (parent links), so every backtest is a trial of that family. The loop
NEVER opens the holdout, never reads holdout runs/facts and never changes a lifecycle state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..backtest import explain
from ..core.errors import UserFacingError
from ..storage import db
from . import budget as budget_mod, prompts as lp, scoring, search, selection

logger = logging.getLogger(__name__)

FEATURE = "sp-loop"
FEATURE_LABEL = "Hedge Fund · Discovery loop"
DIAG_FEATURE = "sp-loop-diagnosis"
SEED_FEATURE = "sp-loop-seed"
TEMPERATURE = 0.4
DIAG_TEMPERATURE = 0.2
MAX_TOKENS = 4000
DIAG_MAX_TOKENS = 1200
TIERS = ("standard", "premium")
IDEA_MAX = 4000

VARIANTS_DEFAULT, VARIANTS_MIN, VARIANTS_MAX = 6, 2, 8          # SPEC §15 (user, 2026-10-17)
GENERATIONS_DEFAULT, GENERATIONS_MIN, GENERATIONS_MAX = 4, 1, 5
SEARCH_DEFAULT, SEARCH_MIN, SEARCH_MAX = 1, 0, 3                 # default — user confirm (§16.1)
MAX_BACKTESTS = 120
YIELD_SECONDS = 5.0
POLL_SECONDS = 0.25
PROBE_INTERVAL_SECONDS = 30.0

STATUSES = ("running", "paused", "paused_budget", "interrupted", "done", "failed", "cancelled")
TERMINAL = ("done", "failed", "cancelled")
STATUS_EVENTS = ("created",) + STATUSES

BUSY_MESSAGE = "A discovery loop is already running — wait until it is finished, or cancel it."
NOT_FOUND_MESSAGE = "Discovery loop not found."
AI_INTERACTION_NOTICE = ("You are interacting with an AI system. The loop's diagnoses and strategy changes are "
                         "AI-generated and not verified. AI-generated research output for your own decision. Not a "
                         "personal recommendation by a licensed advisor; you decide and carry the risk. Backtests are "
                         "hypothetical.")          # EU AI Act Art. 50(1); S10-H: + context notice (b) (core/notices)
HOLDOUT_NOTE = ("The loop never opens the holdout: it is opened only by you, once per strategy family, from the "
                "strategy's validation report.")
NO_FINALIST_MESSAGE = "No finalist: no variant passed the pre-checks."
RUN_CAP_NOTE = "stopped early: per-run AI cap reached"
UNIVERSE_CHANGED = "The loop keeps the seed's universe (comparable scores) — the AI changed the universe."
TOPIC_FAILED = "The AI texts of this variant did not pass the topic check — they are not shown."
SCOPE_NOTE = ("The discovery loop only refines trading strategies in the rule language — the AI answer was "
              "discarded.")

_lock = threading.Lock()
_active: Dict[str, "_Flags"] = {}            # loop id -> flags of the running orchestrator
_progress: Dict[str, dict] = {}
_loop_runs: Dict[str, str] = {}              # run id -> loop id, for the jobs a running loop submitted (§16.8)
_last_job_end = 0.0
_wake = threading.Event()
_validation_dispatch_lock = threading.Lock()


class LoopError(explain.ExplainError):
    """A loop request that cannot be served (HTTP status, code, extra payload)."""


class _Flags:
    def __init__(self):
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.thread: Optional[threading.Thread] = None


class _Stop(Exception):
    """Ends the orchestrator session with a status event (paused, paused_budget, cancelled, failed)."""

    def __init__(self, status: str, message: str, **data):
        super().__init__(message)
        self.status, self.message, self.data = status, message, data


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def _audit_quiet(action: str, detail: dict, actor: str = "system") -> None:
    try:
        db.add_audit(actor, action, detail)
    except Exception:     # noqa: BLE001
        logger.warning("loop: audit row %s could not be written", action, exc_info=True)


# --- persistence --------------------------------------------------------------------------------------

def _event(loop_id: str, event: str, *, generation: Optional[int] = None, variant_id: Optional[str] = None,
           data: Optional[dict] = None, audit: Optional[tuple] = None) -> int:
    return db.s5b_insert("loop_events", {"loop_id": loop_id, "event": event, "generation": generation,
                                         "variant_id": variant_id,
                                         "data_json": json.dumps(data or {}, ensure_ascii=False, default=str)},
                         audit)


def _events(loop_id: str) -> List[dict]:
    return db.s5b_select("loop_events", {"loop_id": loop_id})


def _run_row(loop_id) -> dict:
    if not isinstance(loop_id, str) or not loop_id.startswith("lr-") or len(loop_id) > 64:
        raise LoopError(404, "not_found", NOT_FOUND_MESSAGE)
    rows = db.s5b_select("loop_runs", {"id": loop_id})
    if not rows:
        raise LoopError(404, "not_found", NOT_FOUND_MESSAGE)
    return rows[0]


def _new_variant(loop_id: str, generation: int, slot: int, origin: str, parent: Optional[str]) -> str:
    vid = "lv-" + uuid.uuid4().hex[:12]
    db.s5b_insert("loop_variants", {"id": vid, "loop_id": loop_id, "generation": generation, "slot": slot,
                                    "origin": origin, "parent_variant_id": parent})
    return vid


def _ai_insert(loop_id: str, variant_id: Optional[str], role: str, output: dict, check, prov: dict, usage: dict,
               cost: float) -> int:
    return db.s5b_insert("loop_ai_messages", {"loop_id": loop_id, "variant_id": variant_id, "role": role,
                                              "output_json": json.dumps(output, ensure_ascii=False, default=str),
                                              "check_json": json.dumps(check, ensure_ascii=False),
                                              "provenance_json": json.dumps(prov, ensure_ascii=False, sort_keys=True),
                                              "usage_json": json.dumps(usage, ensure_ascii=False, sort_keys=True),
                                              "cost_usd": round(float(cost or 0.0), 6)})


# --- state (fold over the events) ---------------------------------------------------------------------

class State:
    def __init__(self, loop_id: str):
        self.row = _run_row(loop_id)
        self.id = loop_id
        self.config = self.row["config"]
        self.seed_in = self.row["seed"]
        self.events = _events(loop_id)
        self.variants: Dict[str, dict] = {}
        for r in db.s5b_select("loop_variants", {"loop_id": loop_id}):
            self.variants[r["id"]] = {**r, "strategy_id": None, "versions": [], "state": "planned", "reason": None,
                                      "chosen_version": None, "run_id": None, "score": None, "topic_check": None,
                                      "carried_from": None, "message_ids": []}
        self.status, self.status_reason = "running", None
        self.seed: Optional[dict] = None
        self.generations: Dict[int, dict] = {}
        self.finalists: Optional[dict] = None
        self.finalist_events: Dict[str, List[dict]] = {}
        self.run_cap_reached = False
        self.notes: List[str] = []
        self.cancel_requested = False
        self.backtests_started = 0
        self.done_note = None
        for e in self.events:
            self._apply(e)

    def _ver(self, v: dict, version: int) -> dict:
        for x in v["versions"]:
            if x["version"] == version:
                return x
        x = {"version": version, "kind": None, "logic_hash": None, "parameter": None, "value": None, "run_id": None,
             "run_status": None, "score": None, "error": None, "code": None, "reused": False}
        v["versions"].append(x)
        return x

    def _apply(self, e: dict) -> None:
        ev, d, vid = e["event"], e.get("data") or {}, e.get("variant_id")
        v = self.variants.get(vid) if vid else None
        if ev in STATUS_EVENTS:
            self.status = "running" if ev == "created" else ev
            self.status_reason = d.get("message")
            if ev == "done":
                self.done_note = d.get("note")
        elif ev == "cancel_requested":
            self.cancel_requested = True
        elif ev == "seed_ready":
            self.seed = dict(d)
        elif ev == "generation_planned":
            self.generations.setdefault(e["generation"], {})["slots"] = list(d.get("slots") or [])
        elif ev == "selection":
            self.generations.setdefault(e["generation"], {})["selection"] = dict(d)
        elif ev == "run_cap_reached":
            self.run_cap_reached = True
        elif ev == "note":
            self.notes.append(d.get("text") or "")
        elif ev == "finalists":
            self.finalists = dict(d)
        elif ev in ("promoted", "approved", "rejected", "validation_started", "validation_finished"):
            self.finalist_events.setdefault(vid, []).append(dict(d, event=ev, at=e["created_at"]))
        elif v is None:
            return
        elif ev == "variant_saved":
            v["strategy_id"] = d.get("strategy_id")
            x = self._ver(v, int(d["version"]))
            x.update(kind=d.get("kind"), logic_hash=d.get("logic_hash"), parameter=d.get("parameter"),
                     value=d.get("value"))
            if v["state"] in ("planned", "ai"):
                v["state"] = "saved"
        elif ev == "ai_started":
            v["state"] = "ai"
        elif ev == "ai_message":
            v["message_ids"].append(d.get("message_id"))
        elif ev == "topic_checked":
            v["topic_check"] = d.get("status")
        elif ev == "variant_failed":
            v["state"], v["reason"] = "failed", d.get("reason")
        elif ev == "variant_duplicate":
            v["state"], v["reason"] = "duplicate", d.get("reason")
        elif ev == "backtest_started":
            x = self._ver(v, int(d["version"]))
            x.update(run_id=d.get("run_id"), run_status="started", reused=bool(d.get("reused")))
            if not d.get("reused") and not d.get("resumed_from"):
                self.backtests_started += 1
            v["state"] = "backtesting"
        elif ev == "backtest_finished":
            x = self._ver(v, int(d["version"]))
            x.update(run_id=d.get("run_id"), run_status=d.get("status"), score=d.get("score"), error=d.get("error"),
                     code=d.get("code"))
        elif ev == "variant_scored":
            v.update(state="scored", chosen_version=d.get("chosen_version"), run_id=d.get("run_id"),
                     score=d.get("score"), strategy_id=d.get("strategy_id") or v["strategy_id"])
            if d.get("carried_from"):
                v["carried_from"] = d["carried_from"]
            if d.get("score") is None or (d.get("score") or {}).get("score") is None:
                v["reason"] = d.get("reason") or (d.get("score") or {}).get("reason")

    # helpers
    def gen_variants(self, g: int) -> List[dict]:
        ids = (self.generations.get(g) or {}).get("slots") or []
        return [self.variants[i] for i in ids if i in self.variants]

    def known_hashes(self) -> set:
        return {x["logic_hash"] for v in self.variants.values() for x in v["versions"] if x.get("logic_hash")}

    def last_selected_generation(self) -> Optional[int]:
        gs = [g for g, x in self.generations.items() if x.get("selection")]
        return max(gs) if gs else None


# --- config ---------------------------------------------------------------------------------------------

def planned_backtests(n: int, g: int, p: int, seed_reused: bool = False) -> int:
    k = selection.k_elites(n)
    total = (0 if seed_reused else 1) + (n - 1) * (1 + p) + (g - 1) * max(0, n - k) * (1 + p)
    return min(MAX_BACKTESTS, total)


def planned_ai_calls(n: int, g: int, idea: bool) -> int:
    k = selection.k_elites(n)
    return (2 if idea else 0) + (n - 1) * 2 + (g - 1) * max(0, n - k) * 3


def check_config(body) -> dict:
    """{field: message} for POST /v2/loops."""
    allowed = ("seed", "variants", "generations", "search_points", "model", "history_from", "auto_resume")
    if not isinstance(body, dict):
        return {"_": "expected a JSON object {seed, variants?, generations?, search_points?, model?, history_from?, "
                     "auto_resume?}"}
    errors = {str(k): "unknown field" for k in body if k not in allowed}
    seed = body.get("seed")
    if not isinstance(seed, dict):
        errors["seed"] = 'required: {kind: "strategy", strategy_id, version?} | {kind: "idea", text} | ' \
                         '{kind: "paper", paper_id}'
    else:
        kind = seed.get("kind")
        keys = {"strategy": ("kind", "strategy_id", "version"), "idea": ("kind", "text"),
                "paper": ("kind", "paper_id")}.get(kind)
        if keys is None:
            errors["seed.kind"] = "one of strategy, idea, paper"
        else:
            for k in seed:
                if k not in keys:
                    errors[f"seed.{k}"] = "unknown field"
            if kind == "strategy":
                if not isinstance(seed.get("strategy_id"), str) or not 1 <= len(seed["strategy_id"]) <= 64:
                    errors["seed.strategy_id"] = "a strategy id"
                if "version" in seed and (isinstance(seed["version"], bool) or not isinstance(seed["version"], int)
                                          or seed["version"] < 1):
                    errors["seed.version"] = "an integer ≥ 1"
            elif kind == "idea":
                t = seed.get("text")
                if not isinstance(t, str) or not t.strip() or len(t) > IDEA_MAX:
                    errors["seed.text"] = f"1..{IDEA_MAX} characters"
            elif kind == "paper":
                if not isinstance(seed.get("paper_id"), str) or not 1 <= len(seed["paper_id"]) <= 64:
                    errors["seed.paper_id"] = "a paper id"
    for k, lo, hi in (("variants", VARIANTS_MIN, VARIANTS_MAX), ("generations", GENERATIONS_MIN, GENERATIONS_MAX),
                      ("search_points", SEARCH_MIN, SEARCH_MAX)):
        if k in body and (isinstance(body[k], bool) or not isinstance(body[k], int) or not lo <= body[k] <= hi):
            errors[k] = f"an integer {lo}..{hi}"
    if "model" in body and body["model"] not in TIERS:
        errors["model"] = "one of standard, premium"
    if "history_from" in body and body["history_from"] not in ("2016", "2000"):
        errors["history_from"] = "one of 2016, 2000"
    if "auto_resume" in body and not isinstance(body["auto_resume"], bool):
        errors["auto_resume"] = "true/false"
    return errors


def _config(body: dict) -> dict:
    return {"variants": int(body.get("variants", VARIANTS_DEFAULT)),
            "generations": int(body.get("generations", GENERATIONS_DEFAULT)),
            "search_points": int(body.get("search_points", SEARCH_DEFAULT)),
            "model": body.get("model", "standard"), "history_from": body.get("history_from", "2016"),
            "auto_resume": bool(body.get("auto_resume", True)), "autonomous": budget_mod.autonomous(),
            "budget": budget_mod.limits()}


def _resolve_seed(seed: dict) -> dict:
    """{kind, strategy_id?, version?, paper_id?, text?} with a strategy for strategy/paper seeds (404/409)."""
    if seed["kind"] == "strategy":
        row = db.strategy_get(seed["strategy_id"])
        if row is None:
            raise LoopError(404, "not_found", "Strategy not found.")
        version = int(seed.get("version") or row["current_version"])
        if db.strategy_version_get(row["id"], version) is None:
            raise LoopError(404, "not_found", "Strategy version not found.")
        return {"kind": "strategy", "strategy_id": row["id"], "version": version}
    if seed["kind"] == "paper":
        papers = db.s5b_select("papers", {"id": seed["paper_id"]})
        if not papers:
            raise LoopError(404, "not_found", "Paper not found.")
        conf = db.s5b_select("paper_confirmations", {"paper_id": seed["paper_id"]})
        if not conf or not conf[0].get("strategy_id"):
            raise LoopError(409, "paper_not_confirmed", "This paper has no confirmed strategy yet — confirm the "
                                                        "extraction first (Studio → Paper).")
        row = db.strategy_get(conf[0]["strategy_id"])
        if row is None:
            raise LoopError(409, "paper_not_confirmed", "The paper's strategy does not exist any more.")
        return {"kind": "paper", "paper_id": seed["paper_id"], "strategy_id": row["id"],
                "version": int(row["current_version"])}
    return {"kind": "idea", "text": seed["text"].strip()}


# --- start / control ----------------------------------------------------------------------------------

def _first_call_estimate(tier: str, spec: Optional[dict]) -> float:
    from strategy_platform.core import llm
    msgs = lp.refine_messages("mutation", lp.MUTATION_ASPECTS[0], spec or {}, None, None, None, [], [])
    return llm.estimate_cost(msgs, MAX_TOKENS, tier)


def _require_free() -> None:
    with _lock:
        for lid, f in _active.items():
            if f.thread is not None and f.thread.is_alive():
                raise LoopError(409, "busy", BUSY_MESSAGE, loop_id=lid)


def start(body: dict) -> dict:
    """POST /v2/loops (validated body) -> the loop payload (202)."""
    _require_free()
    seed = _resolve_seed(body["seed"])
    cfg = _config(body)
    explain._model_id(cfg["model"])                         # 503 no_model before anything is stored
    spec = None
    if seed.get("strategy_id"):
        spec = json.loads(db.strategy_version_get(seed["strategy_id"], seed["version"])["spec_json"])
    est = _first_call_estimate(cfg["model"], spec)
    b = budget_mod.LoopBudget("lr-pending")
    msg = b._check(est)
    if msg is not None and msg != budget_mod.RUN_REACHED_MESSAGE:
        raise LoopError(503 if msg == budget_mod.SPEND_UNRECORDED_MESSAGE else 402,
                        "spend_unrecorded" if msg == budget_mod.SPEND_UNRECORDED_MESSAGE else "budget", msg,
                        **b.fields(est))
    if msg == budget_mod.RUN_REACHED_MESSAGE:
        raise LoopError(402, "budget", "The per-run AI cap is smaller than one loop AI call — raise it in the AI "
                                       "budget settings.", **b.fields(est))
    lid = "lr-" + uuid.uuid4().hex[:12]
    stored_seed = dict(seed)
    db.s5b_insert("loop_runs", {"id": lid, "seed_kind": seed["kind"],
                                "seed_json": json.dumps(stored_seed, ensure_ascii=False),
                                "config_json": json.dumps(cfg, ensure_ascii=False)},
                  ("user", "loop_created", {"loop_id": lid, "seed": {k: v for k, v in seed.items() if k != "text"},
                                            "config": cfg}))
    _event(lid, "created", data={"config": cfg})
    _spawn(lid)
    return get_loop(lid)


def _spawn(loop_id: str) -> None:
    with _lock:
        for lid, f in _active.items():
            if f.thread is not None and f.thread.is_alive():
                raise LoopError(409, "busy", BUSY_MESSAGE, loop_id=lid)
        flags = _Flags()
        _active[loop_id] = flags
        flags.thread = threading.Thread(target=_orchestrate, args=(loop_id, flags), name=f"loop-{loop_id}",
                                        daemon=True)
        flags.thread.start()


def is_active(loop_id: str) -> bool:
    with _lock:
        f = _active.get(loop_id)
        return bool(f and f.thread and f.thread.is_alive())


def _flags(loop_id: str) -> Optional[_Flags]:
    with _lock:
        return _active.get(loop_id)


def cancel(loop_id: str) -> dict:
    st = State(loop_id)
    if st.status in TERMINAL:
        raise LoopError(409, "not_running", f"The loop is already {st.status}.")
    f = _flags(loop_id)
    _event(loop_id, "cancel_requested", audit=("user", "loop_cancel_requested", {"loop_id": loop_id}))
    if f is not None and f.thread is not None and f.thread.is_alive():
        f.cancel.set()
        rid = (_progress.get(loop_id) or {}).get("run_id")
        if rid:
            from ..backtest import jobs
            jobs.manager().cancel(rid)
        _wake.set()
    else:
        _event(loop_id, "cancelled", data={"message": "Cancelled while the loop was not running."})
    return get_loop(loop_id)


def pause(loop_id: str) -> dict:
    st = State(loop_id)
    f = _flags(loop_id)
    if st.status != "running" or f is None or f.thread is None or not f.thread.is_alive():
        raise LoopError(409, "not_running", "The loop is not running.")
    f.pause.set()
    _wake.set()
    _audit_quiet("loop_pause_requested", {"loop_id": loop_id}, "user")
    return get_loop(loop_id)


def resume(loop_id: str, actor: str = "user") -> dict:
    st = State(loop_id)
    if st.status in TERMINAL:
        raise LoopError(409, "not_resumable", f"The loop is {st.status} — start a new loop.")
    if st.cancel_requested:
        raise LoopError(409, "not_resumable", "The loop was cancelled — start a new loop.")
    if is_active(loop_id):
        raise LoopError(409, "running", "The loop is already running.")
    _require_free()
    _audit_quiet("loop_resumed", {"loop_id": loop_id, "from_status": st.status}, actor)
    _spawn(loop_id)
    return get_loop(loop_id)


def recover() -> List[str]:
    """Backend start (after ``jobs.recover_interrupted``): loops left ``running`` -> ``interrupted``; the newest
    one with ``auto_resume`` continues automatically. -> the interrupted loop ids."""
    out = []
    for r in db.s5b_select("loop_runs", order="created_at", desc=True, limit=200):
        st = State(r["id"])
        if st.status == "running" and not is_active(r["id"]):
            if st.cancel_requested:            # review M1: a cancel is final, also across a restart
                _event(r["id"], "cancelled", data={"message": "Cancelled (the backend stopped before the loop "
                                                              "could finish its current step)."})
                continue
            _event(r["id"], "interrupted", data={"message": "The backend stopped while the loop was running."})
            out.append(r["id"])
    for lid in out:
        if (State(lid).config or {}).get("auto_resume"):
            try:
                resume(lid, actor="system")
            except LoopError as ex:
                logger.warning("loop %s: automatic resume refused: %s", lid, ex.user_message)
            break
    return out


# --- the orchestrator ----------------------------------------------------------------------------------

def _set_progress(loop_id: str, phase: str, message: str, **kw) -> None:
    with _lock:
        _progress[loop_id] = dict({"phase": phase, "message": str(message)[:300], "updated_at": _now_iso(),
                                   "generation": None, "variant_id": None, "slot": None, "run_id": None}, **kw)


def _orchestrate(loop_id: str, flags: _Flags) -> None:
    try:
        _event(loop_id, "running")
        _Runner(loop_id, flags).run()
    except _Stop as s:
        logger.info("loop %s: %s (%s)", loop_id, s.status, s.message)
        try:
            _event(loop_id, s.status, data=dict(s.data, message=s.message))
        except Exception:     # noqa: BLE001
            logger.exception("loop %s: could not record %s", loop_id, s.status)
    except Exception:     # noqa: BLE001 — every outcome ends visibly
        logger.exception("loop %s failed unexpectedly", loop_id)
        try:
            _event(loop_id, "failed", data={"message": "The discovery loop failed unexpectedly (see backend.log)."})
        except Exception:     # noqa: BLE001
            pass
    finally:
        with _lock:
            if _active.get(loop_id) is flags:
                _active.pop(loop_id, None)
            _progress.pop(loop_id, None)
            for rid in [r for r, lid in _loop_runs.items() if lid == loop_id]:
                _loop_runs.pop(rid, None)
        try:
            dispatch_validations()
        except Exception:     # noqa: BLE001
            logger.exception("loop %s: validation dispatch after the run failed", loop_id)


class _Runner:
    def __init__(self, loop_id: str, flags: _Flags):
        self.id = loop_id
        self.flags = flags
        self.st = State(loop_id)
        self.cfg = self.st.config
        self.n, self.g_max, self.p = self.cfg["variants"], self.cfg["generations"], self.cfg["search_points"]
        self.tier = self.cfg["model"]
        self.budget = budget_mod.LoopBudget(loop_id)
        self.model = None

    def refresh(self) -> State:
        self.st = State(self.id)
        return self.st

    def check_flags(self, *, allow_pause: bool = True) -> None:
        if self.flags.cancel.is_set():
            raise _Stop("cancelled", "The loop was cancelled.")
        if allow_pause and self.flags.pause.is_set():
            raise _Stop("paused", "Paused by the user — resume to continue.")

    # ---- main --------------------------------------------------------------------------------------------
    def run(self) -> None:
        self.check_flags()
        if self.st.seed is None:
            self.prepare_seed()
        for g in range(1, self.g_max + 1):
            st = self.refresh()
            if (st.generations.get(g) or {}).get("selection"):
                continue
            if g > 1 and not (st.generations.get(g) or {}).get("slots"):
                if st.run_cap_reached:
                    break
                if not self.plan(g):
                    break
            elif g == 1 and not (st.generations.get(1) or {}).get("slots"):
                self.plan(1)
            for v in self.refresh().gen_variants(g):
                self.check_flags()
                self.work_variant(v["id"], g)
            self.select(g)
            if self.refresh().run_cap_reached:
                break
        self.finish()

    # ---- seed --------------------------------------------------------------------------------------------
    def prepare_seed(self) -> None:
        seed = self.st.seed_in
        if seed["kind"] in ("strategy", "paper"):
            row = db.strategy_get(seed["strategy_id"])
            if row is None:
                raise _Stop("failed", "The seed strategy does not exist any more.")
            _event(self.id, "seed_ready", data={"strategy_id": row["id"], "version": int(seed["version"]),
                                                 "family_id": row.get("family_id"), "root_created": False})
            self.refresh()
            return
        _set_progress(self.id, "seed", "AI drafts the seed strategy from the idea")
        spec, prov = self.seed_draft(seed["text"])
        from ..lifecycle import registry as lc
        res = lc.create_from_loop(self.named(spec, 1, 1), dict(prov, loop_id=self.id, generation=1, variant_id=None))
        row = db.strategy_get(res["id"])
        _event(self.id, "seed_ready", data={"strategy_id": res["id"], "version": res["version"],
                                            "family_id": row.get("family_id"), "root_created": True})
        self.refresh()

    def seed_draft(self, text: str):
        """The Studio path for an idea seed: Studio prompt + scope guard + validator (≤ 1 repair) + topic check."""
        from ..research import memory, prompts as sp, studio, topic_guard
        from strategy_platform.core import llm
        mem = memory.select_for_prompt(None, None)
        messages = sp.build_messages(text, current_spec=None, last_run=None, memory_facts=mem, history=[])
        audit = {"loop_id": self.id, "role": "seed", "model_tier": self.tier}
        parsed, raw, used, usage, est, cost = self.call(messages, audit, feature=SEED_FEATURE)
        reason = sp.scope_guard(parsed, sp.CLARIFY_MAX_IN_A_ROW)
        if reason is not None or (parsed or {}).get("kind") != "draft":
            self.store_ai(None, "seed", {"discarded": True, "discarded_reason": reason or "not_a_draft"}, None, used,
                          usage, est, cost)
            raise _Stop("failed", "The idea could not be turned into a strategy: " + SCOPE_NOTE)
        try:
            ans = sp.validate_answer(parsed)
        except ValueError as ve:
            raise _Stop("failed", f"The AI answer for the seed did not have the required format ({ve}).")
        val = studio._validate_spec(ans["spec"])
        if not val["ok"]:
            rmsgs = sp.repair_messages(messages, raw, val["errors"])
            p2, raw2, used, u2, e2, c2 = self.call(rmsgs, dict(audit, repair=True), feature=SEED_FEATURE)
            cost += c2
            if sp.scope_guard(p2, 0) is None and (p2 or {}).get("kind") == "draft":
                try:
                    ans = sp.validate_answer(p2)
                    val = studio._validate_spec(ans["spec"])
                except ValueError:
                    pass
        if not val["ok"]:
            self.store_ai(None, "seed", {"discarded": True, "discarded_reason": "draft_invalid",
                                         "validation": val}, None, used, usage, est, cost)
            raise _Stop("failed", "The AI could not draft a valid strategy from the idea (validator errors: "
                                  + "; ".join(str(e.get("message"))[:120] for e in val["errors"][:3]) + ").")
        topic = asyncio.run(topic_guard.check(sp.displayable_texts(ans, {"spec": ans["spec"]}), self.budget, audit))
        out = {"reply": ans["reply"], "assumptions": ans["assumptions"], "operator_requests": ans["operator_requests"],
               "spec": studio._clean_spec(ans["spec"]), "kind": "draft"}
        if not topic["on_topic"]:
            self.store_ai(None, "seed", {"discarded": True, "discarded_reason": "topic_check_failed"}, None, used,
                          usage, est, cost)
            raise _Stop("failed", "The idea could not be turned into a strategy: " + TOPIC_FAILED)
        mid = self.store_ai(None, "seed", out, None, used, usage, est, cost, topic="passed")
        prov = {"feature": SEED_FEATURE, "model": used, "model_tier": self.tier, "message_id": mid,
                "prompt_hash": sp.prompt_hash(messages), "generated_at": _now_iso()}
        return out["spec"], prov

    # ---- LLM -----------------------------------------------------------------------------------------------
    def call(self, messages, audit, *, feature=FEATURE, max_tokens=MAX_TOKENS, temperature=TEMPERATURE):
        """One budget-checked call through the Studio's ``_call`` with the LOOP budget -> (parsed, raw, used,
        usage, estimate, cost). Budget blocks: day/month/off/unreadable -> _Stop(paused_budget); per-run cap ->
        _RunCap; no key / no model -> _Stop(paused); other errors -> LoopError (the variant fails)."""
        from ..research import studio
        if self.model is None:
            try:
                self.model = explain._model_id(self.tier)
            except explain.ExplainError as ex:
                raise _Stop("paused", ex.user_message)
        before = self.budget.run_spent
        try:
            parsed, raw, used, usage, est, _cost = asyncio.run(studio._call(
                messages, self.model, self.tier, self.budget, dict(audit, loop_id=self.id), feature=feature,
                feature_label=FEATURE_LABEL, max_tokens=max_tokens, temperature=temperature))
        except studio.StudioBudgetBlocked as ex:
            msg = ex.user_message
            if msg == budget_mod.RUN_REACHED_MESSAGE:
                raise _RunCap(msg)
            raise _Stop("paused_budget", msg)
        except explain.ExplainError as ex:
            if ex.code in ("no_key", "no_model"):
                raise _Stop("paused", ex.user_message)
            raise LoopError(ex.status, ex.code, ex.user_message, cost_usd=round(self.budget.run_spent - before, 6))
        return parsed, raw, used, usage, est, round(self.budget.run_spent - before, 6)

    def store_ai(self, vid, role, output, check, used, usage, est, cost, topic="pending") -> int:
        from strategy_platform.core import llm
        prov = dict(llm.provenance(DIAG_FEATURE if role == "diagnosis" else FEATURE, used or self.model or ""),
                    model_tier=self.tier, role=role, loop_id=self.id)
        u = {"cost_usd": round(cost, 6), "estimate_usd": round(est or 0.0, 6),
             "prompt_tokens": int((usage or {}).get("prompt_tokens") or 0),
             "completion_tokens": int((usage or {}).get("completion_tokens") or 0)}
        mid = _ai_insert(self.id, vid, role, dict(output, topic_check=topic), check, prov, u, cost)
        if vid:
            _event(self.id, "ai_message", variant_id=vid, data={"message_id": mid, "role": role,
                                                                "cost_usd": round(cost, 6)})
        return mid

    # ---- planning ------------------------------------------------------------------------------------------
    def named(self, spec: dict, g: int, slot: int) -> dict:
        from ..strategy import limits
        out = dict(spec)
        suffix = f" (loop {self.id[3:9]} g{g}·{slot})"
        base = str(out.get("name") or "Loop variant")
        out["name"] = (base[:max(1, limits.MAX_NAME_LEN - len(suffix))] + suffix)[:limits.MAX_NAME_LEN]
        return out

    def plan(self, g: int) -> bool:
        """Plans generation ``g`` in ONE transaction (review L8): the variant rows, ``generation_planned`` and the
        elites' carry-over (``variant_scored``) — a crash leaves either nothing (re-planned on resume) or all."""
        st = self.st
        _set_progress(self.id, "plan", f"planning generation {g}", generation=g)
        rows, slots = [], []

        def variant(slot, origin, parent):
            vid = "lv-" + uuid.uuid4().hex[:12]
            rows.append(("loop_variants", {"id": vid, "loop_id": self.id, "generation": g, "slot": slot,
                                           "origin": origin, "parent_variant_id": parent}))
            slots.append(vid)
            return vid

        def event(name, vid, data):
            rows.append(("loop_events", {"loop_id": self.id, "event": name, "generation": g, "variant_id": vid,
                                         "data_json": json.dumps(data, ensure_ascii=False, default=str)}))

        later = []
        if g == 1:
            seed = st.seed
            vid = variant(1, "seed", None)
            spec = json.loads(db.strategy_version_get(seed["strategy_id"], seed["version"])["spec_json"])
            for slot in range(2, self.n + 1):
                variant(slot, "mutation", vid)
            later.append(("variant_saved", vid, {"strategy_id": seed["strategy_id"], "version": seed["version"],
                                                 "kind": "base", "logic_hash": search.logic_hash(spec)}))
        else:
            sel = st.generations[g - 1]["selection"]
            elites, refine, culled = sel["elites"], sel["refine"], sel["culled"]
            if not elites and not refine:
                _event(self.id, "note", generation=g, data={"text": f"Generation {g} not planned: no usable "
                                                                    f"variant in generation {g - 1}."})
                self.refresh()
                return False
            slot = 0
            for e in elites:
                slot += 1
                vid = variant(slot, "elite", e)
                ev = st.variants[e]
                later.append(("variant_scored", vid, {"chosen_version": ev["chosen_version"], "run_id": ev["run_id"],
                                                      "score": ev["score"], "strategy_id": ev["strategy_id"],
                                                      "carried_from": e}))
            for r in refine:
                slot += 1
                variant(slot, "refined", r)
            for i in range(len(culled) if elites else 0):
                slot += 1
                variant(slot, "child", elites[i % len(elites)])
        event("generation_planned", None, {"slots": list(slots)})
        for name, vid, data in later:
            event(name, vid, data)
        db.s5b_insert_many(rows)
        self.refresh()
        return True

    # ---- one variant ---------------------------------------------------------------------------------------
    def work_variant(self, vid: str, g: int) -> None:
        from ..backtest import runcard
        from ..lifecycle import registry as lc
        v = self.st.variants[vid]
        if v["state"] in ("failed", "duplicate", "scored"):
            return
        if not v["versions"]:
            if v["origin"] in ("seed", "elite"):
                return
            if self.st.run_cap_reached:
                _event(self.id, "variant_failed", generation=g, variant_id=vid,
                       data={"reason": f"Not created: {budget_mod.RUN_REACHED_MESSAGE}", "code": "run_cap"})
                self.refresh()
                return
            try:
                self.ai_variant(vid, g)
            except _RunCap as rc:
                _event(self.id, "run_cap_reached", generation=g, data={"message": str(rc)})
                _event(self.id, "variant_failed", generation=g, variant_id=vid,
                       data={"reason": f"Not created: {rc}", "code": "run_cap"})
            except LoopError as ex:
                _event(self.id, "variant_failed", generation=g, variant_id=vid,
                       data={"reason": ex.user_message, "code": ex.code})
            except (lc.Conflict, lc.SpecInvalid, lc.NotFound) as ex:      # review M4: the variant, not the loop
                _event(self.id, "variant_failed", generation=g, variant_id=vid,
                       data={"reason": _conflict_text(ex), "code": "conflict"})
            v = self.refresh().variants[vid]
            if v["state"] in ("failed", "duplicate"):
                return
        for x in sorted(v["versions"], key=lambda x: x["version"]):
            if x["run_status"] in ("done", "failed", "cancelled", "not_run"):
                continue
            self.check_flags()
            try:
                self.backtest(vid, g, v["strategy_id"], x["version"], x)
            except (LoopError, runcard.ResumeRefused, runcard.NotFound, lc.Conflict, lc.NotFound) as ex:
                code = getattr(ex, "code", None) or "start_failed"
                why = ex.user_message if isinstance(ex, UserFacingError) else _conflict_text(ex)
                _event(self.id, "backtest_finished", generation=g, variant_id=vid,
                       data={"version": x["version"], "run_id": x.get("run_id"), "status": "not_run", "error": why,
                             "code": code, "score": None})
        v = self.refresh().variants[vid]
        scored = [x for x in v["versions"] if x.get("score") and (x["score"] or {}).get("score") is not None]
        errs = [x for x in v["versions"] if x.get("code")]
        if not scored and errs:
            _event(self.id, "variant_failed", generation=g, variant_id=vid,
                   data={"reason": errs[0]["error"], "code": errs[0]["code"]})
        elif scored:
            best = max(scored, key=lambda x: (x["score"]["score"], 1 if x["kind"] == "base" else 0, -x["version"]))
            _event(self.id, "variant_scored", generation=g, variant_id=vid,
                   data={"chosen_version": best["version"], "run_id": best["run_id"], "score": best["score"],
                         "strategy_id": v["strategy_id"]})
        else:
            first = sorted(v["versions"], key=lambda x: x["version"])[0] if v["versions"] else {}
            why = (first.get("score") or {}).get("reason") or first.get("error") or "no finished backtest"
            _event(self.id, "variant_scored", generation=g, variant_id=vid,
                   data={"chosen_version": first.get("version"), "run_id": first.get("run_id"),
                         "score": first.get("score"), "strategy_id": v["strategy_id"], "reason": why})
        self.refresh()

    def parent_context(self, parent: dict) -> dict:
        """spec, card, score card, run facts (≤ 60, holdout filtered), full fact table, key metrics."""
        from ..backtest import metrics as metrics_mod, runcard
        from ..research import prompts as sp
        spec = json.loads(db.strategy_version_get(parent["strategy_id"], int(parent["chosen_version"]))["spec_json"])
        card = runcard.get(parent["run_id"]) if parent.get("run_id") else None
        facts = []
        if card is not None:
            scoring.holdout_guard(card)
            if isinstance(card.get("attribution"), dict) and card["attribution"].get("facts"):
                facts = explain.build_context(card)["facts"]
        sc = parent.get("score")
        sfacts = scoring.score_facts(sc) if sc else []
        return {"spec": spec, "card": card, "score": sc, "score_facts": sfacts,
                "run_facts": sp.last_run_facts(facts), "all_facts": sfacts + facts,
                "key_metrics": metrics_mod.summary(card.get("metrics")) if card else None}

    def siblings(self, g: int, exclude: str) -> List[dict]:
        out = []
        for v in self.st.gen_variants(g):
            if v["id"] == exclude or not v["versions"] or not v["strategy_id"]:
                continue
            base = sorted(v["versions"], key=lambda x: x["version"])[0]
            row = db.strategy_version_get(v["strategy_id"], base["version"])
            if row:
                out.append(lp.sibling_of(json.loads(row["spec_json"])))
        return out

    def ai_variant(self, vid: str, g: int) -> None:
        from ..lifecycle import registry as lc
        from ..research import memory, prompts as sp, studio, topic_guard
        st = self.st
        v = st.variants[vid]
        parent = st.variants[v["parent_variant_id"]]
        if parent.get("carried_from"):
            parent = dict(parent, versions=st.variants[parent["carried_from"]]["versions"])
        _event(self.id, "ai_started", generation=g, variant_id=vid)
        _set_progress(self.id, "ai", f"generation {g}, slot {v['slot']}: AI {v['origin']}", generation=g,
                      variant_id=vid, slot=v["slot"])
        ctx = self.parent_context(parent)
        fam = (st.seed or {}).get("family_id")
        mem = [{"id": f["id"], "kind": f["kind"], "family": f["family_id"], "text": (f.get("fact") or {}).get("text")}
               for f in memory.select_for_prompt(fam, ctx["spec"])]
        audit = {"loop_id": self.id, "variant_id": vid, "generation": g, "model_tier": self.tier}
        diagnosis, diag_mid = None, None
        stored = {}                               # a resumed variant reuses its stored (not yet topic-checked) steps
        for m in db.s5b_select("loop_ai_messages", {"loop_id": self.id, "variant_id": vid}):
            out_m = m.get("output") or {}
            if not out_m.get("discarded") and (m["role"] != "refinement" or out_m.get("valid")):
                stored[m["role"]] = m
        if "diagnosis" in stored:
            diagnosis = {k: stored["diagnosis"]["output"][k] for k in ("summary", "weaknesses", "ideas")}
            diag_mid = stored["diagnosis"]["id"]
        if v["origin"] in ("refined", "child") and diagnosis is None:
            dmsgs = lp.diagnosis_messages(ctx["score_facts"], ctx["run_facts"], mem, ctx["key_metrics"])
            parsed, _raw, used, usage, est, cost = self.call(dmsgs, dict(audit, role="diagnosis"), feature=DIAG_FEATURE,
                                                             max_tokens=DIAG_MAX_TOKENS, temperature=DIAG_TEMPERATURE)
            try:
                diagnosis = lp.validate_diagnosis(parsed)
            except ValueError as ve:
                self.store_ai(vid, "diagnosis", {"discarded": True, "discarded_reason": "bad_json"}, None, used,
                              usage, est, cost, topic="skipped")
                raise LoopError(502, "bad_json", f"The AI diagnosis did not have the required format ({ve}) — "
                                                 f"charged ${cost:.4f}.")
            check = self.number_check(diagnosis, ctx["all_facts"])
            diag_mid = self.store_ai(vid, "diagnosis", diagnosis, check, used, usage, est, cost)
        aspect = lp.MUTATION_ASPECTS[(int(v["slot"]) - 2) % len(lp.MUTATION_ASPECTS)] if v["origin"] == "mutation" \
            else None
        rmsgs = lp.refine_messages(v["origin"], aspect, ctx["spec"], diagnosis, ctx["score"], ctx["run_facts"], mem,
                                   self.siblings(g, vid))
        if lp.size(rmsgs) > lp.PROMPT_MAX_CHARS:
            raise LoopError(409, "prompt_too_large", "The refinement context is too large.")
        if "refinement" in stored:
            out = dict(stored["refinement"]["output"])
            out.pop("topic_check", None)
            ans = {"reply": out["reply"], "assumptions": out["assumptions"],
                   "operator_requests": out["operator_requests"], "spec": out["spec"], "kind": "draft"}
            ref_mid = stored["refinement"]["id"]
            used = (stored["refinement"].get("provenance") or {}).get("model")
            return self.after_refinement(vid, g, v, parent, diagnosis, diag_mid, ans, out, ref_mid, used, rmsgs,
                                         audit)
        parsed, raw, used, usage, est, cost = self.call(rmsgs, dict(audit, role="refinement"))
        reason = sp.scope_guard(parsed, sp.CLARIFY_MAX_IN_A_ROW)
        if reason is None and (parsed or {}).get("kind") != "draft":
            reason = "not_a_draft"
        if reason is not None:
            self.store_ai(vid, "refinement", {"discarded": True, "discarded_reason": reason}, None, used, usage, est,
                          cost, topic="skipped")
            raise LoopError(422, "discarded", f"{SCOPE_NOTE} ({reason})")
        try:
            ans = sp.validate_answer(parsed)
        except ValueError as ve:
            self.store_ai(vid, "refinement", {"discarded": True, "discarded_reason": "bad_json"}, None, used, usage,
                          est, cost, topic="skipped")
            raise LoopError(502, "bad_json", f"The AI refinement did not have the required format ({ve}) — charged "
                                             f"${cost:.4f}.")
        val = studio._validate_spec(ans["spec"])
        repaired, repair_errors = False, None
        if not val["ok"]:
            repair_errors = val["errors"]
            try:
                p2, _r2, used, u2, e2, c2 = self.call(sp.repair_messages(rmsgs, raw, val["errors"]),
                                                       dict(audit, role="refinement", repair=True))
            except _RunCap:
                p2, c2 = None, 0.0
            cost += c2
            if p2 is not None and sp.scope_guard(p2, 0) is None and (p2 or {}).get("kind") == "draft":
                try:
                    a2 = sp.validate_answer(p2)
                    v2 = studio._validate_spec(a2["spec"])
                    ans, val, repaired = a2, v2, True
                except ValueError:
                    pass
        out = {"kind": "draft", "reply": ans["reply"], "assumptions": ans["assumptions"],
               "operator_requests": ans["operator_requests"], "spec": studio._clean_spec(ans["spec"]),
               "valid": val["ok"], "validation": val, "repaired": repaired, "repair_errors": repair_errors,
               "task": v["origin"], "aspect": aspect, "operator_requests_label": "unverified AI suggestion"}
        if not val["ok"]:
            out.update(reply="", assumptions=[], operator_requests=[], discarded=True, discarded_reason="draft_invalid")
            self.store_ai(vid, "refinement", out, None, used, usage, est, cost, topic="skipped")
            raise LoopError(422, "draft_invalid", "The AI variant did not pass the validator: "
                            + "; ".join(str(e.get("message"))[:120] for e in val["errors"][:3]))
        ref_mid = self.store_ai(vid, "refinement", out, None, used, usage, est, cost)
        return self.after_refinement(vid, g, v, parent, diagnosis, diag_mid, ans, out, ref_mid, used, rmsgs, audit)

    def after_refinement(self, vid, g, v, parent, diagnosis, diag_mid, ans, out, ref_mid, used, rmsgs, audit):
        """Topic check of every displayable text -> universe / duplicate checks -> save + search points."""
        from ..lifecycle import registry as lc
        from ..research import prompts as sp, topic_guard
        st = self.st
        texts = lp.diagnosis_texts(diagnosis) + sp.displayable_texts(ans, {"spec": ans["spec"]})
        topic = asyncio.run(topic_guard.check(texts, self.budget, dict(audit, role="topic_check")))
        if not topic["on_topic"] and topic["calls"] == 0 and self.budget.last_block:
            # review L5: the topic check was BLOCKED by the budget — not an off-topic verdict (§16.6)
            if self.budget.last_block == budget_mod.RUN_REACHED_MESSAGE:
                raise _RunCap(self.budget.last_block)
            raise _Stop("paused_budget", self.budget.last_block)
        _event(self.id, "topic_checked", generation=g, variant_id=vid,
               data={"status": topic["status"], "reason_audit": topic["reason"], "message_ids": [diag_mid, ref_mid],
                     "calls": topic["calls"]})
        if not topic["on_topic"]:
            raise LoopError(422, "topic_check_failed", TOPIC_FAILED)
        spec = out["spec"]
        from ..validation import families
        seed_spec = json.loads(db.strategy_version_get(st.seed["strategy_id"], st.seed["version"])["spec_json"])
        try:
            same = families.universe_key(spec) == families.universe_key(seed_spec)
        except Exception:     # noqa: BLE001 — an unresolvable universe is not the seed's universe
            same = False
        if not same:
            raise LoopError(422, "universe_changed", UNIVERSE_CHANGED)
        h = search.logic_hash(spec)
        known = self.st.known_hashes()
        if h in known:
            other = next((x for x in st.variants.values() for y in x["versions"] if y.get("logic_hash") == h), None)
            _event(self.id, "variant_duplicate", generation=g, variant_id=vid,
                   data={"reason": "identical trading logic to variant " + (other or {}).get("id", "?")
                                   + " — not backtested (no trial)", "of": (other or {}).get("id")})
            return
        prov = {"feature": FEATURE, "model": used, "model_tier": self.tier, "loop_id": self.id, "generation": g,
                "variant_id": vid, "message_id": ref_mid, "prompt_hash": sp.prompt_hash(rmsgs),
                "generated_at": _now_iso()}
        named = self.named(spec, g, int(v["slot"]))
        prow = db.strategy_get(parent["strategy_id"])
        if v["origin"] == "refined" and prow is not None and prow["origin"] == "loop" \
                and prow["id"] != st.seed["strategy_id"]:
            res = lc.new_loop_version(prow["id"], named, int(prow["current_version"]), prov)
        else:
            res = lc.create_from_loop(named, prov, {"strategy_id": parent["strategy_id"],
                                                    "version": int(parent["chosen_version"])})
        if res.get("unchanged"):
            raise LoopError(422, "duplicate", "The refined spec is identical to the strategy's current version.")
        _event(self.id, "variant_saved", generation=g, variant_id=vid,
               data={"strategy_id": res["id"], "version": res["version"], "kind": "base", "logic_hash": h})
        known.add(h)
        base_version = res["version"]
        for cand in search.candidates(spec, self.p, exclude=known):
            sprov = dict(prov, feature="sp-loop-search", parameter=cand["parameter"], value=cand["value"],
                         derived_from_version=base_version)
            cur = db.strategy_get(res["id"])
            r2 = lc.new_loop_version(res["id"], self.named(cand["spec"], g, int(v["slot"])),
                                     int(cur["current_version"]), sprov)
            if r2.get("unchanged"):
                continue
            _event(self.id, "variant_saved", generation=g, variant_id=vid,
                   data={"strategy_id": res["id"], "version": r2["version"], "kind": "search",
                         "logic_hash": cand["logic_hash"], "parameter": cand["parameter"], "value": cand["value"]})
            known.add(cand["logic_hash"])
        self.refresh()

    @staticmethod
    def number_check(diag: dict, facts: List[dict]) -> dict:
        pieces = [("summary", diag["summary"])] + [(f"weaknesses.{i}", t) for i, t in enumerate(diag["weaknesses"])] \
            + [(f"ideas.{i}", t) for i, t in enumerate(diag["ideas"])]
        unverified, unknown = [], []
        for key, text in pieces:
            r = explain.number_check({"summary": text, "sections": [], "hypotheses": [], "limits": []}, facts, "en")
            unverified += [dict(u, section=key) for u in r["unverified"]]
            unknown += [i for i in r["unknown_ids"] if i not in unknown]
        return {"ok": not unverified and not unknown, "unverified": unverified, "unknown_ids": unknown}

    # ---- backtests on the shared slot (§16.8) ----------------------------------------------------------------
    def request(self) -> dict:
        return {"history_from": "2000"} if self.cfg.get("history_from") == "2000" else {}

    def reusable_seed_card(self, sid: str, version: int) -> Optional[dict]:
        return reusable_seed_card(sid, version, self.cfg.get("history_from"))

    def backtest(self, vid: str, g: int, sid: str, version: int, x: dict) -> None:
        from ..backtest import jobs, runcard
        v = self.st.variants[vid]
        run_id = x.get("run_id")
        if run_id is None and v["origin"] == "seed":
            card = self.reusable_seed_card(sid, version)
            if card is not None:
                _event(self.id, "backtest_started", generation=g, variant_id=vid,
                       data={"version": version, "run_id": card["id"], "reused": True})
                self.finished(vid, g, version, card)
                return
        while True:
            if run_id is not None:
                card = runcard.get(run_id)
                if card is None:
                    run_id = None
                    continue
                status = card["status"]
                if status in ("queued", "running"):
                    run_id = self.wait_job(vid, g, run_id)
                    continue
                if status == "done":
                    self.finished(vid, g, version, card)
                    return
                if status == "failed" and card.get("error_kind") in ("interrupted", "connection"):
                    if card.get("error_kind") == "connection" and runcard.resumable(card):
                        self.wait_connection(vid, g, card)
                        try:
                            new = self.submit(vid, g, lambda: jobs.resume(run_id))
                        except runcard.ResumeRefused as ex:          # review M4: §16.1 — a fresh backtest
                            logger.info("loop %s: resume of %s refused (%s) — fresh backtest", self.id, run_id,
                                        ex.message)
                            run_id = None
                            continue
                        _event(self.id, "backtest_started", generation=g, variant_id=vid,
                               data={"version": version, "run_id": new, "resumed_from": run_id})
                        run_id = new
                        continue
                    run_id = None              # interrupted: a fresh backtest (a failed card is never a trial)
                    continue
                if status == "cancelled" and self.flags.cancel.is_set():
                    raise _Stop("cancelled", "The loop was cancelled.")
                self.finished(vid, g, version, card)
                return
            req = self.request()
            new = self.submit(vid, g, lambda: jobs.manager().submit(sid, version, req,
                                                                    auto_resume=bool(self.cfg.get("auto_resume"))))
            _event(self.id, "backtest_started", generation=g, variant_id=vid, data={"version": version, "run_id": new})
            run_id = new

    def submit(self, vid: str, g: int, fn) -> str:
        """Waits for a fair turn on the slot (§16.8) and submits -> run id. Busy -> keeps waiting."""
        from ..backtest import jobs
        global _last_job_end
        msg_shown = None
        while True:
            self.check_flags()
            mgr = jobs.manager()
            active = mgr.active_run_id()
            others = _others_waiting()
            wait_yield = time.monotonic() - _last_job_end < YIELD_SECONDS
            if active is None and not others and not wait_yield:
                try:
                    run_id = fn()
                except jobs.JobBusy:
                    continue
                except UserFacingError as ex:
                    raise LoopError(503, "start_failed", f"The backtest could not start: {ex.user_message}")
                with _lock:
                    _loop_runs[run_id] = self.id
                _set_progress(self.id, "backtest", "backtest running", generation=g, variant_id=vid, run_id=run_id)
                return run_id
            text = ("waiting for the backtest slot (another job is running)" if active else
                    "waiting: Studio / paper drafts get the free slot first" if others else
                    "yielding the slot to manual runs for a moment")
            if text != msg_shown:
                _set_progress(self.id, "waiting_slot", text, generation=g, variant_id=vid)
                msg_shown = text
            _wake.wait(POLL_SECONDS)
            _wake.clear()

    def wait_job(self, vid: str, g: int, run_id: str) -> str:
        from ..backtest import jobs, runcard
        global _last_job_end
        cancelled = False
        while True:
            card = runcard.get(run_id)
            if card is None or card["status"] not in ("queued", "running"):
                break
            if self.flags.cancel.is_set() and not cancelled:
                jobs.manager().cancel(run_id)
                cancelled = True
            mgr = jobs.manager()
            if mgr.is_waiting(run_id):
                _set_progress(self.id, "waiting_connection", (mgr.progress(run_id) or {}).get("message")
                              or "Connection lost — waiting", generation=g, variant_id=vid, run_id=run_id)
            else:
                p = mgr.progress(run_id) or {}
                _set_progress(self.id, "backtest" if card.get("kind") == "backtest" else "validation",
                              f"{p.get('phase', 'queued')}: {p.get('message', '')}", generation=g, variant_id=vid,
                              run_id=run_id, percent=p.get("percent"))
            mgr.wait(POLL_SECONDS)
        _last_job_end = time.monotonic()
        return run_id

    def wait_connection(self, vid: str, g: int, card: dict) -> None:
        """status waiting_connection: probe the failed source at most every 30 s (no time limit, cancellable)."""
        source = "alpaca"
        try:
            from ..data import network
            msg = f"Connection lost — waiting for the data source (run {card['id']})…"
            while True:
                _set_progress(self.id, "waiting_connection", msg, generation=g, variant_id=vid, run_id=card["id"])
                if self.flags.cancel.wait(max(PROBE_INTERVAL_SECONDS, float(network.next_probe_in(source)))):
                    raise _Stop("cancelled", "The loop was cancelled.")
                if self.flags.pause.is_set():
                    raise _Stop("paused", "Paused by the user — resume to continue.")
                if network.probe(source):
                    return
        except _Stop:
            raise
        except UserFacingError as ex:
            raise LoopError(503, "connection", ex.user_message)

    def finished(self, vid: str, g: int, version: int, card: dict) -> None:
        sc, err = None, None
        if card["status"] == "done":
            try:
                sc = scoring.score(card)
            except scoring.HoldoutDataRefused as ex:
                raise _Stop("failed", f"Holdout data refused: {ex}")
        else:
            err = card.get("error") or f"the backtest is {card['status']}"
        _event(self.id, "backtest_finished", generation=g, variant_id=vid,
               data={"version": version, "run_id": card["id"], "status": card["status"], "error": err, "score": sc})
        self.refresh()

    # ---- selection + finish --------------------------------------------------------------------------------------
    def population(self, g: int) -> List[dict]:
        from ..backtest import runcard
        out = []
        for v in self.st.gen_variants(g):
            sc = v.get("score") or {}
            card = runcard.get(v["run_id"]) if v.get("run_id") and sc.get("score") is not None else None
            out.append({"variant_id": v["id"], "slot": v["slot"], "score": sc.get("score"), "sharpe": sc.get("sharpe"),
                        "checks_passed": bool(sc.get("checks_passed")), "state": v["state"],
                        "reason": v.get("reason"), "returns": selection.returns_of((card or {}).get("curve"))})
        return out

    def select(self, g: int) -> None:
        _set_progress(self.id, "selection", f"selection of generation {g}", generation=g)
        res = selection.select(self.population(g), self.n)
        _event(self.id, "selection", generation=g, data=res)
        self.refresh()

    def finish(self) -> None:
        st = self.refresh()
        g = st.last_selected_generation()
        if st.finalists is None:
            if g is None:
                _event(self.id, "finalists", data={"variants": [], "note": NO_FINALIST_MESSAGE, "generation": None})
            else:
                sel = st.generations[g]["selection"]
                fin = selection.finalists(self.population(g), self.n, sel.get("duplicates") or {})
                _event(self.id, "finalists", generation=g, data={"variants": fin, "generation": g,
                                                                   "note": None if fin else NO_FINALIST_MESSAGE,
                                                                   "autonomous": bool(self.cfg.get("autonomous"))})
                for vid in fin:
                    self.promote(vid)
            st = self.refresh()
        if self.cfg.get("autonomous"):
            for vid in (st.finalists or {}).get("variants") or []:
                self.check_flags()
                self.validate(vid)
        self.write_memory()
        note = RUN_CAP_NOTE if self.refresh().run_cap_reached else None
        _event(self.id, "done", data={"note": note, "message": (st.finalists or {}).get("note")})

    def promote(self, vid: str) -> None:
        from ..lifecycle import registry as lc
        v = self.st.variants[vid]
        row = db.strategy_get(v["strategy_id"])
        if row is None or row["origin"] != "loop" or int(row["current_version"]) == int(v["chosen_version"]):
            _event(self.id, "promoted", variant_id=vid, data={"strategy_id": v["strategy_id"],
                                                               "version": int(v["chosen_version"]),
                                                               "from_version": None})
            return
        spec = json.loads(db.strategy_version_get(row["id"], int(v["chosen_version"]))["spec_json"])
        try:
            res = lc.new_loop_version(row["id"], spec, int(row["current_version"]),
                                      {"feature": "sp-loop-promote", "loop_id": self.id, "variant_id": vid,
                                       "derived_from_version": int(v["chosen_version"]), "generated_at": _now_iso()})
        except (lc.Conflict, lc.SpecInvalid, lc.NotFound) as ex:
            _event(self.id, "promoted", variant_id=vid,
                   data={"strategy_id": v["strategy_id"], "version": int(v["chosen_version"]), "from_version": None,
                         "note": "not promoted: " + _conflict_text(ex)})
            return
        _event(self.id, "promoted", variant_id=vid,
               data={"strategy_id": row["id"], "version": res["version"], "from_version": int(v["chosen_version"]),
                     "note": "same trading logic as the evaluated version (no new backtest)"})

    def validate(self, vid: str) -> None:
        """Autonomous mode: the finalist's validation run (G1–G8; NEVER the holdout)."""
        from ..backtest import runcard
        from ..validation import jobs as vjobs
        fin = finalist_state(self.refresh(), vid)
        if fin["state"] == "validation_start_failed":
            return
        if fin["validation_run_id"] is None:
            hist = "2000" if self.cfg.get("history_from") == "2000" else None
            try:
                rid = self.submit(vid, None, lambda: vjobs.service().submit_validation(
                    fin["strategy_id"], fin["version"], hist, auto_resume=bool(self.cfg.get("auto_resume"))))
            except (LoopError, runcard.NotFound) as ex:
                _event(self.id, "validation_finished", variant_id=vid,
                       data={"run_id": None, "status": "start_failed",
                             "error": ex.user_message if isinstance(ex, UserFacingError) else _conflict_text(ex)})
                return
            _event(self.id, "validation_started", variant_id=vid, data={"run_id": rid, "actor": "loop"})
            fin["validation_run_id"] = rid
        card = runcard.get(fin["validation_run_id"])
        if card is not None and card["status"] in ("queued", "running"):
            self.wait_job(vid, None, card["id"])
        _record_validation(self.id, vid, fin["validation_run_id"])

    def write_memory(self) -> None:
        st = self.refresh()
        if not st.seed:
            return
        scored = [v for v in st.variants.values() if (v.get("score") or {}).get("score") is not None
                  and v["origin"] != "elite"]
        best = max(scored, key=lambda v: v["score"]["score"]) if scored else None
        fins = (st.finalists or {}).get("variants") or []
        failed = [v for v in st.variants.values() if v["state"] in ("failed", "duplicate")]
        reasons = {}
        for v in failed:
            key = (v.get("reason") or "")[:60]
            reasons[key] = reasons.get(key, 0) + 1
        text = (f"Discovery loop {self.id}: {self.n} variants × {self.g_max} generations, "
                f"{st.backtests_started} backtests; best score "
                + (f"{best['score']['score']:.2f} (Sharpe {best['score']['sharpe']:.2f}, {best['strategy_id']})"
                   if best and best["score"].get("sharpe") is not None else "n/a")
                + f"; {len(fins)} finalist(s); {len(failed)} failed/duplicate variant(s).")
        fact = {"text": text, "data": {"loop_id": self.id, "backtests": st.backtests_started,
                                       "best": None if not best else {"variant_id": best["id"],
                                                                      "strategy_id": best["strategy_id"],
                                                                      "score": best["score"]["score"],
                                                                      "sharpe": best["score"]["sharpe"]},
                                       "finalists": fins, "failures": reasons},
                "universe_key": None, "operators": [], "run_kind": "loop", "range": None}
        try:
            from ..research import memory
            seed_spec = json.loads(db.strategy_version_get(st.seed["strategy_id"], st.seed["version"])["spec_json"])
            fact["universe_key"] = memory.universe_key_of(seed_spec)
            fact["operators"] = memory.operators_of(seed_spec)
        except Exception:     # noqa: BLE001
            pass
        db.memory_facts_insert(self.id, [{"family_id": st.seed.get("family_id") or st.seed["strategy_id"],
                                          "strategy_id": st.seed["strategy_id"], "version": int(st.seed["version"]),
                                          "run_id": self.id, "kind": "loop_result",
                                          "fact_json": json.dumps(fact, ensure_ascii=False, default=str)}])


class _RunCap(Exception):
    """The per-run AI cap blocks a call: the loop stops creating variants (§16.6)."""


def _others_waiting() -> bool:
    """A Studio draft or paper replication waits for the slot (they get a free slot first, §16.8)."""
    try:
        from ..research import studio
        if studio._waiting(studio._latest_events()):
            return True
    except Exception:     # noqa: BLE001
        logger.debug("studio queue unreadable", exc_info=True)
    try:
        from ..research import paper_strategy
        if any(r["event"] == "waiting" for r in paper_strategy._latest().values()):
            return True
    except Exception:     # noqa: BLE001
        logger.debug("paper queue unreadable", exc_info=True)
    return False


def on_slot_free() -> None:
    """``backtest.jobs._after_job`` hook: wakes the orchestrator, dispatches approved finalists' validations."""
    global _last_job_end
    _last_job_end = time.monotonic()
    _wake.set()
    if db.is_open():
        dispatch_validations()


# --- finalists ----------------------------------------------------------------------------------------------

def finalist_state(st: State, vid: str) -> dict:
    from ..backtest import runcard
    from ..validation import report
    v = st.variants[vid]
    evs = st.finalist_events.get(vid) or []
    out = {"variant_id": vid, "strategy_id": v["strategy_id"], "version": int(v["chosen_version"]),
           "evaluated_version": int(v["chosen_version"]), "promoted": False, "score": (v.get("score") or {}).get("score"),
           "sharpe": (v.get("score") or {}).get("sharpe"), "run_id": v.get("run_id"), "state": "proposed",
           "validation_run_id": None, "validation_status": None, "validation_error": None, "passed": None,
           "eligible_state": None, "reject_reason": None, "decided_at": None}
    for e in evs:
        if e["event"] == "promoted":
            out["version"] = int(e["version"])
            out["promoted"] = e.get("from_version") is not None
        elif e["event"] == "approved":
            out.update(state="approved", decided_at=e["at"])
        elif e["event"] == "rejected":
            out.update(state="rejected", reject_reason=e.get("reason"), decided_at=e["at"])
        elif e["event"] == "validation_started":
            out.update(validation_run_id=e.get("run_id"), state="validating")
        elif e["event"] == "validation_finished":
            out.update(validation_status=e.get("status"), passed=e.get("passed"))
            if e.get("status") == "start_failed":
                out.update(state="validation_start_failed", validation_error=e.get("error"))
    if out["validation_run_id"]:
        card = runcard.get(out["validation_run_id"])
        if card is not None:
            out["validation_status"] = card["status"]
            if card["status"] == "done":
                out["passed"] = report.effective_passed(card)
                # "passed_gates" = G1–G8 passed (NOT the lifecycle state "validated", which needs the holdout the
                # user opens); the strategy may then move to "candidate" by the user's own transition
                out["state"] = "passed_gates" if out["passed"] else "failed_gates"
                out["eligible_state"] = "candidate" if out["passed"] else None
            elif card["status"] in ("failed", "cancelled"):
                out["state"] = "validation_" + card["status"]
    return out


def _record_validation(loop_id: str, vid: str, run_id: str) -> None:
    from ..backtest import runcard
    from ..validation import report
    card = runcard.get(run_id)
    if card is None or card["status"] in ("queued", "running"):
        return
    _event(loop_id, "validation_finished", variant_id=vid,
           data={"run_id": run_id, "status": card["status"],
                 "passed": report.effective_passed(card) if card["status"] == "done" else None})


def approve(loop_id: str, vid: str) -> dict:
    st = State(loop_id)
    fins = (st.finalists or {}).get("variants") or []
    if vid not in fins:
        raise LoopError(404, "not_found", "Finalist not found.")
    if st.config.get("autonomous"):
        raise LoopError(409, "autonomous", "This loop ran fully autonomously — it validated its finalists itself.")
    f = finalist_state(st, vid)
    if f["state"] != "proposed":
        raise LoopError(409, "decided", f"This finalist is already {f['state']}.")
    _event(loop_id, "approved", variant_id=vid, data={"actor": "user"},
           audit=("user", "loop_finalist_approved", {"loop_id": loop_id, "variant_id": vid,
                                                      "strategy_id": f["strategy_id"], "version": f["version"]}))
    dispatch_validations()
    return get_loop(loop_id)


def reject(loop_id: str, vid: str, reason: Optional[str]) -> dict:
    st = State(loop_id)
    if vid not in ((st.finalists or {}).get("variants") or []):
        raise LoopError(404, "not_found", "Finalist not found.")
    if st.config.get("autonomous"):
        raise LoopError(409, "autonomous", "This loop ran fully autonomously — it validated its finalists itself.")
    f = finalist_state(st, vid)
    if f["state"] != "proposed":
        raise LoopError(409, "decided", f"This finalist is already {f['state']}.")
    _event(loop_id, "rejected", variant_id=vid, data={"reason": reason, "actor": "user"},
           audit=("user", "loop_finalist_rejected", {"loop_id": loop_id, "variant_id": vid, "reason": reason}))
    return get_loop(loop_id)


def dispatch_validations() -> None:
    """Approved finalists (approval mode) get their validation run when the slot is free (FIFO by approval).
    Also records finished validations. Never the holdout."""
    from ..backtest import jobs
    from ..validation import jobs as vjobs
    if not db.is_open() or not _validation_dispatch_lock.acquire(blocking=False):
        return
    try:
        for r in db.s5b_select("loop_runs", order="created_at", desc=True, limit=50):
            st = State(r["id"])
            for vid in (st.finalists or {}).get("variants") or []:
                f = finalist_state(st, vid)
                if f["validation_run_id"] and f["validation_status"] in ("done", "failed", "cancelled") \
                        and not any(e["event"] == "validation_finished" for e in st.finalist_events.get(vid) or []):
                    _record_validation(st.id, vid, f["validation_run_id"])
                if f["state"] != "approved" or f["validation_run_id"]:
                    continue
                if jobs.manager().active_run_id() is not None or _others_waiting() or is_active(st.id):
                    return
                hist = "2000" if st.config.get("history_from") == "2000" else None
                try:
                    rid = vjobs.service().submit_validation(f["strategy_id"], f["version"], hist,
                                                            auto_resume=bool(st.config.get("auto_resume")))
                except jobs.JobBusy:
                    return
                except Exception as ex:     # noqa: BLE001 — recorded, visible
                    msg = ex.user_message if isinstance(ex, UserFacingError) else str(ex)[:200]
                    _event(st.id, "validation_finished", variant_id=vid, data={"run_id": None, "status": "start_failed",
                                                                               "error": msg})
                    continue
                _event(st.id, "validation_started", variant_id=vid, data={"run_id": rid, "actor": "user"},
                       audit=("user", "loop_validation_started", {"loop_id": st.id, "variant_id": vid, "run_id": rid}))
                return
    finally:
        _validation_dispatch_lock.release()


# --- payloads -----------------------------------------------------------------------------------------------

EVENTS_MAX = 500
SHOWN_EVENT_DATA = {"seed_ready", "generation_planned", "variant_saved", "variant_failed", "variant_duplicate",
                    "backtest_started", "backtest_finished", "variant_scored", "selection", "run_cap_reached",
                    "finalists", "promoted", "approved", "rejected", "validation_started", "validation_finished",
                    "note", "topic_checked", "ai_message", "ai_started", "cancel_requested"} | set(STATUS_EVENTS)


def _ai_out(m: dict, topic: Optional[str], final: bool = False) -> dict:
    """``final``: the variant (or the loop) ended — a message still waiting for its topic check never gets one
    and is not shown (review L6)."""
    out = dict(m.get("output") or {})
    stored_topic = out.pop("topic_check", None)
    topic = topic or stored_topic
    discarded = bool(out.pop("discarded", False))
    reason = out.pop("discarded_reason", None)
    if topic == "pending" and final:
        topic = "skipped"
        if not discarded:
            discarded, reason = True, "not_topic_checked"
    if topic == "failed":
        discarded, reason = True, "topic_check_failed"
    shown = None
    if not discarded and topic in ("passed", "skipped"):
        shown = out
    elif discarded and reason == "draft_invalid" and out.get("validation") is not None:
        shown = {"validation": out.get("validation")}      # the validator card only — no AI spec texts (M3)
    return {"id": m["id"], "role": m["role"], "output": shown, "pending": topic == "pending" and not discarded,
            "discarded": discarded, "discarded_reason": reason, "topic_check": topic,
            "check": m.get("check") if shown is not None else None, "provenance": m.get("provenance"),
            "usage": m.get("usage"), "cost_usd": m.get("cost_usd"), "created_at": m["created_at"]}


def _event_out(e: dict) -> dict:
    d = dict(e.get("data") or {})
    d.pop("reason_audit", None)                      # the topic check's own reason is for the audit only
    if e["event"] == "backtest_finished" and isinstance(d.get("score"), dict):
        d["score"] = {k: d["score"].get(k) for k in ("score", "sharpe", "checks_passed", "failed_checks", "reason")}
    if e["event"] == "selection":
        d.pop("ranking", None)
    if e["event"] == "variant_scored" and isinstance(d.get("score"), dict):
        d["score"] = {k: d["score"].get(k) for k in ("score", "sharpe", "checks_passed", "reason")}
    return {"id": e["id"], "event": e["event"], "generation": e.get("generation"), "variant_id": e.get("variant_id"),
            "data": d, "created_at": e["created_at"]}


def _variant_out(st: State, v: dict, msgs: Dict[int, dict], run_status: Dict[str, Optional[str]]) -> dict:
    ai = [msgs[i] for i in v["message_ids"] if i in msgs]
    topic = v.get("topic_check")
    final = v["state"] not in ("planned", "ai") or st.status in TERMINAL
    diag = next((m for m in ai if m["role"] == "diagnosis"), None)
    ref = next((m for m in reversed(ai) if m["role"] == "refinement"), None)
    src = st.variants.get(v.get("carried_from")) if v.get("carried_from") else None
    versions = (src or v)["versions"]
    sc = v.get("score")
    name = None
    if v.get("strategy_id"):
        row = db.strategy_get(v["strategy_id"])
        name = (row or {}).get("name")
    return {"id": v["id"], "generation": v["generation"], "slot": v["slot"], "origin": v["origin"],
            "parent_variant_id": v["parent_variant_id"], "carried_from": v.get("carried_from"),
            "strategy_id": v.get("strategy_id"), "strategy_name": name,
            "versions": [{"version": x["version"], "kind": x["kind"], "parameter": x["parameter"], "value": x["value"],
                          "run_id": x["run_id"], "run_status": run_status.get(x["run_id"]) or x["run_status"],
                          "reused": x["reused"], "score": (x.get("score") or {}).get("score"), "error": x.get("error")}
                         for x in sorted(versions, key=lambda x: x["version"])],
            "chosen_version": v.get("chosen_version"), "run_id": v.get("run_id"), "state": v["state"],
            "reason": v.get("reason"), "score": sc,
            "key_metrics": None if not sc else {"sharpe": sc.get("sharpe"),
                                                "total_return": (sc.get("inputs") or {}).get("total_return"),
                                                "cagr": (sc.get("inputs") or {}).get("cagr"),
                                                "max_drawdown": (sc.get("inputs") or {}).get("max_drawdown")},
            "topic_check": topic, "diagnosis": _ai_out(diag, topic, final) if diag else None,
            "refinement": _ai_out(ref, topic, final) if ref else None}


def _planned(st: State) -> int:
    c = st.config
    return planned_backtests(c["variants"], c["generations"], c["search_points"])


def get_loop(loop_id: str, events_after: Optional[int] = None) -> dict:
    from ..backtest import jobs, runcard
    st = State(loop_id)
    active = is_active(loop_id)
    status = st.status
    with _lock:
        prog = dict(_progress[loop_id]) if loop_id in _progress else None
    if active and prog and prog.get("phase") in ("waiting_slot", "waiting_connection"):
        status = prog["phase"]
    msgs = {m["id"]: m for m in db.s5b_select("loop_ai_messages", {"loop_id": loop_id})}
    run_ids = {x["run_id"] for v in st.variants.values() for x in v["versions"] if x.get("run_id")}
    run_status = {}
    for rid in run_ids:
        r = db.backtest_run_get(rid)
        run_status[rid] = (r or {}).get("status")
    gens = []
    for g in sorted(st.generations):
        sel = st.generations[g].get("selection")
        gens.append({"generation": g,
                     "variants": [_variant_out(st, v, msgs, run_status) for v in st.gen_variants(g)],
                     "selection": None if sel is None else {k: sel.get(k) for k in ("elites", "refine", "culled",
                                                                                   "duplicates", "k", "c", "reasons",
                                                                                   "ranking")}})
    fam = (st.seed or {}).get("family_id")
    if fam is None and st.seed_in.get("strategy_id"):
        fam = (db.strategy_get(st.seed_in["strategy_id"]) or {}).get("family_id")
    remaining = max(0, _planned(st) - st.backtests_started) if status not in TERMINAL else 0
    vals = 0
    if status not in TERMINAL and st.config.get("autonomous"):
        started = sum(1 for evs_ in st.finalist_events.values() for e in evs_ if e["event"] == "validation_started")
        vals = max(0, selection.k_elites(st.config["variants"]) - started) if st.finalists is None else \
            max(0, len((st.finalists or {}).get("variants") or []) - started)
    try:
        trials = scoring.projected_g4(fam, remaining, vals)
    except Exception as ex:     # noqa: BLE001 — shown with the reason, never a made-up number
        trials = {"unavailable": str(getattr(ex, "user_message", ex))[:200]}
    evs = [e for e in st.events if events_after is None or e["id"] > events_after][-EVENTS_MAX:]
    fins = []
    for vid in (st.finalists or {}).get("variants") or []:
        fins.append(finalist_state(st, vid))
    job_progress = None
    if prog and prog.get("run_id"):
        job_progress = jobs.manager().progress(prog["run_id"])
    seed_out = {k: v for k, v in st.seed_in.items()}
    try:
        lim = budget_mod.limits()
    except budget_mod.SettingsInvalid as ex:
        lim = {"unavailable": str(ex)}
    ai_cost = round(sum(float(m.get("cost_usd") or 0.0) for m in msgs.values()), 6)
    return {"id": st.id, "created_at": st.row["created_at"], "status": status, "persisted_status": st.status,
            "status_reason": st.status_reason, "active": active, "seed": seed_out,
            "root_strategy_id": (st.seed or {}).get("strategy_id"), "family_id": fam,
            "config": {k: st.config.get(k) for k in ("variants", "generations", "search_points", "model",
                                                     "history_from", "auto_resume", "autonomous")},
            "progress": prog, "job_progress": job_progress,
            "counts": {"backtests_started": st.backtests_started, "backtests_planned": _planned(st),
                       "variants": len(st.variants), "failed": sum(1 for v in st.variants.values()
                                                                   if v["state"] == "failed"),
                       "duplicates": sum(1 for v in st.variants.values() if v["state"] == "duplicate"),
                       "ai_messages": len(msgs)},
            "trials": trials,
            "budget": {"spent_run_usd": budget_mod.run_spent(loop_id), "ai_messages_cost_usd": ai_cost,
                       "limits": lim, "paused_reason": budget_mod.paused_reason()},
            "run_cap_reached": st.run_cap_reached, "generations": gens,
            "finalists": fins, "finalists_note": (st.finalists or {}).get("note"), "done_note": st.done_note,
            "notes": st.notes, "events": [_event_out(e) for e in evs],
            "last_event_id": st.events[-1]["id"] if st.events else None,
            "can": {"cancel": status not in TERMINAL, "pause": active and st.status == "running",
                    "resume": (not active) and st.status in ("paused", "paused_budget", "interrupted")},
            "ai_interaction_notice": AI_INTERACTION_NOTICE, "holdout_note": HOLDOUT_NOTE}


def list_loops(limit: int = 50) -> dict:
    out = []
    for r in db.s5b_select("loop_runs", order="created_at", desc=True, limit=limit):
        st = State(r["id"])
        best = None
        for v in st.variants.values():
            s = (v.get("score") or {}).get("score")
            if s is not None and (best is None or s > best):
                best = s
        status = st.status
        if is_active(r["id"]):
            with _lock:
                ph = (_progress.get(r["id"]) or {}).get("phase")
            if ph in ("waiting_slot", "waiting_connection"):
                status = ph
        out.append({"id": r["id"], "created_at": r["created_at"], "status": status,
                    "status_reason": st.status_reason, "seed": {k: v for k, v in st.seed_in.items() if k != "text"},
                    "seed_text": (st.seed_in.get("text") or None),
                    "root_strategy_id": (st.seed or {}).get("strategy_id"), "family_id": (st.seed or {}).get("family_id"),
                    "config": {k: st.config.get(k) for k in ("variants", "generations", "search_points", "model",
                                                             "autonomous")},
                    "generations_done": len([g for g in st.generations.values() if g.get("selection")]),
                    "best_score": best, "finalists": len((st.finalists or {}).get("variants") or []),
                    "backtests_started": st.backtests_started})
    return {"loops": out, "ai_interaction_notice": AI_INTERACTION_NOTICE}


def reusable_seed_card(sid: str, version: int, history_from: Optional[str] = None) -> Optional[dict]:
    """The seed version's existing done ``backtest`` card that IS the loop's base run (no new trial): same
    version (= same content hash), the current engine version, the loop's history setting, no explicit range,
    benchmark / capital absent OR equal to the defaults, and ``range.end`` = today's training end. A card
    with stress scenarios qualifies (they are extra runs; the base result is the same) — click-test
    2026-10-22: the WPF sends benchmark "SPY" and capital 100000 explicitly, and a stress run was refused."""
    from ..backtest import engine, runcard
    from ..core.instruments import instrument
    from ..validation import families
    want_hist = "2000" if history_from == "2000" else None
    for row in runcard.list_runs(sid, version=int(version), status="done", limit=20, kind="backtest"):
        card = runcard.get(row["id"])
        if card is None or card.get("engine_version") != engine.ENGINE_VERSION:
            continue
        req = card.get("request") or {}
        if req.get("start") is not None or req.get("end") is not None:
            continue
        hist = req.get("history_from")
        if (None if hist in (None, "2016") else str(hist)) != want_hist:
            continue
        spec = card.get("spec") or {}
        try:
            default_bench = instrument(str(spec.get("benchmark") or engine.DEFAULT_BENCHMARK)).symbol
            if req.get("benchmark") is not None and instrument(str(req["benchmark"])).symbol != default_bench:
                continue
        except (UserFacingError, ValueError):
            continue
        if req.get("capital") is not None and float(req["capital"]) != float(engine.DEFAULT_CAPITAL):
            continue
        try:
            end = families.training_end(spec).isoformat()
        except UserFacingError:
            continue
        if (card.get("range") or {}).get("end") == end:
            return card
    return None


def estimate(strategy_id: Optional[str], variants: int, generations: int, search_points: int, tier: str,
             idea: bool, history_from: Optional[str] = None) -> dict:
    """GET /v2/loops/estimate -> planned backtests / AI calls / USD + projected trials."""
    from strategy_platform.core import llm
    spec, fam, reused = None, None, False
    if strategy_id:
        row = db.strategy_get(strategy_id)
        if row is None:
            raise LoopError(404, "not_found", "Strategy not found.")
        fam = row.get("family_id")
        spec = json.loads(db.strategy_version_get(row["id"], int(row["current_version"]))["spec_json"])
        # review 2026-10-22: a reusable seed card is no new backtest and no new trial — the estimate says so too
        seed_card = reusable_seed_card(row["id"], int(row["current_version"]), history_from)
        reused = seed_card is not None
    bt = planned_backtests(variants, generations, search_points, reused)
    calls = planned_ai_calls(variants, generations, idea)
    msgs = lp.refine_messages("mutation", lp.MUTATION_ASPECTS[0], spec or {}, None, None, None, [], [])
    per_call = llm.estimate_cost(msgs, MAX_TOKENS, tier)
    k = selection.k_elites(variants)
    auto = budget_mod.autonomous()
    return {"backtests_planned": bt, "seed_reused": reused, "ai_calls_planned": calls, "estimate_per_call_usd": round(per_call, 6),
            "estimate_usd": round(per_call * calls, 6), "validations_if_autonomous": k, "autonomous": auto,
            "trials": scoring.projected_g4(fam, bt, k if auto else 0), "budget": budget_mod.descriptor(),
            "note": "Estimate before the start: repair rounds and topic checks may cost a little more; the "
                    "per-run cap stops the loop's AI when reached."}


# --- settings (GET/PUT /v2/settings/loop) -----------------------------------------------------------------------

def get_settings() -> dict:
    try:
        return {"autonomous": budget_mod.autonomous(), "budget": budget_mod.descriptor(),
                "defaults": {"variants": VARIANTS_DEFAULT, "generations": GENERATIONS_DEFAULT,
                             "search_points": SEARCH_DEFAULT},
                "limits": {"variants": [VARIANTS_MIN, VARIANTS_MAX], "generations": [GENERATIONS_MIN, GENERATIONS_MAX],
                           "search_points": [SEARCH_MIN, SEARCH_MAX], "max_backtests": MAX_BACKTESTS},
                "autonomous_note": ("When on, the loop validates its finalists (gates G1–G8) itself; the holdout is "
                                    "always opened only by you."), "holdout_note": HOLDOUT_NOTE}
    except budget_mod.SettingsInvalid as ex:
        raise LoopError(503, "settings_invalid", str(ex)) from None


def check_settings(body) -> dict:
    if not isinstance(body, dict):
        return {"_": "expected a JSON object {autonomous?, monthly_usd?, daily_usd?, per_run_usd?}"}
    lim = {k: v for k, v in body.items() if k != "autonomous"}
    errors = budget_mod.check_limits(lim, partial=True) if lim else {}
    if "autonomous" in body and not isinstance(body["autonomous"], bool):
        errors["autonomous"] = "true/false"
    if not body:
        errors["_"] = "at least one of autonomous, monthly_usd, daily_usd, per_run_usd"
    return errors


def put_settings(body: dict, actor: str = "user") -> dict:
    if "autonomous" in body:
        try:
            old = budget_mod.autonomous()
        except budget_mod.SettingsInvalid:
            old = None
        db.meta_set(db.LOOP_AUTONOMOUS_KEY, "1" if body["autonomous"] else "0")
        db.add_audit(actor, "loop_settings_changed", {"old": {"autonomous": old},
                                                     "new": {"autonomous": bool(body["autonomous"])}})
    lim = {k: v for k, v in body.items() if k != "autonomous"}
    if lim:
        budget_mod.apply(lim, actor)
    return get_settings()


def loop_of_run(run_id: Optional[str]) -> Optional[str]:
    """The loop whose orchestrator submitted ``run_id`` while it is active (409 bodies, §16.8)."""
    if not run_id:
        return None
    with _lock:
        lid = _loop_runs.get(run_id)
        f = _active.get(lid) if lid else None
        return lid if f is not None and f.thread is not None and f.thread.is_alive() else None


def _conflict_text(ex) -> str:
    for attr in ("message", "user_message"):
        v = getattr(ex, attr, None)
        if isinstance(v, str) and v:
            return v[:300]
    from ..lifecycle import registry as lc
    if isinstance(ex, lc.SpecInvalid):
        return "The spec did not pass the validator when it was saved."
    if isinstance(ex, lc.NotFound):
        return "The strategy does not exist any more."
    return str(ex)[:300] or type(ex).__name__
