"""Market Research MR2a — bars (market_research/SPEC.md §7.1/§7.2): session-aligned aggregation
(09:30 / 04:00 anchors, DST weeks US vs EU, early close), model c (SIP up to sip_through, 15-min rule,
IEX tail), the 30-day 1m store, paging with ``before``, 1D/1W, crypto UTC alignment.
Fakes only (no network); temp market_research.db outside the repo.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_data_bars -v
"""
import unittest
from datetime import date, datetime, timedelta, timezone

try:
    from mr_data_testkit import FakeSources, TempStore, bar_row, utc
except ImportError:  # pragma: no cover - run as package
    from tests.mr_data_testkit import FakeSources, TempStore, bar_row, utc

from market_research import store
from market_research.bars import aggregate as agg
from market_research.bars import intraday, series
from market_research.data import DataService
from strategy_platform.core import calendars
from strategy_platform.core.clock import iso, to_utc

NOW = utc(2026, 10, 15, 15, 0)          # Thursday 11:00 EDT


def minutes_of(day, session="eth", feed="sip", vol=10.0):
    w = agg.session_window(day, session)
    out, t = [], w[0]
    while t < w[1]:
        out.append(agg.minute(bar_row(t, vol), feed))
        t += timedelta(minutes=1)
    return out


def service(now=NOW):
    clock = {"now": now}
    fs = FakeSources(lambda: clock["now"])
    return DataService(sources=fs, now=lambda: clock["now"]), fs, clock


