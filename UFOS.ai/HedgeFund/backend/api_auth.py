"""Access control for this backend's local HTTP API and WebSocket server.

Security-by-default rule (CLAUDE.md, 2026-09-25). Each module keeps its own
copy of this file (compartmentalization) — keep the copies in sync:
Global Monitoring/backend, HedgeFund/backend, LiveStreamAgent/backend.

1. Loopback only — bind_host() always returns 127.0.0.1. The former
   ALLOW_REMOTE_ACCESS opt-in and the WS_HOST / HTTP_HOST switches existed for
   containers and were removed with Docker (user decision 2026-10-24).
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
4. Host check (stage 10 security audit L2, 2026-10-24) — every HTTP request
   and WebSocket handshake, /health and /version included, must carry
   Host: 127.0.0.1:<port> or localhost:<port>, <port> being the port the
   server actually listens on (read from the socket). Anything else (a DNS-
   rebinding page, another host name) gets 403 "host not allowed"; when the
   port can't be read the request is refused too (fail closed).

Residual risk (documented on purpose): the token file is readable by any
program running as the same Windows user.
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
HOST_REFUSED = "host not allowed"

_token = None


BIND_HOST = "127.0.0.1"


def bind_host(what: str = "server") -> str:
    """The address every server of this backend listens on: 127.0.0.1, always (no remote access since stage 10)."""
    return BIND_HOST


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


def allowed_hosts(port) -> frozenset:
    """Host header values accepted by a server listening on ``port`` (point 4 of the module docstring)."""
    p = int(port)
    return frozenset({f"127.0.0.1:{p}", f"localhost:{p}"})


def host_allowed(host_header, port) -> bool:
    return str(host_header or "").strip().lower() in allowed_hosts(port)


def local_port(transport):
    """The local port of a connection's transport (``get_extra_info("sockname")``), or None."""
    try:
        sock = transport.get_extra_info("sockname") if transport is not None else None
        return int(sock[1]) if sock else None
    except Exception:
        return None


def check(method: str, path: str, headers, origins: frozenset, websocket: bool = False, port=None):
    """(status, message) when the request must be refused, else None. Pure — unit tested. ``port`` (the server's
    listening port) enables the Host check; the middleware / WebSocket hook always pass it."""
    if port is not None and not host_allowed(headers.get("Host"), port):
        return HTTPStatus.FORBIDDEN, HOST_REFUSED
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
        port = local_port(getattr(request, "transport", None))
        refused = ((HTTPStatus.FORBIDDEN, HOST_REFUSED) if port is None else
                   check(request.method, request.path_qs, request.headers, origins, port=port))
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
        port = None
        try:
            addr = getattr(connection, "local_address", None)
            port = int(addr[1]) if addr else None
        except Exception:
            port = None
        if port is None:
            port = local_port(getattr(connection, "transport", None))
        refused = ((HTTPStatus.FORBIDDEN, HOST_REFUSED) if port is None else
                   check("GET", request.path, request.headers, origins, websocket=True, port=port))
        if refused:
            status, message = refused
            logger.warning("Refused WebSocket connection from %s: %s (origin %s)",
                           getattr(connection, "remote_address", "?"), message,
                           request.headers.get("Origin") or "-")
            return connection.respond(status, message + "\n")
        return None
    return _process_request
