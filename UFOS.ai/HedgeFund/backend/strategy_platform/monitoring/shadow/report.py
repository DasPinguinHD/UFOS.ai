"""Exportable Shadow Account audit report (SPEC §19.8): self-contained HTML (inline CSS, NO script, NO external
resource, every text HTML-escaped — statement texts are untrusted) or JSON. Returned to the WPF, which saves it as a
local file; the backend records only an audit row (``shadow_report_export``, sha256).
"""
from __future__ import annotations

import hashlib
import html
import json
import math
from datetime import datetime, timezone
from typing import List, Optional

from . import service

DISCLAIMER = ("Hypothetical comparison and behaviour statistics of your own past trades — monitoring only, for your "
              "own review. Your data never left this computer.")          # S10-H context wording
METHOD = [
    "FIFO lots per instrument (ISIN, else symbol); a sale beyond the imported purchases is listed as unmatched.",
    "Disposition effect after Odean (1998): on each sell day every held position counts as a realised or paper "
    "gain/loss; PGR = RG/(RG+PG), PLR = RL/(RL+PL).",
    "Turnover and concentration on the cost basis (no prices needed); timing = benchmark 20-session forward returns "
    "after your trades vs its average.",
    "Shadow portfolios receive exactly your dated trade cash flows (buys in, sale proceeds out) at the day's close; "
    "no costs, taxes or slippage; fractional units; a withdrawal beyond the shadow's value is financed at 0 %.",
    "Currency: FRED DEXUSEU (Federal Reserve H.10 noon buying rate), not the ECB reference rate.",
]


def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _n(v, nd=2, pct=False) -> str:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        return "—"
    return f"{v * 100:.{nd}f} %" if pct else f"{v:,.{nd}f}"


def build(account_id: str, fmt: str = "html", rules: Optional[List[dict]] = None, benchmark: str = "SPY",
          actor: str = "user") -> dict:
    from ...storage import db
    det = service.detail(account_id)
    diag = service.diagnose(account_id, benchmark)
    sh = service.shadows(account_id, rules or [{"type": "buy_and_hold", "symbol": benchmark}])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    from .. import tax_notice
    payload = {"generated_at": now, "generator": "UFOS.ai Hedge Fund · Shadow Account", "disclaimer": DISCLAIMER,
               "tax_notice": tax_notice.NOTICE,
               "account": det["account"], "imports": det["imports"], "positions": det["positions"],
               "unmatched": det["unmatched"], "valuation": det["valuation"],
               "diagnostics": {k: v for k, v in diag.items() if k != "facts"}, "shadows": sh, "method": METHOD}
    payload["price_notes"] = price_notes(diag, sh)
    if diag.get("unavailable"):
        payload["diagnostics"]["message"] = NO_TRADES if not det["imports"] else "Nothing to analyse: " + \
            diag["unavailable"]
    if fmt == "json":
        content = json.dumps(payload, ensure_ascii=False, indent=1, default=str, allow_nan=False)
        ctype, ext = "application/json", "json"
    else:
        content = _html(payload)
        ctype, ext = "text/html; charset=utf-8", "html"
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    db.add_audit(actor, "shadow_report_export", {"account_id": account_id, "format": fmt, "sha256": sha})
    return {"filename": f"ufos-shadow-report-{account_id}-{now[:10]}.{ext}", "content_type": ctype,
            "content": content, "sha256": sha, "generated_at": now}


PRICE_NOTES = {"eodhd": "Prices: EODHD (personal use, no redistribution).",
               "kraken_public": "Prices: Kraken public market data (crypto in EUR)."}


def price_notes(diag: dict, sh: dict) -> List[str]:
    """The data-source notes SPEC §20.11 promises, for the sources this report's numbers used."""
    used = {s.get("provider") for s in ((diag.get("coverage") or {}).get("price_sources") or [])}
    return [PRICE_NOTES[k] for k in ("eodhd", "kraken_public") if k in used]


