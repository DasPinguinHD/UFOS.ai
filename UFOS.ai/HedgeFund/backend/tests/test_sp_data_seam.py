"""Stage 3b data (SPEC §8.1/§8.2): hedgefund.db v3 migration, the cache's
two adjustment series, and data/seam.py — seam pass (constant factor), seam
fail (split missing in one source, noisy days) -> warning + no pre-seam rows,
no Tiingo row on/after the seam ever stored, SecretMissing without a key,
corporate actions merged without overlap. Temp hedgefund.db, fakes, offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_data_seam -v
"""
import math
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

try:
    from test_sp_data_support import TempDb, equity_row, utc
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import TempDb, equity_row, utc

from strategy_platform import secrets
from strategy_platform.core import calendars
from strategy_platform.core.errors import InvalidRequest, SecretMissing
from strategy_platform.data import pit, seam as seam_mod
from strategy_platform.data.cache import BarCache
from strategy_platform.data.providers import tiingo
from strategy_platform.storage import db as sp_db

KEY = "tiingo-test-key"
SPLIT_2015 = date(2015, 9, 1)            # 2:1 split inside Tiingo's pre-seam history


def weekdays(a, b):
    d, out = a, []
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


PRE = weekdays(date(2015, 6, 1), date(2015, 12, 31))
POST = calendars.trading_days(date(2016, 1, 4), date(2016, 5, 31))
OVERLAP = seam_mod.overlap_dates()


def true_price(i):
    return 50.0 + 0.05 * i + 0.3 * math.sin(i / 3.0)


class World:
    """Both sources over one true price path. Tiingo: raw prices with a 2:1 split
    on SPLIT_2015 (its response ends at the overlap end). Alpaca: split series =
    true / 2 (a later split after 2016 halves today's basis), all series = that × 0.98."""

    def __init__(self, *, tiingo_noise=None, hide_overlap_split=False, tiingo_missing=False,
                 alpaca_volume=2_000_000.0):
        self.alpaca_volume = alpaca_volume      # SIP consolidated volume (= Tiingo 1e6 shares / factor 0.5)
        self.tiingo_noise = tiingo_noise or {}
        self.hide_overlap_split = hide_overlap_split
        self.tiingo_missing = tiingo_missing
        self.calls = []
        self.index = {d: i for i, d in enumerate(PRE + POST)}

    def tiingo_rows(self, start, end):
        out = []
        split_day = OVERLAP[20] if self.hide_overlap_split else None
        for d in PRE + [x for x in POST if x <= seam_mod.overlap_end()]:
            if not (start <= d <= end):
                continue
            p = true_price(self.index[d]) * self.tiingo_noise.get(d, 1.0)
            raw = p * 2 if d < SPLIT_2015 else p
            if split_day and d >= split_day:
                raw = raw / 2                     # a real split the source does not report
            out.append({"date": d, "open": raw, "high": raw * 1.01, "low": raw * 0.99, "close": raw,
                        "volume": 1_000_000.0 / (2 if d < SPLIT_2015 else 1), "adj_open": p * 0.98,
                        "adj_high": p * 0.98 * 1.01, "adj_low": p * 0.98 * 0.99, "adj_close": p * 0.98,
                        "adj_volume": 1_000_000.0, "div_cash": {date(2015, 7, 1): 0.4, date(2015, 10, 1): 0.5,
                                                                OVERLAP[30]: 0.2}.get(d, 0.0),
                        "split_factor": 2.0 if d == SPLIT_2015 else 1.0})
        return out

    def alpaca_bars(self, symbol, start, end, adjustment, source="alpaca_sip"):
        out = []
        for d in POST:
            ts = tiingo.bar_ts(d)                  # NY midnight (EST in winter, EDT in summer)
            row = equity_row(d.isoformat(), 1.0, symbol=symbol, ts=ts, known_at=pit.known_at_iso("equity", "1Day", ts))
            if not (start <= datetime.fromisoformat(row["ts"]) <= end):
                continue
            p = true_price(self.index[d]) / 2 * (0.98 if adjustment == "all" else 1.0)
            vol = self.alpaca_volume(d) if callable(self.alpaca_volume) else self.alpaca_volume
            row.update(open=p, high=p * 1.01, low=p * 0.99, close=p, volume=vol, source=source)
            out.append(row)
        return out

    def fetch(self, data_type, op, *args, **kw):
        self.calls.append((data_type, op, args, kw.get("adjustment")))
        if data_type == "equity_bars_pre2016":
            if self.tiingo_missing:
                raise tiingo.UnknownSymbol(args[0])
            return self.tiingo_rows(args[1], args[2])
        if data_type in ("equity_bars", "equity_bars_history"):      # IEX (live) / SIP (history)
            symbol, _tf, start, end = args
            return self.alpaca_bars(symbol, start, end, kw.get("adjustment", "all"),
                                    "alpaca_sip" if data_type == "equity_bars_history" else "alpaca")
        if data_type == "corporate_actions":
            return [{"symbol": "SPY", "kind": "cash_dividend", "ex_date": "2016-03-01", "rate": 0.3,
                     "old_rate": None, "new_rate": None, "process_date": None, "payable_date": "2016-03-15",
                     "special": False, "source": "alpaca"},
                    {"symbol": "SPY", "kind": "forward_split", "ex_date": "2016-04-15", "rate": None,
                     "old_rate": 1.0, "new_rate": 2.0, "process_date": None, "payable_date": None,
                     "special": None, "source": "alpaca"}]
        raise AssertionError((data_type, op))


