"""The Research data service (MR2 REST side, SPEC §4.2 / §7): one object the routes call.

All methods are BLOCKING (SQLite + Alpaca REST through the shared gate) — the routes run them via
``executor.run_blocking`` inside ``http_gate.research_priority()`` (SPEC §5.1, §7.5). Errors:
``ValueError`` / ``BadParam`` → 400, ``SymbolNotCovered`` → 404, other ``UserFacingError`` (missing keys,
source unavailable, calendar not covered, store unavailable) → 503 with its message.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from strategy_platform.core.clock import to_utc

from . import assets as assets_mod
from . import market
from . import sources as sources_mod
from . import store as default_store
from .bars import intraday, series
from .stats import context, keystats, snapshot


class DataService:
    def __init__(self, *, sources=None, store=default_store, now: Optional[Callable[[], datetime]] = None):
        self._sources = sources
        self.store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.loader = intraday.MinuteLoader(self, store=store, now=self.now)

    # the loader calls sip_1m / sip_30m / iex_1m / crypto_1m on this object → always the current sources
    def __getattr__(self, name):
        if name in ("sip_1m", "sip_30m", "iex_1m", "crypto_1m"):
            return getattr(self.sources, name)
        raise AttributeError(name)

    @property
    def sources(self):
        return self._sources if self._sources is not None else sources_mod.default()

    def now(self) -> datetime:
        return to_utc(self._now())

    def refresh_assets(self) -> int:
        return assets_mod.refresh(self.sources, self.now(), store=self.store)

    def search_assets(self, q: str, limit: int) -> dict:
        return assets_mod.search(q, limit, self.sources, store=self.store, now=self.now())

    def market_status(self) -> dict:
        return market.status(self.now())

    def asset(self, symbol: str) -> dict:
        return assets_mod.require(symbol, self.sources, store=self.store, now=self.now())

    def bars(self, symbol: str, tf: str, session: Optional[str], before, limit: Optional[int]) -> dict:
        a = self.asset(symbol)
        return series.bars(self.loader, self.sources, a["symbol"], a["asset_class"], tf, session, before, limit,
                           self.now())

    def snapshot(self, symbol: str) -> dict:
        a = self.asset(symbol)
        ctx = context.build(self.loader, self.sources, a["symbol"], a["asset_class"], self.now())
        return snapshot.snapshot(ctx, a)

    def stats(self, symbol: str) -> dict:
        a = self.asset(symbol)
        ctx = context.build(self.loader, self.sources, a["symbol"], a["asset_class"], self.now())
        return keystats.stats(ctx, a)
