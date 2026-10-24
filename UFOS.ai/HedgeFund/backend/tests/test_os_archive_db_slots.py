"""OS1 archive storage, config and slot plan (SP §21.2 / §21.5): migration v1 + os_schema row, every table insert-only,
read-only opening (None when not set up, writes refused), the validated override config, the NY slot plan on the NYSE
calendar — 30 slots, early close without 15:25, holidays empty, DST (EU change 2026-10-25 does not move NY slots; US
change 2026-11-01 moves the UTC times), slot start = slot − 90 s for chain slots, next slot across a weekend."""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone

from strategy_platform.options_archive import config, db, slots


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ufos-os1db-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "options_archive.db")

    def test_migration_and_insert_only(self):
        conn = db.open_rw(self.path, lambda: 42)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual([tuple(r) for r in conn.execute("SELECT version, applied_ms FROM os_schema")], [(1, 42)])
        self.assertEqual(db.migrate(conn), 1)                           # idempotent
        conn.execute("INSERT INTO os_runs (kind, slot_ms, started_ms, finished_ms, status, source, model_version, "
                     "assumptions_hash, app_version) VALUES ('intraday', 1, 1, 2, 'ok', 'alpaca_data', 'raw', 'h', 'v')")
        conn.execute("INSERT INTO os_gaps (run_id, slot_ms, kind, what, recorded_ms) VALUES (1, 1, 'late', 'x', 3)")
        for sql in ("INSERT INTO os_configs VALUES ('h', '{}', 1)",
                    "INSERT INTO os_contracts (symbol, underlying, first_seen_ms) VALUES ('SPY1', 'SPY', 1)",
                    "INSERT INTO os_contract_sightings VALUES (1, 'SPY1', 1)",
                    "INSERT INTO os_open_interest (run_id, symbol, valid_for, available_ms, observed_ms) VALUES "
                    "(1, 'SPY1', '2026-10-22', 1, 1)",
                    "INSERT INTO os_chain_snapshots (run_id, symbol, available_ms, observed_ms) VALUES (1, 'SPY1', 1, 1)",
                    "INSERT INTO os_bars (symbol, timeframe, t_ms, available_ms, observed_ms) VALUES ('SPY1', '1Day', 1, 1, 1)",
                    "INSERT INTO os_fred (series_id, date, value, available_ms, observed_ms) VALUES ('VIXCLS', 'd', 1, 1, 1)"):
            conn.execute(sql)
        for t in db.INSERT_ONLY:
            for sql in (f"UPDATE {t} SET rowid = rowid", f"DELETE FROM {t}"):
                with self.assertRaises(sqlite3.DatabaseError, msg=(t, sql)):
                    conn.execute(sql)
        conn.close()
        ro = db.open_ro(self.path)
        self.assertEqual(ro.execute("SELECT COUNT(*) FROM os_runs").fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO os_gaps (slot_ms, kind, what, recorded_ms) VALUES (1, 'x', 'y', 1)")
        ro.close()
        self.assertIsNone(db.open_ro(os.path.join(self.tmp, "missing.db")))

    def test_newer_schema_refused(self):
        conn = db.open_rw(self.path)
        conn.execute("PRAGMA user_version = 9")
        with self.assertRaises(sqlite3.DatabaseError):
            db.migrate(conn)
        conn.close()

    def test_config_override_validated(self):
        p = os.path.join(self.tmp, "cfg.json")
        self.assertEqual(config.load(p), config.Config())
        with open(p, "w") as fh:
            json.dump({"strike_band_pct": 12.5, "fixed_slots": ["09:55", "15:20"], "timeouts_s": {"contracts": 40}}, fh)
        c = config.load(p)
        self.assertEqual((c.strike_band_pct, c.fixed_slots, c.timeouts_s["contracts"], c.timeouts_s["fred"]),
                         (12.5, ("09:55", "15:20"), 40.0, 20.0))
        self.assertNotEqual(c.assumptions_hash, config.Config().assumptions_hash)
        for bad, msg in (({"nope": 1}, "unknown key"), ({"rate_per_min": 60}, "rate_per_min"),
                         ({"page_limit": 5000}, "page_limit"), ({"oi_slot": "8h30"}, "HH:MM"),
                         ({"strike_band_pct": "10"}, "number")):
            with open(p, "w") as fh:
                json.dump(bad, fh)
            with self.assertRaises(ValueError) as cm:
                config.load(p)
            self.assertIn(msg, str(cm.exception))


class SlotsTest(unittest.TestCase):
    cfg = config.Config()

    def test_regular_day(self):
        s = slots.day_slots(date(2026, 10, 23), self.cfg)
        self.assertEqual(len(s), 30)
        labels = [x.label for x in s]
        self.assertEqual(labels[:4], ["oi", "fred", "09:45", "09:55"])
        self.assertEqual(labels[-3:], ["15:30", "15:45", "eod"])
        self.assertIn("15:25", labels)
        self.assertEqual(sum(1 for x in s if x.kind in ("intraday", "eod")), 28)      # = PROPOSED_CADENCE (§21.2)
        self.assertEqual([x.label for x in s if x.fixed], ["09:55", "15:25"])
        eod = s[-1]
        self.assertEqual(eod.ny.strftime("%H:%M"), "16:15")

    def test_early_close_and_holiday(self):
        s = slots.day_slots(date(2026, 11, 27), self.cfg)           # day after Thanksgiving: 13:00 close
        labels = [x.label for x in s]
        self.assertNotIn("15:25", labels)
        self.assertIn("09:55", labels)
        self.assertEqual(labels[-2:], ["12:45", "eod"])
        self.assertEqual(s[-1].ny.strftime("%H:%M"), "13:15")
        self.assertEqual(slots.day_slots(date(2026, 11, 26), self.cfg), [])     # Thanksgiving
        self.assertEqual(slots.day_slots(date(2026, 10, 24), self.cfg), [])     # Saturday

    def test_dst_gap_week(self):
        def utc(d, label):
            return slots.find(d, label, self.cfg).utc.strftime("%H:%M")
        self.assertEqual(utc(date(2026, 10, 23), "15:25"), "19:25")   # EDT (UTC−4)
        self.assertEqual(utc(date(2026, 10, 26), "15:25"), "19:25")   # EU already on CET, NY still EDT
        self.assertEqual(utc(date(2026, 10, 30), "09:55"), "13:55")
        self.assertEqual(utc(date(2026, 11, 2), "15:25"), "20:25")    # EST (UTC−5) after 2026-11-01
        s = slots.find(date(2026, 11, 2), "09:55", self.cfg)
        self.assertEqual((s.utc - s.start_utc(self.cfg)).total_seconds(), 90)
        oi = slots.find(date(2026, 11, 2), "oi", self.cfg)
        self.assertEqual(oi.start_utc(self.cfg), oi.utc)

    def test_next_slot(self):
        n = slots.next_slot(datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc), self.cfg)    # Saturday
        self.assertEqual((n.kind, n.ny.date().isoformat(), n.label), ("oi", "2026-10-26", "oi"))
        n = slots.next_slot(datetime(2026, 10, 23, 19, 24, tzinfo=timezone.utc), self.cfg)   # 15:24 NY
        self.assertEqual(n.label, "15:30")                       # 15:25 already had to start at 15:23:30
        n = slots.next_slot(datetime(2026, 10, 23, 19, 23, tzinfo=timezone.utc), self.cfg)
        self.assertEqual(n.label, "15:25")


