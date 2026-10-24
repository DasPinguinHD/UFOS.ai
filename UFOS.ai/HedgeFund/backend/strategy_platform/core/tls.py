"""One TLS client context for the HedgeFund backend's HTTP collectors (click-test 2026-10-22).

Cause of "SSLCertVerificationError: unable to get local issuer certificate" for ec.europa.eu (Eurostat): Python's
``ssl.create_default_context()`` on Windows reads only the root certificates ALREADY installed in the Windows
store; Windows installs most trusted roots on demand (automatic root update through CryptoAPI, e.g. when a browser
first meets them), which Python never triggers — so a root no browser on this PC has used yet is missing. (An
incomplete chain sent by the server would give the same error; it could not be checked from the build machine.
If that is the cause, certifi does not help: the collector status then shows the verification error — no
workaround, verification is never disabled.)

``client_context()`` = the system store PLUS the Mozilla root bundle of ``certifi`` (requirements.txt). Certificate
and host-name verification stay ON (``CERT_REQUIRED``, ``check_hostname``) — never disabled.
"""
from __future__ import annotations

import ssl
import threading

_lock = threading.Lock()
_ctx = None


def client_context() -> ssl.SSLContext:
    """The shared verifying client context (system roots + certifi roots)."""
    global _ctx
    with _lock:
        if _ctx is None:
            import certifi
            ctx = ssl.create_default_context()          # system store, CERT_REQUIRED, check_hostname
            ctx.load_verify_locations(cafile=certifi.where())
            # explicit checks (not ``assert``: ``python -O`` would strip it) — never a non-verifying context
            if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
                raise ssl.SSLError("the TLS client context does not verify certificates and host names — refused")
            _ctx = ctx
        return _ctx
