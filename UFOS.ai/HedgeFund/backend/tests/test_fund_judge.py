"""fund/judge.py — rule score, hysteresis, change detection and the batch LLM
judge with a fake LLM (valid / garbage JSON, unknown symbol, vetoed short,
budget, model modes, carried decisions). Offline, no network.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_judge -v
"""
import asyncio
import json
import unittest

from fund import judge as J
from fund import llm as llm_mod
from fund import params as P
from fund.llm import Budget, LlmUnavailable
from fund.types import AgentSignal, MarketContext, PositionView, RiskAssessment

NOW = "2026-09-28T15:00:00+00:00"


def sig(agent, score, conf, sym="SPY"):
    return AgentSignal(agent=agent, symbol=sym, score=score, confidence=conf, summary=f"{agent} {score:+.2f}")


def sigs(t, m, s=None, sym="SPY"):
    out = {"technical": sig("technical", t[0], t[1], sym), "macro": sig("macro", m[0], m[1], sym)}
    if s is not None:
        out["sentiment"] = sig("sentiment", s[0], s[1], sym)
    return out


class FakeJudgeLlm:
    """Answers per model with a dict symbol -> (action, confidence) or a raw string."""

    def __init__(self, answers=None, raw=None, cost=0.01, exc=None):
        self.answers = answers or {}
        self.raw = raw
        self.cost = cost
        self.exc = exc
        self.calls = []

    async def __call__(self, messages, model, max_tokens, feature, feature_label, temperature=0.2):
        user = messages[-1]["content"]
        data = json.loads(user.split("<<<DATA\n", 1)[1].split("\nDATA>>>", 1)[0])
        symbols = [d["symbol"] for d in data]
        self.calls.append({"model": model, "symbols": symbols, "feature": feature, "temperature": temperature,
                           "messages": messages})
        if self.exc:
            raise self.exc
        usage = {"cost_usd": self.cost, "prompt_tokens": 1000, "completion_tokens": 200}
        if self.raw is not None:
            try:
                parsed = llm_mod.extract_json(self.raw)
            except ValueError:
                raise LlmUnavailable("bad_json", "invalid", usage=usage, model=model)
            return parsed, self.raw, model, usage
        per_model = self.answers.get(model, self.answers.get("*", {}))
        out = []
        for s in symbols:
            a = per_model.get(s, ("LONG", 0.8))
            out.append({"symbol": s, "action": a[0], "confidence": a[1], "rationale": f"{model} says {a[0]}"})
        return out, json.dumps(out), model, usage


def ctx(positions=None):
    return MarketContext(now=NOW, params=P.defaults(), session={}, positions=positions or {})


def params(**kw):
    return P.merge(None, kw)


def run(coro):
    return asyncio.run(coro)


class RuleTest(unittest.TestCase):
    def test_rule_score_and_agreement(self):
        s, agr = J.rule_score(sigs((0.6, 1.0), (-0.2, 0.5)), {"technical": 1.0, "macro": 1.0})
        self.assertAlmostEqual(s, (0.6 - 0.1) / 1.5)
        self.assertAlmostEqual(agr, 1.0 / 1.5)
        # weight 0 and confidence 0 are ignored; risk is never directional
        s2, agr2 = J.rule_score(sigs((0.6, 1.0), (-0.9, 0.0), (-1.0, 1.0)), {"technical": 1.0, "macro": 1.0,
                                                                            "sentiment": 0.0})
        self.assertAlmostEqual((s2, agr2), (0.6, 1.0))
        self.assertEqual(J.rule_score({}, {"technical": 1}), (0.0, 0.0))

    def test_hysteresis(self):
        self.assertEqual(J.rule_action(0.25, None), "LONG")
        self.assertEqual(J.rule_action(-0.2, "LONG"), "SHORT")
        self.assertEqual(J.rule_action(0.05, "LONG"), "FLAT")
        self.assertEqual(J.rule_action(0.12, "LONG"), "LONG")      # in the band: keep
        self.assertEqual(J.rule_action(0.12, "SHORT"), "SHORT")
        self.assertEqual(J.rule_action(0.12, None), "FLAT")

    def test_confidence_and_veto(self):
        self.assertAlmostEqual(J.rule_confidence(0.5, 1.0), 0.8)
        self.assertAlmostEqual(J.rule_confidence(0.5, 0.0), 0.4)
        self.assertEqual(J.rule_confidence(0.9, 1.0), 1.0)
        d = J.rule_decision("SPY", sigs((-0.8, 1), (-0.8, 1)), RiskAssessment("SPY", veto_short=True), None, params())
        self.assertEqual((d.rule_action, d.action, d.source), ("SHORT", "FLAT", "rules"))
        self.assertTrue(d.rationale.startswith("Rule-based:"))

    def test_is_flip(self):
        self.assertTrue(J.is_flip("LONG", "SHORT", True))
        self.assertTrue(J.is_flip("FLAT", "LONG", False))
        self.assertTrue(J.is_flip(None, "SHORT", False))
        self.assertTrue(J.is_flip("LONG", "FLAT", True))
        self.assertFalse(J.is_flip("LONG", "FLAT", False))
        self.assertFalse(J.is_flip("LONG", "LONG", True))


