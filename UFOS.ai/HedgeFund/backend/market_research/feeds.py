"""Feed status for GET /research/feeds/status (SPEC §1.2).

MR1: the market-data hub does not exist yet, so every feed reports ``off``
with the reason. MR2 replaces ``feeds_status()`` / ``demand()`` /
``budget()`` with the hub's live values (same shapes).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

from typing import List

from . import limits

# (id, label, feed_label, symbol_limit) — fixed order (SPEC §1.2)
FEEDS = (
    ("stocks", "Stocks", "IEX · real time", limits.STOCK_STREAM_SYMBOL_LIMIT),
    ("crypto", "Crypto", "Alpaca crypto · real time", None),
    ("news", "News", "Alpaca · Benzinga", None),
)
FEED_STATES = ("off", "connecting", "live", "waiting", "unavailable")
NOT_BUILT_MESSAGE = "Live data is not built yet (stage MR2)."
DEMAND_NOTE = "Live streams run only while a Research view is visible or an alert is active."


def feeds_status() -> List[dict]:
    return [{"id": fid, "label": label, "feed_label": feed_label, "state": "off", "message": NOT_BUILT_MESSAGE,
             "attempt": 0, "connected_since": None, "last_message_at": None, "symbols": 0,
             "symbol_limit": limit}
            for fid, label, feed_label, limit in FEEDS]


def demand() -> dict:
    return {"views": 0, "alerts": 0, "streams_wanted": False, "note": DEMAND_NOTE}


def budget() -> dict:
    # MR2: measured by the shared Alpaca gate's research priority (http_gate.research_stats, SPEC §7.5)
    from strategy_platform.data import http_gate
    st = http_gate.research_stats("alpaca")
    return {"source": "alpaca", "shared_per_minute": limits.SHARED_ALPACA_PER_MINUTE,
            "research_max_per_minute": limits.RESEARCH_MAX_PER_MINUTE,
            "research_used_last_minute": st["used_last_minute"], "waiting_seconds": st["waiting_seconds"]}


class HubFeeds:
    """MR2: feeds/demand from the market-data hub (hub/hub.py), budget from this module (same shapes).
    ``ResearchService`` uses it; the module-level functions above stay the hub-less MR1 view."""

    def __init__(self, hub):
        self.hub = hub

    def feeds_status(self) -> List[dict]:
        return self.hub.feeds_status()

    def demand(self) -> dict:
        return self.hub.demand()

    def budget(self) -> dict:
        return budget()
