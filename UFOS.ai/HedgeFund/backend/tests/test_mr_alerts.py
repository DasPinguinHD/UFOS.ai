"""Market Research MR7 — alerts REST + store (SPEC §13 "MR7 backend notes"): validation of every rule type,
limits (200 rules, cooldown range, day-move range), duplicates, line links (alertLine of the same symbol, one
alert per line, cannot be re-enabled once the line is gone), PATCH / DELETE, the alert log route, the exact
alert object + runtime note, store v7 + 90-day log retention, audit rows, route registration.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_alerts -v
"""
import json
import logging
import sqlite3
import unittest
from datetime import timedelta

try:
    from mr_data_testkit import TempStore
    from mr_alerts_testkit import NOW, AnyData, alert_line, run
except ImportError:  # pragma: no cover
    from tests.mr_data_testkit import TempStore
    from tests.mr_alerts_testkit import NOW, AnyData, alert_line, run

from market_research import ResearchService, api as mr_api, limits, store
from market_research import alerts as al
from market_research import drawings as dr
from market_research.supervisor import ResearchSupervisor
from strategy_platform.api.routes import NO_BODY, dumps


def setUpModule():
    logging.getLogger("market_research").setLevel(logging.CRITICAL + 1)


def tearDownModule():
    logging.getLogger("market_research").setLevel(logging.NOTSET)


class Q(dict):
    def __init__(self, pairs=()):
        super().__init__()
        self.pairs = list(pairs)
        for k, v in self.pairs:
            self.setdefault(k, v)

    def getall(self, k, *default):
        # aiohttp semantics: a missing key raises KeyError unless a default is given
        vals = [v for kk, v in self.pairs if kk == k]
        if not vals:
            if default:
                return default[0]
            raise KeyError(k)
        return vals


