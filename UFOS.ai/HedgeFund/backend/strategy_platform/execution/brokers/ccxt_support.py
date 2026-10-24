"""Shared ccxt plumbing of the crypto demo adapters (SPEC §18.3, §18.13, §18.14): the lazy optional import (pinned
``ccxt==4.5.84`` in requirements-broker.txt, its own pip step), the mapping of ccxt exception classes, SAFE response
metadata for diagnostics, and ``harden_exchange`` (every request URL checked by the live guard before it is sent;
JSON text parsed; anything else → ``BrokerFormatError`` with shape + HTTP metadata, never a body, query or id).

Written for the Binance Spot demo (stage 8b); the retired Kraken demo adapter was removed in stage 10 (S10-K).
Part of UFOS.ai, a proof-of-concept portfolio project; PAPER / DEMO TRADING ONLY.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Optional
from urllib.parse import urlparse

from . import live_guard
from .common import BrokerFormatError, BrokerNotInstalled, shape_of

logger = logging.getLogger(__name__)

NOT_INSTALLED = ("ccxt is not installed in the HedgeFund backend environment (restart the app so it installs "
                 "requirements-broker.txt)")
OPTIONAL_STATUS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))), ".venv", "ufos-optional-status.txt")

_SDK = None
_SDK_LOCK = threading.Lock()


def install_failure(path: Optional[str] = None) -> Optional[str]:
    """The reason recorded by run_hedgefund_backend.ps1 when its separate ccxt install failed
    (``.venv/ufos-optional-status.txt`` = "failed <hash> <utc> <reason>"), else None."""
    try:
        with open(path or OPTIONAL_STATUS_FILE, encoding="ascii", errors="replace") as fh:
            line = fh.readline(1000).strip()
    except OSError:
        return None
    parts = line.split(" ", 3)
    if len(parts) < 3 or parts[0] != "failed":
        return None
    reason = parts[3] if len(parts) == 4 else "no reason recorded"
    return "".join(c if 32 <= ord(c) < 127 else " " for c in reason)[:280].strip() or "no reason recorded"


def load_sdk(label: str = "Crypto"):
    global _SDK
    with _SDK_LOCK:
        if _SDK is None:
            try:
                import ccxt                      # noqa: F401 — lazy, optional (requirements-broker.txt)
            except ImportError as ex:
                why = install_failure()
                if why:
                    raise BrokerNotInstalled(f"{label} adapter unavailable: ccxt could not be installed ({why})") \
                        from ex
                raise BrokerNotInstalled(NOT_INSTALLED) from ex
            _SDK = ccxt
        return _SDK


def sdk_info(label: str = "Crypto") -> dict:
    try:
        m = load_sdk(label)
    except BrokerNotInstalled as ex:
        return {"name": "ccxt", "installed": False, "version": None, "message": ex.user_message}
    return {"name": "ccxt", "installed": True, "version": getattr(m, "__version__", None), "message": None}


def ccxt_kind(ex: BaseException) -> str:
    """duplicate | auth | not_found | refused | rate_limit | transient | other — by the ccxt class hierarchy."""
    names = [c.__name__ for c in type(ex).__mro__]
    if "DuplicateOrderId" in names:       # a subclass of InvalidOrder — the id may already be there
        return "duplicate"
    for n in ("AuthenticationError", "PermissionDenied", "AccountSuspended"):
        if n in names:
            return "auth"
    if "OrderNotFound" in names:
        return "not_found"
    for n in ("InsufficientFunds", "InvalidOrder", "BadSymbol", "BadRequest", "ArgumentsRequired", "NotSupported"):
        if n in names:
            return "refused"
    for n in ("RateLimitExceeded", "DDoSProtection"):
        if n in names:
            return "rate_limit"
    for n in ("RequestTimeout", "NetworkError", "ExchangeNotAvailable", "OnMaintenance"):
        if n in names:
            return "transient"
    if isinstance(ex, (TimeoutError, ConnectionError, OSError)):
        return "transient"
    return "other"


def short_msg(ex: BaseException) -> str:
    return " ".join(str(ex).split())[:200]


def url_path(url) -> str:
    try:
        return urlparse(str(url)).path[:80]
    except ValueError:
        return "?"


HTML_HINTS = (("cf-chl", "a Cloudflare challenge page?"), ("challenge-platform", "a Cloudflare challenge page?"),
              ("captcha", "a captcha page?"), ("<form", "a login / form page?"))


def response_meta(resp) -> dict:
    """SAFE metadata of an HTTP response (no body, no query, no ids): status, content type, server, the final path
    and host and the redirect count."""
    try:
        hist = list(getattr(resp, "history", None) or [])
        headers = getattr(resp, "headers", None) or {}
        return {"status": getattr(resp, "status_code", None),
                "content_type": re.sub(r"[^A-Za-z0-9/+.;= -]", "", str(headers.get("Content-Type") or ""))[:40] or None,
                "server": re.sub(r"[^A-Za-z0-9/._ -]", "", str(headers.get("Server") or ""))[:20] or None,
                "final_path": url_path(getattr(resp, "url", "")) or None, "redirects": len(hist),
                "final_host": urlparse(str(getattr(resp, "url", ""))).hostname or None}
    except Exception:            # noqa: BLE001 — metadata only
        return {}


def describe_meta(meta: dict, requested_path: str) -> str:
    if not meta:
        return ""
    parts = [f"HTTP {meta.get('status')}"]
    if meta.get("content_type"):
        parts.append(meta["content_type"].split(";")[0])
    if meta.get("server"):
        parts.append(f"server {meta['server']}")
    if meta.get("redirects"):
        parts.append(f"redirected {meta['redirects']}× to {meta.get('final_host')}{meta.get('final_path')}")
    elif meta.get("final_path") and meta["final_path"] != requested_path:
        parts.append(f"answered for {meta['final_path']}")
    return "; " + ", ".join(parts)


def harden_exchange(ex, broker: str, format_msg: str, log_label: str) -> None:
    """Wraps the exchange's ``fetch``: the FULL URL of every request is asserted by the live guard BEFORE it is sent and
    redirects are never followed (``allow_redirects=False``; a 3xx → ``BrokerFormatError`` with status + Location host);
    a text body that is JSON is parsed with ccxt's own ``parse_json``; anything else raises ``BrokerFormatError``
    with the SAFE shape and response metadata (captured from the requests session) and a hint for challenge /
    login pages."""
    orig = ex.fetch
    if getattr(orig, "_ufos_shape_guard", False):
        return
    tl = threading.local()
    session = getattr(ex, "session", None)
    sreq = getattr(session, "request", None)
    if session is not None and callable(sreq) and not getattr(sreq, "_ufos_meta", False):
        def request(*a, **kw):
            # review fix 2026-10-19: redirects are NEVER followed — requests would follow a 30x to another host and keep
            # the API-key header; the guard checked only the first URL. A 3xx is a format error (not retried).
            kw["allow_redirects"] = False
            resp = sreq(*a, **kw)
            tl.meta = response_meta(resp)
            status = getattr(resp, "status_code", None)
            if isinstance(status, int) and 300 <= status < 400:
                loc = str((getattr(resp, "headers", None) or {}).get("Location") or "")
                try:
                    host = urlparse(loc).hostname or ("the same host" if loc.startswith("/") else "no Location")
                except ValueError:
                    host = "an unparsable Location"
                host = re.sub(r"[^A-Za-z0-9.-]", "", host)[:60] if host and " " not in host else host
                logger.warning("%s: redirect HTTP %s to %s refused (not followed)", log_label, status, host)
                raise BrokerFormatError(f"{format_msg}: the server answered with a redirect (HTTP {status} → {host}) — "
                                        "not followed, no request leaves the demo host"[:300])
            return resp
        request._ufos_meta = True
        session.request = request

    def fetch(url, method="GET", headers=None, body=None):
        live_guard.assert_paper_url(broker, url)          # the FULL URL of this request (demo host only)
        tl.meta = {}
        out = orig(url, method, headers, body)
        if isinstance(out, (bytes, bytearray)):
            out_text = bytes(out).decode("utf-8", "replace")
        elif isinstance(out, str):
            out_text = out
        else:
            return out
        text = out_text.lstrip("﻿ \t\r\n")
        if text[:1] in ("{", "["):
            try:
                parse = getattr(ex, "parse_json", None)
                return parse(text) if callable(parse) else json.loads(text)
            except ValueError:
                pass
        path = url_path(url)
        low = out_text[:20000].lower()
        hint = next((h for marker, h in HTML_HINTS if marker in low), None)
        diag = shape_of(out) + describe_meta(getattr(tl, "meta", {}) or {}, path) + \
            (f" — looks like {hint}" if hint else "")
        logger.warning("%s: unexpected response shape for %s %s: %s", log_label, method, path, diag)
        raise BrokerFormatError(f"{format_msg}: unexpected response shape: {diag}"[:300])
    fetch._ufos_shape_guard = True
    ex.fetch = fetch
