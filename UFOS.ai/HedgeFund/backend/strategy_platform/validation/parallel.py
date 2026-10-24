"""Parallel compute for validation runs (SPEC §10.2, R2 notes §10.6).

Grid backtests and stress scenarios of a validation run run in a process
pool; everything else (the training backtest, folds, gates, the report,
every database write and every network request) stays in the parent.

Pool size: ``plan_workers`` -> ``max(1, os.cpu_count() - 1)`` (user decision
2026-10-09: all cores but one), reduced by the memory guard so that
``workers x per_worker_bytes <= MEMORY_LIMIT_BYTES`` (2 GB). The estimate per
worker (documented constants, deliberately conservative):
``WORKER_BASE_BYTES + symbols x sessions x (ROW_BYTES x SERIES_PER_SYMBOL x
SNAPSHOT_COPIES + 8 x PANEL_ARRAYS)`` — the data snapshot the worker holds
(pickled + one unpickled copy during a run) plus the engine's panel arrays.
Never more workers than tasks. One worker = no process at all: the tasks run
in the parent thread on the parent's data loader (``TaskRunner`` "inline"),
i.e. exactly the sequential computation.

Process start (Windows-safe ``spawn``): workers are started with
``multiprocessing.get_context("spawn")``; while a worker process starts,
``sys.modules["__main__"]`` is swapped for an empty module (``_hidden_main``)
so the child does NOT re-run the parent's main script (``python main.py``
would otherwise be executed again as ``__mp_main__`` in every worker —
logging set-up, server imports). The worker entry ``_worker_main`` is a
top-level function of this module (picklable); this module imports only the
standard library at module level.

Worker isolation (security, user-accepted 2026-10-09) — ``install_worker_guard``
runs first in every worker:
- ``strategy_platform.secrets`` and ``keyring`` / ``win32cred`` are replaced by
  stub modules: importing them binds the stub, ANY use raises
  ``WorkerIsolationError`` (a real ImportError would break the feature
  registry, which imports ``secrets`` at module level without using it);
- network: ``socket.socket.connect/connect_ex/sendto``,
  ``socket.create_connection`` and ``socket.getaddrinfo`` raise — this blocks
  ``data.http_gate``, urllib, aiohttp and the Alpaca SDK alike (``http_gate``
  itself is imported by the engine's modules, so its import cannot be refused);
- storage: ``sqlite3.connect`` raises, and ``storage.db``'s ``init`` /
  ``_open`` / ``_connect`` raise (``storage.db`` is imported by
  ``validation.families`` at module level, so its import cannot be refused);
- environment variables that look like credentials (``SECRET_ENV_RE``) are
  removed from the worker's environment.
Workers receive ONLY in-memory data: the spec, the request values, the base
run (costs scenarios) and a data snapshot ``{call key: pickled (rows, info) |
frozen error}`` served by ``ReplayLoader``. Results go back to the parent,
which writes everything.

Data snapshot: the parent first runs every task against a
``CollectingLoader`` (empty data: the engine records each loader call and
stops with "no bars" — the calls do not depend on the data), then fetches
those calls through the parent's ``RecordingLoader`` (network, cache,
checkpoints) and ships them. Errors of a call that a sequential run would
see inside the engine (``UserFacingError`` other than a connection error,
e.g. a missing Tiingo key -> episode "not available") are shipped frozen and
re-raised in the worker. A connection error is NOT shipped: it stops the
validation (``is_connection_error``) so the job can wait and resume
(SPEC §10.3). External features (``{"ext": name}``) depend on the panel's
dates, which only the engine knows: the worker raises ``NeedsPrefetch`` with
the missing keys, the parent loads them (``evaluate.load_ext``, exactly the
sequential call) and re-runs those tasks in a new pool round (at most
``MAX_PREFETCH_ROUNDS``).

Determinism: results are returned in task order (index), whatever the order
in which workers finish -> the report is identical to the sequential one
(test_sp_parallel). Cancel: ``cancel()`` is polled every ``POLL_SECONDS``;
it terminates all workers and raises ``BacktestCancelled``.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import pickle
import re
import sys
import threading
import time
import traceback
import types
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

MEMORY_LIMIT_BYTES = 2 * 1024 ** 3          # SPEC §10.2: the whole pool's estimate stays <= 2 GB
WORKER_BASE_BYTES = 200 * 1024 ** 2         # interpreter + numpy + engine modules + simulation state
ROW_BYTES = 700                             # one bar / volume / action row as Python objects
SERIES_PER_SYMBOL = 4                       # all + split prices, volume, corporate actions
SNAPSHOT_COPIES = 2                         # pickled snapshot + the unpickled copy of a running task
PANEL_ARRAYS = 12                           # float64 panel fields / signal / weights per symbol-session
MAX_PREFETCH_ROUNDS = 3
POLL_SECONDS = 0.2
JOIN_SECONDS = 5.0
WATCHDOG_SECONDS = 1.0                      # a worker notices a dead parent within about this time
BLOCKED_MODULES = ("strategy_platform.secrets", "keyring", "win32cred")
SECRET_ENV_RE = re.compile(r"KEY|SECRET|TOKEN|PASSW|CREDENTIAL|AUTH|APCA|ALPACA|TIINGO|FRED|OPENROUTER|EDGAR",
                           re.IGNORECASE)


class WorkerIsolationError(RuntimeError):
    """A compute worker tried to use secrets, the network or the database (SPEC §10.2)."""


class NeedsPrefetch(Exception):
    """A worker needs data that is not in its snapshot (``keys``); not a UserFacingError on purpose,
    so neither the engine nor the stress runner turns it into a scenario status."""

    def __init__(self, keys):
        super().__init__(f"{len(keys)} data item(s) missing in the worker snapshot")
        self.keys = list(keys)


class ConnectionLost(Exception):
    """Carries a connection-kind error (``error``) through code that turns every UserFacingError into a
    scenario/config status (stress runner, grid) — a lost connection must stop the run (SPEC §10.3)."""

    def __init__(self, error: BaseException):
        super().__init__(str(error))
        self.error = error


# --- error helpers ------------------------------------------------------------------------------------

def is_connection_error(ex: Optional[BaseException]) -> bool:
    """True when ``ex``, an explicit ``ConnectionLost`` or an exception it was raised FROM (``__cause__``
    only — review 2026-10-10: an error raised while merely handling a timeout, ``__context__``, is not a
    connection failure) is connection-type per ``data.network.error_kind`` (R1, SPEC §10.1)."""
    from ..data import network
    seen = set()
    stack = [ex]
    while stack and len(seen) < 32:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, ConnectionLost):
            return True
        if network.error_kind(e) == network.CONNECTION:
            return True
        stack.append(e.__cause__)
    return False


def root_connection_error(ex: BaseException) -> BaseException:
    """The error to report for a connection failure (unwraps ``ConnectionLost``)."""
    return ex.error if isinstance(ex, ConnectionLost) else ex


def _picklable(value) -> bool:
    try:
        pickle.dumps(value)
        return True
    except Exception:  # noqa: BLE001 — anything unpicklable is left out
        return False


def freeze_exception(ex: BaseException) -> dict:
    """A picklable description of ``ex`` (class path, args, attributes) — UserFacingError subclasses have
    their own __init__ signatures, so they cannot be pickled directly."""
    attrs = {k: v for k, v in vars(ex).items() if _picklable(v)}
    args = tuple(a for a in ex.args if _picklable(a))
    return {"module": type(ex).__module__, "qualname": type(ex).__qualname__, "args": args, "attrs": attrs}


def thaw_exception(frozen: dict) -> BaseException:
    """The exception described by ``freeze_exception`` (same class, message and attributes)."""
    import importlib
    obj: Any = importlib.import_module(frozen["module"])
    for part in frozen["qualname"].split("."):
        obj = getattr(obj, part)
    ex = obj.__new__(obj)
    BaseException.__init__(ex, *frozen["args"])
    ex.__dict__.update(frozen["attrs"])
    return ex


# --- canonical JSON / hashing -------------------------------------------------------------------------

def _default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if hasattr(o, "item"):           # numpy scalar
        return o.item()
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
                      default=_default)


# Keys of loaded rows that record WHEN a row was fetched / became known, not WHAT the data is: left out of
# the data checkpoint hash (review 2026-10-10: corporate actions are re-fetched daily, so hashing them made
# every overnight resume fail with "data differs" although the data was identical).
VOLATILE_ROW_KEYS = frozenset(("fetched_at", "known_at", "checked_at", "updated_at", "created_at",
                               "realtime_start", "retrieved_at"))


def _data_only(rows):
    if isinstance(rows, list):
        return [{k: v for k, v in r.items() if k not in VOLATILE_ROW_KEYS} if isinstance(r, dict) else r
                for r in rows]
    return rows


def rows_sha256(rows) -> str:
    """sha256 of the canonical JSON of loaded rows without ``VOLATILE_ROW_KEYS`` (data checkpoint,
    SPEC §10.3)."""
    text = json.dumps(_data_only(rows), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- loaders ------------------------------------------------------------------------------------------

DATA_METHODS = ("history", "corporate_actions", "volume", "crypto_bars")


def _norm(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def call_key(method: str, *args) -> tuple:
    """Hashable key of one loader call (dates as ISO strings)."""
    return (method,) + tuple(_norm(a) for a in args)


def data_step_key(key: tuple) -> str:
    return "data:" + ":".join("" if x is None else str(x) for x in key)


def ext_names(spec) -> List[str]:
    """Names of the external features ``{"ext": name}`` used anywhere in ``spec``."""
    out = set()
    stack = [spec]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            if len(n) == 1 and isinstance(n.get("ext"), str):
                out.add(n["ext"])
            stack.extend(n.values())
        elif isinstance(n, (list, tuple)):
            stack.extend(n)
    return sorted(out)


def ext_key(name: str, panel) -> tuple:
    dates = list(panel.dates)
    return ("ext", name, tuple(panel.symbols), _norm(dates[0]) if dates else None,
            _norm(dates[-1]) if dates else None)


class CollectingLoader:
    """Records every data call and returns empty data (the engine then stops with "no bars")."""

    max_workers = 1

    def __init__(self):
        self.keys: List[tuple] = []
        self._lock = threading.Lock()

    def _rec(self, key):
        with self._lock:
            if key not in self.keys:
                self.keys.append(key)
        return [], {"warnings": [], "sources": []}

    def history(self, symbol, adjustment, start, end=None, *, progress=None):
        return self._rec(call_key("history", symbol, adjustment, start, end))

    def corporate_actions(self, symbol, start, end=None, *, progress=None):
        return self._rec(call_key("corporate_actions", symbol, start, end))

    def volume(self, symbol, start, end=None, *, progress=None):
        return self._rec(call_key("volume", symbol, start, end))

    def crypto_bars(self, symbol, start, end=None):
        return self._rec(call_key("crypto_bars", symbol, start, end))

    def ext_values(self, spec, panel):
        return None


class RecordingLoader:
    """The parent's loader for one job: delegates to the real loader (``inner``) and
    - writes a data checkpoint per call (``data:<call>``: row count + sha256 of the rows) and compares it
      with the checkpoint of an earlier attempt of the same chain: different rows -> ``BasisChanged``
      (SPEC §10.3 "the cached data for the already-loaded symbols still hashes the same");
    - with ``keep`` also memoises every result (pickled; each call returns a fresh copy) and the frozen
      user-facing errors, for the worker snapshot (``snapshot``) and to avoid loading a call twice.
    Thread-safe (the engine loads symbols in parallel, SPEC §10.1)."""

    def __init__(self, inner, *, checkpoints=None, keep: bool = False):
        self.inner = inner
        self.checkpoints = checkpoints
        self.keep = keep
        self._memo: Dict[tuple, tuple] = {}
        self._lock = threading.Lock()

    @property
    def max_workers(self):
        return getattr(self.inner, "max_workers", None)

    def _call(self, key: tuple, fn: Callable):
        if self.keep:
            with self._lock:
                hit = self._memo.get(key)
            if hit is not None:
                if hit[0] == "err":
                    raise thaw_exception(hit[1])
                return pickle.loads(hit[1])
        try:
            value = fn()
        except Exception as ex:
            if self.keep and _storable_error(ex):
                with self._lock:
                    self._memo[key] = ("err", freeze_exception(ex))
            raise
        self._check(key, value)
        if self.keep:
            blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            with self._lock:
                self._memo[key] = ("ok", blob)
        return value

    def _check(self, key: tuple, value) -> None:
        if self.checkpoints is None:
            return
        rows = value[0] if isinstance(value, tuple) and value else value
        digest = rows_sha256(rows)
        step = data_step_key(key)
        with self._lock:
            prev = self.checkpoints.get(step)
            if prev is None:
                self.checkpoints.put(step, {"call": list(key), "rows": len(rows or []), "rows_sha256": digest})
                return
        if prev.get("rows_sha256") != digest:
            from ..backtest.runcard import BasisChanged
            what = f"{key[1]} {key[0].replace('_', ' ')}" + (f" ({key[2]})" if key[0] == "history" else "")
            raise BasisChanged(f"the loaded data of {what} differs from the first attempt")

    def history(self, symbol, adjustment, start, end=None, *, progress=None):
        return self._call(call_key("history", symbol, adjustment, start, end),
                          lambda: self.inner.history(symbol, adjustment, start, end, progress=progress))

    def corporate_actions(self, symbol, start, end=None, *, progress=None):
        return self._call(call_key("corporate_actions", symbol, start, end),
                          lambda: self.inner.corporate_actions(symbol, start, end, progress=progress))

    def volume(self, symbol, start, end=None, *, progress=None):
        return self._call(call_key("volume", symbol, start, end),
                          lambda: self.inner.volume(symbol, start, end, progress=progress))

    def crypto_bars(self, symbol, start, end=None):
        return self._call(call_key("crypto_bars", symbol, start, end),
                          lambda: self.inner.crypto_bars(symbol, start, end))

    def ext_values(self, spec, panel):
        fn = getattr(self.inner, "ext_values", None)
        return fn(spec, panel) if fn is not None else None

    # --- snapshot for workers ---------------------------------------------------------------------------

    def fetch(self, key: tuple, progress=None) -> None:
        """Loads ``key`` into the memo (data call or ``("ext", …)``). Errors a sequential run would see
        inside the engine are kept (frozen); connection errors, cancellation and bugs propagate."""
        if key[0] == "ext":
            with self._lock:
                if key in self._memo:
                    return
            try:
                value = _load_ext(key)
            except Exception as ex:
                from ..strategy.evaluate import EvaluationError
                if _storable_error(ex) or isinstance(ex, EvaluationError):
                    with self._lock:
                        self._memo[key] = ("err", freeze_exception(ex))
                    return
                raise
            with self._lock:
                self._memo[key] = ("ok", pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
            return
        method, args = key[0], list(key[1:])
        if method not in DATA_METHODS:
            raise ValueError(f"unknown loader call {method!r}")
        for i, a in enumerate(args):
            if isinstance(a, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", a):
                args[i] = date.fromisoformat(a)
        kwargs = {} if method == "crypto_bars" else {"progress": progress}
        try:
            getattr(self, method)(*args, **kwargs)
        except Exception as ex:
            if not _storable_error(ex):
                raise

    def has(self, key: tuple) -> bool:
        with self._lock:
            return key in self._memo

    def snapshot(self, keys: Iterable[tuple]) -> Dict[tuple, tuple]:
        with self._lock:
            return {k: self._memo[k] for k in keys if k in self._memo}


def _storable_error(ex: BaseException) -> bool:
    """A user-facing error of a data call that a sequential run would meet inside the engine (and the
    engine/stress runner turns into a status): shipped to workers. Connection errors (-> wait/resume),
    cancellations and anything else propagate."""
    from ..core.errors import UserFacingError
    if not isinstance(ex, UserFacingError) or getattr(ex, "is_cancellation", False):
        return False
    return not is_connection_error(ex)


def _load_ext(key: tuple):
    """``evaluate.load_ext`` for the panel described by the key (it uses the panel's symbols and its first
    and last date only)."""
    from ..strategy import evaluate as ev
    _, name, symbols, first, last = key
    dates = [] if first is None else [date.fromisoformat(first), date.fromisoformat(last)]
    return ev.load_ext(name, types.SimpleNamespace(dates=dates, symbols=list(symbols)))


class ReplayLoader:
    """The worker's loader: serves the snapshot only (``NeedsPrefetch`` for a missing call)."""

    max_workers = 1

    def __init__(self, snapshot: Dict[tuple, tuple]):
        self.snapshot = snapshot

    def _get(self, key: tuple):
        hit = self.snapshot.get(key)
        if hit is None:
            raise NeedsPrefetch([key])
        if hit[0] == "err":
            raise thaw_exception(hit[1])
        return pickle.loads(hit[1])

    def history(self, symbol, adjustment, start, end=None, *, progress=None):
        return self._get(call_key("history", symbol, adjustment, start, end))

    def corporate_actions(self, symbol, start, end=None, *, progress=None):
        return self._get(call_key("corporate_actions", symbol, start, end))

    def volume(self, symbol, start, end=None, *, progress=None):
        return self._get(call_key("volume", symbol, start, end))

    def crypto_bars(self, symbol, start, end=None):
        return self._get(call_key("crypto_bars", symbol, start, end))

    def ext_values(self, spec, panel):
        names = ext_names(spec)
        if not names:
            return None
        keys = {n: ext_key(n, panel) for n in names}
        missing = [k for k in keys.values() if k not in self.snapshot]
        if missing:
            raise NeedsPrefetch(missing)
        return {n: self._get(k) for n, k in keys.items()}


