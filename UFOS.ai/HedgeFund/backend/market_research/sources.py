"""Research's access to Alpaca REST (SPEC §7, §12) — ONLY through the Strategy Platform data layer:

- 1m SIP history: ``AlpacaSipProvider.get_bars(…, "1Min", …)`` — the provider clamps every request end
  to ``sip_request_end`` (now − 15 min): the restricted last 15 minutes are never requested;
- 30-minute SIP history (``"30Min"``) for 1H/4H days older than the 1m store;
- 1m IEX (live tail at load) and crypto 1m: ``AlpacaProvider.get_bars``;
- last trade: ``AlpacaProvider.latest_trade`` (IEX / crypto);
- daily equity bars: the hedgefund.db bar cache (``data.cache``), SIP daily history
  (``equity_bars_history``), ``adjustment`` ``all`` (chart) or ``split`` (statistics);
- crypto hours: the hedgefund.db cache, 1Hour crypto bars (UTC aligned; Research builds 4H / UTC days /
  weeks from them, Q9);
- asset list: ``AlpacaProvider.assets()`` (MR2 extension);
- splits (MR5 drawing split rule): ``AlpacaProvider.corporate_actions`` (existing, unchanged);
- news (MR6): ``AlpacaProvider.news`` (Benzinga via Alpaca, extension: ``images``); logos (MR6):
  ``AlpacaProvider.logo`` (new provider op; HTTP 401/403 → ``LogosNotAvailable``).

Every call goes through the shared Alpaca ``http_gate``; the CALLER runs it inside
``http_gate.research_priority()`` on the Research pool (``executor.run_blocking``), so Research uses at
most 60 req/min of the shared 180/min and yields to trading/platform requests (SPEC §7.5). Missing keys,
refused plans, network failures raise the provider's ``UserFacingError`` — never another source.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import threading
from datetime import timedelta
from typing import List, Optional

from strategy_platform.core.clock import to_utc

HISTORY_DATA_TYPE = "equity_bars_history"
ONE_SECOND = timedelta(seconds=1)


class AlpacaSources:
    def __init__(self, *, iex=None, sip=None, cache=None):
        self._iex, self._sip, self._cache = iex, sip, cache
        self._lock = threading.Lock()

    def _providers(self):
        with self._lock:
            if self._iex is None or self._sip is None:
                from strategy_platform.data.providers.alpaca import AlpacaProvider, AlpacaSipProvider
                if self._iex is None:
                    self._iex = AlpacaProvider()
                if self._sip is None:
                    self._sip = AlpacaSipProvider()
            return self._iex, self._sip

    def _bar_cache(self):
        if self._cache is not None:
            return self._cache
        from strategy_platform.data import cache
        return cache.default_cache()

    # --- 1m (start inclusive, end EXCLUSIVE: Alpaca's end is inclusive, so ask for end − 1 s) -----------
    @staticmethod
    def _range(rows, start, end) -> List[dict]:
        s, e = to_utc(start), to_utc(end)
        return [r for r in rows or [] if s <= to_utc(r["ts"]) < e]

    def sip_1m(self, symbol: str, start, end) -> List[dict]:
        _iex, sip = self._providers()
        return self._range(sip.get_bars(symbol, "1Min", to_utc(start), to_utc(end) - ONE_SECOND), start, end)

    def sip_30m(self, symbol: str, start, end) -> List[dict]:
        """SIP 30-minute bars (clock aligned, i.e. on the 09:30 / 04:00 anchors) for days older than the 1m
        store — 1H/4H history (review M4: 32 bars per session instead of 960)."""
        _iex, sip = self._providers()
        return self._range(sip.get_bars(symbol, "30Min", to_utc(start), to_utc(end) - ONE_SECOND), start, end)

    def iex_1m(self, symbol: str, start, end) -> List[dict]:
        iex, _sip = self._providers()
        return self._range(iex.get_bars(symbol, "1Min", to_utc(start), to_utc(end) - ONE_SECOND), start, end)

    def crypto_1m(self, symbol: str, start, end) -> List[dict]:
        iex, _sip = self._providers()
        return self._range(iex.get_bars(symbol, "1Min", to_utc(start), to_utc(end) - ONE_SECOND), start, end)

    def latest_trade(self, symbol: str) -> dict:
        iex, _sip = self._providers()
        return iex.latest_trade(symbol)

    def assets(self) -> List[dict]:
        iex, _sip = self._providers()
        return iex.assets()

    def splits(self, symbols: List[str], start, end) -> List[dict]:
        """MR5 split rule: forward/reverse splits (ex_date in [start, end]) via the existing provider's
        ``corporate_actions`` (Alpaca CorporateActionsClient, through the shared gate)."""
        iex, _sip = self._providers()
        return iex.corporate_actions(symbols, start, end, kinds=("forward_split", "reverse_split"))

    def news(self, symbol: str, end=None, limit: int = 20) -> List[dict]:
        """MR6: the symbol's newest Alpaca news (≤ limit, newest first); ``end`` inclusive (Alpaca)."""
        iex, _sip = self._providers()
        return iex.news([symbol], end=end, limit=limit)

    def logo(self, symbol: str):
        """MR6: PNG bytes, or None when Alpaca has no logo for the symbol."""
        iex, _sip = self._providers()
        return iex.logo(symbol)

    # --- hedgefund.db cache -----------------------------------------------------------------------
    def daily(self, symbol: str, start, end, *, adjustment: str = "all", fresh=None) -> List[dict]:
        return self._bar_cache().get_bars(symbol, "1Day", to_utc(start), to_utc(end), require_fresh_through=fresh,
                                          adjustment=adjustment, data_type=HISTORY_DATA_TYPE)

    def crypto_hourly(self, symbol: str, start, end) -> List[dict]:
        return self._bar_cache().get_bars(symbol, "1Hour", to_utc(start), to_utc(end))


_default: Optional[AlpacaSources] = None


def default() -> AlpacaSources:
    global _default
    if _default is None:
        _default = AlpacaSources()
    return _default
