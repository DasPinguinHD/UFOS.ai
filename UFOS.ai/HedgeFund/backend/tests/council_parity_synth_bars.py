"""SYNTHETIC daily bars for the Council parity harness (licence clean-up 2026-10-25).

The committed real Alpaca fixture (``council_parity_real_bars.json``) was redistribution of licensed market data and
was purged from the repository history. This module generates bars of the SAME SHAPE at test time — the same symbols
(SPY, QQQ, TLT, GLD, BTC-USD), 260 bars each up to 2026-10-23, rows ``[date, open, high, low, close, volume]``, the
equities on NYSE trading days, BTC 7 days a week — from a seeded random walk with realistic per-symbol drift and
volatility. No value comes from a real market feed; the same seed always gives the same bars (deterministic tests).

The real-data variant of the parity check reads the user's own local export instead
(``backend/data/local_fixtures/council_parity_real_bars.json``, git-ignored; ``tools/export_council_parity_bars.py``).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta
from typing import Dict, List

from strategy_platform.core import calendars

VERSION = 1
LAST_DAY = date(2026, 10, 23)
N_BARS = 260
TRADING_DAYS_PER_YEAR = {"equity": 252, "crypto": 365}
# symbol: (asset class, start level, annual drift, annual volatility, typical daily volume) — invented, plausible
PARAMS = {
    "SPY": ("equity", 400.0, 0.08, 0.16, 70e6),
    "QQQ": ("equity", 350.0, 0.10, 0.22, 40e6),
    "TLT": ("equity", 90.0, 0.00, 0.14, 25e6),
    "GLD": ("equity", 180.0, 0.06, 0.15, 8e6),
    "BTC-USD": ("crypto", 60000.0, 0.20, 0.55, 900.0),
}


def _dates(asset: str) -> List[date]:
    if asset == "crypto":
        return [LAST_DAY - timedelta(days=N_BARS - 1 - i) for i in range(N_BARS)]
    days = calendars.trading_days(LAST_DAY - timedelta(days=400), LAST_DAY)
    return days[-N_BARS:]


def bars(symbol: str, seed: int = VERSION) -> List[list]:
    asset, level, drift, vol, volume = PARAMS[symbol]
    rng = random.Random(f"council-parity-synthetic:{seed}:{symbol}")
    n = TRADING_DAYS_PER_YEAR[asset]
    mu, sigma = drift / n - 0.5 * (vol ** 2) / n, vol / math.sqrt(n)
    out, close = [], level
    for d in _dates(asset):
        open_ = close * math.exp(rng.gauss(0.0, sigma * 0.25))            # overnight gap
        close = open_ * math.exp(mu + rng.gauss(0.0, sigma * 0.95))
        span = abs(rng.gauss(0.0, sigma * 0.6))
        high = max(open_, close) * math.exp(span * rng.random())
        low = min(open_, close) * math.exp(-span * rng.random())
        vol_day = volume * math.exp(rng.gauss(0.0, 0.35))
        digits = 2
        out.append([d.isoformat(), round(open_, digits), round(high, digits), round(low, digits),
                    round(close, digits), round(vol_day, 0 if asset == "equity" else 4)])
    return out


def generate(seed: int = VERSION) -> Dict:
    """The fixture document (same layout as the user's local real-data export)."""
    return {
        "_about": ("SYNTHETIC council parity bars (seeded random walk, tests/council_parity_synth_bars.py v%d) — "
                   "not market data." % VERSION),
        "generator": f"tests/council_parity_synth_bars.py v{VERSION} seed {seed}",
        "symbols": {sym: {"source": ["synthetic"], "bars": bars(sym, seed)} for sym in PARAMS},
    }
