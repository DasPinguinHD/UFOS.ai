"""Stage 11 Risk Lab view (SP §26.4): ``GET /v2/risk/daily-returns`` — the daily return series of the Overview's
ACTIVE paper account set, READ-ONLY, behind the backend's token check like every ``/v2`` route. Nothing here trades,
writes, or touches the client heartbeat.

The WPF Risk Lab view feeds these returns to RiskLab's one-shot CLI (``python -m risklab.app var|backtest``, RiskLab's
own venv). This backend never imports RiskLab (and RiskLab never imports it); the numbers agree by construction:

* the account set and its equity series come from the ONE Overview reader (``self._ov_risk_inputs()`` →
  ``overview_routes.risk_inputs`` → ``active_account_set``), including its refusal when the set has more than one
  account (per-account histories are never summed);
* end-of-day points, consecutive-trading-day simple returns, gaps and the latest equity come from
  ``portfolio/overview_risk.py`` (``end_of_day``, ``dated_daily_returns``, ``latest_equity``) — the same functions
  ``/v2/overview/risk`` uses, so RiskLab-convention HS VaR on ``-latest_equity * returns`` equals the Overview figure
  (pinned by ``tests/test_rlview_daily_returns.py``).

Contract ``UFOS.ai/docs/tasks/RISKLAB_VIEW_TASK.md`` §B.1. No fallback: an unreadable source is ``reason`` (safe text,
never raw exception text), never a stand-in series.

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only, research only.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Mapping

from .overview_routes import _iso, _parse, _why
from .trading_routes import _bad

logger = logging.getLogger(__name__)

RETURNS_METHOD = ("simple returns r_t = E_t / E_{t-1} − 1 between the end-of-day equity (last point of each NYSE "
                  "trading day whose regular session has closed) of CONSECUTIVE trading days; a missing day is a "
                  "skipped gap (counted, never filled); days[i] = the trading day return i ends on; latest_equity = "
                  "the newest valued equity point (HS P&L scenario = latest_equity × r_t, SP §26.2)")


class RiskRoutesMixin:
    """Handler of ``/v2/risk/daily-returns``. Reads through the Overview hooks ``_ov_now`` / ``_ov_risk_inputs``
    (``OverviewRoutesMixin``; tests replace them with fakes) — one source for both routes."""

    # GET /v2/risk/daily-returns ---------------------------------------------------------------------------------
    async def risk_daily_returns(self, query: Mapping, body, match: Mapping):
        from ..portfolio import overview_risk as orisk
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        now = self._ov_now()
        try:
            inp = await asyncio.to_thread(self._ov_risk_inputs)
        except Exception as ex:      # noqa: BLE001 — said, never guessed
            logger.warning("/v2/risk/daily-returns: inputs unreadable: %s", _why(ex))
            why = f"account state unreadable: {_why(ex)}"
            inp = {"account": None, "account_ids": [], "excluded_ids": [], "points": [], "series_reason": why}
        pts = inp.get("points") or []
        reason = inp.get("series_reason")
        if reason is None and not pts:
            reason = "no stored equity points yet"
        out = {"account": inp.get("account"), "account_ids": list(inp.get("account_ids") or []),
               "excluded_ids": list(inp.get("excluded_ids") or []), "generated_at": _iso(now), "as_of": None,
               "available": False, "reason": reason, "days": [], "returns": [], "n_daily_returns": 0, "gaps": 0,
               "first_day": None, "last_day": None, "latest_equity": None, "currency": "USD",
               "return_kind": "simple", "method": RETURNS_METHOD}
        if reason is not None:
            return 200, out
        eod = orisk.end_of_day(pts, now)
        days, rets, gaps = orisk.dated_daily_returns(eod)
        latest = orisk.latest_equity(pts)
        out.update(as_of=_iso(_parse(pts[-1].get("ts"))), days=[d.isoformat() for d in days], returns=rets,
                   n_daily_returns=len(rets), gaps=gaps, first_day=eod[0][0].isoformat() if eod else None,
                   last_day=eod[-1][0].isoformat() if eod else None, latest_equity=latest)
        if latest is None:
            out["reason"] = "no valued equity point (no finite positive equity in the stored series)"
            return 200, out
        out["available"] = True
        return 200, out


RISK_ROUTES = (
    ("GET", "/v2/risk/daily-returns", "risk_daily_returns", False),
)
