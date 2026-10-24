"""Stage 10 S10-C R1 — /v2/council/* (SP §22.11) and the model-settings source of core/llm (S10-E, ENV_API §4/§9):
state before / after the switch-over, runs + run detail, "Run now" (503 / 202), params and broker-params (409 / 404
before the switch-over, strict validation 400, audit-first writes), the Council orders switch (OFF always, ON only
when switched over and not halted, audited), and ``core.llm.models()`` never reading the process environment (an
unreadable settings source raises instead of defaulting). Temp databases, fakes — no network."""
import asyncio
import os
import sys
import types
import unittest
from unittest import mock

try:
    from test_s10_council_core import open_council, switched_over
    from test_s10_council_service import FRI_1100, CouncilCase
except ImportError:  # pragma: no cover
    from tests.test_s10_council_core import open_council, switched_over
    from tests.test_s10_council_service import FRI_1100, CouncilCase

from strategy_platform.api import council_routes as cr
from strategy_platform.api.routes import NO_BODY, ROUTES, V2Api
from strategy_platform.core import llm as llm_mod
from strategy_platform.council import params as cp
from strategy_platform.council import service as svc_mod
from strategy_platform.council import store as cs
from strategy_platform.execution import accounts
from strategy_platform.storage import db

try:
    from sp_trading_testkit import TradingCase
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import TradingCase


def call(name, query=None, body=NO_BODY, match=None):
    return asyncio.run(getattr(V2Api(), name)(query or {}, body, match or {}))


def audits(action):
    with db._connect() as conn:
        return [r["action"] for r in conn.execute("SELECT action FROM audit WHERE action = ?", (action,))]


class RegistrationTest(unittest.TestCase):
    def test_routes_registered_once(self):
        council = [r for r in ROUTES if r[1].startswith("/v2/council/")]
        self.assertEqual(len(council), len(cr.COUNCIL_ROUTES))
        for _m, _p, name, _b in council:
            self.assertTrue(callable(getattr(V2Api, name)))


class BeforeSwitchOverTest(TradingCase):
    now = FRI_1100

    def setUp(self):
        super().setUp()
        svc_mod.set_default(None)
        self.addCleanup(svc_mod.set_default, None)

    def test_state_and_reads(self):
        st, body = call("council_state")
        self.assertEqual(st, 200)
        self.assertEqual((body["active"], body["task"], body["label"]), (False, "not_active", svc_mod.LABEL))
        self.assertIsNone(body["last_run"])
        self.assertEqual(call("council_runs"), (200, {"runs": []}))
        self.assertEqual(call("council_runs", {"limit": "0"})[0], 400)
        self.assertEqual(call("council_runs", {"x": "1"})[0], 400)
        self.assertEqual(call("council_run_detail", match={"id": "7"})[0], 404)
        self.assertEqual(call("council_run_detail", match={"id": "x"})[0], 400)
        st, body = call("council_params_get")
        self.assertEqual((st, body["active"], body["params"]), (200, False, None))
        self.assertEqual(body["limits"]["interval_minutes"]["step"], 5)

    def test_writes_refused(self):
        self.assertEqual(call("council_run")[0], 503)                       # no service built
        self.assertEqual(call("council_params_put", body={"min_confidence": 0.5})[0], 409)
        self.assertEqual(call("council_broker_params_get")[0], 404)         # paper-council not migrated
        self.assertEqual(call("council_broker_params_put", body={"fees_bps": 6.0})[0], 404)
        st, body = call("council_orders", body={"enabled": True})
        self.assertEqual((st, body["code"]), (409, "not_active"))
        st, body = call("council_orders", body={"enabled": False})         # OFF always works
        self.assertEqual((st, body["orders_enabled"]), (200, False))
        self.assertEqual(call("council_orders", body={"enabled": "yes"})[0], 400)
        self.assertEqual(call("council_orders", body={"enabled": False, "x": 1})[0], 400)


