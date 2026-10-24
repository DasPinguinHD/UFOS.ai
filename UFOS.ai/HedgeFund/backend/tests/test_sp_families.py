"""Stage 4 V1 (SPEC §9.1/§9.2): strategy families, the v6 migration, the
holdout window, contamination, eligibility, holdout events and trials.
Temp databases outside the repo, fake data, fixed clocks, offline.
Part of UFOS.ai (proof of concept)."""
import copy
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.backtest import engine, metrics, runcard  # noqa: E402
from strategy_platform.core.clock import FixedClock  # noqa: E402
from strategy_platform.lifecycle import registry as lc  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
from strategy_platform.validation import families as fam  # noqa: E402
import test_sp_backtest_support as sup  # noqa: E402

SYMS = ["AAA", "BBB"]
NOW = "2026-10-09T15:00:00+00:00"          # Friday, session open -> last complete session 2026-10-08


def spec_dict(name="fam", symbols=SYMS, variant=0):
    """``variant`` changes the logic (max_weight) -> another logic hash (SPEC §9.8)."""
    s = sup.spec(symbols, max_weight=1.0 - 0.01 * variant)
    s["name"] = name
    return s


def spec_text(name="fam", symbols=SYMS, variant=0, **extra):
    s = spec_dict(name, symbols, variant)
    s.update(extra)
    return json.dumps(s)


def engine_result():
    prices = {"AAA": sup.series(50), "BBB": sup.series(80, drift=-0.0005), "SPY": sup.series(400)}
    return engine.run(sup.spec(SYMS), data=sup.FakeLoader(prices))


