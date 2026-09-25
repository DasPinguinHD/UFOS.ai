"""Data collectors for the Global Monitoring System.

Each module here is one independent poller for one of this module's data
sources (see docs/global-monitoring-system-plan.md, Section 3):
news_collector (world-news RSS; replaced gdelt_collector 2026-09-24),
gdacs_collector, opensky_collector, yahoo_collector. (sec_edgar_collector
and fred_collector moved to the Hedge Fund module's Financial Newsroom on
2026-09-24 — HedgeFund/backend/newsroom/.)

Every collector exposes the same shape:

    async def run(emit, report_status) -> None:
        ...

`emit(event)` is called for every new/updated event (see events.make_event
for the schema) and `report_status(ok: bool, message: str)` is called after
every poll cycle, success or failure — this drives the "Ampel pro Feed"
traffic-light list in the frontend's left sidebar. `run()` is expected to
loop forever (poll, sleep, repeat); main.py starts each one as its own
asyncio task, so one collector crashing or being disabled (missing API key)
never takes another one down.

See backend/README.md and backend/collectors/README.md for the full picture.
"""
