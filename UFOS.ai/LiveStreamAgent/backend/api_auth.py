"""Access control for this backend's local HTTP API and WebSocket server.

Security-by-default rule (CLAUDE.md, 2026-09-25). Each module keeps its own
copy of this file (compartmentalization) — keep the copies in sync:
Global Monitoring/backend, HedgeFund/backend, LiveStreamAgent/backend.

1. Loopback by default — bind_host() returns 127.0.0.1 unless the user opts
   in with ALLOW_REMOTE_ACCESS=true in .env. An old .env that still says
   WS_HOST=0.0.0.0 is therefore ignored (with a warning) instead of silently
   exposing the backend to the local network.
2. Per-start session token — init_token() creates a fresh random token at
   every backend start; publish_token() writes it to data/api-token
   (git-ignored) once the servers are bound. The app
   reads that file (Shared/BackendAuth.cs) and sends it as the X-UFOS-Token
   header. Every HTTP endpoint except /health and /version and every
   WebSocket connection requires it. Browsers can't set headers on a
   WebSocket, so the WebSocket also accepts ?token=… (never logged here).
3. Origin check — requests sent by a web page carry an Origin header. Only
   the origins this module expects are accepted (Global Monitoring: its
   WebView page; the others: none), so a random website open in the user's
   browser can't call the API — not even "simple" requests that skip the
   CORS preflight. Native clients (the WPF app) send no Origin.

Residual risks (documented on purpose): the token file is readable by any
program running as the same Windows user; with ALLOW_REMOTE_ACCESS=true the
traffic (incl. the token) is unencrypted http/ws — only use it on a trusted
network.
"""
import hmac
import logging
import os
import secrets
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-UFOS-Token"
TOKEN_QUERY = "token"
TOKEN_FILE = Path(__file__).resolve().parent / "data" / "api-token"
PUBLIC_PATHS = frozenset({"/health", "/version"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_token = None


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def remote_access_allowed() -> bool:
    return _truthy(os.getenv("ALLOW_REMOTE_ACCESS"))


def bind_host(configured, what: str) -> str:
    """The address a server may listen on: loopback unless ALLOW_REMOTE_ACCESS=true."""
    host = str(configured or "").strip() or "127.0.0.1"
    if host in LOOPBACK_HOSTS:
        return host
    if remote_access_allowed():
        logger.warning("%s listens on %s (ALLOW_REMOTE_ACCESS=true): reachable from other machines. "
                       "Traffic is NOT encrypted (plain http/ws) — use only on a trusted network. "
                       "The session token is still required.", what, host)
        return host
    logger.warning("%s: configured host %s ignored — listening on 127.0.0.1 only. "
                   "Set ALLOW_REMOTE_ACCESS=true in .env to expose it on the network.", what, host)
    return "127.0.0.1"


def allowed_origins(default=()) -> frozenset:
    """Origins allowed to call this backend from a web page (env CORS_ALLOWED_ORIGINS overrides)."""
    raw = os.getenv("CORS_ALLOWED_ORIGINS")
    if raw is None:
        return frozenset(default)
    return frozenset(o.strip() for o in raw.split(",") if o.strip())


def init_token() -> str:
    """Create this start's token (in memory). Call publish_token() once the
    servers are bound, so a second instance that fails to bind its port can't
    overwrite the token file of the backend that is actually running."""
    global _token
    _token = secrets.token_urlsafe(32)
    return _token


def publish_token(path=None) -> bool:
    """Write the current token to data/api-token (atomically) for the app."""
    target = Path(path) if path else TOKEN_FILE
    if not _token:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(_token)
        os.replace(tmp, target)
        return True
    except OSError as ex:
        # The API stays locked (no client can know the token) rather than open.
        logger.error("Could not write the API token file %s: %s — the app won't be able to connect.", target, ex)
        return False


def token():
    return _token


def token_matches(candidate) -> bool:
    if not _token or not candidate:
        return False
    return hmac.compare_digest(str(candidate).encode("utf-8"), _token.encode("utf-8"))


def _query_token(path: str) -> str:
    try:
        return (parse_qs(urlsplit(path).query).get(TOKEN_QUERY) or [""])[0]
    except Exception:
        return ""


def check(method: str, path: str, headers, origins: frozenset, websocket: bool = False):
    """(status, message) when the request must be refused, else None. Pure — unit tested."""
    origin = headers.get("Origin")
    if origin is not None and origin not in origins:
        return HTTPStatus.FORBIDDEN, "origin not allowed"
    if not websocket and (method == "OPTIONS" or urlsplit(path).path in PUBLIC_PATHS):
        return None
    supplied = headers.get(TOKEN_HEADER) or (_query_token(path) if websocket else "")
    if not token_matches(supplied):
        return HTTPStatus.UNAUTHORIZED, "missing or invalid API token"
    return None


def cors_headers(origin, origins: frozenset) -> dict:
    """CORS response headers for an allowed page origin (none for native clients)."""
    if origin is None or origin not in origins:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, " + TOKEN_HEADER,
        "Access-Control-Max-Age": "3600",
        "Vary": "Origin",
    }


def http_middleware(origins: frozenset):
    """aiohttp middleware: origin + token check, CORS headers for allowed page origins."""
    from aiohttp import web

    @web.middleware
    async def _auth(request, handler):
        origin = request.headers.get("Origin")
        extra = cors_headers(origin, origins)
        refused = check(request.method, request.path_qs, request.headers, origins)
        if refused:
            status, message = refused
            logger.warning("Refused %s %s from %s: %s (origin %s)", request.method, request.path,
                           request.remote, message, origin or "-")
            return web.json_response({"error": message}, status=int(status), headers=extra)
        if request.method == "OPTIONS":
            return web.Response(headers=extra)
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            # Raised HTTP errors (404 …) keep the CORS headers, so the page sees the status.
            for key, value in extra.items():
                ex.headers[key] = value
            raise
        for key, value in extra.items():
            response.headers[key] = value
        return response

    return _auth


def ws_process_request(origins: frozenset):
    """process_request hook for websockets.serve (websockets >= 14 API)."""
    def _process_request(connection, request):
        refused = check("GET", request.path, request.headers, origins, websocket=True)
        if refused:
            status, message = refused
            logger.warning("Refused WebSocket connection from %s: %s (origin %s)",
                           getattr(connection, "remote_address", "?"), message,
                           request.headers.get("Origin") or "-")
            return connection.respond(status, message + "\n")
        return None
    return _process_request
