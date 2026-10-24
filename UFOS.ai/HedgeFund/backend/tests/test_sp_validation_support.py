"""Fakes for the stage-4 validation tests (no tests here): a synthetic
market with an optional planted, persistent cross-sectional signal, a FAST
vectorised stand-in for ``engine.run`` (same call signature; result dict
with the fields the validation layer reads: dates, equity, benchmark,
fills, range, request, benchmark_info, analysis), a metrics function built
from ``backtest.metrics`` pieces, and a stress function. Offline,
deterministic. Part of UFOS.ai (proof of concept).

Signals (chosen by ``kind``):
- ``planted``: signal = a * z + (1 - a) * u0 — z is the latent AR(1) factor
  that drives the next day's cross-sectional returns, u0 independent noise;
  parameter ``a`` (0..1, step 0.1, base 0.5);
- ``noise``: signal = u_k — independent noise panel number k (parameter
  ``k``), unrelated to returns.
Portfolio: every ``REBALANCE`` days long the top 3 / short the bottom 3
symbols, equal weights, gross 1 (dollar neutral); a position is opened in
full and closed in full when the symbol leaves the group (no drift trades),
cost ``cost_bps`` per unit of traded notional.
"""
from datetime import date, datetime, time, timezone  # noqa: F401 (date re-exported for tests)

import numpy as np

from strategy_platform.core import calendars

SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ"]
FIRST = date(2020, 1, 2)
LAST = date(2026, 10, 8)
REBALANCE = 5
COST_BPS = 5.0
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)       # last complete NYSE session 2026-10-08


def spec_planted(a=0.5, name="planted"):
    return {"schema": "ufos.strategy/1", "name": name, "universe": {"symbols": list(SYMS)},
            "parameters": {"a": {"value": a, "min": 0.0, "max": 1.0, "step": 0.1}},
            "signal": {"op": "mul", "args": [{"param": "a"}, {"field": "close"}]},
            "portfolio": {"mode": "long_short", "selection": {"type": "top_bottom_n", "n": 3},
                          "weighting": "equal", "max_weight": 1.0, "gross_exposure": 1.0},
            "rebalance": {"frequency": "weekly"}}


def spec_noise(k=0, kmax=7, name="noise"):
    s = spec_planted(name=name)
    s["parameters"] = {"k": {"value": k, "min": 0, "max": kmax, "step": 1, "integer": True}}
    s["signal"] = {"op": "add", "args": [{"field": "close"}, {"param": "k"}]}
    return s


class Market:
    def __init__(self, seed=1, *, phi=0.98, beta=0.0015, vol=0.012, n_noise=64, first=FIRST, last=LAST):
        rng = np.random.default_rng(seed)
        self.dates = [d.isoformat() for d in calendars.trading_days(first, last)]
        T, N = len(self.dates), len(SYMS)
        z = np.zeros((T, N))
        z[0] = rng.normal(size=N)
        sd = np.sqrt(1 - phi * phi)
        for t in range(1, T):
            z[t] = phi * z[t - 1] + sd * rng.normal(size=N)
        self.z = z
        mkt = rng.normal(0.0003, 0.009, size=T)
        r = np.zeros((T, N))
        zc = z - z.mean(axis=1, keepdims=True)
        r[1:] = mkt[1:, None] + beta * zc[:-1] + rng.normal(0, vol, size=(T - 1, N))
        self.ret = r
        self.close = 100.0 * np.cumprod(1.0 + r, axis=0)
        self.bench = 400.0 * np.cumprod(1.0 + mkt)
        self.noise = rng.normal(size=(n_noise, T, N))
        self.calls = []

    def signal(self, kind, spec):
        p = spec["parameters"]
        if kind == "planted":
            a = float(p["a"]["value"])
            return a * self.z + (1.0 - a) * self.noise[0]
        return self.noise[int(p["k"]["value"])]

    def engine(self, kind):
        def run(spec, *, start=None, end=None, history_from=None, capital=100000.0, benchmark=None, progress=None,
                cancel=None, data=None, cost_overrides=None, clamp_warmup=False, holdout_access=False,
                record_symbol_pnl=True):            # record_symbol_pnl: engine flag (SPEC §11.5, L7)
            self.calls.append({"start": start, "end": end, "cost_overrides": cost_overrides,
                               "holdout_access": holdout_access, "params": {k: v["value"] for k, v in
                                                                            (spec.get("parameters") or {}).items()}})
            cost = COST_BPS * ((cost_overrides or {}).get("fees_bps", 10.0) / 10.0)
            return self.simulate(self.signal(kind, spec), start=start, end=end, capital=capital, cost_bps=cost)
        return run

    def simulate(self, S, *, start=None, end=None, capital=100000.0, cost_bps=COST_BPS):
        d = self.dates
        i0 = 0 if start is None else next(i for i, x in enumerate(d) if x >= str(start))
        i1 = len(d) - 1 if end is None else max(i for i, x in enumerate(d) if x <= str(end))
        N = len(SYMS)
        held = {}                       # symbol index -> (side, shares)
        eq = [float(capital)]
        fills = []
        w = np.zeros(N)
        value = float(capital)
        for t in range(i0, i1):
            if (t - i0) % REBALANCE == 0:
                order = np.argsort(S[t], kind="mergesort")
                longs, shorts = set(order[-3:].tolist()), set(order[:3].tolist())
                target = {i: 1 for i in longs}
                target.update({i: -1 for i in shorts})
                traded = 0.0
                ts = datetime.combine(date.fromisoformat(d[t + 1]), time(14, 36), tzinfo=timezone.utc).isoformat()
                for i in sorted(set(held) | set(target)):
                    old = held.get(i)
                    new = target.get(i)
                    if old is not None and (new is None or new != old[0]):
                        px = float(self.close[t, i])
                        fills.append({"symbol": SYMS[i], "side": "sell" if old[0] > 0 else "cover", "qty": old[1],
                                      "fill_price": px, "fees": 0.0, "ts": ts, "notional": old[1] * px,
                                      "origin": "autopilot", "participation_cut": False})
                        traded += old[1] * px
                        del held[i]
                    if new is not None and i not in held:
                        px = float(self.close[t, i])
                        qty = value / 6.0 / px
                        held[i] = (new, qty)
                        fills.append({"symbol": SYMS[i], "side": "buy" if new > 0 else "short", "qty": qty,
                                      "fill_price": px, "fees": 0.0, "ts": ts, "notional": qty * px,
                                      "origin": "autopilot", "participation_cut": False})
                        traded += qty * px
                w = np.zeros(N)
                for i, (side, _) in held.items():
                    w[i] = side / 6.0
                value -= traded * cost_bps / 1e4
            value *= 1.0 + float(w @ self.ret[t + 1])
            eq.append(value)
        dates = d[i0:i1 + 1]
        bench = (self.bench[i0:i1 + 1] / self.bench[i0] * capital).tolist()
        analysis = {"dates": list(d), "symbols": list(SYMS), "signal": S, "close_all": self.close, "i0": i0,
                    "i1": i1, "calendar": "nyse", "dollar_volume": {}, "crypto": []}
        peak = np.maximum.accumulate(np.asarray(eq))
        half = [0.0] + [v / 2.0 for v in eq[1:]]
        return {"engine_version": "fake-validation-engine", "dates": dates, "equity": eq, "benchmark": bench,
                "long_mv": half, "short_mv": half, "drawdown_pct": (1.0 - np.asarray(eq) / peak).tolist(),
                "orders": [], "events": [], "stats": {}, "data": {}, "costs": {"model": "fake"},
                "broker_params": {}, "fills": fills,
                "range": {"start": dates[0], "end": dates[-1], "calendar": "nyse"},
                "request": {"capital": capital}, "benchmark_info": {"symbol": "SPY"}, "analysis": analysis,
                "warnings": []}


