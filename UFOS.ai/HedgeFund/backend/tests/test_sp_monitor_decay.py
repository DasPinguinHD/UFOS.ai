"""Stage 9 (SPEC §19.1/§19.2): live vs expected + the decay monitor — the documented rule fires on simulated decay and
not on noise (plan acceptance, seeded Monte Carlo), the drawdown / volatility rules, no look-ahead (a day's check uses
only returns up to that day), pause → alert + audit, the user's decision (keep running re-arms, demote aborts the
run), the monitoring payloads. Offline, temp databases, synthetic returns. Paper only."""
import json
import math
import random
import unittest
from datetime import date

try:
    from sp_monitor_testkit import EXPECTED, MonCase, after_close, write_returns
except ImportError:  # pragma: no cover
    from tests.sp_monitor_testkit import EXPECTED, MonCase, after_close, write_returns

from strategy_platform.execution.errors import TradingError
from strategy_platform.live import incubation
from strategy_platform.monitoring import alerts, decay, expected, service
from strategy_platform.storage import db

MU, SD = 0.0004, 0.01
# the calibration setup of SPEC §19.14 (M5): per basis (calendar, holdout sessions, live sessions per year); the
# expected basis is ESTIMATED from a simulated holdout of that length (as in reality), noise = the true process
BASES = {"equity_24m": ("nyse", 504, 252), "equity_12m": ("nyse", 252, 252), "equity_6m": ("nyse", 126, 252),
         "crypto_12m": ("utc", 365, 365)}
TRUE = {"nyse": (0.0004, 0.01), "utc": (0.0012, 0.03)}


def holdout_basis(rng, cal, h):
    mu, sd = TRUE[cal]
    xs = [rng.gauss(mu, sd) for _ in range(h)]
    m = sum(xs) / h
    s = math.sqrt(sum((x - m) ** 2 for x in xs) / (h - 1))
    return {"mean_daily": m, "std_daily": s, "max_drawdown": expected.drawdown_now(xs)[1], "sessions": h,
            "calendar": cal}


def first_trigger(exp, lr, cal="nyse", start=0):
    for k in range(start + 1, len(lr) + 1):
        a = decay.assess(exp, lr[:k], cal, {"index": start} if start else None)
        if a["triggered"]:
            return k - start, a["triggered"]
    return None, None


