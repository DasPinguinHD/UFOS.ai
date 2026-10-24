"""/v2 stage-2 routes (SPEC §7.11): operators, universes, strategies list /
detail / version / export, validate, create (user + import), update (409s),
duplicate, transition, preview (fake preview module), input validation
(unknown fields, types, size limit, file name), route registration incl.
the body-size guard, and main.py wiring (seeding, client_max_size). Real
validator (B1), temp hedgefund.db, no network.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_api_strategies -v
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

import strategy_platform.strategy as strategy_pkg
from strategy_platform.api import routes
from strategy_platform.core.errors import DataSourceUnavailable
from strategy_platform.lifecycle import registry as lc
from strategy_platform.storage import db
from strategy_platform.strategy.limits import MAX_TEXT_BYTES

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def call(name, body=routes.NO_BODY, query=None, match=None):
    return asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))


def spec(name="Test momentum", lookback=20, **extra):
    s = {"schema": "ufos.strategy/1", "name": name, "category": "momentum", "tags": ["momentum"],
         "universe": {"symbols": ["SPY", "TLT"]},
         "features": {"mom": {"op": "pct_change", "args": [{"field": "close"}, lookback]}},
         "signal": {"op": "cs_rank", "args": [{"ref": "mom"}]},
         "portfolio": {"mode": "long_only", "selection": {"type": "top_n", "n": 1}, "weighting": "equal"}}
    s.update(extra)
    return s


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sp-apis-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        db.close()
        db.init(os.path.join(self.dir, "hedgefund.db"))
        self.addCleanup(db.close)

    def create(self, origin="user", **kw):
        body = {"text": json.dumps(spec(**kw)), "origin": origin}
        if origin == "import":
            body["file_name"] = "x.json"
        status, payload = call("strategy_create", body)
        self.assertEqual(status, 201, payload)
        return payload["id"]


class CatalogueTest(Case):
    def test_operators(self):
        status, payload = call("dsl_operators")
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"operators", "fields", "ext"})
        self.assertIn("close", payload["fields"])
        self.assertIn("cs_rank", [o["name"] for o in payload["operators"]])
        self.assertEqual({e["name"] for e in payload["ext"]}, {"macro.regime_score", "calendar.event_risk"})
        self.assertEqual(call("dsl_operators", query={"x": "1"})[0], 400)

    def test_universes(self):
        status, payload = call("universes")
        self.assertEqual(status, 200)
        u = payload["universes"][0]
        self.assertEqual(set(u), {"key", "name", "symbols", "note"})
        self.assertEqual(set(u["symbols"][0]), {"symbol", "category", "asset_class"})


class ValidateTest(Case):
    def test_valid_and_invalid_are_200(self):
        status, payload = call("strategy_validate", {"text": json.dumps(spec())})
        self.assertEqual((status, payload["ok"]), (200, True))
        self.assertEqual(set(payload), {"ok", "errors", "warnings", "facts"})
        status, payload = call("strategy_validate", {"text": '{"schema": 1,\n "name": }'})
        self.assertEqual((status, payload["ok"]), (200, False))
        self.assertEqual(payload["errors"][0]["code"], "parse_error")
        self.assertEqual(payload["errors"][0]["line"], 2)

    def test_body_checks(self):
        self.assertEqual(call("strategy_validate", {"text": 1})[0], 400)
        self.assertEqual(call("strategy_validate", {"text": "{}", "extra": 1})[1]["errors"], {"extra": "unknown field"})
        self.assertEqual(call("strategy_validate", ["text"])[0], 400)
        self.assertEqual(call("strategy_validate")[0], 400)
        big = '{"name": "' + "x" * MAX_TEXT_BYTES + '"}'
        status, payload = call("strategy_validate", {"text": big})
        self.assertEqual(status, 400)
        self.assertIn("too large", payload["error"])


class CreateUpdateTest(Case):
    def test_create_user_and_detail_fields(self):
        sid = self.create()
        status, payload = call("strategy_get", match={"id": sid})
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"strategy", "versions", "transitions", "allowed_transitions"})
        s = payload["strategy"]
        for k in ("id", "name", "origin", "category", "state", "current_version", "content_hash", "tags",
                  "updated_at", "ai_generated", "external_text", "library_key", "valid", "description", "library"):
            self.assertIn(k, s)
        self.assertEqual((s["state"], s["valid"], s["tags"]), ("draft", True, ["momentum"]))
        self.assertEqual(set(payload["versions"][0]), {"version", "content_hash", "created_at", "origin"})
        self.assertEqual(set(payload["transitions"][0]), {"id", "ts", "version", "from_state", "to_state", "actor",
                                                          "reason", "evidence"})
        self.assertEqual(set(payload["allowed_transitions"][0]), {"to", "allowed", "reason_if_not"})

    def test_list_with_filters(self):
        self.create()
        self.create(origin="import")
        status, payload = call("strategies_list")
        self.assertEqual((status, len(payload["strategies"])), (200, 2))
        self.assertIn("library_status", payload)
        item = payload["strategies"][0]
        self.assertEqual(set(item), {"id", "name", "origin", "category", "state", "current_version", "content_hash",
                                     "tags", "updated_at", "ai_generated", "external_text", "library_key", "valid",
                                     "created_at", "state_changed_at",
                                     # stage 4 (SPEC §9.7 chips, V2 notes)
                                     "family_id", "contaminated", "holdout_failed_until", "trial_count",
                                     "has_exception"})
        self.assertEqual(len(call("strategies_list", query={"origin": "import"})[1]["strategies"]), 1)
        self.assertEqual(call("strategies_list", query={"state": "bogus"})[0], 400)
        self.assertEqual(call("strategies_list", query={"category": "../x"})[0], 400)
        self.assertEqual(call("strategies_list", query={"sort": "x"})[0], 400)

    def test_create_invalid_returns_validation(self):
        status, payload = call("strategy_create", {"text": json.dumps(spec(signal={"op": "lag", "args": [
            {"field": "close"}, 0]})), "origin": "user"})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["errors"])
        self.assertIn("error", payload)

    def test_create_body_checks(self):
        t = json.dumps(spec())
        self.assertEqual(call("strategy_create", {"text": t, "origin": "library"})[0], 400)
        self.assertEqual(call("strategy_create", {"text": t, "origin": "user", "state": "live"})[0], 400)
        self.assertEqual(call("strategy_create", {"text": t, "origin": "user", "file_name": "a.json"})[0], 400)
        self.assertEqual(call("strategy_create", {"text": t, "origin": "import", "file_sha256": "zz"})[0], 400)
        self.assertEqual(call("strategy_create", {"text": t, "origin": "import", "file_name": 5})[0], 400)

    def test_import_file_name_is_base_name_only(self):
        t = json.dumps(spec(provenance={"state": "validated"}))
        status, res = call("strategy_create", {"text": t, "origin": "import",
                                               "file_name": "C:\\Users\\me\\Secret Folder\\strat.json",
                                               "file_sha256": "AB" * 32})
        self.assertEqual(status, 201)
        status, v = call("strategy_version", match={"id": res["id"], "v": "1"})
        self.assertEqual(v["envelope"]["source_file"]["name"], "strat.json")
        self.assertEqual(v["envelope"]["claimed"], {"state": "validated"})
        self.assertNotIn("Secret Folder", json.dumps(v))

    def test_version_detail_and_export(self):
        sid = self.create()
        status, v = call("strategy_version", match={"id": sid, "v": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(set(v), {"spec", "spec_text", "envelope", "validation", "summary", "library"})
        self.assertEqual(set(v["summary"]), {"universe_text", "signal_text", "features_text", "portfolio_text",
                                             "rebalance_text", "risk_text"})
        self.assertIn("mom", v["summary"]["signal_text"])
        self.assertIn("pct_change", v["summary"]["features_text"]["mom"])
        self.assertEqual(json.loads(v["spec_text"]), v["spec"])
        self.assertEqual(call("strategy_version", match={"id": sid, "v": "2"})[0], 404)
        self.assertEqual(call("strategy_version", match={"id": sid, "v": "-1"})[0], 404)
        status, ex = call("strategy_export", match={"id": sid, "v": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(ex["file_name"], "test-momentum-v1.json")
        exported = json.loads(ex["text"])
        self.assertEqual(exported["provenance"]["exported_from"]["strategy_id"], sid)
        # re-import: claims land in `claimed`, the content hash is unchanged
        status, res = call("strategy_create", {"text": ex["text"], "origin": "import", "file_name": ex["file_name"]})
        self.assertEqual(status, 201)
        a = call("strategy_get", match={"id": sid})[1]["strategy"]["content_hash"]
        b = call("strategy_get", match={"id": res["id"]})[1]["strategy"]["content_hash"]
        self.assertEqual(a, b)

    def test_update_and_conflicts(self):
        sid = self.create()
        status, res = call("strategy_update", {"text": json.dumps(spec(lookback=30)), "base_version": 1},
                           match={"id": sid})
        self.assertEqual((status, res["version"]), (200, 2))
        status, res = call("strategy_update", {"text": json.dumps(spec(lookback=40)), "base_version": 1},
                           match={"id": sid})
        self.assertEqual((status, res["current_version"]), (409, 2))
        self.assertEqual(call("strategy_update", {"text": "{}", "base_version": True}, match={"id": sid})[0], 400)
        self.assertEqual(call("strategy_update", {"text": "{}", "base_version": 1},
                              match={"id": "s-000000000000"})[0], 404)
        lc.seed_library([{"key": "k1", "spec": spec("Lib"), "metadata": {"key": "k1", "family": "momentum"}}])
        lib = db.strategy_by_library_key("k1")["id"]
        status, res = call("strategy_update", {"text": json.dumps(spec()), "base_version": 1}, match={"id": lib})
        self.assertEqual((status, res["error"]), (409, "Library strategies are read-only — duplicate them first."))
        status, res = call("strategy_duplicate", {"name": "My copy"}, match={"id": lib})
        self.assertEqual(status, 201)
        self.assertEqual(call("strategy_get", match={"id": res["id"]})[1]["strategy"]["name"], "My copy")
        self.assertEqual(call("strategy_duplicate", match={"id": lib})[0], 201)
        self.assertEqual(call("strategy_duplicate", {"nam": "x"}, match={"id": lib})[0], 400)


class TransitionTest(Case):
    def test_transition_route(self):
        sid = self.create()
        status, res = call("strategy_transition", {"to": "idea", "reason": "later"}, match={"id": sid})
        self.assertEqual((status, res["strategy"]["state"]), (200, "idea"))
        self.assertIn("allowed_transitions", res)
        status, res = call("strategy_transition", {"to": "validated", "reason": "trust me"}, match={"id": sid})
        self.assertEqual(status, 409)
        self.assertIn("only a candidate can become validated", res["error"])     # stage 4 (SPEC §9.5)
        st, res = call("strategy_transition", {"to": "draft", "reason": " "}, match={"id": sid})
        self.assertEqual((st, res["strategy"]["state"]), (200, "draft"))      # fix1023: the note is optional
        st, res = call("strategy_transition", {"to": "idea"}, match={"id": sid})
        self.assertEqual((st, res["strategy"]["state"]), (200, "idea"))
        self.assertEqual(call("strategy_transition", {"to": "draft", "reason": "x" * 501}, match={"id": sid})[0], 400)
        self.assertEqual(call("strategy_transition", {"to": "nope", "reason": "x"}, match={"id": sid})[0], 400)


class PreviewTest(Case):
    def fake_preview(self, fn):
        mod = types.ModuleType("strategy_platform.strategy.preview")
        mod.preview = fn
        p1 = mock.patch.dict(sys.modules, {"strategy_platform.strategy.preview": mod})
        p2 = mock.patch.object(strategy_pkg, "preview", mod, create=True)
        p1.start(), p2.start()
        self.addCleanup(p2.stop)
        self.addCleanup(p1.stop)

    def test_preview_ok_and_failure(self):
        sid = self.create()
        seen = {}

        def ok(spec_, as_of=None, days=20):
            seen.update(name=spec_["name"], days=days)
            return {"as_of": "2026-10-01", "rows": [], "history": [], "warnings": [], "data": {}}
        self.fake_preview(ok)
        status, res = call("strategy_preview", {"days": 5}, match={"id": sid})
        self.assertEqual((status, res["as_of"], seen["days"]), (200, "2026-10-01", 5))
        self.assertEqual(call("strategy_preview", match={"id": sid})[0], 200)
        self.assertEqual(seen["days"], 5)          # stage 4 review: default and maximum 5 (holdout window)
        self.assertEqual(call("strategy_preview", {"days": 0}, match={"id": sid})[0], 400)
        status, res = call("strategy_preview", {"days": 6}, match={"id": sid})
        self.assertEqual((status, res["errors"]["days"]), (400, "1..5"))
        self.assertEqual(call("strategy_preview", {"days": 250}, match={"id": sid})[0], 400)
        self.assertEqual(call("strategy_preview", {"version": 9}, match={"id": sid})[0], 404)

        def fail(spec_, as_of=None, days=20):
            raise DataSourceUnavailable("alpaca", "equity_bars", "HTTP 503")
        self.fake_preview(fail)
        status, res = call("strategy_preview", {}, match={"id": sid})
        self.assertEqual((status, res["kind"]), (503, "DataSourceUnavailable"))
        self.assertIn("alpaca is unavailable", res["error"])

        def bad_data(spec_, as_of=None, days=20):
            raise ValueError("two daily bars for SPY on 2026-09-30")      # e.g. panel.from_bars
        self.fake_preview(bad_data)
        status, res = call("strategy_preview", {}, match={"id": sid})
        self.assertEqual((status, res["kind"]), (503, "PreviewDataError"))
        self.assertIn("two daily bars for SPY", res["error"])


class PreviewIntegrationTest(Case):
    """The route through B1's real preview (fake bars, fixed clock — no network)."""

    def test_real_preview(self):
        import functools
        import math
        from datetime import datetime, timezone
        from strategy_platform.core import calendars
        from strategy_platform.core.clock import FixedClock
        from strategy_platform.data import pit
        from strategy_platform.strategy import preview as real
        now = datetime(2026, 9, 30, 21, 30, tzinfo=timezone.utc)

        def get_bars(symbol, timeframe, start, end, require_fresh_through=None):
            out = []
            for i, d in enumerate(calendars.trading_days(start.date(), end.date())):
                ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                px = 100 + (5 if symbol == "SPY" else 2) * math.sin(i / 7) + (0.1 if symbol == "SPY" else 0.05) * i
                out.append({"symbol": symbol, "timeframe": "1Day", "ts": ts.isoformat(), "open": px, "high": px + 1,
                            "low": px - 1, "close": px, "volume": 1e6, "vwap": px, "trade_count": 100,
                            "source": "alpaca", "known_at": pit.known_at_iso("equity", "1Day", ts),
                            "fetched_at": now.isoformat()})
            return [b for b in out if b["known_at"] <= end.isoformat()]

        fake = functools.partial(real.preview, get_bars=get_bars, clock=FixedClock(now),
                                 configured_source=lambda data_type: "alpaca")
        sid = self.create()
        with mock.patch.object(real, "preview", fake):
            status, res = call("strategy_preview", {"days": 5}, match={"id": sid})
        self.assertEqual(status, 200, res)
        self.assertEqual({r["symbol"] for r in res["rows"]}, {"SPY", "TLT"})
        self.assertEqual(len(res["history"]), 5)
        self.assertAlmostEqual(sum(r["target_weight"] for r in res["rows"]), 1.0)
        self.assertEqual(call("strategy_preview", {"days": 251}, match={"id": sid})[0], 400)


