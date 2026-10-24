"""News thumbnail proxy (fetch + checks) and image header checks for thumbnails and logos (SPEC §9, MR6).

The WPF never contacts third-party image servers: it asks the backend
(``GET /research/news/images/{key}``), and only the backend fetches — and only URLs that came from Alpaca's
news data and pass ``news_text.safe_image_url`` (https, DNS host name, port 443). Per download:

- the host name must resolve to public (global) addresses only (no loopback / private / link-local);
- no automatic redirects: ≤ ``NEWS_IMAGE_MAX_REDIRECTS`` hops, each target checked like the first URL;
- timeouts ``NEWS_IMAGE_TIMEOUT_SECONDS`` per connect/read and ``NEWS_IMAGE_TOTAL_SECONDS`` overall;
- ≤ ``MAX_NEWS_IMAGE_BYTES`` (Content-Length checked first, then while reading);
- ``Content-Type`` in ``NEWS_IMAGE_TYPES`` (png, jpeg, gif — never SVG, no WebP: review L6) AND the file signature must
  match it; the pixel size from the header ≤ ``MAX_IMAGE_SIDE`` per side;
- through an ``http_gate.HttpGate`` of its own (source ``news_images``, ``NEWS_IMAGES_PER_MINUTE``, one
  attempt) — image hosts are not Alpaca, so the Alpaca budget is not used.

``ImageRejected`` = the answer broke a rule (cached as ``rejected``, not retried); ``ImageFetchFailed`` =
network / HTTP failure (retried after ``NEWS_IMAGE_RETRY_SECONDS``). No cookies, no referrer, no keys are
sent. Residual risk (stated in the settings note): the image servers see the user's IP address.

Stdlib only (http.client, ssl, socket). Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
import struct
import threading
import time as _time
import http.client
from typing import Callable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from strategy_platform.core.errors import UserFacingError

from . import limits
from .news_text import safe_image_url

USER_AGENT = "UFOS.ai-research/1 (news thumbnail proxy; proof-of-concept)"
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}


class ImageRejected(UserFacingError):
    """The image answer broke a rule (type, size, signature, unsafe URL) — not retried."""


class ImageFetchFailed(UserFacingError):
    """The image could not be downloaded (network, timeout, HTTP status) — retried later."""


def sniff(data: bytes) -> Optional[str]:
    """MIME type from the file signature (png, jpeg, gif, webp) or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _jpeg_size(d: bytes) -> Optional[Tuple[int, int]]:
    i, n = 2, len(d)
    while i + 4 <= n:
        if d[i] != 0xFF:
            return None
        while i < n and d[i] == 0xFF:              # fill bytes
            i += 1
        if i >= n:
            return None
        m = d[i]
        i += 1
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:  # markers without a length
            continue
        if m in (0xD9, 0xDA):                       # end of image / start of scan before a frame header
            return None
        if i + 2 > n:
            return None
        seg = struct.unpack(">H", d[i:i + 2])[0]
        if seg < 2:
            return None
        if m in _JPEG_SOF:
            if i + 7 > n:
                return None
            h, w = struct.unpack(">HH", d[i + 3:i + 7])
            return w, h
        i += seg
    return None


def dimensions(data: bytes, mime: str) -> Optional[Tuple[int, int]]:
    """(width, height) from the image header, or None when it cannot be read."""
    try:
        if mime == "image/png":
            if data[12:16] != b"IHDR":
                return None
            return struct.unpack(">II", data[16:24])
        if mime == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if mime == "image/jpeg":
            return _jpeg_size(data)
        if mime == "image/webp":
            chunk = data[12:16]
            if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L" and data[20:21] == b"\x2f":
                b = data[21:25]
                return 1 + (b[0] | (b[1] & 0x3F) << 8), 1 + ((b[1] >> 6) | b[2] << 2 | (b[3] & 0x0F) << 10)
            if chunk == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
    except (struct.error, IndexError):
        return None
    return None


def normalise_type(content_type) -> str:
    t = str(content_type or "").split(";")[0].strip().lower()
    return _ALIASES.get(t, t)


def check_image(data: bytes, content_type, *, allowed=limits.NEWS_IMAGE_TYPES,
                max_bytes: int = limits.MAX_NEWS_IMAGE_BYTES, max_side: int = limits.MAX_IMAGE_SIDE) -> str:
    """The checked MIME type, else ``ImageRejected`` (declared type, signature, size, pixel size)."""
    declared = normalise_type(content_type)
    if "svg" in declared or data.lstrip()[:1] == b"<":
        raise ImageRejected("SVG/markup images are not accepted")
    if declared not in allowed:
        raise ImageRejected(f"the image type is not allowed ({declared[:40] or 'no content type'}; allowed: "
                            + ", ".join(t.split("/")[1] for t in allowed) + ")")
    if len(data) > max_bytes:
        raise ImageRejected(f"the image is larger than {max_bytes // 1024} KB")
    actual = sniff(data)
    if actual != declared:
        raise ImageRejected("the file content does not match its declared type " + declared)
    size = dimensions(data, actual)
    if size is None or size[0] <= 0 or size[1] <= 0:
        raise ImageRejected("the image header could not be read")
    if max(size) > max_side:
        raise ImageRejected(f"the image is larger than {max_side} pixels per side ({size[0]}×{size[1]})")
    return actual