class ChangeDetectionTest(unittest.TestCase):
    def setUp(self):
        self.judged = {"id": 7, "ts": "2026-09-28T13:00:00+00:00", "action": "LONG", "confidence": 0.8,
                       "rationale": "earlier", "source": "llm", "model": "m", "provenance": {"ai_generated": True}}
        self.prev = J.previous_state(self.judged, agent_scores=[{"agent": "technical", "score": 0.6},
                                                                {"agent": "macro", "score": 0.4}])
        self.cur = sigs((0.65, 0.8), (0.4, 0.6))

    def check(self, prev=None, cur=None, rule="LONG", now=NOW, **kw):
        return J.needs_judgement(self.prev if prev is None else prev, cur or self.cur, rule, params(), now, **kw)

    def test_unchanged_is_carried(self):
        self.assertEqual(self.check(), (False, "no material change"))

    def test_reasons(self):
        self.assertTrue(J.needs_judgement(None, self.cur, "LONG", params(), NOW)[0])
        self.assertIn("technical score moved", self.check(cur=sigs((0.4, 0.8), (0.4, 0.6)))[1])
        self.assertIn("differs", self.check(rule="FLAT")[1])
        self.assertIn("h old", self.check(now="2026-09-28T19:30:00+00:00")[1])
        self.assertIn("outside the loop", self.check(prev={**self.prev, "position_changed": True})[1])
        self.assertIn("parameters", self.check(prev={**self.prev, "params_changed": True})[1])
        self.assertIn("vetoes", self.check(risk=RiskAssessment("SPY", veto=True))[1])
        rules_prev = J.previous_state({**self.judged, "source": "rules"}, agent_scores={"technical": 0.6, "macro": 0.4})
        self.assertFalse(self.check(prev=rules_prev)[0])
        self.assertTrue(self.check(prev=rules_prev, llm_possible=True)[0])

    def test_decision_params_changed(self):
        a = params()
        self.assertFalse(J.decision_params_changed(a, params(fees_bps=20)))
        self.assertTrue(J.decision_params_changed(a, params(min_confidence=60)))
        self.assertTrue(J.decision_params_changed(a, params(agent_weights={"macro": 0.5})))