class StorageDownTest(Case):
    def test_corrupt_record_is_503(self):
        import sqlite3
        sid = self.create()
        conn = sqlite3.connect(db.db_path())
        conn.execute("DROP TRIGGER strategy_versions_no_update")
        conn.execute("UPDATE strategy_versions SET spec_json = 'x' WHERE strategy_id = ?", (sid,))
        conn.commit()
        conn.close()
        status, res = call("strategy_get", match={"id": sid})
        self.assertEqual((status, res["kind"]), (503, "StorageUnavailable"))
        self.assertEqual(call("strategies_list")[0], 503)

    def test_storage_failure_is_503(self):
        db.close()
        status, res = call("strategies_list")
        self.assertEqual((status, res["kind"]), (503, "StorageUnavailable"))


class RegisterTest(Case):
    def test_routes_registered_and_body_limit(self):
        web = types.ModuleType("aiohttp.web")

        class TooLarge(Exception):
            pass

        class Resp:
            def __init__(self, text, status):
                self.text, self.status = text, status
        web.HTTPRequestEntityTooLarge = TooLarge
        web.json_response = lambda data, status=200, dumps=json.dumps: Resp(dumps(data), status)

        class App:
            def __init__(self):
                self.router = types.SimpleNamespace(routes={})
                self.router.add_route = lambda m, p, h: self.router.routes.__setitem__((m, p), h)
        web.Application = App
        pkg = types.ModuleType("aiohttp")
        pkg.web = web
        with mock.patch.dict(sys.modules, {"aiohttp": pkg, "aiohttp.web": web}):
            app = web.Application()
            routes.register(app)
        r = app.router.routes
        for key in (("GET", "/v2/strategies"), ("POST", "/v2/strategies"), ("PUT", "/v2/strategies/{id}"),
                    ("GET", "/v2/strategies/{id}/versions/{v}/export"), ("POST", "/v2/strategies/validate"),
                    ("GET", "/v2/dsl/operators"), ("GET", "/v2/universes")):
            self.assertIn(key, r)

        class BigRequest:
            can_read_body = True
            query, match_info = {}, {}

            async def text(self):
                raise TooLarge()
        resp = asyncio.run(r[("POST", "/v2/strategies/validate")](BigRequest()))
        self.assertEqual(resp.status, 400)
        self.assertIn("too large", resp.text)
        self.assertGreaterEqual(routes.max_body_bytes(), MAX_TEXT_BYTES + 4096)


class MainWiringTest(unittest.TestCase):
    def test_main_seeds_library_and_sets_body_limit(self):
        with open(os.path.join(_BACKEND, "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        code = src.split('"""', 2)[2]
        self.assertIn("sp_lifecycle.seed_library", code)
        self.assertLess(code.index("sp_storage.init"), code.index("sp_lifecycle.seed_library"))
        self.assertIn("client_max_size=", code)
        self.assertIn("v2_routes.max_body_bytes()", code)


if __name__ == "__main__":
    unittest.main()