def fake_metrics(result):
    from strategy_platform.backtest import metrics
    miss = metrics._Missing()
    perf = metrics.performance(result, miss)
    perf.update(metrics.trades(result, miss))
    return {"calendar": "nyse", "performance": perf, "benchmark": metrics.benchmark_stats(result, miss),
            "signal_quality": metrics.signal_quality(result, result["analysis"]),
            "capacity": {"capital": 100000.0, "capacity_usd": 5e7, "at_most": None, "orders": 100,
                         "orders_at_limit": 0, "q90_ratio": 0.002, "reason": None},
            "unavailable": miss.reasons}


def fake_stress(market, kind, episode=None):
    """run_stress stand-in: costs x2 + slippage x3 (fake engine with higher costs) and one 'episode' =
    ``episode`` (start, end) dates of the synthetic data (or none)."""
    from strategy_platform.backtest import metrics

    def summary(res):
        miss = metrics._Missing()
        perf = metrics.performance(res, miss)
        b = metrics.performance({"range": res["range"], "dates": res["dates"], "equity": res["benchmark"]},
                                metrics._Missing())
        return {"strategy": {"sharpe": perf.get("sharpe"), "max_drawdown": perf.get("max_drawdown")},
                "benchmark": {"symbol": "SPY", "max_drawdown": b.get("max_drawdown")}, "unavailable": miss.reasons}

    def run_stress(spec, base, *, data=None, progress=None, cancel=None, seed=None, run_engine=None, **kw):
        eng = market.engine(kind)
        rng = base["range"]
        out = []
        if episode:
            res = eng(spec, start=episode[0], end=episode[1])
            out.append({"key": "synthetic_episode", "kind": "episode", "label": "Synthetic episode", "status": "done",
                        "reason": None, "window": {"start": episode[0], "end": episode[1]}, "metrics": summary(res)})
        out.append({"key": "locked", "kind": "episode", "label": "Locked episode", "status": "not_available",
                    "reason": "not available: inside the locked holdout window", "metrics": None})
        res = eng(spec, start=rng["start"], end=rng["end"], cost_overrides={"fees_bps": 20.0})
        out.append({"key": "costs_x2", "kind": "synthetic", "label": "Costs x2", "status": "done", "reason": None,
                    "metrics": summary(res)})
        return {"seed": seed, "scenarios": out}

    def synthetic_costs(key, label, spec, base, fm, sm, *, data=None, cancel=None, run_engine=None, **kw):
        rng = base["range"]
        res = market.engine(kind)(spec, start=rng["start"], end=rng["end"], cost_overrides={"fees_bps": 10.0 * fm})
        return {"key": key, "kind": "synthetic", "label": label, "status": "done", "reason": None,
                "metrics": summary(res)}
    return run_stress, synthetic_costs


def best_noise_k(market, kmax, end):
    """The k whose noise signal has the best training Sharpe (the 'overfitted' pick)."""
    from strategy_platform.validation import folds, stats
    best = None
    for k in range(kmax + 1):
        res = market.simulate(market.noise[k], end=end)
        s = stats.sharpe([r for _, r in folds.daily_returns(res["equity"])], 252)
        if best is None or s > best[0]:
            best = (s, k)
    return best[1]