class JudgeTest(unittest.TestCase):
    def setUp(self):
        self.signals = {"SPY": sigs((0.6, 0.9), (0.5, 0.7), sym="SPY"),
                        "TLT": sigs((-0.5, 0.9), (-0.4, 0.7), sym="TLT")}
        self.risk = {"SPY": RiskAssessment("SPY"), "TLT": RiskAssessment("TLT")}
        self.models = llm_mod.models()

    def test_valid_llm_batch(self):
        fake, b = FakeJudgeLlm({"*": {"SPY": ("LONG", 0.82), "TLT": ("SHORT", 1.7)}}), Budget(1.0)
        out = run(J.judge(ctx(), self.signals, self.risk, {}, params(), b, llm=fake))
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual((call["model"], call["feature"], call["temperature"]), (self.models["standard"], "fund-judge", 0.2))
        self.assertEqual(sorted(call["symbols"]), ["SPY", "TLT"])
        self.assertIn("not investment advice", call["messages"][0]["content"])
        self.assertEqual(out["SPY"].action, "LONG")
        self.assertEqual(out["SPY"].source, "llm")
        self.assertEqual(out["SPY"].provenance["generator"], "UFOS.ai/HedgeFund/fund-judge")
        self.assertAlmostEqual(out["TLT"].confidence, 0.017)    # 1 < c <= 100 is a percentage
        pct = run(J.judge(ctx(), self.signals, self.risk, {}, params(), Budget(1.0),
                          llm=FakeJudgeLlm({"*": {"SPY": ("LONG", 82)}})))
        self.assertAlmostEqual(pct["SPY"].confidence, 0.82)     # percent answer normalised
        self.assertAlmostEqual(sum(d.usage["cost_usd"] for d in out.values()), 0.01)
        self.assertAlmostEqual(b.run_spent, 0.01)
        summary = J.summarize(out)
        self.assertEqual(summary["judge_mode"], "llm")
        self.assertAlmostEqual(summary["cost_usd"], 0.01)

    def test_garbage_json_falls_back_to_rules(self):
        b = Budget(1.0)
        out = run(J.judge(ctx(), self.signals, self.risk, {}, params(), b, llm=FakeJudgeLlm(raw="Sorry, no.")))
        for d in out.values():
            self.assertEqual(d.source, "rules")
            self.assertTrue(d.rationale.startswith("Rule-based (AI answer invalid)"))
            self.assertIsNone(d.provenance)
        self.assertEqual(out["SPY"].action, "LONG")
        self.assertEqual(out["TLT"].action, "SHORT")
        self.assertAlmostEqual(b.run_spent, 0.01)
        self.assertAlmostEqual(J.summarize(out)["cost_usd"], 0.01)
        self.assertEqual(J.summarize(out)["judge_mode"], "rules")

    def test_unknown_symbol_and_bad_action(self):
        raw = json.dumps([{"symbol": "SPY", "action": "BUY", "confidence": 0.9, "rationale": "x"},
                          {"symbol": "EVIL", "action": "LONG", "confidence": 1, "rationale": "y"},
                          {"symbol": "TLT", "action": "flat", "confidence": 0.7, "rationale": "z " * 100}])
        out = run(J.judge(ctx(), self.signals, self.risk, {}, params(), Budget(1.0), llm=FakeJudgeLlm(raw=raw)))
        self.assertNotIn("EVIL", out)
        self.assertEqual(out["SPY"].source, "rules")
        self.assertTrue(out["SPY"].rationale.startswith("Rule-based (AI answer invalid for this symbol)"))
        self.assertEqual((out["TLT"].source, out["TLT"].action), ("llm", "FLAT"))
        self.assertLessEqual(len(out["TLT"].rationale.split()), 61)
        self.assertEqual(J.summarize(out)["judge_mode"], "mixed")

    def test_vetoed_short_becomes_flat(self):
        risk = dict(self.risk, TLT=RiskAssessment("TLT", veto_short=True, reasons=["shorts disabled"]))
        fake = FakeJudgeLlm({"*": {"TLT": ("SHORT", 0.9)}})
        out = run(J.judge(ctx(), self.signals, risk, {}, params(), Budget(1.0), llm=fake))
        self.assertEqual((out["TLT"].action, out["TLT"].source), ("FLAT", "llm"))
        self.assertIn("short vetoed", out["TLT"].rationale)
        data = fake.calls[0]["messages"][-1]["content"]
        self.assertIn('"veto_short": true', data)

    def test_budget_exhausted_and_disabled(self):
        fake = FakeJudgeLlm()
        b = Budget(0.05)
        b.add(0.05)
        out = run(J.judge(ctx(), self.signals, self.risk, {}, params(ai_daily_budget_usd=0.05), b, llm=fake))
        self.assertEqual(fake.calls, [])
        self.assertTrue(all(d.rationale.startswith("Rule-based (AI budget reached)") for d in out.values()))
        out = run(J.judge(ctx(), self.signals, self.risk, {}, params(ai_daily_budget_usd=0.0), Budget(0), llm=fake))
        self.assertEqual(fake.calls, [])
        self.assertTrue(all(d.rationale.startswith("Rule-based (AI disabled") for d in out.values()))
        # estimate does not fit the remaining budget -> no call either
        tiny = Budget(1e-7)
        out = run(J.judge(ctx(), self.signals, self.risk, {}, params(), tiny, llm=fake))
        self.assertEqual(fake.calls, [])
        self.assertTrue(out["SPY"].rationale.startswith("Rule-based (AI budget reached)"))

    def test_no_key(self):
        fake = FakeJudgeLlm(exc=LlmUnavailable("no_key", "OPENROUTER_API_KEY is not set"))
        out = run(J.judge(ctx(), self.signals, self.risk, {}, params(), Budget(1.0), llm=fake))
        self.assertTrue(out["SPY"].rationale.startswith("Rule-based (AI not configured)"))

    def test_premium_mode(self):
        fake = FakeJudgeLlm()
        run(J.judge(ctx(), self.signals, self.risk, {}, params(judge_model_mode="premium"), Budget(5.0), llm=fake))
        self.assertEqual([c["model"] for c in fake.calls], [self.models["premium"]])

    def test_premium_on_flip_only_for_flips(self):
        std, prem = self.models["standard"], self.models["premium"]
        judged = {"id": 1, "ts": "2026-09-28T14:50:00+00:00", "action": "LONG", "confidence": 0.8,
                  "rationale": "r", "source": "llm", "model": std}
        # both symbols need judgement (fresh params change); SPY stays LONG, TLT flips LONG -> SHORT
        previous = {s: J.previous_state({**judged, "id": i}, params_changed=True) for i, s in enumerate(["SPY", "TLT"])}
        fake = FakeJudgeLlm({std: {"SPY": ("LONG", 0.8), "TLT": ("SHORT", 0.7)},
                             prem: {"TLT": ("FLAT", 0.6)}})
        b = Budget(5.0)
        pos = {"TLT": PositionView("TLT", 10, 90.0, 90.0)}
        out = run(J.judge(ctx(pos), self.signals, self.risk, previous, params(judge_model_mode="premium_on_flip"),
                          b, llm=fake))
        self.assertEqual([c["model"] for c in fake.calls], [std, prem])
        self.assertEqual(fake.calls[1]["symbols"], ["TLT"])
        self.assertEqual((out["TLT"].action, out["TLT"].model), ("FLAT", prem))     # premium wins
        self.assertEqual((out["SPY"].action, out["SPY"].model), ("LONG", std))
        self.assertAlmostEqual(out["TLT"].usage["cost_usd"], 0.005 + 0.01)
        self.assertAlmostEqual(sum(d.usage["cost_usd"] for d in out.values()), b.run_spent)

    def test_premium_on_flip_without_flips_is_one_call(self):
        fake = FakeJudgeLlm({"*": {"SPY": ("FLAT", 0.5), "TLT": ("FLAT", 0.5)}})
        run(J.judge(ctx(), self.signals, self.risk, {}, params(judge_model_mode="premium_on_flip"), Budget(5), llm=fake))
        self.assertEqual(len(fake.calls), 1)

    def test_carried_decisions(self):
        judged = {"id": 42, "ts": "2026-09-28T14:00:00+00:00", "action": "LONG", "confidence": 0.8,
                  "rationale": "AI said long", "source": "llm", "model": "m", "provenance": {"ai_generated": True}}
        previous = {"SPY": J.previous_state(judged, agent_scores={"technical": 0.6, "macro": 0.5})}
        fake = FakeJudgeLlm({"*": {"TLT": ("SHORT", 0.8)}})
        out = run(J.judge(ctx(), self.signals, self.risk, previous, params(), Budget(1.0), llm=fake))
        self.assertEqual(fake.calls[0]["symbols"], ["TLT"])
        spy = out["SPY"]
        self.assertEqual((spy.source, spy.action, spy.carried_from, spy.rationale), ("carried", "LONG", 42, "AI said long"))
        self.assertIsNone(spy.usage)
        self.assertEqual(spy.provenance, {"ai_generated": True})
        self.assertEqual(J.summarize(out)["judge_mode"], "llm")
        # everything carried -> no call at all, mode "none"
        fake2 = FakeJudgeLlm()
        out2 = run(J.judge(ctx(), {"SPY": self.signals["SPY"]}, self.risk, previous, params(), Budget(1.0), llm=fake2))
        self.assertEqual(fake2.calls, [])
        self.assertEqual(J.summarize(out2)["judge_mode"], "none")

    def test_hysteresis_uses_previous_action_in_fallback(self):
        weak = {"SPY": sigs((0.12, 1.0), (0.12, 1.0))}
        previous = {"SPY": J.previous_state({"id": 1, "ts": "2026-09-27T10:00:00+00:00", "action": "LONG",
                                             "confidence": 0.7, "source": "rules"})}
        out = run(J.judge(ctx(), weak, {}, previous, params(ai_daily_budget_usd=0), Budget(0)))
        self.assertEqual(out["SPY"].action, "LONG")
        self.assertEqual(out["SPY"].rule_action, "LONG")


