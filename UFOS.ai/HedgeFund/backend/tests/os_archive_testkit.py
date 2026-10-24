"""Fakes for the OS1 options-archive tests (SP §21): a fake clock (wall time + monotonic + sleep in one), a fake
Alpaca / FRED transport (contracts with OI, chain snapshots with greeks, latest IEX trades, FRED observations; errors,
5xx, 429, network failures, latency), keys in a MemoryStore-like object. No network, temp dirs only."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from strategy_platform import secrets

KID, SKEY, FKEY = "PKARCHIVEKEY12345", "archiveSecret0987654321abcd", "fredarchivekey0123456789abcdef01"
SPOT = {"SPY": 600.0, "QQQ": 500.0}


class Clock:
    def __init__(self, start: datetime):
        self.t = start
        self.mono = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def monotonic(self):
        return self.mono

    def sleep(self, s):
        s = max(0.0, float(s))
        self.slept.append(s)
        self.t += timedelta(seconds=s)
        self.mono += s

    def advance(self, s):
        self.t += timedelta(seconds=s)
        self.mono += s


def occ(sym, exp, cp, k):
    return f"{sym}{exp:%y%m%d}{cp}{int(round(k * 1000)):08d}"


class FakeApi:
    def __init__(self, clock, today: date, page=60, latency=0.2, quote_age_s=20.0, oi_date=None, oi_value=1234,
                 missing_greeks=0, after_slot=0, stale=0, fail=None, fred_values=None, fred_missing=(),
                 no_spot=(), skew_s=0.0, date_header=True):
        self.skew_s, self.date_header = skew_s, date_header      # server Date = fake now + skew_s
        self.clock, self.today, self.page, self.latency = clock, today, page, latency
        self.quote_age_s, self.oi_date, self.oi_value = quote_age_s, oi_date, oi_value
        self.missing_greeks, self.after_slot, self.stale = missing_greeks, after_slot, stale
        self.fail = dict(fail or {})        # path prefix → list of actions: "net" | 500 | 429 | 400 …
        self.fred_values = dict(fred_values or {"VIXCLS": [("2026-10-22", "17.1"), ("2026-10-21", ".")],
                                                "VXVCLS": [("2026-10-22", "19.4")], "VXNCLS": [("2026-10-22", "21.0")]})
        self.fred_missing, self.no_spot = set(fred_missing), set(no_spot)
        self.calls = []
        self.slot_ms = None                 # quote times are set relative to this (the slot) when given

    def contracts(self, sym):
        out = []
        for exp in (self.today + timedelta(days=1), self.today + timedelta(days=28), self.today + timedelta(days=56)):
            for i in range(-6, 7):
                k = SPOT[sym] + 10 * i
                for cp, typ in (("C", "call"), ("P", "put")):
                    out.append({"symbol": occ(sym, exp, cp, k), "underlying_symbol": sym, "type": typ,
                                "style": "american", "size": "100", "status": "active",
                                "expiration_date": exp.isoformat(), "strike_price": f"{k:.1f}",
                                "open_interest": str(self.oi_value),
                                "open_interest_date": (self.oi_date or "2026-10-22")})
        return out

    def _page(self, items, qs, key, as_dict=False):
        start = int((qs.get("page_token") or ["0"])[0])
        chunk = items[start:start + self.page]
        nxt = str(start + self.page) if start + self.page < len(items) else None
        return {key: dict(chunk) if as_dict else chunk, "next_page_token": nxt}

    def _h(self):
        if not self.date_header:
            return {}
        from email.utils import formatdate
        return {"Date": formatdate(self.clock.now().timestamp() + self.skew_s, usegmt=True)}

    def _iso(self, ms):
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers), timeout))
        self.clock.advance(self.latency)
        u = urlparse(url)
        qs = parse_qs(u.query)
        for prefix, actions in self.fail.items():
            if u.path.startswith(prefix) and actions:
                a = actions.pop(0)
                if a == "net":
                    raise ConnectionResetError(f"reset while talking to {url}")
                return a, self._h(), json.dumps({"message": "fake error", "error_message": "fake error"}).encode()
        if u.path.startswith("/v2/stocks/"):
            sym = u.path.split("/")[3]
            if sym in self.no_spot:
                return 200, self._h(), b'{"trade": null}'
            return 200, self._h(), json.dumps({"trade": {"p": SPOT[sym], "t": self._iso(int(self.clock.now().timestamp() * 1000) - 1000)}}).encode()
        if u.path == "/v2/options/contracts":
            rows = [r for r in self.contracts(qs["underlying_symbols"][0])
                    if qs["expiration_date_gte"][0] <= r["expiration_date"] <= qs["expiration_date_lte"][0]]
            return 200, self._h(), json.dumps(self._page(rows, qs, "option_contracts")).encode()
        if u.path.startswith("/v1beta1/options/snapshots/"):
            sym = u.path.rsplit("/", 1)[1]
            lo, hi = float(qs["strike_price_gte"][0]), float(qs["strike_price_lte"][0])
            ref = self.slot_ms or int(self.clock.now().timestamp() * 1000)
            items = []
            n = 0
            for c in self.contracts(sym):
                if not (lo <= float(c["strike_price"]) <= hi and qs["expiration_date_gte"][0] <= c["expiration_date"]
                        <= qs["expiration_date_lte"][0]):
                    continue
                qt = ref - int(self.quote_age_s * 1000)
                if n < self.after_slot:
                    qt = ref + 5000
                elif n < self.after_slot + self.stale:
                    qt = ref - 900_000
                snap = {"latestQuote": {"t": self._iso(qt), "bp": 1.0, "ap": 1.1, "bs": 5, "as": 7},
                        "latestTrade": {"t": self._iso(qt - 60_000), "p": 1.05},
                        "dailyBar": {"v": 321},
                        "greeks": {"delta": 0.5, "gamma": 0.01, "theta": -0.1, "vega": 0.2, "rho": 0.01},
                        "impliedVolatility": 0.18}
                if n >= self.after_slot + self.stale and n < self.after_slot + self.stale + self.missing_greeks:
                    snap.pop("greeks")
                items.append((c["symbol"], snap))
                n += 1
            return 200, self._h(), json.dumps(self._page(items, qs, "snapshots", as_dict=True)).encode()
        if u.path == "/fred/series/observations":
            sid = qs["series_id"][0]
            if sid in self.fred_missing:
                return 400, self._h(), json.dumps({"error_code": 400, "error_message": "Bad Request. The series does not exist."}).encode()
            obs = [{"date": d, "value": v} for d, v in self.fred_values.get(sid, [])]
            return 200, self._h(), json.dumps({"observations": obs}).encode()
        return 404, self._h(), b"{}"


class Sec:
    def __init__(self, values=None, broken=()):
        self.values = dict(values if values is not None else
                           {"alpaca-paper-key-id": KID, "alpaca-paper-secret-key": SKEY, "fred-api-key": FKEY})
        self.broken = set(broken)

    def get_secret(self, name):
        if name in self.broken:
            raise OSError("credential store busy")
        return self.values.get(name)


class ArchiveCase(unittest.TestCase):
    """A temp data dir per test; ``make(start)`` builds an Archive on the fake clock + transport."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ufos-os1-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make(self, start: datetime, api=None, cfg=None, sec=None, mode="service"):
        from strategy_platform.options_archive.config import Config
        from strategy_platform.options_archive.service import Archive
        self.clock = Clock(start)
        self.api = api or FakeApi(self.clock, start.date())
        self.api.clock = self.clock
        self.arch = Archive(cfg or Config(), self.tmp, transport=self.api, secrets_module=sec or Sec(),
                            now=self.clock.now, sleep=self.clock.sleep, monotonic=self.clock.monotonic, mode=mode)
        self.addCleanup(self.arch.close)
        return self.arch

    def rows(self, sql, args=()):
        return [dict(r) for r in self.arch.conn.execute(sql, args)]

    def all_text(self):
        out = []
        for root, _d, files in os.walk(self.tmp):
            for f in files:
                if f.endswith((".json", ".log")):
                    with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                        out.append(fh.read())
        return "\n".join(out)
