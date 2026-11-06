"""Synthetic archives for the OS2 tests (SP §21.12): an ``options_archive.db`` in a temp dir built with the OS1
migration, filled with a constructed chain — prices from BSM with a known volatility smile, open interest with
point-in-time stamps, FRED rows (rate, VIX series). No Alpaca data, no network."""
import json
import math
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from strategy_platform.core import calendars
from strategy_platform.options_archive import config as oconfig
from strategy_platform.options_archive import db as odb
from strategy_platform.options_structure import bs
from strategy_platform.options_structure import config as scfg

NY = calendars.NY
UTC = timezone.utc


def ny_ms(d: date, hh: int, mm: int, ss: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=NY).timestamp() * 1000)


def occ(root: str, exp: date, kind: str, strike: float) -> str:
    return f"{root}{exp:%y%m%d}{'C' if kind == 'call' else 'P'}{int(round(strike * 1000)):08d}"


class Archive:
    """Builder for one synthetic archive DB."""

    def __init__(self, path: str):
        self.conn = odb.open_rw(path, lambda: 1)
        self.path = path
        self.acfg = oconfig.Config()

    def run(self, kind: str, slot_ms: int, status: str = "ok", label: str = "10:00", started=None, finished=None,
            assumptions_hash: str = "h") -> int:
        cur = self.conn.execute(
            "INSERT INTO os_runs (kind, slot_ms, slot_label, started_ms, finished_ms, status, source, feed, "
            "model_version, assumptions_hash, app_version) VALUES (?, ?, ?, ?, ?, ?, 'alpaca_data', 'indicative', "
            "'raw', ?, 'test')", (kind, slot_ms, label, started or slot_ms - 60000, finished or slot_ms - 1000,
                                  status, assumptions_hash))
        return cur.lastrowid

    def contract(self, sym: str, underlying: str, kind: str, strike: float, exp: date, size=100, first_seen=1):
        self.conn.execute("INSERT INTO os_contracts (symbol, underlying, type, strike, expiration, style, size, "
                          "first_seen_ms) VALUES (?, ?, ?, ?, ?, 'american', ?, ?)",
                          (sym, underlying, kind, strike, exp.isoformat(), size, first_seen))

    def oi(self, run_id, sym, oi, valid_for: date, available_ms, observed_ms=None):
        self.conn.execute("INSERT INTO os_open_interest (run_id, symbol, open_interest, valid_for, available_ms, "
                          "observed_ms, early_oi) VALUES (?, ?, ?, ?, ?, ?, 0)",
                          (run_id, sym, oi, valid_for.isoformat(), available_ms, observed_ms or available_ms))

    def snap(self, run_id, sym, spot, bid, ask, quote_t_ms, available_ms, iv=None, gamma=None, spot_t_ms=None):
        self.conn.execute("INSERT INTO os_chain_snapshots (run_id, symbol, spot, spot_t_ms, bid, ask, bid_size, ask_size,"
                          " quote_t_ms, iv, delta, gamma, theta, vega, rho, available_ms, observed_ms) VALUES "
                          "(?, ?, ?, ?, ?, ?, 1, 1, ?, ?, 0.5, ?, -0.1, 0.1, 0.01, ?, ?)",
                          (run_id, sym, spot, spot_t_ms or quote_t_ms, bid, ask, quote_t_ms, iv, gamma, available_ms,
                           available_ms))

    def fred(self, series, d: date, value, available_ms):
        self.conn.execute("INSERT INTO os_fred (series_id, date, value, available_ms, observed_ms) VALUES (?, ?, ?, ?, ?)",
                          (series, d.isoformat(), value, available_ms, available_ms))

    def gap(self, run_id, slot_ms, kind, underlying, what):
        self.conn.execute("INSERT INTO os_gaps (run_id, slot_ms, kind, underlying, what, recorded_ms) VALUES "
                          "(?, ?, ?, ?, ?, ?)", (run_id, slot_ms, kind, underlying, what, slot_ms))


def smile_vol(k: float, atm=0.20, skew=-0.30, curv=0.8) -> float:
    """A constructed smile in log-moneyness k = ln(K / F)."""
    return max(0.05, atm + skew * k + curv * k * k)


def build_chain(a: Archive, d: date, slot_ms: int, *, symbol="SPY", spot=600.0, expiries=None, strikes=None,
                rate_pct=4.0, q=0.012, oi=1000, oi_fn=None, run_kind="intraday", spread=0.02, quote_age_ms=30000,
                status="ok", vendor_iv_shift=0.01, oi_valid_for=None, oi_available_ms=None, label="10:00",
                vol_fn=None, oi_run=None, quote_age_fn=None, assumptions_hash="h"):
    """One run with a full synthetic chain priced by BSM from ``smile_vol``; returns (run_id, info)."""
    acfg = a.acfg
    expiries = expiries or [d + timedelta(days=7), d + timedelta(days=14), d + timedelta(days=28),
                            d + timedelta(days=35), d + timedelta(days=42)]
    strikes = strikes or [round(spot * (1 + x / 100.0)) for x in range(-5, 6)]
    r = bs.rate_from_discount_yield(rate_pct, 91)
    run = a.run(run_kind, slot_ms, status=status, label=label, assumptions_hash=assumptions_hash)
    prev = calendars.previous_trading_day(d)
    oi_vf = oi_valid_for or prev
    oi_av = oi_available_ms if oi_available_ms is not None else ny_ms(d, 8, 0)
    if oi_run is None:
        oi_run = a.run("oi", ny_ms(d, 8, 30), label="oi", started=ny_ms(d, 8, 30), finished=ny_ms(d, 8, 31))
    for exp in expiries:
        T = (int(datetime.combine(exp, calendars.REGULAR_CLOSE, tzinfo=NY).timestamp() * 1000) - slot_ms) / (365.0 * 86400000)
        F = spot * math.exp((r - q) * T)
        for K in strikes:
            for kind in ("call", "put"):
                s = occ(symbol, exp, kind, K)
                if not a.conn.execute("SELECT 1 FROM os_contracts WHERE symbol = ?", (s,)).fetchone():
                    a.contract(s, symbol, kind, float(K), exp)
                if T <= 0:
                    continue
                vol = (vol_fn or smile_vol)(math.log(K / F))
                p = bs.price(kind, spot, K, T, r, q, vol)
                h = max(0.005, spread * p / 2)
                age = quote_age_fn(exp, K) if quote_age_fn else quote_age_ms
                a.snap(run, s, spot, max(0.0, p - h), p + h, slot_ms - age, slot_ms - 2000,
                       iv=vol + vendor_iv_shift, gamma=bs.gamma(spot, K, T, r, q, vol))
                n = oi_fn(kind, K, exp) if oi_fn else oi
                if n is not None and not a.conn.execute(
                        "SELECT 1 FROM os_open_interest WHERE symbol = ? AND valid_for = ?", (s, oi_vf.isoformat())
                ).fetchone():
                    a.oi(oi_run, s, n, oi_vf, oi_av)
    return run, {"rate": r, "q": q, "spot": spot, "oi_run": oi_run}


class OS2Case(unittest.TestCase):
    """A temp data dir with a synthetic archive; the OS2 config defaults."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ufos-os2-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.arch = Archive(os.path.join(self.tmp, "options_archive.db"))
        self.addCleanup(self.arch.conn.close)
        self.cfg = scfg.Config()
        self.acfg = self.arch.acfg

    def write_override(self, name, data):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
