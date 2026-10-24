"""OS1 archive runs (SP §21.1–§21.3) on a fake clock + fake transport (no network): chain snapshot PIT stamping and
explicit parameters (feed=indicative, expiry bounds, page size ≤ 1000, per-endpoint timeouts), staleness / after-slot
/ missing fields / coverage → os_gaps (rows kept, nothing filled), late snapshots, OI (T+1 08:00 floor, early_oi,
stale_oi, corrections as new rows, unchanged values not repeated), FRED ("." gaps, revisions, missing series, key
missing / unreadable), ONE retry then stop, 5xx retried once, 429 stop, the own 30/min gate, the allow-list."""
import logging
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

try:
    from os_archive_testkit import FKEY, KID, SKEY, ArchiveCase, Clock, FakeApi, Sec
except ImportError:  # pragma: no cover
    from tests.os_archive_testkit import FKEY, KID, SKEY, ArchiveCase, Clock, FakeApi, Sec

from strategy_platform.options_archive import slots
from strategy_platform.options_archive.config import Config
from strategy_platform.options_archive.transport import Http, RateGate, Refused

D = datetime(2026, 10, 26, tzinfo=timezone.utc).date()      # Monday, New York on EDT
CFG = Config()


def slot(label, d=D):
    return slots.find(d, label, CFG)


class ChainTest(ArchiveCase):
    def start_at(self, s, offset_s=0.0, **api_kw):
        clock = Clock(s.start_utc(CFG) + timedelta(seconds=offset_s))
        api = FakeApi(clock, D, **api_kw)
        api.slot_ms = s.ms
        self.make(clock.now(), api)
        return self.arch

    def test_snapshot_ok_pit_and_params(self):
        s = slot("15:25")
        out = self.start_at(s).run(s)
        self.assertEqual((out["status"], out["rows"], out["gaps"], out["retries"]), ("ok", 96, 0, 0))
        rows = self.rows("SELECT * FROM os_chain_snapshots ORDER BY symbol")
        self.assertEqual(len(rows), 96)
        r = rows[0]
        self.assertEqual(r["quote_t_ms"], s.ms - 20_000)                       # valid_for = the quote time
        self.assertEqual(r["available_ms"], r["observed_ms"])
        self.assertTrue(s.start_utc(CFG).timestamp() * 1000 < r["observed_ms"] < s.ms)   # received before the slot
        self.assertEqual((r["bid"], r["ask"], r["bid_size"], r["ask_size"], r["daily_volume"], r["iv"]),
                         (1.0, 1.1, 5, 7, 321, 0.18))
        self.assertEqual(r["spot"], 600.0 if r["symbol"].startswith("SPY") else 500.0)
        run = self.rows("SELECT * FROM os_runs")[0]
        self.assertEqual((run["kind"], run["slot_label"], run["source"], run["feed"], run["spot_source"],
                          run["model_version"], run["assumptions_hash"]),
                         ("intraday", "15:25", "alpaca_data", "indicative", "alpaca_iex_trade", "raw",
                          CFG.assumptions_hash))
        snaps = [(parse_qs(urlparse(u).query), t) for _m, u, _h, t in self.api.calls if "/snapshots/" in u]
        for q, t in snaps:
            self.assertEqual((q["feed"], q["limit"], q["expiration_date_gte"], q["expiration_date_lte"], t),
                             (["indicative"], ["1000"], ["2026-10-26"], ["2026-12-10"], 20.0))
        self.assertEqual({t for _m, u, _h, t in self.api.calls if "/stocks/" in u}, {10.0})

    def test_staleness_gaps_rows_kept(self):
        s = slot("09:55")
        out = self.start_at(s, after_slot=5, stale=5, missing_greeks=5).run(s)
        self.assertEqual(out["status"], "partial")
        g = self.rows("SELECT kind, underlying, what FROM os_gaps ORDER BY id")
        kinds = sorted({(x["kind"], x["underlying"]) for x in g})
        self.assertEqual(kinds, [(k, u) for k in ("after_slot", "coverage", "field_missing", "stale_quote")
                                 for u in ("QQQ", "SPY")])
        cov = next(x for x in g if x["kind"] == "coverage" and x["underlying"] == "SPY")
        self.assertIn("37 of 52 contracts usable", cov["what"])
        self.assertEqual(len(self.rows("SELECT * FROM os_chain_snapshots")), 96)      # raw rows kept, readers apply
        self.assertEqual(len(self.rows("SELECT * FROM os_chain_snapshots WHERE delta IS NULL")), 10)  # not filled

    def test_late_snapshot(self):
        s = slot("09:55")
        out = self.start_at(s, offset_s=89.5).run(s)                          # started 0.5 s before the slot
        self.assertEqual(out["status"], "late")
        self.assertEqual(self.rows("SELECT kind FROM os_gaps"), [{"kind": "late"}])

    def test_no_spot_no_chain(self):
        s = slot("10:00")
        out = self.start_at(s, no_spot=("QQQ",)).run(s)
        self.assertEqual(out["status"], "partial")
        g = self.rows("SELECT kind, underlying FROM os_gaps")
        self.assertEqual(g, [{"kind": "not_collected", "underlying": "QQQ"}])
        self.assertEqual(len(self.rows("SELECT * FROM os_chain_snapshots WHERE symbol LIKE 'QQQ%'")), 0)