def src_for(data_type):
    """The registry's configured sources (user decision 2026-10-06)."""
    return "alpaca_sip" if data_type in ("equity_bars_history", "equity_volume") else "alpaca"


class SeamCase(unittest.TestCase):
    def setUp(self):
        self._db = TempDb()
        self.db = self._db.__enter__()
        self.addCleanup(self._db.__exit__, None, None, None)
        secrets.set_store(secrets.MemoryStore({secrets.target("tiingo-api-key"): KEY}))
        self.addCleanup(secrets.set_store, None)
        self.now = utc(2016, 6, 1, 21, 0)

    def service(self, world):
        cache = BarCache(fetch=world.fetch, configured_source=src_for, clock=lambda: self.now)
        return seam_mod.Seam(cache=cache, fetch=world.fetch, clock=lambda: self.now)

    def tiingo_stored(self, symbol="SPY"):
        return [r for adj in ("all", "split") for r in self.db.query_bars(symbol, "1Day", None, None, "tiingo",
                                                                          adjustment=adj)]


class MigrationTest(unittest.TestCase):
    def test_v2_bars_survive_the_rebuild_as_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hedgefund.db")
            conn = sqlite3.connect(path)
            for version, statements in sp_db.MIGRATIONS[:2]:
                for sql in statements:
                    conn.execute(sql)
            conn.execute("PRAGMA user_version = 2")
            conn.execute("INSERT INTO bars VALUES ('SPY','1Day','2026-10-01T04:00:00+00:00',1,2,0.5,1.5,100,1.2,7,"
                         "'alpaca','2026-10-01T20:00:00+00:00','2026-10-02T00:00:00+00:00')")
            conn.commit()
            conn.close()
            try:
                self.assertEqual(sp_db.init(path), sp_db.SCHEMA_VERSION)
                rows = sp_db.query_bars("SPY", "1Day", None, None, "alpaca")
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]["close"], rows[0]["trade_count"], rows[0]["adjustment"]), (1.5, 7, "all"))
                # the same bar may now exist once per adjustment series
                sp_db.upsert_bars([dict(rows[0], close=0.75, adjustment="split")])
                self.assertEqual(sp_db.query_bars("SPY", "1Day", None, None, "alpaca")[0]["close"], 1.5)
                self.assertEqual(sp_db.query_bars("SPY", "1Day", None, None, "alpaca", adjustment="split")[0]["close"],
                                 0.75)
                self.assertEqual(sp_db.init(path), sp_db.SCHEMA_VERSION)                      # idempotent
                info = sp_db.storage_info()
                self.assertEqual((info["rows"]["bars"], info["rows"]["corporate_actions"], info["rows"]["seam_checks"]),
                                 (2, 0, 0))
                with self.assertRaises(ValueError):
                    sp_db.upsert_bars([dict(rows[0], adjustment="raw")])
            finally:
                sp_db.close()

    def test_failed_migration_keeps_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.db")
            conn = sqlite3.connect(path)
            for version, statements in sp_db.MIGRATIONS[:2]:
                for sql in statements:
                    conn.execute(sql)
            conn.execute("PRAGMA user_version = 2")
            conn.execute("CREATE TABLE bars_v3 (x)")       # makes the v3 rebuild fail half-way
            conn.execute("INSERT INTO bars (symbol,timeframe,ts,open,high,low,close,source,known_at,fetched_at) "
                         "VALUES ('SPY','1Day','t',1,1,1,1,'alpaca','k','f')")
            conn.commit()
            with self.assertRaises(sqlite3.DatabaseError):
                sp_db.migrate(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='corporate_actions'")
                             .fetchone()[0], 0)
            conn.close()