class AfterSwitchOverTest(CouncilCase):
    def setUp(self):
        super().setUp()
        svc_mod.set_default(self.svc)
        self.addCleanup(svc_mod.set_default, None)

    def test_state_after_a_run(self):
        out = self.run_cycle()
        st, body = call("council_state")
        self.assertEqual(st, 200)
        self.assertEqual((body["active"], body["task"], body["orders_enabled"]), (True, "idle", True))
        self.assertEqual(body["last_run"]["id"], out["run_id"])
        self.assertEqual({s["symbol"] for s in body["symbols"]}, {"SPY", "BTC-USD"})
        self.assertTrue(all(s["execution"]["order_state"] == "filled" for s in body["symbols"]))
        self.assertEqual(body["budget"]["status"], "ok")
        self.assertIn("not validated", body["record"]["text"])
        st, d = call("council_run_detail", match={"id": str(out["run_id"])})
        self.assertEqual((st, d["id"]), (200, out["run_id"]))

    def test_run_now(self):
        st, body = call("council_run")
        self.assertEqual((st, body["orders"], body["started"]), (202, {"equity": True, "crypto": True}, True))
        self.assertEqual(cs.run_detail(body["run_id"])["status"], "running")
        self.assertEqual(call("council_run")[0], 409)                       # one manual run at a time
        self.assertEqual(call("council_run", body={"x": 1})[0], 400)

    def test_params_put(self):
        v0 = cs.latest_params()[0]
        st, body = call("council_params_put", body={"interval_minutes": 7})
        self.assertEqual(st, 400)
        self.assertIn("interval_minutes", body["errors"])
        self.assertEqual(call("council_params_put", body={"drawdown_stop_pct": 10})[0], 400)    # dropped key
        st, body = call("council_params_put", body={"interval_minutes": 20, "reason": "slower"})
        self.assertEqual((st, body["version"], body["params"]["interval_minutes"]), (200, v0 + 1, 20))
        self.assertEqual(cs.latest_params()[1]["interval_minutes"], 20)
        self.assertEqual(audits("council_params"), ["council_params"])

    def test_broker_params(self):
        st, body = call("council_broker_params_get")
        self.assertEqual((st, body["override"]), (200, None))
        self.assertEqual(set(body["effective"]) >= set(cp.BROKER_KEYS), True)
        self.assertEqual(call("council_broker_params_put", body={"min_confidence": 0.5})[0], 400)  # not broker
        st, body = call("council_broker_params_put", body={"fees_bps": 6.0})
        self.assertEqual((st, body["override"], body["effective"]["fees_bps"]), (200, {"fees_bps": 6.0}, 6.0))
        self.assertEqual(cs.latest_broker_override()[1], {"fees_bps": 6.0})
        self.assertEqual(audits("council_broker_params"), ["council_broker_params"])

    def test_orders_switch(self):
        st, body = call("council_orders", body={"enabled": False, "reason": "pause"})
        self.assertEqual((st, body["orders_enabled"]), (200, False))
        self.assertFalse(cs.orders_enabled())
        st, body = call("council_orders", body={"enabled": True})
        self.assertEqual((st, body["orders_enabled"]), (200, True))
        self.assertTrue(cs.orders_enabled())
        self.assertEqual(audits("council_orders_on"), ["council_orders_on"])
        accounts.get(accounts.COUNCIL).set_halt(True, "kill", "test")
        call("council_orders", body={"enabled": False})
        st, body = call("council_orders", body={"enabled": True})
        self.assertEqual((st, body["code"]), (409, "halted"))
        self.assertFalse(cs.orders_enabled())


class ModelSettingsTest(unittest.TestCase):
    """S10-E: the model ids come from the HedgeFund settings source (newsroom.ai), never from the environment."""

    def test_no_environment_read(self):
        with mock.patch.object(llm_mod, "_newsroom_ai", return_value=None), \
                mock.patch.dict(os.environ, {"OPENROUTER_MODEL": "env/model",
                                             "OPENROUTER_ANALYSIS_PREMIUM_MODEL": "env/premium"}):
            self.assertEqual(llm_mod.models(), llm_mod.DEFAULT_MODELS)

    def test_settings_source_used_and_strict(self):
        ai = types.SimpleNamespace(analysis_models=lambda: {"standard": "s/one", "premium": "p/two"})
        with mock.patch.object(llm_mod, "_newsroom_ai", return_value=ai):
            self.assertEqual(llm_mod.models(), {"standard": "s/one", "premium": "p/two"})

        def broken():
            raise ValueError("data/settings.json: openrouter_model: must be a string")
        ai = types.SimpleNamespace(analysis_models=broken)
        with mock.patch.object(llm_mod, "_newsroom_ai", return_value=ai):
            with self.assertRaises(llm_mod.LlmUnavailable) as cm, self.assertLogs(llm_mod.logger, "WARNING"):
                llm_mod.models()
            self.assertEqual(cm.exception.reason, "error")
            self.assertIn("openrouter_model", cm.exception.message)
            self.assertIsNone(svc_mod._premium_model())

    def test_no_os_import_left(self):
        self.assertNotIn("os", vars(llm_mod))
        self.assertNotIn("dotenv", sys.modules.get(llm_mod.__name__).__doc__ or "")


if __name__ == "__main__":
    unittest.main()
