"""Market Research MR5 review fixes (SPEC §13 "MR5 backend notes"): M3 client_id idempotency (POST + import),
L6 non-string tool → 400 (not 500), L7 Unicode space separators allowed / control + format characters refused,
L9 loading drawings triggers a split check when none ran today; store v6 migration idempotent.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_drawings_review -v
"""
import os
import sqlite3
import tempfile
import unittest
import uuid
from unittest import mock

try:
    from test_mr_drawings import Case, body, NOW
    from test_mr_data_api import Q as _Q
except ImportError:  # pragma: no cover - run as package
    from tests.test_mr_drawings import Case, body, NOW
    from tests.test_mr_data_api import Q as _Q

from market_research import drawings as dr, store


class ClientIdTest(Case):
    def test_post_retry_returns_existing(self):
        cid = str(uuid.uuid4())
        s1, a = self.call("drawing_create", b=body(client_id=cid))
        self.assertEqual(s1, 201)
        self.assertEqual(a["client_id"], cid)
        s2, b = self.call("drawing_create", b=body(client_id=cid.upper()))      # retry after a timeout
        self.assertEqual((s2, b), (200, a))
        s3, c = self.call("drawing_create", b=body(client_id=cid.replace("-", ""), name="changed"))
        self.assertEqual((s3, c["id"], c["name"]), (200, a["id"], None))        # content not compared
        self.assertEqual(len(store.drawings_list("AAPL")), 1)
        s4, p = self.call("drawing_create", b=dict(body(client_id=cid), symbol="MSFT"))
        self.assertEqual(s4, 409)
        self.assertIn("another symbol", p["error"])
        s5, d = self.call("drawing_create", b=body())
        self.assertEqual((s5, d["client_id"]), (201, None))
        for bad in ("abc", 5, "g" * 32, str(uuid.uuid4()) + "0", ""):
            st, p = self.call("drawing_create", b=body(client_id=bad))
            self.assertEqual(st, 400, bad)
            self.assertIn("client_id", p["errors"])
        self.assertEqual(self.call("drawing_patch", b={"client_id": cid}, match={"id": a["id"]})[0], 400)
        _, f = self.call("drawings_export", (("symbol", "AAPL"),))
        self.assertTrue(all("client_id" not in x for x in f["drawings"]))

    def test_import_retry(self):
        self.create()
        self.create(tool="priceLine")
        _, f = self.call("drawings_export", (("symbol", "AAPL"),))
        cid = str(uuid.uuid4())
        s1, r1 = self.call("drawings_import", (("symbol", "AAPL"), ("client_id", cid)), f)
        self.assertEqual((s1, r1["imported"], r1["repeated"]), (200, 2, False))
        s2, r2 = self.call("drawings_import", (("symbol", "AAPL"), ("client_id", cid)), f)
        self.assertEqual((s2, r2["repeated"], [d["id"] for d in r2["drawings"]]),
                         (200, True, [d["id"] for d in r1["drawings"]]))
        s3, r3 = self.call("drawings_import", (("symbol", "AAPL"), ("client_id", cid), ("mode", "replace")), f)
        self.assertEqual((s3, r3["repeated"]), (200, True))                     # replace retry deletes nothing
        self.assertEqual(len(store.drawings_list("AAPL")), 4)
        s4, r4 = self.call("drawings_import", (("symbol", "AAPL"),), f)          # no client_id: a new import
        self.assertEqual((s4, r4["repeated"], len(store.drawings_list("AAPL"))), (200, False, 6))
        self.assertEqual(self.call("drawings_import", (("symbol", "AAPL"), ("client_id", "x")), f)[0], 400)
        g = dict(f, drawings=[dict(f["drawings"][0], client_id=cid)])
        self.assertEqual(self.call("drawings_import", (("symbol", "AAPL"),), g)[0], 400)   # not a file key


