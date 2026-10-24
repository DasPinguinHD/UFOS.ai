"""Stage 8b review fixes 2026-10-19 (SPEC §18.14): redirects never followed (3xx → format error, not retried — a
redirect to api.binance.com / www.binance.com sends nothing there), NOTIONAL on limit orders, the step from ccxt's
precisionMode (TICK_SIZE 10 / 0.001, DECIMAL_PLACES, unknown refused), refused cancels counted in the kill, the
connect in ONE transaction, a missing baseline blocks the account until confirmed again, USDT accounts (own cap
setting, own BASE/USDT quotes, USDT messages), and the backup before the v23 migration. Offline, temp DBs."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

try:
    from sp_binance_testkit import FakeBinance, InvalidOrder, State, fast_gate, make_binance
    from sp_broker_testkit import KEYS, BrokerCase
except ImportError:  # pragma: no cover
    from tests.sp_binance_testkit import FakeBinance, InvalidOrder, State, fast_gate, make_binance
    from tests.sp_broker_testkit import KEYS, BrokerCase

from strategy_platform import secrets
from strategy_platform.execution import accounts, oms
from strategy_platform.execution.brokers import binance_demo, mirror, reconcile, service
from strategy_platform.execution.brokers.common import BrokerFormatError
from strategy_platform.execution.errors import TradingError
from strategy_platform.portfolio import settings as pf_settings
from strategy_platform.storage import db

BID = "binance-demo"
START = {"USDT": 5000.0, "BTC": 0.05, "ETH": 1.0, "BNB": 2.0}


class Resp:
    def __init__(self, status, location):
        self.status_code, self.text, self.history = status, "", []
        self.url = "https://demo-api.binance.com/api/v3/account"
        self.headers = {"Location": location, "Content-Type": "text/html"}


class RedirSession:
    def __init__(self, status, location):
        self.status, self.location, self.calls = status, location, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, dict(kw)))
        return Resp(self.status, self.location)


def adapter_with_session(status, location):
    st = State()
    sess = RedirSession(status, location)

    class Redirecting(FakeBinance):
        state = st

        def __init__(self, cfg):
            super().__init__(cfg)
            self.session = sess

        def fetch(self, url, method="GET", headers=None, body=None):     # like ccxt: one request per call
            self.state.sent.append((method, url))
            return self.session.request(method, url, headers={"X-MBX-APIKEY": self.apiKey}).text
    from types import SimpleNamespace
    return st, sess, binance_demo.BinanceDemoAdapter(sdk=SimpleNamespace(binance=Redirecting, __version__="4.5.84"),
                                                     gate_obj=fast_gate())


class AdapterFixesTest(unittest.TestCase):
    def setUp(self):
        secrets.set_store(secrets.MemoryStore(dict(KEYS)))
        self.addCleanup(secrets.set_store, None)

    def test_redirects_never_followed(self):
        for status, loc, host in ((301, "https://api.binance.com/api/v3/account", "api.binance.com"),
                                  (302, "https://www.binance.com/en/login", "www.binance.com")):
            st, sess, b = adapter_with_session(status, loc)
            with self.assertRaises(BrokerFormatError) as cm:
                b.account()
            msg = cm.exception.user_message
            self.assertIn(f"redirect (HTTP {status} → {host})", msg)
            self.assertIn("not followed", msg)
            self.assertEqual(len(sess.calls), 1)                       # not retried
            self.assertIs(sess.calls[0][2]["allow_redirects"], False)  # requests never follows it
            self.assertEqual({u.split("/")[2] for _m, u, _k in sess.calls}, {"demo-api.binance.com"})

    def test_limit_orders_checked_against_notional(self):
        st, b = make_binance()
        cases = ((dict(limit_price=100.0, qty=0.00002), "NOTIONAL: 0.00 USDT is below the minimum 5.00"),
                 (dict(limit_price=1001.0, qty=8999.0), "NOTIONAL: 9,007,999.00 USDT is above the maximum 9,000,000.00 USDT"))
        for kw, text in cases:
            a = b.submit({"client_order_id": "man-000000000001", "symbol": "BTC-USD", "side": "buy", "type": "limit",
                          "tif": "gtc", "origin": "manual", **kw})
            self.assertEqual(a["status"], "rejected")
            self.assertTrue(a["reject_reason"].startswith(text), a["reject_reason"])
        self.assertEqual(st.orders, {})

    def test_step_from_precision_mode(self):
        st, b = make_binance()
        mk = b.markets()
        mk["SOL/USDT"]["precision"]["amount"] = 10.0                   # TICK_SIZE: the value IS the step
        mk["ETH/USDT"]["precision"]["amount"] = 0.001
        self.assertEqual(b.round_qty("SOL-USD", 25.0), 20.0)
        self.assertAlmostEqual(b.round_qty("ETH-USD", 0.0025), 0.002)
        b._precision_mode = binance_demo.DECIMAL_PLACES
        mk["ETH/USDT"]["precision"]["amount"] = 3
        self.assertAlmostEqual(b.round_qty("ETH-USD", 0.0025), 0.002)
        b._precision_mode = 3                                          # SIGNIFICANT_DIGITS: refused, no guess
        with self.assertRaises(BrokerFormatError):
            b.instrument("ETH-USD")


class FlowFixesTest(BrokerCase):
    def bconnect(self):
        out = service.connect(BID, service.confirm_phrase(BID), "connect the demo", "user", dict(START))
        return accounts.get(BID), out

    def test_kill_reports_refused_cancels(self):
        acc, _ = self.bconnect()
        lim = self.bmanual(acc, "BTC-USD", "buy", 0.001, type="limit", limit_price=50000.0, tif="gtc")
        self.assertEqual(lim["state"], "accepted")
        ex = self.binance.exchange()

        def refuse(id, symbol=None, params=None):
            ex._req("private", "order", "DELETE")
            raise InvalidOrder("Order was not canceled due to cancel restrictions.")
        ex.cancel_order = refuse
        out = service.kill(acc, "kill with a refused cancel")
        self.assertFalse(out["ok"])
        self.assertEqual(len(out["refused"]), 1)
        self.assertEqual(out["refused"][0]["client_order_id"], lim["client_order_id"])
        self.assertIn("cancel restrictions", out["refused"][0]["reason"])
        self.assertIn("cancel(s) refused by the broker", out["error"])

    def test_connect_is_one_transaction(self):
        with mock.patch.object(pf_settings, "write", side_effect=RuntimeError("disk full (test)")):
            with self.assertRaises(RuntimeError):
                self.bconnect()
        with db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM br_accounts").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM br_connections").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pa_meta WHERE key = 'baseline'").fetchone()[0], 0)
        acc, out = self.bconnect()                                     # nothing half-written blocks a retry
        self.assertEqual(out["reconciliation"]["status"], "ok")

    def test_missing_baseline_blocks_until_confirmed(self):
        acc, _ = self.bconnect()
        acc.store.meta_set(mirror.BASELINE_KEY, None)                  # e.g. a restored / damaged database
        res = reconcile.run(acc, "manual", user=True)
        self.assertEqual(res["status"], "unreachable")
        self.assertIn("baseline missing", res["error"])
        st = service.broker_status(BID)
        self.assertTrue(st["baseline"]["missing"])
        self.assertTrue(st["can"]["connect"])
        self.assertIn("baseline missing", mirror.link_block_reason(acc))
        o = self.bmanual(acc, "BTC-USD", "buy", 0.001)
        self.assertEqual(o["state"], "rejected")
        self.assertEqual(self.bstate.orders, {})
        with self.assertRaises(ValueError):                            # never assumed empty: balances required
            service.connect(BID, service.confirm_phrase(BID), "confirm again", "user")
        out = service.connect(BID, service.confirm_phrase(BID), "confirm again", "user", dict(START))
        self.assertEqual(out["reconciliation"]["status"], "ok", out["reconciliation"])
        self.assertEqual(mirror.baseline(accounts.get(BID).store), START)
        self.assertFalse(service.broker_status(BID)["baseline"]["missing"])
        with self.assertRaises(TradingError) as cm:
            service.connect(BID, service.confirm_phrase(BID), "again", "user", dict(START))
        self.assertEqual(cm.exception.code, "already_connected")

    def test_usdt_cap_quotes_and_messages(self):
        acc, _ = self.bconnect()
        s = pf_settings.get(BID)
        self.assertEqual(s["currency"], "USDT")
        self.assertIn("max_order_notional_usdt", s["settings"])
        self.assertNotIn("max_order_notional_usd", s["settings"])
        self.assertEqual(s["bounds"]["max_order_notional_usdt"]["unit"], "USDT")
        self.assertNotIn("max_order_notional_usdt", pf_settings.get("paper-main")["settings"])
        _c, err = pf_settings.validate({"max_order_notional_usd": 500.0}, BID)
        self.assertIn("its money is USDT", err["max_order_notional_usd"])
        self.bstate.prices["BTC/USDT"] = 61000.0                       # the demo's own price ≠ the USD feed (60000)
        self.binance._prices.clear()
        self.assertEqual(accounts.quote_for(acc, "BTC-USD").price, 61000.0)
        pf_settings.put(BID, {"max_order_notional_usdt": 100.0}, "test cap")
        o = self.bmanual(acc, "BTC-USD", "buy", 0.002)
        self.assertEqual(o["state"], "rejected")
        self.assertIn("order notional 122.00 USDT exceeds the limit 100.00 USDT", o["reject_reason"])
        pf_settings.put(BID, {"max_order_notional_usdt": 1_000_000.0}, "test cap")
        o = self.bmanual(acc, "BTC-USD", "buy", 0.001)
        self.assertEqual(o["state"], "filled", o)
        view = acc.broker.account({"BTC-USD": accounts.quote_for(acc, "BTC-USD")})
        self.assertAlmostEqual(view.equity, 5000.0 - 61.0 + 0.000999 * 61000.0, places=4)


class BackupBeforeV23Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ufos-v23-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db.close()
        self.addCleanup(db.close)

    def make_v22(self, path):
        conn = sqlite3.connect(path, isolation_level=None)
        for version, statements in db.MIGRATIONS:
            if version > 22:
                break
            for sql in statements:
                sql(conn) if callable(sql) else conn.execute(sql)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        conn.execute("INSERT INTO br_accounts (id, broker, label, params_json, created_at, created_by) VALUES"
                     " ('alpaca-paper', 'alpaca_paper', 'A', '{}', 'then', 'user')")
        conn.commit()
        conn.close()

    def backups(self):
        folder = os.path.join(self.tmp, "backups")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def test_backup_once_before_v23(self):
        path = os.path.join(self.tmp, "hedgefund.db")
        self.make_v22(path)
        self.assertEqual(db.init(path), db.SCHEMA_VERSION)
        names = self.backups()          # since v25 (SPEC §22.3) a pre-v25 copy is written too (same v22 content)
        self.assertEqual([n.split("-")[0] for n in names], ["hedgefund.db.pre", "hedgefund.db.pre"])
        (name,) = [n for n in names if n.startswith("hedgefund.db.pre-v23-")]
        self.assertTrue(any(n.startswith("hedgefund.db.pre-v25-") for n in names))
        b = sqlite3.connect(os.path.join(self.tmp, "backups", name))
        self.assertEqual(b.execute("PRAGMA user_version").fetchone()[0], 22)
        self.assertEqual(b.execute("SELECT id FROM br_accounts").fetchall(), [("alpaca-paper",)])
        b.close()
        db.close()
        self.assertEqual(db.init(path), db.SCHEMA_VERSION)            # already migrated: no further copy
        self.assertEqual(len(self.backups()), 2)

    def test_no_backup_for_a_new_database(self):
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        self.assertEqual(self.backups(), [])

    def test_failed_backup_stops_the_migration(self):
        path = os.path.join(self.tmp, "hedgefund.db")
        self.make_v22(path)
        with mock.patch.object(db, "backup_before", side_effect=OSError("disk full (test)")):
            with self.assertRaises(Exception):
                db.init(path)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 22)
        conn.close()


if __name__ == "__main__":
    unittest.main()