# --- downloading -------------------------------------------------------------------------------------
# Review fixes (2026-10-17): H1 every byte read is bounded by the deadline (small reads, socket timeout =
# remaining time, plus a kill timer that shuts the socket at the deadline — also during headers / chunk lines /
# the TLS handshake); DNS runs in a bounded helper thread with the remaining time as timeout. L5 the connection
# goes to the IP that was checked (no second resolution), TLS SNI + certificate check against the host name.

_DNS_SLOTS = threading.BoundedSemaphore(4)       # stuck name lookups never pile up threads


def resolve(host: str, timeout: float) -> List[str]:
    """getaddrinfo in a helper thread, at most ``timeout`` s (the lookup itself cannot be cancelled)."""
    if not _DNS_SLOTS.acquire(blocking=False):
        raise ImageFetchFailed("too many image-server name lookups are pending")
    box: dict = {}

    def work():
        try:
            box["r"] = sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
        except Exception as ex:   # noqa: BLE001 — reported below
            box["e"] = ex
        finally:
            _DNS_SLOTS.release()
    t = threading.Thread(target=work, name="research-dns", daemon=True)
    t.start()
    t.join(max(0.0, timeout))
    if t.is_alive():
        raise ImageFetchFailed("the image server name could not be resolved in time")
    if "e" in box:
        raise ImageFetchFailed("the image server name could not be resolved") from box["e"]
    return box.get("r") or []


class _PinnedConnection(http.client.HTTPSConnection):
    """HTTPS to a fixed, already checked IP; SNI + certificate verification use the host name."""

    def __init__(self, host: str, ip: str, port: int, timeout: float, *, tls: bool = True, on_socket=None):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip, self._tls, self._on_socket = ip, tls, on_socket

    def connect(self):
        raw = socket.create_connection((self._ip, self.port), self.timeout)
        if self._on_socket is not None:
            self._on_socket(raw)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host) if self._tls else raw


class _DeadlineResp:
    def __init__(self, conn, resp, deadline: float, per_op: float, mono, timer):
        self._conn, self._resp, self._deadline, self._per_op, self._mono, self._timer = \
            conn, resp, deadline, per_op, mono, timer
        self.status, self.headers = int(resp.status), resp.headers

    def read(self, n: int) -> bytes:
        left = self._deadline - self._mono()
        if left <= 0:
            raise ImageFetchFailed("the image download took too long")
        if self._conn.sock is not None:
            self._conn.sock.settimeout(max(0.05, min(self._per_op, left)))
        try:
            data = self._resp.read1(min(int(n), 8192))
            if not data and self._mono() >= self._deadline - 0.05:
                raise ImageFetchFailed("the image download took too long")   # the kill timer closed the socket
            return data
        except (OSError, http.client.HTTPException, ValueError) as ex:
            if self._mono() >= self._deadline - 0.05:
                raise ImageFetchFailed("the image download took too long") from ex
            raise

    def close(self) -> None:
        self._timer.cancel()
        for c in (self._resp, self._conn):
            try:
                c.close()
            except Exception:   # noqa: BLE001
                pass


def pinned_open(url: str, ip: str, headers: dict, timeout: float, deadline: float, *,
                mono: Callable[[], float] = _time.monotonic, tls: bool = True, port: Optional[int] = None):
    """One GET to ``ip`` (checked) for ``url``, no redirects followed, hard-capped by ``deadline``."""
    parts = urlsplit(url)
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    sockets: list = []

    def kill():
        for sk in sockets:
            try:
                sk.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sk.close()
            except OSError:
                pass
    left = deadline - mono()
    if left <= 0:
        raise ImageFetchFailed("the image download took too long")
    timer = threading.Timer(left, kill)
    timer.daemon = True
    timer.start()
    conn = _PinnedConnection(parts.hostname, ip, port or parts.port or 443, max(0.05, min(timeout, left)), tls=tls,
                             on_socket=sockets.append)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
    except BaseException as ex:
        timer.cancel()
        conn.close()
        if mono() >= deadline - 0.05:
            raise ImageFetchFailed("the image download took too long") from ex
        raise
    return _DeadlineResp(conn, resp, deadline, timeout, mono, timer)


def _image_gate():
    from strategy_platform.data import http_gate
    return http_gate.HttpGate("news_images", limits.NEWS_IMAGES_PER_MINUTE, capacity=10, max_attempts=1,
                              acquire_timeout=limits.NEWS_IMAGE_GATE_WAIT_SECONDS)


def _classify(ex: BaseException, data_type: str):
    if isinstance(ex, (socket.timeout, TimeoutError)) or "timed out" in str(ex).lower():
        return ImageFetchFailed("the image server did not answer in time")
    if isinstance(ex, ssl.SSLError):
        return ImageFetchFailed("the TLS connection to the image server failed")
    if isinstance(ex, (http.client.HTTPException, OSError)):
        return ImageFetchFailed("the image server could not be reached")
    return ImageFetchFailed(f"the download failed ({type(ex).__name__})")