class Case(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()
        self.svc = ResearchService(supervisor=ResearchSupervisor(), data=AnyData())
        self.svc.alerts._now = lambda: NOW
        self.api = mr_api.ResearchApi(self.svc)

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def call(self, name, body=NO_BODY, query=(), **match):
        status, payload = run(getattr(self.api, name)(Q(query), body, match))
        json.loads(dumps(payload))
        return status, payload

    def create(self, **kw):
        b = {"symbol": "AAPL", "kind": "price_cross", "params": {"level": 190.0, "direction": "up"}}
        b.update(kw)
        st, p = self.call("alert_create", b)
        self.assertEqual(st, 201, p)
        return p


class CreateTest(Case):
    def test_each_kind_and_exact_object(self):
        a = self.create()
        self.assertEqual(set(a), {"id", "symbol", "kind", "params", "cooldown_seconds", "enabled", "drawing_id",
                                  "last_triggered_at", "disabled_reason", "created_at", "updated_at", "status"})
        self.assertEqual((a["cooldown_seconds"], a["enabled"], a["drawing_id"]), (900, True, None))
        self.assertEqual(set(a["status"]), {"state", "reason", "live", "level", "last_price", "prev_close",
                                            "cooldown_until"})
        self.assertEqual(a["status"]["state"], "not_running")
        self.assertIn("only while UFOS.ai runs", a["status"]["reason"])
        d = alert_line(p=181.5)
        b = self.create(kind="line_cross", params={"drawing_id": d["id"]})
        self.assertEqual((b["params"], b["drawing_id"]), ({"drawing_id": d["id"], "direction": "any"}, d["id"]))
        c = self.create(kind="day_move", params={"pct": 5, "direction": "any"}, cooldown_seconds=60)
        self.assertEqual((c["params"], c["cooldown_seconds"]), ({"pct": 5.0, "direction": "any"}, 60))
        n = self.create(symbol="btc-usd", kind="news", params={}, enabled=False)
        self.assertEqual((n["symbol"], n["enabled"], n["status"]["state"]), ("BTC-USD", False, "disabled"))
        st, lst = self.call("alerts_list")
        self.assertEqual([x["kind"] for x in lst["alerts"]], ["price_cross", "line_cross", "day_move", "news"])
        self.assertEqual(lst["runtime"]["note"], al.RUNTIME_NOTE)
        self.assertEqual(set(lst["limits"]), {"max_alerts", "cooldown_default", "cooldown_min", "cooldown_max",
                                              "day_move_pct_min", "day_move_pct_max"})
        self.assertTrue(any(x["action"] == "alert_created" for x in store.list_audit()))

    def test_validation(self):
        bad = [NO_BODY, [], {}, {"symbol": "AAPL"}, {"symbol": "bad sym", "kind": "news", "params": {}},
               {"symbol": "AAPL", "kind": "volume", "params": {}},
               {"symbol": "AAPL", "kind": "news", "params": {"x": 1}},
               {"symbol": "AAPL", "kind": "news", "params": {}, "extra": 1},
               {"symbol": "AAPL", "kind": "price_cross", "params": {"level": 0, "direction": "up"}},
               {"symbol": "AAPL", "kind": "price_cross", "params": {"level": float("inf"), "direction": "up"}},
               {"symbol": "AAPL", "kind": "price_cross", "params": {"level": "100", "direction": "up"}},
               {"symbol": "AAPL", "kind": "price_cross", "params": {"level": True, "direction": "up"}},
               {"symbol": "AAPL", "kind": "price_cross", "params": {"level": 100, "direction": "any"}},
               {"symbol": "AAPL", "kind": "price_cross", "params": {"level": 2e9, "direction": "up"}},
               {"symbol": "AAPL", "kind": "day_move", "params": {"pct": 0.05, "direction": "up"}},
               {"symbol": "AAPL", "kind": "day_move", "params": {"pct": 51, "direction": "up"}},
               {"symbol": "AAPL", "kind": "day_move", "params": {"pct": 5}},
               {"symbol": "AAPL", "kind": "line_cross", "params": {"drawing_id": "x"}},
               {"symbol": "AAPL", "kind": "line_cross", "params": {"drawing_id": "a" * 32}},     # no such drawing
               {"symbol": "AAPL", "kind": "news", "params": {}, "cooldown_seconds": 59},
               {"symbol": "AAPL", "kind": "news", "params": {}, "cooldown_seconds": 86401},
               {"symbol": "AAPL", "kind": "news", "params": {}, "cooldown_seconds": 90.5},
               {"symbol": "AAPL", "kind": "news", "params": {}, "enabled": "yes"},
               {"symbol": "AAPL", "kind": "news", "params": {}, "pad": "x" * 5000}]
        for b in bad:
            st, p = self.call("alert_create", b)
            self.assertEqual(st, 400, (b, p))
        self.assertEqual(self.call("alert_create", {"symbol": "ZZZZ", "kind": "news", "params": {}})[1]["kind"],
                         "SymbolNotCovered")
        self.assertEqual(self.call("alert_create", {"symbol": "AAPL", "kind": "news", "params": {}}, (("x", "1"),))[0], 400)
        self.assertEqual(store.alerts_list(), [])

    def test_line_must_be_an_alert_line_of_the_symbol(self):
        seg = dr.create({"symbol": "AAPL", "tool": "segment", "points": [{"t": "2026-10-14T14:00:00+00:00", "p": 1.0},
                                                                         {"t": "2026-10-14T15:00:00+00:00", "p": 2.0}]}, now=NOW)
        st, p = self.call("alert_create", {"symbol": "AAPL", "kind": "line_cross", "params": {"drawing_id": seg["id"]}})
        self.assertIn("not an alert line", p["errors"]["params.drawing_id"])
        msft = alert_line("MSFT")
        st, p = self.call("alert_create", {"symbol": "AAPL", "kind": "line_cross", "params": {"drawing_id": msft["id"]}})
        self.assertIn("belongs to MSFT", p["errors"]["params.drawing_id"])
        line = alert_line()
        self.create(kind="line_cross", params={"drawing_id": line["id"], "direction": "up"})
        st, p = self.call("alert_create", {"symbol": "AAPL", "kind": "line_cross",
                                           "params": {"drawing_id": line["id"], "direction": "down"}})
        self.assertEqual((st, p), (409, {"error": "This alert line already has an alert."}))

    def test_limits_and_duplicates(self):
        self.create()
        st, p = self.call("alert_create", {"symbol": "aapl", "kind": "price_cross", "params": {"level": 190, "direction": "up"}})
        self.assertEqual((st, p["error"]), (409, "An identical alert for this symbol already exists."))
        for i in range(limits.MAX_ALERTS - 1):
            self.create(params={"level": 200.0 + i, "direction": "up"})
        st, p = self.call("alert_create", {"symbol": "AAPL", "kind": "news", "params": {}})
        self.assertEqual((st, p["error"]), (409, "At most 200 alerts — delete one first."))


class PatchDeleteLogTest(Case):
    def test_patch(self):
        a = self.create()
        st, p = self.call("alert_update", {"params": {"level": 195, "direction": "down"}, "cooldown_seconds": 120},
                          id=a["id"])
        self.assertEqual((st, p["params"], p["cooldown_seconds"]), (200, {"level": 195.0, "direction": "down"}, 120))
        self.assertEqual(self.call("alert_update", {"enabled": False}, id=a["id"])[1]["status"]["state"], "disabled")
        for b in (NO_BODY, {}, {"symbol": "MSFT"}, {"kind": "news"}, {"params": {"level": -1, "direction": "up"}},
                  {"enabled": 1}, {"cooldown_seconds": 10}, {"x": 1}):
            self.assertEqual(self.call("alert_update", b, id=a["id"])[0], 400, b)
        self.assertEqual(self.call("alert_update", {"enabled": True}, id="f" * 32)[0], 404)
        self.assertEqual(self.call("alert_update", {"enabled": True}, id="../x")[0], 404)
        b = self.create(params={"level": 10, "direction": "up"})
        st, p = self.call("alert_update", {"params": {"level": 195, "direction": "down"}}, id=b["id"])
        self.assertEqual(st, 409)

    def test_line_cannot_move_to_another_drawing_or_return_after_delete(self):
        l1, l2 = alert_line(), alert_line(p=120.0)
        a = self.create(kind="line_cross", params={"drawing_id": l1["id"]})
        st, p = self.call("alert_update", {"params": {"drawing_id": l2["id"], "direction": "any"}}, id=a["id"])
        self.assertEqual(st, 400)
        self.call("alert_update", {"enabled": False}, id=a["id"])
        dr.delete(l1["id"])
        st, p = self.call("alert_update", {"enabled": True}, id=a["id"])
        self.assertEqual(st, 409)
        self.assertIn("deleted", p["error"])

    def test_delete_and_log(self):
        a = self.create()
        lid = store.alert_fire(a["id"], "2026-10-15T15:00:00+00:00", {}, {"symbol": "AAPL", "kind": "price_cross",
                                                                          "message": "m", "price": 190.1})
        st, p = self.call("alerts_log")
        self.assertEqual((st, p["limit"], p["retention_days"]), (200, 100, 90))
        self.assertEqual(p["items"][0], {"id": lid, "alert_id": a["id"], "symbol": "AAPL",
                                         "ts": "2026-10-15T15:00:00+00:00", "kind": "price_cross", "message": "m",
                                         "price": 190.1, "detail": {}})
        self.assertEqual(len(self.call("alerts_log", query=(("alert_id", a["id"]),))[1]["items"]), 1)
        self.assertEqual(len(self.call("alerts_log", query=(("alert_id", "0" * 32),))[1]["items"]), 0)
        for q in ((("limit", "0"),), (("limit", "501"),), (("alert_id", "x"),), (("foo", "1"),)):
            self.assertEqual(self.call("alerts_log", query=q)[0], 400, q)
        self.assertEqual(self.call("alert_delete", id=a["id"]), (200, {"deleted": a["id"]}))
        self.assertEqual(self.call("alert_delete", id=a["id"])[0], 404)
        self.assertEqual(len(self.call("alerts_log")[1]["items"]), 1)      # the log outlives the rule
        self.assertIsNone(store.alert_fire(a["id"], "2026-10-15T15:01:00+00:00", {}, {"symbol": "AAPL",
                                           "kind": "price_cross", "message": "m"}))   # rule gone: nothing written


class StoreTest(Case):
    def test_retention_90_days_and_schema(self):
        a = self.create()
        old = (NOW - timedelta(days=91)).isoformat(timespec="seconds")
        new = (NOW - timedelta(days=89)).isoformat(timespec="seconds")
        for t in (old, new):
            store.alert_fire(a["id"], t, {}, {"symbol": "AAPL", "kind": "price_cross", "message": t})
        deleted = store.prune(NOW)
        self.assertEqual(deleted["alert_log"], 1)
        self.assertEqual([x["message"] for x in store.alert_log_list(10)], [new])
        self.assertEqual(len(store.alerts_list()), 1)                     # rules: until deleted
        self.assertGreaterEqual(store.SCHEMA_VERSION, 7)
        with sqlite3.connect(store.db_path()) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"alerts", "alert_log"} <= tables)

    def test_disable_with_reason_writes_audit(self):
        a = self.create()
        self.assertTrue(store.alert_disable(a["id"], "why", "2026-10-15T15:00:00+00:00"))
        self.assertFalse(store.alert_disable(a["id"], "why", "2026-10-15T15:00:00+00:00"))
        r = store.alert_get(a["id"])
        self.assertEqual((r["enabled"], r["disabled_reason"]), (False, "why"))
        self.assertTrue(any(x["action"] == "alert_disabled" for x in store.list_audit()))
        # switching on again by the user clears the reason
        self.assertIsNone(self.call("alert_update", {"enabled": True}, id=a["id"])[1]["disabled_reason"])

    def test_routes_registered(self):
        names = {(m, p): n for m, p, n, _ in mr_api.ROUTES}
        for m, p in (("GET", "/research/alerts"), ("POST", "/research/alerts"), ("GET", "/research/alerts/log"),
                     ("PATCH", "/research/alerts/{id}"), ("DELETE", "/research/alerts/{id}")):
            self.assertIn((m, p), names)
        self.assertEqual(len(names), len(mr_api.ROUTES))
        self.assertIn("alert_engine", [t["name"] for t in self.svc.supervisor.status()["tasks"]])