def with_end(result, end):
    d = copy.deepcopy(result.to_dict())
    d["range"]["end"] = end
    return d


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = engine_result()
        cls.metrics = metrics.compute(cls.result)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp4f-")
        self.path = os.path.join(self.tmp, "hedgefund.db")
        db.init(self.path)
        fam.set_clock(FixedClock(NOW))

    def tearDown(self):
        fam.set_clock(None)
        db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def raw(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def done(self, sid, *, result=None, metrics_=None, kind="backtest"):
        card = runcard.create(sid, kind=kind)
        runcard.mark_running(card["id"])
        return runcard.finish(card["id"], result if result is not None else self.result,
                              metrics_ if metrics_ is not None else self.metrics)


class FamilyTest(Case):
    def test_duplicate_chain_and_versions_share_the_root_family(self):
        a = lc.create_from_text(spec_text("root"), "user")["id"]
        b = lc.duplicate(a)["id"]
        c = lc.duplicate(b, name="grandchild")["id"]
        lc.new_version(c, spec_text("grandchild v2"), 1)
        self.assertEqual({fam.family_of(x) for x in (a, b, c)}, {a})
        self.assertEqual(set(db.family_strategy_ids(a)), {a, b, c})
        other = lc.create_from_text(spec_text("other", variant=1), "user")["id"]
        self.assertEqual(fam.family_of(other), other)
        self.assertIsNotNone(db.family_get(other))

    def test_import_of_an_export_joins_only_with_a_verified_claim(self):
        a = lc.create_from_text(spec_text("root"), "user")["id"]
        h = db.strategy_version_get(a, 1)["content_hash"]
        good = lc.create_from_text(spec_text("re-import", provenance={"exported_from": {
            "strategy_id": a, "version": 1, "content_hash": h}}), "import", file_name="x.json")["id"]
        forged = lc.create_from_text(spec_text("forged", variant=1, provenance={"exported_from": {
            "strategy_id": a, "version": 1, "content_hash": "0" * 64}}), "import")["id"]
        missing = lc.create_from_text(spec_text("missing", variant=2, provenance={"exported_from": {
            "strategy_id": "s-000000000000", "version": 1, "content_hash": h}}), "import")["id"]
        plain = lc.create_from_text(spec_text("plain", variant=3), "import")["id"]
        self.assertEqual(fam.family_of(good), a)
        self.assertEqual(fam.family_of(forged), forged)
        self.assertEqual(fam.family_of(missing), missing)
        self.assertEqual(fam.family_of(plain), plain)
        dup = lc.duplicate(good)["id"]
        self.assertEqual(fam.family_of(dup), a)

    def test_logic_twins_join_the_existing_family(self):
        # review 2026-10-09: copy-paste of the spec text / an import with the provenance stripped
        a = lc.create_from_text(spec_text("root"), "user")["id"]
        fam.record_holdout_event(a, 1, fam.holdout_window("equity"), "fail", "bt-000000000001")
        pasted = lc.create_from_text(spec_text("Renamed copy", description="new words", tags=["x"],
                                               category="momentum"), "user")["id"]
        stripped = lc.create_from_text(spec_text("stripped export"), "import", file_name="e.json")["id"]
        changed = lc.create_from_text(spec_text("one value differs", variant=1), "user")["id"]
        self.assertEqual((fam.family_of(pasted), fam.family_of(stripped), fam.family_of(changed)), (a, a, changed))
        for sid in (pasted, stripped):
            el = fam.holdout_eligibility(sid)
            self.assertFalse(el["eligible"])
            self.assertEqual(el["eligible_from"], "2027-10-08")
        self.assertTrue(fam.holdout_eligibility(changed)["eligible"])
        self.assertEqual(db.logic_hash_of(spec_dict("x")), db.logic_hash_of(dict(spec_dict("y"), tags=["t"])))
        self.assertNotEqual(db.logic_hash_of(spec_dict("x")), db.logic_hash_of(spec_dict("x", variant=1)))

    def test_edit_into_a_twin_uses_the_merged_family_state(self):
        a = lc.create_from_text(spec_text("root"), "user")["id"]
        fam.record_holdout_event(a, 1, fam.holdout_window("equity"), "fail", "bt-000000000001")
        b = lc.create_from_text(spec_text("innocent", variant=4), "user")["id"]
        self.assertTrue(fam.holdout_eligibility(b)["eligible"])
        lc.new_version(b, spec_text("innocent"), 1)                 # now the locked logic
        self.assertEqual(fam.family_of(b), b)                       # family never changes after creation
        st = fam.family_state(b)
        self.assertEqual((st["related"], st["seen_until"]), ([b, a], date(2026, 10, 8)))
        el = fam.holdout_eligibility(b)
        self.assertFalse(el["eligible"])
        self.assertIn("failed the holdout", el["reason"])
        self.assertEqual(el["family_events"], 1)
        chips = lc.family_chips(lc.get(b))
        self.assertEqual((chips["contaminated"], chips["holdout_failed_until"]), (True, "2027-10-08"))

    def test_family_of_unknown(self):
        with self.assertRaises(lc.NotFound):
            fam.family_of("s-000000000000")


class MigrationTest(unittest.TestCase):
    """A v5 database with strategies, versions and run cards -> v6 computes families, seen_until
    and the trial numbers of the existing done cards."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp4m-")
        self.path = os.path.join(self.tmp, "hedgefund.db")

    def tearDown(self):
        db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_v5_to_v6(self):
        conn = sqlite3.connect(self.path)
        for version, statements in db.MIGRATIONS[:5]:
            for sql in statements:
                sql(conn) if callable(sql) else conn.execute(sql)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        t = "2026-01-01T00:00:00+00:00"

        def strategy(sid, n, envelopes):
            conn.execute("INSERT INTO strategies (id, name, origin, category, state, current_version, created_at, "
                         "updated_at, state_changed_at, library_key) VALUES (?, ?, 'user', 'other', 'draft', ?, ?, ?, "
                         "?, NULL)", (sid, sid, len(envelopes), f"2026-01-0{n}T00:00:00+00:00", t, t))
            for v, env in enumerate(envelopes, start=1):
                conn.execute("INSERT INTO strategy_versions VALUES (?, ?, '{}', ?, ?, '{}', ?)",
                             (sid, v, f"h-{sid}-{v}", json.dumps(env), t))

        R, D1, D2, I, J = (f"s-{c * 12}" for c in "abcde")
        strategy(R, 1, [{"origin": "user", "parent": None},
                        {"origin": "user", "parent": {"strategy_id": R, "version": 1}}])
        strategy(D1, 2, [{"origin": "duplicate", "parent": {"strategy_id": R, "version": 2}}])
        strategy(D2, 3, [{"origin": "duplicate", "parent": {"strategy_id": D1, "version": 1}}])
        strategy(I, 4, [{"origin": "import", "parent": None, "claimed": {"exported_from": {
            "strategy_id": D1, "version": 1, "content_hash": f"h-{D1}-1"}}}])
        strategy(J, 5, [{"origin": "import", "parent": None, "claimed": {"exported_from": {
            "strategy_id": R, "version": 1, "content_hash": "wrong"}}}])

        def run(rid, sid, status, end, finished):
            conn.execute("INSERT INTO backtest_runs (id, created_at, updated_at, finished_at, strategy_id, version, "
                         "content_hash, status, spec_json, request_json, range_json) VALUES (?, ?, ?, ?, ?, 1, 'h', ?, "
                         "'{}', '{}', ?)", (rid, t, t, finished, sid, status,
                                            None if end is None else json.dumps({"end": end})))

        run("bt-000000000001", R, "done", "2024-04-30", "2026-02-01T00:00:00+00:00")
        run("bt-000000000002", D2, "done", "2025-01-15", "2026-02-02T00:00:00+00:00")
        run("bt-000000000003", R, "failed", None, "2026-02-03T00:00:00+00:00")
        run("bt-000000000004", I, "done", "2024-12-31", "2026-02-04T00:00:00+00:00")
        run("bt-000000000005", J, "done", "2023-05-01", "2026-02-05T00:00:00+00:00")
        conn.commit()
        conn.close()

        self.assertEqual(db.init(self.path), db.SCHEMA_VERSION)
        conn = sqlite3.connect(self.path)
        try:                                                        # v7: logic hashes backfilled
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategy_logic").fetchone()[0], 6)
            self.assertEqual(conn.execute("SELECT COUNT(DISTINCT logic_hash) FROM strategy_logic").fetchone()[0], 1)
        finally:
            conn.close()
        self.assertEqual({s: db.strategy_get(s)["family_id"] for s in (R, D1, D2, I, J)},
                         {R: R, D1: R, D2: R, I: R, J: J})
        self.assertEqual(fam.seen_until(R), date(2025, 1, 15))
        self.assertEqual(fam.seen_until(J), date(2023, 5, 1))
        self.assertEqual(fam.trial_count(R), 3)
        trials = {r["id"]: r["trial_number"] for r in db.backtest_runs_list(limit=10)}
        self.assertEqual(trials, {"bt-000000000001": 1, "bt-000000000002": 2, "bt-000000000003": None,
                                  "bt-000000000004": 3, "bt-000000000005": 1})
        self.assertEqual({r["kind"] for r in db.backtest_runs_list(limit=10)}, {"backtest"})
        conn = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):            # finished cards stay immutable
                conn.execute("UPDATE backtest_runs SET trial_number = 9 WHERE id = 'bt-000000000001'")
        finally:
            conn.close()


class WindowTest(unittest.TestCase):
    def tearDown(self):
        fam.set_clock(None)

    def test_month_arithmetic(self):
        self.assertEqual(fam.add_months(date(2026, 3, 31), -1), date(2026, 2, 28))
        self.assertEqual(fam.add_months(date(2024, 2, 29), 12), date(2025, 2, 28))
        self.assertEqual(fam.add_months(date(2026, 1, 15), -13), date(2024, 12, 15))
        self.assertEqual(fam.add_months(date(2026, 12, 31), -24), date(2024, 12, 31))

    def test_equity_crypto_mixed(self):
        self.assertEqual(fam.holdout_window("equity", date(2026, 10, 8)),
                         {"start": date(2024, 10, 8), "end": date(2026, 10, 8)})
        self.assertEqual(fam.holdout_window("mixed", date(2026, 10, 8))["start"], date(2024, 10, 8))
        self.assertEqual(fam.holdout_window("crypto", date(2026, 10, 8)),
                         {"start": date(2025, 10, 8), "end": date(2026, 10, 8)})
        # 2026-09-02 - 24 months = 2024-09-02 (Labor Day) -> first session 2024-09-03
        self.assertEqual(fam.holdout_window("equity", date(2026, 9, 2))["start"], date(2024, 9, 3))
        # weekend -> Monday; crypto counts calendar days
        self.assertEqual(fam.holdout_window("equity", date(2027, 1, 4))["start"], date(2025, 1, 6))
        self.assertEqual(fam.holdout_window("crypto", date(2027, 1, 4))["start"], date(2026, 1, 4))
        with self.assertRaises(ValueError):
            fam.holdout_window("bonds", date(2026, 10, 8))

    def test_universe_kind_and_training_end(self):
        self.assertEqual(fam.universe_kind(spec_dict()), "equity")
        self.assertEqual(fam.universe_kind(spec_dict(symbols=["BTC-USD", "ETH-USD"])), "crypto")
        self.assertEqual(fam.universe_kind(spec_dict(symbols=["SPY", "BTC-USD"])), "mixed")
        self.assertEqual(fam.training_end(spec_dict(), date(2026, 9, 2)), date(2024, 8, 30))   # Fri before Labor Day
        self.assertEqual(fam.training_end(spec_dict(symbols=["BTC-USD"]), date(2026, 10, 8)), date(2025, 10, 7))
        self.assertEqual(fam.training_end(spec_dict(symbols=["SPY", "BTC-USD"]), date(2026, 10, 8)),
                         date(2024, 10, 7))

    def test_last_session_from_the_clock(self):
        fam.set_clock(FixedClock(NOW))
        self.assertEqual(fam.last_session("equity"), date(2026, 10, 8))
        self.assertEqual(fam.last_session("crypto"), date(2026, 10, 8))
        self.assertEqual(fam.last_session("equity", fam.clock_mod.to_utc("2026-10-09T20:30:00Z")), date(2026, 10, 9))
        self.assertEqual(fam.last_session("equity", fam.clock_mod.to_utc("2026-10-11T12:00:00Z")), date(2026, 10, 9))
        self.assertEqual(fam.holdout_window("equity")["end"], date(2026, 10, 8))

    def test_violation_message(self):
        ex = fam.HoldoutWindowViolation(date(2024, 10, 8))
        self.assertEqual(ex.user_message, "This end date lies inside the locked holdout window (from 2024-10-08). "
                                          "The holdout is opened once per strategy family via 'Open holdout'.")
        self.assertIsInstance(ex, fam.InvalidRequest)


class SeenTrialsTest(Case):
    def test_trials_and_seen_until_from_run_cards(self):
        a = lc.create_from_text(spec_text("root"), "user")["id"]
        b = lc.duplicate(a)["id"]
        self.assertEqual((fam.trial_count(a), fam.seen_until(a)), (0, None))
        c1 = self.done(a)
        failed = runcard.create(b)
        runcard.fail(failed["id"], "boom")
        c2 = self.done(b, result=with_end(self.result, "2025-03-03"))
        c3 = self.done(a, result=with_end(self.result, "2024-02-01"), kind="validation")
        self.assertEqual([c1["trial_number"], c2["trial_number"], c3["trial_number"]], [1, 2, 3])
        self.assertEqual(c3["kind"], "validation")
        self.assertIsNone(runcard.get(failed["id"])["trial_number"])
        self.assertEqual(fam.trial_count(a), 3)
        self.assertEqual(fam.seen_until(a), date(2025, 3, 3))       # max, never lowered
        sharpe = self.metrics["performance"]["sharpe"]
        self.assertEqual(fam.family_trial_sharpes(a), [sharpe] * 3)
        other = lc.create_from_text(spec_text("other", variant=1), "user")["id"]
        self.assertEqual(self.done(other)["trial_number"], 1)
        self.assertEqual(runcard.list_runs(a, kind="validation")[0]["id"], c3["id"])
        self.assertEqual(runcard.latest_done(a, 1, kind="backtest")["id"], c1["id"])

    def test_record_seen_and_contaminated(self):
        a = lc.create_from_text(spec_text("root"), "user")["id"]
        w = fam.holdout_window("equity", date(2026, 10, 8))
        fam.record_seen(a, date(2024, 10, 7))
        self.assertFalse(fam.is_contaminated(a, w))
        fam.record_seen(a, "2024-10-08")
        self.assertTrue(fam.is_contaminated(a, w))
        fam.record_seen(a, date(2020, 1, 1))
        self.assertEqual(fam.seen_until(a), date(2024, 10, 8))
        self.assertTrue(lc.get(a)["contaminated"])
        self.assertEqual(set(lc.family_chips(lc.get(a))),
                         {"family_id", "contaminated", "holdout_failed_until", "trial_count"})

    def test_trial_number_is_set_once(self):
        a = lc.create_from_text(spec_text("root"), "user")["id"]
        card = runcard.create(a)
        with self.assertRaises(sqlite3.DatabaseError):          # not while queued/running
            self.raw("UPDATE backtest_runs SET status = 'running', trial_number = 4 WHERE id = ?", (card["id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            self.raw("UPDATE backtest_runs SET status = 'running', kind = 'holdout' WHERE id = ?", (card["id"],))
        with self.assertRaises(ValueError):
            db.backtest_run_insert(dict(id="bt-x", status="queued", kind="other"), ("engine", "x", None))
        with self.assertRaises(ValueError):
            runcard.create(a, kind="other")


class EligibilityTest(Case):
    def setUp(self):
        super().setUp()
        self.sid = lc.create_from_text(spec_text("elig"), "user")["id"]

    def test_fresh_family_gets_the_full_window(self):
        fam.record_seen(self.sid, "2024-04-30")
        el = fam.holdout_eligibility(self.sid)
        self.assertTrue(el["eligible"])
        self.assertIsNone(el["reason"])
        self.assertEqual(el["window"], {"start": "2024-10-08", "end": "2026-10-08"})
        self.assertEqual((el["unseen_from"], el["eligible_from"], el["mode"]), ("2024-10-08", None, "full"))
        self.assertEqual((el["family_events"], el["universe_events"], el["contaminated"]), (0, 0, False))

    def test_contaminated_needs_twelve_unseen_months(self):
        fam.record_seen(self.sid, "2025-11-26")           # + 12 months = 2026-11-26, Thanksgiving
        el = fam.holdout_eligibility(self.sid)
        self.assertFalse(el["eligible"])
        self.assertTrue(el["contaminated"])
        self.assertEqual(el["eligible_from"], "2026-11-27")
        self.assertEqual(el["unseen_from"], "2025-11-28")
        self.assertIn("2026-11-27", el["reason"])
        self.assertIn("2025-11-26", el["reason"])
        fam.set_clock(FixedClock("2026-11-27T21:30:00Z"))   # after the close of 2026-11-27 (early close)
        el = fam.holdout_eligibility(self.sid)
        self.assertTrue(el["eligible"])
        self.assertEqual((el["mode"], el["window"]), ("unseen", {"start": "2025-11-28", "end": "2026-11-27"}))

    def test_contaminated_already_eligible(self):
        fam.record_seen(self.sid, "2025-09-30")
        el = fam.holdout_eligibility(self.sid)
        self.assertTrue(el["eligible"])
        self.assertEqual(el["window"], {"start": "2025-10-01", "end": "2026-10-08"})

    def test_failed_family_lock_and_one_retest(self):
        dup = lc.duplicate(self.sid)["id"]
        full = fam.holdout_window("equity")
        eid = fam.record_holdout_event(self.sid, 1, full, "fail", "bt-000000000009")
        self.assertIsInstance(eid, int)
        self.assertEqual(fam.seen_until(fam.family_of(dup)), date(2026, 10, 8))
        for sid in (self.sid, dup):
            el = fam.holdout_eligibility(sid)
            self.assertFalse(el["eligible"])
            self.assertEqual(el["eligible_from"], "2027-10-08")
            self.assertIn("failed the holdout on 2026-10-09", el["reason"])
            self.assertEqual(el["family_events"], 1)
            self.assertEqual(lc.get(sid)["holdout_failed_until"], "2027-10-08")
        fam.set_clock(FixedClock("2027-10-08T21:00:00Z"))
        el = fam.holdout_eligibility(dup)
        self.assertTrue(el["eligible"])
        self.assertEqual(el["window"], {"start": "2026-10-09", "end": "2027-10-08"})
        fam.record_holdout_event(dup, 1, el["window"], "pass", "bt-00000000000a")
        self.assertIsNone(fam.holdout_failed_at(self.sid))      # a passed re-test lifts the lock
        # ... but the window is seen again (the next eligibility date lies in 2028: beyond the NYSE calendar)
        self.assertEqual(fam.seen_until(self.sid), date(2027, 10, 8))     # family id = the root's id
        self.assertTrue(fam.is_contaminated(fam.family_of(dup), fam.holdout_window("equity")))
        self.assertEqual(len(db.holdout_events_list(fam.family_of(dup))), 2)

    def test_universe_counter_counts_other_families_on_the_same_universe(self):
        other = lc.create_from_text(spec_text("same universe", variant=1), "user")["id"]
        third = lc.create_from_text(spec_text("other universe", symbols=["AAA"]), "user")["id"]
        old = lc.create_from_text(spec_text("long ago", variant=2), "user")["id"]
        full = fam.holdout_window("equity")
        fam.record_holdout_event(other, 1, full, "pass", "bt-000000000001")
        fam.record_holdout_event(third, 1, full, "pass", "bt-000000000002")
        fam.record_holdout_event(old, 1, {"start": "2020-01-02", "end": "2022-01-03"}, "fail", "bt-000000000003")
        el = fam.holdout_eligibility(self.sid)
        self.assertEqual((el["universe_events"], el["family_events"]), (1, 0))
        self.assertEqual(fam.holdout_eligibility(other)["universe_events"], 0)

    def test_holdout_events_are_insert_only(self):
        fam.record_holdout_event(self.sid, 1, fam.holdout_window("equity"), "fail", "bt-000000000001")
        for sql in ("UPDATE holdout_events SET result = 'pass'", "DELETE FROM holdout_events"):
            with self.assertRaises(sqlite3.DatabaseError) as cm:
                self.raw(sql)
            self.assertIn("insert-only", str(cm.exception))
        with self.assertRaises(ValueError):
            fam.record_holdout_event(self.sid, 1, fam.holdout_window("equity"), "maybe", "bt-1")
        with self.assertRaises(lc.NotFound):
            fam.record_holdout_event(self.sid, 7, fam.holdout_window("equity"), "pass", "bt-1")
        actions = [a["action"] for a in db.list_audit(limit=5)]
        self.assertIn("holdout_opened", actions)


if __name__ == "__main__":
    unittest.main()
