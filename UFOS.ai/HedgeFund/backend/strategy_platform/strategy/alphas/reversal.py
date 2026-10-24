"""Reversal / mean-reversion family of the alpha library (SPEC §7.9)."""
from ._build import ETF_PRESET, entry, f, op, param, p, r, spec

ENTRIES = [
    entry("st_reversal_5d", "reversal", "Lehmann — Fads, Martingales, and Market Efficiency (1990); "
          "Jegadeesh (1990)", 5,
          "Weekly losers bought, winners sold. Designed for single stocks (liquidity provision); on ETFs the "
          "effect is weaker and costs matter.",
          spec(name="Short-term reversal 5d", category="reversal",
               description="Long the 5 ETFs with the worst 5-day return, short the 5 with the best.",
               tags=["reversal", "short-term"], universe=ETF_PRESET,
               parameters={"lookback": param(5, 2, 21, step=1, description="Return lookback in bars.")},
               signal=op("cs_rank", op("neg", op("pct_change", f("close"), p("lookback")))),
               portfolio={"mode": "long_short", "selection": {"type": "top_bottom_n", "n": 5}, "max_weight": 0.1},
               frequency="weekly")),
    entry("rsi2_reversion", "reversal", "Connors, Alvarez — Short Term Trading Strategies That Work (2008)", 3,
          "Buy short-term oversold (RSI(2) < 10) assets in a long-term uptrend (close > SMA200). Stateless "
          "form: held while the condition holds (the book exits on a close above the 5-day SMA).",
          spec(name="RSI(2) mean reversion", category="reversal",
               description="Hold ETFs whose 2-day RSI is below 10 while they trade above their 200-day average.",
               tags=["reversal", "rsi", "connors"], universe=ETF_PRESET,
               features={"oversold": op("lt", op("rsi", 2), 10),
                         "uptrend": op("gt", f("close"), op("sma", f("close"), 200))},
               signal=op("and", r("oversold"), r("uptrend")),
               portfolio={"selection": {"type": "threshold", "long_above": 0.5}, "max_weight": 0.2},
               frequency="daily")),
    entry("bollinger_reversion", "reversal", "Bollinger — Bollinger on Bollinger Bands (2001)", 5,
          "Fade 2-sigma deviations from the 20-day mean (z-score of the close).",
          spec(name="Bollinger z-score reversion", category="reversal",
               description="Long when the close is more than 2 standard deviations below its 20-day mean, short "
                           "when more than 2 above.",
               tags=["reversal", "bollinger"], universe=ETF_PRESET,
               parameters={"window": param(20, 10, 60, step=5, description="Band window in bars.")},
               signal=op("neg", op("ts_zscore", f("close"), p("window"))),
               portfolio={"mode": "long_short", "selection": {"type": "threshold", "long_above": 2,
                                                              "short_below": -2}, "max_weight": 0.1},
               frequency="daily")),
]
