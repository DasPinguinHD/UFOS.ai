"""Market Research — the "Research" section of the HedgeFund module.

Contract: ``market_research/SPEC.md`` (binding). Research only — it never
places orders; paper trading only. Everything here runs under its own error
boundary (``supervisor.py``): a Research failure marks Research
"unavailable" with the reason and never stops the paper engine, kill switch,
dead-man switch, newsroom collectors or Strategy Platform jobs.

main.py:
    research = market_research.install(app)   # before the runner starts (routes)
    research.start()                          # after engine.start(): creates the supervised tasks only
    await research.shutdown()                 # in /shutdown, ≤ SHUTDOWN_TIMEOUT_SECONDS, never raises

Not inside ``strategy_platform/`` on purpose (its planned ``research/``
package is Strategy Studio — a different thing).

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import api, assets, drawings, feeds, limits, maintenance, watchlists
from .alerts import AlertEngine
from .logos import LogoService
from .news import NewsService
from .data import DataService
from .hub import MarketDataHub
from .supervisor import ResearchSupervisor, RestartPolicy

logger = logging.getLogger(__name__)

_service: Optional["ResearchService"] = None


class ResearchService:
    """The Research section's backend: supervisor + its tasks + feed status."""

    def __init__(self, *, store_path: Optional[str] = None, supervisor: Optional[ResearchSupervisor] = None,
                 feeds_module=feeds, data: Optional[DataService] = None, hub: Optional[MarketDataHub] = None):
        self.supervisor = supervisor or ResearchSupervisor()
        self.data = data or DataService()          # MR2 REST data layer (data.py)
        # MR2b: the market-data hub (only owner of the Alpaca streams) + its feed status
        self.hub = hub or MarketDataHub(data=lambda: self.data)
        if self.hub.status_fn is None:
            self.hub.status_fn = self.status_payload
        self.feeds = feeds.HubFeeds(self.hub) if feeds_module is feeds else feeds_module
        # MR4: watchlists in memory for the hub's watchlist:<id> topics (demand registration of shown lists)
        self.workspace = watchlists.Workspace(self.hub)
        if self.hub.watchlist_symbols is None:
            self.hub.watchlist_symbols = self.workspace.symbols_for
        self.prev_close_cache = None                # stats.quote.PrevCloseCache, created on first use
        # MR6: news (REST + the hub's news stream hook), thumbnail proxy + setting, logos
        self.news = NewsService(lambda: self.data)
        self.logos = LogoService(lambda: self.data)
        self.news.logos = self.logos
        if self.hub.news_hook is None:
            self.hub.news_hook = self.news.on_stream_item
        # MR7: alerts evaluated on the hub's live trades / news (hub.trade_hook), demand = enabled rules
        self.alerts = AlertEngine(self.hub, lambda: self.data)
        self.supervisor.add("store_maintenance", "Research database maintenance",
                            lambda: maintenance.run(store_path))
        self.supervisor.add("asset_refresh", "Research asset list refresh (daily)",
                            lambda: assets.run(lambda: self.data.sources))
        self.supervisor.add("market_data_hub", "Live market data (Alpaca streams)", self.hub.run)
        # MR5: split rule for stored drawings (corporate actions at start + daily, drawings.py)
        self.supervisor.add("drawing_split_check", "Drawing split check (daily)",
                            lambda: drawings.run(lambda: self.data.sources))
        self.supervisor.add("news_stream_store", "News stream storage", self.news.run)
        self.supervisor.add("alert_engine", "Price & news alerts", self.alerts.run)

    def start(self) -> None:
        """Creates the supervised tasks (sync, no I/O). Never raises."""
        self.supervisor.start()

    async def shutdown(self, timeout: float = limits.SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Cancels the Research tasks, waits at most ``timeout`` s. Never raises."""
        await self.supervisor.stop(timeout)

    def status_payload(self) -> dict:
        return api.status_payload(self.supervisor, self.feeds)


def install(app, *, store_path: Optional[str] = None) -> ResearchService:
    """Creates the process-wide service and adds the /research/* routes to ``app``."""
    global _service
    svc = ResearchService(store_path=store_path)
    api.register(app, svc)
    from .hub import ws as hub_ws
    hub_ws.register_ws(app, svc)                 # GET /research/ws (MR2b), same api_auth middleware
    _service = svc
    logger.info("Market Research installed (/research/*)")
    return svc


def service() -> Optional[ResearchService]:
    return _service


__all__ = ["ResearchService", "ResearchSupervisor", "RestartPolicy", "install", "service"]
