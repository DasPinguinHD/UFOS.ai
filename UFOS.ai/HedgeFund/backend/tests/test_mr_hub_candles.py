"""Market Research MR2b — live candles from trades (market_research/SPEC.md §7.2, §13 MR2b notes):
bucket boundaries (RTH/ETH anchors, last bucket, early close, DST weeks US vs EU, 1D/1W t, Monday holiday,
crypto UTC), agreement with the REST side's bars/aggregate.py, closing bars, seeds + stream minute bars
without double counting, vwap rules, rate limiter.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_hub_candles -v
"""
import unittest
from datetime import datetime, timedelta, timezone

from market_research.bars import aggregate as agg
from market_research.hub.candles import LiveCandle, RateLimiter, bucket_for

UTC = timezone.utc


def T(*a):
    return datetime(*a, tzinfo=UTC)


def iso(dt):
    return dt.isoformat(timespec="seconds")


class BucketTest(unittest.TestCase):
    def test_rth_eth_anchors_and_last_bucket(self):
        b = bucket_for(T(2026, 10, 15, 13, 34, 59), "5m", "rth", "equity")              # 09:34 EDT
        self.assertEqual((b.t, b.end), (T(2026, 10, 15, 13, 30), T(2026, 10, 15, 13, 35)))
        b = bucket_for(T(2026, 10, 15, 19, 45), "1H", "rth", "equity")                  # 15:45 -> 15:30–16:00
        self.assertEqual((b.t, b.end), (T(2026, 10, 15, 19, 30), T(2026, 10, 15, 20, 0)))
        self.assertEqual(bucket_for(T(2026, 10, 15, 17, 40), "4H", "rth", "equity").t, T(2026, 10, 15, 17, 30))
        self.assertIsNone(bucket_for(T(2026, 10, 15, 13, 0), "1m", "rth", "equity"))    # 09:00 pre-market
        self.assertIsNone(bucket_for(T(2026, 10, 15, 20, 0), "1m", "rth", "equity"))    # 16:00 = end (exclusive)
        self.assertEqual(bucket_for(T(2026, 10, 15, 13, 0), "1H", "eth", "equity").t, T(2026, 10, 15, 13, 0))
        self.assertEqual(bucket_for(T(2026, 10, 15, 21, 0), "4H", "eth", "equity").t, T(2026, 10, 15, 20, 0))
        self.assertIsNone(bucket_for(T(2026, 10, 17, 15, 0), "1m", "eth", "equity"))    # Saturday

    def test_early_close(self):
        # 2026-11-27 (day after Thanksgiving, EST): RTH ends 13:00, ETH 17:00
        b = bucket_for(T(2026, 11, 27, 17, 45), "1H", "rth", "equity")
        self.assertEqual((b.t, b.end), (T(2026, 11, 27, 17, 30), T(2026, 11, 27, 18, 0)))
        self.assertIsNone(bucket_for(T(2026, 11, 27, 18, 5), "1m", "rth", "equity"))
        self.assertIsNotNone(bucket_for(T(2026, 11, 27, 18, 5), "1m", "eth", "equity"))
        self.assertIsNone(bucket_for(T(2026, 11, 27, 22, 0), "1m", "eth", "equity"))
        self.assertEqual(bucket_for(T(2026, 11, 27, 17, 0), "1D", "rth", "equity").end, T(2026, 11, 27, 18, 0))

    def test_dst_weeks_us_vs_eu(self):
        # US DST starts 2026-03-08, EU 2026-03-29: the open moves in UTC on US dates only
        self.assertEqual(bucket_for(T(2026, 3, 6, 14, 30), "1m", "rth", "equity").t, T(2026, 3, 6, 14, 30))
        self.assertIsNone(bucket_for(T(2026, 3, 6, 13, 30), "1m", "rth", "equity"))
        self.assertEqual(bucket_for(T(2026, 3, 9, 13, 30), "1m", "rth", "equity").t, T(2026, 3, 9, 13, 30))
        self.assertEqual(bucket_for(T(2026, 3, 30, 13, 30), "1H", "rth", "equity").t, T(2026, 3, 30, 13, 30))
        self.assertEqual(bucket_for(T(2026, 3, 6, 15, 0), "1D", "rth", "equity").t, T(2026, 3, 6, 5, 0))
        self.assertEqual(bucket_for(T(2026, 3, 9, 15, 0), "1D", "rth", "equity").t, T(2026, 3, 9, 4, 0))
        # November: US back to EST on 2026-11-01
        self.assertEqual(bucket_for(T(2026, 11, 2, 14, 30), "5m", "rth", "equity").t, T(2026, 11, 2, 14, 30))

    def test_week_label_and_monday_holiday(self):
        b = bucket_for(T(2026, 1, 20, 15, 0), "1W", "rth", "equity")                  # MLK Monday 2026-01-19
        self.assertEqual(b.t, T(2026, 1, 19, 5, 0))
        self.assertEqual(b.open, T(2026, 1, 20, 14, 30))
        self.assertEqual(b.end, T(2026, 1, 23, 21, 0))
        c = bucket_for(T(2026, 10, 15, 3, 0), "1W", "utc", "crypto")
        self.assertEqual((c.t, c.end), (T(2026, 10, 12), T(2026, 10, 19)))
        self.assertEqual(bucket_for(T(2026, 10, 15, 23, 59), "1D", "utc", "crypto").t, T(2026, 10, 15))
        self.assertEqual(bucket_for(T(2026, 10, 15, 3, 17), "4H", "utc", "crypto").t, T(2026, 10, 15, 0, 0))

    def test_agrees_with_rest_aggregation(self):
        for day in (T(2026, 3, 9), T(2026, 11, 27), T(2026, 10, 15), T(2026, 3, 6)):
            for minute in range(0, 24 * 60, 7):
                t = day + timedelta(minutes=minute)
                for tf in agg.INTRADAY_TFS:
                    for sess in agg.SESSIONS:
                        b = bucket_for(t, tf, sess, "equity")
                        self.assertEqual(b.t if b else None, agg.bucket_start(t, tf, sess, "equity"), (t, tf, sess))
                        if b:
                            self.assertEqual(b.end, agg.bucket_end(b.t, tf, sess, "equity"))
                    self.assertEqual(bucket_for(t, tf, "utc", "crypto").t, agg.bucket_start(t, tf, "rth", "crypto"))
            for sess in agg.SESSIONS:
                b = bucket_for(day + timedelta(hours=15), "1D", sess, "equity")
                if b:
                    self.assertEqual(b.t, agg.ny_midnight((day + timedelta(hours=15)).astimezone(agg.NY).date()))
                    self.assertEqual(bucket_for(day + timedelta(hours=15), "1W", sess, "equity").t,
                                     agg.week_start(day + timedelta(hours=15), "equity"))


