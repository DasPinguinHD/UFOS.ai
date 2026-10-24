"""strategy_platform/strategy/features — external features (SPEC §7.8):
registry, FRED/ALFRED client (fake transport, gate, redaction, errors),
macro.regime_score (point in time, mirrors strategy/features/macro_agent.py, store
coverage) and calendar.event_risk. Temp hedgefund.db, fake Credential
Manager, no network.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_features -v
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from strategy_platform import secrets
from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing
from strategy_platform.data import http_gate
from strategy_platform.storage import db
from strategy_platform.strategy import features
from strategy_platform.strategy.features import event_risk, fred, macro_regime

KEY = "fredKEY-0123456789"


def month_iter(start: date, n: int):
    y, m = start.year, start.month
    for _ in range(n):
        yield date(y, m, 1)
        m += 1
        if m > 12:
            y, m = y + 1, 1


class FakeFred:
    """Fake transport: series -> [{date, value, realtime_start}] (several vintages allowed)."""

    def __init__(self, series=None, releases=None):
        self.series = series or {}
        self.releases = releases or {}
        self.calls = []
        self.fail = None          # (status, message) or an exception

    def __call__(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        if isinstance(self.fail, Exception):
            raise self.fail
        if self.fail:
            status, msg = self.fail
            return status, json.dumps({"error_code": status, "error_message": msg}).encode(), {}
        if url.endswith("series/observations"):
            sid = params["series_id"] + (":" + params["units"] if "units" in params else "")
            a, b = params["observation_start"], params["observation_end"]
            obs = [{"date": o["date"], "value": o["value"], "realtime_start": o["realtime_start"],
                    "realtime_end": "9999-12-31"} for o in self.series.get(sid, []) if a <= o["date"] <= b]
            return 200, json.dumps({"observations": obs}).encode(), {}
        if url.endswith("release/dates"):
            rid = int(params["release_id"])
            rows = [{"release_id": rid, "date": d} for d in self.releases.get(rid, [])]
            return 200, json.dumps({"release_dates": rows}).encode(), {}
        return 404, b"{}", {}


def no_sleep_gate():
    return http_gate.HttpGate("fred", 6000, capacity=1000, sleep=lambda s: None)


class FeatureCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sp-feat-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        db.close()
        db.init(os.path.join(self.dir, "hedgefund.db"))
        self.addCleanup(db.close)
        secrets.set_store(secrets.MemoryStore({secrets.target("fred-api-key"): KEY}))
        self.addCleanup(secrets.set_store, None)

    def client(self, fake):
        return fred.FredClient(transport=fake, gate=no_sleep_gate())


class RegistryTest(unittest.TestCase):
    def test_catalogue_and_get(self):
        names = [c["name"] for c in features.catalogue()]
        self.assertEqual(names, ["calendar.event_risk", "macro.regime_score"])
        for c in features.catalogue():
            self.assertTrue(c["point_in_time"])
            self.assertEqual(set(c), {"name", "description", "point_in_time", "scope", "source"})
        self.assertEqual(features.get("macro.regime_score").scope, "asset_class")
        self.assertEqual(features.get("calendar.event_risk").scope, "global")
        with self.assertRaises(KeyError):
            features.get("nope")

    def test_key_for_maps_symbols_to_agent_asset_classes(self):
        f = features.get("macro.regime_score")
        self.assertEqual(f.key_for("TLT"), "govt_bonds")
        self.assertEqual(f.key_for("BTC-USD"), "crypto")
        self.assertEqual(f.key_for("XLK"), "equities_growth")

    def test_values_for_global_and_asset_class(self):
        g = features.ExtFeature("t.g", "d", load=lambda a, b: {"2026-01-02": 1.0})
        self.assertEqual(g.values_for(["SPY", "TLT"], None, None)["TLT"], {"2026-01-02": 1.0})
        a = features.ExtFeature("t.a", "d", load=lambda a, b: {"x": {"2026-01-02": 2.0}}, scope="asset_class",
                                key_for=lambda s: "x" if s == "SPY" else "y")
        self.assertEqual(a.values_for(["SPY", "TLT"], None, None), {"SPY": {"2026-01-02": 2.0}, "TLT": {}})
        with self.assertRaises(ValueError):
            features.ExtFeature("t.b", "d", load=lambda a, b: {}, scope="asset_class")


class FredClientTest(FeatureCase):
    def test_first_release_request_and_earliest_vintage(self):
        fake = FakeFred({"DGS10": [
            {"date": "2020-01-02", "value": "1.88", "realtime_start": "2020-01-03"},
            {"date": "2020-01-02", "value": "1.90", "realtime_start": "2020-02-01"},   # later vintage ignored
            {"date": "2020-01-03", "value": ".", "realtime_start": "2020-01-06"},       # no observation
        ]})
        rows = self.client(fake).first_releases("DGS10", None, date(2020, 1, 1), date(2020, 1, 31))
        self.assertEqual(rows, [{"date": "2020-01-02", "value": 1.88, "realtime_start": "2020-01-03"}])
        params = fake.calls[0][1]
        self.assertEqual((params["realtime_start"], params["realtime_end"], params["output_type"]),
                         ("1776-07-04", "9999-12-31", "4"))
        self.assertEqual(params["api_key"], KEY)

    def test_daily_requests_are_split_into_windows(self):
        fake = FakeFred({})
        self.client(fake).first_releases("DGS10", None, date(2016, 1, 1), date(2019, 12, 31), "daily")
        self.assertEqual(len(fake.calls), 4)
        fake2 = FakeFred({})
        self.client(fake2).first_releases("FEDFUNDS", None, date(2016, 1, 1), date(2019, 12, 31), "monthly")
        self.assertEqual(len(fake2.calls), 1)

    def test_missing_key_stops_before_any_request(self):
        secrets.set_store(secrets.MemoryStore({}))
        fake = FakeFred({})
        with self.assertRaises(SecretMissing):
            self.client(fake).first_releases("DGS10", None, date(2020, 1, 1), date(2020, 1, 2))
        self.assertEqual(fake.calls, [])

    def test_rejected_key_message(self):
        fake = FakeFred({})
        fake.fail = (400, "Bad Request.  The value for variable api_key is not registered.")
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.client(fake).first_releases("DGS10", None, date(2020, 1, 1), date(2020, 1, 2))
        self.assertIn("rejected the API key", cm.exception.user_message)
        self.assertEqual(len(fake.calls), 1)

    def test_server_errors_are_retried_then_fail(self):
        fake = FakeFred({})
        fake.fail = (503, "unavailable")
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.client(fake).first_releases("DGS10", None, date(2020, 1, 1), date(2020, 1, 2))
        self.assertEqual(len(fake.calls), 3)
        self.assertIn("gave up after 3 attempts", cm.exception.user_message)

    def test_network_error_text_never_contains_the_key(self):
        fake = FakeFred({})
        fake.fail = OSError(f"cannot connect to https://api.stlouisfed.org/fred/x?api_key={KEY}&file_type=json")
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.client(fake).first_releases("DGS10", None, date(2020, 1, 1), date(2020, 1, 2))
        self.assertNotIn(KEY, str(cm.exception))
        self.assertNotIn(KEY, cm.exception.user_message)

    def test_malformed_payload_is_an_error_not_skipped(self):
        def transport(url, params, timeout):
            return 200, json.dumps({"observations": [{"date": "2020-01-02", "value": "abc",
                                                      "realtime_start": "2020-01-03"}]}).encode(), {}
        with self.assertRaises(DataSourceUnavailable):
            fred.FredClient(transport, no_sleep_gate()).first_releases("DGS10", None, date(2020, 1, 1),
                                                                       date(2020, 1, 5))


def synthetic_series(start=date(2014, 1, 1), months=96, lag_days=40, dgs10=4.0, sahm=0.1):
    """First releases for every macro input. Monthly values published lag_days after the month."""
    out = {}
    for sid in ("PCEPILFE:pc1", "PAYEMS:pc1", "INDPRO:pc1", "FEDFUNDS", "SAHMREALTIME"):
        rows = []
        for i, m in enumerate(month_iter(start, months)):
            if sid == "PCEPILFE:pc1":
                v = 2.0 + 0.5 * ((i // 9) % 2) + 0.01 * i
            elif sid == "FEDFUNDS":
                v = 2.0
            elif sid == "SAHMREALTIME":
                v = sahm
            else:
                v = 1.5 + (0.03 * i if sid == "PAYEMS:pc1" else -0.01 * i)
            rows.append({"date": m.isoformat(), "value": str(round(v, 4)),
                         "realtime_start": (m + timedelta(days=31 + lag_days)).isoformat()})
        out[sid] = rows
    for sid, val in (("DGS10", dgs10), ("T10Y3M", 0.5), ("VIXCLS", 15.0), ("BAMLH0A0HYM2", 3.0)):
        rows = []
        d = start
        while d < start + timedelta(days=months * 31):
            if d.weekday() < 5:
                rows.append({"date": d.isoformat(), "value": str(val), "realtime_start": (d + timedelta(days=1)).isoformat()})
            d += timedelta(days=1)
        out[sid] = rows
    return out


def as_obs(series):
    return {sid: [{"date": r["date"], "value": float(r["value"]), "realtime_start": r["realtime_start"]}
                  for r in rows] for sid, rows in series.items()}


class MacroRegimeTest(FeatureCase):
    def test_values_exist_for_every_class_and_day(self):
        out = macro_regime.compute(as_obs(synthetic_series()), date(2019, 1, 1), date(2019, 1, 31))
        self.assertIn("govt_bonds", out)
        self.assertIn("crypto", out)
        for cls, series in out.items():
            self.assertEqual(len(series), 31, cls)
            for v in series.values():
                self.assertTrue(-1.0 <= v <= 1.0)

    def test_no_value_before_any_input_is_known(self):
        out = macro_regime.compute(as_obs(synthetic_series()), date(2013, 6, 1), date(2013, 6, 5))
        self.assertTrue(all(series == {} for series in out.values()))

    def test_point_in_time_future_releases_do_not_change_the_past(self):
        obs = as_obs(synthetic_series())
        start, end = date(2018, 1, 1), date(2018, 12, 31)
        full = macro_regime.compute(obs, start, end)
        for cut in ("2018-03-15", "2018-07-02", "2018-11-20"):
            known = {sid: [o for o in rows if o["realtime_start"] < cut] for sid, rows in obs.items()}
            truncated = macro_regime.compute(known, start, date.fromisoformat(cut))
            for cls in full:
                for d, v in truncated[cls].items():
                    self.assertAlmostEqual(full[cls][d], v, places=12, msg=f"{cls} {d}")

    def test_release_enters_the_next_day(self):
        obs = as_obs(synthetic_series(dgs10=4.0))
        # one high 10Y print for 2019-03-04 published 2019-03-05 -> affects 2019-03-06 onwards
        obs["DGS10"] = [o for o in obs["DGS10"] if o["date"] < "2019-03-04"] + [
            {"date": "2019-03-04", "value": 5.5, "realtime_start": "2019-03-05"}]
        out = macro_regime.compute(obs, date(2019, 3, 4), date(2019, 3, 7))["equities"]
        self.assertEqual(out["2019-03-04"], out["2019-03-05"])
        self.assertAlmostEqual(out["2019-03-06"], out["2019-03-05"] - 0.2, places=6)

    def test_mirrors_the_macro_agent(self):
        from strategy_platform.strategy.features import macro_agent as macro
        from newsroom import macro_derived
        obs = as_obs(synthetic_series())
        d = "2019-06-10"
        out = macro_regime.compute(obs, date(2019, 6, 10), date(2019, 6, 10))
        known = {sid: {o["date"]: o["value"] for o in rows if o["realtime_start"] < d} for sid, rows in obs.items()}
        pts = lambda sid: [{"date": k, "value": known[sid][k]} for k in sorted(known[sid])][-48:]
        reg = macro_derived.regime({"PCEPILFE": pts("PCEPILFE:pc1")},
                                   {"PAYEMS": pts("PAYEMS:pc1"), "INDPRO": pts("INDPRO:pc1")})
        self.assertIsNotNone(reg)
        latest = lambda sid: max(known[sid].items())
        readings = {k: {"value": latest(s)[1], "date": latest(s)[0], "monthly": m, "unit": ""}
                    for k, s, m in (("DGS10", "DGS10", False), ("T10Y3M", "T10Y3M", False), ("VIXCLS", "VIXCLS", False),
                                    ("BAMLH0A0HYM2", "BAMLH0A0HYM2", False), ("SAHM_RULE", "SAHMREALTIME", True),
                                    ("FEDFUNDS", "FEDFUNDS", True), ("PCEPILFE", "PCEPILFE:pc1", True))}
        ctx = SimpleNamespace(now=d + "T00:00:00+00:00", macro={
            "available": True, "as_of": None, "readings": readings,
            "regime": {"quadrant": reg["quadrant"], "label": reg["label"], "date": reg["date"],
                       "borderline": reg["borderline"], "months_in_regime": reg["months_in_regime"]}})
        for sym, cls in (("SPY", "equities"), ("TLT", "govt_bonds"), ("BTC-USD", "crypto"), ("XLE", "equities_cyclical")):
            self.assertAlmostEqual(out[cls][d], macro.evaluate(ctx, sym).score, places=6, msg=cls)

    def test_load_stores_first_releases_and_reuses_complete_coverage(self):
        fake = FakeFred(synthetic_series())
        client = self.client(fake)
        now = datetime(2019, 7, 1, 12, tzinfo=timezone.utc)
        out = macro_regime.load("2019-01-01", "2019-01-31", client=client, now=now)
        self.assertTrue(out["equities"])
        first_calls = len(fake.calls)
        self.assertGreater(first_calls, 0)
        self.assertTrue(db.query_ext_observations("PAYEMS:pc1", "fred"))
        # end before the fetch date and start inside the coverage -> served from hedgefund.db
        again = macro_regime.load("2019-02-01", "2019-03-31", client=client, now=now)
        self.assertEqual(len(fake.calls), first_calls)
        self.assertTrue(again["equities"])
        # an end on/after the last fetch -> fetched again (newer releases may exist)
        macro_regime.load("2019-06-01", "2019-07-01", client=client, now=now)
        self.assertGreater(len(fake.calls), first_calls)

    def test_load_failure_raises_no_stand_in(self):
        fake = FakeFred({})
        fake.fail = (500, "boom")
        with self.assertRaises(DataSourceUnavailable):
            macro_regime.load("2019-01-01", "2019-01-31", client=self.client(fake))


class EventRiskTest(FeatureCase):
    RELEASES = {10: ["2025-12-10", "2026-01-13", "2026-02-11", "2026-03-11"],
                50: ["2025-12-05", "2026-01-09", "2026-02-06", "2026-03-06"],
                53: ["2025-12-23", "2026-01-29", "2026-02-26", "2026-03-26"],
                101: ["2025-12-10", "2026-01-28", "2026-03-18"]}

    def test_flags_day_before_and_day_of(self):
        out = event_risk.load("2026-01-05", "2026-01-31", client=self.client(FakeFred(releases=self.RELEASES)))
        self.assertEqual(out["2026-01-13"], 1.0)      # CPI
        self.assertEqual(out["2026-01-12"], 1.0)      # trading day before
        self.assertEqual(out["2026-01-09"], 1.0)      # jobs report (Friday)
        self.assertEqual(out["2026-01-08"], 1.0)
        self.assertEqual(out["2026-01-14"], 0.0)
        self.assertEqual(out["2026-01-10"], 0.0)      # Saturday
        self.assertEqual(out["2026-01-27"], 0.0)      # NO day-before flag for FOMC (can be unscheduled)
        self.assertEqual(out["2026-01-28"], 1.0)      # FOMC statement day
        self.assertEqual(len(out), 27)
        self.assertTrue(db.query_ext_observations("release:101", "fred"))

    def test_day_before_skips_holidays_and_weekends(self):
        rel = dict(self.RELEASES)
        rel[10] = ["2025-12-10", "2026-01-20", "2026-03-11"]       # Tuesday after MLK day (Mon 2026-01-19)
        out = event_risk.load("2026-01-14", "2026-01-21", client=self.client(FakeFred(releases=rel)))
        self.assertEqual(out["2026-01-16"], 1.0)      # Friday = previous trading day
        self.assertEqual(out["2026-01-19"], 0.0)

    def test_unscheduled_fomc_actions_never_flag_the_day_before(self):
        # regression (review 2026-10-03): 2020-03-03 (Tue) and 2020-03-15 (Sun) were unscheduled cuts
        rel = {10: ["2020-02-13", "2020-03-11", "2020-04-10"], 50: ["2020-02-07", "2020-03-06", "2020-04-03"],
               53: ["2020-02-27", "2020-03-26", "2020-04-29"],
               101: ["2020-01-29", "2020-03-03", "2020-03-15", "2020-04-29"]}
        out = event_risk.load("2020-03-01", "2020-03-20", client=self.client(FakeFred(releases=rel)))
        self.assertEqual(out["2020-03-02"], 0.0)      # Monday before the surprise cut: unknown then
        self.assertEqual(out["2020-03-03"], 1.0)      # known by that day's close
        self.assertEqual(out["2020-03-13"], 0.0)      # Friday before the Sunday action
        self.assertEqual(out["2020-03-15"], 0.0)      # Sunday itself
        self.assertEqual(out["2020-03-16"], 1.0)      # next trading day
        self.assertEqual(out["2020-03-05"], 1.0)      # day before the (pre-scheduled) jobs report
        self.assertEqual(out["2020-03-10"], 1.0)      # day before CPI

    def test_schedule_not_covering_the_range_fails(self):
        rel = dict(self.RELEASES)
        rel[101] = ["2025-12-10", "2026-01-28"]
        with self.assertRaises(DataSourceUnavailable) as cm:
            event_risk.load("2026-01-05", "2026-02-28", client=self.client(FakeFred(releases=rel)))
        self.assertIn("FOMC Press Release", cm.exception.user_message)
        with self.assertRaises(DataSourceUnavailable):
            event_risk.load("2025-11-01", "2026-01-31", client=self.client(FakeFred(releases=self.RELEASES)))


class EvaluatorIntegrationTest(FeatureCase):
    """B1's evaluate.load_ext/align_ext consume values_for() of the real features (fake FRED)."""

    def test_align_with_panel(self):
        from strategy_platform.strategy import evaluate
        fake = FakeFred(synthetic_series(start=date(2014, 1, 1), months=96), EventRiskTest.RELEASES)
        fred.set_default_client(self.client(fake))
        self.addCleanup(fred.set_default_client, None)
        panel = SimpleNamespace(dates=[date(2019, 3, 4), date(2019, 3, 5), date(2019, 3, 6)],
                                symbols=["SPY", "TLT", "BTC-USD"], shape=(3, 3))
        vals = evaluate.load_ext("macro.regime_score", panel)
        arr = evaluate.align_ext(vals, panel)
        self.assertEqual(arr.shape, (3, 3))
        self.assertTrue((abs(arr) <= 1).all())
        panel2 = SimpleNamespace(dates=[date(2026, 1, 12), date(2026, 1, 14)], symbols=["SPY"], shape=(2, 1))
        with mock.patch.object(evaluate, "EXT_HISTORY_DAYS", 0):
            arr2 = evaluate.align_ext(evaluate.load_ext("calendar.event_risk", panel2), panel2)
        self.assertEqual(arr2[:, 0].tolist(), [1.0, 0.0])


if __name__ == "__main__":
    unittest.main()