class CacheSeriesTest(SeamCase):
    def test_series_are_cached_separately_never_mixed(self):
        world = World()
        cache = BarCache(fetch=world.fetch, configured_source=lambda dt: "alpaca", clock=lambda: self.now)
        a = cache.get_bars("SPY", "1Day", utc(2016, 1, 4), utc(2016, 2, 1))
        s = cache.get_bars("SPY", "1Day", utc(2016, 1, 4), utc(2016, 2, 1), adjustment="split")
        self.assertEqual(len(a), len(s))
        self.assertTrue(all(abs(x["close"] / y["close"] - 0.98) < 1e-12 for x, y in zip(a, s)))
        self.assertEqual({r["adjustment"] for r in a} | {r["adjustment"] for r in s}, {"all", "split"})
        self.assertEqual([c[3] for c in world.calls], [None, "split"])      # 'all' keeps the stage-1 call shape
        n = len(world.calls)
        self.assertEqual(cache.get_bars("SPY", "1Day", utc(2016, 1, 4), utc(2016, 2, 1), adjustment="split"), s)
        self.assertEqual(len(world.calls), n)                                   # served from hedgefund.db
        self.assertEqual(self.db.query_bars("SPY", "1Day", None, None, "alpaca")[0]["close"], a[0]["close"])
        with self.assertRaises(ValueError):
            cache.get_bars("BTC-USD", "1Day", utc(2016, 1, 4), utc(2016, 2, 1), adjustment="split")
        with self.assertRaises(ValueError):
            cache.get_bars("SPY", "1Day", utc(2016, 1, 4), utc(2016, 2, 1), adjustment="raw")