# --- worker isolation guard -----------------------------------------------------------------------------

_GUARDED = False


class _BlockedModule(types.ModuleType):
    """Stub for a module workers must not use: importing works, every attribute use raises."""

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise WorkerIsolationError(f"{self.__name__}.{name}: compute workers have no access to secrets "
                                   "(SPEC §10.2)")


def _blocked(what: str):
    def fn(*args, **kwargs):
        raise WorkerIsolationError(f"{what}: compute workers have no {('network' if 'socket' in what else 'database')} "
                                   "access (SPEC §10.2)")
    fn.__name__ = f"blocked_{what.replace('.', '_')}"
    return fn


_BLOCKED_EVENT_PREFIXES = ("socket.", "ctypes.", "subprocess.", "os.exec", "os.spawn", "os.posix_spawn",
                           "os.system", "os.fork", "winreg.", "_winapi.CreateProcess", "webbrowser.",
                           "urllib.Request", "http.client.connect", "ftplib.", "smtplib.", "poplib.",
                           "imaplib.", "nntplib.", "telnetlib.")
_BLOCKED_EVENTS = frozenset(("sqlite3.connect", "sqlite3.connect/handle", "sqlite3.enable_load_extension",
                             "sqlite3.load_extension"))
_CREDENTIAL_DIR_MARKERS = (os.sep + "microsoft" + os.sep + "credentials", os.sep + "microsoft" + os.sep + "protect",
                           os.sep + "microsoft" + os.sep + "vault", os.sep + ".ssh" + os.sep,
                           "/microsoft/credentials", "/microsoft/protect", "/.ssh/")
