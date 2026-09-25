"""Financial Newsroom — the HedgeFund module's live-data section.

Moved here from the Global Monitoring module on 2026-09-24 (insider trades
and macro indicators belong to the trading side, not the world map). Each
newsroom section is one collector feeding main.py's HTTP API plus one WPF
view in HedgeFund/Newsroom/:

  section            collector                 endpoint(s)                       WPF view
  Insider Trades     sec_edgar_collector.py    GET /newsroom/insider,            InsiderTradesView
                                               POST /newsroom/insider-summary (AI)
  Macro Indicators   fred_collector.py         GET /newsroom/macro               MacroIndicatorsView
  Economic Calendar  calendar_collector.py     GET /newsroom/calendar            EconomicCalendarView
  Analysis &         overview.py (no collector; GET /newsroom/overview,     (WPF side)
  Reasoning          reads the data above)     POST /newsroom/analysis (AI)

Adding a section later = a collector module here, an entry in
main.py's NEWSROOM_SOURCES + a GET endpoint, and a UserControl view in
HedgeFund/Newsroom/ registered with one line in NewsroomSections.cs (the
newsroom window's navigation is built from that list; the Hedge Fund
window's "Financial Newsroom" button opens its first entry).
"""
