"""Instruments: symbols, asset classes, and the mapping to Alpaca symbols.

Platform symbols: US stocks/ETFs as on the exchange ("SPY", "BRK.B"); crypto
as "BASE-USD" ("BTC-USD"), the format the old engine already uses. Alpaca
uses "BTC/USD" for crypto pairs.
"""
import re
from dataclasses import dataclass

CRYPTO_RE = re.compile(r"^([A-Z0-9]{2,10})-USD$")
EQUITY_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

KNOWN_ETFS = frozenset(
    "SPY QQQ IWM DIA TLT IEF SHY GLD SLV USO UNG XLE XLF XLK XLV XLI XLY XLP XLU XLB XLRE XLC "
    "EEM EFA HYG LQD UUP VNQ "
    # stage 2: the rest of the liquid_etfs_crypto universe (strategy/universe.py)
    "VTI IJH IJR MDY VGK EWJ FXI EWZ INDA EWG EWU EWC EWY EWT EWA AGG TIP EMB MUB BIL DBC DBA IYR "
    "MTUM QUAL USMV VLUE SPLV IWF IWD FXE FXY".split())


@dataclass(frozen=True)
class Instrument:
    symbol: str          # platform symbol
    asset_class: str     # "crypto" | "equity"
    kind: str            # "crypto" | "etf" | "stock"
    alpaca_symbol: str   # "BTC/USD" or the equity symbol
    fractional: bool     # crypto: 8 decimals; equities: whole shares (old engine rule)


def asset_class(symbol: str) -> str:
    # normalised like instrument() (fix 2026-10-02: "btc-usd" was classed as equity)
    return "crypto" if CRYPTO_RE.match(str(symbol or "").strip().upper()) else "equity"


def instrument(symbol: str) -> Instrument:
    s = (symbol or "").strip().upper()
    m = CRYPTO_RE.match(s)
    if m:
        return Instrument(s, "crypto", "crypto", f"{m.group(1)}/USD", True)
    if not EQUITY_RE.match(s):
        raise ValueError(f"not a supported symbol: {symbol!r}")
    return Instrument(s, "equity", "etf" if s in KNOWN_ETFS else "stock", s, False)


def from_alpaca_symbol(alpaca_symbol: str) -> str:
    """"BTC/USD" -> "BTC-USD"; equities unchanged."""
    s = (alpaca_symbol or "").strip().upper()
    return s.replace("/", "-") if "/" in s else s
