"""Kraken Futures demo RETIRED (SPEC §18.13, user decision 2026-10-19 — Kraken decommissioned the demo; stage 10
S10-K removed its adapter, branches and key names). The retirement guard stays: every broker action on kraken-demo
answers 409 "retired" and no broker client is ever built for it — also for an old kraken-demo row (account,
connection record, route) that hedgefund.db may still hold; the runtime / poller / global kill skip such a row
without error, routes to it are shown blocked (never moved silently) and block the incubation start, crypto routes
offer paper-main / binance-demo only, the broker list shows the retired card only while such a row exists, the
retired key names are still accepted by the keys-changed audit for "removed", and the Kraken PUBLIC price source
(api.kraken.com, Shadow Account) is unaffected. Fakes, temp DBs, offline."""
import asyncio
import importlib.util
import io
import json
import unittest

try:
    from sp_broker_testkit import BrokerCase, make_strategy
except ImportError:  # pragma: no cover
    from tests.sp_broker_testkit import BrokerCase, make_strategy

from strategy_platform.api import routes
from strategy_platform.data.providers import kraken_public
from strategy_platform.execution import accounts, control, oms
from strategy_platform.execution.brokers import mirror, service
from strategy_platform.live import incubation
from strategy_platform.portfolio import settings as pf_settings
from strategy_platform.storage import db

REASON = "retired — Kraken decommissioned its Futures demo environment (2026); see Brokers notes"


def call(name, query=None, body=routes.NO_BODY, match=None):
    status, payload = asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))
    routes.dumps(payload)
    return status, payload


def legacy_rows(connected=True, route_sid=None):
    """An old kraken-demo row as hedgefund.db may still hold it (created while the demo existed): the account, its
    connection record and optionally a strategy routed there — written directly, as the adapter is gone."""
    ts = db.utc_now_iso()
    with db._connect() as conn:
        conn.execute("INSERT INTO br_accounts (id, broker, label, params_json, created_at, created_by) VALUES"
                     " (?, ?, ?, ?, ?, ?)", ("kraken-demo", "kraken_demo", "Kraken Futures demo account",
                                             json.dumps(accounts.broker_params(50000.0), sort_keys=True), ts, "user"))
        pf_settings.write("kraken-demo", pf_settings.defaults(), "system", "broker account connected (defaults)",
                          conn=conn)
        if connected:
            conn.execute("INSERT INTO br_connections (account_id, ts, status, actor, reason, detail_json) VALUES"
                         " (?, ?, 'connected', 'user', 'before the retirement', ?)",
                         ("kraken-demo", ts, json.dumps({"first": True, "cash_basis": 50000.0, "currency": "USD",
                                                         "baseline": None})))
        if route_sid:
            conn.execute("INSERT INTO br_routes (strategy_id, account_id, ts, actor, reason) VALUES (?, ?, ?, 'user',"
                         " 'before the retirement')", (route_sid, "kraken-demo", ts))
    accounts.reset_cache()


