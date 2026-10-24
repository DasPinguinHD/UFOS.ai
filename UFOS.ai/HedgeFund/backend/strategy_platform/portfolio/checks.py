"""Pre-trade checks (SPEC §17.6) run by the OMS on every order before it is stored as
``new`` → ``submitted``. Each check yields ``{check, ok, message}``; the first failure
rejects the order with that message (never skipped silently). Protective orders (kill,
drawdown, margin) only get the trading-hours note — they always queue.

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List, Optional

PROTECTIVE = ("kill", "drawdown", "margin")
REDUCING = ("sell", "cover")


def _res(check: str, ok: bool, message: Optional[str] = None) -> dict:
    return {"check": check, "ok": bool(ok), "message": message}


def increases_risk(side: str) -> bool:
    """buy / short add exposure (a buy that covers part of a short is a ``cover`` in this broker)."""
    return side not in REDUCING


def _money(currency: str, x: float) -> str:
    """"$1,234.50" for USD, "1,234.50 USDT" for a USDT account (never converted, SPEC §18.14)."""
    return f"{x:,.2f} {currency}" if currency and currency != "USD" else f"${x:,.2f}"


def latest_quote_price(account, symbol: str) -> Optional[float]:
    """A fresh read of the latest quote (the market order's collar reference, retest 2026-10-23) — the one quote
    source of the account (``accounts.quote_for``, within the HTTP gate's budget); None when it cannot be read."""
    from ..execution import accounts
    try:
        q = accounts.quote_for(account, symbol)
    except Exception:      # noqa: BLE001 — the check then fails with its reason
        return None
    p = getattr(q, "price", None) if q is not None else None
    try:
        return float(p) if p is not None and float(p) > 0 else None
    except (TypeError, ValueError):
        return None


def run(account, order: dict, *, now: datetime, settings: dict, quote, acct_view, valuation_error: Optional[str],
        working: List[dict], recent: List[dict], orders_today: int, day_loss: dict) -> List[dict]:
    """``order``: {symbol, side, qty, origin, ref_price, allow_reduce, source_id}; ``quote``: the Quote used
    (None = none); ``acct_view``: AccountView or None (``valuation_error`` says why); ``working``: the
    account's non-terminal OMS orders; ``recent``: OMS orders created within the duplicate window;
    ``orders_today``: non-protective orders of this class today; ``day_loss``: risk.day_loss()."""
    from strategy_platform.core import params as fund_params
    sym, side, origin = order["symbol"], order["side"], order["origin"]
    crypto = fund_params.is_crypto(sym)
    cls = "crypto" if crypto else "equity"
    out: List[dict] = []
    if origin in PROTECTIVE:
        can = account.broker._can_fill(now, sym)
        out.append(_res("trading_hours", True, None if can else "market closed — the protective order queues until "
                                                              "the session"))
        return out
    # trading hours
    if not account.broker._can_fill(now, sym):
        out.append(_res("trading_hours", False, f"{sym}: outside trading hours — equities trade in the NYSE regular "
                                                "session (from 5 minutes after the open)"))
        return out
    out.append(_res("trading_hours", True))
    held = float((account.store.get_position(sym) or {}).get("qty") or 0.0)
    adds = increases_risk(side)
    # halt
    h = account.halt()
    if h["active"] and h["kind"] == "reconcile":
        # SPEC §18.5: the mirror book is wrong — even a "closing" order could open a position; only the broker
        # kill switch (built from the BROKER's positions) still trades
        out.append(_res("halt", False, f"the broker account is halted: {h['reason'] or 'reconciliation mismatch'} — "
                                       "no orders until the reconciliation is resolved (Settings → Brokers)"))
        return out
    if h["active"] and adds:
        out.append(_res("halt", False, f"the account is halted ({h['reason'] or h['kind']}) — only closing orders "
                                       "(sell/cover) are accepted until it is resumed"))
        return out
    out.append(_res("halt", True))
    broker_checks = getattr(account, "pre_trade_checks", None)
    if broker_checks is not None:           # stage 8 broker accounts (SPEC §18.4): venue, position side, link
        extra = broker_checks(order, now)
        out.extend(extra)
        if any(not r["ok"] for r in extra):
            return out
    # daily loss
    dl = (day_loss or {}).get(cls) or {}
    from . import risk
    latched_at = risk.latched(account, cls, now)       # review fix M1: the block holds for the whole class day
    if (dl.get("blocked") or latched_at) and adds:
        until = dl.get("until") or risk._next_midnight(cls, now).astimezone(timezone.utc).isoformat(timespec="seconds")
        pnl = dl.get("pnl_pct")
        if latched_at and not (pnl is not None and pnl <= -float(dl.get("limit_pct") or 0.0)):
            why = f"daily loss limit was reached today (at {latched_at}); the block holds"
        else:
            why = f"daily loss limit reached ({pnl:.2f} % ≤ −{float(dl['limit_pct']):g} %)"
        out.append(_res("daily_loss", False, f"{why} — no new {cls} orders until {until}"))
        return out
    out.append(_res("daily_loss", True))
    # duplicate guard
    if any(w["symbol"] == sym for w in working):
        out.append(_res("duplicate", False, f"an order for {sym} is still working — wait for it or cancel it"))
        return out
    if any(r["symbol"] == sym and r["side"] == side and abs(float(r.get("qty") or 0) - float(order.get("qty") or 0))
           < 1e-9 and r.get("source_id") == order.get("source_id") for r in recent):
        out.append(_res("duplicate", False, f"the same order ({side} {order.get('qty')} {sym}) was placed less than "
                                            f"{settings['duplicate_window_seconds']} s ago"))
        return out
    out.append(_res("duplicate", True))
    # daily order count
    if orders_today >= int(settings["max_orders_per_day"]):
        out.append(_res("daily_order_count", False, f"{orders_today} {cls} orders today — the limit is "
                                                    f"{int(settings['max_orders_per_day'])}"))
        return out
    out.append(_res("daily_order_count", True))
    if quote is None:
        err = order.get("quote_error")
        out.append(_res("quote", not adds, None if not adds else
                        f"no current quote for {sym}" + (f" ({err})" if err else "") + " — order not sent"))
        return out
    px = float(quote.price)
    qty = float(order.get("qty") or 0.0)
    if acct_view is None:
        if adds:
            out.append(_res("valuation", False, f"the account cannot be valued ({valuation_error}) — no new orders"))
            return out
        out.append(_res("valuation", True, "closing order without a valuation"))
        return out
    equity = float(acct_view.equity)
    # max order notional
    notional = qty * px
    # SPEC §18.14: the cap of the account's own money (binance-demo: USDT — its own setting, never converted)
    cur = getattr(account, "currency", "USD") or "USD"
    cap_key = "max_order_notional_usdt" if cur == "USDT" else "max_order_notional_usd"
    cap = min(float(settings["max_order_notional_pct"]) / 100.0 * equity, float(settings[cap_key]))
    if adds and notional > cap + 1e-6:
        out.append(_res("max_order_notional", False, f"order notional {_money(cur, notional)} exceeds the limit "
                                                     f"{_money(cur, cap)}"))
        return out
    out.append(_res("max_order_notional", True))
    # max position
    sign = 1.0 if side in ("buy", "cover") else -1.0
    after = held + sign * qty
    lim_pct = float(settings["max_position_pct_crypto" if crypto else "max_position_pct_equity"])
    if abs(after) > abs(held) and abs(after) * px > lim_pct / 100.0 * equity + 1e-6:
        out.append(_res("max_position", False, f"{sym} position after the order {_money(cur, abs(after) * px)} exceeds "
                                               f"{lim_pct:g} % of equity"))
        return out
    out.append(_res("max_position", True))
    # price collar (retest 2026-10-23: never a pass without a reference — no implicit fallback). Reference: the
    # decision's reference price (runtime orders), else a fresh read of the latest quote (manual / protective
    # orders; a resting limit / stop away from the market is legitimate and is not compared). No reference → a
    # risk-increasing order fails; a closing order passes with the reason stated.
    collar = float(settings["price_collar_pct_crypto" if crypto else "price_collar_pct_equity"])
    ref, ref_src = order.get("ref_price"), "the decision's reference price"
    if not ref:
        ref = order["fresh_quote_price"] if "fresh_quote_price" in order else latest_quote_price(account, sym)
        ref_src = "the latest quote"
    try:
        ref = float(ref) if ref is not None else None
    except (TypeError, ValueError):
        ref = None
    if ref is None or not math.isfinite(ref) or ref <= 0:
        if adds:
            out.append(_res("price_collar", False, f"no reference price for {sym} — the latest quote could not be "
                                                   "read, so the price collar cannot be checked"))
            return out
        # a closing / protective order (kill switch, stops) is never held back by missing data — as the quote check
        out.append(_res("price_collar", True, f"closing order: no reference price for {sym} could be read — not "
                                              "blocked (closing orders are never held back by missing data)"))
    else:
        dev = abs(px / ref - 1.0) * 100.0
        if dev > collar:
            out.append(_res("price_collar", False, f"quote {px:,.4f} is {dev:.2f} % away from {ref_src} {ref:,.4f} "
                                                   f"(collar {collar:g} %) — possible bad tick"))
            return out
        out.append(_res("price_collar", True, f"{dev:.2f} % from {ref_src} {ref:,.4f} (collar {collar:g} %)"))
    # short / borrow rules
    p = account.params
    if side == "short":
        if not p.get("allow_shorts", True):
            out.append(_res("short_rules", False, "short selling is disabled for this account"))
            return out
        if sym in (p.get("not_shortable") or []):
            out.append(_res("short_rules", False, f"{sym} is not shortable (no borrow available)"))
            return out
    out.append(_res("short_rules", True))
    # buying power
    if side in ("buy", "short"):
        bp = max(0.0, float(acct_view.buying_power))
        half = fund_params.fees_bps_for(sym, p) / 2.0 / 10000.0
        im = fund_params.margin_pcts_for(sym, p)[0] / 100.0
        if side == "buy":
            fp = px * (1.0 + half)
            need_per = fp * (1.0 + half)
        else:
            fp = px * (1.0 - half)
            need_per = fp * (im + half)
        need = qty * need_per
        min_qty = 1.0 if not crypto else fund_params.CRYPTO_MIN_NOTIONAL / px
        if need > bp + 1e-6 and not (order.get("allow_reduce") and bp >= min_qty * need_per):
            out.append(_res("buying_power", False, f"needs {_money(getattr(account, 'currency', 'USD'), need)} of buying "
                                                   f"power, available {_money(getattr(account, 'currency', 'USD'), bp)}"))
            return out
        out.append(_res("buying_power", True, None if need <= bp + 1e-6 else "reduced to the available buying power"))
    else:
        out.append(_res("buying_power", True))
    return out


def failed(results: List[dict]) -> Optional[dict]:
    for r in results:
        if not r["ok"]:
            return r
    return None
