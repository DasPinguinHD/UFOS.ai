"""Alpha101 subset (Kakushadze — 101 Formulaic Alphas, 2016), SPEC §7.9.

Only alphas that need OHLCV/vwap and no industry neutralisation. Notation:
rank → cs_rank, correlation → ts_corr, covariance → ts_cov, Ts_Rank →
ts_rank (0..1 here instead of 1..n — monotone, same ordering), returns →
returns(1). All were designed for single-stock cross-sections (thousands of
names, intraday-ish holding); on ~60 ETFs they are illustrative.
"""
from ._build import ETF_PRESET, entry, f, op, r, spec

REF = "Kakushadze — 101 Formulaic Alphas (2016)"
NOTE = ("Designed for single-stock cross-sections; on a ~60-ETF universe it is illustrative only. "
        "Long the 10 highest, short the 10 lowest values, daily.")
C, O, H, L, V, VW = f("close"), f("open"), f("high"), f("low"), f("volume"), f("vwap")
D1 = op("delta", C, 1)


def _a(num, formula, signal, features=None, holding=2):
    return entry(f"alpha101_{num:03d}", "alpha101", REF, holding, f"{NOTE} Formula: {formula}",
                 spec(name=f"Alpha101 #{num}", category="alpha101",
                      description=f"Alpha #{num} of Kakushadze (2016): {formula}",
                      tags=["alpha101", "price-volume"], universe=ETF_PRESET, features=features,
                      signal=signal,
                      portfolio={"mode": "long_short", "selection": {"type": "top_bottom_n", "n": 10},
                                 "max_weight": 0.1},
                      frequency="daily"))


ENTRIES = [
    _a(2, "-1 * correlation(rank(delta(log(volume), 2)), rank((close - open) / open), 6)",
       op("neg", op("ts_corr", op("cs_rank", op("delta", op("log", V), 2)),
                    op("cs_rank", op("div", op("sub", C, O), O)), 6))),
    _a(3, "-1 * correlation(rank(open), rank(volume), 10)",
       op("neg", op("ts_corr", op("cs_rank", O), op("cs_rank", V), 10))),
    _a(4, "-1 * Ts_Rank(rank(low), 9)", op("neg", op("ts_rank", op("cs_rank", L), 9))),
    _a(6, "-1 * correlation(open, volume, 10)", op("neg", op("ts_corr", O, V, 10))),
    _a(9, "(0 < ts_min(delta(close, 1), 5)) ? delta(close, 1) : ((ts_max(delta(close, 1), 5) < 0) ? "
          "delta(close, 1) : (-1 * delta(close, 1)))",
       op("where", op("lt", 0, op("ts_min", r("d1"), 5)), r("d1"),
          op("where", op("lt", op("ts_max", r("d1"), 5), 0), r("d1"), op("neg", r("d1")))),
       features={"d1": D1}),
    _a(12, "sign(delta(volume, 1)) * (-1 * delta(close, 1))",
       op("mul", op("sign", op("delta", V, 1)), op("neg", D1)), holding=1),
    _a(13, "-1 * rank(covariance(rank(close), rank(volume), 5))",
       op("neg", op("cs_rank", op("ts_cov", op("cs_rank", C), op("cs_rank", V), 5)))),
    _a(14, "(-1 * rank(delta(returns, 3))) * correlation(open, volume, 10)",
       op("mul", op("neg", op("cs_rank", op("delta", op("returns", 1), 3))), op("ts_corr", O, V, 10))),
    _a(16, "-1 * rank(covariance(rank(high), rank(volume), 5))",
       op("neg", op("cs_rank", op("ts_cov", op("cs_rank", H), op("cs_rank", V), 5)))),
    _a(33, "rank(-1 * ((1 - (open / close))^1))",
       op("cs_rank", op("neg", op("sub", 1, op("div", O, C))))),
    _a(41, "((high * low)^0.5) - vwap", op("sub", op("pow", op("mul", H, L), 0.5), VW)),
    _a(53, "-1 * delta(((close - low) - (high - close)) / (close - low), 9)",
       op("neg", op("delta", op("div", op("sub", op("sub", C, L), op("sub", H, C)), op("sub", C, L)), 9)), holding=5),
    _a(54, "(-1 * ((low - close) * (open^5))) / ((low - high) * (close^5))",
       op("div", op("neg", op("mul", op("sub", L, C), op("pow", O, 5))),
          op("mul", op("sub", L, H), op("pow", C, 5)))),
    _a(101, "(close - open) / ((high - low) + .001)",
       op("div", op("sub", C, O), op("add", op("sub", H, L), 0.001)), holding=1),
]
