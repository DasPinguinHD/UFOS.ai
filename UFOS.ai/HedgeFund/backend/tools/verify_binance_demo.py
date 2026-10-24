"""Manual verification against the REAL Binance Spot DEMO MODE (Strategy Platform SPEC §18.14 item 11).

Run on Windows (from HedgeFund\\backend):   .\\.venv\\Scripts\\python.exe tools\\verify_binance_demo.py
Optional round trip (places ONE tiny order):  … tools\\verify_binance_demo.py --place-test-order

DEMO ONLY — no flag changes that: the client is built by the adapter itself (``BinanceDemoAdapter.exchange``: ccxt
``enable_demo_trading(True)``, never sandbox / Testnet; ``urls['api']`` cut to the spot APIs, each asserted on
``demo-api.binance.com``; every request's URL asserted again before it is sent). The script checks the URLs once
more before the first request and stops with exit code 3 when any is not the demo host. Keys come from the Windows
Credential Manager (``binance-demo-api-key`` / ``binance-demo-api-secret``) and are never printed.

Read-only by default: ccxt version → demo endpoint → URL guard self-test (a live URL is refused, nothing sent) →
server time (clock skew) → markets (exchangeInfo: BASE/USDT count, BTC/USDT filters) → balances (non-zero, as
reported — USDT, not converted) → open orders (platform ``UFOS_`` vs others).
``--place-test-order``: ONE LIMIT buy of BTC/USDT at about 50 % below the last price (cannot fill), sized just above
the NOTIONAL minimum, client id ``UFOSV_<hex>`` (NOT the platform's ``UFOS_`` prefix, so the platform never treats it
as its own) → looked up by that id → cancelled → confirmed cancelled. Refused (exit 4) while binance-demo has
working platform orders or when that cannot be checked. On any error after the order was placed it tries to cancel.

Exit codes: 0 RESULT: OK · 1 RESULT: FAIL (n) · 2 keys missing / unreadable · 3 not the demo endpoint ·
4 test order refused · 5 not run with the backend venv (.venv\\Scripts\\python.exe). Part of UFOS.ai, a proof-of-concept portfolio project; DEMO TRADING ONLY (simulated funds).
"""
from __future__ import annotations

import math
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, List, Optional

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
if __name__ == "__main__":              # S10-H: refuse the system Python before any non-stdlib import
    from strategy_platform.core import venv_guard
    venv_guard.require("verify_binance_demo.py", BACKEND_DIR, 5)

SYMBOL = "BTC-USD"                    # → BTC/USDT
EXPECTED_CCXT = "4.5.84"
PRICE_FACTOR = 0.5                    # a buy 50 % below the market cannot fill
NOTIONAL_MARGIN = 1.2                 # 20 % above the NOTIONAL minimum
VERIFY_PREFIX = "UFOSV_"              # not "UFOS_": never the platform's order
CONFIRM_POLLS = 10
POLL_SECONDS = 1.0
KEYS_HINT = ("Create an API key at demo.binance.com → API Management (system-generated / HMAC), enter it in Hedge "
             "Fund → ⚙ Keys & data → Keys (binance-demo-api-key / binance-demo-api-secret), then run this again.")


class Report:
    def __init__(self, out: Callable[[str], None], secrets_to_hide: List[str]):
        self.out = out
        self.hide = [s for s in secrets_to_hide if s]
        self.fails = 0

    def clean(self, text: str) -> str:
        text = " ".join(str(text).split())
        for s in self.hide:
            text = text.replace(s, "***")
        return text[:300]

    def line(self, status: str, step: str, reason: str = "") -> None:
        if status == "FAIL":
            self.fails += 1
        self.out(self.clean(f"{status:<4} {step}" + (f": {reason}" if reason else "")))


def err_line(ex: BaseException) -> str:
    return f"{type(ex).__name__}: {getattr(ex, 'user_message', None) or str(ex) or ''}"


def working_platform_orders() -> Optional[int]:
    """How many platform orders work on binance-demo (None: cannot be checked)."""
    try:
        from strategy_platform.execution import oms
        return len(oms.list_orders("binance-demo", states=oms.WORKING, limit=1000))
    except Exception:            # noqa: BLE001
        return None


