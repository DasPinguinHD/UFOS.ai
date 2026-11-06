"""OS2 routes (SP §21.12): ``/v2/options-structure/*`` — READ-ONLY views of ``data/options_structure.db`` (opened
``mode=ro``) and of the archive (``mode=ro``), behind the backend's token check like every ``/v2`` route. Every
payload carries ``provisional`` and the assumptions; a part that could not be computed says why. Errors: 400 with the
reason, 404 for an unknown slot — never a stand-in value. No H1 / H2 results (``…/progress`` is a counter only).

``GET /v2/options-structure/state?symbol=SPY&as_of=`` — the newest regime snapshot available at ``as_of`` (default
now) with its age; older than ``state_max_age_minutes`` → ``available: false`` with the reason (no live fallback).
``…/gex?symbol=&slot=``, ``…/surface?symbol=&slot=`` (slot = ``latest`` or the slot time, ISO-8601 with offset or
epoch ms), ``…/history?symbol=&field=&days=``, ``…/calendar?days=&as_of=``, ``…/quality?symbol=&days=``,
``…/progress``.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Optional

from .trading_routes import _bad

SYMBOLS = ("SPY", "QQQ")
HISTORY_FIELDS = ("gex_sign", "agree", "flip", "flip_distance_pct", "call_wall", "put_wall", "call_wall_distance_pct",
                  "put_wall_distance_pct", "em_position", "vix_label", "vrp_sign", "gex_total_A", "gex_total_B",
                  "gex_total_C")
HISTORY_DAYS_MAX = 90
QUALITY_DAYS_MAX = 31
CALENDAR_DAYS_MAX = 400
PROGRESS_TARGET = 120


def _parse_time(v: str) -> Optional[int]:
    """ISO-8601 with an offset (or Z) or epoch ms → epoch ms; None when invalid."""
    s = str(v).strip()
    if s.isdigit():
        n = int(s)
        return n if 946684800000 <= n <= 4102444800000 else None      # 2000 … 2100
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None or not 2000 <= dt.year <= 2100:
        return None
    return int(dt.timestamp() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


class OptionsStructureRoutesMixin:
    """Handlers of the OS2 routes (``data_dir`` / ``now_fn`` shared with the OS1 mixin; tests set them)."""

    def _os2_now_ms(self) -> int:
        now = self.now_fn() if getattr(self, "now_fn", None) else datetime.now(timezone.utc)
        return int(now.timestamp() * 1000)

    def _os2_ctx(self):
        """(cfg, archive cfg, assumptions hash, provisional, provisional keys, assumptions) or raises ValueError."""
        from strategy_platform.options_archive import config as oconfig
        from strategy_platform.options_structure import config as scfg
        dd = getattr(self, "data_dir", None)
        acfg = oconfig.load(os.path.join(dd, "options_archive_config.json") if dd else None)
        cfg = scfg.load(dd)
        prov, keys = scfg.provisional(cfg, acfg, scfg.load_confirmed(dd, cfg))
        return cfg, acfg, scfg.assumptions_hash(cfg, acfg), prov, keys, scfg.assumptions(cfg, acfg)

    def _os2_store(self):
        from strategy_platform.options_structure import store
        return store.open_ro(store.path(getattr(self, "data_dir", None)))

    def _os2_common(self, query: Mapping, allowed, errors: dict) -> Optional[str]:
        for k in query:
            if k not in allowed:
                errors[k] = "unknown parameter"
        sym = query.get("symbol", "SPY")
        if sym not in SYMBOLS:
            errors["symbol"] = f"one of {', '.join(SYMBOLS)}"
            return None
        return sym

    async def _os2_run(self, fn):
        try:
            ctx = self._os2_ctx()
        except (ValueError, OSError) as ex:
            return 409, {"error": f"options structure config: {str(ex)[:200]}", "code": "config_invalid"}
        return await asyncio.to_thread(fn, ctx)

    @staticmethod
    def _os2_meta(ctx) -> dict:
        from strategy_platform.options_structure.config import MODEL_VERSION
        return {"model_version": MODEL_VERSION, "assumptions_hash": ctx[2], "provisional": ctx[3],
                "provisional_keys": ctx[4], "assumptions": ctx[5]}

    # GET /v2/options-structure/state
    async def options_structure_state(self, query: Mapping, body, match: Mapping):
        errors: dict = {}
        sym = self._os2_common(query, ("symbol", "as_of"), errors)
        as_of = self._os2_now_ms()
        if "as_of" in query:
            as_of = _parse_time(query["as_of"])
            if as_of is None:
                errors["as_of"] = "ISO-8601 with an offset or epoch ms (years 2000 … 2100)"
        if errors:
            return _bad(errors)

        def go(ctx):
            from strategy_platform.options_structure import regime, store
            from strategy_platform.options_structure.config import MODEL_VERSION
            out = {"symbol": sym, "as_of": _iso(as_of), **self._os2_meta(ctx),
                   "job": {k: regime.state[k] for k in ("running", "last_tick_at", "pending", "paused_reason",
                                                        "last_error")}}
            conn = self._os2_store()
            if conn is None:
                return 200, {**out, "available": False, "reason": "no OS2 results yet (options_structure.db does not "
                                                                   "exist — the job has not run)"}
            try:
                row = store.latest(conn, sym, "regime", as_of, MODEL_VERSION, ctx[2])
            finally:
                conn.close()
            if row is None:
                return 200, {**out, "available": False,
                             "reason": "no regime snapshot available at this time under the current assumptions"}
            age_min = (as_of - row["valid_for_ms"]) / 60000.0
            out.update({"slot": _iso(row["valid_for_ms"]), "available_at": _iso(row["available_ms"]),
                        "age_minutes": round(age_min, 1), "run_id": row["run_id"]})
            if age_min > ctx[0].state_max_age_minutes:
                return 200, {**out, "available": False,
                             "reason": f"the newest archived slot is {age_min:.0f} min old (> "
                                       f"{ctx[0].state_max_age_minutes} min) — no fresh slot"}
            p = row["payload"]
            keep = ("gex_sign", "signs", "agree", "gex_total", "flip", "flip_distance_pct", "call_wall", "put_wall",
                    "call_wall_distance_pct", "put_wall_distance_pct", "em_position", "em_basis", "vix_label",
                    "vrp_sign", "next_event", "spot", "rate", "dividend_yield", "oi", "gex_status", "gex_excluded",
                    "gex_included_expiries", "gex_evaluable")
            return 200, {**out, "available": row["status"] != "unavailable", "status": row["status"],
                         "reason": None if row["status"] != "unavailable" else "; ".join(row["reasons"])[:600],
                         "reasons": row["reasons"], "regime": {k: p.get(k) for k in keep}}
        return await self._os2_run(go)

    def _slot_part(self, query: Mapping, part: str):
        errors: dict = {}
        sym = self._os2_common(query, ("symbol", "slot"), errors)
        slot = query.get("slot", "latest")
        slot_ms = None
        if slot != "latest":
            slot_ms = _parse_time(slot)
            if slot_ms is None:
                errors["slot"] = "'latest', ISO-8601 with an offset or epoch ms"
        return sym, slot, slot_ms, errors

    async def _os2_part(self, query: Mapping, part: str):
        sym, slot, slot_ms, errors = self._slot_part(query, part)
        if errors:
            return _bad(errors)
        now = self._os2_now_ms()

        def go(ctx):
            from strategy_platform.options_structure import store
            from strategy_platform.options_structure.config import MODEL_VERSION
            conn = self._os2_store()
            row = None
            if conn is not None:
                try:
                    row = (store.latest(conn, sym, part, now, MODEL_VERSION, ctx[2]) if slot_ms is None else
                           store.at_slot(conn, sym, part, slot_ms, MODEL_VERSION, ctx[2]))
                finally:
                    conn.close()
            if row is None or row["available_ms"] > now:
                return 404, {"error": f"no {part} result for {sym} at slot {slot} (unknown slot, not computed yet, or "
                                      "not available yet)", "code": "unknown_slot"}
            return 200, {"symbol": sym, "slot": _iso(row["valid_for_ms"]), "available_at": _iso(row["available_ms"]),
                         "computed_at": _iso(row["computed_ms"]), "status": row["status"], "reasons": row["reasons"],
                         "run_id": row["run_id"], "source_runs": row["source_runs"], **self._os2_meta(ctx),
                         "provisional": row["provisional"] or ctx[3], part: row["payload"]}
        return await self._os2_run(go)

    # GET /v2/options-structure/gex
    async def options_structure_gex(self, query: Mapping, body, match: Mapping):
        return await self._os2_part(query, "gex")

    # GET /v2/options-structure/surface
    async def options_structure_surface(self, query: Mapping, body, match: Mapping):
        return await self._os2_part(query, "surface")

    # GET /v2/options-structure/history
    async def options_structure_history(self, query: Mapping, body, match: Mapping):
        errors: dict = {}
        sym = self._os2_common(query, ("symbol", "field", "days"), errors)
        field = query.get("field")
        if field not in HISTORY_FIELDS:
            errors["field"] = f"one of {', '.join(HISTORY_FIELDS)}"
        days = 30
        if "days" in query:
            try:
                days = int(query["days"])
                if not 1 <= days <= HISTORY_DAYS_MAX:
                    raise ValueError
            except ValueError:
                errors["days"] = f"1 … {HISTORY_DAYS_MAX}"
        if errors:
            return _bad(errors)
        now = self._os2_now_ms()

        def go(ctx):
            from strategy_platform.options_structure import store
            from strategy_platform.options_structure.config import MODEL_VERSION
            conn = self._os2_store()
            rows = []
            if conn is not None:
                try:
                    rows = store.history(conn, sym, "regime", now - days * 86400000, now, MODEL_VERSION, ctx[2])
                finally:
                    conn.close()
            pts = []
            for r in rows:
                p = r["payload"]
                if field.startswith("gex_total_"):
                    v = (p.get("gex_total") or {}).get(field[-1])
                else:
                    v = p.get(field)
                pts.append({"slot": _iso(r["valid_for_ms"]), "available_at": _iso(r["available_ms"]),
                            "status": r["status"], "value": v})
            return 200, {"symbol": sym, "field": field, "days": days, "points": pts, **self._os2_meta(ctx),
                         "note": "the history covers the CURRENT OS2 assumptions only — it restarts after a change of "
                                 "the OS2 configuration (a new assumptions hash; earlier rows are kept, not shown)"}
        return await self._os2_run(go)

    # GET /v2/options-structure/calendar
    async def options_structure_calendar(self, query: Mapping, body, match: Mapping):
        errors = {k: "unknown parameter" for k in query if k not in ("days", "as_of")}
        days = 90
        if "days" in query:
            try:
                days = int(query["days"])
                if not 1 <= days <= CALENDAR_DAYS_MAX:
                    raise ValueError
            except ValueError:
                errors["days"] = f"1 … {CALENDAR_DAYS_MAX}"
        as_of = self._os2_now_ms()
        if "as_of" in query:
            as_of = _parse_time(query["as_of"])
            if as_of is None:
                errors["as_of"] = "ISO-8601 with an offset or epoch ms (years 2000 … 2100)"
        if errors:
            return _bad(errors)

        def go():
            from strategy_platform.core import calendars
            from strategy_platform.options_structure import calendar as ocal
            d = datetime.fromtimestamp(as_of / 1000, timezone.utc).astimezone(calendars.NY).date()
            try:
                exps = ocal.expirations(d, d + timedelta(days=days))
            except Exception as ex:      # noqa: BLE001 — calendar end: said, never guessed
                return 200, {"as_of": _iso(as_of), "expirations": None, "reason": str(ex)[:200],
                             "features": ocal.features(as_of)}
            fomc, ferr = ocal.load_fomc()
            return 200, {"as_of": _iso(as_of), "days": days, "expirations": exps,
                         "fomc": [x.isoformat() for x in fomc if d <= x <= d + timedelta(days=days)],
                         "fomc_reason": ferr, "features": ocal.features(as_of, (fomc, ferr)),
                         "rules": {"monthly": "3rd Friday; holiday → the trading day before",
                                   "quarterly": "the monthly expirations of Mar / Jun / Sep / Dec",
                                   "vix": "30 days before the next month's SPX monthly expiration (holiday rule as "
                                          "Cboe); not a trading day → the trading day before"}}
        return await asyncio.to_thread(go)

    # GET /v2/options-structure/quality
    async def options_structure_quality(self, query: Mapping, body, match: Mapping):
        errors: dict = {}
        sym = self._os2_common(query, ("symbol", "days"), errors)
        days = 5
        if "days" in query:
            try:
                days = int(query["days"])
                if not 1 <= days <= QUALITY_DAYS_MAX:
                    raise ValueError
            except ValueError:
                errors["days"] = f"1 … {QUALITY_DAYS_MAX}"
        if errors:
            return _bad(errors)
        now = self._os2_now_ms()

        def go(ctx):
            from strategy_platform.options_structure import store
            from strategy_platform.options_structure.config import MODEL_VERSION
            conn = self._os2_store()
            rows = []
            if conn is not None:
                try:
                    rows = store.history(conn, sym, "quality", now - days * 86400000, now, MODEL_VERSION, ctx[2])
                finally:
                    conn.close()
            slots = []
            agg = {"slots": len(rows), "slots_with_gex_coverage_gap": 0, "slots_with_iv_coverage_gap": 0}
            for r in rows:
                p = r["payload"]
                ex = p.get("expiries") or []
                agg["slots_with_gex_coverage_gap"] += 1 if (not ex or any(not e.get("available_gex") for e in ex)) else 0
                agg["slots_with_iv_coverage_gap"] += 1 if (not ex or any(not e.get("available_iv") for e in ex)) else 0
                slots.append({"slot": _iso(r["valid_for_ms"]), "run_id": r["run_id"], "run_status": p.get("run_status"),
                              "slot_label": p.get("slot_label"), "contracts": p.get("contracts"),
                              "flags": p.get("flags"), "vendor_iv": p.get("vendor_iv"), "oi": p.get("oi"),
                              "rate": p.get("rate"), "parity_q": p.get("parity_q"),
                              "expiries": [{k: e.get(k) for k in ("expiration", "days", "contracts", "coverage_iv_pct",
                                                                  "coverage_gex_pct", "available_iv", "available_gex")}
                                           for e in ex],
                              "archive_gaps": p.get("archive_gaps"), "reasons": r["reasons"]})
            return 200, {"symbol": sym, "days": days, "summary": agg, "slots": slots, **self._os2_meta(ctx)}
        return await self._os2_run(go)

    # GET /v2/options-structure/progress
    async def options_structure_progress(self, query: Mapping, body, match: Mapping):
        if set(query):
            return _bad({k: "unknown parameter" for k in query})

        def go(ctx):
            from strategy_platform.core import calendars
            from strategy_platform.options_archive import config as oconfig
            from strategy_platform.options_structure import quotes
            conn = quotes.open_archive_ro(oconfig.paths(getattr(self, "data_dir", None))["db"])
            if conn is None:
                return 200, {"target": PROGRESS_TARGET, "sessions_archived": 0, "sessions_eligible": 0,
                             "note": "the archive has no database yet", **self._os2_meta(ctx)}
            try:
                rows = conn.execute("SELECT slot_ms, slot_label, status FROM os_runs WHERE kind = 'intraday' AND "
                                    "status IN ('ok', 'partial')").fetchall()
            finally:
                conn.close()
            by_day: dict = {}
            for r in rows:
                d = datetime.fromtimestamp(r["slot_ms"] / 1000, timezone.utc).astimezone(calendars.NY).date()
                by_day.setdefault(d, set()).add(r["slot_label"])
            fixed = tuple(ctx[1].fixed_slots)
            early = sorted(d for d in by_day if calendars.is_early_close(d))
            eligible = [d for d in by_day if d not in early and all(f in by_day[d] for f in fixed)]
            return 200, {"target": PROGRESS_TARGET, "sessions_archived": len(by_day),
                         "sessions_eligible": len(eligible), "early_close_excluded": len(early),
                         "first_session": min(by_day).isoformat() if by_day else None,
                         "eligible_rule": f"regular sessions whose fixed slots {', '.join(fixed)} were collected on "
                                          "time (status ok / partial, not late or failed)",
                         "note": "a progress counter only — no H1 / H2 results before the evaluation date "
                                 "(frozen pre-registration, SP §21.8)", **self._os2_meta(ctx)}
        return await self._os2_run(go)


OPTIONS_STRUCTURE_ROUTES = (
    ("GET", "/v2/options-structure/state", "options_structure_state", False),
    ("GET", "/v2/options-structure/gex", "options_structure_gex", False),
    ("GET", "/v2/options-structure/surface", "options_structure_surface", False),
    ("GET", "/v2/options-structure/history", "options_structure_history", False),
    ("GET", "/v2/options-structure/calendar", "options_structure_calendar", False),
    ("GET", "/v2/options-structure/quality", "options_structure_quality", False),
    ("GET", "/v2/options-structure/progress", "options_structure_progress", False),
)
