"""CSV exports of the HedgeFund loop (fund/SPEC.md §11):
GET /fund/export/orders.csv and /fund/export/decisions.csv.

Paper trading only — every row is a simulated order / decision, not
investment advice. UTF-8 text with a header row, RFC 4180 quoting (csv
module). Text cells that a spreadsheet would treat as a formula (leading
= + - @, tab, CR) get a leading apostrophe (CSV injection guard) — model
rationales are untrusted text. AI-generated rationales are marked in the
``ai_generated`` / ``model`` columns (EU AI Act Art. 50 labelling).
Disclaimer: both exports end with a constant ``note`` column (EXPORT_NOTE)
on every row — a comment or banner line would break CSV parsers, an extra
column doesn't, and it travels with every copied row.
Stdlib only.
"""
from __future__ import annotations

import csv
import io
import json
import math
from typing import Iterable, List, Optional, Sequence

EXPORT_NOTE = "Paper trading · simulated · not investment advice"

ORDER_COLUMNS = ("id", "ts", "symbol", "side", "qty", "status", "origin", "fill_price", "fees", "notional",
                 "filled_at", "reject_reason", "run_id", "recommendation_id", "order_type", "mode", "rationale",
                 "note")

DECISION_COLUMNS = ("id", "ts", "run_id", "symbol", "action", "confidence", "target_weight", "rule_score", "source",
                    "ai_generated", "model", "cost_usd", "status", "skip_reason", "order_id",
                    "technical_score", "technical_confidence", "macro_score", "macro_confidence",
                    "sentiment_score", "sentiment_confidence", "sentiment_source",
                    "risk_size_multiplier", "risk_veto", "risk_veto_short", "rationale", "note")

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return "" if not math.isfinite(value) else repr(value)
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        text = "'" + text
    return text


def to_csv(columns: Sequence[str], rows: Iterable[dict]) -> str:
    """Header + rows; a ``note`` column is always filled with EXPORT_NOTE
    (the source value, if any, is ignored)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([_cell(EXPORT_NOTE if c == "note" else r.get(c)) for c in columns])
    return buf.getvalue()


def orders_csv(store, limit: int = 100000) -> str:
    """All paper orders, oldest first."""
    rows = list(reversed(store.list_orders(limit=limit) or []))
    return to_csv(ORDER_COLUMNS, rows)


def _loads(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _decision_rows(store, limit: int) -> List[dict]:
    db = getattr(store, "db", None)
    if db is None or not db.is_enabled():
        return []
    sql = """SELECT r.id, r.ts, r.run_id, r.symbol, r.action, r.confidence, r.target_weight, r.rule_score,
                    r.source, r.model, r.provenance_json, r.usage_json, r.status, r.skip_reason, r.order_id,
                    r.rationale,
                    t.score AS technical_score, t.confidence AS technical_confidence,
                    m.score AS macro_score, m.confidence AS macro_confidence,
                    s.score AS sentiment_score, s.confidence AS sentiment_confidence, s.source AS sentiment_source,
                    k.details_json AS risk_details
             FROM fund_recommendations r
             LEFT JOIN fund_agent_scores t ON t.run_id = r.run_id AND t.symbol = r.symbol AND t.agent = 'technical'
             LEFT JOIN fund_agent_scores m ON m.run_id = r.run_id AND m.symbol = r.symbol AND m.agent = 'macro'
             LEFT JOIN fund_agent_scores s ON s.run_id = r.run_id AND s.symbol = r.symbol AND s.agent = 'sentiment'
             LEFT JOIN fund_agent_scores k ON k.run_id = r.run_id AND k.symbol = r.symbol AND k.agent = 'risk'
             ORDER BY r.id DESC LIMIT ?"""
    try:
        with store._connect() as conn:
            raw = conn.execute(sql, (max(1, int(limit)),)).fetchall()
    except Exception:
        return []
    out = []
    for r in reversed(raw):
        d = dict(r)
        prov = _loads(d.pop("provenance_json", None))
        usage = _loads(d.pop("usage_json", None))
        risk = _loads(d.pop("risk_details", None)) or {}
        d["ai_generated"] = bool(isinstance(prov, dict) and prov.get("ai_generated"))
        d["cost_usd"] = usage.get("cost_usd") if isinstance(usage, dict) else None
        if isinstance(risk, dict):
            d["risk_size_multiplier"] = risk.get("size_multiplier")
            d["risk_veto"] = risk.get("veto")
            d["risk_veto_short"] = risk.get("veto_short")
        out.append(d)
    return out


def decisions_csv(store, limit: int = 100000) -> str:
    """Recommendations (oldest first) joined with the agent scores of their
    run and symbol (runs that produced a recommendation are exempt from the
    90-day run retention, so their scores stay available)."""
    return to_csv(DECISION_COLUMNS, _decision_rows(store, limit))