_SECRET_FILE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3")


def _forbidden_path(path) -> bool:
    if isinstance(path, int):
        return False
    try:
        p = os.fsdecode(path)
    except (TypeError, ValueError):
        return False
    low = p.replace("\\", "/").lower()
    base = low.rsplit("/", 1)[-1]
    if base == ".env" or base.startswith(".env.") or base.endswith(_SECRET_FILE_SUFFIXES):
        return True
    if low.endswith("data/api-token") or base == "api-token":
        return True
    return any(m.replace("\\", "/") in low for m in _CREDENTIAL_DIR_MARKERS)


def _audit_hook(event: str, args) -> None:
    """Worker audit hook (review 2026-10-10, finding 5): catches what monkey-patching cannot — raw
    ``_socket`` / UDP ``sendmsg`` / asyncio connects, ``_sqlite3`` / ``sqlite3.Connection(path)``,
    ``ctypes`` (e.g. CredReadW), new processes, and ``open()`` of .env / *.db / api-token / credential
    store files."""
    if event in _BLOCKED_EVENTS or event.startswith(_BLOCKED_EVENT_PREFIXES):
        raise WorkerIsolationError(f"{event}: not allowed in a compute worker (SPEC §10.2)")
    if event in ("open", "os.open", "io.open") and args and _forbidden_path(args[0]):
        raise WorkerIsolationError(f"open({args[0]!r}): not allowed in a compute worker (SPEC §10.2)")


