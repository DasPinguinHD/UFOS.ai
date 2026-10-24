"""Universe presets and resolution (SPEC §7.3, §7.11 GET /v2/universes).

Preset ``liquid_etfs_crypto``: 60 liquid US-listed ETFs across broad market,
size, sectors, countries/regions, bonds, commodities, real estate, factors
and currencies, plus BTC-USD, ETH-USD, SOL-USD. Every entry has a
``category`` and the instrument ``asset_class`` (equity | crypto — this
drives calendars and the spec's ``asset_classes`` filter).

The list is TODAY's selection of liquid funds: backtests on it carry a mild
survivorship/selection bias (validator warning ``survivorship``). Several
funds started after 2016 or were renamed; a symbol without history simply
has no bars before its listing (excluded, never filled).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from ..core.instruments import instrument

CATEGORIES = {
    "broad_market": "US broad market",
    "size": "US size segments",
    "sector": "US sectors",
    "country": "Countries / regions",
    "bonds": "Bonds",
    "commodities": "Commodities",
    "real_estate": "Real estate",
    "factor": "Factors / styles",
    "currency": "Currencies",
    "crypto": "Crypto (spot, 24/7)",
}

_LIQUID_ETFS = (
    ("broad_market", "SPY QQQ DIA VTI"),
    ("size", "IWM IJH IJR MDY"),
    ("sector", "XLB XLC XLE XLF XLI XLK XLP XLRE XLU XLV XLY"),
    ("country", "EFA EEM VGK EWJ FXI EWZ INDA EWG EWU EWC EWY EWT EWA"),
    ("bonds", "TLT IEF SHY AGG LQD HYG TIP EMB MUB BIL"),
    ("commodities", "GLD SLV USO UNG DBC DBA"),
    ("real_estate", "VNQ IYR"),
    ("factor", "MTUM QUAL USMV VLUE SPLV IWF IWD"),
    ("currency", "UUP FXE FXY"),
)
_CRYPTO = ("BTC-USD", "ETH-USD", "SOL-USD")

LIQUID_ETF_SYMBOLS = tuple(s for _c, line in _LIQUID_ETFS for s in line.split())


def _entries() -> List[dict]:
    out = [{"symbol": s, "category": cat, "asset_class": "equity"}
           for cat, line in _LIQUID_ETFS for s in line.split()]
    out += [{"symbol": s, "category": "crypto", "asset_class": "crypto"} for s in _CRYPTO]
    return out


PRESETS: Dict[str, dict] = {
    "liquid_etfs_crypto": {
        "key": "liquid_etfs_crypto",
        "name": "Liquid US ETFs + crypto",
        "note": ("60 liquid US-listed ETFs (broad market, size, sectors, countries, bonds, commodities, "
                 "real estate, factors, currencies) and BTC, ETH, SOL. Today's list of liquid funds — "
                 "backtests carry a mild survivorship bias. Crypto trades 24/7 on UTC days; in a mixed "
                 "universe crypto values are aligned to NYSE closes by when each bar was known."),
        "symbols": _entries(),
    },
}


def universes() -> List[dict]:
    """GET /v2/universes payload items: [{key, name, symbols: [{symbol, category, asset_class}], note}]."""
    return [{"key": p["key"], "name": p["name"], "symbols": [dict(e) for e in p["symbols"]], "note": p["note"]}
            for p in PRESETS.values()]


def preset(key: str) -> Optional[dict]:
    return PRESETS.get(key)


def category_of(symbol: str) -> Optional[str]:
    for p in PRESETS.values():
        for e in p["symbols"]:
            if e["symbol"] == symbol:
                return e["category"]
    return None


def resolve(universe: dict) -> List[str]:
    """The symbols of a (validated) spec universe, in a deterministic order:
    preset order filtered by ``asset_classes``, or the given symbol list
    (normalised by core.instruments). Raises ValueError for an unknown preset
    or an invalid symbol (the validator reports these first)."""
    if "preset" in universe:
        p = PRESETS.get(universe["preset"])
        if p is None:
            raise ValueError(f"unknown universe preset {universe['preset']!r}")
        classes = set(universe.get("asset_classes") or ("equity", "crypto"))
        return [e["symbol"] for e in p["symbols"] if e["asset_class"] in classes]
    return [instrument(s).symbol for s in universe.get("symbols") or []]


def asset_classes_of(symbols: Sequence[str]) -> List[str]:
    return sorted({instrument(s).asset_class for s in symbols})
