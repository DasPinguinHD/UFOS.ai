"""Stage 11 Risk Lab view (SP §26.4): ``GET /v2/risk/daily-returns`` — shape, token protection, the Overview's
active-account rule incl. the refusal for > 1 account, an unreadable source, and the PARITY with ``/v2/overview/risk``:
RiskLab-convention HS VaR 99 % / ES 97.5 % on ``-latest_equity * returns`` of this route equals the Overview figures.
The RiskLab convention is computed here with the HF re-implementation (``portfolio/overview_risk.hs_var_es``) and once
more spelled out literally — RiskLab is never imported (no import in either direction). Fakes only: synthetic
equity series, temp dirs, no network, no real database."""
import asyncio
import json
import math
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_platform.api import overview_routes as ov  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.portfolio import overview_risk as R  # noqa: E402

NY = calendars.NY
UTC = timezone.utc
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)          # Thursday 10:00 New York, session open (today not closed)


def at(d, hh, mm=0):
    return datetime.combine(d, time(hh, mm), tzinfo=NY).astimezone(UTC).isoformat()


def synthetic_points(n_days=140, seed=7, drop=None, e0=100000.0):
    """Synthetic paper equity: one close point per NYSE trading day (+ a midday point and a weekend point that must
    not change anything, + a point TODAY whose session has not closed by NOW). ``drop`` = a trading-day index left
    out (→ one gap). Deterministic (seeded); not market data."""
    rng = random.Random(seed)
    days = calendars.trading_days(date(2026, 3, 2), date(2026, 11, 4))[-n_days:]
    pts, e = [], e0
    for i, d in enumerate(days):
        if i > 0:
            e *= 1.0 + rng.gauss(0.0003, 0.012)
        if i == drop:
            continue
        pts.append({"ts": at(d, 12), "equity": e * 0.999})
        pts.append({"ts": at(d, 16, 5), "equity": e})
        if d.weekday() == 4:
            pts.append({"ts": at(d + timedelta(days=1), 12), "equity": e * 1.5})
    pts.append({"ts": at(date(2026, 11, 5), 9, 45), "equity": e * 1.01})     # today: session still open
    return pts, days


def riskl_hs(losses, alpha):
    """RiskLab ``hs_var_es`` convention spelled out: VaR = L_(k), k = ceil(round(n·α, 9)) (1-based, ascending, no
    interpolation); ES = mean of the losses ≥ VaR."""
    xs = sorted(losses)
    k = int(math.ceil(round(len(xs) * alpha, 9)))
    v = xs[k - 1]
    tail = [x for x in xs if x >= v]
    return v, sum(tail) / len(tail)


