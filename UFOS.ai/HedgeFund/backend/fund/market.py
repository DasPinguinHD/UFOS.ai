"""TEMPORARY shim (stage 10 S10-C step M4) — the module moved to ``strategy_platform.data.market_feed`` (SP §22.7).
This old path is the SAME module object (``sys.modules`` alias: no second copy of state, monkeypatching keeps
working); removed with fund/ (R4). New code imports ``strategy_platform.data.market_feed`` — the guard test enforces it."""
import sys

from strategy_platform.data import market_feed as _m

sys.modules[__name__] = _m