class LineLinkTest(Case):
    """The drawing↔alert link (``drawings.alert_id``) is written in the SAME transaction as the alert."""

    def line_alert(self, line, **kw):
        return self.create(kind="line_cross", params={"drawing_id": line["id"]}, **kw)

    def test_create_patch_delete_keep_the_link(self):
        line = alert_line()
        a = self.line_alert(line)
        d = store.drawing_get(line["id"])
        self.assertEqual(d["alert_id"], a["id"])
        self.assertEqual(d["updated_at"], a["created_at"])
        self.call("alert_update", {"enabled": False}, id=a["id"])
        self.assertIsNone(store.drawing_get(line["id"])["alert_id"])
        self.call("alert_update", {"enabled": True}, id=a["id"])
        self.assertEqual(store.drawing_get(line["id"])["alert_id"], a["id"])
        self.call("alert_update", {"cooldown_seconds": 120}, id=a["id"])           # other fields: link unchanged
        self.assertEqual(store.drawing_get(line["id"])["alert_id"], a["id"])
        self.call("alert_delete", id=a["id"])
        self.assertIsNone(store.drawing_get(line["id"])["alert_id"])

    def test_disabled_create_and_system_disable(self):
        l1, l2 = alert_line(), alert_line(p=120.0)
        self.line_alert(l1, enabled=False)
        self.assertIsNone(store.drawing_get(l1["id"])["alert_id"])
        b = self.line_alert(l2)
        self.assertTrue(store.alert_disable(b["id"], "reason", "2026-10-15T15:00:00+00:00"))
        self.assertIsNone(store.drawing_get(l2["id"])["alert_id"])

    def test_line_linked_to_another_alert_is_refused_dangling_link_is_replaced(self):
        l1, l2 = alert_line(), alert_line(p=120.0)
        a = self.line_alert(l1)
        store.drawing_update(l2["id"], {"alert_id": a["id"]}, "2026-10-15T15:00:00+00:00")   # stale link (e.g. an old DB row)
        st, p = self.call("alert_create", {"symbol": "AAPL", "kind": "line_cross", "params": {"drawing_id": l2["id"]}})
        self.assertEqual((st, p), (409, {"error": "This alert line already has an alert."}))
        self.assertEqual(len(store.alerts_list()), 1)
        l3 = alert_line(p=130.0)
        store.drawing_update(l3["id"], {"alert_id": "e" * 32}, "2026-10-15T15:00:00+00:00")  # points to no existing alert
        c = self.line_alert(l3)
        self.assertEqual(store.drawing_get(l3["id"])["alert_id"], c["id"])

    def test_checks_repeated_inside_the_transaction(self):
        line = alert_line()
        real = store.drawing_get
        fake = dict(real(line["id"]))
        dr.delete(line["id"])                                    # gone between the pre-check and the write
        orig = store.drawing_get
        store.drawing_get = lambda i: fake if i == line["id"] else orig(i)
        try:
            st, p = self.call("alert_create", {"symbol": "AAPL", "kind": "line_cross",
                                               "params": {"drawing_id": line["id"]}})
        finally:
            store.drawing_get = orig
        self.assertEqual((st, p), (409, {"error": "The linked alert line no longer exists."}))
        self.assertEqual(store.alerts_list(), [])

    def test_failed_insert_rolls_the_link_back(self):
        line = alert_line()
        with sqlite3.connect(store.db_path()) as c:
            c.execute("CREATE TRIGGER fail_alert BEFORE INSERT ON alerts BEGIN SELECT RAISE(ABORT, 'boom'); END")
        with self.assertRaises(store.StoreUnavailable):
            self.call("alert_create", {"symbol": "AAPL", "kind": "line_cross", "params": {"drawing_id": line["id"]}})
        self.assertIsNone(store.drawing_get(line["id"])["alert_id"])
        self.assertEqual(store.drawing_get(line["id"])["updated_at"], line["updated_at"])


if __name__ == "__main__":
    unittest.main()
