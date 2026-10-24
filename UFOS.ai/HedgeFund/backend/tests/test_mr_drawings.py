"""Market Research MR5 — drawings (SPEC §10 MR5, §13 "MR5 backend notes"): validation per tool, limits,
CRUD + lock rules through the routes, export/import round trip and rejection of bad files, chart settings.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_drawings -v
"""
import asyncio
import json
import logging
import unittest
from unittest import mock

try:
    from mr_data_testkit import FakeSources, TempStore, utc
    from test_mr_data_api import Q
except ImportError:  # pragma: no cover - run as package
    from tests.mr_data_testkit import FakeSources, TempStore, utc
    from tests.test_mr_data_api import Q

from market_research import ResearchService, api as mr_api, drawings as dr, chart_settings as cs, limits, store
from market_research.data import DataService
from market_research.supervisor import ResearchSupervisor
from strategy_platform.api.routes import NO_BODY, dumps

NOW = utc(2026, 10, 15, 15, 0)


def setUpModule():
    logging.getLogger("market_research").setLevel(logging.CRITICAL + 1)


def tearDownModule():
    logging.getLogger("market_research").setLevel(logging.NOTSET)


def run(c):
    return asyncio.run(c)


def pts(n, p=100.0):
    return [{"t": f"2026-10-{10 + (i % 5):02d}T14:{i % 60:02d}:00+00:00", "p": p + i} for i in range(n)]


def body(tool="segment", n=None, **kw):
    n = n if n is not None else dr.TOOLS.get(tool, (2,))[0]
    b = {"symbol": "AAPL", "tool": tool, "points": pts(n)}
    b.update(kw)
    return b


