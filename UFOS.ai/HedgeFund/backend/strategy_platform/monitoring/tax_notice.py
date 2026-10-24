"""The fixed tax notice (SPEC §19.15). User decision 2026-10-18: UFOS.ai has NO tax export — "Ich glaube es ist
nicht gut, wenn wir ein System zum export von steuerdaten haben. Das ist riskant, weil das muss alles richtig sein.
Ich glaube es ist besser, darauf hinzuweisen, dass man sich seine steuerberichte bei den brokern extra holen muss".
The backend and the WPF share this ONE wording through ``GET /v2/monitoring/tax-notice``.
"""
from __future__ import annotations

VERSION = 1
DECIDED = "2026-10-18"
NOTICE = ("UFOS.ai does not produce tax data or tax reports. For your tax return, use the official annual tax documents "
          "from each broker (e.g. the Jahressteuerbescheinigung of a German broker, or the broker's annual tax "
          "statement). Figures shown in UFOS.ai (P&L, realised gains, reports) are for monitoring only and are not a "
          "basis for tax filings.")
PAPER_NOTICE = "Paper and demo accounts are simulated and have no tax relevance."


def payload() -> dict:
    return {"version": VERSION, "decided": DECIDED, "notice": NOTICE, "paper_notice": PAPER_NOTICE,
            "tax_export": False}
