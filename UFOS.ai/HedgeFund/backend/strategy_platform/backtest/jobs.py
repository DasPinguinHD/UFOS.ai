"""Backtest jobs (SPEC §8.5, notes §8.11): ONE backtest at a time in a
worker thread.

Life of a job: ``submit`` creates the run card (``queued``) and starts the
worker -> ``runcard.mark_running`` -> ``engine.run`` (progress phases
``data`` / ``simulate``; the seam check is reported inside ``data``) ->
``metrics.compute`` (phase ``metrics``) -> ``stress.run_stress`` when the
request asks for it (phase ``stress``) -> ``runcard.finish`` (phase
``saving``). Failures end the card with ``runcard.fail``: a
``UserFacingError`` keeps its ``user_message``; any other exception is
logged with its traceback and the card gets ``GENERIC_FAILURE`` (no stack
trace leaves the backend). Cancellation: ``cancel`` sets a flag the engine
and the stress runner check per simulated day / scenario
(``BacktestCancelled``) -> ``runcard.cancel``.

Progress lives in memory only: ``{phase, percent, message, updated_at}``
(``percent`` = 0..100 WITHIN the phase). After a backend restart a card
left ``queued``/``running`` cannot continue: ``recover_interrupted`` (called
at startup) marks it failed with ``INTERRUPTED_MESSAGE``.

Stage 4 (SPEC §9.9): ``submit_work(create_card, work, …)`` is the public entry
for other job kinds (validation / holdout runs) on the same single slot;
``submit`` is a thin caller of it.

Resumable runs (SPEC §10.3, R2 notes): a failed card gets ``error_kind``
(``error_kind_of``: "connection" when the root cause is a connection-type
error of the data layer, "basis", else "error"; "interrupted" after a backend
restart). Auto-resume (card flag ``auto_resume``, never for holdout runs): on
a connection failure the job stays in the slot in the state ``waiting``
(progress phase ``waiting``, message "Connection lost — waiting for Alpaca
(attempt n/5)…", the card notes error_kind "connection"), probes the source
(``data.network.probe``) at most every ``PROBE_INTERVAL_SECONDS`` (30 s; no
time limit), and when the probe succeeds re-runs ``work`` on the SAME card
(resume_count + 1; the work reuses its checkpoints). After
``MAX_AUTO_RESUMES`` (5) automatic resumes the next connection failure fails
the card normally (still resumable by hand). Cancel works while waiting.
``resume(run_id)`` = the manual resume: a NEW card with ``resumed_from``.
Crash recovery: a card left queued/running fails with "interrupted", or with
"connection" when it was waiting for the connection (then resumable).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, List, Optional

from ..core.errors import UserFacingError
from . import runcard

logger = logging.getLogger(__name__)

BUSY_MESSAGE = "A backtest is already running."
GENERIC_FAILURE = "Backtest failed — see backend.log"
INTERRUPTED_MESSAGE = "Backend stopped during the run"
CANCELLED_MESSAGE = "The backtest was cancelled."
PHASES = ("queued", "data", "simulate", "metrics", "stress", "saving", "waiting")
ACTIVE_STATUSES = ("queued", "running")
MAX_AUTO_RESUMES = 5                    # SPEC §10.3 (user-accepted bound)
PROBE_INTERVAL_SECONDS = 30.0           # SPEC §10.3: probe at most every 30 s (data.network enforces it too)
WAITING_INTERRUPTED_MESSAGE = "Backend stopped while the run was waiting for the connection"
SOURCE_LABELS = {"alpaca": "Alpaca", "tiingo": "Tiingo", "fred": "FRED", "sec_edgar": "SEC EDGAR"}


class JobBusy(Exception):
    """Another backtest is queued or running (HTTP 409)."""

    def __init__(self, run_id: str):
        super().__init__(BUSY_MESSAGE)
        self.run_id = run_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def error_kind_of(ex: BaseException) -> str:
    """run card error_kind of a failure (SPEC §10.3)."""
    if isinstance(ex, runcard.BasisChanged):
        return "basis"
    from ..validation.parallel import is_connection_error
    return "connection" if is_connection_error(ex) else "error"


def _source_key(src: str) -> str:
    s = src.lower()
    if "alpaca" in s:
        return "alpaca"
    if "tiingo" in s:
        return "tiingo"
    if "fred" in s:
        return "fred"
    if "edgar" in s or s.startswith("sec"):
        return "sec_edgar"
    return s


def source_of(ex: BaseException) -> str:
    """The failing data source of a connection error (``source`` attribute of the error or of the error it
    was raised from; review 2026-10-10: FRED/EDGAR are no longer reported as Alpaca): alpaca | tiingo |
    fred | sec_edgar | the source's own name; 'unknown' when no error names one."""
    seen = set()
    e = ex
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        e = getattr(e, "error", None) or e
        src = str(getattr(e, "source", "") or "")
        if src:
            return _source_key(src)
        e = e.__cause__
    return "unknown"


