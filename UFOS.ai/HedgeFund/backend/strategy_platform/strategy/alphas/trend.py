"""Trend family of the alpha library (SPEC §7.9)."""
from ._build import ETF_PRESET, entry, f, op, p, param, r, spec

ENTRIES = [
    entry("sma_50_200", "trend", "Golden cross (50/200-day moving averages, trading practice); "
          "cf. Faber (2007) for a 10-month SMA rule", 40,
          "Long every ETF whose 50-day average is above its 200-day average, equal weights. Faber (2007) used "
          "price vs a 10-month average instead; the 50/200 cross is a practitioner rule, not from that paper.",
          spec(name="SMA 50/200 trend", category="trend",
               description="Hold ETFs in an uptrend (50-day average above the 200-day average).",
               tags=["trend", "moving-average"], universe=ETF_PRESET,
               parameters={"fast": param(50, 10, 100, step=10, description="Fast average in bars."),
                           "slow": param(200, 100, 300, step=25, description="Slow average in bars.")},
               signal=op("gt", op("sma", f("close"), p("fast")), op("sma", f("close"), p("slow"))),
               portfolio={"selection": {"type": "threshold", "long_above": 0.5}, "max_weight": 0.1},
               frequency="weekly")),
    entry("donchian_55_20", "trend", "Faith — Way of the Turtle (2007); Donchian channels", 20,
          "Turtle system 2 in stateless form: in a trade while the latest 55-day high is more recent than the "
          "latest 20-day low (entry on the breakout, exit on the 20-day breakdown). Inverse-volatility sizing "
          "stands in for the turtles' ATR units.",
          spec(name="Donchian breakout 55/20", category="trend",
               description="Long after a 55-day high until a 20-day low; sized by inverse volatility.",
               tags=["trend", "breakout", "turtle"], universe=ETF_PRESET,
               parameters={"entry": param(55, 20, 100, step=5, description="Breakout channel in bars."),
                           "exit": param(20, 5, 55, step=5, description="Exit channel in bars.")},
               features={"since_high": op("ts_argmax", f("high"), p("entry")),
                         "since_low": op("ts_argmin", f("low"), p("exit"))},
               signal=op("lt", r("since_high"), r("since_low")),
               portfolio={"selection": {"type": "threshold", "long_above": 0.5}, "weighting": "inverse_vol",
                          "vol_lookback": 20, "max_weight": 0.1},
               frequency="daily")),
    entry("risk_on_off", "trend", "Trend-based risk-on/off switch (practitioner rule, in the spirit of Faber "
          "2007 and Antonacci 2014)", 10,
          "Hold SPY or TLT, whichever trades further above its 200-day average (risk-on vs risk-off).",
          spec(name="Risk-on/off switch (SPY vs TLT)", category="trend",
               description="Hold equities or long bonds depending on which is in the stronger trend.",
               tags=["trend", "switch", "risk-on-off"], universe={"symbols": ["SPY", "TLT"]},
               parameters={"window": param(200, 100, 300, step=25, description="Trend average in bars.")},
               signal=op("sub", op("div", f("close"), op("sma", f("close"), p("window"))), 1),
               portfolio={"selection": {"type": "top_n", "n": 1}}, frequency="weekly")),
    entry("crypto_trend_vol_target", "trend", "Hurst, Ooi, Pedersen — A Century of Evidence on Trend-Following "
          "(2017); applied to crypto", 10,
          "BTC/ETH/SOL held above their 50-day average, inverse-volatility weights; the 40 % annual volatility "
          "target is applied by the backtest/runner (stage 3).",
          spec(name="Crypto trend with volatility target", category="trend",
               description="Hold the major coins in an uptrend, sized by inverse volatility, targeting 40 % "
                           "annual volatility.",
               tags=["trend", "crypto", "vol-target"], universe={"symbols": ["BTC-USD", "ETH-USD", "SOL-USD"]},
               parameters={"window": param(50, 20, 200, step=10, description="Trend average in days.")},
               signal=op("gt", f("close"), op("sma", f("close"), p("window"))),
               portfolio={"selection": {"type": "threshold", "long_above": 0.5}, "weighting": "inverse_vol",
                          "vol_lookback": 30},
               risk={"vol_target_annual": 0.4}, frequency="daily")),
    entry("vol_targeted_trend", "trend", "Moreira, Muir — Volatility-Managed Portfolios (2017); "
          "Moskowitz et al. (2012)", 10,
          "Multi-asset 12-month trend, long only, inverse-volatility weights; 10 % annual volatility target "
          "applied by the backtest/runner (stage 3).",
          spec(name="Volatility-targeted trend", category="trend",
               description="Hold asset-class ETFs with a positive 12-month return, sized by inverse volatility, "
                           "targeting 10 % annual volatility.",
               tags=["trend", "multi-asset", "vol-target"],
               universe={"symbols": ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]},
               parameters={"lookback": param(252, 126, 378, step=21, description="Trend lookback in bars.")},
               features={"ret": op("pct_change", f("close"), p("lookback"))},
               signal=r("ret"),
               portfolio={"selection": {"type": "threshold", "long_above": 0}, "weighting": "inverse_vol",
                          "vol_lookback": 60, "max_weight": 0.25},
               risk={"vol_target_annual": 0.1}, frequency="weekly")),
]
