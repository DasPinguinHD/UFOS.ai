"""Portfolio insights (SPEC §20, stage 9b): a ROUGH GUIDE over the user's real depots (the Shadow Account imports
only — never platform, paper or broker accounts). Realised P&L (FIFO, brokerage / other / unclassified — §20.9),
dividends,
withholding, interest and fees as given in the imports, unrealised P&L and allocation — per account, per currency,
for the current and the previous calendar year (UTC). NO FX conversion of sums: every sum is per currency. Not tax
data (§19.15). Computed locally; nothing is sent to an AI.

Price per open position, in this order, never estimated: (1) a valuation (one contained in an import first, then
one the user entered); (2) the ONE configured source (``shadow/pricing.py``, §20.11): Alpaca for US tickers (user
mapping, or the import's symbol for a USD position), EODHD for a listing the user picked, Kraken public for BTC /
ETH / SOL in EUR — the latest RAW close in the listing's currency; same currency as the cost → value + P&L, another
currency → the value in that currency only ("different currencies — not converted"); (3) "not available (no price
source)". Never an FX conversion (user decision 2026-10-18).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ..execution.errors import TradingError
from ..storage import db
from ..core.errors import UserFacingError
from . import prices, tax_notice
from .shadow import normalize, pricing, symbols
from .shadow import service as shadow

LABEL = ("Rough guide only — not tax data. The broker's annual tax statement (e.g. Jahressteuerbescheinigung) is "
         "authoritative.")
PRIVACY = "Computed on this computer from your imported statements — nothing is sent anywhere."
LIMITS = [
    "Original currency everywhere — no FX conversion. A listing in another currency than the cost shows its value "
    "without P&L (different currencies — not converted).",
    "EODHD listings: splits after your purchase are not checked — set a valuation if a split happened. EODHD data "
    "is for your personal use only.",
    "Only your imported statements (Shadow Account) are used — no platform, paper or broker accounts.",
    "Realised P&L uses FIFO over the imported transactions; sales without an imported purchase are not used.",
    "Corporate actions (splits, spin-offs, mergers) are not part of the imports; a position with a stock split after "
    "its first purchase is not valued from market data.",
    "Dividends, withholding, interest, fees and tax refunds are taken as given in the imports — nothing is "
    "recomputed.",
    "Staking rewards are acquisitions at their market value at receipt (as reported in the import).",
    "Pending private-market orders (pre-payments) are listed separately and are not positions.",
    "Not tax data and no tax categories — use your broker's annual tax statement for your return.",
]
BUCKETS = ("brokerage", "other", "unclassified")
# §20.9 (user decision 2026-10-18): stocks AND ETFs / funds together; "other" = bonds, crypto, derivatives, the rest
BUCKET_LABELS = {"brokerage": "Brokerage (stocks + ETFs)", "other": "Other (bonds, crypto, derivatives, other)",
                 "unclassified": "Unclassified"}
BROKERAGE_CLASSES = ("share", "etf", "fund")
STAKING_NOTE = "includes staking rewards (cost basis = market value at receipt)"
PRICE_WINDOW_DAYS = 20          # ≈ 10–14 sessions: the latest close and the one before (review MEDIUM)
NO_PRICE = "not available (no price source)"


def _bucket(ac: Optional[str]) -> str:
    if ac in BROKERAGE_CLASSES:
        return "brokerage"
    if ac in normalize.ASSET_CLASSES:
        return "other"
    return "unclassified"


def _r(v, nd: int = 2):
    return None if v is None else round(float(v), nd)


def _today() -> date:
    from ..execution import accounts
    return accounts.now().astimezone(timezone.utc).date()


class _Year:
    def __init__(self):
        self.c: Dict[str, dict] = {}

    def cur(self, c: str) -> dict:
        if c not in self.c:
            self.c[c] = {"realised": {b: {"gains": 0.0, "losses": 0.0, "net": 0.0, "parts": 0} for b in BUCKETS},
                         "dividends": 0.0, "withholding": 0.0, "interest": 0.0, "trading_fees": 0.0, "other_fees": 0.0,
                         "taxes_on_sales": 0.0, "tax_refunds": 0.0}
        return self.c[c]

    def merge(self, other: "_Year") -> None:
        for c, v in other.c.items():
            t = self.cur(c)
            for b in BUCKETS:
                for k in ("gains", "losses", "net", "parts"):
                    t["realised"][b][k] += v["realised"][b][k]
            for k in ("dividends", "withholding", "interest", "trading_fees", "other_fees", "taxes_on_sales",
                      "tax_refunds"):
                t[k] += v[k]

    def out(self) -> dict:
        res = []
        for c in sorted(self.c):
            v = self.c[c]
            real = {b: {"gains": _r(v["realised"][b]["gains"]), "losses": _r(v["realised"][b]["losses"]),
                        "net": _r(v["realised"][b]["net"]), "parts": v["realised"][b]["parts"]} for b in BUCKETS}
            real["net"] = _r(math.fsum(v["realised"][b]["net"] for b in BUCKETS))
            res.append({"currency": c, "realised": real, "dividends": _r(v["dividends"]),
                        "withholding": _r(v["withholding"]), "interest": _r(v["interest"]),
                        "trading_fees": _r(v["trading_fees"]), "other_fees": _r(v["other_fees"]),
                        "fees": _r(v["trading_fees"] + v["other_fees"]), "taxes_on_sales": _r(v["taxes_on_sales"]),
                        "tax_refunds": _r(v["tax_refunds"])})
        return {"currencies": res}


def _blank(key: str, m: dict, cur: str, qty: float, cost: Optional[float]) -> dict:
    return {"key": key, "symbol": m.get("symbol"), "isin": m.get("isin"), "name": m.get("name"),
            "asset_class": m.get("asset_class"), "currency": cur, "qty": qty, "broker_qty": None, "cost": _r(cost),
            "avg_cost": _r(cost / qty, 6) if cost is not None and qty else None, "price": None,
            "price_source": "none", "valuation_source": None, "as_of": None, "value": None, "unrealised": None,
            "unrealised_pct": None, "day_change": None, "weight": None, "note": None,
            "price_detail": None, "market_symbol": None, "symbol_source": None, "price_provider": None, "listing": None}


QTY_TOL_ABS = 1e-6
QTY_TOL_REL = 1e-4


def _valuations(account_id: str):
    """(latest import valuation per (key, currency) with the broker's qty, latest user valuation per (key, currency
    or None), the newest import snapshot {(key, currency): entry} — for broker positions without imported lots)."""
    imp: Dict[Tuple[str, str], dict] = {}
    usr: Dict[Tuple[str, Optional[str]], dict] = {}
    newest_day, snap = None, {}
    for row in shadow.valuation_rows(account_id):
        if row["source"] == "import":
            if newest_day is None or row["as_of"] > newest_day:
                newest_day, snap = row["as_of"], {}
            for p in row["prices"]:
                k = (p["key"], p.get("currency"))
                e = {"price": float(p["price"]), "as_of": row["as_of"], "qty": p.get("qty")}
                if k not in imp or row["as_of"] >= imp[k]["as_of"]:
                    imp[k] = e
                if row["as_of"] == newest_day:
                    snap[k] = e
        else:
            for p in row["prices"]:
                k = (p["key"], p.get("currency"))
                if k not in usr or row["as_of"] >= usr[k]["as_of"]:
                    usr[k] = {"price": float(p["price"]), "as_of": row["as_of"]}
    return imp, usr, snap


def _qty_mismatch(broker_qty, book_qty: float) -> bool:
    if broker_qty is None:
        return False
    return abs(float(broker_qty) - book_qty) > max(QTY_TOL_ABS, QTY_TOL_REL * max(abs(book_qty), 1.0))


def _account(acc: dict, years: Tuple[int, int], today: date, maps: Optional[Dict[str, dict]] = None) -> dict:
    tx = shadow.txns(acc["id"])
    maps = symbols.mappings() if maps is None else maps
    ys = {y: _Year() for y in years}
    book = normalize.Book()
    meta: Dict[str, dict] = {}
    first_buy: Dict[Tuple[str, str], str] = {}
    not_used: List[dict] = []
    pending: List[dict] = []
    unpaired: List[dict] = []
    staked: set = set()
    for t in tx:
        y = int(t["date"][:4])
        yb = ys.get(y)
        if t["key"]:
            m = meta.setdefault(t["key"], {"symbol": None, "isin": None, "name": None, "asset_class": None,
                                           "currency": t["currency"]})
            for k in ("symbol", "isin", "name", "asset_class"):
                if t.get(k):
                    m[k] = t[k]
        k = t["kind"]
        cur = t["currency"]
        if k == "pending_buy":
            pending.append({"date": t["date"], "key": t["key"], "name": t.get("name"), "currency": cur,
                            "amount": _r(t["gross"]), "fee": _r(t["fee"]),
                            "note": "pending private-market order (pre-payment) — not a position"
                                    + ("; its fee is included in the fees" if t["fee"] else "")})
            if yb is not None:
                yb.cur(cur)["trading_fees"] += t["fee"] or 0.0          # paid now (review fix 2026-10-18)
            continue
        if k == "corporate_action":
            unpaired.append({"date": t["date"], "key": t["key"], "qty": t.get("qty"), "note": t.get("note"),
                             "ext_id": t.get("ext_id")})
            continue
        if k in ("buy", "sell", "staking"):
            bk = (t["key"], cur)                                 # lots per currency: never mixed
            if k in ("buy", "staking"):
                first_buy.setdefault(bk, t["date"])
                if k == "staking":
                    staked.add(f"{t['key']}|{cur}")
                book.buy(f"{t['key']}|{cur}", t["date"], t["qty"], {"c": t["gross"] + t["fee"]},
                         {"ac": t.get("asset_class")})
            else:
                before = len(book.unmatched)
                parts = book.sell(f"{t['key']}|{cur}", t["date"], t["qty"], {"c": t["gross"] - t["fee"]},
                                  {"ac": t.get("asset_class")})
                if yb is not None:
                    for p in parts:
                        g = p["proceeds"]["c"] - p["cost"]["c"]
                        ac = (p["close_meta"] or {}).get("ac") or (p["meta"] or {}).get("ac") \
                            or (meta.get(t["key"]) or {}).get("asset_class")
                        r = yb.cur(cur)["realised"][_bucket(ac)]
                        r["gains" if g >= 0 else "losses"] += g
                        r["net"] += g
                        r["parts"] += 1
                if len(book.unmatched) > before and y in years:
                    u = book.unmatched[-1]
                    not_used.append({"date": t["date"], "key": t["key"], "qty": u["qty"],
                                     "reason": "no earlier purchase in the imported data"})
            if yb is not None:
                yb.cur(cur)["trading_fees"] += t["fee"] or 0.0
                if k == "sell":
                    yb.cur(cur)["taxes_on_sales"] += t["tax"] or 0.0     # as reported in the import (review LOW)
            continue
        if yb is None:
            continue
        c = yb.cur(cur)
        if k == "dividend":
            c["dividends"] += t["gross"] or 0.0
            c["withholding"] += t["tax"] or 0.0
        elif k == "interest":
            c["interest"] += t["gross"] or 0.0
            c["withholding"] += t["tax"] or 0.0
        elif k == "withholding":
            c["withholding"] += t["tax"] or 0.0
        elif k == "fee":
            c["other_fees"] += t["fee"] or 0.0
        elif k == "tax_refund":
            c["tax_refunds"] += t["tax"] or 0.0                 # as reported in the import
    # --- open positions ------------------------------------------------------------------------------------------
    imp, usr, snap = _valuations(acc["id"])
    hold = {kk: h for kk, h in book.holdings().items() if h["qty"] > 1e-9}
    positions: List[dict] = []
    closes = None
    held_ccy: Dict[str, set] = {}
    for kk in hold:
        key, cur = kk.rsplit("|", 1)
        held_ccy.setdefault(key, set()).add(cur)
    for kk, h in sorted(hold.items()):
        key, cur = kk.rsplit("|", 1)
        m = dict(meta.get(key) or {}, currency=cur)
        cost = h["amounts"]["c"]
        pos = _blank(key, m, cur, h["qty"], cost)
        why = None
        v = imp.get((key, cur))
        src = "import" if v else None
        if v is not None and _qty_mismatch(v.get("qty"), h["qty"]):
            why = (f"broker reports {float(v['qty']):g}, imported lots give {h['qty']:g} on {v['as_of']} — missing "
                   "history or a split?")
            pos["broker_qty"] = float(v["qty"])
            v = None
        if v is None and why is None:
            u = usr.get((key, cur))
            if u is None and (key, None) in usr:
                if len(held_ccy.get(key, ())) == 1:
                    u = usr[(key, None)]
                else:
                    why = (f"your valuation of {key} has no currency, but it is held in "
                           f"{' and '.join(sorted(held_ccy[key]))} — enter the price with its currency")
            if u is not None:
                v, src = u, "user"
        if v is not None:
            pos.update(price=v["price"], price_source="valuation", valuation_source=src, as_of=v["as_of"],
                       price_detail="valuation in the import" if src == "import" else "your valuation")
        elif why is None:
            plan, why = pricing.plan_for(m, maps)
            raw = None
            if why is None and plan.provider == "alpaca_us":
                sym = plan.symbol
                if closes is None:
                    first = min(first_buy.values()) if first_buy else today.isoformat()
                    # a short recent price window (cheap on every poll) + the splits since the first purchase
                    closes = prices.Closes(today - timedelta(days=PRICE_WINDOW_DAYS), today,
                                           actions_start=date.fromisoformat(first) - timedelta(days=1))
                fb = first_buy.get((key, cur)) or today.isoformat()
                raw = closes.raw(sym)
                sp = closes.splits_between(sym, fb, today.isoformat()) if raw is not None else []
                if raw is not None and closes.splits(sym) is None:
                    why = f"no corporate actions for {sym}"
                elif sp:
                    why = f"stock split of {sym} on {sp[0][0]} — the imported lots are not split-adjusted"
                elif not raw:
                    why = f"no market data for {sym} ({closes.errors.get(sym, 'no bars')})"
            elif why is None:
                try:
                    raw = dict(tuple(b) for b in pricing.bars(plan))
                except (UserFacingError, ValueError) as ex:
                    why = pricing.reason_of(ex)
                if why is None and not raw:
                    why = f"no closes for {plan.symbol} from {plan.provider}"
            if why is None:
                days = sorted(d for d in raw if d <= today.isoformat())
                if not days:
                    why = f"no market data for {plan.symbol} up to today"
                else:
                    last = days[-1]
                    px, prev = raw[last], (raw[days[-2]] if len(days) >= 2 else None)
                    common = dict(as_of=last, market_symbol=plan.symbol, symbol_source=plan.source,
                                  price_provider=plan.provider)
                    if plan.currency == cur:
                        pos.update(price=px, price_source="market", price_detail=plan.detail, **common)
                        if prev is not None:
                            pos["day_change"] = _r(h["qty"] * (px - prev))
                    else:
                        # original currency everywhere (§20.11): the value in the listing's currency, NO P&L
                        lv = h["qty"] * px
                        pos.update(price_source="market_other_currency",
                                   listing={"currency": plan.currency, "price": px, "value": _r(lv), "as_of": last},
                                   price_detail=f"{plan.detail} — {plan.currency} {lv:,.2f}; {pricing.DIFFERENT}",
                                   note=f"{pricing.DIFFERENT} (listing {plan.currency}, cost {cur})", **common)
        if why is not None:
            pos["note"] = f"{NO_PRICE}: {why}"
        if kk in staked:
            pos["note"] = f"{pos['note']}; {STAKING_NOTE}" if pos["note"] else STAKING_NOTE
        if pos["price"] is not None:
            val = h["qty"] * pos["price"]
            pos.update(value=_r(val), unrealised=_r(val - cost),
                       unrealised_pct=_r((val - cost) / cost * 100.0) if cost > 0 else None)
        positions.append(pos)
    # broker positions (newest import snapshot) without imported lots: listed, never dropped (review HIGH)
    for (key, cur), e in sorted(snap.items(), key=lambda x: (x[0][0], x[0][1] or "")):
        if f"{key}|{cur}" in hold:
            continue
        m = dict(meta.get(key) or {}, currency=cur)
        pos = _blank(key, m, cur, 0.0, None)
        pos.update(broker_qty=float(e["qty"]) if e.get("qty") is not None else None, as_of=e["as_of"],
                   note=f"{NO_PRICE}: broker reports {float(e.get('qty') or 0):g} on {e['as_of']}, no imported lots — "
                        "import the older statements")
        positions.append(pos)
    allocation = allocation_of(positions)
    rejected = [{"import_id": i["id"], "filename": i["filename"], "rejected": i["rejected"]}
                for i in shadow.imports_of(acc["id"]) if i["rejected"]]
    reasons = _reasons(positions, not_used, ys, rejected)
    # a corporate action paired later (an overlapping export brought the partner: ext id "warrant|tilg") is resolved
    paired = [t["ext_id"] for t in tx if t["kind"] == "sell" and "|" in (t.get("ext_id") or "")]
    unpaired = [u for u in unpaired if not u["ext_id"] or not any(
        p.startswith(u["ext_id"] + "|") or p.endswith("|" + u["ext_id"]) for p in paired)]
    for u in unpaired:
        reasons.append(f"corporate action on {u['date']} for {u['key']} without a matching cash booking — the "
                       "position is not closed: " + (u["note"] or "partner missing"))
    return {"account_id": acc["id"], "label": acc["label"],
            "templates": sorted({i["template"] for i in shadow.imports_of(acc["id"]) if i["template"]}),
            "state": "incomplete" if reasons else ("ok" if tx else "no_imports"), "reasons": reasons,
            "_years": ys, "years": {str(y): ys[y].out() for y in years}, "positions": positions,
            "allocation": allocation, "not_used": not_used, "rejected_rows": rejected, "pending": pending,
            "unpaired_corporate_actions": unpaired}


def allocation_of(positions: List[dict]) -> List[dict]:
    out = []
    for cur in sorted({p["currency"] for p in positions}):
        ps = [p for p in positions if p["currency"] == cur]
        valued = [p for p in ps if p["value"] is not None]
        mv = math.fsum(p["value"] for p in valued)
        cost_v = math.fsum(p["cost"] for p in valued)
        cost_t = math.fsum(p["cost"] for p in ps if p["cost"] is not None)
        for p in valued:
            p["weight"] = _r(p["value"] / mv, 6) if mv > 0 else None
        conc = None
        if mv > 0:
            w = sorted((p["value"] / mv for p in valued), reverse=True)
            hhi = math.fsum(x * x for x in w)
            conc = {"hhi": _r(hhi, 4), "effective_n": _r(1 / hhi, 2) if hhi > 0 else None,
                    "top1_pct": _r(w[0] * 100), "top3_pct": _r(math.fsum(w[:3]) * 100)}
        dc = [p["day_change"] for p in valued if p["day_change"] is not None]
        out.append({"currency": cur, "market_value": _r(mv) if valued else None, "cost_valued": _r(cost_v),
                    "cost_total": _r(cost_t),
                    "coverage_pct": _r(cost_v / cost_t * 100.0) if cost_t > 0 else None,
                    "unrealised": _r(mv - cost_v) if valued else None,
                    "day_change": _r(math.fsum(dc)) if dc else None, "concentration": conc,
                    "positions": len(ps), "valued": len(valued)})
    return out


def _reasons(positions, not_used, ys, rejected) -> List[str]:
    out = []
    n = sum(1 for p in positions if p["price"] is None and p["cost"] is not None
            and p["price_source"] != "market_other_currency")
    if n:
        out.append(f"{n} position(s) without a price source")
    oc = sum(1 for p in positions if p["price_source"] == "market_other_currency")
    if oc:
        out.append(f"{oc} position(s) valued only in another currency (no P&L)")
    mm = [p for p in positions if p["broker_qty"] is not None and p["cost"] is not None]
    if mm:
        out.append(f"{len(mm)} position(s) where the broker's quantity differs from the imported lots: "
                   + ", ".join(f"{p['key']} (broker {p['broker_qty']:g}, lots {p['qty']:g})" for p in mm[:10]))
    nolots = [p for p in positions if p["cost"] is None]
    if nolots:
        out.append(f"{len(nolots)} broker position(s) without imported lots: "
                   + ", ".join(f"{p['key']} ({p['broker_qty']:g})" for p in nolots[:10]))
    if not_used:
        out.append(f"{len(not_used)} sale(s) without an earlier imported purchase (not used)")
    un = sum(v["realised"]["unclassified"]["parts"] for y in ys.values() for v in y.c.values())
    if un:
        out.append(f"{un} realised part(s) of unclassified instruments (classify them in Shadow Account)")
    if rejected:
        out.append(f"{sum(r['rejected'] for r in rejected)} imported row(s) were rejected at import")
    return out


def _years(year: Optional[int]) -> Tuple[int, int]:
    y = int(year) if year is not None else _today().year
    return y, y - 1


def portfolio(year: Optional[int] = None, account: Optional[str] = None) -> dict:
    y, py = _years(year)
    today = _today()
    rows = shadow.account_ids()
    if account is not None:
        rows = [r for r in rows if r["id"] == account]
        if not rows:
            raise TradingError(404, "not_found", shadow.NOT_FOUND)
    maps = symbols.mappings()
    accs = [_account(r, (y, py), today, maps) for r in rows]
    tot = {yy: _Year() for yy in (y, py)}
    all_pos: List[dict] = []
    for a in accs:
        for yy in (y, py):
            tot[yy].merge(a["_years"][yy])
        all_pos.extend(dict(p) for p in a["positions"])
    for a in accs:
        a.pop("_years")
    has_tx = any(a["state"] != "no_imports" for a in accs)
    reasons = [f"{a['label']}: {r}" for a in accs for r in a["reasons"]]
    state = "no_imports" if not has_tx else ("incomplete" if reasons else "ok")
    if state == "no_imports":
        reasons = ["No depot imported yet — import a broker statement in Hedge Fund → Shadow Account."]
    ty, typ = tot[y].out(), tot[py].out()
    return {"state": state, "reasons": reasons, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "year": y, "previous_year": py, "label": LABEL, "tax_notice": tax_notice.NOTICE, "privacy": PRIVACY,
            "limits": list(LIMITS), "buckets": [{"key": b, "label": BUCKET_LABELS[b]} for b in BUCKETS],
            "accounts": accs,
            "totals": {"years": {str(y): ty, str(py): typ}, "allocation": allocation_of(all_pos)},
            "yoy": _yoy(ty, typ)}


def _yoy(cur: dict, prev: dict) -> List[dict]:
    a = {c["currency"]: c for c in cur["currencies"]}
    b = {c["currency"]: c for c in prev["currencies"]}
    out = []
    for c in sorted(set(a) | set(b)):
        row = {"currency": c}
        for k in ("realised_net", "dividends", "withholding", "interest", "fees"):
            def g(x):
                if x is None:
                    return 0.0
                return x["realised"]["net"] if k == "realised_net" else x[k]
            cv, pv = g(a.get(c)), g(b.get(c))
            row[k] = {"current": _r(cv), "previous": _r(pv), "delta": _r(cv - pv)}
        out.append(row)
    return out


def _fmt(v: float, cur: str) -> str:
    sign = "+" if v > 0 else ("−" if v < 0 else "±")
    return f"{sign}{abs(v):,.2f} {cur}"


def summary() -> dict:
    d = portfolio()
    y, py = d["year"], d["previous_year"]
    cur = {c["currency"]: c for c in d["totals"]["years"][str(y)]["currencies"]}
    prev = {c["currency"]: c for c in d["totals"]["years"][str(py)]["currencies"]}
    alloc = {a["currency"]: a for a in d["totals"]["allocation"]}
    rows = []
    for c in sorted(set(cur) | set(alloc)):
        v, p, al = cur.get(c), prev.get(c), alloc.get(c) or {}
        rows.append({"currency": c, "realised_net": v["realised"]["net"] if v else 0.0,
                     "realised_brokerage": v["realised"]["brokerage"]["net"] if v else 0.0,
                     "realised_other": v["realised"]["other"]["net"] if v else 0.0,
                     "realised_unclassified": v["realised"]["unclassified"]["net"] if v else 0.0,
                     "dividends": v["dividends"] if v else 0.0, "withholding": v["withholding"] if v else 0.0,
                     "interest": v["interest"] if v else 0.0, "tax_refunds": v["tax_refunds"] if v else 0.0,
                     "fees": v["fees"] if v else 0.0, "taxes_on_sales": v["taxes_on_sales"] if v else 0.0,
                     "unrealised": al.get("unrealised"), "positions": al.get("positions", 0),
                     "valued": al.get("valued", 0),
                     "market_value": al.get("market_value"), "coverage_pct": al.get("coverage_pct"),
                     "previous": {"realised_net": p["realised"]["net"] if p else 0.0,
                                  "dividends": p["dividends"] if p else 0.0, "fees": p["fees"] if p else 0.0}})
    n_acc = sum(1 for a in d["accounts"] if a["state"] != "no_imports")
    vals = [p for a in d["accounts"] for p in a["positions"]]
    # same definition as _reasons(): no price, has lots, not "valued only in another currency" (live test 2026-10-19:
    # the card derived positions - valued and miscounted other-currency positions as unpriced)
    unpriced = sum(1 for p in vals if p["price"] is None and p["cost"] is not None
                   and p["price_source"] != "market_other_currency")
    as_of = {"valuation": max((p["as_of"] for p in vals if p["price_source"] == "valuation"), default=None),
             "market": max((p["as_of"] for p in vals if p["price_source"] == "market"), default=None)}
    if d["state"] == "no_imports":
        headline, detail = "No depot imported yet", "Import a broker statement in Hedge Fund → Shadow Account"
    else:
        real = [r for r in rows if r["realised_net"]]
        headline = f"Realised {y}: " + (" · ".join(_fmt(r["realised_net"], r["currency"]) for r in real)
                                        if real else "nothing realised yet")
        parts = []
        for r in rows:
            if not r["positions"]:
                continue
            if r["valued"] and r["unrealised"] is not None and r["coverage_pct"] is not None:
                parts.append(f"Unrealised {_fmt(r['unrealised'], r['currency'])} (coverage {r['coverage_pct']:.0f} %)")
            else:
                parts.append(f"{r['currency']}: nothing valued ({r['positions']} position(s) without a price source)")
        parts.append(f"{n_acc} depot{'s' if n_acc != 1 else ''}")
        detail = " · ".join(parts)
    return {"state": d["state"], "reasons": d["reasons"], "generated_at": d["generated_at"], "year": y,
            "previous_year": py, "accounts": n_acc, "label": LABEL, "tax_notice": tax_notice.NOTICE,
            "currencies": rows, "headline": headline, "detail": detail, "as_of": as_of, "unpriced": unpriced}


_ = db
