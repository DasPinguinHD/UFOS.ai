"""TEMPORARY shim (stage 10 S10-C step M3) — the module moved to ``strategy_platform.core.llm`` (SP §22.7).
This old path is the SAME module object (``sys.modules`` alias: no second copy of state, monkeypatching keeps
working); removed with fund/ (R4). New code imports ``strategy_platform.core.llm`` — the guard test enforces it."""
import sys

from strategy_platform.core import llm as _m

sys.modules[__name__] = _m