class AlertIdManagedTest(Case):
    """Since MR7 only the alert transaction writes drawings.alert_id (clients cannot)."""

    def test_post_patch_rejected_response_keeps_it(self):
        for v in ("ab" * 16, None):
            st, p = self.call("drawing_create", b=body(tool="alertLine", alert_id=v))
            self.assertEqual(st, 400)
            self.assertTrue(p["errors"]["alert_id"].startswith("alert_id is managed by alerts"))
        d = self.create(tool="alertLine")
        for v in ("cd" * 16, None):
            st, p = self.call("drawing_patch", b={"alert_id": v, "visible": False}, match={"id": d["id"]})
            self.assertEqual(st, 400)
            self.assertEqual(list(p["errors"]), ["alert_id"])
            self.assertTrue(p["errors"]["alert_id"].startswith("alert_id is managed by alerts"))
        self.assertTrue(store.drawing_get(d["id"])["visible"])                     # nothing changed
        store.drawing_update(d["id"], {"alert_id": "cd" * 16}, d["updated_at"])  # what the alert transaction does
        _, lst = self.call("drawings_list", (("symbol", "AAPL"),))
        self.assertEqual(lst["drawings"][0]["alert_id"], "cd" * 16)               # still in responses
        st, p = self.call("drawing_patch", b={"name": "Bell"}, match={"id": d["id"]})
        self.assertEqual((st, p["alert_id"]), (200, "cd" * 16))                   # other edits keep the link

    def test_import_strips_alert_id(self):
        self.create(tool="alertLine")
        self.create()
        _, f = self.call("drawings_export", (("symbol", "AAPL"),))
        st, r = self.call("drawings_import", (("symbol", "AAPL"),), f)
        self.assertEqual((st, r["alert_id_ignored"]), (200, False))
        g = dict(f, drawings=[dict(f["drawings"][0], alert_id="ef" * 16), f["drawings"][1]])
        st, r = self.call("drawings_import", (("symbol", "AAPL"),), g)
        self.assertEqual((st, r["imported"], r["alert_id_ignored"]), (200, 2, True))
        self.assertEqual([d["alert_id"] for d in r["drawings"]], [None, None])
        g2 = dict(f, drawings=[dict(f["drawings"][0], alert_id="ef" * 16, bogus=1)])
        st, p = self.call("drawings_import", (("symbol", "AAPL"),), g2)
        self.assertEqual(st, 400)
        self.assertEqual(list(p["errors"]), ["drawings[0].bogus"])


class TypeAndTextTest(Case):
    def test_non_string_tool_is_400(self):
        for tool in ([], {}, 5, None, True, ["segment"]):
            st, p = self.call("drawing_create", b=dict(body(), tool=tool))
            self.assertEqual(st, 400, tool)
            self.assertIn("tool", p["errors"])
        self.create()
        _, f = self.call("drawings_export", (("symbol", "AAPL"),))
        for tool in ([], {"a": 1}, 3):
            g = dict(f, drawings=[dict(f["drawings"][0], tool=tool)])
            st, p = self.call("drawings_import", (("symbol", "AAPL"),), g)
            self.assertEqual(st, 400, tool)
            self.assertIn("drawings[0].tool", p["errors"])

    def test_unicode_spaces_allowed_controls_refused(self):
        d = self.create(tool="simpleAnnotation", text="Ziel 100 $ — ok\nzweite Zeile\tTab", name=" 52W high ")
        self.assertEqual(d["text"], "Ziel 100 $ — ok\nzweite Zeile\tTab")
        self.assertEqual(d["name"], "52W high")
        for field, val, code in (("text", "a\u0007b", "U+0007"), ("text", "a​b", "U+200B"),
                                 ("text", "rtl‮override", "U+202E"), ("text", "a b", "U+2028"),
                                 ("name", "a\tb", "U+0009"), ("name", "a\nb", "U+000A"), ("text", "x", "U+E000")):
            with self.subTest(field=field, code=code):
                b = body(tool="simpleAnnotation", text="ok")
                b[field] = val
                st, p = self.call("drawing_create", b=b)
                self.assertEqual(st, 400)
                self.assertIn(code, p["errors"][field])
                self.assertIn("control or formatting characters", p["errors"][field])


