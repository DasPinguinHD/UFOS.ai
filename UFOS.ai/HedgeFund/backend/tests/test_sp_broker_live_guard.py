"""Stage 8 live guard (SPEC §18.7): live needs ALL four conditions (live flag, typed confirmation, strategy in state
Live, capital cap) — and this build refuses even then with the hard-coded message; no live endpoint can be built
or used (endpoint_for(live=True), URL assertion, a source scan of the broker code for live hosts). Settings and
checks are audited; the routes never answer allowed: true. Temp DBs, offline."""
import asyncio
import json
import os
import re
import unittest

try:
    from sp_broker_testkit import BrokerCase, make_strategy
except ImportError:  # pragma: no cover
    from tests.sp_broker_testkit import BrokerCase, make_strategy

from strategy_platform.api import routes
from strategy_platform.execution.brokers import live_guard
from strategy_platform.storage import db

MSG = "Live trading is disabled in this build (stage 12 requires the user's explicit go)."
BROKERS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategy_platform",
                           "execution")


def call(name, query=None, body=routes.NO_BODY, match=None):
    status, payload = asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))
    routes.dumps(payload)
    return status, payload


class LiveGuardTest(BrokerCase):
    def full_guard(self):
        return {"live_flag": True, "capital_cap_usd": 5000.0}

    def test_message_is_exact(self):
        self.assertEqual(live_guard.LIVE_DISABLED, MSG)
        self.assertFalse(live_guard.BUILD_ALLOWS_LIVE)

    def test_each_condition_named(self):
        phrase = live_guard.confirmation_phrase("alpaca-paper")
        self.assertEqual(phrase, "GO LIVE ALPACA-PAPER WITH REAL MONEY")
        base = dict(guard=self.full_guard(), confirm=phrase, strategy_state="live")
        cases = {"live_flag": dict(base, guard={"live_flag": False, "capital_cap_usd": 5000.0}),
                 "typed_confirmation": dict(base, confirm="go live"),
                 "strategy_live": dict(base, strategy_state="incubation"),
                 "capital_cap": dict(base, guard={"live_flag": True, "capital_cap_usd": None})}
        for cond, kw in cases.items():
            r = live_guard.evaluate("alpaca-paper", **kw)
            self.assertFalse(r["allowed"])
            self.assertEqual(r["failed"], [cond])
            self.assertEqual(r["refusal"], MSG)
        r = live_guard.evaluate("alpaca-paper", **base)          # all four met: STILL refused by the build
        self.assertEqual((r["allowed"], r["failed"], r["all_conditions_met"], r["refusal"]), (False, [], True, MSG))
        r = live_guard.evaluate("binance-demo", guard={}, confirm=None, strategy_state=None)
        self.assertEqual(set(r["failed"]), {"live_flag", "typed_confirmation", "strategy_live", "capital_cap"})

    def test_no_live_endpoint(self):
        with self.assertRaises(live_guard.LiveTradingDisabled) as cm:
            live_guard.endpoint_for("alpaca_paper", live=True)
        self.assertEqual(cm.exception.user_message, MSG)
        self.assertEqual(live_guard.endpoint_for("binance_demo"), "https://demo-api.binance.com")
        with self.assertRaises(ValueError):
            live_guard.endpoint_for("kraken_demo")            # S10-K: the retired demo has no endpoint at all
        for broker, url in (("alpaca_paper", "https://api.alpaca.markets"),
                            ("alpaca_paper", "http://paper-api.alpaca.markets"),
                            ("alpaca_paper", "https://paper-api.alpaca.markets.evil.example"),
                            ("binance_demo", "https://api.binance.com/api/v3/account"),
                            ("binance_demo", None),
                            ("kraken_demo", "https://demo-futures.kraken.com/derivatives/api/"),
                            ("kraken_demo", "https://futures.kraken.com/derivatives/api/")):
            with self.assertRaises(live_guard.LiveTradingDisabled):
                live_guard.assert_paper_url(broker, url)
        with self.assertRaises(live_guard.LiveTradingDisabled):
            live_guard.assert_paper_urls("binance_demo", [])
        with self.assertRaises(live_guard.LiveTradingDisabled):
            live_guard.require_paper_order("alpaca-paper", {"live": True})

    def test_source_scan_has_no_live_host(self):
        pats = [re.compile(r"(?<!paper-)api\.alpaca\.markets"), re.compile(r"(?<!demo-)futures\.kraken\.com"),
                re.compile(r"api\.kraken\.com")]
        hits = []
        for root, _dirs, files in os.walk(BROKERS_DIR):
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                with open(path, encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        if any(p.search(line) for p in pats):
                            hits.append((os.path.relpath(path, BROKERS_DIR), n, line.strip()))
        allowed = [h for h in hits if h[0].endswith("live_guard.py") and "LIVE_HOSTS" in h[2]]
        self.assertEqual(hits, allowed)
        self.assertEqual(len(allowed), 1)

    def test_settings_and_check_audited(self):
        with self.assertRaises(ValueError):
            live_guard.set_settings("alpaca-paper", True, 10.0, "too small cap")
        g = live_guard.set_settings("alpaca-paper", True, 2500.0, "test the guard")
        self.assertEqual((g["live_flag"], g["capital_cap_usd"]), (True, 2500.0))
        sid = make_strategy(state="validated")
        out = live_guard.check("alpaca-paper", strategy_id=sid,
                               confirm=live_guard.confirmation_phrase("alpaca-paper"))
        self.assertEqual((out["allowed"], out["failed"], out["refusal"]), (False, ["strategy_live"], MSG))
        with db._connect() as c:
            acts = [r["action"] for r in c.execute("SELECT action FROM audit").fetchall()]
        self.assertIn("live_guard_settings", acts)
        self.assertIn("live_guard_check", acts)

    def test_routes(self):
        st, p = call("broker_live_guard_get", match={"id": "binance-demo"})
        self.assertEqual((st, p["build_allows_live"], p["message"]), (200, False, MSG))
        self.assertEqual([c["id"] for c in p["conditions"]], ["live_flag", "typed_confirmation", "strategy_live",
                                                              "capital_cap"])
        self.assertEqual(p["confirmation_phrase"], "GO LIVE BINANCE-DEMO WITH REAL MONEY")
        st, p = call("broker_live_guard_put", match={"id": "binance-demo"},
                     body={"live_flag": "yes", "capital_cap_usd": 5, "reason": "x"})
        self.assertEqual(st, 400)
        self.assertEqual(set(p["errors"]), {"live_flag", "capital_cap_usd", "reason"})
        st, p = call("broker_live_guard_put", match={"id": "binance-demo"},
                     body={"live_flag": True, "capital_cap_usd": 1000, "reason": "testing the guard"})
        self.assertEqual((st, p["settings"]["live_flag"]), (200, True))
        st, p = call("broker_live_guard_check", match={"id": "binance-demo"},
                     body={"confirm": "GO LIVE BINANCE-DEMO WITH REAL MONEY"})
        self.assertEqual((st, p["allowed"], p["refusal"]), (200, False, MSG))
        self.assertEqual(p["failed"], ["strategy_live"])
        self.assertEqual(call("broker_live_guard_get", match={"id": "ibkr-live"})[0], 404)


if __name__ == "__main__":
    unittest.main()