_HOOKED = False


def install_audit_hook() -> None:
    """Pre-imports what the engine needs (numpy imports ctypes at import time), then adds the audit hook
    (cannot be removed for the life of the process). Called in the worker entry BEFORE unpickling."""
    global _HOOKED
    if _HOOKED:
        return
    import numpy  # noqa: F401
    import zoneinfo  # noqa: F401
    from ..backtest import engine, metrics, stress  # noqa: F401
    from ..strategy import evaluate, features  # noqa: F401
    from . import folds  # noqa: F401
    import strategy_platform.execution.paper_broker  # noqa: F401
    import strategy_platform.core.params  # noqa: F401
    sys.addaudithook(_audit_hook)
    _HOOKED = True


def install_worker_guard() -> None:
    """Isolates the current process (module docstring). Idempotent; called first in every worker."""
    global _GUARDED
    if _GUARDED:
        return
    for k in list(os.environ):
        if SECRET_ENV_RE.search(k):
            os.environ.pop(k, None)
    for name in BLOCKED_MODULES:
        stub = _BlockedModule(name)
        sys.modules[name] = stub
        parent, _, child = name.rpartition(".")
        if parent and parent in sys.modules:
            setattr(sys.modules[parent], child, stub)
    import socket
    socket.socket.connect = _blocked("socket.connect")
    socket.socket.connect_ex = _blocked("socket.connect_ex")
    socket.socket.sendto = _blocked("socket.sendto")
    socket.create_connection = _blocked("socket.create_connection")
    socket.getaddrinfo = _blocked("socket.getaddrinfo")
    import sqlite3
    sqlite3.connect = _blocked("sqlite3.connect")
    try:
        sqlite3.dbapi2.connect = sqlite3.connect
    except AttributeError:
        pass
    import strategy_platform                 # the package itself (parent of the blocked secrets module)
    strategy_platform.secrets = sys.modules["strategy_platform.secrets"]
    from ..storage import db                 # imported by validation.families: block its use instead
    for fn in ("init", "_open", "_connect"):
        setattr(db, fn, _blocked(f"storage.db.{fn}"))
    _GUARDED = True


