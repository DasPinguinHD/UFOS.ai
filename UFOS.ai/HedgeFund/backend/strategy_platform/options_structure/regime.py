"""Regime service (SP §21.12, task §6): after each archived chain slot, per symbol, the OS2 computations (quality →
own IV / greeks → GEX × 3 dealer variants → surface → regime), stored point-in-time in ``options_structure.db``.

* Inputs are read-only (the archive DB ``mode=ro``; the platform's cached daily bars via ``storage.db.query_bars``
  with ``known_by`` = the slot — no fetch; the Newsroom FOMC schedule / calendar snapshot). The job makes NO network
  calls of its own.
* Point-in-time: ``valid_for`` = the slot; ``available_at`` = the max ``available_at`` of every input used (chain
  rows, OI rows, the rate and VIX observations, the newest bar's ``known_at``); OI / FRED / bars are selected with
  ``available_at ≤ the slot``. A part whose inputs fail the quality rules is stored ``unavailable`` with the reasons —
  nothing is filled.
* Idempotent and resumable: a (run, symbol) is done when its ``regime`` row exists for the current model version and
  assumptions hash; all four parts are written in one transaction; a crash leaves nothing → redone next tick. A
  computation error is logged and retried on the next ticks (at most ``MAX_ATTEMPTS`` per process), never stored as a
  value. New assumptions (a changed config) → a new hash → everything is recomputed under it (old rows stay).
* PROVISIONAL: every row carries ``provisional`` while the archive config or an OS2 key is provisional.
* No H1 / H2 statistics are computed here (frozen pre-registration, SP §21.8) — no regressions, no hit rates.

Regime fields: ``gex_sign`` (total GEX under A, ``all`` bucket), ``signs`` / ``agree`` (A–C), ``flip`` (A) and the
spot's distance to it and to the call / put walls in %, ``em_position`` = (spot − previous close) / today's expected
move (straddle; IV-based if no straddle) — inside the move when |x| ≤ 1, ``vix_label``, ``vrp_sign``, ``next_event``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from strategy_platform.core import calendars

from . import bs, calendar as ocal, gex as ogex, model, quotes, store, surface
from .config import MODEL_VERSION, Config, assumptions, assumptions_hash, provisional

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
TICK_SECONDS = 60
VIX_SERIES = ("VIXCLS", "VXVCLS", "VXNCLS")

state = {"running": False, "last_tick_at": None, "last_error": None, "processed": 0, "failed": 0,
         "pending": None, "paused_reason": None}
_attempts: Dict[Tuple[int, str, str], int] = {}


def default_bars(symbol: str, as_of_ms: int) -> List[Tuple[date, float, int]]:
    """(NY session date, close, known_at ms) of the cached daily bars known by ``as_of`` — local store only."""
    from strategy_platform.storage import db as sdb
    from strategy_platform.data import registry
    src = None
    try:
        row = sdb.data_source_get("equity_bars")
        src = row.get("source") if row else None
    except Exception:            # noqa: BLE001
        src = None
    src = src or registry.DEFAULT_SOURCES["equity_bars"]
    as_of = datetime.fromtimestamp(as_of_ms / 1000, timezone.utc)
    rows = sdb.query_bars(symbol, "1Day", as_of - timedelta(days=60), as_of, src, known_by=as_of)
    out = []
    for r in rows:
        ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
        kn = datetime.fromisoformat(str(r["known_at"]).replace("Z", "+00:00"))
        out.append((ts.astimezone(calendars.NY).date(), float(r["close"]), int(kn.timestamp() * 1000)))
    return out


class Context:
    """Everything a computation needs besides the archive connection (injectable for tests)."""

    def __init__(self, cfg: Config, acfg, bars_fn: Callable = default_bars, fomc=None, cpi=None,
                 clock_ms: Callable[[], int] = store.now_ms, confirmed: Optional[dict] = None):
        self.cfg, self.acfg, self.bars_fn, self.clock_ms = cfg, acfg, bars_fn, clock_ms
        self.fomc = fomc if fomc is not None else ocal.load_fomc()
        self.cpi = cpi if cpi is not None else ocal.load_cpi()
        self.ahash = assumptions_hash(cfg, acfg)
        self.ajson = json.dumps(assumptions(cfg, acfg), sort_keys=True, separators=(",", ":"))
        self.provisional, self.provisional_keys = provisional(cfg, acfg, confirmed)


def _rate(aconn, ctx: Context, slot_ms: int, slot_d: date) -> Tuple[Optional[float], Optional[dict], Optional[str]]:
    series = list(ctx.acfg.rate_series)
    if not series:
        return None, None, "no rate series configured in the archive (rate_series empty)"
    obs = quotes.fred_latest(aconn, series[0], slot_ms)
    if obs is None:
        return None, None, f"no {series[0]} observation archived by the slot (risk-free rate)"
    age = (slot_d - date.fromisoformat(obs["date"])).days
    if age > ctx.cfg.rate_max_age_days:
        return None, obs, f"{series[0]} of {obs['date']} is {age} days old (> {ctx.cfg.rate_max_age_days})"
    try:
        return bs.rate_from_discount_yield(float(obs["value"]), ctx.cfg.rate_tenor_days), obs, None
    except ValueError as ex:
        return None, obs, f"{series[0]}: {ex}"


def _rr_ref(sv: dict, target: int) -> Optional[dict]:
    c = [v for v in sv["expiries"] if v["available"] and v.get("rr25") is not None]
    if not c:
        return None
    v = min(c, key=lambda x: (abs(x["days"] - target), x["days"]))
    return {"expiration": v["expiration"], "days": v["days"], "rr25": v["rr25"]}


def _rr_percentile(sconn, symbol: str, slot_ms: int, ctx: Context, today: Optional[dict]) -> dict:
    out = {"value": None, "n": 0, "reason": None, "basis": "share of earlier archived slots (available by this slot) "
           f"with a 25Δ RR ≤ today's, expiry nearest {ctx.cfg.vrp_target_days} d"}
    if today is None:
        out["reason"] = "no 25Δ RR today"
        return out
    vals = []
    for r in sconn.execute("SELECT payload FROM os2_results WHERE symbol = ? AND part = 'surface' AND model_version = ?"
                           " AND assumptions_hash = ? AND valid_for_ms < ? AND available_ms <= ? AND status != "
                           "'unavailable'", (symbol, MODEL_VERSION, ctx.ahash, slot_ms, slot_ms)):
        ref = json.loads(r["payload"]).get("rr_ref")
        if ref and ref.get("rr25") is not None:
            vals.append(float(ref["rr25"]))
    out["n"] = len(vals)
    if len(vals) < ctx.cfg.rr_history_min:
        out["reason"] = f"only {len(vals)} earlier slots archived (≥ {ctx.cfg.rr_history_min} needed)"
        return out
    out["value"] = 100.0 * sum(1 for v in vals if v <= today["rr25"]) / len(vals)
    return out


def _q_view(pc: model.PricedChain, fixed: float) -> dict:
    """The implied dividend yield per expiry (used) next to the fixed per-symbol assumption (cross-check only)."""
    return {"method": "put-call parity per expiry: median over near-ATM pairs with both mids usable (American "
                      "exercise: approximate)",
            "by_expiry": {e.isoformat(): v for e, v in sorted(pc.q_by_expiry.items())},
            # flagged + shown, still used (SP §21.12): |q| or the pair dispersion above the PROVISIONAL thresholds
            "flagged_expiries": {e.isoformat(): v["flags"] for e, v in sorted(pc.q_by_expiry.items())
                                 if v.get("flags")},
            "fixed_cross_check": fixed}


def compute(aconn, sconn, run: dict, symbol: str, ctx: Context) -> List[dict]:
    """The four parts of one (archive run, symbol) — rows ready for ``store.write``."""
    cfg, acfg = ctx.cfg, ctx.acfg
    slot_ms = int(run["slot_ms"])
    ch = quotes.load_chain(aconn, run, symbol, acfg, cfg)
    slot_d = ch.slot_date
    computed = ctx.clock_ms()
    base_av = ch.available_ms or int(run.get("finished_ms") or slot_ms)
    rows: Dict[str, dict] = {}

    def row(part, status, payload, reasons, available, sources):
        rows[part] = {"run_id": ch.run_id, "symbol": symbol, "part": part, "model_version": MODEL_VERSION,
                      "status": status, "valid_for_ms": slot_ms, "available_ms": int(available),
                      "computed_ms": computed, "provisional": ctx.provisional, "reasons": [str(x)[:600] for x in reasons],
                      "payload": payload, "source_runs": sorted(set(sources))}

    rate, rate_obs, rate_why = _rate(aconn, ctx, slot_ms, slot_d)
    q_fixed = cfg.dividend_yield[symbol]              # cross-check only (the model uses the implied q per expiry)
    fred = {s: quotes.fred_latest(aconn, s, slot_ms) for s in VIX_SERIES}
    meta = {"assumptions_hash": ctx.ahash, "provisional": ctx.provisional, "provisional_keys": ctx.provisional_keys,
            "spot": ch.spot, "spot_t_ms": ch.spot_t_ms, "run_kind": ch.run_kind, "run_status": ch.run_status,
            "slot_label": run.get("slot_label"), "rate": {"value": rate, "observation": rate_obs, "reason": rate_why},
            "dividend_yield": {"method": "implied per expiry (put-call parity)", "fixed_cross_check": q_fixed},
            "oi": quotes.oi_summary(ch, cfg.oi_max_age_sessions)}
    pc = None
    reasons_common = list(ch.reasons)
    if rate is None:
        reasons_common.append(f"risk-free rate unavailable: {rate_why}")
    elif ch.quotes and ch.spot is not None:
        pc = model.price_chain(ch, rate, cfg, acfg.coverage_min_pct)
        meta["dividend_yield"] = _q_view(pc, q_fixed)
    sources = [ch.run_id]
    rate_av = int(rate_obs["available_ms"]) if rate_obs else 0
    if rate_obs and rate_obs.get("run_id"):
        sources.append(int(rate_obs["run_id"]))
    # --- quality (always stored) --------------------------------------------------------------------------------
    oi_reasons: Dict[str, int] = {}
    for qq in ch.quotes:
        if qq.oi_reason:
            key = qq.oi_reason.split(" (")[0]
            oi_reasons[key] = oi_reasons.get(key, 0) + 1
    qual = {**meta, "flags": quotes.flag_counts(ch), "contracts": len(ch.quotes),
            "expiries": list(pc.expiries.values()) if pc else [],
            "vendor_iv": model.vendor_iv_stats(pc) if pc else {"n": 0},
            "q_flagged_expiries": [e["expiration"] for e in pc.expiries.values() if e.get("q_flags")] if pc else [],
            "oi": {**quotes.oi_summary(ch, cfg.oi_max_age_sessions), "missing": oi_reasons},
            "archive_gaps": ch.archive_gaps}
    row("quality", "ok" if ch.quotes else "unavailable", qual, reasons_common, max(base_av, rate_av), sources)
    # --- GEX ------------------------------------------------------------------------------------------------------
    if pc is None:
        g = {"available": False, "reasons": list(reasons_common) or ["no priced chain"]}
    else:
        g = ogex.compute(pc, cfg)
    oi_av = max([x.oi_available_ms for x in ch.quotes if x.oi_available_ms] or [0])
    oi_runs = {x.oi_run_id for x in ch.quotes if x.oi_run_id}
    gex_av = max(base_av, rate_av, oi_av)
    row("gex", (g.get("status") or "ok") if g.get("available") else "unavailable", {**meta, **g}, g.get("reasons") or [], gex_av,
        sources + list(oi_runs))
    # --- surface ----------------------------------------------------------------------------------------------
    fred_av = max([int(v["available_ms"]) for v in fred.values() if v] or [0])
    fred_runs = [int(v["run_id"]) for v in fred.values() if v and v.get("run_id")]
    closes, bars_av, bars_why = [], 0, None
    try:
        raw = ctx.bars_fn(symbol, slot_ms) or []
        closes = [(d, c) for d, c, _k in raw]
        bars_av = max([k for _d, _c, k in raw] or [0])
    except Exception as ex:      # noqa: BLE001 — bar store unavailable: said
        bars_why = f"cached daily bars not readable ({type(ex).__name__})"
    try:
        prev = calendars.previous_trading_day(slot_d)
    except Exception:            # noqa: BLE001
        prev = None
    if pc is None:
        sv = {"available": False, "reasons": list(reasons_common) or ["no priced chain"]}
        sv_status = "unavailable"
    else:
        sv = surface.compute(pc, cfg, fred, closes, prev)
        if bars_why:
            sv["reasons"].append(bars_why)
        sv["rr_ref"] = _rr_ref(sv, cfg.vrp_target_days)
        sv["rr_percentile"] = _rr_percentile(sconn, symbol, slot_ms, ctx, sv["rr_ref"])
        any_ok = any(v["available"] for v in sv["expiries"])
        sv_status = ("ok" if not sv["reasons"] else "partial") if any_ok else "unavailable"
        if not any_ok:
            sv["reasons"].append("no expiry with enough usable quotes (coverage)")
    sv_av = max(base_av, rate_av, fred_av, bars_av)
    row("surface", sv_status, {**meta, **sv}, sv.get("reasons") or [], sv_av, sources + fred_runs)
    # --- regime ---------------------------------------------------------------------------------------------------
    cal = ocal.features(slot_ms, ctx.fomc, ctx.cpi)
    reg = {**meta, "gex_sign": None, "signs": None, "agree": None, "gex_total": None, "gex_status": None,
           "gex_excluded": None, "gex_included_expiries": None, "gex_evaluable": None, "flip": None,
           "flip_distance_pct": None, "call_wall": None, "put_wall": None, "call_wall_distance_pct": None,
           "put_wall_distance_pct": None, "em_position": None, "em_basis": None, "vix_label": None, "vrp_sign": None,
           "next_event": cal.get("next_event"), "calendar": cal, "sessions_note": "no H1 / H2 statistics (SP §21.8)"}
    reasons = []
    S = ch.spot
    if g.get("available"):
        gt = g["total"]                       # sum over the quote-qualified expiries (user decision 2026-11-06)
        tot = gt["value"]
        reg["gex_total"] = tot
        reg["gex_status"] = gt["status"]
        reg["gex_included_expiries"] = gt["included"]
        reg["gex_excluded"] = {"text": gt["excluded_text"], "expiries": gt["excluded"],
                               "gamma_share_pct": gt["excluded_gamma_share_pct"],
                               # the evaluability basis (user 2026-11-06 b) — shown also when the gamma share is None
                               "oi_share_pct": (gt["excluded_oi"] or {}).get("share_of_total_oi_pct"),
                               "oi": gt["excluded_oi"]}
        ev = gt["evaluable"]
        reg["gex_evaluable"] = ev
        if not ev["ok"]:
            reasons.append(ev["reason"])      # "GEX partial: excluded OI x % > 15 %" → regime unavailable below
        elif gt["status"] == "partial":
            reasons.append(f"GEX partial: {gt['excluded_text']}")
    if g.get("available") and not reg["gex_evaluable"]["ok"]:
        pass                                  # not evaluable: no regime sign / flip / walls from this decision time
    elif g.get("available"):
        reg["gex_sign"] = ogex.sign_of(tot["A"])
        reg["signs"], reg["agree"] = g.get("signs"), g.get("agree")
        fl = (g.get("flip") or {}).get("A") or {}
        reg["flip"] = fl.get("level")
        reg["flip_distance_pct"] = fl.get("distance_pct")
        if fl.get("reason"):
            reasons.append(f"flip: {fl['reason']}")
        w = g.get("walls") or {}
        for k in ("call_wall", "put_wall"):
            reg[k] = w.get(k)
            if w.get(k) and S:
                reg[f"{k}_distance_pct"] = 100.0 * (w[k] / S - 1.0)
    else:
        reasons.extend(g.get("reasons") or ["GEX unavailable"])
    if pc is not None:
        em = (sv.get("expected_move") or {}).get("today")
        last_close = closes[-1] if closes else None
        if em and S and last_close and prev and last_close[0] == prev:
            move = em.get("straddle") if em.get("straddle") else em.get("iv_based")
            if move:
                reg["em_position"] = (S - last_close[1]) / move
                reg["em_basis"] = {"expiration": em["expiration"], "move": move, "previous_close": last_close[1],
                                   "kind": "straddle" if em.get("straddle") else "iv_based"}
        if reg["em_position"] is None:
            reasons.append("position in the expected move unavailable (no expected move or no previous close)")
        vx = sv.get("vix_structure") or {}
        reg["vix_label"] = vx.get("label")
        if vx.get("reason"):
            reasons.append(f"VIX structure: {vx['reason']}")
        reg["vrp_sign"] = (sv.get("vrp") or {}).get("sign")
        if reg["vrp_sign"] is None:
            reasons.append("VRP unavailable")
    reasons.extend(cal.get("reasons") or [])
    status = "unavailable" if reg["gex_sign"] is None else ("ok" if not reasons else "partial")
    row("regime", status, reg, reasons, max(gex_av, sv_av), sources + list(oi_runs) + fred_runs)
    return [rows[p] for p in store.PARTS]


def pending(aconn, sconn, ctx: Context, symbols) -> List[Tuple[dict, str]]:
    done = {(r["run_id"], r["symbol"]) for r in sconn.execute(
        "SELECT run_id, symbol FROM os2_results WHERE part = 'regime' AND model_version = ? AND assumptions_hash = ?",
        (MODEL_VERSION, ctx.ahash))}
    out = []
    for run in quotes.chain_runs(aconn, 0, 1_000_000):
        for s in symbols:
            if (run["id"], s) not in done:
                out.append((run, s))
    return out


def run_pending(aconn, sconn, ctx: Context, limit: Optional[int] = None) -> dict:
    """Process up to ``limit`` pending (run, symbol) pairs oldest first; returns counts."""
    todo = pending(aconn, sconn, ctx, ctx.acfg.symbols)
    n = ok = failed = skipped = 0
    for run, s in todo:
        if limit is not None and n >= limit:
            break
        key = (run["id"], s, ctx.ahash)
        if _attempts.get(key, 0) >= MAX_ATTEMPTS:
            skipped += 1
            continue
        n += 1
        try:
            rows = compute(aconn, sconn, run, s, ctx)
            store.write(sconn, rows, ctx.ahash, ctx.ajson, ctx.clock_ms)
            ok += 1
        except Exception as ex:      # noqa: BLE001 — logged + retried later, never stored as a value
            _attempts[key] = _attempts.get(key, 0) + 1
            failed += 1
            logger.exception("OS2 computation failed for run %s %s (attempt %s): %s", run["id"], s, _attempts[key],
                             type(ex).__name__)
    return {"processed": ok, "failed": failed, "skipped": skipped, "pending": max(0, len(todo) - ok)}


def tick(data_dir: Optional[str] = None, ctx: Optional[Context] = None) -> dict:
    """One job step: load configs, open the archive read-only and the OS2 store, process a batch."""
    from strategy_platform.options_archive import config as oconfig
    from . import config as scfg
    try:
        acfg = oconfig.load(None if data_dir is None else os.path.join(data_dir, "options_archive_config.json"))
        cfg = scfg.load(data_dir)
        confirmed = scfg.load_confirmed(data_dir, cfg)
    except (ValueError, OSError) as ex:
        state["paused_reason"] = f"config refused: {str(ex)[:200]}"
        return {"paused": state["paused_reason"]}
    state["paused_reason"] = None
    aconn = quotes.open_archive_ro(oconfig.paths(data_dir)["db"])
    if aconn is None:
        state["paused_reason"] = "the options archive has no database yet (not set up)"
        return {"paused": state["paused_reason"]}
    sconn = store.open_rw(store.path(data_dir))
    try:
        ctx = ctx or Context(cfg, acfg, confirmed=confirmed)
        res = run_pending(aconn, sconn, ctx, cfg.job_batch)
    finally:
        aconn.close()
        sconn.close()
    state["processed"] += res["processed"]
    state["failed"] += res["failed"]
    state["pending"] = res["pending"]
    return res


async def run_forever(stop_event: Optional[asyncio.Event] = None, data_dir: Optional[str] = None) -> None:
    """The backend task: one tick at a time, every ``TICK_SECONDS``; never raises into the app."""
    state["running"] = True
    try:
        while stop_event is None or not stop_event.is_set():
            try:
                await asyncio.to_thread(tick, data_dir)
                state["last_error"] = None
            except Exception as ex:      # noqa: BLE001
                state["last_error"] = f"{type(ex).__name__}: {str(ex)[:200]}"
                logger.exception("OS2 job tick failed")
            state["last_tick_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            await asyncio.sleep(TICK_SECONDS)
    finally:
        state["running"] = False
