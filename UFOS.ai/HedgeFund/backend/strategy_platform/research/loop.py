"""Hook module for the central AI budget window (UFOS.ai/docs/ai-budgets-spec.md §4.4): ``ai_budgets.PROVIDER_MODULES``
names ``strategy_platform.research.loop``; the discovery loop itself lives in ``strategy_platform/discovery/`` (SPEC
§16) — this module only re-exports its budget hook (no logic here)."""
from ..discovery.budget import ai_budget_entries, ai_budget_put  # noqa: F401
