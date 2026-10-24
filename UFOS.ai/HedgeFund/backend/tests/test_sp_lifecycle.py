"""strategy_platform/lifecycle/registry.py + hedgefund.db v2 (SPEC §7.6,
§7.10): create / import / versions (insert-only, optimistic concurrency) /
duplicate / stage-2 transitions with audit rows / library seeding
(idempotent by library_key + content hash, failures surfaced). Real
validator (B1), temp hedgefund.db.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_lifecycle -v
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from strategy_platform.lifecycle import registry as lc
from strategy_platform.storage import db


def spec(name="Test momentum", lookback=20, **extra):
    s = {"schema": "ufos.strategy/1", "name": name, "category": "momentum",
         "universe": {"symbols": ["SPY", "TLT"]},
         "signal": {"op": "pct_change", "args": [{"field": "close"}, lookback]}}
    s.update(extra)
    return s


def text(**kw):
    return json.dumps(spec(**kw))


class LifecycleCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sp-lc-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        db.close()
        self.path = os.path.join(self.dir, "hedgefund.db")
        db.init(self.path)
        self.addCleanup(db.close)


class SchemaTest(LifecycleCase):
    def test_v2_tables(self):
        self.assertGreaterEqual(db.SCHEMA_VERSION, 2)
        info = db.storage_info()
        for t in ("strategies", "strategy_versions", "strategy_transitions", "ext_observations"):
            self.assertEqual(info["rows"][t], 0)

    def test_versions_are_insert_only(self):
        sid = lc.create_from_text(text(), "user")["id"]
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE strategy_versions SET spec_json = '{}' WHERE strategy_id = ?", (sid,))
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM strategy_versions WHERE strategy_id = ?", (sid,))


class CreateTest(LifecycleCase):
    def test_user_strategy(self):
        res = lc.create_from_text(text(), "user")
        self.assertEqual(res["version"], 1)
        row = lc.get(res["id"])
        self.assertEqual((row["state"], row["origin"], row["name"], row["category"]),
                         ("draft", "user", "Test momentum", "momentum"))
        v = lc.get_version(res["id"], 1)
        self.assertEqual(v["spec"]["portfolio"]["mode"], "long_only")           # defaults stored
        env = v["envelope"]
        self.assertEqual((env["origin"], env["created_by"], env["ai_generated"], env["external_text"]),
                         ("user", "user", False, False))
        self.assertEqual(env["content_hash"], v["content_hash"])
        trans = db.strategy_transitions_list(res["id"])
        self.assertEqual((trans[0]["from_state"], trans[0]["to_state"]), (None, "draft"))
        self.assertEqual(db.list_audit(action="strategy_created")[0]["detail"]["id"], res["id"])

    def test_invalid_spec_is_not_stored(self):
        with self.assertRaises(lc.SpecInvalid) as cm:
            lc.create_from_text(json.dumps(spec(signal={"op": "nope", "args": []})), "user")
        self.assertIn("unknown_op", [e["code"] for e in cm.exception.validation["errors"]])
        with self.assertRaises(lc.SpecInvalid) as cm:
            lc.create_from_text("{not json", "user")
        self.assertEqual(cm.exception.validation["errors"][0]["code"], "parse_error")
        self.assertEqual(db.storage_info()["rows"]["strategies"], 0)

    def test_import_claims_are_never_trusted(self):
        claims = {"state": "live", "validated": True, "run_card": "rc-1", "ai_generated": False}
        t = json.dumps(spec(provenance=claims))
        res = lc.create_from_text(t, "import", file_name=r"C:\Users\x\Desktop\..\evil/strategy.json",
                                  file_sha256="ab" * 32)
        row = lc.get(res["id"])
        self.assertEqual(row["state"], "draft")
        v = lc.get_version(res["id"], 1)
        env = v["envelope"]
        self.assertEqual(env["claimed"], claims)
        self.assertNotIn("provenance", v["spec"])
        self.assertEqual((env["ai_generated"], env["external_text"], env["origin"]), ("unknown", True, "import"))
        self.assertEqual(env["source_file"]["name"], "strategy.json")
        self.assertEqual(env["source_file"]["bytes"], len(t.encode("utf-8")))
        self.assertEqual(env["source_file"]["sha256_claimed"], "ab" * 32)
        # the hash ignores provenance
        self.assertEqual(v["content_hash"], lc._schema().content_hash(spec()))

    def test_file_name_cleaning(self):
        self.assertEqual(lc.clean_file_name("../../a/b\\c.json"), "c.json")
        self.assertIsNone(lc.clean_file_name("dir/"))
        long = "x" * 300 + ".json"
        cleaned = lc.clean_file_name(long)
        self.assertEqual(len(cleaned), 120)
        self.assertTrue(cleaned.endswith(".json"))
        self.assertEqual(lc.clean_file_name("a\x00b\nc.json"), "abc.json")

    def test_oversized_claims_rejected(self):
        t = json.dumps(spec(provenance={"blob": "x" * (lc.MAX_CLAIMED_BYTES + 10)}))
        with self.assertRaises(lc.SpecInvalid):
            lc.create_from_text(t, "import", file_name="a.json")


class VersionTest(LifecycleCase):
    def test_new_version_and_stale_base(self):
        sid = lc.create_from_text(text(), "user")["id"]
        res = lc.new_version(sid, text(lookback=30), 1)
        self.assertEqual((res["version"], res["unchanged"]), (2, False))
        self.assertEqual(lc.get(sid)["current_version"], 2)
        with self.assertRaises(lc.Conflict) as cm:
            lc.new_version(sid, text(lookback=40), 1)
        self.assertEqual(cm.exception.extra["current_version"], 2)
        same = lc.new_version(sid, text(lookback=30), 2)
        self.assertEqual((same["version"], same["unchanged"]), (2, True))
        v1, v2 = lc.get_version(sid, 1), lc.get_version(sid, 2)
        self.assertNotEqual(v1["content_hash"], v2["content_hash"])
        self.assertEqual(v2["envelope"]["parent"], {"strategy_id": sid, "version": 1})

    def test_imported_texts_stay_external_after_edit(self):
        sid = lc.create_from_text(text(), "import", file_name="a.json")["id"]
        lc.new_version(sid, text(lookback=30), 1)
        self.assertTrue(lc.get_version(sid, 2)["envelope"]["external_text"])

    def test_unknown_strategy(self):
        with self.assertRaises(lc.NotFound):
            lc.get("s-000000000000")
        with self.assertRaises(lc.NotFound):
            lc.get("../etc")

    def test_duplicate(self):
        sid = lc.create_from_text(text(), "import", file_name="a.json")["id"]
        dup = lc.duplicate(sid)
        row = lc.get(dup["id"])
        self.assertEqual((row["origin"], row["state"], row["name"]), ("duplicate", "draft", "Copy of Test momentum"))
        env = lc.get_version(dup["id"], 1)["envelope"]
        self.assertEqual(env["parent"], {"strategy_id": sid, "version": 1})
        self.assertTrue(env["external_text"])
        named = lc.duplicate(sid, "Mine")
        self.assertEqual(lc.get(named["id"])["name"], "Mine")
        with self.assertRaises(lc.SpecInvalid):
            lc.duplicate(sid, "x" * 200)


class TransitionTest(LifecycleCase):
    def test_stage2_rules(self):
        sid = lc.create_from_text(text(), "user")["id"]
        allowed = {t["to"]: t for t in lc.allowed_transitions(lc.get(sid))}
        self.assertNotIn("draft", allowed)
        self.assertTrue(allowed["idea"]["allowed"])
        self.assertTrue(allowed["retired"]["allowed"])
        self.assertFalse(allowed["backtested"]["allowed"])
        self.assertEqual(allowed["backtested"]["reason_if_not"], "needs a finished backtest of version 1")  # 3d
        self.assertIn("live guard", allowed["live"]["reason_if_not"])
        self.assertIsNone(allowed["idea"]["reason_if_not"])

        lc.transition(sid, "idea", "parking it")
        lc.transition(sid, "draft", "back to work")
        lc.transition(sid, "retired", "done")
        with self.assertRaises(lc.Conflict):
            lc.transition(sid, "idea", "not allowed from retired")
        row = lc.transition(sid, "draft", "revived")
        self.assertEqual(row["state"], "draft")
        with self.assertRaises(lc.Conflict) as cm:
            lc.transition(sid, "backtested", "please")
        self.assertIn("needs a finished backtest of version 1", cm.exception.message)  # 3d rule
        trans = db.strategy_transitions_list(sid)
        self.assertEqual([t["to_state"] for t in trans], ["draft", "idea", "draft", "retired", "draft"])
        self.assertEqual(trans[-1]["reason"], "revived")
        self.assertEqual(len(db.list_audit(action="strategy_transition")), 4)

    def test_reason_required_and_bounded(self):
        sid = lc.create_from_text(text(), "user")["id"]
        lc.transition(sid, "idea", "   ")                      # fix1023: an optional note ("" stored)
        self.assertEqual(db.strategy_transitions_list(sid)[-1]["reason"], "")
        with self.assertRaises(ValueError):
            lc.transition(sid, "draft", "x" * 501)

    def test_edit_returns_to_draft(self):
        sid = lc.create_from_text(text(), "user")["id"]
        lc.transition(sid, "retired", "stop")
        lc.new_version(sid, text(lookback=25), 1)
        row = lc.get(sid)
        self.assertEqual(row["state"], "draft")
        last = db.strategy_transitions_list(sid)[-1]
        self.assertEqual((last["actor"], last["from_state"], last["to_state"], last["version"]),
                         ("system", "retired", "draft", 2))


class SeedTest(LifecycleCase):
    def entries(self, lookback=20):
        return [{"key": "tsmom_test", "spec": spec("TS momentum", lookback), "metadata": {"key": "tsmom_test"}},
                {"key": "xs_test", "spec": spec("XS momentum", 60), "metadata": {"key": "xs_test"}}]

    def test_seeding_is_idempotent_and_versions_changes(self):
        st = lc.seed_library(self.entries())
        self.assertEqual((st["ok"], st["created"], st["updated"], st["unchanged"]), (True, 2, 0, 0))
        st = lc.seed_library(self.entries())
        self.assertEqual((st["created"], st["updated"], st["unchanged"]), (0, 0, 2))
        st = lc.seed_library(self.entries(lookback=21))
        self.assertEqual((st["created"], st["updated"], st["unchanged"]), (0, 1, 1))
        row = db.strategy_by_library_key("tsmom_test")
        self.assertEqual((row["origin"], row["state"], row["current_version"]), ("library", "idea", 2))
        env = lc.get_version(row["id"], 2)["envelope"]
        self.assertEqual((env["created_by"], env["origin"]), ("system", "library"))
        self.assertEqual(lc.seed_status()["unchanged"], 1)

    def test_library_is_read_only(self):
        lc.seed_library(self.entries())
        row = db.strategy_by_library_key("tsmom_test")
        with self.assertRaises(lc.Conflict) as cm:
            lc.new_version(row["id"], text(), 1)
        self.assertEqual(cm.exception.message, lc.READ_ONLY_MESSAGE)
        self.assertTrue(all(not t["allowed"] for t in lc.allowed_transitions(row)))
        dup = lc.duplicate(row["id"])
        self.assertEqual(lc.get(dup["id"])["state"], "draft")

    def test_invalid_entry_is_reported(self):
        bad = self.entries() + [{"key": "broken", "spec": spec(signal={"op": "nope", "args": []}), "metadata": {}}]
        st = lc.seed_library(bad)
        self.assertFalse(st["ok"])
        self.assertEqual(st["errors"][0]["key"], "broken")
        self.assertEqual(st["created"], 2)
        self.assertIn("1 invalid entry skipped", st["message"])

    def test_storage_failure_is_recorded_and_raised(self):
        db.close()
        with self.assertRaises(db.StorageUnavailable):
            lc.seed_library(self.entries())
        self.assertFalse(lc.seed_status()["ok"])
        self.assertIn("hedgefund.db", lc.seed_status()["message"])


class ReviewFixTest(LifecycleCase):
    """Review 2026-10-03: concurrent-save conflict -> 409, corrupt records -> StorageUnavailable,
    library metadata failures propagate."""

    def raw(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def test_version_number_taken_by_another_writer_is_a_conflict(self):
        sid = lc.create_from_text(text(), "user")["id"]
        conn = self.raw()
        row = conn.execute("SELECT spec_json, content_hash, envelope_json, validation_json, created_at "
                           "FROM strategy_versions WHERE strategy_id = ?", (sid,)).fetchone()
        conn.execute("INSERT INTO strategy_versions VALUES (?, 2, ?, ?, ?, ?, ?)", (sid,) + tuple(row))
        conn.commit()
        with self.assertRaises(lc.Conflict) as cm:
            lc.new_version(sid, text(lookback=33), 1)
        self.assertIn("changed meanwhile", cm.exception.message)

    def test_corrupt_envelope_is_not_replaced_by_defaults(self):
        sid = lc.create_from_text(text(), "import", file_name="a.json")["id"]
        conn = self.raw()
        conn.execute("DROP TRIGGER strategy_versions_no_update")
        conn.execute("UPDATE strategy_versions SET envelope_json = '{broken' WHERE strategy_id = ?", (sid,))
        conn.commit()
        for fn in (lambda: lc.new_version(sid, text(lookback=33), 1), lambda: lc.duplicate(sid),
                   lambda: lc.get_version(sid, 1)):
            with self.assertRaises(db.StorageUnavailable) as cm:
                fn()
            self.assertIn("unreadable", cm.exception.user_message)

    def test_library_metadata_errors_propagate(self):
        from unittest import mock
        self.assertIsNone(lc.library_metadata(None))
        with mock.patch.object(lc, "library_entries", side_effect=RuntimeError("alphas broken")):
            with self.assertRaises(RuntimeError):
                lc.library_metadata("tsmom")


class RealLibraryTest(LifecycleCase):
    """Integration: the real alpha library (B1) through the real validator."""

    def test_real_library_seeds_cleanly_and_idempotently(self):
        from strategy_platform.strategy import alphas
        st = lc.seed_library()
        self.assertTrue(st["ok"], st["errors"])
        self.assertEqual(st["created"], len(alphas.LIBRARY))
        st = lc.seed_library()
        self.assertEqual((st["created"], st["updated"], st["unchanged"]), (0, 0, len(alphas.LIBRARY)))
        rows = db.strategies_list(origin="library")
        self.assertEqual(len(rows), len(alphas.LIBRARY))
        self.assertTrue(all(r["state"] == "idea" for r in rows))
        key = alphas.keys()[0]
        self.assertEqual(lc.library_metadata(key)["key"], key)
        self.assertIsNone(lc.library_metadata("no-such-key"))


if __name__ == "__main__":
    unittest.main()