class ImageFetcher:
    """``opener(url, ip, headers, timeout, deadline)`` -> response (``status``, ``headers.get``, ``read(n)``,
    ``close()``); ``resolver(host, timeout)`` -> [ip]. Defaults: ``pinned_open`` / ``resolve``."""

    def __init__(self, *, opener: Optional[Callable] = None, resolver: Optional[Callable] = None,
                 gate=None, monotonic: Callable[[], float] = _time.monotonic,
                 timeout: float = limits.NEWS_IMAGE_TIMEOUT_SECONDS, total: float = limits.NEWS_IMAGE_TOTAL_SECONDS,
                 max_bytes: int = limits.MAX_NEWS_IMAGE_BYTES, max_redirects: int = limits.NEWS_IMAGE_MAX_REDIRECTS):
        self._open = opener or (lambda url, ip, headers, timeout, deadline:
                                pinned_open(url, ip, headers, timeout, deadline, mono=self._mono))
        self._resolve = resolver or resolve
        self._gate = gate
        self._gate_lock = threading.Lock()
        self._mono = monotonic
        self.timeout, self.total, self.max_bytes, self.max_redirects = timeout, total, max_bytes, max_redirects
        self.requests: List[str] = []              # URLs requested (status / tests)

    def gate(self):
        with self._gate_lock:
            if self._gate is None:
                self._gate = _image_gate()
            return self._gate

    def _check_host(self, url: str, deadline: float) -> Tuple[str, str]:
        """(url, the checked IP to connect to)."""
        safe = safe_image_url(url)
        if safe is None:
            raise ImageRejected("the image URL is not an allowed https address")
        host = urlsplit(safe).hostname
        left = deadline - self._mono()
        if left <= 0:
            raise ImageFetchFailed("the image download took too long")
        try:
            addrs = self._resolve(host, left)
        except (OSError, UnicodeError, ValueError) as ex:
            raise ImageFetchFailed("the image server name could not be resolved") from ex
        if not addrs:
            raise ImageFetchFailed("the image server name could not be resolved")
        ips = []
        for a in addrs:
            try:
                ip = ipaddress.ip_address(str(a).split("%")[0])
            except ValueError:
                raise ImageRejected("the image server resolved to an invalid address")
            if not ip.is_global:
                raise ImageRejected("the image server resolves to a private or local address")
            ips.append(str(ip))
        v4 = [x for x in ips if ":" not in x]
        return safe, (v4 or ips)[0]

    def fetch(self, url: str) -> Tuple[str, bytes]:
        """(mime, bytes) of a checked image, else ``ImageRejected`` / ``ImageFetchFailed``."""
        return self.gate().call(lambda: self._fetch(url), data_type="news_images", classify=_classify)

    def _fetch(self, url: str) -> Tuple[str, bytes]:
        deadline = self._mono() + self.total
        current = url
        headers = {"User-Agent": USER_AGENT, "Accept": ", ".join(limits.NEWS_IMAGE_TYPES),
                   "Accept-Encoding": "identity", "Connection": "close"}
        for hop in range(self.max_redirects + 1):
            current, ip = self._check_host(current, deadline)
            left = deadline - self._mono()
            if left <= 0:
                raise ImageFetchFailed("the image download took too long")
            self.requests.append(current)
            resp = self._open(current, ip, dict(headers), min(self.timeout, left), deadline)
            try:
                if 300 <= resp.status < 400:
                    loc = resp.headers.get("Location") if resp.headers is not None else None
                    if not loc:
                        raise ImageFetchFailed(f"the image server answered HTTP {resp.status} without a target")
                    if hop >= self.max_redirects:
                        raise ImageRejected(f"more than {self.max_redirects} redirects")
                    current = urljoin(current, str(loc))
                    continue
                if resp.status != 200:
                    raise ImageFetchFailed(f"the image server answered HTTP {resp.status}")
                ctype = resp.headers.get("Content-Type") if resp.headers is not None else None
                declared = normalise_type(ctype)
                if "svg" in declared:
                    raise ImageRejected("SVG/markup images are not accepted")
                if declared not in limits.NEWS_IMAGE_TYPES:
                    raise ImageRejected(f"the image type is not allowed ({declared[:40] or 'no content type'})")
                length = resp.headers.get("Content-Length") if resp.headers is not None else None
                if length is not None and str(length).strip().isdigit() and int(length) > self.max_bytes:
                    raise ImageRejected(f"the image is larger than {self.max_bytes // 1024} KB")
                body = bytearray()
                while True:
                    if self._mono() > deadline:
                        raise ImageFetchFailed("the image download took too long")
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise ImageRejected(f"the image is larger than {self.max_bytes // 1024} KB")
                data = bytes(body)
                return check_image(data, declared, max_bytes=self.max_bytes), data
            finally:
                resp.close()
        raise ImageRejected(f"more than {self.max_redirects} redirects")