class ReviewFixesJudgeTest(unittest.TestCase):
    """Review 2026-09-26: rule-action change detection, confidence parsing,
    timeout charging, model modes."""

    def setUp(self):
        self.signals = {"SPY": sigs((0.6, 0.9), (0.5, 0.7), sym="SPY")}      # rules: LONG
        self.risk = {"SPY": RiskAssessment("SPY")}
        self.models = llm_mod.models()
        # the LLM said FLAT last time although the rules said LONG
        self.judged = {"id": 3, "ts": "2026-09-28T14:30:00+00:00", "action": "FLAT", "confidence": 0.7,
                       "rationale": "AI disagrees", "source": "llm", "model": "m", "rule_score": 0.55}

    def test_llm_disagreement_is_not_rejudged_every_cycle(self):
        scores = {"technical": 0.6, "macro": 0.5}
        prev = {"SPY": J.previous_state(self.judged, agent_scores=scores, last_rule_action="LONG")}
        fake = FakeJudgeLlm()
        out = run(J.judge(ctx(), self.signals, self.risk, prev, params(), Budget(1.0), llm=fake))
        self.assertEqual(fake.calls, [])
        self.assertEqual((out["SPY"].source, out["SPY"].action, out["SPY"].rule_action), ("carried", "FLAT", "LONG"))
        # derived from the stored rule_score when the engine has no memory (restart)
        derived = J.previous_state(self.judged, agent_scores=scores)
        self.assertEqual(derived["last_rule_action"], "LONG")
        self.assertEqual(J.needs_judgement(derived, self.signals["SPY"], "LONG", params(), NOW),
                         (False, "no material change"))

    def test_rule_action_change_still_triggers(self):
        prev = J.previous_state(self.judged, agent_scores={"technical": 0.6, "macro": 0.5}, last_rule_action="FLAT")
        need, why = J.needs_judgement(prev, self.signals["SPY"], "LONG", params(), NOW)
        self.assertTrue(need)
        self.assertIn("rule action changed FLAT → LONG", why)
        # unknown previous rule action (score in the hysteresis band) -> compare with the last action
        banded = J.previous_state({**self.judged, "rule_score": 0.12}, agent_scores={"technical": 0.6, "macro": 0.5})
        self.assertIsNone(banded["last_rule_action"])
        self.assertIn("differs from last action", J.needs_judgement(banded, self.signals["SPY"], "LONG", params(),
                                                                    NOW)[1])

    def test_rule_action_from_score(self):
        self.assertEqual(J.rule_action_from_score(0.3), "LONG")
        self.assertEqual(J.rule_action_from_score(-0.2), "SHORT")
        self.assertEqual(J.rule_action_from_score(0.01), "FLAT")
        self.assertIsNone(J.rule_action_from_score(0.1))
        self.assertIsNone(J.rule_action_from_score(None))
        self.assertIsNone(J.rule_action_from_score("x"))

    def test_confidence_parsing(self):
        items = {s: {"risk": RiskAssessment(s)} for s in ("A", "B", "C", "D", "E", "F", "G", "H")}
        raw = [{"symbol": "A", "action": "LONG", "confidence": 3},          # 3 %
               {"symbol": "B", "action": "LONG", "confidence": "82"},       # 82 %
               {"symbol": "C", "action": "LONG", "confidence": 0.4},
               {"symbol": "D", "action": "LONG", "confidence": 150},        # invalid
               {"symbol": "E", "action": "LONG", "confidence": -0.2},       # invalid
               {"symbol": "F", "action": "LONG", "confidence": 10 ** 400},  # invalid, no OverflowError
               {"symbol": "G", "action": "LONG", "confidence": True},       # invalid
               {"symbol": "H", "action": "LONG", "confidence": 1}]          # 1 = 100 %
        out = J.validate_answer(raw, items)
        self.assertAlmostEqual(out["A"][1], 0.03)
        self.assertAlmostEqual(out["B"][1], 0.82)
        self.assertAlmostEqual(out["C"][1], 0.4)
        self.assertAlmostEqual(out["H"][1], 1.0)
        for s in ("D", "E", "F", "G"):
            self.assertNotIn(s, out)
        res = run(J.judge(ctx(), self.signals, self.risk, {}, params(), Budget(1.0),
                          llm=FakeJudgeLlm({"*": {"SPY": ("LONG", 250)}})))
        self.assertEqual(res["SPY"].source, "rules")
        self.assertTrue(res["SPY"].rationale.startswith("Rule-based (AI answer invalid for this symbol)"))

    def test_timeout_charges_the_estimate(self):
        b = Budget(1.0)
        fake = FakeJudgeLlm(exc=LlmUnavailable("error", "timeout", attempted=True))
        out = run(J.judge(ctx(), self.signals, self.risk, {}, params(), b, llm=fake))
        self.assertEqual(out["SPY"].source, "rules")
        self.assertGreater(b.run_spent, 0.0)
        b2 = Budget(1.0)
        run(J.judge(ctx(), self.signals, self.risk, {}, params(), b2,
                    llm=FakeJudgeLlm(exc=LlmUnavailable("no_key", "no key"))))
        self.assertEqual(b2.run_spent, 0.0)                  # never sent -> nothing charged

    def test_unreadable_budget_means_rules(self):
        fake = FakeJudgeLlm()
        out = run(J.judge(ctx(), self.signals, self.risk, {}, params(), Budget(1.0, spent_fn=lambda: None), llm=fake))
        self.assertEqual(fake.calls, [])
        self.assertTrue(out["SPY"].rationale.startswith("Rule-based (AI budget reached)"))

    def test_premium_model_only_when_selected(self):
        std, prem = self.models["standard"], self.models["premium"]
        for mode, expected in (("standard", [std]), ("premium_on_flip", [std]), ("premium", [prem])):
            fake = FakeJudgeLlm({"*": {"SPY": ("FLAT", 0.5)}})
            run(J.judge(ctx(), self.signals, self.risk, {}, params(judge_model_mode=mode), Budget(5.0), llm=fake))
            self.assertEqual([c["model"] for c in fake.calls], expected, mode)

    def test_prompt_carries_block_short_increase(self):
        fake = FakeJudgeLlm()
        risk = {"SPY": RiskAssessment("SPY", block_short_increase=True, reasons=["short exposure cap 30% reached"])}
        run(J.judge(ctx(), self.signals, risk, {}, params(), Budget(1.0), llm=fake))
        data = json.loads(fake.calls[0]["messages"][-1]["content"].split("<<<DATA\n", 1)[1].split("\nDATA>>>")[0])
        self.assertTrue(data[0]["risk"]["block_short_increase"])
        # the cap is no veto: an LLM SHORT stays SHORT (sizing only blocks the increase)
        out = run(J.judge(ctx(), self.signals, risk, {}, params(), Budget(1.0),
                          llm=FakeJudgeLlm({"*": {"SPY": ("SHORT", 0.9)}})))
        self.assertEqual(out["SPY"].action, "SHORT")


if __name__ == "__main__":
    unittest.main()
