"""Strategy Platform — the rebuilt HedgeFund module (plan: project doc
claude/strategy-platform-plan.md; contract: strategy_platform/SPEC.md).

Named `strategy_platform`, not `platform`: the latter would shadow Python's
stdlib module `platform`, which aiohttp and other libraries import.

Part of UFOS.ai, a proof-of-concept portfolio project. Paper trading by
default; not investment advice.
"""
