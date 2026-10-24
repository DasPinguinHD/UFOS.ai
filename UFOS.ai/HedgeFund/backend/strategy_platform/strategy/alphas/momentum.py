"""Momentum family of the alpha library (SPEC §7.9)."""
from ._build import ETF_PRESET, entry, f, op, p, param, r, spec

SECTORS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]

_MOM_PARAMS = {
    "formation": param(231, 105, 357, step=21, description="Formation period in bars (12 months minus the skip)."),
    "skip": param(21, 1, 42, step=1, description="Most recent bars skipped (short-term reversal)."),
}
_MOM_12_1 = op("pct_change", op("lag", f("close"), p("skip")), p("formation"))

ENTRIES = [
    entry("tsmom_12m", "momentum", "Moskowitz, Ooi, Pedersen — Time Series Momentum (2012)", 21,
          "Each ETF is held long when its own past 12-month return is positive and short when negative "
          "(the paper's plain 12-month lookback, no skipped month); inverse-volatility weights. The paper used "
          "futures; ETFs are a proxy.",
          spec(name="Time-series momentum 12m", category="momentum",
               description="Long assets whose 12-month return is positive, short those whose return is "
                           "negative, sized by inverse volatility.",
               tags=["momentum", "time-series", "trend"], universe=ETF_PRESET,
               parameters={"lookback": param(252, 126, 378, step=21, description="Lookback in bars (12 months).")},
               features={"mom": op("pct_change", f("close"), p("lookback"))}, signal=r("mom"),
               portfolio={"mode": "long_short", "selection": {"type": "threshold", "long_above": 0, "short_below": 0},
                          "weighting": "inverse_vol", "vol_lookback": 60, "max_weight": 0.1},
               frequency="monthly")),
    entry("xsmom_12_1", "momentum", "Jegadeesh, Titman — Returns to Buying Winners and Selling Losers (1993); "
          "12-1 skip-month convention per Carhart (1997)", 21,
          "Cross-sectional ranking of 12-month returns skipping the most recent month (the 12-1 convention of "
          "Carhart 1997; Jegadeesh/Titman skipped one week); long the top 10 ETFs. Designed for single stocks — "
          "on ETFs it mixes asset-class and country momentum.",
          spec(name="Cross-sectional momentum 12-1", category="momentum",
               description="Rank ETFs by their 12-month return skipping the last month; hold the 10 strongest.",
               tags=["momentum", "cross-sectional"], universe=ETF_PRESET,
               parameters=dict(_MOM_PARAMS), features={"mom": _MOM_12_1}, signal=op("cs_rank", r("mom")),
               portfolio={"selection": {"type": "top_n", "n": 10}, "max_weight": 0.2}, frequency="monthly")),
    entry("dual_momentum", "momentum", "Antonacci — Dual Momentum Investing (2014)", 21,
          "Relative momentum (best 12-month return) combined with absolute momentum (return > 0, else cash). "
          "Simplified: the absolute filter compares with 0 instead of T-bills, and bonds compete in the same "
          "ranking instead of being the fixed defensive asset.",
          spec(name="Dual momentum (SPY / EFA / AGG)", category="momentum",
               description="Hold the asset with the best 12-month return if that return is positive; otherwise "
                           "stay in cash.",
               tags=["momentum", "absolute", "relative"], universe={"symbols": ["SPY", "EFA", "AGG"]},
               parameters={"lookback": param(252, 126, 378, step=21, description="Momentum lookback in bars.")},
               features={"ret": op("pct_change", f("close"), p("lookback"))},
               signal=op("where", op("gt", r("ret"), 0), r("ret"), op("nan")),
               portfolio={"selection": {"type": "top_n", "n": 1}}, frequency="monthly")),
    entry("high_52w", "momentum", "George, Hwang — The 52-Week High and Momentum Investing (2004)", 21,
          "Closeness of the close to its 52-week high, ranked across ETFs; long the 10 nearest to their highs.",
          spec(name="52-week-high proximity", category="momentum",
               description="Hold the 10 ETFs trading closest to their 52-week high.",
               tags=["momentum", "anchoring"], universe=ETF_PRESET,
               features={"proximity": op("div", f("close"), op("ts_max", f("high"), 252))},
               signal=op("cs_rank", r("proximity")),
               portfolio={"selection": {"type": "top_n", "n": 10}, "max_weight": 0.2}, frequency="monthly")),
    entry("sector_rotation", "momentum", "Moskowitz, Grinblatt — Do Industries Explain Momentum? (1999)", 21,
          "Six-month momentum across the 11 SPDR sector ETFs; hold the top 3.",
          spec(name="Sector-rotation momentum", category="momentum",
               description="Each month, hold the three US sector ETFs with the strongest 6-month return.",
               tags=["momentum", "sectors", "rotation"], universe={"symbols": list(SECTORS)},
               parameters={"lookback": param(126, 63, 252, step=21, description="Momentum lookback in bars.")},
               signal=op("cs_rank", op("pct_change", f("close"), p("lookback"))),
               portfolio={"selection": {"type": "top_n", "n": 3}}, frequency="monthly")),
]
