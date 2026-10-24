"""Market-data hub (SPEC §7.6) + the app's WebSocket ``/research/ws`` (SPEC §4.3).

- ``hub.py``: ``MarketDataHub`` — the only owner of the Alpaca stream connections;
- ``alpaca_stream.py``: one connection per endpoint (state machine, backoff, probe);
- ``protocol.py``: Alpaca stream messages; ``transport.py``: the websockets client;
- ``candles.py``: live candles from trades; ``demand.py``: topics, subscribers, priority;
- ``ws.py``: ``/research/ws`` sessions (hub subscribers).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from .alpaca_stream import HubConfig
from .hub import MarketDataHub

__all__ = ["HubConfig", "MarketDataHub"]
