"""MR6 logos (SPEC §4.2, §13 "MR6 backend notes"): ``GET /research/symbols/{s}/logo``.

Source: Alpaca ``GET /v1beta1/logos/{symbol}`` via the new provider op ``AlpacaProvider.logo`` (no
placeholder image is ever requested). Alpaca documents logos as a paid add-on (Broker API), so on the free
plan a refusal (HTTP 401/403) is expected: it is remembered account-wide (meta ``logos_refused``) and
answered as 404 ``kind: "LogosNotOnPlan"`` with the reason; Alpaca is asked again after
``LOGO_PLAN_RECHECK_SECONDS``. The UI's normal empty state (ticker initials as plain text) covers both
"no logo" and "not on this plan" — no other logo source, no generated image.

Cache (``logos`` table): ``ok`` rows (PNG bytes) are served for ``RETENTION_DAYS["logos"]`` (30) days,
``none`` rows (Alpaca answered 404) for ``LOGO_NONE_DAYS`` (7). Checks: ``image/png``, PNG signature,
≤ ``MAX_LOGO_BYTES``, ≤ ``MAX_LOGO_SIDE`` pixels per side (provider + ``media.check_image``).
Blocking; the route runs it on the Research pool inside ``http_gate.research_priority()``.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from strategy_platform.core.errors import DataSourceUnavailable, UserFacingError

from . import limits
from . import store as default_store
from .media import ImageRejected, check_image

logger = logging.getLogger(__name__)

META_REFUSED = "logos_refused"


class NoLogo(UserFacingError):
    def __init__(self, symbol: str):
        super().__init__(f"No logo available from Alpaca for {symbol}.")


class LogosNotOnPlan(UserFacingError):
    def __init__(self, reason: str):
        super().__init__(limits.clip(f"Logos are not available on this Alpaca plan: {reason}", 600))
        self.reason = reason


def _parse(ts) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class LogoService:
    def __init__(self, data_fn: Callable[[], object], *, store=default_store):
        self._data_fn = data_fn
        self.store = store
        self._lock = threading.Lock()
        self._refused: Optional[dict] = None       # {"at": iso, "reason": str} (mirror of meta logos_refused)
        self._last_ok: Optional[str] = None

    def status(self) -> dict:
        """{state: unknown|available|not_on_plan, reason, checked_at} — no I/O beyond the meta row."""
        refused = self._load_refused()
        if refused:
            return {"state": "not_on_plan", "reason": LogosNotOnPlan(refused["reason"]).user_message,
                    "checked_at": refused["at"]}
        if self._last_ok:
            return {"state": "available", "reason": None, "checked_at": self._last_ok}
        return {"state": "unknown", "reason": None, "checked_at": None}

    def _load_refused(self) -> Optional[dict]:
        raw = self.store.meta_get(META_REFUSED)
        try:
            v = json.loads(raw) if raw else None
        except ValueError:
            v = None
        self._refused = v if isinstance(v, dict) and "at" in v and "reason" in v else None
        return self._refused

    def logo(self, symbol: str) -> bytes:
        """PNG bytes, else NoLogo / LogosNotOnPlan (404) or a UserFacingError (503)."""
        data = self._data_fn()
        a = data.asset(symbol)                     # SymbolNotCovered → 404
        sym = a["symbol"]
        now = data.now()
        row = self.store.logo_get(sym)
        if row is not None:
            at = _parse(row["fetched_at"])
            age = (now - at) if at else None
            if row["status"] == "ok" and age is not None and age < timedelta(days=limits.RETENTION_DAYS["logos"]):
                return row["png"]
            if row["status"] == "none" and age is not None and age < timedelta(days=limits.LOGO_NONE_DAYS):
                raise NoLogo(sym)
        refused = self._load_refused()
        if refused:
            at = _parse(refused["at"])
            if at is not None and (now - at).total_seconds() < limits.LOGO_PLAN_RECHECK_SECONDS:
                raise LogosNotOnPlan(refused["reason"])
        from strategy_platform.data.providers.alpaca import LogosNotAvailable
        stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        try:
            png = data.sources.logo(sym)
        except LogosNotAvailable as ex:
            self.store.meta_set(META_REFUSED, json.dumps({"at": stamp, "reason": ex.reason}))
            self._last_ok = None
            logger.info("Research logos: Alpaca refused the logo endpoint (%s)", ex.reason)
            raise LogosNotOnPlan(ex.reason) from ex
        if refused:
            self.store.meta_set(META_REFUSED, None)    # the plan includes logos now
        self._last_ok = stamp
        if png is None:
            self.store.logo_set(sym, "none", stamp)
            raise NoLogo(sym)
        try:
            check_image(png, "image/png", allowed=("image/png",), max_bytes=limits.MAX_LOGO_BYTES,
                        max_side=limits.MAX_LOGO_SIDE)
        except ImageRejected as ex:
            raise DataSourceUnavailable("alpaca", "logos", f"the logo for {sym} was rejected: {ex.user_message}") from ex
        self.store.logo_set(sym, "ok", stamp, png)
        return png
