"""Which ONE source may price an instrument, in which currency (stage 9d, SPEC §20.11). No fallback chains.

``plan_for(meta, maps)`` → (Plan, None) or (None, reason). ``meta``: key, symbol, isin, asset_class and
``currency`` = the currency of the position's cost / the trades. Order (after a valuation, which callers check
first): issuer derivatives and bonds never have a source; crypto BTC / ETH / SOL in EUR → Kraken public (fixed
table); a user mapping of the ISIN → its listing (EODHD in the listing's currency, or the Alpaca US ticker in USD);
unmapped, a USD position with a symbol in the import and a US ISIN → Alpaca (as §19.14 H2). The caller compares
``plan.currency`` with the cost currency: equal → value + P&L; different → value only, "different currencies — not
converted".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

MAP_HINT = "pick its listing (Shadow Account → Symbol mapping)"
DIFFERENT = "different currencies — not converted"


@dataclass(frozen=True)
class Plan:
    provider: str            # alpaca_us | eodhd | kraken_public
    symbol: str              # ticker / CODE.EXCHANGE / display pair
    currency: str            # the listing's currency
    source: str              # user_mapping | import | fixed_pair
    detail: str              # the price_detail text

    @property
    def request_symbol(self) -> str:
        if self.provider == "kraken_public":
            return self.symbol.split("/")[0].replace("XBT", "BTC")
        return self.symbol


def plan_for(m: dict, maps: Dict[str, dict]) -> Tuple[Optional[Plan], Optional[str]]:
    from ...data.providers import kraken_public
    ac = m.get("asset_class")
    isin = m.get("isin") or ""
    cur = m.get("currency")
    if ac == "derivative":
        return None, "derivative — issuer product without a price source; enter a valuation price"
    if ac == "bond":
        return None, "bond — no price source; enter a valuation price"
    if ac == "crypto":
        sym = (m.get("symbol") or "").upper()
        pair = kraken_public.pair_for(sym)
        if pair is None or cur != "EUR":
            return None, f"crypto {sym or '?'} — only BTC, ETH, SOL in EUR (Kraken public)"
        return Plan("kraken_public", pair[1], "EUR", "fixed_pair", f"Kraken close of {pair[1]} (public market data)"), None
    mp = maps.get(isin)
    if mp is not None:
        if mp.get("price_source") == "eodhd":
            return Plan("eodhd", mp["listing"], mp["currency"], "user_mapping",
                        f"EODHD close of {mp['listing']} ({mp['currency']}, your listing)"), None
        return Plan("alpaca_us", mp["symbol"], "USD", "user_mapping",
                    f"market close via mapped symbol {mp['symbol']} (user mapping)"), None
    if isin and m.get("symbol") and cur == "USD" and isin.startswith("US"):
        return Plan("alpaca_us", m["symbol"], "USD", "import", f"market close of {m['symbol']} (symbol from the import)"), None
    if isin:
        return None, f"no listing mapped — {MAP_HINT}"
    if not m.get("symbol"):
        return None, "no symbol in the import"
    if cur != "USD":
        return None, f"traded in {cur} — market data only for USD-listed US securities"
    return None, "no ISIN in the import — market data only for identified US securities (US ISIN)"


def bars(plan: Plan) -> List[List]:
    """[[day, close]] (listing currency) of an EODHD or Kraken plan — raises UserFacingError with the reason."""
    if plan.provider == "eodhd":
        from ...data.providers import eodhd
        return eodhd.closes(plan.symbol)
    if plan.provider == "kraken_public":
        from ...data.providers import kraken_public
        return kraken_public.closes(plan.request_symbol)
    raise ValueError(plan.provider)


def reason_of(ex: BaseException) -> str:
    return str(getattr(ex, "user_message", None) or ex)[:240]