def guard_active() -> bool:
    return _GUARDED


# --- tasks (top-level: picklable, run in workers AND inline) ----------------------------------------------

def _engine_of(shared: dict):
    if shared.get("run_engine") is not None:
        return shared["run_engine"]
    from ..backtest import engine
    return engine.run


def _connection_guarded(run_engine):
    """Wraps run_engine so a connection error escapes the stress runner's UserFacingError handling."""
    def run(spec, **kw):
        try:
            return run_engine(spec, **kw)
        except Exception as ex:
            from ..core.errors import UserFacingError
            if isinstance(ex, UserFacingError) and not getattr(ex, "is_cancellation", False) \
                    and is_connection_error(ex):
                raise ConnectionLost(ex) from ex
            raise
    return run


def _returns_by_date(result) -> Dict[str, float]:
    from . import folds
    get = (lambda k: result.get(k)) if isinstance(result, dict) else (lambda k: getattr(result, k))
    dates = list(get("dates") or [])
    return {dates[j]: r for j, r in folds.daily_returns(get("equity") or [])}


def grid_task(shared: dict, payload: dict) -> dict:
    """One parameter-grid configuration -> {series: {date: return}} | {reason} (SPEC §9.3 step 3)."""
    from ..core.errors import UserFacingError
    run_engine = _engine_of(shared)
    try:
        res = run_engine(payload["spec"], start=shared["start"], end=shared["end"], capital=shared["capital"],
                         benchmark=shared["benchmark"], cancel=shared.get("cancel"), data=shared["data"],
                         record_symbol_pnl=False)       # SPEC §11.5 (L7): grid runs carry no symbol_pnl
    except UserFacingError as ex:
        if getattr(ex, "is_cancellation", False):
            raise
        if is_connection_error(ex):
            raise ConnectionLost(ex) from ex
        return {"series": None, "reason": ex.user_message}
    return {"series": _returns_by_date(res), "reason": None}