class AnchorTest(unittest.TestCase):
    def test_rth_1h_and_4h_anchored_at_0930(self):
        d = date(2026, 10, 14)
        bars = agg.aggregate(minutes_of(d), "1H", "rth", "equity", sip_through=NOW)
        ny = [to_utc(b["t"]).astimezone(agg.NY).strftime("%H:%M") for b in bars]
        self.assertEqual(ny, ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"])
        last_start = to_utc(bars[-1]["t"])
        self.assertEqual(agg.bucket_end(last_start, "1H", "rth", "equity") - last_start, timedelta(minutes=30))
        self.assertEqual(bars[-1]["n"], 30 * 3)                    # 30 minutes in the last bucket
        four = agg.aggregate(minutes_of(d), "4H", "rth", "equity", sip_through=NOW)
        self.assertEqual([to_utc(b["t"]).astimezone(agg.NY).strftime("%H:%M") for b in four], ["09:30", "13:30"])
        self.assertTrue(all(b["final"] for b in four))

    def test_eth_1h_and_4h_anchored_at_0400(self):
        d = date(2026, 10, 14)
        hours = agg.aggregate(minutes_of(d), "1H", "eth", "equity", sip_through=NOW)
        self.assertEqual(len(hours), 16)
        self.assertEqual(to_utc(hours[0]["t"]).astimezone(agg.NY).strftime("%H:%M"), "04:00")
        self.assertEqual(to_utc(hours[-1]["t"]).astimezone(agg.NY).strftime("%H:%M"), "19:00")
        four = agg.aggregate(minutes_of(d), "4H", "eth", "equity", sip_through=NOW)
        self.assertEqual([to_utc(b["t"]).astimezone(agg.NY).strftime("%H:%M") for b in four],
                         ["04:00", "08:00", "12:00", "16:00"])

    def test_rth_drops_extended_minutes(self):
        d = date(2026, 10, 14)
        five = agg.aggregate(minutes_of(d), "5m", "rth", "equity", sip_through=NOW)
        self.assertEqual(len(five), 78)
        self.assertEqual(sum(b["v"] for b in five), 390 * 10.0)

    def test_dst_us_vs_eu_weeks(self):
        # spring 2026: US switches 2026-03-08, EU 2026-03-29 -> in the week of 03-09 New York is already EDT
        cases = [(date(2026, 3, 6), 14), (date(2026, 3, 9), 13), (date(2026, 3, 27), 13), (date(2026, 3, 30), 13),
                 # autumn 2026: EU switches back 2026-10-25, US 2026-11-01
                 (date(2026, 10, 26), 13), (date(2026, 10, 30), 13), (date(2026, 11, 2), 14)]
        for d, utc_hour in cases:
            with self.subTest(day=d):
                bars = agg.aggregate(minutes_of(d), "1H", "rth", "equity", sip_through=NOW)
                first = to_utc(bars[0]["t"])
                self.assertEqual((first.hour, first.minute), (utc_hour, 30))
                self.assertEqual(first.astimezone(agg.NY).strftime("%H:%M"), "09:30")
                eth = agg.aggregate(minutes_of(d), "4H", "eth", "equity", sip_through=NOW)
                self.assertEqual(to_utc(eth[0]["t"]).hour, utc_hour - 5)          # 04:00 NY

    def test_early_close_last_candle_shorter(self):
        d = date(2026, 11, 27)                                       # day after Thanksgiving: 13:00 close
        self.assertTrue(calendars.is_early_close(d))
        late = utc(2026, 12, 1)
        hours = agg.aggregate(minutes_of(d), "1H", "rth", "equity", sip_through=late)
        ny = [to_utc(b["t"]).astimezone(agg.NY).strftime("%H:%M") for b in hours]
        self.assertEqual(ny, ["09:30", "10:30", "11:30", "12:30"])
        last = to_utc(hours[-1]["t"])
        self.assertEqual(agg.bucket_end(last, "1H", "rth", "equity").astimezone(agg.NY).strftime("%H:%M"), "13:00")
        self.assertEqual(hours[-1]["n"], 30 * 3)
        four = agg.aggregate(minutes_of(d), "4H", "rth", "equity", sip_through=late)
        self.assertEqual(len(four), 1)                               # 09:30–13:00 only
        self.assertEqual(agg.bucket_end(to_utc(four[0]["t"]), "4H", "rth", "equity"),
                         calendars.session_bounds(d)[1])
        eth = agg.session_window(d, "eth")
        self.assertEqual(eth[1].astimezone(agg.NY).strftime("%H:%M"), "17:00")
        eth_hours = agg.aggregate(minutes_of(d), "1H", "eth", "equity", sip_through=late)
        self.assertEqual(to_utc(eth_hours[-1]["t"]).astimezone(agg.NY).strftime("%H:%M"), "16:00")

    def test_holiday_and_weekend_minutes_dropped(self):
        t = utc(2026, 11, 26, 15, 0)                                  # Thanksgiving
        self.assertIsNone(agg.bucket_start(t, "5m", "rth", "equity"))
        self.assertIsNone(agg.bucket_start(utc(2026, 10, 17, 15, 0), "5m", "eth", "equity"))


class ValuesTest(unittest.TestCase):
    def test_ohlcv_vwap_n(self):
        t0 = utc(2026, 10, 14, 13, 30)
        ms = [{"t": t0, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100, "vwap": 10.2, "n": 5, "feed": "sip"},
              {"t": t0 + timedelta(minutes=1), "o": 10.5, "h": 12, "l": 10, "c": 11, "v": 300, "vwap": 11.0,
               "n": 7, "feed": "sip"},
              # minute 2 absent (no trades) — never filled
              {"t": t0 + timedelta(minutes=3), "o": 11, "h": 11.5, "l": 8.5, "c": 9, "v": 100, "vwap": 9.5, "n": 1,
               "feed": "sip"}]
        b = agg.aggregate(ms, "5m", "rth", "equity", sip_through=NOW)
        self.assertEqual(len(b), 1)
        b = b[0]
        self.assertEqual((b["o"], b["h"], b["l"], b["c"], b["v"], b["n"]), (10, 12, 8.5, 9, 500, 13))
        self.assertAlmostEqual(b["vwap"], (10.2 * 100 + 11.0 * 300 + 9.5 * 100) / 500)
        self.assertEqual(b["t"], "2026-10-14T13:30:00+00:00")
        ms[1]["vwap"] = None
        self.assertIsNone(agg.aggregate(ms, "5m", "rth", "equity", sip_through=NOW)[0]["vwap"])
        one = agg.aggregate(ms, "1m", "rth", "equity", sip_through=NOW)
        self.assertEqual([x["t"][11:16] for x in one], ["13:30", "13:31", "13:33"])

    def test_tail_bucket_not_final_and_sip_final_rule(self):
        through = utc(2026, 10, 15, 14, 47)
        ms = [agg.minute(bar_row(utc(2026, 10, 15, 14, 40) + timedelta(minutes=i)), "sip") for i in range(7)]
        ms += [agg.minute(bar_row(utc(2026, 10, 15, 14, 47) + timedelta(minutes=i)), "iex_tail") for i in range(3)]
        b = agg.aggregate(ms, "5m", "rth", "equity", sip_through=through)
        self.assertEqual([(x["t"][11:16], x["feed"], x["final"]) for x in b],
                         [("14:40", "sip", True), ("14:45", "iex_tail", False)])
        b = agg.aggregate(ms[:7], "5m", "rth", "equity", sip_through=through)
        self.assertEqual(b[1]["feed"], "sip")
        self.assertFalse(b[1]["final"])               # 14:45–14:50 not complete in SIP yet

    def test_crypto_utc_alignment(self):
        t0 = utc(2026, 10, 14, 21, 0)
        ms = [agg.minute(bar_row(t0 + timedelta(minutes=i)), "crypto") for i in range(10 * 60)]
        four = agg.aggregate(ms, "4H", "rth", "crypto", now=NOW)
        self.assertEqual([x["t"][11:16] for x in four], ["20:00", "00:00", "04:00"])
        days = agg.aggregate(ms, "1D", "rth", "crypto", now=NOW)
        self.assertEqual([x["t"] for x in days], ["2026-10-14T00:00:00+00:00", "2026-10-15T00:00:00+00:00"])
        self.assertTrue(days[0]["final"])
        self.assertTrue(all(x["feed"] == "crypto" for x in four))
        self.assertEqual(agg.week_start(utc(2026, 10, 18, 23, 59), "crypto"), utc(2026, 10, 12))

    def test_weeks_labelled_monday_even_on_holiday(self):
        days = [agg.daily_bar({"ts": iso(agg.ny_midnight(d)), "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                               "volume": 10, "vwap": 1.2, "trade_count": 1}, "sip_daily")
                for d in calendars.trading_days("2026-09-07", "2026-09-18")]   # Mon 09-07 = Labor Day
        w = agg.weeks(days, "equity", NOW)
        self.assertEqual([x["t"] for x in w], [iso(agg.ny_midnight(date(2026, 9, 7))),
                                               iso(agg.ny_midnight(date(2026, 9, 14)))])
        self.assertEqual(w[0]["v"], 40)                               # 4 sessions
        self.assertTrue(w[0]["final"])
        partial = agg.weeks(days, "equity", utc(2026, 9, 17, 15))
        self.assertFalse(partial[-1]["final"])


class ModelCTest(unittest.TestCase):
    def test_sip_up_to_sip_through_then_iex_tail(self):
        with TempStore():
            ds, fs, _c = service()
            r = ds.bars("AAPL", "1m", "rth", None, None)
            through = intraday.sip_through(NOW)
            self.assertEqual(r["sip_through"], iso(through))
            self.assertEqual(through, utc(2026, 10, 15, 14, 45))
            for b in r["bars"]:
                if b["t"] < iso(through):
                    self.assertEqual((b["feed"], b["final"]), ("sip", True))
                else:
                    self.assertEqual((b["feed"], b["final"]), ("iex_tail", False))
            self.assertEqual(r["bars"][-1]["t"], "2026-10-15T14:59:00+00:00")
            self.assertEqual(len(r["bars"]), 780)
            ends = [to_utc(c[2]) for c in fs.calls if isinstance(c, tuple) and c[0] == "sip_1m"]
            self.assertTrue(ends and max(ends) <= NOW - timedelta(minutes=15))    # 15-min rule (fake asserts too)

    def test_1m_store_reused_and_tail_never_stored(self):
        with TempStore():
            ds, fs, clock = service()
            ds.bars("AAPL", "5m", "rth", None, None)
            n_sip = fs.calls.count("sip_1m")
            ds.bars("AAPL", "5m", "rth", None, None)
            self.assertEqual(fs.calls.count("sip_1m"), n_sip)          # same minute: nothing new
            clock["now"] = NOW + timedelta(minutes=10)
            ds.bars("AAPL", "5m", "rth", None, None)
            self.assertEqual(fs.calls.count("sip_1m"), n_sip + 1)      # only today's newly released minutes
            last = [c for c in fs.calls if isinstance(c, tuple) and c[0] == "sip_1m"][-1]
            self.assertEqual((last[1], last[2]), ("2026-10-15T14:45:00+00:00", "2026-10-15T14:55:00+00:00"))
            rows = store.bars_1m_query("AAPL", "iex", "2000-01-01", "2100-01-01")
            self.assertEqual(rows, [])

    def test_sip_failure_is_reported_no_iex_instead(self):
        from strategy_platform.core.errors import DataSourceUnavailable
        with TempStore():
            ds, fs, _c = service()
            fs.fail["sip_1m"] = DataSourceUnavailable("alpaca_sip", "equity_bars_history", "plan refused")
            with self.assertRaises(DataSourceUnavailable):
                ds.bars("AAPL", "5m", "rth", None, None)
            self.assertNotIn("iex_1m", fs.calls)


class PagingTest(unittest.TestCase):
    def test_paging_left_contiguous_until_retention(self):
        with TempStore():
            ds, _fs, _c = service()
            seen, before, pages = [], None, 0
            while True:
                r = ds.bars("AAPL", "15m", "rth", before, 200)
                ts = [b["t"] for b in r["bars"]]
                self.assertEqual(ts, sorted(ts))
                if seen:
                    self.assertLess(ts[-1], seen[0])
                seen = ts + seen
                pages += 1
                if not r["has_more_left"]:
                    break
                before = to_utc(ts[0])
                self.assertLess(pages, 20)
            self.assertEqual(len(seen), len(set(seen)))
            first_day = to_utc(seen[0]).astimezone(agg.NY).date()
            self.assertEqual(first_day, calendars.trading_days("2026-09-16", "2026-09-30")[0])  # retention bound
            n_days = len(calendars.trading_days("2026-09-16", "2026-10-14"))
            self.assertEqual(len(seen), n_days * 26 + 6)              # + today 09:30–11:00
            with self.assertRaises(series.BadParam) as cm:
                ds.bars("AAPL", "5m", "rth", utc(2026, 9, 10, 15), None)
            self.assertEqual(cm.exception.user_message, series.OLD_INTRADAY)

    def test_1h_older_than_retention_is_transient(self):
        with TempStore():
            ds, fs, _c = service()
            r = ds.bars("AAPL", "1H", "rth", utc(2026, 8, 1), 70)
            self.assertEqual(len(r["bars"]), 70)
            self.assertTrue(r["has_more_left"])
            self.assertLess(r["bars"][-1]["t"], "2026-08-01")
            self.assertEqual(store.bars_1m_query("AAPL", "sip", "2000-01-01", "2026-09-16"), [])
            self.assertEqual((fs.calls.count("sip_30m"), fs.calls.count("sip_1m")), (1, 0))   # review M4

    def test_4h_default_window_one_30m_call(self):
        with TempStore():
            ds, fs, _c = service()
            r = ds.bars("AAPL", "4H", "eth", None, None)
            self.assertEqual(fs.calls.count("sip_30m"), 1)          # ~100 old sessions in ONE call (32 bars each)
            self.assertLessEqual(fs.calls.count("sip_1m"), 3)        # the 30 stored days, ≤ 10 days per call
            self.assertEqual(len(r["bars"]), 126 * 4)

    def test_30m_history_equals_1m_aggregation(self):
        try:
            from mr_data_testkit import thirty_minute_rows
        except ImportError:  # pragma: no cover
            from tests.mr_data_testkit import thirty_minute_rows
        for d in (date(2026, 3, 9), date(2026, 11, 2), date(2026, 11, 27)):     # DST weeks + early close
            for session in ("rth", "eth"):
                w = agg.session_window(d, "eth")
                rows, t = [], w[0]
                while t < w[1]:
                    rows.append(bar_row(t, 5.0 + (t.minute % 7)))
                    t += timedelta(minutes=1)
                ones = [agg.minute(r, "sip") for r in rows]
                halves = [agg.minute(r, "sip") for r in thirty_minute_rows(rows)]
                for tf in ("1H", "4H"):
                    with self.subTest(day=d, session=session, tf=tf):
                        a = agg.aggregate(ones, tf, session, "equity", sip_through=NOW + timedelta(days=60))
                        b = agg.aggregate(halves, tf, session, "equity", sip_through=NOW + timedelta(days=60))
                        self.assertEqual(len(a), len(b))
                        for x, y in zip(a, b):
                            self.assertEqual({k: x[k] for k in x if k != "vwap"}, {k: y[k] for k in y if k != "vwap"})
                            self.assertAlmostEqual(x["vwap"], y["vwap"])

    def test_retention_prune(self):
        with TempStore():
            store.bars_1m_upsert("AAPL", "sip", [bar_row(utc(2026, 9, 1, 14))], iso(NOW))
            store.bars_1m_upsert("AAPL", "sip", [bar_row(utc(2026, 10, 14, 14))], iso(NOW))
            store.bars_1m_day_set("AAPL", "sip", "2026-09-01", iso(utc(2026, 9, 2)), iso(NOW))
            deleted = store.prune(NOW)
            self.assertEqual((deleted["bars_1m"], deleted["bars_1m_days"]), (1, 1))
            self.assertEqual(len(store.bars_1m_query("AAPL", "sip", "2000-01-01", "2100-01-01")), 1)

    def test_daily_paging_to_history_start_and_partial_today(self):
        with TempStore():
            ds, fs, clock = service()
            r = ds.bars("AAPL", "1D", "rth", None, None)
            self.assertEqual(len(r["bars"]), 504)
            today = r["bars"][-1]
            self.assertEqual(today["t"], iso(agg.ny_midnight(date(2026, 10, 15))))
            self.assertFalse(today["final"])
            self.assertTrue(all(b["final"] and b["feed"] == "sip_daily" for b in r["bars"][:-1]))
            daily_calls = [c for c in fs.calls if isinstance(c, tuple) and c[0] == "daily"]
            self.assertEqual(daily_calls[0][1:], ("all", iso(utc(2026, 10, 14, 20))))   # fresh: last close
            before, n = None, 0
            while True:
                r = ds.bars("AAPL", "1D", "rth", before, 2000)
                n += len(r["bars"])
                if not r["has_more_left"]:
                    break
                before = to_utc(r["bars"][0]["t"])
            self.assertEqual(r["bars"][0]["t"][:10], "2016-01-04")
            clock["now"] = utc(2026, 10, 15, 20, 20)                    # close + 20 min: SIP daily has today
            r = ds.bars("AAPL", "1D", "rth", None, 3)
            self.assertTrue(r["bars"][-1]["final"])
            self.assertEqual(r["bars"][-1]["feed"], "sip_daily")

    def test_weekly_since_2016(self):
        with TempStore():
            ds, _fs, _c = service()
            r = ds.bars("AAPL", "1W", "rth", None, None)
            self.assertEqual(r["bars"][0]["t"], iso(agg.ny_midnight(date(2016, 1, 4))))
            self.assertFalse(r["has_more_left"])
            self.assertFalse(r["bars"][-1]["final"])
            self.assertEqual(r["bars"][-1]["t"], iso(agg.ny_midnight(date(2026, 10, 12))))

    def test_before_before_history_start(self):
        with TempStore():
            ds, _fs, _c = service()
            r = ds.bars("AAPL", "1D", "rth", utc(2015, 6, 1), None)
            self.assertEqual((r["bars"], r["has_more_left"]), ([], False))


class SessionFlagTest(unittest.TestCase):
    """Bar ``s`` = pre | rth | post (bridge contract, extended-hours shading) from the bucket start."""

    def flags(self, d, tf="1m", session="eth"):
        return {b["t"]: b["s"] for b in agg.aggregate(minutes_of(d), tf, session, "equity", sip_through=NOW)}

    def test_boundaries_04_0930_16_20(self):
        f = self.flags(date(2026, 10, 14))                         # EDT: 04:00 = 08:00Z … 20:00 = 00:00Z
        self.assertEqual(f["2026-10-14T08:00:00+00:00"], "pre")     # 04:00 first ETH minute
        self.assertEqual(f["2026-10-14T13:29:00+00:00"], "pre")     # 09:29
        self.assertEqual(f["2026-10-14T13:30:00+00:00"], "rth")     # 09:30
        self.assertEqual(f["2026-10-14T19:59:00+00:00"], "rth")     # 15:59
        self.assertEqual(f["2026-10-14T20:00:00+00:00"], "post")    # 16:00
        self.assertEqual(f["2026-10-14T23:59:00+00:00"], "post")    # 19:59 last ETH minute
        self.assertNotIn("2026-10-15T00:00:00+00:00", f)            # 20:00: outside ETH
        self.assertEqual(len(f), 960)
        h = self.flags(date(2026, 10, 14), "1H")
        self.assertEqual([h[k] for k in sorted(h)], ["pre"] * 6 + ["rth"] * 6 + ["post"] * 4)   # 09:00 ETH hour = pre
        self.assertEqual(set(self.flags(date(2026, 10, 14), "5m", "rth").values()), {"rth"})

    def test_dst_weeks(self):
        for d, open_utc in ((date(2026, 3, 6), "14:30"), (date(2026, 3, 9), "13:30"), (date(2026, 10, 27), "13:30"),
                            (date(2026, 11, 2), "14:30")):
            with self.subTest(day=d):
                f = self.flags(d)
                hh, mm = map(int, open_utc.split(":"))
                o = datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc)
                self.assertEqual(f[iso(o)], "rth")
                self.assertEqual(f[iso(o - timedelta(minutes=1))], "pre")
                close = o + timedelta(hours=6, minutes=30)
                self.assertEqual(f[iso(close - timedelta(minutes=1))], "rth")
                self.assertEqual(f[iso(close)], "post")

    def test_early_close(self):
        f = self.flags(date(2026, 11, 27))                         # EST, close 13:00 = 18:00Z, ETH ends 17:00
        self.assertEqual(f["2026-11-27T17:59:00+00:00"], "rth")
        self.assertEqual(f["2026-11-27T18:00:00+00:00"], "post")
        self.assertEqual(max(f), "2026-11-27T21:59:00+00:00")
        h = self.flags(date(2026, 11, 27), "1H")
        self.assertEqual(h["2026-11-27T18:00:00+00:00"], "post")    # 13:00 ETH hour
        self.assertEqual(h["2026-11-27T17:00:00+00:00"], "rth")     # 12:00 hour spans the close: by its start

    def test_rest_and_absent_cases(self):
        with TempStore():
            ds, _fs, _c = service()
            r = ds.bars("AAPL", "15m", "eth", None, 200)
            self.assertTrue(all(b["s"] in ("pre", "rth", "post") for b in r["bars"]))
            self.assertEqual(r["bars"][-1]["s"], "rth")             # 10:45 ET tail bar
            for tf in ("1D", "1W"):
                self.assertTrue(all("s" not in b for b in ds.bars("AAPL", tf, None, None, 5)["bars"]))
            for tf in ("1m", "4H", "1D"):
                self.assertTrue(all("s" not in b for b in ds.bars("BTC-USD", tf, None, None, 5)["bars"]))
        self.assertIsNone(agg.session_flag(utc(2026, 11, 26, 15), "equity", "5m"))   # Thanksgiving

    def test_hub_live_candles_carry_flag(self):
        from market_research.hub.candles import LiveCandle
        since = utc(2026, 10, 15, 11, 0)
        for t, flag in ((utc(2026, 10, 15, 12, 0, 5), "pre"), (utc(2026, 10, 15, 14, 0, 5), "rth"),
                        (utc(2026, 10, 15, 21, 0, 5), "post")):
            lc = LiveCandle("AAPL", "5m", "eth", "equity")
            lc.apply_trade(t, 100.0, 1, since)
            self.assertEqual(lc.bar()["s"], flag)
        btc = LiveCandle("BTC-USD", "5m", "utc", "crypto")
        btc.apply_trade(utc(2026, 10, 15, 12, 0, 5), 100.0, 1, since)
        self.assertNotIn("s", btc.bar())


class CryptoBarsTest(unittest.TestCase):
    def test_crypto_intraday_and_days_utc(self):
        with TempStore():
            ds, fs, clock = service()
            r = ds.bars("BTC-USD", "4H", None, None, 12)
            self.assertEqual((r["session"], r["tz"], r["sip_through"]), ("utc", "UTC", None))
            self.assertEqual([b["t"][11:16] for b in r["bars"][-4:]], ["00:00", "04:00", "08:00", "12:00"])
            self.assertFalse(r["bars"][-1]["final"])
            d = ds.bars("BTC-USD", "1D", None, None, 3)
            self.assertEqual([b["t"] for b in d["bars"]], ["2026-10-13T00:00:00+00:00", "2026-10-14T00:00:00+00:00",
                                                           "2026-10-15T00:00:00+00:00"])
            self.assertEqual(d["bars"][1]["v"], 24 * 120.0)              # one full UTC day of hours
            m = ds.bars("BTC-USD", "1m", None, None, 30)
            self.assertEqual(m["bars"][-1]["t"], "2026-10-15T14:59:00+00:00")   # complete minutes only
            self.assertTrue(all(b["feed"] == "crypto" and b["final"] for b in m["bars"]))
            # coverage stays CRYPTO_SETTLE behind now: the last 2 minutes are fetched again
            clock["now"] = NOW + timedelta(minutes=1)
            ds.bars("BTC-USD", "1m", None, None, 30)
            last = [c for c in fs.calls if isinstance(c, tuple) and c[0] == "crypto_1m"][-1]
            self.assertEqual(last[1:], ("2026-10-15T14:58:00+00:00", "2026-10-15T15:01:00+00:00"))

    def test_crypto_old_intraday_rejected(self):
        with TempStore():
            ds, _fs, _c = service()
            with self.assertRaises(series.BadParam):
                ds.bars("BTC-USD", "5m", None, utc(2026, 9, 1), None)


if __name__ == "__main__":
    unittest.main()