class ConfirmedDefaultsTest(unittest.TestCase):
    """User decision 2026-10-24 13:49 after the OS0 run 20261024T114530Z: filter ±10 % / ≤ 45 days and 28 snapshots
    per session confirmed (defaults, no override file); OI floor + FRED series confirmed by the run; quote staleness
    and coverage stay provisional until the RTH run (Monday 2026-10-26)."""

    def test_defaults_are_the_confirmed_values(self):
        c = config.Config()
        for k, v in config.CONFIRMED.items():
            self.assertEqual(getattr(c, k), v, k)
        self.assertEqual((c.strike_band_pct, c.expiry_days), (10.0, 45))
        self.assertEqual(c.fred_series, ("VIXCLS", "VXVCLS", "VXNCLS"))
        self.assertEqual(config.PROVISIONAL_KEYS, ("quote_max_age_s", "coverage_min_pct"))
        self.assertEqual((c.quote_max_age_s, c.coverage_min_pct), (300, 80.0))
        st = config.key_status(c)
        self.assertEqual(set(st["confirmed_keys"]), set(config.CONFIRMED))
        self.assertEqual(st["provisional_keys"], ["quote_max_age_s", "coverage_min_pct"])
        self.assertIn("20261024T114530Z", st["confirmed_by"])
        # the confirmation is kept OUT of the hashed config: the registered archive's assumptions_hash is unchanged
        self.assertEqual(c.assumptions_hash, "acccfa4219e01e2bed31958f9320bba40746b7f4dbe87002986860f4d42d3134")

    def test_28_snapshots_per_regular_session(self):
        plan = slots.day_slots(date(2026, 10, 26), config.Config())
        chain = [s for s in plan if s.kind in ("intraday", "eod")]
        self.assertEqual(len(chain), 28)                            # 25 every 15 min + 09:55 + 15:25 + EOD

    def test_override_of_a_confirmed_key_is_reported_provisional(self):
        from dataclasses import replace
        st = config.key_status(replace(config.Config(), strike_band_pct=12.0))
        self.assertNotIn("strike_band_pct", st["confirmed_keys"])
        self.assertIn("strike_band_pct", st["provisional_keys"])
        from strategy_platform.options_archive import report
        v = report.config_view(config.Config())
        self.assertEqual(v["provisional_keys"], ["quote_max_age_s", "coverage_min_pct"])
        self.assertIn("expiry_days", v["confirmed_keys"])


if __name__ == "__main__":
    unittest.main()