def episode_task(shared: dict, payload: dict) -> dict:
    """One historical stress episode (``backtest.stress.episode``)."""
    from ..backtest import stress
    a, b = date.fromisoformat(payload["start"]), date.fromisoformat(payload["end"])
    return stress.episode(payload["key"], payload["label"], a, b, shared["spec"], capital=shared["stress_capital"],
                          benchmark=shared["stress_benchmark"], data=shared["data"], cancel=shared.get("cancel"),
                          run_engine=_connection_guarded(_engine_of(shared)))


def costs_task(shared: dict, payload: dict) -> dict:
    """One synthetic costs scenario (``backtest.stress.synthetic_costs``) on the base run's range."""
    from ..backtest import stress
    return stress.synthetic_costs(payload["key"], payload["label"], shared["spec"], shared["base_result"],
                                  payload["fee_mult"], payload["slippage_mult"], data=shared["data"],
                                  cancel=shared.get("cancel"), run_engine=_connection_guarded(_engine_of(shared)))


def isolation_check_task(shared: dict, payload: dict) -> dict:
    """Self-check of the worker guard (tests, diagnostics): tries each forbidden access and reports
    {check: "blocked: <error type>" | "ALLOWED"} plus the environment variables that look like secrets."""
    out: Dict[str, Any] = {"guard_active": guard_active(), "main_module_file": getattr(sys.modules.get("__main__"),
                                                                                      "__file__", None)}

    def attempt(name, fn):
        try:
            fn()
            out[name] = "ALLOWED"
        except WorkerIsolationError as ex:
            out[name] = "blocked: " + type(ex).__name__
        except Exception as ex:  # noqa: BLE001 — anything else is reported, not hidden
            out[name] = "error: " + type(ex).__name__

    def use_secrets():
        from strategy_platform import secrets
        secrets.require_secret("tiingo-api-key")

    def use_keyring():
        import keyring
        keyring.get_password("x", "y")

    def use_socket():
        import socket
        socket.create_connection(("127.0.0.1", 9), timeout=1)

    def use_dns():
        import socket
        socket.getaddrinfo("example.com", 443)

    def use_sqlite():
        import sqlite3
        sqlite3.connect(":memory:")

    def use_storage():
        from strategy_platform.storage import db
        db.init(payload.get("db_path") or ":memory:")

    def use_http_gate():
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:9/", timeout=1)

    tmp = payload.get("tmp_dir") or os.getcwd()

    def raw_socket():
        import _socket
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", 9))

    def udp_sendmsg():
        import _socket
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.sendmsg([b"x"], [], 0, ("127.0.0.1", 9))

    def raw_sqlite():
        import _sqlite3
        _sqlite3.connect(os.path.join(tmp, "raw.db"))

    def sqlite_connection_class():
        import sqlite3
        sqlite3.Connection(os.path.join(tmp, "cls.db"))

    def use_ctypes():
        import ctypes
        import ctypes.util
        ctypes.CDLL(ctypes.util.find_library("c") or "msvcrt")

    def asyncio_connect():
        import asyncio

        loop = asyncio.new_event_loop()            # its self-pipe is a socketpair: already blocked here
        try:
            loop.run_until_complete(asyncio.wait_for(asyncio.open_connection("127.0.0.1", 9), 2))
        finally:
            loop.close()

    def open_env():
        open(os.path.join(tmp, ".env"), "w").close()

    def open_db():
        open(os.path.join(tmp, "hedgefund.db"), "rb").close()

    def open_token():
        open(os.path.join(tmp, "data", "api-token")).close()

    def new_process():
        import subprocess
        subprocess.run([sys.executable, "-c", "pass"])

    for name, fn in (("secrets", use_secrets), ("keyring", use_keyring), ("socket", use_socket), ("dns", use_dns),
                     ("sqlite", use_sqlite), ("storage", use_storage), ("http", use_http_gate),
                     ("raw_socket", raw_socket), ("udp_sendmsg", udp_sendmsg), ("raw_sqlite", raw_sqlite),
                     ("sqlite_connection", sqlite_connection_class), ("ctypes", use_ctypes),
                     ("asyncio_connect", asyncio_connect), ("open_env", open_env), ("open_db", open_db),
                     ("open_token", open_token), ("subprocess", new_process)):
        attempt(name, fn)
    out["audit_hook"] = _HOOKED
    out["secret_env"] = sorted(k for k in os.environ if SECRET_ENV_RE.search(k))
    return out


def hold_task(shared: dict, payload: dict) -> dict:
    """Diagnostics/tests: keeps a worker busy for ``seconds`` (≤ 60) — e.g. to check that workers end with
    their parent."""
    time.sleep(min(60.0, max(0.0, float(payload.get("seconds", 1.0)))))
    return {"pid": os.getpid()}


TASKS = {"hold": hold_task, "grid": grid_task, "episode": episode_task, "costs": costs_task, "isolation_check": isolation_check_task}


# --- planning ---------------------------------------------------------------------------------------------

