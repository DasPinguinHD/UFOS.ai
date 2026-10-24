"""One archive run (SP §21.2 / §21.3): contracts + open interest (``oi``), a chain snapshot (``intraday`` / ``eod`` /
``manual``) or the FRED VIX series (``fred``) — point-in-time stamped, staleness and every missing piece recorded as
``os_gaps`` rows, NEVER filled with stand-ins. Everything of a run is written in ONE transaction at its end (the run
row first, its rows after); a crash leaves no half run — the slot then shows as missing.

PIT (§21.3): chain rows — ``quote_t_ms`` = valid_for, ``available_ms`` = ``observed_ms`` = the receive time of the
page; quotes after the slot or older than ``quote_max_age_s`` at the slot are gaps (the raw row is kept for audit,
readers apply the rule); coverage below ``coverage_min_pct`` is a ``coverage`` gap. OI rows — valid_for =
``open_interest_date``; ``available_ms`` = max(observed, T+1 ``oi_floor`` New York) (PROVISIONAL floor), ``early_oi``
when fetched before it; an OI whose valid_for is not the previous session is a ``stale_oi`` gap; a changed value for
the same valid_for is a NEW row (correction). FRED — valid_for = the observation date, ``available_ms`` = observed;
"." = a missing value (gap); a revised value is a new row.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from strategy_platform.core import calendars

from . import db as odb
from .config import VERSION, Config, hm
from .slots import NY, Slot
from .transport import DATA, FRED, TRADING, Http, RateLimited, Refused, Stop

logger = logging.getLogger("options_archive")
KEY_ID, SECRET_KEY, FRED_KEY = "alpaca-paper-key-id", "alpaca-paper-secret-key", "fred-api-key"


def _f(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _i(v) -> Optional[int]:
    x = _f(v)
    return int(x) if x is not None else None


def iso_ms(v) -> Optional[int]:
    """RFC 3339 (incl. nanoseconds) → epoch ms; None when unparsable."""
    import re
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?", str(v or "").strip())
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group(1) + ("." + m.group(2)[:6].ljust(6, "0") if m.group(2) else "")
                                    + ("+00:00" if m.group(3) in (None, "Z") else m.group(3)))
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _date(v) -> Optional[date]:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


class Run:
    """In-memory run: rows and gaps until the one write at the end."""

    def __init__(self, kind: str, slot_ms: int, label: str, started_ms: int, source: str, feed: Optional[str] = None,
                 spot_source: Optional[str] = None):
        self.kind, self.slot_ms, self.label, self.started_ms = kind, slot_ms, label, started_ms
        self.source, self.feed, self.spot_source = source, feed, spot_source
        self.snapshots: List[tuple] = []
        self.contracts: List[tuple] = []
        self.sightings: List[tuple] = []
        self.oi: List[tuple] = []
        self.fred: List[tuple] = []
        self.gaps: List[tuple] = []
        self.data_rows = 0
        self.failed = False
        self.late = False
        self.error: Optional[str] = None
        self.stopped: Optional[str] = None
        self.exit_code = 0
        self.skew_ms: Optional[int] = None

    def gap(self, kind: str, what: str, reason: Optional[str] = None, underlying: Optional[str] = None,
            symbol: Optional[str] = None, now_ms: int = 0) -> None:
        self.gaps.append((self.slot_ms, kind, underlying, symbol, what[:300], (reason or "")[:300] or None, now_ms))


SKEW_GAP_MS = 30_000            # PC clock vs server Date: above → a ``clock_skew`` gap (the run continues)
SKEW_STOP_MS = 300_000          # above → the run stops (exit 3): every PIT stamp would be wrong


class ClockSkew(Stop):
    """This PC's clock is too far from the server's ``Date`` — PIT stamps would be wrong."""


class Collector:
    def __init__(self, conn, cfg: Config, http: Http, *, secrets_module=None,
                 now_ms: Callable[[], int] = odb.now_ms):
        self.conn, self.cfg, self.http, self.now_ms = conn, cfg, http, now_ms
        self._sec = secrets_module
        self._hide: List[str] = []

    # --- keys (Credential Manager only; never logged) -----------------------------------------------------
    def _secrets(self):
        if self._sec is None:
            from strategy_platform import secrets as sec
            self._sec = sec
        return self._sec

    def alpaca_headers(self) -> dict:
        try:
            kid, skey = self._secrets().get_secret(KEY_ID), self._secrets().get_secret(SECRET_KEY)
        except Exception as ex:      # noqa: BLE001
            raise Stop(f"the Credential Manager could not be read ({type(ex).__name__})", code=2) from None
        if not kid or not skey:
            raise Stop("Alpaca paper key ID / secret key not set — enter them in ⚙ Keys & data → Keys", code=2)
        self._hide += [kid, skey]
        return {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": skey, "Accept": "application/json"}

    def fred_key(self) -> Tuple[Optional[str], Optional[str]]:
        try:
            k = self._secrets().get_secret(FRED_KEY)
        except Exception as ex:      # noqa: BLE001 — reported as such, never as "no key"
            return None, f"the FRED key could not be read from the Credential Manager ({type(ex).__name__})"
        if not k:
            return None, "no FRED key in the Credential Manager (fred-api-key)"
        self._hide.append(k)
        return k, None

    def clean(self, text: Optional[str]) -> Optional[str]:
        if text is None:
            return None
        t = " ".join(str(text).split())
        for s in self._hide:
            if s and len(s) >= 4:
                t = t.replace(s, "***")
        return t[:300]

    # --- one run -----------------------------------------------------------------------------------------
    def run(self, slot: Slot, manual: bool = False) -> dict:
        """One run. ``manual``: recorded as kind ``manual`` (label ``manual`` / ``manual:<slot>``) — never counted as a
        slot's run; chain quotes are then checked against the receive time, not a slot."""
        kind = "manual" if manual else slot.kind
        self.http.last_skew_ms = None
        src = {"oi": ("alpaca_trading", None, None), "fred": ("fred", None, None)}.get(
            slot.kind, ("alpaca_data", "indicative", "alpaca_iex_trade"))
        started = self.now_ms()
        r = Run(kind, started if manual else slot.ms, self._label(slot, manual), started, *src)
        before = sum(self.http.count.values())
        retries_before = len(self.http.retries)
        try:
            if slot.kind == "oi":
                self._oi(r, slot)
            elif slot.kind == "fred":
                self._fred(r, slot)
            else:
                self._chain(r, slot, deadline=not manual)
        except RateLimited as ex:
            r.failed, r.error, r.stopped, r.exit_code = True, str(ex), "rate_limited", 4
            r.gap("rate_limited", "HTTP 429 — the run stopped (no retry)", str(ex), now_ms=self.now_ms())
        except Stop as ex:
            r.failed, r.error, r.stopped, r.exit_code = True, self.clean(str(ex)), "stop", ex.code
            gk = "clock_skew" if isinstance(ex, ClockSkew) else ("network" if ex.code == 3 else "keys")
            r.gap(gk, "the run stopped", self.clean(str(ex)), now_ms=self.now_ms())
        except Refused as ex:            # a bug, never a request
            r.failed, r.error, r.stopped, r.exit_code = True, str(ex), "stop", 3
            r.gap("refused", "a request outside the allow-list was refused", str(ex), now_ms=self.now_ms())
        finished = self.now_ms()
        if not manual and slot.kind in ("intraday", "eod") and finished > slot.ms and not r.failed:
            r.late = True
            r.gap("late", f"the snapshot finished {(finished - slot.ms) / 1000:.1f} s after the slot", None,
                  now_ms=finished)
        status = "failed" if (r.failed or r.data_rows == 0) else ("late" if r.late else
                                                                   ("partial" if r.gaps else "ok"))
        if status == "failed" and not r.error:
            r.error = "no data collected"
        requests = sum(self.http.count.values()) - before
        retries = len(self.http.retries) - retries_before
        run_id = self._write(r, status, finished, requests, retries)
        out = {"id": run_id, "kind": kind, "slot_label": r.label, "slot_ms": r.slot_ms, "status": status,
               "requests": requests, "pages": requests - retries, "retries": retries, "gaps": len(r.gaps),
               "rows": r.data_rows, "error": self.clean(r.error), "stopped": r.stopped,
               "exit_code": r.exit_code if r.stopped else (0 if status == "ok" else 1)}
        logger.info("run %s %s %s: %s, %d request(s), %d gap(s)%s", run_id, kind, r.label, status, requests,
                    len(r.gaps), f" — {out['error']}" if out["error"] else "")
        return out

    @staticmethod
    def _label(slot: Slot, manual: bool) -> str:
        return slot.label if not manual else ("manual" if slot.label == "manual" else f"manual:{slot.label}")

    def record_crash(self, slot: Slot, manual: bool, error: str) -> Optional[int]:
        """An unexpected error inside a run (a bug, an answer the code did not foresee): a ``failed`` run row + a
        ``crash`` gap, so the slot is visibly lost — never silently. Returns the run id (None if even that failed)."""
        kind = "manual" if manual else slot.kind
        now = self.now_ms()
        src = {"oi": ("alpaca_trading", None, None), "fred": ("fred", None, None)}.get(
            slot.kind, ("alpaca_data", "indicative", "alpaca_iex_trade"))
        r = Run(kind, now if manual else slot.ms, self._label(slot, manual), now, *src)
        r.failed, r.error = True, self.clean(error)
        r.gap("crash", "the run stopped on an internal error (see the archive log)", self.clean(error), now_ms=now)
        try:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            return self._write(r, "failed", now, 0, 0)
        except Exception:            # noqa: BLE001 — the caller logs; the status file still shows the error
            logger.warning("the crash of run %s %s could not be recorded in the archive database", kind, r.label)
            return None

    def _write(self, r: Run, status: str, finished: int, requests: int, retries: int) -> int:
        c = self.conn
        cfg = self.cfg
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("INSERT OR IGNORE INTO os_configs (hash, json, first_ms) VALUES (?, ?, ?)",
                      (cfg.assumptions_hash, cfg.as_json(), finished))
            cur = c.execute(
                "INSERT INTO os_runs (kind, slot_ms, slot_label, started_ms, finished_ms, status, requests, pages,"
                " retries, error, source, feed, spot_source, model_version, assumptions_hash, app_version, skew_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'raw', ?, ?, ?)",
                (r.kind, r.slot_ms, r.label, r.started_ms, finished, status, requests, max(0, requests - retries),
                 retries, self.clean(r.error), r.source, r.feed, r.spot_source, cfg.assumptions_hash, VERSION,
                 r.skew_ms))
            rid = int(cur.lastrowid)
            c.executemany("INSERT INTO os_chain_snapshots (run_id, symbol, spot, spot_t_ms, bid, ask, bid_size, "
                          "ask_size, quote_t_ms, last, last_t_ms, daily_volume, iv, delta, gamma, theta, vega, rho, "
                          "available_ms, observed_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          [(rid,) + row for row in r.snapshots])
            c.executemany("INSERT OR IGNORE INTO os_contracts (symbol, underlying, type, strike, expiration, style, "
                          "size, first_seen_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", r.contracts)
            c.executemany("INSERT OR IGNORE INTO os_contract_sightings (run_id, symbol, seen_ms) VALUES (?, ?, ?)",
                          [(rid,) + row for row in r.sightings])
            c.executemany("INSERT INTO os_open_interest (run_id, symbol, open_interest, valid_for, "
                          "available_ms, observed_ms, early_oi) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          [(rid,) + row for row in r.oi])
            c.executemany("INSERT OR IGNORE INTO os_fred (series_id, date, value, available_ms, observed_ms) VALUES"
                          " (?, ?, ?, ?, ?)", r.fred)
            c.executemany("INSERT INTO os_gaps (run_id, slot_ms, kind, underlying, symbol, what, reason, recorded_ms)"
                          " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          [(rid,) + g[:5] + (self.clean(g[5]), g[6]) for g in r.gaps])
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return rid

    def _checked(self, r: Run, status=None) -> None:
        """After every request: Alpaca HTTP 401 / 403 = the paper keys were rejected → stop (exit 2); the first
        measured clock skew of the run is stored (``os_runs.skew_ms``) — > 30 s a gap, > 5 min the run stops."""
        if status in (401, 403):
            raise Stop(f"Alpaca rejected the paper keys (HTTP {status}) — check them in ⚙ Keys & data → Keys", code=2)
        sk = self.http.last_skew_ms
        if r.skew_ms is not None or sk is None:
            return
        r.skew_ms = sk
        if abs(sk) > SKEW_STOP_MS:
            raise ClockSkew(f"clock: this PC's clock differs from the server time by {sk / 1000:+.0f} s — correct the "
                            "Windows clock (Settings → Time → Sync now); nothing was stored", code=3)
        if abs(sk) > SKEW_GAP_MS:
            r.gap("clock_skew", f"this PC's clock differs from the server time by {sk / 1000:+.0f} s",
                  "point-in-time stamps of this run may be off by that much", now_ms=self.now_ms())

    # --- chain snapshot ------------------------------------------------------------------------------------
    def _spot(self, r: Run, h: dict, sym: str) -> Tuple[Optional[float], Optional[int]]:
        status, data = self.http.get(DATA, f"/v2/stocks/{sym}/trades/latest", {"feed": "iex"}, h, "stock")
        self._checked(r, status)
        t = (data or {}).get("trade") if isinstance(data, dict) and status == 200 else None
        if not isinstance(t, dict):
            return None, None
        return _f(t.get("p")), iso_ms(t.get("t"))

    def _chain(self, r: Run, slot: Slot, deadline: bool) -> None:
        cfg = self.cfg
        h = self.alpaca_headers()
        today = slot.ny.date() if deadline else datetime.fromtimestamp(r.started_ms / 1000, NY).date()
        ref_ms = slot.ms if deadline else None          # quote ages: vs the slot (manual: vs the receive time)
        for sym in cfg.symbols:
            spot, spot_t = self._spot(r, h, sym)
            if spot is None or spot <= 0:
                r.gap("not_collected", f"{sym}: no spot price (latest IEX trade) — the strike filter cannot be "
                                       "applied; no chain collected", underlying=sym, now_ms=self.now_ms())
                continue
            params = {"feed": "indicative", "limit": cfg.page_limit,                 # feed ALWAYS explicit (§21.1)
                      "expiration_date_gte": today.isoformat(),
                      "expiration_date_lte": (today + timedelta(days=cfg.expiry_days)).isoformat(),
                      "strike_price_gte": round(spot * (1 - cfg.strike_band_pct / 100), 2),
                      "strike_price_lte": round(spot * (1 + cfg.strike_band_pct / 100), 2)}
            observed: Dict[str, int] = {}
            page_seen = {"ms": 0}

            def on_page(data, page_seen=page_seen, observed=observed):
                page_seen["ms"] = self.now_ms()
                for k in (data.get("snapshots") or {}):
                    observed.setdefault(k, page_seen["ms"])
            items, pages, status, capped = self.http.pages(DATA, f"/v1beta1/options/snapshots/{sym}", params, h,
                                                           "snapshots", "snapshots", on_page=on_page)
            self._checked(r, status)
            if status != 200:
                r.gap("page_failed", f"{sym}: snapshot page {pages} answered HTTP {status}", underlying=sym,
                      now_ms=self.now_ms())
            if capped:
                r.gap("page_failed", f"{sym}: stopped at {pages} pages (cap) — the chain is incomplete", underlying=sym,
                      now_ms=self.now_ms())
            snaps = items if isinstance(items, dict) else {}
            counts = {"after_slot": 0, "stale_quote": 0, "field_missing": 0}
            usable = 0
            for occ, v in sorted(snaps.items()):
                if not isinstance(v, dict):
                    counts["field_missing"] += 1
                    continue
                lq = v.get("latestQuote") if isinstance(v.get("latestQuote"), dict) else {}
                lt = v.get("latestTrade") if isinstance(v.get("latestTrade"), dict) else {}
                g = v.get("greeks") if isinstance(v.get("greeks"), dict) else {}
                bar = v.get("dailyBar") if isinstance(v.get("dailyBar"), dict) else {}
                qt = iso_ms(lq.get("t"))
                obs = observed.get(occ, page_seen["ms"] or self.now_ms())
                row = (occ, spot, spot_t, _f(lq.get("bp")), _f(lq.get("ap")), _i(lq.get("bs")), _i(lq.get("as")), qt,
                       _f(lt.get("p")), iso_ms(lt.get("t")), _i(bar.get("v")), _f(v.get("impliedVolatility")),
                       _f(g.get("delta")), _f(g.get("gamma")), _f(g.get("theta")), _f(g.get("vega")),
                       _f(g.get("rho")), obs, obs)
                r.snapshots.append(row)
                r.data_rows += 1
                ok = True
                if qt is None or row[3] is None or row[4] is None or row[11] is None or None in row[12:16]:
                    counts["field_missing"] += 1
                    ok = False
                ref = ref_ms if ref_ms is not None else obs
                if qt is not None:
                    if ref_ms is not None and qt > ref_ms:
                        counts["after_slot"] += 1
                        ok = False
                    elif ref - qt > cfg.quote_max_age_s * 1000:
                        counts["stale_quote"] += 1
                        ok = False
                usable += 1 if ok else 0
            n = len(snaps)
            texts = {"after_slot": "quote(s) stamped after the slot time — excluded",
                     "stale_quote": f"quote(s) older than {cfg.quote_max_age_s} s at the slot",
                     "field_missing": "contract(s) without quote / greeks / implied volatility"}
            for k, cnt in counts.items():
                if cnt:
                    r.gap(k, f"{sym}: {cnt} of {n} {texts[k]}", underlying=sym, now_ms=self.now_ms())
            cov = 100.0 * usable / n if n else 0.0
            if n == 0 and status == 200:
                r.gap("not_collected", f"{sym}: the chain answer had no contracts", underlying=sym, now_ms=self.now_ms())
            elif n and cov < cfg.coverage_min_pct:
                r.gap("coverage", f"{sym}: {usable} of {n} contracts usable ({cov:.1f} % < {cfg.coverage_min_pct:g} %) "
                                  "— GEX unavailable for this slot", underlying=sym, now_ms=self.now_ms())

    # --- contracts + open interest --------------------------------------------------------------------------
    def _oi(self, r: Run, slot: Slot) -> None:
        cfg = self.cfg
        h = self.alpaca_headers()
        today = slot.ny.date()
        expected = calendars.previous_trading_day(today)
        floor_t = hm(cfg.oi_floor)
        for sym in cfg.symbols:
            observed = {"ms": 0}
            items, pages, status, capped = self.http.pages(
                TRADING, "/v2/options/contracts",
                {"underlying_symbols": sym, "status": "active", "expiration_date_gte": today.isoformat(),
                 "expiration_date_lte": (today + timedelta(days=cfg.expiry_days)).isoformat(),   # ALWAYS explicit
                 "limit": cfg.page_limit}, h, "option_contracts", "contracts",
                on_page=lambda d, o=observed: o.__setitem__("ms", self.now_ms()))
            self._checked(r, status)
            if status != 200:
                r.gap("page_failed", f"{sym}: contracts page {pages} answered HTTP {status}", underlying=sym,
                      now_ms=self.now_ms())
            if capped:
                r.gap("page_failed", f"{sym}: stopped at {pages} pages (cap)", underlying=sym, now_ms=self.now_ms())
            rows = [c for c in (items if isinstance(items, list) else []) if isinstance(c, dict) and c.get("symbol")]
            known = self._latest_contracts(sym)
            latest_oi = self._latest_oi(sym)
            missing = stale = conflicts = 0
            seen_oi: Dict[str, int] = {}
            obs = observed["ms"] or self.now_ms()
            for c in rows:
                occ = str(c["symbol"])
                attrs = (c.get("type"), _f(c.get("strike_price")), str(c.get("expiration_date") or "")[:10] or None,
                         c.get("style"), _i(c.get("size")))
                if known.get(occ) != attrs:
                    r.contracts.append((occ, sym) + attrs + (obs,))
                r.sightings.append((occ, obs))
                oi, vf = _i(c.get("open_interest")), _date(c.get("open_interest_date"))
                if oi is None or vf is None:
                    missing += 1
                    continue
                if vf != expected:
                    stale += 1
                floor_ms = int(datetime.combine(calendars.next_trading_day(vf), floor_t, tzinfo=NY).timestamp() * 1000)
                if occ in seen_oi:                   # listed twice in ONE answer: never collapse silently
                    if seen_oi[occ] != oi:
                        conflicts += 1
                    continue
                seen_oi[occ] = oi
                if latest_oi.get((occ, vf.isoformat())) == oi:
                    continue                         # same value already archived — not a correction
                r.oi.append((occ, oi, vf.isoformat(), max(obs, floor_ms), obs, 1 if obs < floor_ms else 0))
                r.data_rows += 1
            if missing:
                r.gap("field_missing", f"{sym}: {missing} of {len(rows)} contract(s) without open_interest / "
                                       "open_interest_date", underlying=sym, now_ms=self.now_ms())
            if conflicts:
                r.gap("oi_conflict", f"{sym}: {conflicts} contract(s) listed twice with different open interest in "
                                     "one answer — the first value was kept", underlying=sym, now_ms=self.now_ms())
            if stale:
                r.gap("stale_oi", f"{sym}: {stale} contract(s) with OI not of the previous session ({expected})",
                      underlying=sym, now_ms=self.now_ms())
            if not rows and status == 200:
                r.gap("not_collected", f"{sym}: no contracts in the window", underlying=sym, now_ms=self.now_ms())
        r.data_rows += len(r.sightings)          # contracts seen count as data even when no OI value is new

    def _latest_contracts(self, sym: str) -> Dict[str, tuple]:
        out = {}
        for row in self.conn.execute("SELECT symbol, type, strike, expiration, style, size FROM os_contracts "
                                     "WHERE underlying = ? ORDER BY first_seen_ms", (sym,)):
            out[row[0]] = tuple(row[1:])
        return out

    def _latest_oi(self, sym: str) -> Dict[tuple, int]:
        out = {}
        for row in self.conn.execute("SELECT o.symbol, o.valid_for, o.open_interest FROM os_open_interest o "
                                     "JOIN os_contracts c ON c.symbol = o.symbol WHERE c.underlying = ? "
                                     "ORDER BY o.available_ms, o.observed_ms", (sym,)):
            out[(row[0], row[1])] = row[2]
        return out

    # --- FRED -------------------------------------------------------------------------------------------------
    def _fred(self, r: Run, slot: Slot) -> None:
        key, why = self.fred_key()
        if key is None:
            r.failed, r.error = True, why
            r.gap("keys", "FRED not collected", why, now_ms=self.now_ms())
            return
        latest = {(row[0], row[1]): row[2] for row in self.conn.execute(
            "SELECT series_id, date, value FROM os_fred ORDER BY available_ms")}
        for sid in self.cfg.fred_series:
            status, data = self.http.get(FRED, "/fred/series/observations",
                                         {"series_id": sid, "api_key": key, "file_type": "json", "sort_order": "desc",
                                          "limit": self.cfg.fred_observations}, None, "fred")
            obs_ms = self.now_ms()
            self._checked(r)
            if status != 200 or not isinstance(data, dict):
                err = str((data or {}).get("error_message") or f"HTTP {status}") if isinstance(data, dict) else \
                    f"HTTP {status}"
                kind = "not_on_fred" if "does not exist" in err.lower() else "fred_error"
                r.gap(kind, f"{sid}: not collected", self.clean(err), underlying=sid, now_ms=obs_ms)
                continue
            dots = 0
            for o in data.get("observations") or []:
                d = _date((o or {}).get("date"))
                if d is None:
                    continue
                raw = (o or {}).get("value")
                val = None if raw in (None, ".") else _f(raw)
                if val is None:
                    dots += 1
                key_ = (sid, d.isoformat())
                if key_ in latest and latest[key_] == val:
                    continue
                r.fred.append((sid, d.isoformat(), val, obs_ms, obs_ms))
                r.data_rows += 1
            r.data_rows += 0 if r.fred else 1        # "nothing new" is a successful run, not a failure
            if dots:
                r.gap("field_missing", f"{sid}: {dots} observation(s) without a value ('.')", underlying=sid,
                      now_ms=obs_ms)
