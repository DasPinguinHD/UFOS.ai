"""Volatility / allocation family of the alpha library (SPEC §7.9)."""
from ._build import ETF_PRESET, entry, f, op, p, param, spec

ENTRIES = [
    entry("low_volatility", "volatility", "Ang, Hodrick, Xing, Zhang — The Cross-Section of Volatility and "
          "Expected Returns (2006)", 21,
          "Hold the 15 ETFs with the lowest 1-year total volatility of daily returns. Ang et al. sorted single "
          "stocks on 1-month idiosyncratic volatility; this is the simpler total-volatility (low-risk) variant "
          "(cf. Baker, Bradley, Wurgler 2011). Across ETFs it tilts towards bonds.",
          spec(name="Low volatility", category="volatility",
               description="Hold the 15 least volatile ETFs (252-day volatility of daily returns).",
               tags=["volatility", "low-risk"], universe=ETF_PRESET,
               parameters={"window": param(252, 63, 504, step=21, description="Volatility window in bars.")},
               signal=op("cs_rank", op("neg", op("ts_std", op("returns", 1), p("window")))),
               portfolio={"selection": {"type": "top_n", "n": 15}, "max_weight": 0.1}, frequency="monthly")),
    entry("inverse_vol_allocation", "allocation", "Maillard, Roncalli, Teiletche — Equally-Weighted Risk "
          "Contributions (2010); naive risk parity", 21,
          "Every asset held, weight proportional to 1/volatility (naive risk parity — correlations ignored).",
          spec(name="Inverse-volatility allocation", category="allocation",
               description="Hold a multi-asset ETF basket with weights proportional to inverse volatility.",
               tags=["allocation", "risk-parity"],
               universe={"symbols": ["SPY", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]},
               signal=op("sign", f("close")),
               portfolio={"selection": {"type": "all"}, "weighting": "inverse_vol", "vol_lookback": 60},
               frequency="monthly")),
]