def per_worker_bytes(n_symbols: int, n_sessions: int) -> int:
    cells = max(1, int(n_symbols)) * max(1, int(n_sessions))
    return WORKER_BASE_BYTES + cells * (ROW_BYTES * SERIES_PER_SYMBOL * SNAPSHOT_COPIES + 8 * PANEL_ARRAYS)


def plan_workers(n_symbols: int, n_sessions: int, n_tasks: Optional[int] = None, *,
                 cpu_count: Optional[int] = None, limit: int = MEMORY_LIMIT_BYTES) -> dict:
    """{workers, cpu_workers, memory_workers, per_worker_bytes, limit_bytes} (module docstring)."""
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    cpu_workers = max(1, int(cpus) - 1)
    per = per_worker_bytes(n_symbols, n_sessions)
    mem_workers = max(1, int(limit // per))
    workers = min(cpu_workers, mem_workers)
    if n_tasks is not None:
        workers = min(workers, max(1, int(n_tasks)))
    return {"workers": max(1, workers), "cpu_workers": cpu_workers, "memory_workers": mem_workers,
            "per_worker_bytes": per, "limit_bytes": limit}


# --- process pool -------------------------------------------------------------------------------------------

_MAIN_LOCK = threading.Lock()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_PROCS: set = set()                  # worker processes of running pools (terminate_all)


def active_workers() -> list:
    with _ACTIVE_LOCK:
        return [p for p in _ACTIVE_PROCS if p.is_alive()]


def terminate_all(timeout: float = 3.0) -> int:
    """Terminates every worker of every running pool (backend /shutdown calls this before os._exit)
    -> the number of processes that were alive."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCS)
    alive = [p for p in procs if p.is_alive()]
    for p in alive:
        p.terminate()
    deadline = time.monotonic() + timeout
    for p in alive:
        p.join(max(0.0, deadline - time.monotonic()))
        if p.is_alive() and hasattr(p, "kill"):
            p.kill()
    return len(alive)


@contextlib.contextmanager
def _hidden_main():
    """While a spawn child starts: an empty ``__main__`` so the child does not re-run the parent's main
    script (multiprocessing.spawn.get_preparation_data reads ``sys.modules["__main__"]``)."""
    with _MAIN_LOCK:
        real = sys.modules.get("__main__")
        sys.modules["__main__"] = types.ModuleType("__main__")
        try:
            yield
        finally:
            if real is not None:
                sys.modules["__main__"] = real


def _parent_watchdog() -> None:
    """Ends this worker at once when the parent process is gone (review 2026-10-10: /shutdown's os._exit left
    daemon workers running, stuck flushing to a dead pipe). ``os._exit`` skips the queue feeder flush."""
    import multiprocessing
    from multiprocessing.connection import wait
    parent = multiprocessing.parent_process()
    ppid = os.getppid()

    def watch():
        while True:
            if parent is not None:
                if wait([parent.sentinel], timeout=WATCHDOG_SECONDS):
                    os._exit(3)
            else:
                time.sleep(WATCHDOG_SECONDS)
            if os.name != "nt" and os.getppid() != ppid:
                os._exit(3)

    threading.Thread(target=watch, name="sp-worker-parent-watchdog", daemon=True).start()


def _worker_main(shared_blob: bytes, task_q, result_q) -> None:
    """Worker entry (spawned): parent watchdog, guard and audit hook first, then tasks until the None
    sentinel."""
    _parent_watchdog()
    install_worker_guard()
    install_audit_hook()
    shared = pickle.loads(shared_blob)
    shared["data"] = ReplayLoader(shared.pop("snapshot", {}) or {})
    shared["cancel"] = None
    while True:
        item = task_q.get()
        if item is None:
            break
        index, name, payload = item
        try:
            out = ("ok", TASKS[name](shared, payload))
        except NeedsPrefetch as ex:
            out = ("need", ex.keys)
        except ConnectionLost as ex:                       # cannot happen without network; reported anyway
            out = ("crash", f"connection error in a worker: {ex}")
        except BaseException:  # noqa: BLE001 — reported to the parent with the traceback
            out = ("crash", traceback.format_exc()[-8000:])
        result_q.put((index, out))


def _run_pool(workers: int, shared_blob: bytes, items: List[Tuple[int, str, dict]], *,
              cancel: Optional[Callable[[], bool]], on_result: Optional[Callable[[int, tuple], None]]) -> Dict[int, tuple]:
    """Runs ``items`` [(index, task name, payload)] on ``workers`` spawned processes -> {index: outcome}."""
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    task_q, result_q = ctx.Queue(), ctx.Queue()
    procs = []
    out: Dict[int, tuple] = {}
    try:
        for i in range(workers):
            p = ctx.Process(target=_worker_main, args=(shared_blob, task_q, result_q),
                            name=f"sp-validation-worker-{i}", daemon=True)
            with _hidden_main():
                p.start()
            procs.append(p)
            with _ACTIVE_LOCK:
                _ACTIVE_PROCS.add(p)
        for it in items:
            task_q.put(it)
        for _ in procs:
            task_q.put(None)
        import queue as queue_mod
        while len(out) < len(items):
            if cancel is not None and cancel():
                from ..backtest.engine import BacktestCancelled
                raise BacktestCancelled()
            try:
                index, outcome = result_q.get(timeout=POLL_SECONDS)
            except queue_mod.Empty:
                if all(not p.is_alive() for p in procs) and result_q.empty():
                    codes = [p.exitcode for p in procs]
                    raise RuntimeError(f"all validation workers ended before finishing (exit codes {codes})")
                continue
            out[index] = outcome
            if on_result is not None:
                on_result(index, outcome)
        for p in procs:
            p.join(JOIN_SECONDS)
        return out
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(JOIN_SECONDS)
        with _ACTIVE_LOCK:
            for p in procs:
                _ACTIVE_PROCS.discard(p)
        for q in (task_q, result_q):
            q.cancel_join_thread()
            q.close()


class TaskRunner:
    """Runs task lists (``TASKS``) either inline (``workers`` 1: the parent's loader and engine, the
    sequential computation) or on a spawned process pool with a data snapshot (module docstring).
    ``loader`` is the job's ``RecordingLoader`` (``keep=True`` for the pool)."""

    def __init__(self, *, workers: int, loader, run_engine=None, cancel: Optional[Callable[[], bool]] = None,
                 progress: Optional[Callable[[str, float, str], None]] = None):
        self.workers = max(1, int(workers))
        self.loader = loader
        self.run_engine = run_engine
        self.cancel = cancel
        self.progress = progress
        self.rounds = 0                 # pool rounds over all run() calls (1 per phase + prefetch rounds)

    @property
    def mode(self) -> str:
        return "pool" if self.workers > 1 else "inline"

    def _emit(self, phase, pct, msg):
        if self.progress is not None:
            self.progress(phase, pct, msg)

    def _check(self):
        if self.cancel is not None and self.cancel():
            from ..backtest.engine import BacktestCancelled
            raise BacktestCancelled()

    def run(self, phase: str, shared: dict, tasks: List[Tuple[str, dict]], *, label: str = "task",
            on_result: Optional[Callable[[int, Any], None]] = None) -> List[Any]:
        """-> the task results in task order. A connection error raises ``ConnectionLost`` (inline) or the
        connection error itself (prefetch); cancel -> ``BacktestCancelled``; a worker crash ->
        RuntimeError (traceback in backend.log)."""
        n = len(tasks)
        results: List[Any] = [None] * n
        if n == 0:
            return results
        if self.mode == "inline":
            sh = dict(shared, data=self.loader, run_engine=self.run_engine, cancel=self.cancel)
            for i, (name, payload) in enumerate(tasks):
                self._check()
                self._emit(phase, 100.0 * i / n, f"{label} {i + 1}/{n}")
                results[i] = TASKS[name](sh, payload)
                if on_result is not None:
                    on_result(i, results[i])
            return results
        # pool: collect the data calls, prefetch them in the parent, ship the snapshot
        keys: List[tuple] = []
        for name, payload in tasks:
            self._check()
            col = CollectingLoader()
            TASKS[name](dict(shared, data=col, run_engine=self.run_engine, cancel=None), payload)
            keys.extend(k for k in col.keys if k not in keys)
        pending = list(range(n))
        done = 0
        rounds = 0
        while pending:
            rounds += 1
            self.rounds += 1
            if rounds > MAX_PREFETCH_ROUNDS:
                raise RuntimeError(f"worker data still missing after {MAX_PREFETCH_ROUNDS} prefetch rounds")
            missing = [k for k in keys if not self.loader.has(k)]
            for j, k in enumerate(missing):
                self._check()
                self._emit(phase, 0.0, f"loading data for the {label}s ({j + 1}/{len(missing)})")
                self.loader.fetch(k)
            blob = pickle.dumps(dict(shared, snapshot=self.loader.snapshot(keys),
                                     run_engine=self.run_engine), protocol=pickle.HIGHEST_PROTOCOL)
            items = [(i, tasks[i][0], tasks[i][1]) for i in pending]
            self._emit(phase, 100.0 * done / n, f"{label}s: {len(items)} on {min(self.workers, len(items))} "
                                                f"worker process(es)")
            need: List[int] = []

            def got(index, outcome):
                nonlocal done
                if outcome[0] == "ok":
                    results[index] = outcome[1]
                    done += 1
                    self._emit(phase, 100.0 * done / n, f"{label} {done}/{n}")
                    if on_result is not None:
                        on_result(index, outcome[1])

            outs = _run_pool(min(self.workers, len(items)), blob, items, cancel=self.cancel, on_result=got)
            for index in sorted(outs):
                kind, value = outs[index]
                if kind == "need":
                    need.append(index)
                    keys.extend(k for k in value if k not in keys)
                elif kind == "crash":
                    logger.error("validation worker failed on %s #%d:\n%s", label, index, value)
                    raise RuntimeError(f"a validation worker failed on {label} #{index}")
            pending = need
        return results