class _Job:
    def __init__(self, run_id: str, spec: dict, request: dict, *, kind: str = "backtest",
                 auto_resume: bool = False):
        self.run_id = run_id
        self.spec = spec
        self.request = request
        self.kind = kind
        self.auto_resume = bool(auto_resume) and kind != "holdout"
        self.auto_resumes = 0
        self.waiting = False
        self.cancel_flag = threading.Event()
        self.progress = {"phase": "queued", "percent": 0.0, "message": "queued", "updated_at": _now()}
        self.thread: Optional[threading.Thread] = None


class JobManager:
    """Runs at most one backtest. The callables are injectable for tests;
    defaults: ``engine.run``, ``metrics.compute``, ``stress.run_stress`` and
    ``engine.DataLoader()`` (``data_factory``)."""

    def __init__(self, *, run_engine: Optional[Callable] = None, compute_metrics: Optional[Callable] = None,
                 run_stress: Optional[Callable] = None, data_factory: Optional[Callable] = None,
                 probe: Optional[Callable[[str], bool]] = None, probe_interval: float = PROBE_INTERVAL_SECONDS,
                 max_auto_resumes: int = MAX_AUTO_RESUMES):
        self._run_engine = run_engine
        self._compute_metrics = compute_metrics
        self._run_stress = run_stress
        self._data_factory = data_factory
        self._probe = probe                     # tests; default data.network.probe
        self._probe_interval = float(probe_interval)
        self.max_auto_resumes = int(max_auto_resumes)
        self._lock = threading.Lock()
        self._job: Optional[_Job] = None

    # --- public -------------------------------------------------------------------------------

    def active_run_id(self) -> Optional[str]:
        with self._lock:
            return self._job.run_id if self._job else None

    def submit(self, strategy_id: str, version: Optional[int], request: dict, *, auto_resume: bool = False) -> str:
        """Creates the queued run card and starts the worker -> run id.
        ``JobBusy`` while another job is active; runcard errors (NotFound, ValueError,
        StorageUnavailable) propagate."""
        return self.submit_work(lambda: runcard.create(strategy_id, version, request, auto_resume=auto_resume),
                                self._run)

    def resume_backtest(self, run_id: str, *, auto_resume: Optional[bool] = None) -> str:
        """Manual resume of a failed backtest card (SPEC §10.3) -> the NEW run id. ``ResumeRefused``
        (checked again under the runner lock), ``JobBusy``."""
        return self.submit_work(lambda: runcard.create_resumed(run_id, auto_resume=auto_resume), self._run)

    def is_waiting(self, run_id: str) -> bool:
        """True while the active job ``run_id`` waits for the connection (status 'waiting' in the API)."""
        with self._lock:
            return bool(self._job is not None and self._job.run_id == run_id and self._job.waiting)

    def submit_work(self, create_card: Callable[[], dict], work: Callable, *, label: str = "backtest",
                    generic_msg: str = GENERIC_FAILURE, cancelled_msg: str = CANCELLED_MESSAGE) -> str:
        """Public entry for every job kind (stage 4: validation/holdout jobs, SPEC §9.9).
        Under the manager lock: ``JobBusy`` while any job is active, else ``create_card()`` (must
        return a queued run card dict with ``id``, ``spec``, ``request``; its errors propagate and
        nothing starts) -> the worker thread runs: cancelled while queued -> ``runcard.cancel``;
        ``runcard.mark_running`` -> ``work(job, progress)`` (``job``: ``run_id``, ``spec``,
        ``request``, ``cancel_flag`` (threading.Event); ``progress(phase, percent, message)``) ->
        the work finishes the card itself. Outcomes: ``BacktestCancelled`` -> ``runcard.cancel
        (cancelled_msg)``; ``UserFacingError`` -> ``runcard.fail(user_message)``; anything else ->
        traceback in backend.log + ``runcard.fail(generic_msg)``. ``create_card`` runs under the
        lock, so it may re-check preconditions race-free. -> run id"""
        with self._lock:
            if self._job is not None:
                raise JobBusy(self._job.run_id)
            card = create_card()
            job = _Job(card["id"], card["spec"], card["request"], kind=card.get("kind", "backtest"),
                       auto_resume=bool(card.get("auto_resume")))
            self._job = job
            job.thread = threading.Thread(target=self._work, args=(job, work, generic_msg, cancelled_msg, label),
                                          name=f"{label}-{job.run_id}", daemon=True)
            try:
                job.thread.start()
            except RuntimeError:
                self._job = None
                logger.exception("%s worker for %s could not start", label, job.run_id)
                self._end(job, "fail", generic_msg)
                raise
        return job.run_id

    def progress(self, run_id: str) -> Optional[dict]:
        """In-memory progress of the active job ``run_id`` (copy) or None."""
        with self._lock:
            job = self._job
            if job is None or job.run_id != run_id:
                return None
            return dict(job.progress)

    def cancel(self, run_id: str) -> bool:
        """Sets the cancel flag of the active job ``run_id`` -> True; False when it is not active."""
        with self._lock:
            job = self._job
            if job is None or job.run_id != run_id:
                return False
            job.cancel_flag.set()
            job.progress = dict(job.progress, message="cancelling…", updated_at=_now())
            return True

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Joins the active worker (tests / shutdown) -> True when no job is active afterwards."""
        with self._lock:
            job = self._job
        if job is not None and job.thread is not None:
            job.thread.join(timeout)
        return self.active_run_id() is None

    # --- worker -------------------------------------------------------------------------------

    def _set_progress(self, job: _Job, phase: str, percent: float, message: str) -> None:
        with self._lock:
            job.progress = {"phase": phase, "percent": round(max(0.0, min(100.0, float(percent))), 2),
                            "message": str(message)[:300], "updated_at": _now()}

    def _end(self, job: _Job, how: str, message: str, error_kind: str = "error") -> None:
        try:
            if how == "cancel":
                runcard.cancel(job.run_id, message)
            else:
                runcard.fail(job.run_id, message, error_kind)
        except Exception:     # noqa: BLE001 — the card cannot be ended (storage down): logged, never hidden
            logger.exception("backtest %s: could not mark the run card %s", job.run_id, how)

    def _work(self, job: _Job, work: Callable, generic_msg: str, cancelled_msg: str,
              label: str = "backtest") -> None:
        try:
            from .engine import BacktestCancelled
            if job.cancel_flag.is_set():
                raise BacktestCancelled()          # cancelled while queued: queued -> cancelled
            runcard.mark_running(job.run_id)
            progress = (lambda phase, percent, message: self._set_progress(job, phase, percent, message))
            while True:
                try:
                    work(job, progress)
                    break
                except Exception as ex:   # noqa: BLE001 — auto-resume or the outcome handling below
                    if not self._should_auto_resume(job, ex):
                        raise
                    logger.warning("%s %s: connection lost (%s) — waiting to resume", label, job.run_id, ex)
                    self._wait_for_connection(job, ex)
                    job.auto_resumes += 1
                    runcard.mark_resumed(job.run_id)
                    self._set_progress(job, "data", 0.0, f"Connection is back — resuming (attempt "
                                       f"{job.auto_resumes}/{self.max_auto_resumes})…")
                    logger.info("%s %s: resumed automatically (%d/%d)", label, job.run_id, job.auto_resumes,
                                self.max_auto_resumes)
        except Exception as ex:   # noqa: BLE001 — every outcome ends the card
            from ..validation.parallel import root_connection_error
            from .engine import BacktestCancelled
            err = root_connection_error(ex)
            if isinstance(ex, BacktestCancelled):
                logger.info("%s %s cancelled", label, job.run_id)
                self._end(job, "cancel", cancelled_msg)
            elif isinstance(err, UserFacingError):
                logger.warning("%s %s failed: %s", label, job.run_id, err)
                self._end(job, "fail", err.user_message, error_kind_of(ex))
            else:
                logger.exception("%s %s failed unexpectedly", label, job.run_id)
                self._end(job, "fail", generic_msg, error_kind_of(ex))
        finally:
            with self._lock:
                if self._job is job:
                    self._job = None
            _after_job(job.run_id)

    # --- auto-resume (SPEC §10.3) --------------------------------------------------------------------

    def _should_auto_resume(self, job: _Job, ex: BaseException) -> bool:
        from ..validation.parallel import is_connection_error
        from .engine import BacktestCancelled
        if isinstance(ex, BacktestCancelled) or job.cancel_flag.is_set():
            return False
        if not (job.auto_resume and job.kind != "holdout" and is_connection_error(ex)):
            return False
        return job.auto_resumes < self.max_auto_resumes

    def _probe_source(self, source: str) -> bool:
        """data.network.probe for its sources (alpaca; tiingo is not probed there). A source without a
        probe (FRED, SEC EDGAR, unknown): no request here — after the wait the run resumes and its next
        real request is the probe (it fails again if the source is still down; no success is faked)."""
        if self._probe is not None:
            return bool(self._probe(source))
        from ..data import network
        if source not in network.SOURCES:
            return True
        return bool(network.probe(source))

    def _next_probe_in(self, source: str) -> float:
        if self._probe is not None:
            return 0.0
        from ..data import network
        if source not in network.SOURCES:
            return 0.0
        return float(network.next_probe_in(source))

    @staticmethod
    def _basis_still_matches(job: _Job) -> None:
        """While waiting (review 2026-10-10, finding 7a): the run can only resume on the basis of its first
        start — once that is gone (e.g. the holdout window rolled over) it stops waiting -> BasisChanged."""
        card = runcard.get(job.run_id)
        why = runcard.basis_problem(runcard.CheckpointStore(job.run_id).get("basis"), runcard.current_basis(card))
        if why:
            raise runcard.BasisChanged(why)

    def _wait_for_connection(self, job: _Job, ex: BaseException) -> None:
        """State ``waiting`` until the probe of the failed source succeeds (no time limit, probe at most every
        ``probe_interval`` seconds) -> returns; cancel -> ``BacktestCancelled``. A probe error that is not a
        connection error (keys rejected, missing key …) propagates — waiting would not fix it."""
        from .engine import BacktestCancelled
        source = source_of(ex)
        label = SOURCE_LABELS.get(source) or (source if source != "unknown" else "the data source")
        msg = (f"Connection lost — waiting for {label} (attempt "
               f"{job.auto_resumes + 1}/{self.max_auto_resumes})…")
        runcard.mark_waiting(job.run_id)
        with self._lock:
            job.waiting = True
        try:
            self._set_progress(job, "waiting", 0.0, msg)
            while True:
                delay = max(self._probe_interval, self._next_probe_in(source))
                if job.cancel_flag.wait(delay):
                    raise BacktestCancelled()
                self._basis_still_matches(job)
                if self._probe_source(source):
                    return
                self._set_progress(job, "waiting", 0.0, msg)
        finally:
            with self._lock:
                job.waiting = False

    def _run(self, job: _Job, progress: Callable[[str, float, str], None]) -> None:
        """The backtest work (``submit_work`` already marked the card running). Re-entrant for the
        auto-resume: basis check + data checkpoints (SPEC §10.3)."""
        from ..validation import parallel
        from . import engine
        from .engine import BacktestCancelled

        def check() -> None:
            if job.cancel_flag.is_set():
                raise BacktestCancelled()

        req = job.request
        card = runcard.get(job.run_id)
        store = runcard.CheckpointStore(job.run_id)
        runcard.ensure_basis(card, store)
        raw = self._data_factory() if self._data_factory else engine.DataLoader()
        data = parallel.RecordingLoader(raw, checkpoints=store)
        run_engine = self._run_engine or engine.run
        self._set_progress(job, "data", 0.0, "starting")
        result = run_engine(job.spec, start=req.get("start"), end=req.get("end"),
                            capital=req.get("capital") if req.get("capital") is not None else engine.DEFAULT_CAPITAL,
                            benchmark=req.get("benchmark"), history_from=req.get("history_from"),
                            progress=progress, cancel=job.cancel_flag.is_set, data=data)
        check()
        self._set_progress(job, "metrics", 0.0, "computing metrics")
        if self._compute_metrics is not None:
            metrics = self._compute_metrics(result)
        else:
            from . import metrics as metrics_mod
            metrics = metrics_mod.compute(result, analysis=getattr(result, "analysis", None))
        self._set_progress(job, "metrics", 90.0, "computing the attribution")
        from . import attribution as attribution_mod
        attribution = attribution_mod.compute(result, metrics)          # SPEC §11.1 (X1, 2026-10-14)
        self._set_progress(job, "metrics", 100.0, "metrics computed")
        stress_out = None
        if req.get("stress"):
            check()
            self._set_progress(job, "stress", 0.0, "starting stress tests")
            if self._run_stress is not None:
                run_stress = self._run_stress
            else:
                from .stress import run_stress
            kwargs = {"data": data, "progress": progress, "cancel": job.cancel_flag.is_set}
            if card is not None and card.get("seed") is not None:
                kwargs["seed"] = card["seed"]
            # a lost connection stops the run (wait/resume) instead of becoming a "failed" scenario
            kwargs["run_engine"] = parallel._connection_guarded(run_engine)
            stress_out = run_stress(job.spec, result, **kwargs)
        check()
        add_warnings(result, store.warnings)
        self._set_progress(job, "saving", 0.0, "saving the run card")
        runcard.finish(job.run_id, result, metrics, stress=stress_out, attribution=attribution)
        logger.info("backtest %s finished", job.run_id)


def _after_job(run_id: str) -> None:
    """After every job (SPEC §13): memory facts of a finished backtest/validation card (never holdout —
    ``research.memory`` checks the card), then the Studio's draft queue gets the free slot. Never raises
    (a failure here must not touch the finished card or the next job)."""
    try:
        from ..research import memory
        memory.on_run_finished(run_id)
    except Exception:     # noqa: BLE001
        logger.exception("memory facts for run %s could not be written", run_id)
    try:
        from ..research import committee
        committee.on_validation_finished(run_id)        # SPEC §14.3: auto committee (setting, default off)
    except Exception:     # noqa: BLE001
        logger.exception("auto committee after run %s failed", run_id)
    try:
        from ..research import studio
        studio.on_slot_free()
    except Exception:     # noqa: BLE001
        logger.exception("Studio queue dispatch after run %s failed", run_id)
    try:
        from ..research import paper_strategy
        paper_strategy.on_slot_free()                    # SPEC §14.2: waiting paper replications
    except Exception:     # noqa: BLE001
        logger.exception("paper replication dispatch after run %s failed", run_id)
    try:
        from ..discovery import loop as discovery_loop
        discovery_loop.on_slot_free()                    # SPEC §16.8: the loop gets the slot after the queues
    except Exception:     # noqa: BLE001
        logger.exception("discovery loop wake-up after run %s failed", run_id)


def add_warnings(result, warnings: List[dict]) -> None:
    """Appends run-card warnings (e.g. ``checkpoint_mismatch``) to an engine result."""
    if not warnings:
        return
    if isinstance(result, dict):
        result.setdefault("warnings", [])
        if result["warnings"] is None:
            result["warnings"] = []
        result["warnings"].extend(warnings)
    else:
        if getattr(result, "warnings", None) is None:
            result.warnings = []
        result.warnings.extend(warnings)


def resume(run_id: str, *, auto_resume: Optional[bool] = None) -> str:
    """Manual resume (POST /v2/backtests/{id}/resume, /v2/validations/{id}/resume) -> the NEW run id.
    ``runcard.NotFound`` (unknown run), ``runcard.ResumeRefused`` (409), ``JobBusy``."""
    card = runcard.get(run_id)
    if card is None:
        raise runcard.NotFound(f"Run {run_id} does not exist.")
    why = runcard.resume_refusal(card)
    if why:
        raise runcard.ResumeRefused(why)
    if card.get("kind") == "validation":
        from ..validation import jobs as vjobs
        return vjobs.service().resume_validation(run_id, auto_resume=auto_resume)
    return manager().resume_backtest(run_id, auto_resume=auto_resume)


def recover_interrupted() -> List[str]:
    """Marks every card left ``queued``/``running`` by an earlier process as failed -> their ids. Error kind
    "interrupted" (``INTERRUPTED_MESSAGE``), or "connection" for a card that was waiting for the
    connection (``WAITING_INTERRUPTED_MESSAGE``; resumable, SPEC §10.3). Call at backend start, before any
    job runs."""
    ids: List[str] = []
    for status in ACTIVE_STATUSES:
        for row in runcard.list_runs(status=status, limit=1000):
            waiting = row.get("error_kind") == "connection"
            try:
                runcard.fail(row["id"], WAITING_INTERRUPTED_MESSAGE if waiting else INTERRUPTED_MESSAGE,
                             "connection" if waiting else "interrupted")
                ids.append(row["id"])
            except runcard.StatusConflict:
                pass          # ended meanwhile
    if ids:
        logger.warning("Marked %d interrupted backtest(s) as failed: %s", len(ids), ", ".join(ids))
    return ids


_MANAGER: Optional[JobManager] = None
_MANAGER_LOCK = threading.Lock()


def manager() -> JobManager:
    """The process-wide job manager."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = JobManager()
        return _MANAGER


def set_manager(m: Optional[JobManager]) -> None:
    """Replaces the process-wide manager (tests)."""
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = m
