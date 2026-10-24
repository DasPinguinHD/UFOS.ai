"""fix1023 round 2 (review 2026-10-23): sequential second incubation start → 409 "still running"; Shadow notes truly
optional; the read-only US ticker check for the mapping dialog + truthful mapping provenance (confirmed_via, v24);
positive function-level tests without confirm words (connect, resolve); the scheduled check that produced a pause is
reported as "check"; one pause event per pause (check vs force-decay race); no trailing ": " for empty notes; the
run detail keeps the decay decisions after a demote. Temp DBs, fakes, offline."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest

try:
    from sp_broker_testkit import BrokerCase
    from sp_monitor_testkit import MonCase, after_close, write_returns
    from sp_trading_testkit import FRI, make_strategy
except ImportError:  # pragma: no cover
    from tests.sp_broker_testkit import BrokerCase
    from tests.sp_monitor_testkit import MonCase, after_close, write_returns
    from tests.sp_trading_testkit import FRI, make_strategy

import sandbox_profile as sp
from strategy_platform.api import routes
from strategy_platform.live import incubation
from strategy_platform.monitoring import decay
from strategy_platform.monitoring.shadow import service as shadow, symbols
from strategy_platform.storage import db

SPGI, AAPL = "US78409V1044", "US0378331005"
ASSETS = [
    {"symbol": "SPGI", "name": "S&P Global Inc.", "asset_class": "us_equity", "exchange": "NYSE", "tradable": True,
     "status": "active"},
    {"symbol": "AAPL", "name": "Apple Inc.", "asset_class": "us_equity", "exchange": "NASDAQ", "tradable": True,
     "status": "active"},
    {"symbol": "PINKY", "name": "Pink sheet", "asset_class": "us_equity", "exchange": "OTC", "tradable": True,
     "status": "active"},
]


def call(name, body=None, match=None, query=None):
    status, payload = asyncio.run(getattr(routes.V2Api(), name)(query or {}, routes.NO_BODY if body is None else body,
                                                                 match or {}))
    routes.dumps(payload)
    return status, payload


def sandbox(case):
    root = tempfile.mkdtemp(prefix="fix1023r2-")
    case.addCleanup(shutil.rmtree, root, True)
    os.makedirs(os.path.join(root, "sandbox"))
    with open(os.path.join(root, sp.MARKER_NAME), "w", encoding="utf-8") as f:
        json.dump({"mode": "sandbox", "created_at": "2026-10-23T12:00:00+00:00", "files": {}}, f)
    sp.use_root(root)
    case.addCleanup(sp.use_root, None)


def decay_alert_count(sid):
    with db._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM mon_alerts WHERE kind = 'decay' AND strategy_id = ?",
                            (sid,)).fetchone()[0]


class IncubationRound2Test(MonCase):
    def test_sequential_second_start_is_409_still_running(self):
        sid = make_strategy()
        self.assertEqual(call("incubation_create", {"strategy_id": sid, "length_sessions": 20})[0], 201)
        st, p = call("incubation_create", {"strategy_id": sid, "length_sessions": 20})
        self.assertEqual((st, p["code"]), (409, "already_incubating"))
        self.assertIn("an incubation run of this strategy is still running", p["error"])
        self.assertEqual(len(incubation.run_rows(sid)), 1)

    def test_empty_note_texts_have_no_trailing_colon(self):
        sid = make_strategy()
        rid = incubation.start(sid, 20, "", now=FRI)["run"]["id"]
        incubation.abort(rid, "", "user")
        self.assertEqual(db.strategy_transitions_list(sid)[-1]["reason"], "incubation aborted")
        sid2 = make_strategy(name="second")
        rid2 = incubation.start(sid2, 20, "", now=FRI)["run"]["id"]
        sandbox(self)
        import sandbox_fixtures
        sandbox_fixtures.force_decay(sid2, now=FRI)
        decay.decide(sid2, "demote", "", now=FRI)
        self.assertEqual(incubation.end_event(rid2)["data"]["reason"], "demoted after the decay monitor paused it")
        # B3: the decisions stay reachable from the (ended) run's detail
        st, p = call("incubation_get", match={"id": rid2})
        self.assertEqual(st, 200)
        self.assertEqual([e["action"] for e in p["decay_events"]], ["paused", "demoted"])
        self.assertTrue(p["decay_events"][0]["synthetic"])
        self.assertEqual(p["decay_events"][0]["rule"], "drawdown")
        self.assertIn("ts", p["decay_events"][1])
        self.assertEqual(call("incubation_get", match={"id": rid})[1]["decay_events"], [])

    def test_scheduled_check_that_pauses_reports_check(self):
        sid, run = self.start_run()
        days = write_returns(run, [0.0005] * 21 + [-0.07, -0.07, -0.005, -0.07, -0.07])
        now = after_close(days[-1])
        self.assertTrue(decay.check_run(run, now)["paused"])
        dec = decay.monitor_view(run, now)["decay"]
        self.assertEqual((dec["state"], dec["last_check_source"]), ("paused", "check"))
        self.assertEqual(dec["last_check_day"], days[-1].isoformat())
        self.assertFalse(dec["paused"]["numbers"]["synthetic"])

    def test_one_pause_event_per_pause_when_racing(self):
        sid, run = self.start_run()
        a = decay.assess(decay.expected.basis(run), [-0.3], run["calendar"])
        a = dict(a, triggered=["drawdown"])
        first = decay._pause(run, a, "2026-10-05", "2026-10-05T21:00:00+00:00")
        second = decay._pause(run, a, "2026-10-05", "2026-10-05T21:00:01+00:00")     # the other path, too late
        self.assertTrue(second["existing"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual([e["action"] for e in decay.events(run_id=run["id"])], ["paused"])
        self.assertEqual(decay_alert_count(sid), 1)


class ShadowRound2Test(MonCase):
    def setUp(self):
        super().setUp()
        symbols.set_assets_source(lambda: ASSETS)
        self.addCleanup(symbols.set_assets_source, None)

    def test_notes_truly_optional(self):
        st, p = call("sh_classification_put", {"key": "DE0005140008", "asset_class": "share", "reason": "ok"})
        self.assertEqual(st, 200, p)
        st, p = call("sh_symbol_map_put", {"isin": SPGI, "symbol": "SPGI", "reason": "x"})
        self.assertEqual(st, 200, p)
        st, p = call("sh_symbol_map_put", {"isin": AAPL, "symbol": "AAPL", "reason": ""})
        self.assertEqual((st, p["mapping"]["reason"]), (200, None))
        st, p = call("sh_symbol_map_put", {"isin": AAPL, "symbol": "AAPL", "reason": "r" * 501})
        self.assertEqual((st, set(p["errors"])), (400, {"reason"}))

    def test_ticker_check_and_truthful_provenance(self):
        st, p = call("sh_symbol_check", query={"ticker": "spgi", "isin": SPGI})
        self.assertEqual(st, 200, p)
        self.assertEqual((p["asset_name"], p["exchange"], p["status"], p["usable"], p["mapped_to_isin"]),
                         ("S&P Global Inc.", "NYSE", "active", True, None))
        self.assertEqual((p["isin_valid"], p["isin_us"]), (True, True))
        st, p = call("sh_symbol_check", query={"ticker": "PINKY"})
        self.assertEqual((p["usable"], p["found"]), (False, True))
        self.assertIn("OTC", p["refused_reason"])
        self.assertFalse(call("sh_symbol_check", query={"ticker": "NOPE"})[1]["found"])
        self.assertEqual(call("sh_symbol_check", query={"ticker": "SPGI", "x": "1"})[0], 400)
        with db._connect() as conn:
            before = conn.execute("SELECT COUNT(*) FROM sh_symbol_maps").fetchone()[0]
        self.assertEqual(before, 0)                                         # the check never writes
        st, p = call("sh_symbol_map_put", {"isin": SPGI, "symbol": "SPGI"})             # no confirm flag
        m = p["mapping"]
        self.assertEqual((st, m["confirmed_via"], m["confirmed_us_listing"], m["asset_name"]),
                         (200, "dialog", False, "S&P Global Inc."))
        st, p = call("sh_symbol_map_put", {"isin": AAPL, "symbol": "AAPL", "confirm_us_listing": True})
        self.assertEqual((p["mapping"]["confirmed_via"], p["mapping"]["confirmed_us_listing"]), ("confirm_flag", True))
        with db._connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT isin, confirmed_us_listing, confirmed_via FROM sh_symbol_maps"
                                                  " ORDER BY id")]
        self.assertEqual(rows, [{"isin": SPGI, "confirmed_us_listing": 0, "confirmed_via": "dialog"},
                                {"isin": AAPL, "confirmed_us_listing": 1, "confirmed_via": "confirm_flag"}])
        p = call("sh_symbol_check", query={"ticker": "SPGI", "isin": AAPL})[1]
        self.assertEqual((p["usable"], p["mapped_to_isin"]), (False, SPGI))
        self.assertEqual(p["current_mapping"], {"symbol": "AAPL", "price_source": "alpaca_us"})

    def test_schema_v24_and_old_rows_read_as_flag(self):
        self.assertGreaterEqual(db.SCHEMA_VERSION, 24)
        with db._connect() as conn:
            conn.execute("INSERT INTO sh_symbol_maps (isin, symbol, exchange, asset_name, confirmed_us_listing, ts,"
                         " actor, reason) VALUES (?, 'SPGI', 'NYSE', 'S&P', 1, '2026-10-01T00:00:00+00:00', 'user',"
                         " NULL)", (SPGI,))
        m = symbols.mappings()[SPGI]
        self.assertEqual((m["confirmed_via"], m["confirmed_us_listing"]), ("confirm_flag", True))


class BrokerFunctionLevelTest(BrokerCase):
    def test_connect_resolve_disconnect_without_confirm(self):
        from strategy_platform.execution import accounts
        from strategy_platform.execution.brokers import reconcile, service
        out = service.connect("alpaca-paper", None, "", "user")
        self.assertEqual(out["broker"]["connection"]["status"], "connected")
        acc = accounts.get("alpaca-paper")
        self.alpaca.foreign_trade("QQQ", 1, 400.0)
        self.assertEqual(reconcile.run(acc, "cycle")["status"], "mismatch")
        res = reconcile.resolve(acc, "accept_broker", "", None, "user")
        self.assertEqual(res["reconciliation"]["status"], "ok")
        st, p = call("broker_disconnect", {}, {"id": "alpaca-paper"})
        self.assertEqual((st, p.get("code")), (409, "not_flat"), p)      # the accepted position is still held


if __name__ == "__main__":
    unittest.main()
