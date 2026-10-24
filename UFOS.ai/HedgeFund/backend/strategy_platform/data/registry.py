"""Data source registry (SPEC §3): one configured source per data type.

- ``DEFAULT_SOURCES``: equity_bars, crypto_bars, equity_quotes,
  crypto_quotes, news, corporate_actions -> ``alpaca``;
  equity_bars_pre2016 -> ``tiingo`` (stage 3, user decision 2026-10-03: daily
  equity history BEFORE the seam only — a different period, never a
  substitute for alpaca; see data/seam.py); macro -> ``fred``
  and insider -> ``sec_edgar`` (served by the newsroom collectors, not by a
  provider here). Stage 1 allows exactly one source per type
  (``ALLOWED_SOURCES``), so seeding a missing row with it is configuration,
  not a fallback: there is no other source it could replace.
- The configured source and the last status (ok/error, time, user message)
  live in hedgefund.db ``data_sources`` (storage.db ``data_source_get/
  set/status_update``); storage failures raise ``StorageUnavailable``.
- ``fetch(data_type, op, *args)`` calls the configured provider and records
  the status after EVERY success and failure (``HistoryNotAvailable`` is a
  request error and leaves the status alone). No fallback chain.
- ``test_source("alpaca")`` -> ``{ok, source, checks: [{name, ok, message}]}``:
  1 daily bar SPY, 1 daily bar BTC/USD, the latest SPY quote (used by
  ``POST /v2/data/test``). Missing / rejected keys -> ok false with that
  message; the remaining checks say "Skipped — <reason>".

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from ..core.errors import (DataSourceUnavailable, InvalidRequest, SecretMissing, SecretStoreUnavailable,
                           UserFacingError)
from .licences import licence_for
from .providers.base import HistoryNotAvailable, SourceAuthRejected, iso

logger = logging.getLogger(__name__)

DEFAULT_SOURCES = {
    "equity_bars": "alpaca", "crypto_bars": "alpaca", "equity_quotes": "alpaca", "crypto_quotes": "alpaca",
    "news": "alpaca", "corporate_actions": "alpaca", "macro": "fred", "insider": "sec_edgar",
    "equity_bars_pre2016": "tiingo",
    # user decision 2026-10-06: historical daily equity bars for backtests come from Alpaca SIP
    # (free for queries ending >= 15 min ago; IEX history starts 2020-07 for most ETFs) — own
    # source label, never mixed with the IEX rows of equity_bars (live paper engine)
    "equity_bars_history": "alpaca_sip",
    # consolidated daily volume: Alpaca SIP from the seam on; before it Tiingo via
    # equity_bars_pre2016 + the seam check (data/seam.py volume())
    "equity_volume": "alpaca_sip",
}
ALLOWED_SOURCES = {dt: (src,) for dt, src in DEFAULT_SOURCES.items()}
COLLECTOR_SOURCES = {"fred": "the newsroom's FRED collector", "sec_edgar": "the newsroom's SEC EDGAR collector"}
TEST_LOOKBACK_DAYS = 10
TESTABLE_SOURCES = ("alpaca",)          # test_source() runs the Alpaca checks only
SIP_SOURCE = "alpaca_sip"
SIP_CHECK_DAY = date(2016, 1, 4)        # the seam: SIP history must reach it (IEX history does not)


def _alpaca_factory():
    from .providers.alpaca import AlpacaProvider
    return AlpacaProvider()


def _alpaca_sip_factory():
    from .providers.alpaca import AlpacaSipProvider
    return AlpacaSipProvider()


def _tiingo_factory():
    from .providers.tiingo import TiingoProvider
    return TiingoProvider()


class Registry:
    def __init__(self, store=None, providers: Optional[Dict[str, Callable[[], object]]] = None, clock=None):
        self._store = store
        self._factories = dict(providers) if providers is not None else {"alpaca": _alpaca_factory,
                                                                          "alpaca_sip": _alpaca_sip_factory,
                                                                          "tiingo": _tiingo_factory}
        self._instances: Dict[str, object] = {}
        self._lock = threading.Lock()
        if clock is None:
            self._now = lambda: datetime.now(timezone.utc)
        else:
            self._now = clock.now_utc if hasattr(clock, "now_utc") else clock

    def _db(self):
        if self._store is not None:
            return self._store
        from ..storage import db
        return db

    @staticmethod
    def _check_type(data_type: str) -> None:
        if data_type not in DEFAULT_SOURCES:
            raise ValueError(f"unknown data type {data_type!r}")

    # --- configuration -------------------------------------------------------------------

    def configured_source(self, data_type: str) -> str:
        self._check_type(data_type)
        db = self._db()
        row = db.data_source_get(data_type)
        if row and row.get("source"):
            return row["source"]
        source = DEFAULT_SOURCES[data_type]
        lic = licence_for(source)
        db.data_source_set(data_type, source, licence=lic["licence"], licence_note=lic["note"])
        return source

    def set_source(self, data_type: str, source: str) -> dict:
        self._check_type(data_type)
        if source not in ALLOWED_SOURCES[data_type]:
            raise ValueError(f"{data_type}: source must be one of {', '.join(ALLOWED_SOURCES[data_type])}")
        lic = licence_for(source)
        return self._db().data_source_set(data_type, source, licence=lic["licence"], licence_note=lic["note"])

    def provider(self, source: str):
        with self._lock:
            inst = self._instances.get(source)
            if inst is None:
                factory = self._factories.get(source)
                if factory is None:
                    raise ValueError(f"no data-layer provider for source {source!r}")
                inst = self._instances[source] = factory()
            return inst

    def get_provider(self, data_type: str):
        source = self.configured_source(data_type)
        if source in COLLECTOR_SOURCES:
            raise ValueError(f"{data_type} is served by {COLLECTOR_SOURCES[source]}, not by a data-layer provider")
        prov = self.provider(source)
        if data_type not in getattr(prov, "data_types", ()):
            raise ValueError(f"source {source!r} does not serve {data_type}")
        return prov

    # --- status --------------------------------------------------------------------------

    def record(self, data_type: str, ok: bool, message: Optional[str] = None) -> None:
        """Status of the latest use; a failing status write is logged, never raised."""
        try:
            self._db().data_source_status_update(data_type, bool(ok), message, at=iso(self._now()))
        except Exception as ex:  # noqa: BLE001 — status is bookkeeping; the call's own result stands
            logger.warning("data source status for %s not recorded: %s", data_type, ex)

    def fetch(self, data_type: str, op: str, *args, **kwargs):
        source = self.configured_source(data_type)
        prov = self.get_provider(data_type)
        try:
            result = getattr(prov, op)(*args, **kwargs)
        except (HistoryNotAvailable, InvalidRequest):
            raise                       # the caller's request, not a source failure: status unchanged
        except ValueError as ex:
            # the provider converts unexpected source data to DataSourceUnavailable itself; a
            # ValueError here is a bad caller argument (symbol, timeframe, time) -> 400-style
            raise InvalidRequest(f"Invalid {data_type.replace('_', ' ')} request: {str(ex)[:200]}") from ex
        except UserFacingError as ex:
            if getattr(ex, "is_cancellation", False):
                raise                   # the caller cancelled (e.g. during a gate wait): not a source failure
            self.record(data_type, False, ex.user_message)
            raise
        except Exception as ex:  # noqa: BLE001 — becomes a clear user-facing error
            err = DataSourceUnavailable(source, data_type, f"unexpected error ({type(ex).__name__})")
            from . import network
            if network.is_connection_error(ex):
                network.mark_connection(err)          # SPEC §10.1: kind "connection"
            logger.warning("%s %s.%s failed: %r", source, data_type, op, ex)
            self.record(data_type, False, err.user_message)
            raise err from ex
        self.record(data_type, True, None)
        return result

    def sources(self) -> List[dict]:
        """For GET /v2/data/sources."""
        out = []
        for dt in DEFAULT_SOURCES:
            source = self.configured_source(dt)
            row = self._db().data_source_get(dt) or {}
            out.append({"data_type": dt, "source": source, "status": row.get("status"),
                        "last_ok_at": row.get("last_ok_at"), "last_error_at": row.get("last_error_at"),
                        "message": row.get("message"),
                        "licence": {"terms": licence_for(source)["licence"], "note": licence_for(source)["note"]}})
        return out

    def ensure_configured(self) -> List[dict]:
        """Seeds every data type's source row (with licence) — e.g. at startup,
        so GET /v2/data/sources lists all types before their first use."""
        return [{"data_type": dt, "source": self.configured_source(dt)} for dt in DEFAULT_SOURCES]

    # --- connection test ---------------------------------------------------------------------

    def test_source(self, source: str = "alpaca") -> dict:
        testable = sorted(s for s in self._factories if s in TESTABLE_SOURCES)
        if source not in testable:
            raise ValueError(f"unknown source {source!r} (testable: {', '.join(testable)})")
        now = self._now()
        start = now - timedelta(days=TEST_LOOKBACK_DAYS)

        def daily_bar(symbol, data_type):
            def run(prov):
                bars = prov.get_bars(symbol, "1Day", start, now)
                if not bars:
                    raise DataSourceUnavailable(source, data_type,
                                                f"no daily bar for {symbol} in the last {TEST_LOOKBACK_DAYS} days")
                b = bars[-1]
                return f"{symbol}: daily bar {b['ts'][:10]}, close {b['close']:g}"
            return run

        def latest_quote(prov):
            q = prov.latest_quote("SPY")
            return f"SPY: bid {q.get('bid')} / ask {q.get('ask')} at {str(q.get('ts'))[:19]}Z"

        plan = [("1 daily bar SPY", "equity_bars", daily_bar("SPY", "equity_bars")),
                ("1 daily bar BTC/USD", "crypto_bars", daily_bar("BTC-USD", "crypto_bars")),
                ("Latest SPY quote", "equity_quotes", latest_quote)]

        def sip_history(_prov):
            # user decision 2026-10-06: backtests need Alpaca SIP history (free when the query ends
            # >= 15 min ago) — this check shows whether the account gets it; no IEX substitute
            sip = self.provider(SIP_SOURCE)
            day = SIP_CHECK_DAY
            bars = sip.get_bars("SPY", "1Day", datetime.combine(day, datetime.min.time(), timezone.utc),
                                datetime.combine(day, datetime.max.time().replace(microsecond=0), timezone.utc))
            if not bars:
                raise DataSourceUnavailable(SIP_SOURCE, "equity_bars_history",
                                            f"no SIP daily bar for SPY on {day.isoformat()} was returned")
            # the newest allowed bar too (last completed session, close >= 15 min ago): a backtest always
            # asks for it, and that is where the 15-minute rule bites (failure of 2026-10-06)
            recent = sip.get_bars("SPY", "1Day", now - timedelta(days=TEST_LOOKBACK_DAYS), now)
            if not recent:
                raise DataSourceUnavailable(SIP_SOURCE, "equity_bars_history", "no recent SIP daily bar for SPY "
                                            f"in the last {TEST_LOOKBACK_DAYS} days was returned")
            return (f"SPY: SIP daily bar {bars[0]['ts'][:10]}, close {bars[0]['close']:g}, volume "
                    f"{bars[0]['volume']:,.0f}; newest {recent[-1]['ts'][:10]}, close {recent[-1]['close']:g}")

        if SIP_SOURCE in self._factories and source == "alpaca":
            plan.append(("SIP historical daily bars (SPY, 2016-01-04 and newest)", "equity_bars_history",
                         sip_history))
        checks: List[dict] = []
        stop: Optional[str] = None

        def check_src(data_type: str) -> str:
            return SIP_SOURCE if data_type == "equity_bars_history" else source
        try:
            prov = self.provider(source)
        except Exception as ex:  # noqa: BLE001
            msg = getattr(ex, "user_message", None) or f"provider could not be created ({type(ex).__name__})"
            return {"ok": False, "source": source, "checks": [{"name": n, "ok": False, "message": msg}
                                                              for n, _d, _f in plan]}
        for name, data_type, fn in plan:
            if stop:
                checks.append({"name": name, "ok": False, "message": f"Skipped — {stop}"})
                continue
            try:
                message = fn(prov)
            except (SecretMissing, SecretStoreUnavailable, SourceAuthRejected) as ex:
                stop = ex.user_message
                checks.append({"name": name, "ok": False, "message": ex.user_message})
                self._record_if_configured(data_type, check_src(data_type), False, ex.user_message)
            except UserFacingError as ex:
                checks.append({"name": name, "ok": False, "message": ex.user_message})
                self._record_if_configured(data_type, check_src(data_type), False, ex.user_message)
            except Exception as ex:  # noqa: BLE001
                logger.warning("data source test %s / %s failed: %r", source, name, ex)
                msg = f"unexpected error ({type(ex).__name__})"
                checks.append({"name": name, "ok": False, "message": msg})
                self._record_if_configured(data_type, check_src(data_type), False, msg)
            else:
                checks.append({"name": name, "ok": True, "message": message})
                self._record_if_configured(data_type, check_src(data_type), True, None)
        return {"ok": bool(checks) and all(c["ok"] for c in checks), "source": source, "checks": checks}

    def _record_if_configured(self, data_type: str, source: str, ok: bool, message: Optional[str]) -> None:
        try:
            if self.configured_source(data_type) != source:
                return
        except Exception as ex:  # noqa: BLE001
            logger.warning("data source status for %s not recorded: %s", data_type, ex)
            return
        self.record(data_type, ok, message)


_default: Optional[Registry] = None
_default_lock = threading.Lock()


def default() -> Registry:
    global _default
    with _default_lock:
        if _default is None:
            _default = Registry()
        return _default


def set_default(registry: Optional[Registry]) -> None:
    """Tests: install a Registry with fakes (None = back to the real one)."""
    global _default
    with _default_lock:
        _default = registry


def configured_source(data_type: str) -> str:
    return default().configured_source(data_type)


def get_provider(data_type: str):
    return default().get_provider(data_type)


def fetch(data_type: str, op: str, *args, **kwargs):
    return default().fetch(data_type, op, *args, **kwargs)


def set_source(data_type: str, source: str) -> dict:
    return default().set_source(data_type, source)


def sources() -> List[dict]:
    return default().sources()


def ensure_configured() -> List[dict]:
    return default().ensure_configured()


def test_source(source: str = "alpaca") -> dict:
    return default().test_source(source)