def _table(head: List[str], rows: List[List[str]]) -> str:
    h = "".join(f"<th>{_e(x)}</th>" for x in head)
    b = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{b or '<tr><td colspan=99>—</td></tr>'}</tbody></table>"


NO_TRADES = "No trades imported yet — nothing to analyse."


def _block(title: str, d: dict, fields: List[tuple], why: Optional[str] = None) -> str:
    if not isinstance(d, dict):
        # never an empty heading: the diagnostic is named with the reason it cannot be computed (9c addendum)
        return f"<h3>{_e(title)}</h3><p class=muted>Not available: {_e(why or 'not computed')}</p>"
    if d.get("not_meaningful"):          # the diagnostics coverage rule (diagnostics.MIN_PRICED_SHARE)
        text = str(d["unavailable"])
        return f"<h3>{_e(title)}</h3><p class=muted>{_e(text[:1].upper() + text[1:])}</p>"
    if "unavailable" in d:
        return f"<h3>{_e(title)}</h3><p class=muted>Not available: {_e(d['unavailable'])}</p>"
    rows = []
    for key, label, kind in fields:
        v = d.get(key)
        rows.append([label, _n(v, 1, pct=True) if kind == "pct" else (_n(v, 2) if kind == "num" else
                                                                       ("—" if v is None else str(v)))])
    return f"<h3>{_e(title)}</h3>" + _table(["Measure", "Value"], rows)


