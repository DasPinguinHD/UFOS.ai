"""Daily AI caps of one backend + the standard ``/ai/budgets`` payload (task B, 2026-10-17).

Contract: ``UFOS.ai/docs/ai-budgets-spec.md`` (§3 enforcement, §4 API). One IDENTICAL
copy per backend (compartmentalization, like api_auth.py) — keep them in sync:
``Global Monitoring/backend/ai_caps.py``, ``LiveStreamAgent/backend/ai_caps.py``,
``HedgeFund/backend/ai_caps.py``.

Usage (per backend):

    caps = DailyCaps("global-monitoring", "Global Monitoring", [CapDef(...), ...],
                     kv_get, kv_set, kv_add=..., available=..., audit=...)
    res = caps.reserve("assess")          # BEFORE the call; raises BudgetBlocked (fail closed)
    try:
        ... the OpenRouter call ...
    except Exception as ex:
        caps.charge_for_exception(res, ex)  # timeout -> books the estimate, else nothing
        raise
    caps.settle(res, usage.get("cost_usd"))  # reported cost, or the estimate when none was reported

``kv_get(key) -> str | None`` / ``kv_set(key, str)`` / ``kv_add(key, delta) -> float`` must
RAISE on any storage failure (never return a stand-in) — ``storage_kv(store)`` builds them
for the GMS/LSA ``storage.Storage`` classes. Limits are stored under ``ai_cap_limit:<key>``,
the per-UTC-day spend under ``ai_cap_spend:<key>:<YYYY-MM-DD>`` (strings, ``repr(float)``).

No unrequested fallbacks: an unreadable limit/spend, a failed spend write (paused until
the backend restarts) or an unusable store (in-memory stand-in) refuses every AI call with
a readable message. Stdlib only. Part of UFOS.ai, a proof-of-concept portfolio project.
"""
import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

SCHEMA = 1
MAX_BODY_BYTES = 4096
LIMIT_PREFIX = "ai_cap_limit:"
SPEND_PREFIX = "ai_cap_spend:"
WHERE = "AI budgets (main window → Settings)"
ORIGIN_REFUSED = "budgets can only be changed from the app"
TEXT_MAX = 300

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapDef:
    """One daily cap. ``estimate_usd``: per-call estimate checked before the call (and booked
    when the provider reports no cost). ``cost_log``: ((module, feature), ...) of the cost log."""
    key: str
    label: str
    default_usd: float
    estimate_usd: float
    max_usd: float = 50.0
    min_usd: float = 0.0
    step: float = 0.1
    group: Optional[str] = None
    notes: str = ""
    cost_log: tuple = ()


class StoreUnavailable(Exception):
    """The module database cannot be read/written (or is an in-memory stand-in)."""


class BudgetBlocked(Exception):
    """A cap refuses the call. ``reason``: off | reached | unreadable | unrecorded | invalid | store."""

    def __init__(self, key: str, reason: str, message: str):
        super().__init__(message)
        self.key = key
        self.reason = reason
        self.message = message

    def payload(self) -> dict:
        return {"error": self.message, "code": "budget", "budget_key": self.key, "reason": self.reason}