def run(argv: Optional[List[str]] = None, adapter=None, out: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep, secrets_module=None,
        working_fn: Callable[[], Optional[int]] = working_platform_orders) -> int:
    """The verification (see the module docstring). Returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    unknown = [a for a in argv if a != "--place-test-order"]
    if unknown:
        out(f"usage: verify_binance_demo.py [--place-test-order]  (unknown: {' '.join(unknown)[:80]})")
        return 1
    place = "--place-test-order" in argv
    from strategy_platform.execution.brokers import binance_demo, live_guard
    from strategy_platform.execution.brokers.common import BrokerNotInstalled
    sec = secrets_module
    if sec is None:
        from strategy_platform import secrets as sec
    try:
        key = sec.get_secret(binance_demo.KEY_SECRET)
        secret = sec.get_secret(binance_demo.SECRET_SECRET)
    except Exception as ex:          # noqa: BLE001 — Credential Manager unreadable (or not Windows)
        out(f"FAIL keys: the Credential Manager could not be read ({type(ex).__name__}). {KEYS_HINT}")
        out("RESULT: FAIL (1)")
        return 2
    rep = Report(out, [key or "", secret or ""])
    if not key or not secret:
        missing = [n for n, v in ((binance_demo.KEY_SECRET, key), (binance_demo.SECRET_SECRET, secret)) if not v]
        rep.line("FAIL", "keys", f"missing {', '.join(missing)}. {KEYS_HINT}")
        out("RESULT: FAIL (1)")
        return 2
    rep.line("INFO", "live guard", f"BUILD_ALLOWS_LIVE={live_guard.BUILD_ALLOWS_LIVE} — demo only, no live endpoint")
    rep.line("INFO", "mode", "read-only" + (" + ONE test order (--place-test-order)" if place else ""))
    # 1. ccxt
    try:
        ccxt = adapter._sdk if adapter is not None and adapter._sdk is not None else binance_demo.load_sdk()
        ver = getattr(ccxt, "__version__", None)
        rep.line("OK" if ver == EXPECTED_CCXT else "INFO", "1 ccxt",
                 f"version {ver}" + (" (the pin)" if ver == EXPECTED_CCXT else f" (the pin is {EXPECTED_CCXT})"))
    except BrokerNotInstalled as ex:
        rep.line("FAIL", "1 ccxt", ex.user_message)
        out(f"RESULT: FAIL ({rep.fails})")
        return 1
    if adapter is None:
        adapter = binance_demo.BinanceDemoAdapter(secrets_module=sec)
    # 2. demo endpoint — hard stop BEFORE any request
    try:
        ex_ = adapter.exchange()
        live_guard.assert_paper_urls(binance_demo.BROKER, list(ex_.urls["api"].values()))
        rep.line("OK", "2 demo endpoint", f"spot APIs {', '.join(sorted(ex_.urls['api']))} on demo-api.binance.com")
    except live_guard.LiveTradingDisabled as ex:
        rep.line("FAIL", "2 demo endpoint", f"refused — {ex.user_message} ({ex.detail})")
        out(f"RESULT: FAIL ({rep.fails})")
        return 3
    except Exception as ex:          # noqa: BLE001
        rep.line("FAIL", "2 demo endpoint", err_line(ex))
        out(f"RESULT: FAIL ({rep.fails})")
        return 1
    # 3. URL guard self-test (nothing is sent)
    try:
        ex_.fetch("https://" + "api." + "binance.com/api/v3/time")      # a live URL — must be refused
        rep.line("FAIL", "3 URL guard", "a live URL was NOT refused")
    except live_guard.LiveTradingDisabled:
        rep.line("OK", "3 URL guard", "a live URL is refused before sending")
    except Exception as ex:          # noqa: BLE001
        rep.line("FAIL", "3 URL guard", err_line(ex))
    steps = [("4 server time", lambda: adapter.server_time(),
              lambda v: f"{v['server_time']} (local clock {'ahead' if v['skew_ms'] >= 0 else 'behind'} by "
                        f"{abs(v['skew_ms'])} ms)"),
             ("5 markets", lambda: (adapter.usdt_markets(), adapter.instrument(SYMBOL)),
              lambda v: f"{v[0]} trading BASE/USDT spot markets; BTC/USDT step {v[1]['qty_step']}, min "
                        f"{v[1]['min_qty']}, tick {v[1]['tick_size']}, NOTIONAL min {v[1]['min_notional']} USDT"),
             ("6 balances", lambda: adapter.balances(),
              lambda v: ", ".join(f"{a} {x['total']:g}" for a, x in sorted(v.items()))[:250] or "none"),
             ("7 open orders", lambda: (len(adapter.open_orders()), adapter.last_foreign_open),
              lambda v: f"{v[0]} with the platform's UFOS_ prefix, {v[1]} other")]
    for name, fn, show in steps:
        try:
            rep.line("OK", name, show(fn()))
        except Exception as ex:      # noqa: BLE001
            rep.line("FAIL", name, err_line(ex))
    if place and rep.fails == 0:
        code = _round_trip(adapter, ex_, rep, sleep, working_fn)
        if code == 4:
            out(f"RESULT: FAIL ({rep.fails})")
            return 4
    elif place:
        rep.line("INFO", "8 test order", "skipped — fix the failed read-only checks first")
    out("RESULT: OK" if rep.fails == 0 else f"RESULT: FAIL ({rep.fails})")
    return 0 if rep.fails == 0 else 1


def _round_trip(adapter, ex, rep: Report, sleep, working_fn) -> int:
    n = working_fn()
    if n is None or n > 0:
        rep.line("FAIL", "8 test order", "refused — binance-demo has working platform orders" if n else
                 "refused — the platform's working orders could not be checked (is the backend database readable?)")
        return 4
    m = adapter.market(SYMBOL)
    f = adapter.filters(m)
    last = adapter.last_price(m)
    tick = f["tick"] or 0.01
    price = max(f["min_price"] or tick, math.floor(last * PRICE_FACTOR / tick) * tick)
    price = round(price, max(0, -int(math.floor(math.log10(tick)))))
    step = f["step"] or 0.00001
    need = (f["min_notional"] or 5.0) * NOTIONAL_MARGIN / price
    qty = max(f["min_qty"] or step, math.ceil(need / step) * step)
    qty = round(qty, max(0, -int(math.floor(math.log10(step)))))
    coid = VERIFY_PREFIX + uuid.uuid4().hex[:16]
    placed = coid                    # from here on an order may exist: the finally block cancels it
    try:
        adapter._read(lambda: ex.create_order(m["symbol"], "limit", "buy", qty, price,
                                                       {"newClientOrderId": coid, "timeInForce": "GTC"}), "test order")
        rep.line("OK", "8 test order placed", f"LIMIT buy {qty:g} BTC @ {price:g} USDT ({qty * price:,.2f} USDT), "
                                              f"id {coid}")
        found = adapter._read(lambda: ex.fetch_order(None, m["symbol"], {"origClientOrderId": coid}), "lookup")
        rep.line("OK" if (found or {}).get("clientOrderId") == coid else "FAIL", "9 lookup by client id",
                 f"status {(found or {}).get('status')}")
        adapter._read(lambda: ex.cancel_order(None, m["symbol"], {"origClientOrderId": coid}), "cancel")
        st = None
        for _k in range(CONFIRM_POLLS):
            st = (adapter._read(lambda: ex.fetch_order(None, m["symbol"], {"origClientOrderId": coid}), "lookup")
                  or {}).get("status")
            if st in ("canceled", "cancelled"):
                break
            sleep(POLL_SECONDS)
        rep.line("OK" if st in ("canceled", "cancelled") else "FAIL", "10 cancelled", f"status {st}")
        placed = None
    except Exception as ex_:         # noqa: BLE001
        rep.line("FAIL", "8-10 test order", err_line(ex_))
    finally:
        if placed is not None:
            try:
                ex.cancel_order(None, m["symbol"], {"origClientOrderId": coid})
                rep.line("INFO", "cleanup", f"cancel sent for {coid}")
            except Exception as ex2:     # noqa: BLE001
                if type(ex2).__name__ == "OrderNotFound":
                    rep.line("INFO", "cleanup", f"{coid} was not placed")
                else:
                    rep.line("FAIL", "cleanup", f"cancel {coid} at demo.binance.com by hand ({err_line(ex2)})")
    return 0


if __name__ == "__main__":
    sys.exit(run())