class OiTest(ArchiveCase):
    def setUp(self):
        super().setUp()
        s = slot("oi")
        self.s = s
        self.clock = Clock(s.utc)
        self.api = FakeApi(self.clock, D, oi_date="2026-10-23")
        self.make(self.clock.now(), self.api)

    def test_oi_floor_corrections_and_staleness(self):
        out = self.arch.run(self.s)
        self.assertEqual((out["status"], out["gaps"]), ("ok", 0))
        oi = self.rows("SELECT * FROM os_open_interest")
        self.assertEqual(len(oi), 104)                        # 2 expiries ≤ 45 d × 13 strikes × C/P × 2 symbols
        floor = int(datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)   # T+1 08:00 New York
        self.assertTrue(all(r["available_ms"] == max(r["observed_ms"], floor) and r["early_oi"] == 0 for r in oi))
        self.assertEqual({r["valid_for"] for r in oi}, {"2026-10-23"})
        self.assertEqual(len(self.rows("SELECT * FROM os_contracts")), 104)
        q = [parse_qs(urlparse(u).query) for _m, u, _h, _t in self.api.calls if "/contracts" in u]
        self.assertTrue(all(x["expiration_date_lte"] == ["2026-12-10"] and x["limit"] == ["1000"] for x in q))
        self.assertEqual({t for _m, u, _h, t in self.api.calls if "/contracts" in u}, {30.0})
        self.arch.run(self.s)                                  # same values: no new OI / contract rows
        self.assertEqual(len(self.rows("SELECT * FROM os_open_interest")), 104)
        self.assertEqual(len(self.rows("SELECT * FROM os_contracts")), 104)
        self.assertEqual(len(self.rows("SELECT * FROM os_contract_sightings")), 208)
        self.api.oi_value = 1500                               # a correction → NEW rows, later available_ms
        self.clock.advance(600)
        self.arch.run(self.s)
        rows = self.rows("SELECT symbol, open_interest, available_ms FROM os_open_interest WHERE symbol = ? "
                         "ORDER BY available_ms", (self.rows("SELECT symbol FROM os_contracts LIMIT 1")[0]["symbol"],))
        self.assertEqual([r["open_interest"] for r in rows], [1234, 1500])
        self.assertLess(rows[0]["available_ms"], rows[1]["available_ms"])

    def test_early_and_stale_oi(self):
        self.api.oi_date = "2026-10-26"                       # "today's" OI already at 08:30 — before its floor
        out = self.arch.run(self.s)
        self.assertEqual(out["status"], "partial")
        oi = self.rows("SELECT * FROM os_open_interest")
        floor = int(datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertTrue(all(r["early_oi"] == 1 and r["available_ms"] == floor for r in oi))
        g = self.rows("SELECT kind, what FROM os_gaps")
        self.assertEqual({x["kind"] for x in g}, {"stale_oi"})
        self.assertIn("not of the previous session (2026-10-23)", g[0]["what"])


class FredTest(ArchiveCase):
    def test_fred_values_gaps_revisions(self):
        s = slot("fred")
        clock = Clock(s.utc)
        api = FakeApi(clock, D, fred_missing=("VXNCLS",))
        self.make(clock.now(), api)
        out = self.arch.run(s)
        self.assertEqual(out["status"], "partial")
        f = self.rows("SELECT series_id, date, value FROM os_fred ORDER BY series_id, date")
        self.assertEqual(f, [{"series_id": "VIXCLS", "date": "2026-10-21", "value": None},
                             {"series_id": "VIXCLS", "date": "2026-10-22", "value": 17.1},
                             {"series_id": "VXVCLS", "date": "2026-10-22", "value": 19.4}])
        g = {(x["kind"], x["underlying"]) for x in self.rows("SELECT kind, underlying FROM os_gaps")}
        self.assertEqual(g, {("field_missing", "VIXCLS"), ("not_on_fred", "VXNCLS")})
        api.fred_values["VIXCLS"] = [("2026-10-22", "17.3"), ("2026-10-21", ".")]
        clock.advance(60)
        self.arch.run(s)
        rows = self.rows("SELECT value FROM os_fred WHERE series_id = 'VIXCLS' AND date = '2026-10-22' "
                         "ORDER BY available_ms")
        self.assertEqual([r["value"] for r in rows], [17.1, 17.3])            # revision = new row
        self.assertNotIn(FKEY, self.all_text())

    def test_fred_key_missing_or_unreadable(self):
        s = slot("fred")
        for sec, text in ((Sec({"alpaca-paper-key-id": KID, "alpaca-paper-secret-key": SKEY}), "no FRED key"),
                          (Sec(broken=("fred-api-key",)), "could not be read from the Credential Manager (OSError)")):
            self.make(s.utc, sec=sec)
            out = self.arch.run(s)
            self.assertEqual(out["status"], "failed")
            self.assertIn(text, out["error"])


class TransportBehaviourTest(ArchiveCase):
    def setUp(self):
        super().setUp()
        self.s = slot("15:25")

    def run_with(self, fail):
        clock = Clock(self.s.start_utc(CFG))
        api = FakeApi(clock, D, fail=fail)
        api.slot_ms = self.s.ms
        self.make(clock.now(), api)
        return self.arch.run(self.s)

    def test_one_retry_then_stop(self):
        out = self.run_with({"/v1beta1/options/snapshots": ["net", "net"]})
        self.assertEqual((out["status"], out["stopped"], out["exit_code"], out["retries"]), ("failed", "stop", 3, 1))
        self.assertEqual(out["error"], "network error on data.alpaca.markets/v1beta1/options/snapshots/SPY: "
                                       "ConnectionResetError: reset while talking to [url] (after 1 retry)")
        g = self.rows("SELECT kind FROM os_gaps")
        self.assertEqual(g, [{"kind": "network"}])
        self.assertEqual(self.rows("SELECT retries FROM os_runs")[0]["retries"], 1)

    def test_5xx_retried_once(self):
        out = self.run_with({"/v1beta1/options/snapshots": [503]})
        self.assertEqual((out["status"], out["retries"]), ("ok", 1))
        self.assertEqual(self.arch.http.retries[0]["reason"], "HTTP 503")

    def test_keys_rejected_stop_exit_2(self):
        out = self.run_with({"/v2/stocks": [401]})
        self.assertEqual((out["status"], out["stopped"], out["exit_code"]), ("failed", "stop", 2))
        self.assertIn("HTTP 401", out["error"])
        self.assertFalse(any("/snapshots/" in c[1] for c in self.api.calls))      # nothing else asked
        s = slot("oi")
        clock = Clock(s.start_utc(CFG))
        self.make(clock.now(), FakeApi(clock, D, fail={"/v2/options/contracts": [403]}))
        out = self.arch.run(s)
        self.assertEqual((out["status"], out["exit_code"]), ("failed", 2))

    def test_429_stops_at_once(self):
        out = self.run_with({"/v1beta1/options/snapshots": [429]})
        self.assertEqual((out["status"], out["stopped"], out["exit_code"], out["retries"]), ("failed", "rate_limited", 4, 0))
        i = next(k for k, c in enumerate(self.api.calls) if "/snapshots/" in c[1])
        self.assertEqual(len(self.api.calls), i + 1)

    def test_own_gate_30_per_minute(self):
        clock = Clock(self.s.start_utc(CFG))
        api = FakeApi(clock, D, page=2, latency=0.05)          # many small pages
        api.slot_ms = None
        self.make(clock.now(), api)
        stamps = []
        orig = api.__call__

        def timed(*a):
            stamps.append(self.clock.monotonic())                 # the archive's own clock (make() built it)
            return orig(*a)
        self.arch.http.transport = timed
        self.arch.run(slots.Slot("intraday", self.clock.now().astimezone(slots.NY), "manual"), manual=True)
        self.assertGreater(len(stamps), 40)
        worst = max(sum(1 for t in stamps if s0 <= t < s0 + 60) for s0 in stamps)
        self.assertLessEqual(worst, 30)

    def test_gate_unit_and_allow_list(self):
        clock = Clock(datetime(2026, 10, 26, tzinfo=timezone.utc))
        g = RateGate(30, clock.monotonic, clock.sleep)
        for _ in range(30):
            g.acquire()
        t0 = clock.monotonic()
        g.acquire()
        self.assertGreaterEqual(clock.monotonic() - t0, 59.9)
        sent = []
        http = Http(lambda *a: sent.append(a) or (200, {}, b"{}"), sleep=clock.sleep)
        for path in ("/v2/orders", "/v2/account", "/v2/positions"):
            with self.assertRaises(Refused):
                http.get("https://paper-api.alpaca.markets", path)
        with self.assertRaises(Refused):
            http.get("https://api.alpaca.markets", "/v2/options/contracts")
        self.assertEqual(sent, [])


class NoSecretsTest(ArchiveCase):
    def test_log_status_report_have_no_keys(self):
        from strategy_platform.options_archive.service import setup_logging
        s = slot("fred")
        self.make(s.utc, FakeApi(Clock(s.utc), D, fail={"/fred/series/observations": ["net", "net"]}))
        h = setup_logging(self.arch.paths["log"], self.arch.hidden)
        self.addCleanup(logging.getLogger("options_archive").removeHandler, h)
        self.addCleanup(h.close)
        self.arch.run(s)
        self.arch.run(slot("oi"))
        logging.getLogger("options_archive").warning("echo %s api_key=%s", KID, FKEY)   # even if someone logs one
        self.arch.write_status()
        from strategy_platform.options_archive import report
        import json
        report.write_status(self.tmp + "/os_archive_gaps_test.json",
                            report.day_report(self.arch.conn, D, CFG, self.clock.now()))
        h.flush()
        text = self.all_text()
        self.assertIn("api_key=***", text)
        for secret in (KID, SKEY, FKEY):
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(open(self.arch.paths["status"]).read())["state"], "idle")


if __name__ == "__main__":
    unittest.main()