class Reservation:
    __slots__ = ("key", "estimate", "day", "done")

    def __init__(self, key: str, estimate: float, day: str):
        self.key, self.estimate, self.day, self.done = key, estimate, day, False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _finite(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _usd(v: float) -> str:
    return f"{v:.2f}" if v >= 0.995 or v == 0 else f"{v:.4f}"


def _r6(v):
    return None if v is None else round(float(v), 6)


def is_timeout(ex: BaseException) -> bool:
    """True when the request was sent but timed out (the provider may still bill it)."""
    seen = 0
    while ex is not None and seen < 5:
        name = type(ex).__name__.lower()
        if "timeout" in name or isinstance(ex, TimeoutError) or "timed out" in str(ex).lower():
            return True
        ex = ex.__cause__ or ex.__context__
        seen += 1
    return False


class DailyCaps:
    """Daily caps of one backend (see module docstring)."""

    def __init__(self, module_id: str, module_label: str, caps, kv_get: Callable, kv_set: Callable,
                 kv_add: Optional[Callable] = None, available: Optional[Callable[[], bool]] = None,
                 audit: Optional[Callable[[dict], object]] = None, clock: Optional[Callable[[], datetime]] = None,
                 logger: Optional[logging.Logger] = None):
        self.module_id = module_id
        self.module_label = module_label
        self.caps = {c.key: c for c in caps}
        if len(self.caps) != len(list(caps)):
            raise ValueError("duplicate cap key")
        self._get, self._set, self._add = kv_get, kv_set, kv_add
        self._available = available or (lambda: True)
        self._audit = audit
        self._clock = clock or utc_now
        self._logger = logger or _log
        self._lock = threading.RLock()
        self._reserved = {}               # key -> estimates of calls in flight
        self._unrecorded = False          # a spend write failed -> paused until restart

    # --- reading ---------------------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    def day(self, now: Optional[datetime] = None) -> str:
        return (now or self._now()).astimezone(timezone.utc).date().isoformat()

    def _cap(self, key: str) -> CapDef:
        if key not in self.caps:
            raise KeyError(key)
        return self.caps[key]

    def _msg(self, cap: CapDef, reason: str, spent: float = 0.0, limit: float = 0.0) -> str:
        label = cap.label
        if reason == "off":
            return f"{label} is switched off (daily cap is 0) — raise it in {WHERE}."
        if reason == "reached":
            return (f"Daily AI cap for {label} reached (${_usd(spent)} of ${_usd(limit)} today, UTC) — "
                    f"raise it in {WHERE} or try again tomorrow.")
        if reason == "unreadable":
            return (f"{label}: today's AI spend could not be read — AI calls are paused until it can be "
                    "read again (see backend.log).")
        if reason == "unrecorded":
            return f"{label}: AI spend could not be recorded — AI calls are paused until the backend restarts."
        if reason == "invalid":
            return f"The stored daily cap for {label} is invalid — set it again in {WHERE}."
        return f"{label}: the module database is unavailable — AI calls are paused (see backend.log)."

    def _store_ok(self) -> bool:
        try:
            return bool(self._available())
        except Exception:     # noqa: BLE001 — unknown = unavailable (fail closed)
            return False

    def limit(self, key: str) -> float:
        """The stored limit (default when never set); raises BudgetBlocked (store / invalid)."""
        cap = self._cap(key)
        try:
            raw = self._get(LIMIT_PREFIX + key)
        except Exception:     # noqa: BLE001
            self._logger.error("AI caps: limit of %s unreadable", key, exc_info=True)
            raise BudgetBlocked(key, "store", self._msg(cap, "store"))
        if raw is None:
            return float(cap.default_usd)
        v = _finite(raw)
        if v is None or not cap.min_usd <= v <= cap.max_usd:
            raise BudgetBlocked(key, "invalid", self._msg(cap, "invalid"))
        return v

    def spent(self, key: str, day: Optional[str] = None) -> float:
        """Spend booked on the UTC ``day``; raises BudgetBlocked("unreadable")."""
        cap = self._cap(key)
        day = day or self.day()
        try:
            raw = self._get(f"{SPEND_PREFIX}{key}:{day}")
        except Exception:     # noqa: BLE001
            self._logger.error("AI caps: spend of %s unreadable", key, exc_info=True)
            raise BudgetBlocked(key, "unreadable", self._msg(cap, "unreadable"))
        if raw is None:
            return 0.0
        v = _finite(raw)
        if v is None or v < 0:
            self._logger.error("AI caps: stored spend of %s is not a number >= 0", key)
            raise BudgetBlocked(key, "unreadable", self._msg(cap, "unreadable"))
        return v

    def _check(self, key: str, estimate: float, day: str):
        """(limit, spent) when a call with ``estimate`` fits, else raises BudgetBlocked."""
        cap = self._cap(key)
        if not self._store_ok():
            raise BudgetBlocked(key, "store", self._msg(cap, "store"))
        if self._unrecorded:
            raise BudgetBlocked(key, "unrecorded", self._msg(cap, "unrecorded"))
        limit = self.limit(key)
        if limit <= 0:
            raise BudgetBlocked(key, "off", self._msg(cap, "off"))
        spent = self.spent(key, day)
        if spent + self._reserved.get(key, 0.0) + estimate > limit + 1e-12:
            raise BudgetBlocked(key, "reached", self._msg(cap, "reached", spent, limit))
        return limit, spent

    def status(self, key: str):
        """None when the next call (with the default estimate) would be allowed, else (reason, message)."""
        cap = self._cap(key)
        try:
            with self._lock:
                self._check(key, float(cap.estimate_usd), self.day())
            return None
        except BudgetBlocked as ex:
            return ex.reason, ex.message

    # --- enforcing -------------------------------------------------------------------------

    def reserve(self, key: str, estimate: Optional[float] = None) -> Reservation:
        """Checks BEFORE the call and holds ``estimate`` until settle/release; raises BudgetBlocked."""
        cap = self._cap(key)
        est = _finite(estimate) if estimate is not None else float(cap.estimate_usd)
        est = max(0.0, est if est is not None else float(cap.estimate_usd))
        with self._lock:
            day = self.day()
            self._check(key, est, day)
            self._reserved[key] = self._reserved.get(key, 0.0) + est
        return Reservation(key, est, day)

    def release(self, res: Optional[Reservation]) -> None:
        """Ends a reservation without booking (the request never left / was rejected unbilled)."""
        if res is None or res.done:
            return
        with self._lock:
            res.done = True
            self._reserved[res.key] = max(0.0, self._reserved.get(res.key, 0.0) - res.estimate)

    def settle(self, res: Optional[Reservation], cost_usd=None) -> float:
        """Books the reported cost (or the estimate when none was reported) -> USD booked.
        A failed write pauses every cap of this backend until it restarts (fail closed)."""
        if res is None or res.done:
            return 0.0
        cost = _finite(cost_usd)
        usd = cost if cost is not None and cost >= 0 else res.estimate
        self.release(res)
        self.book(res.key, usd, res.day)
        return usd

    def charge_for_exception(self, res: Optional[Reservation], ex: BaseException) -> float:
        """A failed call: a timeout books the estimate (the provider may bill it), anything else nothing."""
        if res is None or res.done:
            return 0.0
        if is_timeout(ex):
            return self.settle(res, None)
        self.release(res)
        return 0.0

    def book(self, key: str, usd: float, day: Optional[str] = None) -> None:
        self._cap(key)
        day = day or self.day()
        usd = max(0.0, _finite(usd) or 0.0)
        if usd == 0.0:
            return
        skey = f"{SPEND_PREFIX}{key}:{day}"
        try:
            with self._lock:
                if self._add is not None:
                    self._add(skey, usd)
                else:
                    raw = self._get(skey)
                    cur = 0.0 if raw is None else _finite(raw)
                    if cur is None or cur < 0:
                        raise ValueError("stored spend unreadable")
                    self._set(skey, repr(cur + usd))
        except Exception:     # noqa: BLE001 — fail closed: paused until restart
            self._unrecorded = True
            self._logger.error("AI caps: spend of %.6f USD for %s could not be recorded — AI calls paused "
                               "until the backend restarts", usd, key, exc_info=True)

    # --- payload + PUT ---------------------------------------------------------------------

    def entry(self, key: str, now: Optional[datetime] = None) -> dict:
        cap = self._cap(key)
        now = now or self._now()
        day = self.day(now)
        out = {"key": cap.key, "label": cap.label[:TEXT_MAX], "group": cap.group, "period": "day",
               "period_id": day, "limit_usd": None, "default_usd": _r6(cap.default_usd),
               "min": _r6(cap.min_usd), "max": _r6(cap.max_usd), "step": cap.step, "spent_usd": None,
               "remaining_usd": None, "enabled": False, "status": "ok", "message": None, "editable": True,
               "applies": "immediately", "notes": cap.notes[:TEXT_MAX],
               "cost_log": [{"module": m, "feature": f} for m, f in cap.cost_log]}
        if not self._store_ok():
            out.update(status="error", message=self._msg(cap, "store"))
            return out
        try:
            limit = self.limit(key)
            out.update(limit_usd=_r6(limit), enabled=limit > 0)
            spent = self.spent(key, day)
        except BudgetBlocked as ex:
            out.update(status="error", message=ex.message)
            return out
        out.update(spent_usd=_r6(spent), remaining_usd=_r6(max(0.0, limit - spent)))
        if self._unrecorded:
            out.update(status="paused", message=self._msg(cap, "unrecorded"))
        elif limit <= 0:
            out.update(status="blocked", message=self._msg(cap, "off"))
        elif spent + self._reserved.get(key, 0.0) + float(cap.estimate_usd) > limit + 1e-12:
            out.update(status="blocked", message=self._msg(cap, "reached", spent, limit))
        return out

    def entries(self, now: Optional[datetime] = None) -> list:
        now = now or self._now()
        return [self.entry(k, now) for k in self.caps]

    def payload(self, now: Optional[datetime] = None, entries: Optional[list] = None) -> dict:
        now = now or self._now()
        return module_payload(self.module_id, self.module_label, self.entries(now) if entries is None else entries,
                              now)

    def check_put(self, body) -> dict:
        """{field: message} for PUT /ai/budgets {key, limit_usd} (empty = valid)."""
        return check_put_body(body, lambda k: self.caps.get(k) and (self.caps[k].min_usd, self.caps[k].max_usd))

    def put(self, key: str, limit_usd: float, actor: str = "app") -> dict:
        """Stores a validated limit (audit first when an audit exists) -> {"key", "old", "new"}.
        Raises StoreUnavailable when the audit row or the value cannot be written."""
        cap = self._cap(key)
        new = round(float(limit_usd), 6)
        if not self._store_ok():
            raise StoreUnavailable("the module database is unavailable (in-memory stand-in)")
        try:
            old = self.limit(key)
        except BudgetBlocked as ex:
            if ex.reason != "invalid":
                raise StoreUnavailable("the stored limit could not be read") from ex
            old = None           # repair path: an invalid stored value is replaced
        change = {"module": self.module_id, "key": key, "old": old, "new": new, "actor": actor}
        if self._audit is not None:
            try:
                self._audit(change)
            except Exception as ex:   # noqa: BLE001 — no audit row, no change
                raise StoreUnavailable("the audit row could not be written") from ex
        try:
            self._set(LIMIT_PREFIX + key, repr(new))
        except Exception as ex:       # noqa: BLE001
            raise StoreUnavailable("the limit could not be stored") from ex
        self._logger.info("AI budget changed: %s/%s %s -> %s (by the %s)", self.module_id, cap.key, old, new, actor)
        return {"key": key, "old": old, "new": new}

    # --- route layer (framework-free: (status, payload)) ------------------------------------

    def handle_get(self, query=None):
        if query:
            return 400, {"error": "invalid input", "errors": {str(k): "unknown parameter" for k in query}}
        return 200, self.payload()

    def handle_put(self, body, origin=None, query=None):
        refused = refuse_put(origin, query)
        if refused:
            return refused
        errors = self.check_put(body)
        if errors:
            return 400, {"error": "invalid input", "errors": errors}
        try:
            changed = self.put(body["key"], float(body["limit_usd"]))
        except StoreUnavailable:
            self._logger.error("AI budget %s could not be changed", body.get("key"), exc_info=True)
            return 503, {"error": "The budget could not be stored (module database unavailable) — see backend.log.",
                         "kind": "StoreUnavailable"}
        out = self.payload()
        out["changed"] = changed
        return 200, out


# --- helpers shared by every backend's route glue ---------------------------------------------

async def guarded_call(caps: DailyCaps, key: str, estimate, factory):
    """``await factory()`` under the cap ``key``: reserve BEFORE (raises BudgetBlocked), settle with
    the reported cost of a ``(text, model, usage)`` result, charge a failed call (timeout -> estimate)."""
    res = caps.reserve(key, estimate)
    try:
        result = await factory()
    except BaseException as ex:
        caps.charge_for_exception(res, ex)
        raise
    usage = result[2] if isinstance(result, tuple) and len(result) >= 3 else None
    caps.settle(res, usage.get("cost_usd") if isinstance(usage, dict) else None)
    return result


async def read_json_body(request):
    """PUT body of an aiohttp request (duck-typed, no aiohttp import): (body, None) or
    (None, (status, payload)) for a body > MAX_BODY_BYTES / invalid JSON. Empty body -> None."""
    too_large = (400, {"error": "request body too large", "errors": {"_": f"at most {MAX_BODY_BYTES} bytes"}})
    length = getattr(request, "content_length", None)
    if length is not None and length > MAX_BODY_BYTES:
        return None, too_large
    try:
        raw = await request.content.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            return None, too_large
        return (json.loads(raw.decode("utf-8")) if raw.strip() else None), None
    except (ValueError, UnicodeDecodeError):
        return None, (400, {"error": "invalid JSON body", "errors": {"_": "invalid JSON body"}})


def module_payload(module_id: str, module_label: str, entries: list, now: Optional[datetime] = None) -> dict:
    now = (now or utc_now()).astimezone(timezone.utc)
    return {"schema": SCHEMA, "module": module_id, "label": module_label,
            "generated_at": now.isoformat(timespec="seconds"), "day": now.date().isoformat(),
            "month": now.strftime("%Y-%m"), "budgets": list(entries)}


def refuse_put(origin=None, query=None):
    """403 for a request sent by a web page (Origin header), 400 for query parameters, else None."""
    if origin is not None:
        return 403, {"error": ORIGIN_REFUSED}
    if query:
        return 400, {"error": "invalid input", "errors": {str(k): "unknown parameter" for k in query}}
    return None


def check_put_body(body, bounds: Callable) -> dict:
    """Validates {key, limit_usd}; ``bounds(key)`` -> (min, max) or None for an unknown key."""
    if not isinstance(body, dict):
        return {"_": "expected a JSON object {key, limit_usd}"}
    errors = {str(k)[:64]: "unknown field" for k in body if k not in ("key", "limit_usd")}
    key = body.get("key")
    rng = None
    if "key" not in body:
        errors["key"] = "required"
    elif not isinstance(key, str) or not key or len(key) > 64:
        errors["key"] = "a budget key"
    else:
        rng = bounds(key)
        if not rng:
            errors["key"] = "unknown budget key"
    if "limit_usd" not in body:
        errors["limit_usd"] = "required"
    else:
        v = body["limit_usd"]
        f = None if isinstance(v, (bool, str)) else _finite(v)
        lo, hi = rng if rng else (0.0, 50.0)
        if f is None or not lo <= f <= hi:
            errors["limit_usd"] = f"a number {lo:g}..{hi:g} (0 = off)"
        elif f != 0 and round(f, 6) == 0:            # would be stored as 0 = off (review L7)
            errors["limit_usd"] = "at least 0.000001, or exactly 0 = off"
    return errors


def storage_kv(store):
    """(kv_get, kv_set, kv_add, available) for a GMS/LSA ``storage.Storage`` (meta table, fail-soft
    ``_run``/``_write``): failures are detected with a sentinel and RAISED (no stand-in values)."""
    failed = object()

    def kv_get(key: str):
        row = store._run("read AI budget", lambda c: c.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone(), failed)
        if row is failed:
            raise StoreUnavailable("read failed")
        return None if row is None else row[0]

    def kv_set(key: str, value: str) -> None:
        done = store._write("write AI budget", lambda c: c.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))).rowcount, failed)
        if done is failed:
            raise StoreUnavailable("write failed")

    def kv_add(key: str, delta: float) -> float:
        def op(c):
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            cur = 0.0 if row is None else _finite(row[0])
            if cur is None or cur < 0:
                raise ValueError("stored spend unreadable")
            total = cur + float(delta)
            c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, repr(total)))
            return total
        total = store._write("book AI spend", op, failed)
        if total is failed:
            raise StoreUnavailable("write failed")
        return total

    def available() -> bool:
        return not getattr(store, "in_memory", False)

    return kv_get, kv_set, kv_add, available