class RuleCalibrationTest(unittest.TestCase):
    """The acceptance (plan + review M5): ≈ 5 % noise pauses per strategy-year for every holdout basis, decay caught."""

    def scenario(self, basis, gen, seeds=200, start=0):
        cal, h, n = BASES[basis]
        mu, sd = TRUE[cal]
        hits = []
        for s in range(seeds):
            rng = random.Random(7000 + s)
            exp = holdout_basis(rng, cal, h)
            self.assertEqual(decay.basis_of(exp, cal), basis)
            k, _t = first_trigger(exp, gen(rng, n, mu, sd), cal, start)
            if k:
                hits.append(k)
        return sorted(hits)

    def test_noise_and_decay_per_basis(self):
        for basis in BASES:
            with self.subTest(basis=basis):
                noise = self.scenario(basis, lambda r, n, mu, sd: [r.gauss(mu, sd) for _ in range(n)])
                self.assertLessEqual(len(noise) / 200, 0.06, (basis, noise))
                dec = self.scenario(basis, lambda r, n, mu, sd: [r.gauss(mu - 0.5 * sd, sd) for _ in range(120)])
                self.assertGreaterEqual(len(dec) / 200, 0.85, basis)
                self.assertLessEqual(dec[len(dec) // 2], 50, basis)

    def test_decay_after_a_good_start(self):
        hits = self.scenario("equity_24m", lambda r, n, mu, sd: [r.gauss(mu, sd) for _ in range(40)] +
                             [r.gauss(mu - 0.5 * sd, sd) for _ in range(120)])
        self.assertGreaterEqual(len(hits) / 200, 0.9)

    def test_volatility_regime(self):
        hits = self.scenario("equity_24m", lambda r, n, mu, sd: [r.gauss(mu, 2.5 * sd) for _ in range(60)])
        self.assertGreaterEqual(len(hits), 195)
        self.assertLessEqual(hits[int(0.95 * len(hits)) - 1], 32)      # vol needs 20 sessions + 3 consecutive

    def test_keep_running_noise_after_the_decision(self):
        """M4: after "keep running" at session 60 only the returns after it count — a year of noise afterwards
        re-pauses about as rarely as a fresh start."""
        hits = self.scenario("equity_24m", lambda r, n, mu, sd: [r.gauss(mu - 0.6 * sd, sd) for _ in range(60)] +
                             [r.gauss(mu, sd) for _ in range(n)], start=60)
        self.assertLessEqual(len(hits) / 200, 0.07, hits)


class RuleUnitTest(unittest.TestCase):
    EXP = {"mean_daily": MU, "std_daily": SD, "max_drawdown": 0.05}

    def test_drawdown_fires_at_once(self):
        a = decay.assess(self.EXP, [0.001, -0.05, -0.06])
        self.assertEqual(a["triggered"], ["drawdown"])
        r = {x["rule"]: x for x in a["rules"]}
        self.assertAlmostEqual(r["drawdown"]["threshold"], 0.10)
        self.assertIn("needs 20 sessions", r["return_band"]["message"])

    def test_insufficient_and_watch(self):
        self.assertEqual(decay.assess(self.EXP, [0.0] * 5)["state"], "insufficient")
        lr = [MU] * 20 + [-0.06]               # w=21: S ≈ −0.052 > 21μ − 3σ√21 ≈ −0.129 → not breached
        self.assertIn(decay.assess(self.EXP, lr)["state"], ("ok", "watch"))
        bad = [MU] * 20 + [-0.04] * 3          # drawdown 11.4 % > 10 %
        self.assertEqual(decay.assess(self.EXP, bad)["triggered"], ["drawdown"])

    def test_return_band_needs_three_consecutive(self):
        exp = dict(self.EXP, max_drawdown=1.0)             # isolate the band rule
        lr = [-0.004] * 20                                  # S = −0.08 vs 20μ − 3σ√20 = −0.126 → not breached
        self.assertNotIn("return_band", decay.assess(exp, lr)["triggered"])
        lr = [-0.009] * 20                                  # S = −0.18 < −0.126 from n = 20 on
        a = decay.assess(exp, lr)
        r = {x["rule"]: x for x in a["rules"]}["return_band"]
        self.assertTrue(r["breached"])
        self.assertEqual(r["count"], 1)
        self.assertEqual(a["state"], "watch")
        self.assertEqual(decay.assess(exp, lr + [-0.009, -0.009])["triggered"], ["return_band"])

    def test_rearm_after_keep_running(self):
        exp = dict(self.EXP, max_drawdown=1.0)
        lr = [-0.009] * 22
        self.assertEqual(decay.assess(exp, lr)["triggered"], ["return_band"])
        rearm = {"index": 22}
        a = decay.assess(exp, lr + [-0.009], rearm=rearm)                  # a fresh sample of 1 session
        self.assertEqual((a["triggered"], a["n"], a["since_index"]), ([], 1, 22))
        self.assertIn("since your keep-running decision", a["rules"][0]["message"])
        self.assertEqual(decay.assess(exp, lr + [-0.009] * 21, rearm=rearm)["triggered"], [])   # 21 < 20 + 3 − 1
        self.assertEqual(decay.assess(exp, lr + [-0.009] * 22, rearm=rearm)["triggered"], ["return_band"])
        # the drawdown is measured from the decision: the old drawdown does not count again
        e2 = {"mean_daily": MU, "std_daily": SD, "max_drawdown": 0.05}
        dd_lr = [0.0, -0.11]
        self.assertEqual(decay.assess(e2, dd_lr)["triggered"], ["drawdown"])
        self.assertEqual(decay.assess(e2, dd_lr + [-0.05], rearm={"index": 2})["triggered"], [])
        self.assertEqual(decay.assess(e2, dd_lr + [-0.06, -0.06], rearm={"index": 2})["triggered"], ["drawdown"])

    def test_basis_selection(self):
        self.assertEqual(decay.basis_of({"sessions": 504}), "equity_24m")
        self.assertEqual(decay.basis_of({"sessions": 250}), "equity_12m")
        self.assertEqual(decay.basis_of({"sessions": 120}), "equity_6m")
        self.assertEqual(decay.basis_of({"sessions": 365}, "utc"), "crypto_12m")
        self.assertEqual(decay.rules_for({"sessions": 120})["DD_FACTOR"], 3.5)

    def test_rules_payload(self):
        p = decay.rules_payload()
        keys = {"MIN_SESSIONS", "RETURN_WINDOW", "RETURN_Z", "RETURN_CONSECUTIVE", "DD_FACTOR", "VOL_WINDOW",
                "VOL_FACTOR", "VOL_CONSECUTIVE"}
        self.assertEqual(set(p["rules"]), keys)
        self.assertEqual(set(p["by_basis"]), set(BASES))
        self.assertTrue(all(set(v) == keys for v in p["by_basis"].values()))
        self.assertEqual(p["default_basis"], "equity_24m")
        self.assertEqual(len(p["text"]), 4)


class MonitorIntegrationTest(MonCase):
    def test_realised_from_the_sub_ledger(self):
        sid, run = self.start_run()
        rets = [0.001, -0.002, 0.003]
        days = write_returns(run, rets)
        got = expected.realised(run, days[-1])
        self.assertEqual([d for d, _x in got], [d.isoformat() for d in days])
        for (_d, x), r in zip(got, rets):
            self.assertAlmostEqual(x, math.log(1 + r), places=9)

    def test_no_look_ahead_and_pause(self):
        sid, run = self.start_run()
        rets = [0.0005] * 25 + [-0.04] * 5
        days = write_returns(run, rets)
        # evaluated at the close of day 25: only 25 checks, all ok
        res = decay.check_run(run, after_close(days[24]))
        self.assertEqual((res["checked"], res["paused"]), (25, False))
        self.assertEqual({c["state"] for c in decay.checks(run["id"])}, {"insufficient", "ok"})
        # later: the crash is evaluated day by day with the prefix only
        res = decay.check_run(run, after_close(days[-1]))
        self.assertEqual(res["checked"], 5)
        self.assertTrue(res["paused"])
        cks = decay.checks(run["id"])
        self.assertEqual(len(cks), 30)
        exp = expected.basis(run)
        lr = [math.log(1 + r) for r in rets]
        for k, c in enumerate(cks, start=1):
            a = decay.assess(exp, lr[:k], run["calendar"])
            self.assertEqual(set(c["triggered"]), set(a["triggered"]), c["day"])
        first = next(c for c in cks if c["triggered"])
        self.assertEqual(first["triggered"], ["drawdown"])
        self.assertIn(run["id"], decay.paused_run_ids())
        later = [c for c in cks if c["day"] > first["day"]]
        self.assertTrue(later and all(c["state"] == "paused" for c in later))
        ev = decay.events(run_id=run["id"])
        self.assertEqual([e["action"] for e in ev], ["paused"])        # one pause, not one per session
        al = alerts.list_alerts()["alerts"]
        self.assertEqual([a["kind"] for a in al], ["decay"])
        self.assertTrue(al[0]["toast"])
        self.assertEqual(al[0]["strategy_id"], sid)
        self.assertEqual(db.list_audit(action="decay_paused")[0]["detail"]["rule"], "drawdown")
        # idempotent: running again adds nothing
        self.assertEqual(decay.check_run(run, after_close(days[-1]))["checked"], 0)
        view = decay.monitor_view(run, after_close(days[-1]))
        self.assertEqual(view["decay"]["state"], "paused")
        self.assertEqual(view["decay"]["paused"]["rule"], "drawdown")
        self.assertEqual(view["sessions"], 30)
        self.assertEqual(view["decay"]["basis"], "equity_12m")             # EXPECTED: 250 holdout sessions
        self.assertFalse(view["deviation"]["in_band"])
        json.dumps(view, allow_nan=False)

    def test_decisions(self):
        sid, run = self.start_run()
        days = write_returns(run, [0.0005] * 21 + [-0.07, -0.07])
        now = after_close(days[-1])
        self.set_now(now)
        with self.assertRaises(TradingError) as cm:
            decay.decide(sid, "keep_running", "x" * 5)
        self.assertEqual(cm.exception.code, "not_paused")
        decay.check_run(run, now)
        out = decay.decide(sid, "keep_running", "I accept the drawdown")
        self.assertEqual(out["event"]["action"], "keep_running")
        self.assertNotIn(run["id"], decay.paused_run_ids())
        self.assertEqual(db.list_audit(action="decay_decision")[0]["detail"]["action"], "keep_running")
        # after the decision only the returns after it count: a small loss does not re-pause ...
        days2 = write_returns(run, [0.0005] * 21 + [-0.07, -0.07, -0.005])
        r2 = decay.check_run(run, after_close(days2[-1]))
        self.assertFalse(r2["paused"])
        # ... a fresh drawdown beyond the limit (2.5 × 5 % for a 250-session holdout) does
        days3 = write_returns(run, [0.0005] * 21 + [-0.07, -0.07, -0.005, -0.07, -0.07])
        self.assertTrue(decay.check_run(run, after_close(days3[-1]))["paused"])
        self.set_now(after_close(days3[-1]))
        out = decay.decide(sid, "demote", "", confirm=None)      # fix1023: no typed "DEMOTE", the note is optional
        self.assertEqual(out["event"]["action"], "demoted")
        self.assertEqual(db.strategy_get(sid)["state"], "validated")
        self.assertEqual(incubation.end_event(run["id"])["event"], "aborted")
        det = decay.detail(sid)
        self.assertEqual([e["action"] for e in det["events"]], ["paused", "keep_running", "paused", "demoted"])
        self.assertTrue(det["series"] and det["series"][-1]["band_lo"] is not None)

    def test_overview_and_run_checks(self):
        sid, run = self.start_run()
        days = write_returns(run, [0.0005] * 22)
        self.set_now(after_close(days[-1]))
        out = decay.run_checks()
        self.assertEqual((out["checked"], out["paused"]), (22, []))
        ov = service.overview()
        self.assertEqual(ov["accounts"][0]["account_id"], "paper-main")
        self.assertEqual(ov["strategies"][0]["strategy_id"], sid)
        self.assertEqual(ov["strategies"][0]["decay"]["state"], "ok")
        self.assertEqual(ov["strategies"][0]["expected"]["mean_daily"], EXPECTED["mean_daily"])
        self.assertEqual(set(ov), {"now", "paper_only", "notice", "monitor", "alerts", "accounts", "strategies"})
        json.dumps(ov, allow_nan=False)

    def test_not_found(self):
        with self.assertRaises(TradingError) as cm:
            decay.detail("s-000000000000")
        self.assertEqual(cm.exception.status, 404)


_ = date
if __name__ == "__main__":
    unittest.main()