class Api(unittest.TestCase):
    def setUp(self):
        from strategy_platform.api import routes
        self.routes = routes
        self.api = routes.V2Api()
        self.tmp = tempfile.mkdtemp(prefix="rlview-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.api.data_dir = self.tmp
        self.api.now_fn = lambda: NOW

    def call(self, name, query=None):
        status, payload = asyncio.run(getattr(self.api, name)(query or {}, self.routes.NO_BODY, {}))
        json.loads(self.routes.dumps(payload))
        return status, payload

    def legacy(self, pts):
        self.api._ov_risk_inputs = lambda: {"account": "legacy", "account_ids": ["old-engine"], "excluded_ids": [],
                                            "points": pts, "equity": 1.0, "long_mv": 0.0, "short_mv": 0.0,
                                            "exposure_reason": None, "series_reason": None}


class RouteTest(Api):
    def test_registered_get_only_and_token_protected(self):
        got = {p: (m, b) for m, p, _n, b in self.routes.ROUTES if p.startswith("/v2/risk/")}
        self.assertEqual(got, {"/v2/risk/daily-returns": ("GET", False)})
        import api_auth
        self.assertNotIn("/v2/risk/daily-returns", api_auth.PUBLIC_PATHS)
        self.assertEqual(self.call("risk_daily_returns", {"window": "250"})[0], 400)

    def test_shape_days_returns_gaps(self):
        pts, days = synthetic_points(n_days=30, drop=10)
        self.legacy(pts)
        st, p = self.call("risk_daily_returns")
        self.assertEqual(st, 200)
        self.assertEqual(set(p), {"account", "account_ids", "excluded_ids", "generated_at", "as_of", "available",
                                  "reason", "days", "returns", "n_daily_returns", "gaps", "first_day", "last_day",
                                  "latest_equity", "currency", "return_kind", "method"})
        self.assertTrue(p["available"])
        self.assertIsNone(p["reason"])
        self.assertEqual((p["account"], p["account_ids"]), ("legacy", ["old-engine"]))
        # 30 days, one dropped → 29 closes → 28 pairs, the two around the hole are not consecutive → 27 returns, 1 gap
        self.assertEqual((p["n_daily_returns"], p["gaps"], len(p["days"])), (27, 1, 27))
        self.assertNotIn(days[10].isoformat(), p["days"])
        self.assertNotIn(days[11].isoformat(), p["days"])      # 11 vs 9 is a gap, not a return
        self.assertEqual((p["first_day"], p["last_day"]), (days[0].isoformat(), days[-1].isoformat()))
        self.assertNotIn("2026-11-05", p["days"])               # today's session has not closed by NOW
        closes = {datetime.fromisoformat(x["ts"]).astimezone(NY).date(): x["equity"] for x in pts
                  if x["ts"].endswith("21:05:00+00:00") or x["ts"].endswith("20:05:00+00:00")}
        d1 = days[1]
        self.assertAlmostEqual(p["returns"][0], closes[d1] / closes[days[0]] - 1.0, places=15)
        self.assertEqual(p["latest_equity"], pts[-1]["equity"])            # newest valued point (as the Overview)
        self.assertEqual(p["as_of"], ov._iso(ov._parse(pts[-1]["ts"])))
        self.assertEqual((p["currency"], p["return_kind"]), ("USD", "simple"))

    def test_parity_with_overview_risk(self):
        pts, _days = synthetic_points(n_days=140, drop=70)
        self.legacy(pts)
        _st, dr = self.call("risk_daily_returns")
        _st, ovr = self.call("overview_risk")
        self.assertTrue(dr["available"])
        self.assertEqual(dr["n_daily_returns"], ovr["n_daily_returns"])
        self.assertGreaterEqual(dr["n_daily_returns"], 100)
        self.assertEqual(dr["gaps"], ovr["details"]["gaps_skipped"])
        self.assertEqual(dr["latest_equity"], ovr["details"]["latest_equity"])
        self.assertEqual((dr["first_day"], dr["last_day"]), (ovr["details"]["first_day"], ovr["details"]["last_day"]))
        losses = [-dr["latest_equity"] * r for r in dr["returns"]]
        for key, alpha, idx in (("var99_1d", 0.99, 0), ("es975_1d", 0.975, 1)):
            fig = ovr["figures"][key]
            self.assertTrue(fig["available"], fig["reason"])
            self.assertEqual(R.hs_var_es(losses, alpha)[idx], fig["value"])
            self.assertEqual(riskl_hs(losses, alpha)[idx], fig["value"])

    def test_empty_and_unvalued_series(self):
        self.legacy([])
        p = self.call("risk_daily_returns")[1]
        self.assertEqual((p["available"], p["reason"], p["returns"]), (False, "no stored equity points yet", []))
        self.legacy([{"ts": at(date(2026, 11, 3), 16, 5), "equity": 0}, {"ts": at(date(2026, 11, 4), 16, 5),
                                                                         "equity": float("nan")}])
        p = self.call("risk_daily_returns")[1]
        self.assertFalse(p["available"])
        self.assertIn("no valued equity point", p["reason"])
        self.assertIsNone(p["latest_equity"])

    def test_series_reason_passed_through(self):
        self.api._ov_risk_inputs = lambda: {"account": "legacy", "account_ids": ["old-engine"], "points": [],
                                            "series_reason": "the old engine is not running in this backend"}
        p = self.call("risk_daily_returns")[1]
        self.assertEqual((p["available"], p["reason"], p["days"], p["latest_equity"]),
                         (False, "the old engine is not running in this backend", [], None))

    def test_inputs_fail_safe_reason(self):
        def boom():
            raise RuntimeError("disk I/O error at C:\\secret\\path")
        self.api._ov_risk_inputs = boom
        st, p = self.call("risk_daily_returns")
        self.assertEqual(st, 200)
        self.assertFalse(p["available"])
        self.assertEqual(p["reason"], "account state unreadable: RuntimeError (details in backend.log)")
        self.assertNotIn("secret", json.dumps(p))

    def test_active_set_rule_and_multi_account_refusal(self):
        """Through the REAL Overview reader (``risk_inputs`` → ``active_account_set``): platform mode, broker mirrors
        excluded; two internal accounts → refused (never summed); one → its own series."""
        from strategy_platform.core.types import AccountView
        from strategy_platform.execution import accounts, control, migration
        from strategy_platform.portfolio import views
        pts, _days = synthetic_points(n_days=12)

        class Store:
            def get_reset_at(self):
                return None

            def equity_points(self, since=None, limit=0):
                return pts

        class Acc:
            def __init__(self, aid, broker="internal_paper"):
                self.id, self.broker_name = aid, broker
                self.store, self.broker = Store(), self

            def account(self, pm):
                return AccountView(equity=1000.0, cash=0, long_mv=0.0, short_mv=0.0, buying_power=0, peak_equity=0,
                                   drawdown_pct=0, maintenance_required=0, margin_excess=0, short_exposure_pct=0,
                                   starting_capital=0, day_pnl=0.0, total_pnl=0.0)

        class Mirror(Acc):
            is_broker = True

        with mock.patch.object(control, "legacy_engine", lambda: None), \
                mock.patch.object(migration, "legacy_state", lambda e=None: {"status": "migrated"}), \
                mock.patch.object(views, "quotes_for", lambda a: {}):
            with mock.patch.object(accounts, "all_accounts",
                                   lambda: [Acc("paper-main"), Acc("paper-council"), Mirror("alpaca-paper", "alpaca_paper")]):
                p = self.call("risk_daily_returns")[1]
            self.assertEqual((p["account"], p["account_ids"], p["excluded_ids"]),
                             ("platform", ["paper-main", "paper-council"], ["alpaca-paper"]))
            self.assertFalse(p["available"])
            self.assertTrue(p["reason"].startswith("no platform equity history yet"))
            self.assertIn("not summed", p["reason"])
            self.assertEqual((p["returns"], p["days"], p["latest_equity"]), ([], [], None))
            with mock.patch.object(accounts, "all_accounts", lambda: [Acc("paper-main"), Mirror("alpaca-paper", "alpaca_paper")]):
                p = self.call("risk_daily_returns")[1]
            self.assertEqual((p["account_ids"], p["available"], p["n_daily_returns"]), (["paper-main"], True, 11))

    def test_dated_returns_single_implementation(self):
        pts, _days = synthetic_points(n_days=25, drop=5)
        eod = R.end_of_day(pts, NOW)
        days, rets, gaps = R.dated_daily_returns(eod)
        self.assertEqual(R.daily_returns(eod), (rets, gaps))
        self.assertEqual(len(days), len(rets))
        self.assertTrue(all(calendars.previous_trading_day(d) in {e[0] for e in eod} for d in days))


class _StubWeb:
    """Just enough of aiohttp.web for ``api_auth.http_middleware`` (aiohttp is not needed to run the tests)."""
    class HTTPException(Exception):
        headers = {}

    class Response:
        def __init__(self, text="", status=200, headers=None):
            self.text, self.status, self.headers = text, status, dict(headers or {})

    @staticmethod
    def middleware(fn):
        return fn

    @classmethod
    def json_response(cls, data, status=200, headers=None):
        return cls.Response(json.dumps(data), status, headers)


class _Transport:
    def get_extra_info(self, key):
        return ("127.0.0.1", 8867) if key == "sockname" else None


class _Req:
    def __init__(self, headers):
        self.method, self.path, self.path_qs, self.remote = "GET", "/v2/risk/daily-returns", "/v2/risk/daily-returns", "127.0.0.1"
        self.headers, self.transport, self.query, self.match_info = dict(headers), _Transport(), {}, {}


class TokenMiddlewareTest(Api):
    """Review MINOR 7: through the REAL ``api_auth.http_middleware`` a request without (or with a wrong) token gets 401
    and never reaches the handler; with the token it does."""

    def test_401_without_token(self):
        import types
        import api_auth
        token = api_auth.init_token()
        self.legacy(synthetic_points(n_days=10)[0])
        calls = []

        async def handler(request):
            calls.append(request)
            st, payload = await self.api.risk_daily_returns(request.query, self.routes.NO_BODY, request.match_info)
            return _StubWeb.json_response(payload, status=st)
        saved = {k: sys.modules.get(k) for k in ("aiohttp", "aiohttp.web")}
        pkg = types.ModuleType("aiohttp")
        pkg.web = _StubWeb
        sys.modules["aiohttp"], sys.modules["aiohttp.web"] = pkg, _StubWeb
        try:
            mw = api_auth.http_middleware(frozenset())
            host = {"Host": "127.0.0.1:8867"}
            r = asyncio.run(mw(_Req(host), handler))
            self.assertEqual((r.status, json.loads(r.text)), (401, {"error": "missing or invalid API token"}))
            r = asyncio.run(mw(_Req(dict(host, **{api_auth.TOKEN_HEADER: "wrong"})), handler))
            self.assertEqual(r.status, 401)
            self.assertEqual(calls, [])
            r = asyncio.run(mw(_Req(dict(host, **{api_auth.TOKEN_HEADER: token})), handler))
            self.assertEqual(r.status, 200)
            self.assertTrue(json.loads(r.text)["available"])
            self.assertEqual(len(calls), 1)
        finally:
            for k, m in saved.items():
                if m is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = m


class NoRiskLabImport(unittest.TestCase):
    def test_route_module_does_not_import_risklab(self):
        import strategy_platform.api.risk_routes as rr
        with open(rr.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotRegex(src, r"(?m)^\s*(from|import)\s+risklab")


if __name__ == "__main__":
    unittest.main()