def _html(p: dict) -> str:
    acc, d, sh = p["account"], p["diagnostics"], p["shadows"]
    cur = acc["base_currency"]
    parts = [f"<h1>Shadow Account audit report</h1><p class=note>{_e(p['disclaimer'])}</p>"
             f"<p class=note>{_e(p['tax_notice'])}</p>",
             f"<p>Account: <b>{_e(acc['label'])}</b> ({_e(acc['id'])}) · base currency {_e(cur)} · generated "
             f"{_e(p['generated_at'])}</p>"]
    parts.append("<h2>Imported statements</h2>" + _table(
        ["File", "SHA-256", "Template", "Rows", "Accepted", "Rejected", "Duplicates", "Skipped", "Imported at"],
        [[i["filename"], i["sha256"][:16] + "…", i["template"], i["rows"], i["accepted"], i["rejected"],
          i["duplicates"], i["skipped"], i["created_at"]] for i in p["imports"]]))
    parts.append("<h2>Open positions (FIFO, cost basis)</h2>" + _table(
        ["Instrument", "ISIN", "Symbol", "Class", "Quantity", "Cost", "Currency"],
        [[x["name"] or x["key"], x["isin"] or "", x["symbol"] or "", x["asset_class"] or "unclassified",
          _n(x["qty"], 4), _n(x["cost"]), x["currency"] or ""] for x in p["positions"]]))
    if p["unmatched"]:
        parts.append("<h2>Unmatched sales (missing purchase history)</h2>" + _table(
            ["Date", "Instrument", "Quantity"], [[u["date"], u["key"], _n(u["qty"], 4)] for u in p["unmatched"]]))
    parts.append("<h2>Behaviour diagnostics</h2>")
    why = None
    if d.get("unavailable"):
        why = "no trades imported" if not p["imports"] else d["unavailable"]
        parts.append(f"<p class=muted>{_e(NO_TRADES if not p['imports'] else 'Nothing to analyse: ' + why)}</p>")
    if d.get("period"):
        parts.append(f"<p>Period {_e(d['period']['first'])} – {_e(d['period']['last'])}</p>")
    parts.append(_block("Disposition effect", d.get("disposition"), [
        ("pgr", "PGR (gains realised)", "pct"), ("plr", "PLR (losses realised)", "pct"), ("de", "PGR − PLR", "pct"),
        ("t_stat", "t statistic", "num"), ("sell_days", "Sell days", "int"), ("unpriced_cases", "Unpriced cases",
                                                                               "int")], why))
    parts.append(_block("Trading activity", d.get("turnover"), [
        ("trades", "Trades", "int"), ("trades_per_month", "Trades per month", "num"),
        ("turnover_annual", "Turnover per year (× cost basis)", "num"), ("fees", f"Fees ({cur})", "num"),
        ("fee_share_pct", "Fees % of volume", "num"), ("round_trips_under_30d", "Round trips < 30 days", "int")],
        why))
    parts.append(_block("Holding periods (days)", d.get("holding"), [
        ("median_days", "Median", "int"), ("p25_days", "25th percentile", "int"), ("p75_days", "75th percentile",
                                                                                  "int"),
        ("winners_median_days", "Winners (median)", "int"), ("losers_median_days", "Losers (median)", "int")],
        why))
    conc = d.get("concentration")
    parts.append(_block("Concentration (current, cost basis)", None if conc is None else
                        (conc.get("current") if "unavailable" not in conc else conc),
                        [("top1_pct", "Largest position %", "num"), ("top3_pct", "Top 3 %", "num"),
                         ("effective_n", "Effective number of positions", "num")], why))
    parts.append(_block("Trading after losses", d.get("after_losses"), [
        ("buys_after_loss_avg", "Buys within 5 days after a loss", "num"),
        ("buys_after_gain_avg", "Buys within 5 days after a gain", "num"), ("ratio", "Ratio", "num")], why))
    parts.append(_block("Timing vs market", d.get("timing"), [
        ("benchmark", "Benchmark", "text"), ("buy_fwd_avg_pct", "20-session return after buys %", "num"),
        ("sell_fwd_avg_pct", "20-session return after sells %", "num"),
        ("uncond_fwd_avg_pct", "Average 20-session return %", "num")], why))
    u = sh.get("user") or {}
    if u.get("end_value") is None:       # never a stand-in 0.00 (9c addendum) — like an unavailable shadow row
        rows = [["Your portfolio", "—", "—", "—", "—", "not available: " + (u.get("message") or "no value")]]
    else:
        rows = [["Your portfolio", _n(u.get("end_value")), _n(u.get("pnl")), _n(u.get("mwr_annual"), 1, pct=True),
                 "—", u.get("message") or ""]]
    for s in sh.get("shadows") or []:
        if "unavailable" in s:
            rows.append([s["label"], "—", "—", "—", "—", "not available: " + s["unavailable"]])
        else:
            rows.append([s["label"], _n(s["end_value"]), _n(s["pnl"]), _n(s["mwr_annual"], 1, pct=True),
                         _n(s["twr"], 1, pct=True), ("flows identical" if s["parity"]["flows_equal"] else
                                                     "FLOW MISMATCH") + (f"; {s['conversion']}" if s.get("conversion")
                                                                         else "")])
    parts.append(f"<h2>Shadow portfolios (same cash flows, {_e(cur)})</h2>" + _table(
        ["Portfolio", "End value", "P&L", "Money-weighted / year", "Time-weighted", "Note"], rows))
    parts.append(f"<p class=note>{_e(sh.get('notice'))}</p>")
    for note in p.get("price_notes") or []:
        parts.append(f"<p class=note>{_e(note)}</p>")
    parts.append("<h2>Method</h2><ul>" + "".join(f"<li>{_e(m)}</li>" for m in p["method"]) + "</ul>")
    css = ("body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1b1b1b;background:#fff}"
           "table{border-collapse:collapse;margin:8px 0 16px}th,td{border:1px solid #ccc;padding:4px 8px;"
           "text-align:left;font-size:13px}th{background:#f2f2f2}.note{color:#555;font-size:12px}"
           ".muted{color:#777}h1{font-size:22px}h2{font-size:17px;margin-top:24px}h3{font-size:14px}")
    return ("<!doctype html><html lang=en><head><meta charset=utf-8><meta http-equiv=Content-Security-Policy "
            "content=\"default-src 'none'; style-src 'unsafe-inline'\"><title>Shadow Account audit report</title>"
            f"<style>{css}</style></head><body>" + "".join(parts) + "</body></html>")