class SeamPassTest(SeamCase):
    def test_constant_factor_scales_pre_seam_rows(self):
        world = World()
        progress = []
        rows, info = self.service(world).history("SPY", "split", date(2015, 6, 1), date(2016, 2, 1),
                                                 progress=progress.append)
        self.assertTrue(info["seam"]["ok"], info["seam"]["stats"]["failed"])
        self.assertAlmostEqual(info["seam"]["factor"], 0.5, places=12)
        self.assertEqual(info["sources"], ["tiingo", "alpaca_sip"])
        pre = [r for r in rows if r["source"] == "tiingo"]
        post = [r for r in rows if r["source"] == "alpaca_sip"]
        self.assertEqual(len(pre) + len(post), len(rows))
        self.assertEqual(len(pre), len(PRE))
        self.assertTrue(all(seam_mod.bar_date(r) < seam_mod.SEAM_DATE for r in pre))
        self.assertTrue(all(seam_mod.bar_date(r) >= seam_mod.SEAM_DATE for r in post))
        for r in pre:                                 # true/2 on Alpaca's basis, across the 2015 split
            self.assertAlmostEqual(r["close"], true_price(world.index[seam_mod.bar_date(r)]) / 2, places=9)
        self.assertAlmostEqual(pre[-1]["volume"], 2_000_000.0)                  # 1e6 ÷ 0.5
        self.assertEqual(rows, sorted(rows, key=lambda r: r["ts"]))
        self.assertEqual(info["warnings"], [])
        self.assertTrue(info["seam"]["stats"]["volume"]["ok"])
        self.assertAlmostEqual(info["seam"]["stats"]["volume"]["ratio_median"], 1.0)
        self.assertEqual(info["licences"], {"tiingo": "personal_use", "alpaca_sip": "personal_use"})
        self.assertTrue(progress and "Tiingo" in progress[0]["message"])
        rows_all, info_all = self.service(world).history("SPY", "all", date(2015, 6, 1), date(2016, 2, 1))
        self.assertAlmostEqual(info_all["seam"]["factor"], 0.5, places=12)
        self.assertEqual(sum(1 for c in world.calls if c[0] == "equity_bars_pre2016"), 1)   # one request per symbol
        self.assertEqual(len(self.db.seam_checks_list("SPY")), 2)
        self.service(world).history("SPY", "split", date(2015, 6, 1), date(2016, 2, 1))
        self.assertEqual(len(self.db.seam_checks_list("SPY")), 2)            # unchanged result: no new row

    def test_no_tiingo_row_on_or_after_the_seam_is_stored(self):
        world = World()
        self.service(world).history("SPY", "all", date(2015, 6, 1), date(2016, 5, 31))
        stored = self.tiingo_stored()
        self.assertEqual(len(stored), 2 * len(PRE))
        self.assertTrue(all(r["ts"] < "2016-01-04" for r in stored))
        acts = self.db.query_corporate_actions("SPY", "tiingo")
        self.assertEqual([a["ex_date"] for a in acts], ["2015-07-01", "2015-09-01", "2015-10-01"])
        svc = self.service(world)
        with self.assertRaises(ValueError):
            svc._store_pre_seam("SPY", [dict(stored[0], ts="2016-01-04T05:00:00+00:00")], [])

    def test_range_after_the_seam_needs_no_tiingo(self):
        secrets.set_store(secrets.MemoryStore({}))
        world = World()
        rows, info = self.service(world).history("SPY", "split", date(2016, 1, 4), date(2016, 2, 1))
        self.assertEqual((info["sources"], info["seam"]), (["alpaca_sip"], None))
        self.assertFalse(any(c[0] == "equity_bars_pre2016" for c in world.calls))


class SeamFailTest(SeamCase):
    def assert_no_pre_seam(self, rows, info, symbol="SPY"):
        self.assertFalse(info["seam"]["ok"])
        self.assertTrue(all(r["source"] == "alpaca_sip" for r in rows))
        warn = [w for w in info["warnings"] if w["code"] == "seam_mismatch"]
        self.assertEqual(len(warn), 1)
        self.assertIn("no data before 2016-01-04", warn[0]["message"])
        self.assertTrue(warn[0]["stats"]["failed"])
        self.assertFalse(self.db.seam_check_latest(symbol, info["adjustment"])["ok"])

    def test_split_missing_in_one_source(self):
        rows, info = self.service(World(hide_overlap_split=True)).history("SPY", "split", date(2015, 6, 1),
                                                                         date(2016, 2, 1))
        self.assert_no_pre_seam(rows, info)
        self.assertGreater(info["seam"]["stats"]["max_abs_return_diff"], 0.4)

    def test_noisy_days(self):
        noise = {d: (1.004 if i % 2 else 0.996) for i, d in enumerate(OVERLAP[::6])}   # 10 noisy days of 60
        rows, info = self.service(World(tiingo_noise=noise)).history("SPY", "split", date(2015, 6, 1),
                                                                     date(2016, 2, 1))
        self.assert_no_pre_seam(rows, info)
        self.assertLess(info["seam"]["stats"]["agree_share"], 0.95)

    def test_unknown_ticker_is_a_warning_not_a_guess(self):
        rows, info = self.service(World(tiingo_missing=True)).history("SPY", "all", date(2015, 6, 1),
                                                                      date(2016, 2, 1))
        self.assertTrue(all(r["source"] == "alpaca_sip" for r in rows))
        self.assertEqual([w["code"] for w in info["warnings"]], ["no_pre_seam_data"])

    def test_without_tiingo_key_pre_seam_raises(self):
        secrets.set_store(secrets.MemoryStore({}))
        world = World()
        with self.assertRaises(SecretMissing) as cm:
            self.service(world).history("SPY", "all", date(2015, 6, 1), date(2016, 2, 1))
        self.assertIn("Tiingo API key is not set", cm.exception.user_message)
        self.assertEqual(world.calls, [])
        secrets.set_store(secrets.MemoryStore({secrets.target("tiingo-api-key"): KEY}))
        with self.assertRaises(InvalidRequest):
            self.service(world).history("SPY", "all", date(1999, 1, 4), date(2016, 2, 1))


