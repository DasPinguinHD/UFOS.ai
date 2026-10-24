"""Topics, subscribers and demand of the market-data hub (SPEC §4.3, §7.6).

Topics (``/research/ws`` and in-process subscribers alike):
``trades:<SYM>``, ``candle:<SYM>:<tf>:<session>`` (equities ``rth``/``eth``,
crypto ``utc`` — the value the REST ``/bars`` route echoes for crypto),
``news:<SYM>``, ``alerts``, ``feeds`` and (MR4) ``watchlist:<id>`` (32-hex
id) = ``trades:<SYM>`` of every item of that watchlist in list order (the hub
expands it through ``MarketDataHub.watchlist_symbols``; one subscription).
Symbols are platform symbols (``core/instruments``), normalised upper-case.

A ``Subscriber`` is anything that receives hub messages: a ``/research/ws``
connection (kind ``view``; counts as demand while visible), alerts (kind
``alert``, MR7) and later the Strategy Platform live runtime. ``deliver(msg,
key)`` must not block; ``key`` = coalescing key (newest wins) or None.

Stream symbol priority (30-symbol stock limit, Q10): views before alerts;
within that, the order in which the topics were subscribed (the WPF sends
tabs/charts first, then watchlists, then alerts — §4.3.1), interleaved over
connections by position.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import itertools
import re
from collections import namedtuple
from typing import Dict, List, Optional

from strategy_platform.core.instruments import instrument

from .. import limits
from .candles import CRYPTO_SESSION, EQUITY_SESSIONS, TIMEFRAMES

Topic = namedtuple("Topic", "raw kind symbol asset_class tf session list_id", defaults=(None,))
KINDS = ("trades", "candle", "news", "alerts", "feeds", "watchlist")
LIST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SUBSCRIBER_KINDS = ("view", "alert")
_seq = itertools.count(1)


def parse_topic(text) -> Topic:
    """A topic string -> Topic (canonical ``raw``). ValueError with a short reason otherwise."""
    if not isinstance(text, str) or not text or len(text) > limits.WS_MAX_TOPIC_CHARS:
        raise ValueError("topic must be a string of 1–64 characters")
    if text in ("alerts", "feeds"):
        return Topic(text, text, None, None, None, None)
    parts = text.split(":")
    kind = parts[0]
    if kind == "watchlist":
        if len(parts) != 2 or not LIST_ID_RE.match(parts[1]):
            raise ValueError("watchlist topic needs a 32-character lowercase hex id")
        return Topic(text, "watchlist", None, None, None, None, parts[1])
    if kind not in ("trades", "candle", "news"):
        raise ValueError(f"unknown topic kind {kind[:20]!r}")
    if (kind == "candle" and len(parts) != 4) or (kind != "candle" and len(parts) != 2):
        raise ValueError(f"malformed {kind} topic")
    try:
        inst = instrument(parts[1])
    except ValueError:
        raise ValueError("invalid symbol") from None
    if parts[1] != parts[1].strip() or not parts[1]:
        raise ValueError("invalid symbol")
    sym = inst.symbol
    if kind != "candle":
        return Topic(f"{kind}:{sym}", kind, sym, inst.asset_class, None, None)
    tf, session = parts[2], parts[3]
    if tf not in TIMEFRAMES:
        raise ValueError("unknown timeframe")
    allowed = (CRYPTO_SESSION,) if inst.asset_class == "crypto" else EQUITY_SESSIONS
    if session not in allowed:
        raise ValueError(f"session must be {' or '.join(allowed)} for {sym}")
    return Topic(f"candle:{sym}:{tf}:{session}", kind, sym, inst.asset_class, tf, session)


class Subscriber:
    """Base for hub subscribers. Subclasses implement ``deliver``; the hub never awaits it."""

    kind = "view"

    def __init__(self, kind: str = "view"):
        if kind not in SUBSCRIBER_KINDS:
            raise ValueError("subscriber kind")
        self.kind = kind
        self.seq = next(_seq)
        self.active = kind != "view"          # views: active while visible
        self.topics: Dict[str, Topic] = {}    # insertion order = priority order
        self.expanded: Dict[str, List[str]] = {}   # watchlist:<id> topic -> its symbols (set by the hub)

    def deliver(self, msg: dict, key: Optional[str] = None) -> None:   # pragma: no cover - interface
        raise NotImplementedError

    def symbols(self, kinds=("trades", "candle")) -> List[str]:
        """Symbols of the topics of ``kinds`` in priority order; ``watchlist:`` topics stand for ``trades:``
        of their (expanded) symbols at their own position."""
        out, seen = [], set()
        expanded = getattr(self, "expanded", None) or {}
        for raw, t in self.topics.items():
            if t.kind in kinds and t.symbol is not None:
                syms = (t.symbol,)
            elif t.kind == "watchlist" and "trades" in kinds:
                syms = expanded.get(raw, ())
            else:
                continue
            for sym in syms:
                if sym not in seen:
                    seen.add(sym)
                    out.append(sym)
        return out

    def wants(self, raw: str) -> bool:
        """True if this subscriber receives the messages of topic ``raw`` (directly or via a watchlist)."""
        if raw in self.topics:
            return True
        expanded = getattr(self, "expanded", None)
        if expanded and raw.startswith("trades:"):
            sym = raw[7:]
            return any(sym in syms for syms in expanded.values())
        return False


def rank_symbols(subscribers, asset_class: str, kinds=("trades", "candle")) -> List[str]:
    """Symbols of ``asset_class`` wanted by ACTIVE subscribers, in priority order (no duplicates)."""
    out, seen = [], set()
    for kind in SUBSCRIBER_KINDS:
        subs = sorted((s for s in subscribers if s.active and s.kind == kind), key=lambda s: s.seq)
        lists = [[sym for sym in s.symbols(kinds) if instrument(sym).asset_class == asset_class] for s in subs]
        for row in itertools.zip_longest(*lists):
            for sym in row:
                if sym is not None and sym not in seen:
                    seen.add(sym)
                    out.append(sym)
    return out