class LiveCandleTest(unittest.TestCase):
    def test_trades_ohlcv_and_close(self):
        lc = LiveCandle("AAPL", "1m", "rth", "equity")
        since = T(2026, 10, 15, 13, 59)
        self.assertIsNone(lc.apply_trade(T(2026, 10, 15, 14, 0, 5), 100.0, 10, since))
        lc.apply_trade(T(2026, 10, 15, 14, 0, 30), 101.0, 10, since)
        lc.apply_trade(T(2026, 10, 15, 14, 0, 10), 99.0, 20, since)           # out of order
        self.assertTrue(lc.ready)
        bar = lc.bar()
        self.assertEqual(bar, {"t": "2026-10-15T14:00:00+00:00", "o": 100.0, "h": 101.0, "l": 99.0, "c": 101.0,
                               "v": 40.0, "vwap": (1000 + 1010 + 1980) / 40, "n": 3, "feed": "iex_tail",
                               "final": False, "s": "rth"})                       # MR2a follow-up: session flag
        closed = lc.apply_trade(T(2026, 10, 15, 14, 1, 0, 1000), 102.0, 1, since)
        self.assertEqual(closed["t"], "2026-10-15T14:00:00+00:00")
        self.assertFalse(closed["final"])                                       # equity: SIP replaces it later
        self.assertEqual(lc.bar()["t"], "2026-10-15T14:01:00+00:00")
        # late trade of the closed bucket beyond the grace window: dropped (counted), never a candle
        self.assertIsNone(lc.apply_trade(T(2026, 10, 15, 14, 0, 59), 50.0, 1, since, now=T(2026, 10, 15, 14, 1, 20)))
        self.assertEqual(lc.late_dropped, 1)
        self.assertEqual(lc.bar()["l"], 102.0)
        self.assertIsNone(lc.apply_trade(T(2026, 10, 15, 20, 30), 50.0, 1, since))      # after hours (rth)
        self.assertEqual(lc.bar()["t"], "2026-10-15T14:01:00+00:00")

    def test_crypto_final_on_close(self):
        lc = LiveCandle("BTC-USD", "1m", "utc", "crypto")
        lc.apply_trade(T(2026, 10, 15, 14, 0, 5), 60000.0, 0.1, T(2026, 10, 15, 13))
        self.assertTrue(lc.due(T(2026, 10, 15, 14, 1)))
        self.assertFalse(lc.due(T(2026, 10, 15, 14, 0, 59)))
        bar = lc.close()
        self.assertEqual((bar["feed"], bar["final"]), ("crypto", True))
        self.assertIsNone(lc.bar())

    def test_late_trade_after_clock_close_corrects_the_final_bar(self):
        # review H1: the hub closes by its clock; a trade of that bucket arriving ms later must not restart it
        lc = LiveCandle("BTC-USD", "1m", "utc", "crypto")
        since = T(2026, 10, 15, 13)
        lc.apply_trade(T(2026, 10, 15, 14, 0, 5), 100.0, 2, since)
        lc.apply_trade(T(2026, 10, 15, 14, 0, 40), 102.0, 1, since)
        first = lc.close()
        self.assertEqual((first["c"], first["v"], first["final"]), (102.0, 3, True))
        fixed = lc.apply_trade(T(2026, 10, 15, 14, 0, 59, 900000), 101.0, 1, since, now=T(2026, 10, 15, 14, 1, 0, 50000))
        self.assertEqual((fixed["t"], fixed["o"], fixed["h"], fixed["l"], fixed["c"], fixed["v"], fixed["n"],
                          fixed["final"]),
                         ("2026-10-15T14:00:00+00:00", 100.0, 102.0, 100.0, 101.0, 4, 3, True))
        self.assertIsNone(lc.bucket)                        # no new (one-trade) candle for the same t
        self.assertIsNone(lc.bar())
        # the stream's minute bar of that minute (always after the minute) replaces its trades -> corrected again
        fixed2 = lc.apply_bar({"symbol": "BTC-USD", "t": T(2026, 10, 15, 14, 0), "o": 100.0, "h": 103.0, "l": 99.0,
                               "c": 101.5, "v": 5.0, "vwap": 101.0, "n": 6}, now=T(2026, 10, 15, 14, 1, 3))
        self.assertEqual((fixed2["h"], fixed2["l"], fixed2["c"], fixed2["v"], fixed2["final"]),
                         (103.0, 99.0, 101.5, 5.0, True))
        self.assertIsNone(lc.apply_bar({"symbol": "BTC-USD", "t": T(2026, 10, 15, 14, 0), "o": 100.0, "h": 103.0,
                                        "l": 99.0, "c": 101.5, "v": 5.0, "vwap": 101.0, "n": 6},
                                       now=T(2026, 10, 15, 14, 1, 4)))     # unchanged: nothing re-sent
        # beyond the grace window: dropped and counted
        self.assertIsNone(lc.apply_trade(T(2026, 10, 15, 14, 0, 59), 90.0, 1, since, now=T(2026, 10, 15, 14, 1, 16)))
        self.assertEqual(lc.late_dropped, 1)
        # the next bucket still starts normally; a trade of an even older bucket is dropped
        self.assertIsNone(lc.apply_trade(T(2026, 10, 15, 14, 1, 20), 104.0, 1, since, now=T(2026, 10, 15, 14, 1, 20)))
        self.assertEqual(lc.bar()["t"], "2026-10-15T14:01:00+00:00")
        self.assertIsNone(lc.apply_trade(T(2026, 10, 15, 13, 59, 59), 1.0, 1, since, now=T(2026, 10, 15, 14, 1, 21)))
        self.assertEqual(lc.late_dropped, 2)

    def test_crypto_week_seed_from_hourly_rows_keeps_the_latest_close(self):
        # review M2: crypto 1W seeds finished days from HOURLY rows; the close is today's last trade
        lc = LiveCandle("BTC-USD", "1W", "utc", "crypto")
        now = T(2026, 10, 14, 14, 0, 30)                                   # Wednesday
        self.assertTrue(lc.begin_at(now))
        lc.seed_window(now)
        hours = [{"t": T(2026, 10, 12) + timedelta(hours=h), "o": 50.0 + h * 0.01, "h": 51.0 + h * 0.01,
                  "l": 49.0, "c": 50.0 + h * 0.01, "v": 1.0, "vwap": 50.0, "n": 1} for h in range(48)]
        today = [{"t": T(2026, 10, 14, 0) + timedelta(minutes=m), "o": 59.0, "h": 61.0, "l": 58.0,
                  "c": 59.0 + m / 1000, "v": 1.0, "vwap": 59.0, "n": 1} for m in range(14 * 60)]
        lc.apply_seed(today, days=hours)
        lc.apply_trade(T(2026, 10, 14, 14, 0, 40), 60.0, 1.0, None)
        bar = lc.bar()
        self.assertEqual((bar["t"], bar["o"], bar["c"], bar["l"], bar["h"]),
                         ("2026-10-12T00:00:00+00:00", 50.0, 60.0, 49.0, 61.0))
        self.assertEqual(bar["v"], 48 + 14 * 60 + 1)

    def test_not_continuous_needs_seed(self):
        lc = LiveCandle("AAPL", "5m", "rth", "equity")
        lc.apply_trade(T(2026, 10, 15, 14, 3, 31), 105.0, 5, T(2026, 10, 15, 14, 2))   # stream began mid-bucket
        self.assertTrue(lc.needs_seed)
        self.assertFalse(lc.ready)

    def test_seed_and_stream_minute_bars_without_double_counting(self):
        lc = LiveCandle("AAPL", "5m", "rth", "equity")
        self.assertTrue(lc.begin_at(T(2026, 10, 15, 14, 3, 30)))
        lc.apply_trade(T(2026, 10, 15, 14, 3, 31), 105.0, 5, None)                    # trades-only minute 14:03
        start, until = lc.seed_window(T(2026, 10, 15, 14, 4, 10))
        self.assertEqual((start, until), (T(2026, 10, 15, 14, 0), T(2026, 10, 15, 14, 4)))
        lc.apply_trade(T(2026, 10, 15, 14, 3, 50), 1.0, 1000, None)                   # < until: in the seed
        lc.apply_trade(T(2026, 10, 15, 14, 4, 20), 106.0, 3, None)
        seed = [{"t": T(2026, 10, 15, 14, m), "o": 100.0 + m, "h": 100.5 + m, "l": 99.5 + m, "c": 100.0 + m,
                 "v": 10.0, "vwap": 100.0 + m, "n": 2} for m in range(4)]
        lc.apply_seed(seed)
        self.assertTrue(lc.ready)
        bar = lc.bar()
        self.assertEqual((bar["o"], bar["c"], bar["v"], bar["n"], bar["l"]), (100.0, 106.0, 43.0, 9, 99.5))
        # the stream's minute bar for 14:04 replaces the trades of that minute
        lc.apply_bar({"symbol": "AAPL", "t": T(2026, 10, 15, 14, 4), "o": 106.0, "h": 107.0, "l": 105.0,
                      "c": 106.5, "v": 50.0, "vwap": None, "n": 4})
        bar = lc.bar()
        self.assertEqual((bar["h"], bar["v"], bar["vwap"]), (107.0, 90.0, None))   # a minute without vwap
        # a seed arriving late does not override a stream minute bar
        lc.apply_seed([{"t": T(2026, 10, 15, 14, 4), "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0,
                        "vwap": 1.0, "n": 1}])
        self.assertEqual(lc.bar()["h"], 107.0)

    def test_week_seed_days(self):
        lc = LiveCandle("AAPL", "1W", "rth", "equity")
        lc.begin_at(T(2026, 10, 15, 15, 0))
        lc.seed_window(T(2026, 10, 15, 15, 0))
        lc.apply_seed([{"t": T(2026, 10, 15, 14, 0), "o": 110.0, "h": 111.0, "l": 109.0, "c": 110.5, "v": 5.0,
                        "vwap": 110.0, "n": 1}],
                      days=[{"t": T(2026, 10, 12, 4), "o": 100.0, "h": 120.0, "l": 95.0, "c": 105.0, "v": 1000.0,
                             "vwap": 104.0, "n": 50}])
        bar = lc.bar()
        self.assertEqual((bar["t"], bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]),
                         ("2026-10-12T04:00:00+00:00", 100.0, 120.0, 95.0, 110.5, 1005.0))

    def test_rate_limiter(self):
        now = [0.0]
        rl = RateLimiter(4, monotonic=lambda: now[0])
        self.assertTrue(rl.ready("k"))
        rl.mark("k")
        now[0] = 0.2
        self.assertFalse(rl.ready("k"))
        self.assertTrue(rl.ready("other"))
        now[0] = 0.25
        self.assertTrue(rl.ready("k"))


if __name__ == "__main__":
    unittest.main()