class CheckSeamTest(unittest.TestCase):
    def test_pure_check(self):
        days = [d.isoformat() for d in OVERLAP]
        t = {d: 10.0 + i * 0.01 for i, d in enumerate(days)}
        res = seam_mod.check_seam(t, {d: v * 3 for d, v in t.items()})
        self.assertTrue(res["ok"])
        self.assertAlmostEqual(res["factor"], 3.0)
        short = seam_mod.check_seam({d: t[d] for d in days[:20]}, {d: t[d] for d in days[:20]})
        self.assertFalse(short["ok"])
        self.assertIn("common daily returns", short["stats"]["failed"][0])
        one_bad = dict(t)
        one_bad[days[30]] *= 1.03                       # one 3 % day breaks the single-day limit
        self.assertFalse(seam_mod.check_seam(one_bad, t)["ok"])


class CorporateActionsTest(SeamCase):
    def test_merge_without_overlap_and_split_basis(self):
        world = World()
        rows, info = self.service(world).corporate_actions("SPY", date(2015, 6, 1), date(2016, 5, 31))
        got = [(a["ex_date"], a["kind"], a["source"]) for a in rows]
        self.assertEqual(got, [("2015-07-01", "cash_dividend", "tiingo"), ("2015-09-01", "forward_split", "tiingo"),
                               ("2015-10-01", "cash_dividend", "tiingo"), ("2016-03-01", "cash_dividend", "alpaca"),
                               ("2016-04-15", "forward_split", "alpaca")])
        adj = {a["ex_date"]: a["cash_amount_split_adj"] for a in rows}
        self.assertAlmostEqual(adj["2015-07-01"], 0.4 / 2 * 0.5)        # before the 2015 split, × seam factor
        self.assertAlmostEqual(adj["2015-10-01"], 0.5 * 0.5)
        self.assertAlmostEqual(adj["2016-03-01"], 0.3 / 2)              # Alpaca: ÷ the later 2:1 split
        self.assertIsNone(adj["2016-04-15"])
        self.assertEqual(info["sources"], ["tiingo", "alpaca"])
        self.assertTrue(info["seam"]["ok"])
        self.assertNotIn(OVERLAP[30].isoformat(), [a["ex_date"] for a in rows])
        alp = self.db.query_corporate_actions("SPY", "alpaca")
        self.assertEqual(alp[0]["pay_date"], "2016-03-15")
        self.service(world).corporate_actions("SPY", date(2016, 1, 4), date(2016, 5, 31))
        self.assertEqual(sum(1 for c in world.calls if c[0] == "corporate_actions"), 1)   # refreshed once per day

    def test_failed_seam_drops_pre_seam_actions(self):
        rows, info = self.service(World(hide_overlap_split=True)).corporate_actions("SPY", date(2015, 6, 1),
                                                                                   date(2016, 5, 31))
        self.assertTrue(all(a["source"] == "alpaca" for a in rows))
        self.assertEqual([w["code"] for w in info["warnings"]], ["seam_mismatch"])


if __name__ == "__main__":
    unittest.main()