class Case(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()
        self.fs = FakeSources(lambda: NOW)
        self.svc = ResearchService(supervisor=ResearchSupervisor(), data=DataService(sources=self.fs, now=lambda: NOW))
        self.api = mr_api.ResearchApi(self.svc)
        p = mock.patch.object(dr, "_now", lambda now=None: NOW)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def call(self, name, query=(), b=NO_BODY, match=None):
        status, payload = run(getattr(self.api, name)(Q(query), b, match or {}))
        dumps(payload)                                    # strict JSON
        return status, payload

    def create(self, **kw):
        status, d = self.call("drawing_create", b=body(**kw))
        self.assertEqual(status, 201, d)
        return d


class ValidationTest(Case):
    def test_every_tool_point_counts(self):
        for tool, (lo, hi) in dr.TOOLS.items():
            with self.subTest(tool=tool):
                kw = {"text": "note"} if tool == "simpleAnnotation" else {}
                d = self.create(tool=tool, n=lo, **kw)
                self.assertEqual(len(d["points"]), lo)
                self.assertEqual(set(d), {"id", "symbol", "tool", "points", "style", "name", "text", "locked",
                                          "visible", "timeframes", "alert_id", "price_basis", "created_at",
                                          "updated_at", "client_id"})
                for bad_n in ({lo - 1, hi + 1} if tool != "brush" else {lo - 1}):   # brush > 500: size test
                    status, p = self.call("drawing_create", b=body(tool=tool, n=bad_n, **kw))
                    self.assertEqual(status, 400, (tool, bad_n))
                    self.assertIn("points", p["errors"])

    def test_defaults_and_normalisation(self):
        d = self.create(points=[{"t": "2026-10-15T10:30:00-04:00", "p": 10}, {"t": "2026-10-15T15:00:00", "p": 11.5}])
        self.assertEqual(d["points"], [{"t": "2026-10-15T14:30:00+00:00", "p": 10.0},
                                       {"t": "2026-10-15T15:00:00+00:00", "p": 11.5}])
        self.assertEqual(d["style"], {"color": "accent", "width": 1, "dash": "solid", "levels": None})
        self.assertEqual((d["locked"], d["visible"], d["timeframes"], d["alert_id"], d["text"], d["name"]),
                         (False, True, "all", None, None, None))
        self.assertEqual(d["price_basis"], {"as_of": "2026-10-15", "factor": 1.0, "rescales": 0})
        self.assertRegex(d["id"], r"^[0-9a-f]{32}$")
        c = self.create(**{"symbol": "btc-usd"})
        self.assertEqual((c["symbol"], c["price_basis"]["as_of"]), ("BTC-USD", None))

    def test_rejections(self):
        cases = [
            ({"extra": 1}, "extra"), ({"tool": "circle"}, "tool"), ({"symbol": "not a symbol!"}, "symbol"),
            ({"points": [{"t": "2026-10-15T10:00:00Z", "p": 0}, {"t": "2026-10-15T10:00:00Z", "p": 1}]}, "points[0].p"),
            ({"points": [{"t": "2026-10-15T10:00:00Z", "p": float("inf")}, {"t": "x", "p": 1}]}, "points[0].p"),
            ({"points": [{"t": "yesterday", "p": 1}, {"t": "2026-10-15T10:00:00Z", "p": 1}]}, "points[0].t"),
            ({"points": [{"t": "1980-01-01T00:00:00Z", "p": 1}, {"t": "2026-10-15T10:00:00Z", "p": 1}]}, "points[0].t"),
            ({"points": [{"t": "2026-10-15T10:00:00Z", "p": True}, {"t": "2026-10-15T10:00:00Z", "p": 1}]}, "points[0].p"),
            ({"points": [{"t": "2026-10-15T10:00:00Z", "p": 1, "x": 2}, {"t": "2026-10-15T10:00:00Z", "p": 1}]}, "points[0]"),
            ({"points": [{"t": "2026-10-15T10:00:00Z", "p": 2e9}, {"t": "2026-10-15T10:00:00Z", "p": 1}]}, "points[0].p"),
            ({"style": {"color": "#ff0000"}}, "style.color"), ({"style": {"width": 5}}, "style.width"),
            ({"style": {"width": True}}, "style.width"), ({"style": {"dash": "dotted"}}, "style.dash"),
            ({"style": {"glow": 1}}, "style"), ({"style": {"levels": [0, 50]}}, "style.levels"),
            ({"text": "a\x00b"}, "text"), ({"text": "x" * 281}, "text"), ({"name": "a\nb"}, "name"),
            ({"name": "n" * 41}, "name"), ({"locked": "yes"}, "locked"), ({"timeframes": []}, "timeframes"),
            ({"timeframes": ["1m", "1m"]}, "timeframes"), ({"timeframes": ["2m"]}, "timeframes"),
            ({"alert_id": "0" * 32}, "alert_id"), ({"alert_id": None}, "alert_id"),
        ]
        for extra, field in cases:
            with self.subTest(field=field, extra=str(extra)[:60]):
                status, p = self.call("drawing_create", b=body(**extra))
                self.assertEqual(status, 400, p)
                self.assertIn(field, p["errors"])
        self.assertEqual(store.drawings_list("AAPL"), [])

    def test_tool_specific_fields(self):
        status, p = self.call("drawing_create", b=body(tool="simpleAnnotation"))
        self.assertEqual(status, 400)
        self.assertIn("text", p["errors"])
        d = self.create(tool="simpleAnnotation", text="Earnings gap\nfilled\tlater")
        self.assertEqual(d["text"], "Earnings gap\nfilled\tlater")
        f = self.create(tool="fibonacciLine", style={"levels": [0, 38.2, 61.8, 100], "color": "violet"})
        self.assertEqual(f["style"]["levels"], [0.0, 38.2, 61.8, 100.0])
        for lv in ([0, 0], [500], [], list(range(13))):
            status, _ = self.call("drawing_create", b=body(tool="fibonacciLine", style={"levels": lv}))
            self.assertEqual(status, 400, lv)
        a = self.create(tool="alertLine")
        self.assertIsNone(a["alert_id"])                     # set only by the alert transaction (MR7)
        status, p = self.call("drawing_create", b=body(tool="alertLine", alert_id="ab" * 16))
        self.assertEqual((status, p["errors"]["alert_id"][:29]), (400, "alert_id is managed by alerts"))
        t = self.create(timeframes=["1W", "1m", "1D"])
        self.assertEqual(t["timeframes"], ["1m", "1D", "1W"])

    def test_size_limits(self):
        status, p = self.call("drawing_create", b=body(tool="brush", n=limits.MAX_BRUSH_POINTS))
        self.assertEqual(status, 400)                       # 500 ISO points are larger than 16 KiB
        self.assertTrue(any("large" in v for v in p["errors"].values()), p)
        d = self.create(tool="brush", n=300)
        self.assertEqual(len(d["points"]), 300)
        status, _ = self.call("drawing_create", b=body(tool="brush", n=limits.MAX_BRUSH_POINTS + 1))
        self.assertEqual(status, 400)
        with mock.patch.object(limits, "MAX_DRAWINGS_PER_SYMBOL", 4):          # + the brush above
            for _ in range(3):
                self.create()
            status, p = self.call("drawing_create", b=body())
            self.assertEqual(status, 409)
            self.assertEqual(p["error"], "At most 4 drawings per symbol — delete some first.")
            self.create(symbol="MSFT")                       # per symbol
        self.assertEqual(len(store.drawings_list("AAPL")), 4)


class CrudTest(Case):
    def test_list_patch_delete(self):
        a = self.create()
        b = self.create(tool="horizontalStraightLine")
        status, lst = self.call("drawings_list", (("symbol", "aapl"),))
        self.assertEqual(status, 200)
        self.assertEqual([d["id"] for d in lst["drawings"]], [a["id"], b["id"]])
        self.assertEqual((lst["count"], lst["limit"]), (2, 500))
        self.assertEqual(lst["split_check"], {"state": "pending", "checked_at": None, "reason": None})
        _, c = self.call("drawings_list", (("symbol", "BTC-USD"),))
        self.assertEqual(c["split_check"]["state"], "not_applicable")
        for q in ((), (("symbol", "AAPL"), ("x", "1")), (("symbol", "AAPL"), ("symbol", "MSFT"))):
            self.assertEqual(self.call("drawings_list", q)[0], 400)
        status, p = self.call("drawing_patch", b={"style": {"width": 3}, "name": "Support"}, match={"id": a["id"]})
        self.assertEqual(status, 200, p)
        self.assertEqual(p["style"], {"color": "accent", "width": 3, "dash": "solid", "levels": None})
        self.assertEqual(p["name"], "Support")
        for bad in ({}, {"tool": "rayLine"}, {"symbol": "MSFT"}, {"points": pts(3)}, {"price_basis": {}},
                    {"style": None}, {"visible": 1}):
            self.assertEqual(self.call("drawing_patch", b=bad, match={"id": a["id"]})[0], 400, bad)
        self.assertEqual(self.call("drawing_patch", b={"visible": False}, match={"id": "f" * 32})[0], 404)
        self.assertEqual(self.call("drawing_patch", b={"visible": False}, match={"id": "export"})[0], 404)
        self.assertEqual(self.call("drawing_delete", match={"id": b["id"]}), (200, {"deleted": b["id"]}))
        self.assertEqual(self.call("drawing_delete", match={"id": b["id"]})[0], 404)

    def test_lock_rules(self):
        a = self.create(locked=True)
        status, p = self.call("drawing_patch", b={"points": pts(2, 50)}, match={"id": a["id"]})
        self.assertEqual(status, 409)
        self.assertIn("locked", p["error"])
        self.assertEqual(self.call("drawing_patch", b={"visible": False}, match={"id": a["id"]})[0], 200)
        self.assertEqual(self.call("drawing_delete", match={"id": a["id"]})[0], 409)
        status, p = self.call("drawing_patch", b={"locked": False, "points": pts(2, 50)}, match={"id": a["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(p["points"][0]["p"], 50.0)

    def test_points_patch_resets_basis(self):
        a = self.create()
        store.drawing_update(a["id"], {}, a["updated_at"], basis_as_of="2026-01-05")
        _, p = self.call("drawing_patch", b={"points": pts(2, 70)}, match={"id": a["id"]})
        self.assertEqual(p["price_basis"]["as_of"], "2026-10-15")
        store.drawing_update(a["id"], {}, a["updated_at"], basis_as_of="2026-01-05")
        _, p = self.call("drawing_patch", b={"name": "x"}, match={"id": a["id"]})
        self.assertEqual(p["price_basis"]["as_of"], "2026-01-05")

    def test_delete_all(self):
        self.create(locked=True)
        self.create()
        self.create(symbol="MSFT")
        # fix1023: no confirm flag needed any more (accepted and ignored); the app's dialog confirms
        self.assertEqual(self.call("drawings_delete_all", (("symbol", "AAPL"),)),
                         (200, {"symbol": "AAPL", "deleted": 2}))
        self.assertEqual(self.call("drawings_delete_all", (("symbol", "AAPL"), ("confirm", "true"))),
                         (200, {"symbol": "AAPL", "deleted": 0}))
        self.assertEqual(len(store.drawings_list("MSFT")), 1)
        self.assertTrue(any(a["action"] == "drawings_deleted_all" for a in store.list_audit()))

    def test_routes_registered(self):
        names = {(m, p): n for m, p, n, _ in mr_api.ROUTES}
        for m, p in (("GET", "/research/drawings"), ("POST", "/research/drawings"), ("DELETE", "/research/drawings"),
                     ("PATCH", "/research/drawings/{id}"), ("DELETE", "/research/drawings/{id}"),
                     ("GET", "/research/drawings/export"), ("POST", "/research/drawings/import"),
                     ("GET", "/research/chart-settings"), ("PUT", "/research/chart-settings"),
                     ("GET", "/research/symbols/{symbol}/indicators")):
            self.assertIn((m, p), names)
        self.assertEqual(len(names), len(mr_api.ROUTES))     # no duplicate (method, path)


class ExportImportTest(Case):
    def _three(self):
        self.create(name="Trend", style={"color": "up", "width": 2, "dash": "dashed"})
        self.create(tool="simpleAnnotation", text="Note — ünïcode ✓", timeframes=["1D"])
        al = self.create(tool="alertLine", locked=True)
        store.drawing_update(al["id"], {"alert_id": "cd" * 16}, al["updated_at"])   # linked by an alert

    def test_roundtrip(self):
        self._three()
        status, f = self.call("drawings_export", (("symbol", "AAPL"),))
        self.assertEqual(status, 200)
        self.assertEqual(set(f), {"version", "symbol", "exported_at", "drawings"})
        self.assertEqual((f["version"], f["symbol"], len(f["drawings"])), (1, "AAPL", 3))
        self.assertTrue(all("id" not in d and "alert_id" not in d for d in f["drawings"]))
        text = json.loads(json.dumps(f))                    # through a file
        before = store.drawings_list("AAPL")
        status, r = self.call("drawings_import", (("symbol", "AAPL"), ("mode", "replace")), text)
        self.assertEqual(status, 200, r)
        self.assertEqual((r["imported"], r["replaced"]), (3, 3))
        after = store.drawings_list("AAPL")
        keys = ("tool", "points", "style", "name", "text", "locked", "visible", "timeframes")
        self.assertEqual([{k: d[k] for k in keys} for d in after], [{k: d[k] for k in keys} for d in before])
        self.assertTrue(set(d["id"] for d in after).isdisjoint(d["id"] for d in before))
        self.assertEqual([d["alert_id"] for d in after], [None, None, None])
        status, r = self.call("drawings_import", (("symbol", "AAPL"),), text)       # append (default)
        self.assertEqual((status, r["imported"], r["replaced"]), (200, 3, 0))
        self.assertEqual(len(store.drawings_list("AAPL")), 6)
        _, f2 = self.call("drawings_export", (("symbol", "AAPL"),))
        self.assertEqual(f2["drawings"][:3], f["drawings"])

    def test_bad_files_rejected_nothing_written(self):
        self._three()
        _, f = self.call("drawings_export", (("symbol", "AAPL"),))
        n0 = len(store.drawings_list("AAPL"))

        def variant(fn):
            g = json.loads(json.dumps(f))
            fn(g)
            return g

        bad = [
            ("not object", []), ("version", variant(lambda g: g.update(version=2))),
            ("version bool", variant(lambda g: g.update(version=True))),
            ("symbol", variant(lambda g: g.update(symbol="MSFT"))), ("extra", variant(lambda g: g.update(x=1))),
            ("missing drawings", variant(lambda g: g.pop("drawings"))),
            ("bad tool", variant(lambda g: g["drawings"][1].update(tool="laser"))),
            ("bad points", variant(lambda g: g["drawings"][2].update(points=[]))),
            ("extra key in drawing", variant(lambda g: g["drawings"][0].update(id="ab" * 16))),
            ("basis future", variant(lambda g: g["drawings"][0].update(price_basis={"as_of": "2027-01-01"}))),
            ("basis shape", variant(lambda g: g["drawings"][0].update(price_basis={"as_of": "2026-01-01", "factor": 2}))),
            ("missing basis", variant(lambda g: g["drawings"][0].pop("price_basis"))),
            ("too many", variant(lambda g: g.update(drawings=g["drawings"] * 200))),
            ("too large", variant(lambda g: g["drawings"][0].update(text="x" * 280) or g.update(
                drawings=[dict(g["drawings"][0], points=pts(2))] * 1000))),
        ]
        for name, b in bad:
            with self.subTest(name):
                status, p = self.call("drawings_import", (("symbol", "AAPL"),), b)
                self.assertEqual(status, 400, (name, p))
                self.assertIn("error", p)
        status, p = self.call("drawings_import", (("symbol", "AAPL"),), variant(lambda g: g.update(symbol="MSFT")))
        self.assertEqual(p["error"], "This file holds drawings for MSFT, not AAPL.")
        self.assertEqual(self.call("drawings_import", (("symbol", "AAPL"), ("mode", "merge")), f)[0], 400)
        self.assertEqual(self.call("drawings_import", (("mode", "append"),), f)[0], 400)
        self.assertEqual(self.call("drawings_import", (("symbol", "AAPL"),), NO_BODY)[0], 400)
        self.assertEqual(len(store.drawings_list("AAPL")), n0)
        with mock.patch.object(limits, "MAX_DRAWINGS_PER_SYMBOL", 5):
            status, p = self.call("drawings_import", (("symbol", "AAPL"),), f)
            self.assertEqual(status, 409)                       # 3 + 3 > 5, nothing written
            self.assertEqual(len(store.drawings_list("AAPL")), n0)
            self.assertEqual(self.call("drawings_import", (("symbol", "AAPL"), ("mode", "replace")), f)[0], 200)

    def test_import_crypto_basis_and_export_limit(self):
        g = {"version": 1, "symbol": "BTC-USD", "drawings": [
            {"tool": "priceLine", "points": [{"t": "2026-10-01T00:00:00Z", "p": 60000}], "style": None,
             "name": None, "text": None, "locked": False, "visible": True, "timeframes": "all",
             "price_basis": {"as_of": None}}]}
        status, r = self.call("drawings_import", (("symbol", "BTC-USD"),), g)
        self.assertEqual(status, 200, r)
        self.assertEqual(r["drawings"][0]["price_basis"]["as_of"], None)
        g["drawings"][0]["price_basis"] = {"as_of": "2026-10-01"}
        self.assertEqual(self.call("drawings_import", (("symbol", "BTC-USD"),), g)[0], 400)
        for _ in range(3):
            self.create(tool="brush", n=300)
        with mock.patch.object(limits, "MAX_IMPORT_BYTES", 20000):
            status, p = self.call("drawings_export", (("symbol", "AAPL"),))
            self.assertEqual(status, 409)
            self.assertIn("import limit", p["error"])


class ChartSettingsTest(Case):
    def test_get_put(self):
        status, p = self.call("chart_settings_get", (("symbol", "AAPL"),))
        self.assertEqual((status, p["saved"], p["settings"]),
                         (200, False, {"type": "candles", "indicators": [], "magnet": "off", "session": "rth"}))
        _, c = self.call("chart_settings_get", (("symbol", "ETH-USD"),))
        self.assertIsNone(c["settings"]["session"])
        s = {"type": "line", "indicators": ["SMA:20", "bb", "rsi", "macd"], "magnet": "weak", "session": "eth"}
        status, p = self.call("chart_settings_put", (("symbol", "AAPL"),), s)
        self.assertEqual(status, 200, p)
        self.assertEqual(p["settings"]["indicators"], ["sma:20:close", "bb:20:2:close", "rsi:14", "macd:12:26:9"])
        self.assertEqual(self.call("chart_settings_get", (("symbol", "AAPL"),))[1]["settings"], p["settings"])
        bad = [dict(s, type="bars"), dict(s, magnet="max"), dict(s, session="utc"), dict(s, extra=1),
               {k: v for k, v in s.items() if k != "magnet"}, dict(s, indicators=["rsi:7", "rsi:9", "macd", "rsi"]),
               dict(s, indicators=["sma:20"] * 2), dict(s, indicators=["sma:1:close"] * 1 + ["foo"]),
               dict(s, indicators="sma:20")]
        for b in bad:
            self.assertEqual(self.call("chart_settings_put", (("symbol", "AAPL"),), b)[0], 400, b)
        self.assertEqual(self.call("chart_settings_put", (("symbol", "BTC-USD"),), s)[0], 400)   # session for crypto
        self.assertEqual(self.call("chart_settings_put", (("symbol", "BTC-USD"),), dict(s, session=None))[0], 200)


if __name__ == "__main__":
    unittest.main()
