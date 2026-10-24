"""Broker adapter interface (SPEC §17.7; plan L8): ``account``, ``positions``,
``submit``, ``cancel``, ``order``, ``fills`` (a poll-based fills stream),
``capabilities``. Stage 7 has ONE implementation, the internal paper adapter
(``brokers/paper.py``); stage 8 adds Alpaca paper, stage 8b the Binance Spot demo (ccxt); IBKR /
MT5 are not built but must fit this interface (TODO at the end of the plan).

Order (dict) given to ``submit``: ``{client_order_id, symbol, side (buy|sell|short|
cover), qty | notional, origin (runtime|manual|kill|drawdown|margin), rationale,
allow_reduce: bool, prices?: {symbol: Quote}}``.

Ack (dict) returned by ``submit`` / ``order``: ``{client_order_id, broker_order_id,
status: accepted|filled|rejected|cancelled, qty, filled_qty, avg_price, fees,
notional, reject_reason, ts}``. ``submit`` with a client order id the broker already
knows returns that order's ack (idempotent — nothing is sent twice). A transient
failure raises ``execution.errors.BrokerUnavailable``.

Stage 8 (SPEC §18.3) — real brokers (paper/demo only): the order dict adds ``type``
(market|limit|stop|stop_limit|bracket), ``limit_price``, ``stop_price``, ``take_profit
{limit_price}``, ``stop_loss {stop_price, limit_price?}``, ``tif`` (day|gtc); the ack adds
statuses ``partially_filled`` / ``expired``, ``legs`` (bracket legs) and ``broker_status``;
``fees`` / ``filled_qty`` / ``avg_price`` are CUMULATIVE. Real adapters also implement
``snapshot()`` (account + positions + open orders in one read, for reconciliation),
``open_orders()``, ``cancel_all()``, ``instrument(symbol)`` and expose ``endpoint`` (always a
paper/demo URL — ``live_guard``). IBKR / MT5 would implement the same methods (TODO).

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER TRADING ONLY.
"""
from __future__ import annotations

from typing import List, Optional


class BrokerAdapter:
    name = "base"
    paper = True
    capabilities: dict = {}

    def account(self, prices: Optional[dict] = None) -> dict:          # pragma: no cover - interface
        raise NotImplementedError

    def positions(self, prices: Optional[dict] = None) -> List[dict]:  # pragma: no cover - interface
        raise NotImplementedError

    def submit(self, order: dict) -> dict:                              # pragma: no cover - interface
        raise NotImplementedError

    def cancel(self, client_order_id: str, reason: str = "cancelled") -> bool:   # pragma: no cover
        raise NotImplementedError

    def order(self, client_order_id: str) -> Optional[dict]:            # pragma: no cover - interface
        raise NotImplementedError

    def fills(self, after: int = 0, limit: int = 500) -> List[dict]:    # pragma: no cover - interface
        raise NotImplementedError

    # --- stage 8: real brokers (SPEC §18.3) -------------------------------------------------------------

    endpoint: Optional[str] = None

    def snapshot(self) -> dict:                                         # pragma: no cover - interface
        """{account: {...}, positions: [BrokerPosition], open_orders: [BrokerOrder], at}."""
        raise NotImplementedError

    def open_orders(self) -> List[dict]:                                # pragma: no cover - interface
        raise NotImplementedError

    def cancel_all(self) -> int:                                        # pragma: no cover - interface
        raise NotImplementedError

    def instrument(self, symbol: str) -> dict:                          # pragma: no cover - interface
        raise NotImplementedError
