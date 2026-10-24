"""Licence terms per data source (SPEC §3). Shown with GET /v2/data/sources
and stored with the configured source in hedgefund.db (data_sources.licence).

Terms: ``personal_use`` (only the account holder may use the data — no
redistribution), ``commercial_ok`` (public data, reuse allowed with the noted
conditions), ``unknown`` (not clear for all data of the source — check before
any reuse). Not legal advice; UFOS.ai is a proof-of-concept portfolio project.
"""
from __future__ import annotations

TERMS = ("personal_use", "commercial_ok", "unknown")

LICENCES = {
    "alpaca": {
        "licence": "personal_use",
        "note": ("Alpaca free market-data plan (IEX feed, news, corporate actions): for the account "
                 "holder's own use; no redistribution or public display of the data."),
    },
    "fred": {
        "licence": "unknown",
        "note": ("FRED (Federal Reserve Bank of St. Louis): public, cite \"Source: FRED\". Some series are "
                 "third-party copyrighted (e.g. ICE BofA indices such as BAMLH0A0HYM2) — check the series' "
                 "notes before any reuse."),
    },
    "alpaca_sip": {
        "licence": "personal_use",
        "note": ("Alpaca free market-data plan, SIP historical daily bars (backtests): allowed without a "
                 "subscription only for queries ending at least 15 minutes ago; for the account holder's own "
                 "use, no redistribution or public display."),
    },
    "tiingo": {
        "licence": "personal_use",
        "note": ("Tiingo free \"Starter\" plan (end-of-day prices and volume before 2016-01-04 only): "
                 "internal use only — "
                 "for the account holder's own use; no display or sharing with another person or "
                 "organization (https://www.tiingo.com/about/pricing)."),
    },
    "sec_edgar": {
        "licence": "commercial_ok",
        "note": ("SEC EDGAR filings are public US-government data; requests need a declared User-Agent "
                 "(name + e-mail) and must respect the SEC fair-access limit (10 requests/s)."),
    },
}


def licence_for(source: str) -> dict:
    """{"licence": term, "note": text} for ``source``; ``unknown`` with a
    note when nothing is recorded for it."""
    entry = LICENCES.get(source)
    if entry is None:
        return {"licence": "unknown", "note": f"No licence information is recorded for '{source}'."}
    return dict(entry)
