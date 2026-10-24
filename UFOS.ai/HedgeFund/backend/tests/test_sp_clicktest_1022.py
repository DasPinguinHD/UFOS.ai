"""Live click-test 2026-10-22 (backend findings): (1) AI drafts must carry value / min / max / step for every
parameter — explicit rule in both system prompts, a repair message naming the exact missing keys, never auto-filled
(recorded AI answers of the failing session); (6) the "valuation paused" warning is not repeated every 5 s;
(7) the collectors' TLS context verifies with system roots + certifi; (3) stored run cards get a structured stress
warning summary. Offline."""
import copy
import json
import logging
import os
import ssl
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_platform.discovery import prompts as lp  # noqa: E402
from strategy_platform.research import prompts, studio  # noqa: E402

# the spec of the click-test session's repair answer (2026-10-22 12:45, model deepseek-chat-v3): parameters with
# "value" and "integer" only — the validator answered missing_key for min / max
RECORDED = {
    "schema": "ufos.strategy/1", "name": "Sector Momentum Rotation",
    "description": "Monthly rotation strategy holding the top 3 sector ETFs by 6-month momentum, skipping the most "
                   "recent month, equal-weighted and long-only.",
    "tags": ["momentum", "sector"], "category": "momentum",
    "universe": {"symbols": ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLY", "XLB"]}, "timeframe": "1Day",
    "parameters": {"mom_5m_window": {"value": 126, "integer": True}, "mom_1m_window": {"value": 21, "integer": True}},
    "features": {"momentum_5m": {"op": "pct_change", "args": [{"field": "close"}, {"param": "mom_5m_window"}]},
                 "momentum_1m": {"op": "pct_change", "args": [{"field": "close"}, {"param": "mom_1m_window"}]},
                 "momentum_6m_skip_1m": {"op": "sub", "args": [{"ref": "momentum_5m"}, {"ref": "momentum_1m"}]}},
    "signal": {"op": "cs_rank", "args": [{"ref": "momentum_6m_skip_1m"}]},
    "portfolio": {"mode": "long_only", "selection": {"type": "top_n", "n": 3}, "weighting": "equal",
                  "max_weight": 0.3333, "gross_exposure": 1.0},
    "rebalance": {"frequency": "monthly"}, "execution": {"order_type": "market", "timing": "next_open"},
    "costs": {"model": "paper_broker"}}


def full_form(spec):
    s = copy.deepcopy(spec)
    s["parameters"] = {"mom_5m_window": {"value": 126, "min": 105, "max": 147, "step": 21, "integer": True},
                       "mom_1m_window": {"value": 21, "min": 10, "max": 31, "step": 7, "integer": True}}
    return s


class ParameterRuleTest(unittest.TestCase):
    def test_explicit_rule_in_both_system_prompts(self):
        for p in (prompts.SYSTEM_PROMPT, lp.REFINE_SYSTEM_PROMPT):
            self.assertIn('3a. PARAMETERS: every entry in "parameters" MUST be the full object {"value", "min", '
                          '"max", "step"}', p)
            self.assertIn("Never omit min, max or step", p)

    def test_recorded_answer_is_invalid_with_exact_missing_keys_and_never_filled(self):
        spec = copy.deepcopy(RECORDED)
        val = studio._validate_spec(spec)
        self.assertFalse(val["ok"])
        missing = sorted(e["path"] for e in val["errors"] if e["code"] == "missing_key")
        self.assertEqual(missing, ["$.parameters.mom_1m_window.max", "$.parameters.mom_1m_window.min",
                                   "$.parameters.mom_1m_window.step", "$.parameters.mom_5m_window.max",
                                   "$.parameters.mom_5m_window.min", "$.parameters.mom_5m_window.step"])
        self.assertEqual(spec, RECORDED)                                       # nothing auto-filled
        fixes = prompts.parameter_fixes(val["errors"])
        self.assertEqual([(f["parameter"], f["missing"]) for f in fixes],
                         [("mom_1m_window", ["min", "max", "step"]), ("mom_5m_window", ["min", "max", "step"])])
        self.assertTrue(fixes[0]["required_form"].startswith('{"mom_1m_window": {"value": <number>, "min": <number>'))
        msgs = prompts.repair_messages([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                                       json.dumps({"kind": "draft", "spec": spec}), val["errors"])
        last = msgs[-1]["content"]
        self.assertIn("never drop min, max or step", last)
        data = json.loads(last.partition("\n")[2])
        self.assertEqual(set(data), {"validator_errors", "parameter_fixes", "parameter_form"})
        self.assertEqual({f["parameter"] for f in data["parameter_fixes"]}, {"mom_1m_window", "mom_5m_window"})

    def test_full_form_passes_and_step_is_required_for_drafts(self):
        ok = studio._validate_spec(full_form(RECORDED))
        self.assertTrue(ok["ok"], ok["errors"])
        no_step = full_form(RECORDED)
        del no_step["parameters"]["mom_1m_window"]["step"]
        v = studio._validate_spec(no_step)
        self.assertEqual([(e["path"], e["code"]) for e in v["errors"]], [("$.parameters.mom_1m_window.step",
                                                                          "missing_key")])
        self.assertNotIn("step", no_step["parameters"]["mom_1m_window"])


class StepRepairTest(unittest.TestCase):
    def test_missing_step_is_named_explicitly_in_the_repair(self):
        no_step = full_form(RECORDED)
        del no_step["parameters"]["mom_1m_window"]["step"]
        v = studio._validate_spec(no_step)
        fixes = prompts.parameter_fixes(v["errors"])
        self.assertEqual(fixes[0]["instruction"],
                         'Add "step" (a number > 0: the grid step between min and max, integer for windows) to '
                         'parameter "mom_1m_window"; keep value, min, max unchanged. Use "integer": true when it is '
                         'a window, lag or count.')
        msgs = prompts.repair_messages([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                                       "{}", v["errors"])
        self.assertIn('Add \\"step\\"', msgs[-1]["content"])


class ValuationPausedLogTest(unittest.TestCase):
    def test_logged_once_per_symbol_set_then_every_ten_minutes(self):
        from fund import engine as fe
        from strategy_platform.execution.paper_broker import ValuationPaused
        me = types.SimpleNamespace()
        clock = [1000.0]
        import time as _t
        orig = _t.monotonic
        _t.monotonic = lambda: clock[0]
        self.addCleanup(setattr, _t, "monotonic", orig)
        with self.assertLogs("fund.engine", logging.WARNING) as cm:
            for _ in range(5):                                    # five status polls, 5 s apart
                fe.FundEngine._log_valuation_paused(me, ValuationPaused(["AAA"]))
                clock[0] += 5
            fe.FundEngine._log_valuation_paused(me, ValuationPaused(["AAA", "BBB"]))   # the set changed
            clock[0] += 599
            fe.FundEngine._log_valuation_paused(me, ValuationPaused(["AAA", "BBB"]))   # < 10 min: quiet
            clock[0] += 2
            fe.FundEngine._log_valuation_paused(me, ValuationPaused(["AAA", "BBB"]))   # ≥ 10 min: again
        self.assertEqual(len(cm.records), 3)
        self.assertIn("no current quote for AAA, BBB", cm.records[1].getMessage())


class TlsContextTest(unittest.TestCase):
    def test_system_plus_certifi_and_verification_on(self):
        import certifi
        from strategy_platform.core import tls
        ctx = tls.client_context()
        self.assertEqual((ctx.verify_mode, ctx.check_hostname), (ssl.CERT_REQUIRED, True))
        probe = ssl.create_default_context(cafile=certifi.where())
        mine = {c["serialNumber"] for c in ctx.get_ca_certs()}
        self.assertTrue({c["serialNumber"] for c in probe.get_ca_certs()} <= mine)    # every certifi root loaded
        # an explicit check (not an assert, ``python -O``-proof) refuses a non-verifying context
        orig, tls._ctx = ssl.create_default_context, None

        def lax(*a, **k):
            c = orig(*a, **k)
            c.check_hostname = False
            return c
        ssl.create_default_context = lax
        try:
            with self.assertRaises(ssl.SSLError):
                tls.client_context()
        finally:
            ssl.create_default_context, tls._ctx = orig, None
        self.assertNotIn("assert ", open(tls.__file__, encoding="utf-8").read())
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for f in ("newsroom/fred_collector.py", "newsroom/calendar_collector.py", "newsroom/sec_edgar_collector.py"):
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                src = fh.read()
            self.assertIn("aiohttp.TCPConnector(ssl=tls.client_context())", src, f)
            self.assertNotIn("ssl=False", src, f)



class VenueQuoteTest(unittest.TestCase):
    """Retest 2026-10-23: a crypto venue never asks its own quote source for SPY & co. (no error log)."""

    def test_crypto_venue_only_values_what_it_can_hold(self):
        from strategy_platform.execution.brokers import mirror

        class Remote:
            calls = []

            def quote(self, sym):
                self.calls.append(sym)
                raise ValueError("not a Binance pair")
        acc = types.SimpleNamespace(id="binance-demo", broker_name="binance_demo", remote=Remote())
        acc.can_hold = lambda s: mirror.BrokerAccount.can_hold(acc, s)
        self.assertEqual([acc.can_hold(s) for s in ("SPY", "BTC-USD")], [False, True])
        logger = logging.getLogger("strategy_platform.execution.brokers.mirror")
        with self.assertNoLogs(logger, logging.INFO) if hasattr(self, "assertNoLogs") else _quiet():
            self.assertIsNone(mirror.BrokerAccount._own_quote(acc, "SPY"))
        self.assertEqual(Remote.calls, [])
        alp = types.SimpleNamespace(broker_name="alpaca_paper")
        self.assertEqual([mirror.BrokerAccount.can_hold(alp, s) for s in ("SPY", "BTC-USD")], [True, False])
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategy_platform",
                                "live", "runtime.py"), encoding="utf-8").read()
        self.assertIn('(["SPY"] if can_hold is None or can_hold("SPY") else [])', src)


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    unittest.main()