class RetiredTest(BrokerCase):
    def test_adapter_and_key_names_are_gone(self):
        self.assertIsNone(importlib.util.find_spec("strategy_platform.execution.brokers.kraken_demo"))
        from strategy_platform import secrets
        self.assertFalse([n for n in secrets.SECRETS if n.startswith("kraken")])
        self.assertEqual(set(secrets.RETIRED_SECRETS), {"kraken-demo-api-key", "kraken-demo-api-secret"})
        self.assertNotIn("kraken-demo", accounts.ACTIVE_BROKER_IDS)
        self.assertIn("kraken-demo", accounts.BROKER_IDS)            # still known: an old row stays blocked
        self.assertIsInstance(mirror.remote_adapter("kraken-demo"), mirror.RetiredAdapter)
        self.assertNotIn("kraken-demo", self.asked)

    def test_every_action_409_and_no_call(self):
        m = {"id": "kraken-demo"}
        for name, body, query in (("broker_test", {}, None),
                                  ("broker_connect", {"confirm": "CONNECT KRAKEN-DEMO", "reason": "try it"}, None),
                                  ("broker_reconcile", {}, None),
                                  ("broker_resolve", {"action": "recheck", "reason": "try it"}, None),
                                  ("broker_kill", {"reason": "x"}, None),
                                  ("broker_instrument", routes.NO_BODY, {"symbol": "BTC-USD"})):
            st, p = call(name, query=query, body=body, match=m)
            self.assertEqual((st, p.get("code")), (409, "retired"), name)
            self.assertIn(REASON, p["error"])
        st, p = call("broker_get", match=m)
        b = p["broker"]
        self.assertEqual((b["state"], b["retired"]["reason"], b["chip"]), ("retired", REASON, "RETIRED"))
        self.assertIn("support.kraken.com/articles/360024809011", b["retired"]["sources"][0])
        self.assertTrue(b["notes"][0].startswith("Retired 2026-10-19"))
        self.assertEqual((b["keys"], b["endpoint"], b["sdk"]), ([], None, None))
        self.assertFalse(any(b["can"].values()))
        st, p = call("broker_get", match={"id": "alpaca-paper"})
        self.assertEqual((p["broker"]["state"], p["broker"]["retired"]), ("active", None))
        self.assertNotIn("kraken-demo", [x["id"] for x in call("brokers_list")[1]["brokers"]])   # no row: not listed
        self.assertNotIn("kraken-demo", self.asked)                              # no broker client at all

    def test_connected_row_is_skipped_everywhere(self):
        legacy_rows()
        acc = accounts.get("kraken-demo")
        self.assertIsInstance(acc.remote, mirror.RetiredAdapter)
        st = call("brokers_list")[1]["brokers"]
        self.assertEqual([(x["id"], x["state"]) for x in st],
                         [("alpaca-paper", "active"), ("binance-demo", "active"), ("kraken-demo", "retired")])
        self.assertNotIn("kraken-demo", [a.id for a in accounts.all_accounts()])
        self.rt.cycle()
        service.poll_once()
        service.startup()
        out = control.kill_all("panic", "user")
        self.assertTrue(out["platform"]["ok"], out["platform"])
        self.assertNotIn("kraken-demo", [a["account_id"] for a in out["platform"]["accounts"]])
        o, _ = oms.place(acc, {"symbol": "BTC-USD", "side": "buy", "qty": 0.01, "origin": "manual"})
        self.assertEqual(o["state"], "rejected")
        self.assertIn(REASON, o["reject_reason"])
        with self.assertRaises(Exception) as cm:
            acc.adapter.remote_call("account", user=True)             # even a user call is refused locally
        self.assertIn(REASON, str(getattr(cm.exception, "user_message", cm.exception)))
        st = service.broker_status("kraken-demo")
        self.assertEqual((st["state"], st["can"]["disconnect"], st["can"]["kill"]), ("retired", True, False))
        out = service.disconnect("kraken-demo", "clean up the retired broker")   # record only, no broker call
        self.assertEqual(out["broker"]["connection"]["status"], "disconnected")
        self.assertIn("kraken-demo", [x["id"] for x in call("brokers_list")[1]["brokers"]])   # history stays visible
        self.assertNotIn("kraken-demo", self.asked)

    def test_routing_blocked_not_moved(self):
        sid = make_strategy(symbols=("BTC-USD",), state="validated", name="crypto routed")
        legacy_rows(route_sid=sid)
        r = next(x for x in service.routing_list()["routes"] if x["strategy_id"] == sid)
        self.assertEqual((r["account_id"], r["blocked"], r["allowed_accounts"]),
                         ("kraken-demo", True, ["paper-main", "binance-demo"]))
        self.assertIn("re-route the strategy", r["blocked_reason"])
        self.assertEqual(service.route_for(sid), "kraken-demo")       # never moved silently
        el = incubation.eligibility(sid)
        self.assertEqual((el["eligible"], el["code"], el["account_id"]), (False, "retired", "kraken-demo"))
        st, p = call("brokers_routing_post", body={"strategy_id": sid, "account_id": "kraken-demo",
                                                   "confirm": "ROUTE TO KRAKEN-DEMO", "reason": "again please"})
        self.assertEqual((st, p["code"]), (409, "retired"))
        out = service.set_route(sid, "paper-main", "ROUTE TO PAPER-MAIN", "re-route after the retirement", "user")
        self.assertEqual((out["route"]["account_id"], out["route"]["blocked"]), ("paper-main", False))
        self.assertTrue(incubation.eligibility(sid)["eligible"] or incubation.eligibility(sid)["code"] != "retired")
        self.assertNotIn("kraken-demo", self.asked)

    def test_retired_key_names_audited_on_removal(self):
        for name in ("kraken-demo-api-key", "kraken-demo-api-secret"):
            st, p = call("keys_changed", body={"name": name, "action": "removed"})
            self.assertEqual((st, p["ok"]), (200, True), p)
        for action in ("set", "imported"):
            st, p = call("keys_changed", body={"name": "kraken-demo-api-key", "action": action})
            self.assertEqual(st, 400)
            self.assertIn("retired", p["errors"]["name"])
        with db._connect() as c:
            rows = [json.loads(r["detail_json"])["name"] for r in
                    c.execute("SELECT detail_json FROM audit WHERE action = 'key_removed' ORDER BY id").fetchall()]
        self.assertEqual(rows, ["kraken-demo-api-key", "kraken-demo-api-secret"])
        names = [k["name"] for k in call("keys")[1]["keys"]]
        self.assertFalse([n for n in names if n.startswith("kraken")])

    def test_kraken_public_prices_unaffected(self):
        bodies = {"error": [], "result": {"XXBTZEUR": [[1759276800, "1", "1", "1", "50000.0", "1", "1", 1],
                                                       [1759363200, "1", "1", "1", "51000.0", "1", "1", 1]],
                                          "last": 1759276800}}
        urls = []

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def net(req, timeout=None, context=None):
            urls.append(req.full_url)
            return R(json.dumps(bodies).encode())
        kraken_public.set_transport(net)
        self.addCleanup(kraken_public.set_transport, None)
        kraken_public._cache.clear()
        self.addCleanup(kraken_public._cache.clear)
        self.assertIn("kraken-demo", accounts.RETIRED)
        bars = kraken_public.closes("BTC")
        self.assertTrue(bars)
        self.assertTrue(urls[0].startswith("https://api.kraken.com/0/public/OHLC"))
        self.assertNotIn("kraken-demo", self.asked)


if __name__ == "__main__":
    unittest.main()
