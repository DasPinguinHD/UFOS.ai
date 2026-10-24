"""OS0 read-only data check (tools/verify_alpaca_options.py, SPEC §21) against a fake HTTP transport — no network
(one test uses a loopback HTTP server for the no-redirect opener): URL allow-list + GET only, no key material in the
output or the JSON report (also not from an exception text carrying a FRED URL), paging with a pause between pages (≤ 1000 contracts per page, a progress line per page), per-endpoint timeouts,
ONE counted retry after a timeout / connection reset then STOP, sanitized network reasons,
explicit expiry bounds (the fake applies Alpaca's "next weekend" default), stops (429 → 4, keys rejected → 2, network
/ clock / oversized → 3, "RESULT: STOPPED", the rest FAIL "not run"), data errors → FAIL / PARTIAL with the field,
missing fields not filled in, the budget and storage arithmetic, market-closed limits, FRED errors vs missing series."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from strategy_platform import secrets

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import verify_alpaca_options as tool  # noqa: E402

KID, SKEY, FKEY = "PKFAKEKEYID123456", "fakeSecretKey0987654321abcdef", "fredkey0123456789abcdef0123456789"
NOW = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)          # 14:00 New York, RTH
SPOT = {"SPY": 600.0, "QQQ": 500.0}
EXPIRIES = (date(2026, 10, 24), date(2026, 11, 20), date(2026, 12, 18), date(2027, 1, 15))


def occ(sym, exp, cp, k):
    return f"{sym}{exp:%y%m%d}{cp}{int(round(k * 1000)):08d}"


def contracts(sym):
    out = []
    spot = SPOT[sym]
    for exp in EXPIRIES:
        for i in range(-12, 13):
            k = spot + 5 * i
            for cp, typ in (("C", "call"), ("P", "put")):
                out.append({"symbol": occ(sym, exp, cp, k), "underlying_symbol": sym, "type": typ, "status": "active",
                            "expiration_date": exp.isoformat(), "strike_price": f"{k:.1f}", "open_interest": "1234",
                            "open_interest_date": "2026-10-22"})
    return out


class FakeApi:
    def __init__(self, open_=True, page=80, missing=(), rate_limit_path=None, fred_short=False, raise_on=None,
                 clock_status=200, fred_auth_error=False, malformed=False, late_quote=False):
        self.open, self.page, self.missing, self.rl, self.fred_short = open_, page, set(missing), rate_limit_path, fred_short
        self.raise_on, self.clock_status, self.fred_auth_error = raise_on, clock_status, fred_auth_error
        self.malformed, self.late_quote = malformed, late_quote
        self.calls = []

    def _page(self, items, qs, key, as_dict=False):
        start = int((qs.get("page_token") or ["0"])[0])
        chunk = items[start:start + self.page]
        nxt = str(start + self.page) if start + self.page < len(items) else None
        return {key: dict(chunk) if as_dict else chunk, "next_page_token": nxt}

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        u = urlparse(url)
        qs = parse_qs(u.query)
        if self.rl and u.path.startswith(self.rl):
            return 429, {}, b'{"message":"too many requests"}'
        if self.raise_on and u.path.startswith(self.raise_on[0]):
            raise self.raise_on[1]
        if u.path == "/v2/clock":
            if self.clock_status != 200:
                return self.clock_status, {}, b'{"message":"forbidden"}'
            return 200, {}, json.dumps({"timestamp": "2026-10-23T14:00:00-04:00", "is_open": self.open,
                                        "next_open": "2026-10-26T09:30:00-04:00"}).encode()
        if u.path == "/v2/options/contracts":
            rows = contracts(qs["underlying_symbols"][0])
            today = date(2026, 10, 23)
            # Alpaca's default: expiration_date_lte = the next weekend
            lte = date.fromisoformat(qs["expiration_date_lte"][0]) if "expiration_date_lte" in qs else \
                today + timedelta(days=(6 - today.weekday()) % 7 or 7)
            gte = date.fromisoformat(qs["expiration_date_gte"][0]) if "expiration_date_gte" in qs else today
            rows = [r for r in rows if gte <= date.fromisoformat(r["expiration_date"]) <= lte]
            if self.malformed:
                rows[0] = dict(rows[0], expiration_date="2026-13-45")
            if "oi" in self.missing:
                for r in rows:
                    r.pop("open_interest"), r.pop("open_interest_date")
            return 200, {}, json.dumps(self._page(rows, qs, "option_contracts")).encode()
        if u.path.startswith("/v2/stocks/"):
            sym = u.path.split("/")[3]
            return 200, {}, json.dumps({"trade": {"p": SPOT[sym], "t": "2026-10-23T17:59:59.123456789Z"}}).encode()
        if u.path.startswith("/v1beta1/options/snapshots/"):
            sym = u.path.rsplit("/", 1)[1]
            lo, hi = float(qs["strike_price_gte"][0]), float(qs["strike_price_lte"][0])
            lte = date.fromisoformat(qs["expiration_date_lte"][0])
            items = []
            for c in contracts(sym):
                if lo <= float(c["strike_price"]) <= hi and date.fromisoformat(c["expiration_date"]) <= lte:
                    qt = "2026-10-23T18:00:10Z" if self.late_quote else "2026-10-23T17:59:30.5Z"
                    snap = {"latestQuote": {"t": qt, "bp": 1.0, "ap": 1.1},
                            "greeks": {"delta": 0.5, "gamma": 0.01, "theta": -0.1, "vega": 0.2, "rho": 0.01},
                            "impliedVolatility": 0.18}
                    if "greeks" in self.missing:
                        snap.pop("greeks"), snap.pop("impliedVolatility")
                    items.append((c["symbol"], snap))
            return 200, {}, json.dumps(self._page(items, qs, "snapshots", as_dict=True)).encode()
        if u.path == "/v1beta1/options/bars":
            sym, tf = qs["symbols"][0], qs["timeframe"][0]
            if sym == "SPY240119C00480000":
                bars = []
            elif sym == "SPY240216C00500000":
                bars = [{"t": f"2024-02-{d:02d}T05:00:00Z", "c": 1.0} for d in (1, 2, 5, 6)]
            else:
                bars = [{"t": f"2026-10-{d:02d}T05:00:00Z", "c": 1.0} for d in range(13, 24)] if tf == "1Day" else \
                    [{"t": f"2026-10-23T{h:02d}:{m:02d}:00Z", "c": 1.0} for h in range(14, 18) for m in range(60)]
            return 200, {}, json.dumps({"bars": {sym: bars}, "next_page_token": None}).encode()
        if u.path == "/fred/series":
            sid = qs["series_id"][0]
            if self.fred_auth_error:
                return 400, {}, json.dumps({"error_code": 400, "error_message": "Bad Request.  The value for variable "
                                            "api_key is not registered.  Read https://fred.stlouisfed.org/docs/api/"
                                            "api_key.html for more information."}).encode()
            if sid in ("VIXCLS", "VXVCLS", "VXNCLS") or (self.fred_short and sid == "VXSTCLS"):
                return 200, {}, json.dumps({"seriess": [{"id": sid, "title": sid, "frequency": "Daily",
                                                          "observation_start": "1990-01-02",
                                                          "observation_end": "2026-10-22"}]}).encode()
            return 400, {}, json.dumps({"error_code": 400, "error_message": "Bad Request. The series does not exist."}).encode()
        if u.path == "/fred/series/observations":
            return 200, {}, json.dumps({"observations": [{"date": "2026-10-22", "value": "17.1"}]}).encode()
        return 404, {}, b"{}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ufos-os0-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = secrets.MemoryStore({secrets.target("alpaca-paper-key-id"): KID,
                                          secrets.target("alpaca-paper-secret-key"): SKEY,
                                          secrets.target("fred-api-key"): FKEY})
        self.lines, self.sleeps = [], []

    def run_tool(self, api, argv=()):
        code = tool.run(list(argv), transport=api, out=self.lines.append, secrets_module=self._sec(), now=NOW,
                        sleep=self.sleeps.append, report_dir=self.tmp, wall=lambda: NOW)
        files = os.listdir(self.tmp)
        report = None
        if files:
            with open(os.path.join(self.tmp, files[0]), encoding="utf-8") as fh:
                self.raw = fh.read()
            report = json.loads(self.raw)
        return code, report

    def _sec(self):
        store, broken = self.store, getattr(self, "broken_fred", False)

        class Sec:
            @staticmethod
            def get_secret(name):
                if broken and name == "fred-api-key":
                    raise OSError("credential store busy")
                return store.values.get(secrets.target(name))
        return Sec


class AllowListTest(Base):
    def test_allowed_and_refused(self):
        ok = ("https://paper-api.alpaca.markets/v2/options/contracts", "https://paper-api.alpaca.markets/v2/clock",
              "https://data.alpaca.markets/v1beta1/options/snapshots/SPY", "https://data.alpaca.markets/v1beta1/options/bars",
              "https://data.alpaca.markets/v2/stocks/QQQ/trades/latest", "https://api.stlouisfed.org/fred/series")
        for u in ok:
            self.assertTrue(tool.allowed("GET", u), u)
        bad = [("POST", ok[0]), ("DELETE", ok[0]), ("PATCH", ok[1]),
               ("GET", "https://paper-api.alpaca.markets/v2/orders"), ("GET", "https://paper-api.alpaca.markets/v2/account"),
               ("GET", "https://paper-api.alpaca.markets/v2/positions"),
               ("GET", "https://api.alpaca.markets/v2/options/contracts"),          # live trading host
               ("GET", "http://paper-api.alpaca.markets/v2/clock"), ("GET", "https://paper-api.alpaca.markets:8443/v2/clock"),
               ("GET", "https://data.alpaca.markets/v1beta1/options/snapshots/AAPL"),
               ("GET", "https://paper-api.alpaca.markets.evil.example/v2/clock"),
               ("GET", "https://paper-api.alpaca.markets/v2/options/contracts/../../orders")]
        for m, u in bad:
            self.assertFalse(tool.allowed(m, u), (m, u))
        sent = []
        http = tool.Http(lambda *a: sent.append(a) or (200, {}, b"{}"), sleep=lambda s: None)
        with self.assertRaises(tool.Refused):
            http.get("https://paper-api.alpaca.markets", "/v2/orders")
        self.assertEqual(sent, [])

    def test_only_get_requests_in_a_full_run(self):
        api = FakeApi()
        self.run_tool(api)
        self.assertTrue(api.calls)
        self.assertEqual({m for m, _u, _h in api.calls}, {"GET"})
        self.assertTrue(all(tool.allowed(m, u) for m, u, _h in api.calls))


class RunTest(Base):
    def test_happy_run_report_and_no_secrets(self):
        self.api = FakeApi()
        code, rep = self.run_tool(self.api)
        self.assertEqual(code, 0, self.lines)
        ch = rep["checks"]
        self.assertEqual({k: v["status"] for k, v in ch.items()},
                         {"a_contracts": "PASS", "b_snapshots": "PASS", "c_bars": "PASS", "d_budget": "PASS",
                          "e_storage": "PASS", "f_fred": "PARTIAL"})
        a = ch["a_contracts"]["facts"]["SPY"]
        self.assertEqual((a["window"]["contracts"], a["window"]["expiries"], a["window"]["pages"],
                          a["window"]["open_interest_present_pct"]), (150, 3, 2, 100.0))      # ≤ 60 d: 3 expiries
        self.assertEqual((a["full"]["contracts"], a["full"]["expiries"]), (200, 4))
        q = [parse_qs(urlparse(u).query) for _m, u, _h in self.api.calls if "/options/contracts" in u]
        self.assertTrue(all("expiration_date_lte" in x and "expiration_date_gte" in x for x in q))   # never the default
        snaps = [parse_qs(urlparse(u).query) for _m, u, _h in self.api.calls if "/snapshots/" in u]
        self.assertTrue(snaps and all(x["feed"] == ["indicative"] for x in snaps))
        b = ch["b_snapshots"]["facts"]["SPY"]
        self.assertEqual(b["greeks_present_pct"], 100.0)
        self.assertEqual(b["quote_age_seconds"]["median"], 29.5)
        self.assertEqual(b["quote_age_seconds"]["after_snapshot_time"], 0)
        self.assertEqual(b["filter_counts"]["±10%/≤45d"], 2 * 2 * 25)    # 2 expiries ≤ 45 d × C/P × 25 strikes ±10 %
        c = ch["c_bars"]["facts"]
        self.assertEqual(c["atm_contract"]["symbol"], "SPY261120C00600000")
        self.assertEqual((c["atm_contract"]["daily_bars_last_10d"], c["atm_contract"]["minute_bars_last_3d"]), (11, 240))
        self.assertTrue(c["history_since_2024_02"])
        self.assertEqual([p["earliest_bar"] for p in c["history_probes"]], [None, "2024-02-01"])
        self.assertIn("VXSTCLS NOT on FRED", "\n".join(self.lines))
        self.assertTrue(ch["f_fred"]["facts"]["VXNCLS"]["exists"])
        self.assertTrue(self.sleeps and all(s == tool.PAGE_SLEEP_SECONDS for s in self.sleeps))   # paced pages
        text = self.raw + "\n".join(self.lines)
        for s in (KID, SKEY, FKEY):
            self.assertNotIn(s, text)
        self.assertNotIn("APCA-API", self.raw)
        self.assertTrue(self.lines[-1].startswith("RESULT: OK"))

    def test_quote_after_snapshot_time_counted_not_clamped(self):
        code, rep = self.run_tool(FakeApi(late_quote=True))
        q = rep["checks"]["b_snapshots"]["facts"]["SPY"]["quote_age_seconds"]
        self.assertEqual((q["median"], q["min"]), (-10.0, -10.0))
        self.assertEqual(q["after_snapshot_time"], rep["checks"]["b_snapshots"]["facts"]["SPY"]["contracts"])

    def test_timeout_stops_with_exit_3(self):
        api = FakeApi(raise_on=("/v1beta1/options/snapshots", TimeoutError("timed out")))
        code, rep = self.run_tool(api)
        self.assertEqual(code, 3)
        self.assertIn("network error on data.alpaca.markets/v1beta1/options/snapshots/SPY: TimeoutError", rep["stopped"])
        for name in ("b_snapshots", "c_bars", "d_budget", "e_storage", "f_fred"):
            self.assertEqual(rep["checks"][name]["status"], "FAIL")
            self.assertIn("not run", rep["checks"][name]["facts"]["note"])
        self.assertEqual(self.lines[-1], "RESULT: STOPPED")

    def test_fred_url_in_exception_never_shown(self):
        api = FakeApi(raise_on=("/fred/series", OSError(f"failed https://api.stlouisfed.org/fred/series?series_id="
                                                        f"VIXCLS&api_key={FKEY}")))
        code, rep = self.run_tool(api)
        self.assertEqual(code, 3)
        self.assertNotIn(FKEY, self.raw + "\n".join(self.lines))
        self.assertNotIn("api_key", self.raw + "\n".join(self.lines))

    def test_clock_rejected_keys_exit_2_nothing_else(self):
        api = FakeApi(clock_status=401)
        code, rep = self.run_tool(api)
        self.assertEqual(code, 2)
        self.assertEqual(len(api.calls), 1)
        self.assertIn("rejected the paper keys", rep["stopped"])

    def test_clock_unreadable_stops_no_date_assumed(self):
        api = FakeApi(clock_status=503)
        code, rep = self.run_tool(api)
        self.assertEqual(code, 3)
        self.assertEqual(len(api.calls), 1)
        self.assertIn("no date is assumed", rep["stopped"])

    def test_malformed_expiration_date_is_a_data_finding(self):
        code, rep = self.run_tool(FakeApi(malformed=True))
        a = rep["checks"]["a_contracts"]
        self.assertEqual(a["status"], "PARTIAL")
        self.assertEqual(a["facts"]["SPY"]["window"]["malformed_expiration_date"], 1)
        self.assertEqual(rep["checks"]["c_bars"]["status"], "PASS")              # the rest still runs

    def test_data_error_in_a_check_is_a_fail_not_a_stop(self):
        orig = tool.check_bars
        tool.check_bars = lambda *a, **k: (_ for _ in ()).throw(ValueError("bad field 't': 'xx'"))
        try:
            code, rep = self.run_tool(FakeApi())
        finally:
            tool.check_bars = orig
        self.assertEqual(rep["checks"]["c_bars"]["status"], "FAIL")
        self.assertIn("ValueError: bad field", rep["checks"]["c_bars"]["facts"]["data_error"])
        self.assertIsNone(rep["stopped"])
        self.assertEqual(rep["checks"]["f_fred"]["status"], "PARTIAL")           # later checks ran
        self.assertEqual(code, 1)

    def test_fred_errors_distinguished(self):
        code, rep = self.run_tool(FakeApi(fred_auth_error=True))
        f = rep["checks"]["f_fred"]
        self.assertEqual(f["status"], "FAIL")
        self.assertEqual(f["facts"]["VIXCLS"]["result"], "error")
        self.assertIn("api_key is not registered", f["facts"]["VIXCLS"]["fred_error"])
        self.assertNotIn(FKEY, self.raw)

    def test_fred_key_read_error_is_not_no_key(self):
        self.broken_fred = True
        code, rep = self.run_tool(FakeApi())
        self.assertIn("could not be read from the Credential Manager (OSError)", rep["checks"]["f_fred"]["facts"]["note"])

    def test_429_stops_everything(self):
        api = FakeApi(rate_limit_path="/v1beta1/options/snapshots")
        code, rep = self.run_tool(api)
        self.assertEqual(code, 4)
        self.assertIn("HTTP 429", rep["stopped"])
        self.assertEqual(self.lines[-1], "RESULT: STOPPED (rate limit)")
        self.assertEqual(rep["checks"]["f_fred"]["status"], "FAIL")
        i = next(k for k, (_m, u, _h) in enumerate(api.calls) if "/snapshots/" in u)
        self.assertEqual(len(api.calls), i + 1)                             # nothing after the 429
        self.assertTrue(any(l.startswith("STOP HTTP 429") for l in self.lines))

    def test_missing_fields_partial_not_filled(self):
        code, rep = self.run_tool(FakeApi(missing=("oi", "greeks")))
        self.assertEqual(rep["checks"]["a_contracts"]["status"], "PARTIAL")
        self.assertEqual(rep["checks"]["a_contracts"]["facts"]["SPY"]["window"]["open_interest_present_pct"], 0.0)
        self.assertEqual(rep["checks"]["b_snapshots"]["status"], "PARTIAL")
        self.assertEqual(rep["checks"]["b_snapshots"]["facts"]["QQQ"]["implied_volatility_present_pct"], 0.0)

    def test_market_closed_is_limited(self):
        code, rep = self.run_tool(FakeApi(open_=False))
        self.assertEqual(rep["checks"]["b_snapshots"]["status"], "PARTIAL")
        self.assertIn("market closed", rep["checks"]["b_snapshots"]["facts"]["SPY"]["note_market_closed"])
        self.assertIn("CLOSED", self.lines[0])

    def test_keys_missing_and_no_fred_key(self):
        self.store.values.pop(secrets.target("fred-api-key"))
        code, rep = self.run_tool(FakeApi())
        self.assertEqual(rep["checks"]["f_fred"]["status"], "FAIL")
        self.assertIn("no FRED key", rep["checks"]["f_fred"]["facts"]["note"])
        self.assertEqual(code, 1)
        shutil.rmtree(self.tmp)
        os.makedirs(self.tmp)
        self.store.values.pop(secrets.target("alpaca-paper-secret-key"))
        api = FakeApi()
        self.assertEqual(self.run_tool(api)[0], 2)
        self.assertEqual(api.calls, [])

    def test_bad_args(self):
        self.assertEqual(self.run_tool(FakeApi(), ["--trade"])[0], 1)


class Flaky:
    """Wraps FakeApi: the first ``times`` requests whose path starts with ``path`` raise ``exc``; records timeouts."""

    def __init__(self, api, path, exc, times=1):
        self.api, self.path, self.exc, self.left, self.timeouts = api, path, exc, times, []

    def __call__(self, method, url, headers, timeout):
        self.timeouts.append((urlparse(url).path, timeout))
        if urlparse(url).path.startswith(self.path) and self.left > 0:
            self.left -= 1
            raise self.exc
        return self.api(method, url, headers, timeout)


class NetworkRobustnessTest(Base):
    """User runs 2026-10-23: contracts page timed out at 20 s; "URLError" alone was not diagnosable."""

    def test_contract_pages_at_most_1000_progress_and_timeouts(self):
        api = FakeApi()
        flaky = Flaky(api, "/nothing", None, times=0)
        code, rep = self.run_tool(flaky)
        self.assertEqual(code, 0, self.lines)
        limits = [parse_qs(urlparse(u).query)["limit"][0] for _m, u, _h in api.calls
                  if urlparse(u).path == "/v2/options/contracts"]
        self.assertTrue(limits and all(int(x) <= 1000 for x in limits), limits)
        self.assertEqual(rep["requests"]["contract_page_limit"], 1000)
        self.assertTrue(any("page 1" in ln and "contracts" in ln for ln in self.lines), self.lines[:5])
        self.assertTrue(any("(last page)" in ln for ln in self.lines))
        t = dict(flaky.timeouts)
        self.assertEqual((t["/v2/clock"], t["/v2/options/contracts"]), (20.0, 60.0))
        self.assertEqual(t["/v1beta1/options/bars"], 60.0)
        self.assertEqual(rep["requests"]["retried"], [])

    def test_one_retry_after_a_timeout_counted_and_reported(self):
        api = FakeApi()
        code, rep = self.run_tool(Flaky(api, "/v2/options/contracts", TimeoutError("timed out")))
        self.assertIn(code, (0, 1), self.lines)                        # the run continued
        self.assertIsNone(rep["stopped"])
        r = rep["requests"]["retried"]
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["path"], r[0]["reason"]), ("/v2/options/contracts", "TimeoutError: timed out"))
        sent = sum(1 for _m, u, _h in api.calls if urlparse(u).path == "/v2/options/contracts")
        self.assertEqual(rep["requests"]["by_endpoint"]["contracts"], sent + 1)    # the failed attempt counts
        self.assertIn(tool.RETRY_PAUSE_SECONDS, self.sleeps)
        self.assertTrue(any(ln.strip().startswith("retrying") for ln in self.lines))

    def test_second_failure_stops(self):
        import urllib.error
        api = FakeApi()
        code, rep = self.run_tool(Flaky(api, "/v2/options/contracts",
                                        urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer")),
                                        times=2))
        self.assertEqual(code, 3)
        self.assertIn("network error on paper-api.alpaca.markets/v2/options/contracts: ConnectionResetError: "
                      "[Errno 104] Connection reset by peer (after 1 retry)", rep["stopped"])
        self.assertEqual(len(rep["requests"]["retried"]), 1)

    def test_not_retryable_errors_stop_at_once_with_a_sanitized_reason(self):
        import socket
        import ssl
        import urllib.error
        api = FakeApi()
        code, rep = self.run_tool(Flaky(api, "/v2/clock", urllib.error.URLError(socket.gaierror(11001,
                                                                                               "getaddrinfo failed"))))
        self.assertEqual(code, 3)
        self.assertIn("/v2/clock: gaierror: [Errno 11001] getaddrinfo failed", rep["stopped"])
        self.assertEqual(rep["requests"]["retried"], [])
        e = urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate "
                                                                  "verify failed: unable to get local issuer"))
        self.assertTrue(tool.sanitized_reason(e).startswith("SSLCertVerificationError: [SSL: "
                                                            "CERTIFICATE_VERIFY_FAILED]"), tool.sanitized_reason(e))

    def test_url_with_api_key_in_the_reason_never_leaks(self):
        import urllib.error
        api = FakeApi()
        leak = urllib.error.URLError(OSError(f"failed https://api.stlouisfed.org/fred/series?series_id=VIXCLS"
                                             f"&api_key={FKEY} then api_key={FKEY} and ?api_key={FKEY}"))
        code, rep = self.run_tool(Flaky(api, "/fred/series", leak))
        self.assertEqual(code, 3)
        out = self.raw + "\n".join(self.lines)
        self.assertNotIn(FKEY, out)
        self.assertNotIn("api_key", out)
        self.assertIn("OSError: failed [url]", rep["stopped"])
        self.assertLessEqual(len(tool.sanitized_reason(urllib.error.URLError(OSError("x" * 500)))), tool.REASON_MAX)
        self.assertEqual(tool.sanitized_reason(ValueError(f"secret {FKEY}")), "ValueError")   # type only


class TransportTest(unittest.TestCase):
    def test_no_redirect_and_size_limit(self):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/big":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"x" * 64)
                    return
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        status, _h, _b = tool.fetch_with(tool.build_opener(None), "GET", base + "/redirect", {}, 5)
        self.assertEqual(status, 302)                                     # not followed, not 200
        old = tool.MAX_BODY_BYTES
        tool.MAX_BODY_BYTES = 10
        try:
            with self.assertRaises(tool.Stop):
                tool.fetch_with(tool.build_opener(None), "GET", base + "/big", {}, 5)
        finally:
            tool.MAX_BODY_BYTES = old


class ArithmeticTest(unittest.TestCase):
    def test_budget(self):
        b = tool.budget({"SPY": 2, "QQQ": 1}, {"SPY": 3, "QQQ": 2}, per_day=(1, 28), share_per_min=30)
        self.assertEqual((b["eod_snapshot_requests"], b["intraday_snapshot_requests"]), (8, 5))
        self.assertEqual([r["requests_per_day"] for r in b["per_day"]], [13, 148])
        self.assertEqual(b["per_day"][1]["archive_minutes_in_rth"], 4.7)            # 140 req / 30 per min
        self.assertEqual(b["per_day"][1]["rth_share_pct"], 1.2)
        self.assertEqual(b["intraday_minutes_at_share"], 0.17)
        self.assertTrue(b["one_snapshot_within_one_minute"])
        self.assertEqual((b["backend_share_per_min"], b["archive_share_per_min"], b["split_total_per_min"],
                          b["alpaca_limit_per_min"]), (150, 30, 190, 200))
        self.assertIn(tool.PROPOSED_CADENCE, tool.INTRADAY_PER_DAY)

    def test_backend_gate_matches_the_split(self):
        from strategy_platform.data import http_gate
        self.assertEqual(http_gate.DEFAULT_RATES_PER_MINUTE["alpaca"], tool.BACKEND_SHARE_PER_MIN)
        self.assertLessEqual(tool.BACKEND_SHARE_PER_MIN + http_gate.DEFAULT_CAPACITY + tool.ARCHIVE_SHARE_PER_MIN,
                             tool.ALPACA_LIMIT_PER_MIN - 10)

    def test_storage(self):
        s = tool.storage(1000, per_day=(13,), row_bytes={"snapshot": 200, "oi": 100})
        r = s["per_day"][0]
        self.assertEqual(r["rows_per_day"], 1000 * 14 + 1000)
        self.assertEqual(r["mb_per_day"], 2.9)                  # 14 × 1000 × 200 + 1000 × 100 = 2.9 MB
        self.assertEqual(r["mb_per_month"], 60.9)
        self.assertEqual(r["mb_per_year"], 730.8)

    def test_helpers(self):
        self.assertEqual(tool.occ_parts("SPY240216C00500000"), ("SPY", date(2024, 2, 16), "C", 500.0))
        self.assertIsNone(tool.occ_parts("SPY"))
        c = {"strike_price": "660", "expiration_date": "2026-11-20"}
        self.assertTrue(tool.in_filter(c, 600.0, 10.0, 45, date(2026, 10, 23)))
        self.assertFalse(tool.in_filter(c, 600.0, 5.0, 45, date(2026, 10, 23)))
        self.assertFalse(tool.in_filter(c, None, 10.0, 45, date(2026, 10, 23)))


if __name__ == "__main__":
    unittest.main()