class SplitCheckOnLoadTest(Case):
    def test_list_triggers_check_when_not_run_today(self):
        self.create()
        calls = []
        with mock.patch.object(dr, "request_check", lambda: calls.append(1)):
            self.call("drawings_list", (("symbol", "AAPL"),))                   # never checked → trigger
            self.assertEqual(len(calls), 1)
            store.meta_set(dr.META_SPLIT_CHECK, '{"state": "ok", "checked_at": "2026-10-14T23:30:00+00:00", "reason": null}')
            self.call("drawings_list", (("symbol", "AAPL"),))                   # yesterday (NY) → trigger
            self.assertEqual(len(calls), 2)
            store.meta_set(dr.META_SPLIT_CHECK, '{"state": "ok", "checked_at": "2026-10-15T04:30:00+00:00", "reason": null}')
            self.call("drawings_list", (("symbol", "AAPL"),))                   # 00:30 NY today → no trigger
            self.assertEqual(len(calls), 2)
            self.call("drawings_list", (("symbol", "BTC-USD"),))                # crypto: never
            self.assertEqual(len(calls), 2)

    def test_request_check_wakes_the_task(self):
        import asyncio
        d = self.create()
        store.drawing_update(d["id"], {}, d["updated_at"], basis_as_of="2026-06-01")   # a split could be due

        class Src:
            calls = 0

            def splits(self, symbols, start, end):
                Src.calls += 1
                return []

        async def main():
            async def long_sleep(s):
                await asyncio.sleep(3600)
            task = asyncio.ensure_future(dr.run(lambda: Src(), sleep=long_sleep, now=lambda: NOW))
            for _ in range(300):
                await asyncio.sleep(0.01)
                if Src.calls:
                    break
            store.meta_set(dr.META_SPLIT_CHECK, '{"state": "ok", "checked_at": "2026-10-14T12:00:00+00:00", "reason": null}')
            await self.api.drawings_list(_Q((("symbol", "AAPL"),)), None, {})
            for _ in range(300):
                await asyncio.sleep(0.01)
                if Src.calls >= 2:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(main())
        self.assertGreaterEqual(Src.calls, 2)


class MigrationTest(unittest.TestCase):
    def test_v6_adds_client_id_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.db")
            conn = sqlite3.connect(path)
            for version, stmts in sorted(store.MIGRATIONS, key=lambda m: m[0]):
                if version >= 6:
                    break
                for sql in stmts:
                    sql(conn) if callable(sql) else conn.execute(sql)
                conn.execute(f"PRAGMA user_version = {version}")
                conn.commit()
            conn.execute("INSERT INTO drawings(id, symbol, tool, points_json, style_json, locked, visible, "
                         "timeframes_json, created_at, updated_at) VALUES('a', 'AAPL', 'priceLine', '[]', '{}', 0, 1, "
                         "'\"all\"', 'x', 'x')")
            conn.commit()
            self.assertEqual(store.migrate(conn), store.SCHEMA_VERSION)
            self.assertEqual(store.migrate(conn), store.SCHEMA_VERSION)
            store._add_column(conn, "drawings", "client_id", "TEXT")            # again: no error
            cols = [r[1] for r in conn.execute("PRAGMA table_info(drawings)")]
            self.assertEqual(cols.count("client_id"), 1)
            conn.execute("UPDATE drawings SET client_id = 'k'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO drawings(id, symbol, tool, points_json, style_json, locked, visible, "
                             "timeframes_json, created_at, updated_at, client_id) VALUES('b', 'AAPL', 'priceLine', "
                             "'[]', '{}', 0, 1, '\"all\"', 'x', 'x', 'k')")
            conn.close()


if __name__ == "__main__":
    unittest.main()
