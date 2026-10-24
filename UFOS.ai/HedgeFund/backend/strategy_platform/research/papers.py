"""Paper ingestion (SPEC §14.1, stage 5b): PDF upload, pasted text, or an arXiv / SSRN link.

- Links: exact host allow-list (``ALLOWED_HOSTS``), https on port 443 only, ≤ 5 redirects and each target
  inside the allow-list, SSRF-hardened fetch (resolve -> every address must be global -> connect to the
  CHECKED IP with SNI + certificate verification for the host name, so DNS rebinding cannot redirect the
  connection), hard 30 s deadline, ≤ 20 MB, content-type allow-list (PDF; HTML only for the SSRN abstract
  page), through an ``http_gate.HttpGate`` of its own (source ``papers``, ``PAPERS_PER_MINUTE``, one
  attempt). Same rules as market_research/media.py (implemented here so the MR fetcher stays unchanged).
  SSRN without a direct PDF (login / captcha) -> ``SSRN_LOGIN_MESSAGE`` — never a scraping workaround.
- PDF -> text in a separate process (``pdf_worker.py``, pypdf, pinned): 60 s, memory guard, ≤ 150 pages;
  the temporary PDF (the app's own temp file) is removed right after the extraction — only the extracted
  text (``data/papers/<sha256>.txt``) and the sha256 are kept.
- Paper text is UNTRUSTED: it reaches the LLM only as quoted data (``paper_strategy``).
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time as _time
import uuid
from typing import Callable, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlsplit

from ..backtest.explain import ExplainError
from ..storage import db

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = ("arxiv.org", "export.arxiv.org", "papers.ssrn.com")
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 150
MAX_TEXT_CHARS = 200_000                 # pasted text
MAX_STORED_CHARS = 2_000_000             # extracted PDF text kept
TITLE_MAX = 300
EXTRACT_TIMEOUT_SECONDS = 60
EXTRACT_MEMORY_MB = 1024
FETCH_TOTAL_SECONDS = 30
FETCH_OP_TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 5
PAPERS_PER_MINUTE = 6
USER_AGENT = "UFOS.ai-research/1 (paper import; proof-of-concept)"
SSRN_LOGIN_MESSAGE = "SSRN did not provide the PDF without login — download it in the browser and upload it"
URL_NOT_ALLOWED_MESSAGE = ("Only arXiv and SSRN links are accepted (https://arxiv.org/abs/…, https://arxiv.org/pdf/…, "
                           "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=…).")
_ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?"
_ARXIV_PATH = re.compile(rf"^/(abs|pdf)/({_ARXIV_ID})(?:\.pdf)?/?$")
_SSRN_DELIVERY = re.compile(r"""href=["']((?:https://papers\.ssrn\.com/sol3/)?Delivery\.cfm/[^"'<>\s]+?\.pdf\?"""
                            r"""abstractid=\d+[^"'<>\s]*)["']""", re.I)


class PaperError(ExplainError):
    """A paper request that cannot be served (status, code, message) — same shape as the explain errors."""


def _now_iso() -> str:
    return db.utc_now_iso()


# --- URL rules --------------------------------------------------------------------------------------

def classify_url(url) -> Tuple[str, str]:
    """('arxiv', pdf url) | ('ssrn', abstract url); anything else -> PaperError 400 url_not_allowed."""
    if not isinstance(url, str) or len(url) > 500:
        raise PaperError(400, "url_not_allowed", URL_NOT_ALLOWED_MESSAGE)
    try:
        p = urlsplit(url.strip())
        port = p.port
    except ValueError:
        raise PaperError(400, "url_not_allowed", URL_NOT_ALLOWED_MESSAGE) from None
    host = (p.hostname or "").lower()
    if p.scheme != "https" or port not in (None, 443) or p.username or p.password or p.fragment:
        raise PaperError(400, "url_not_allowed", URL_NOT_ALLOWED_MESSAGE)
    if host in ("arxiv.org", "export.arxiv.org") and not p.query:
        m = _ARXIV_PATH.match(p.path)
        if m:
            return "arxiv", f"https://arxiv.org/pdf/{m.group(2)}"
    if host == "papers.ssrn.com" and p.path == "/sol3/papers.cfm":
        q = parse_qs(p.query, keep_blank_values=True)
        if set(q) == {"abstract_id"} and len(q["abstract_id"]) == 1 and q["abstract_id"][0].isdigit():
            return "ssrn", f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={q['abstract_id'][0]}"
    raise PaperError(400, "url_not_allowed", URL_NOT_ALLOWED_MESSAGE)


def _allowed_target(url: str) -> str:
    """A redirect target / delivery link: https, port 443, host in the allow-list -> the URL, else 400."""
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        port = -1
        p = None
    if p is None or p.scheme != "https" or port not in (None, 443) or (p.hostname or "").lower() not in ALLOWED_HOSTS \
            or p.username or p.password:
        raise PaperError(400, "fetch_rejected", "The server redirected to an address outside arXiv/SSRN.")
    return url


# --- SSRF-hardened fetch --------------------------------------------------------------------------------

def resolve(host: str, timeout: float) -> List[str]:
    box: dict = {}

    def work():
        try:
            box["r"] = sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
        except Exception as ex:   # noqa: BLE001
            box["e"] = ex
    t = threading.Thread(target=work, name="paper-dns", daemon=True)
    t.start()
    t.join(max(0.0, timeout))
    if t.is_alive() or "e" in box:
        raise PaperError(502, "fetch_failed", "The paper server name could not be resolved.")
    return box.get("r") or []


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, timeout: float, on_socket):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._ip, self._on_socket = ip, on_socket

    def connect(self):
        raw = socket.create_connection((self._ip, 443), self.timeout)
        self._on_socket(raw)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class _Resp:
    def __init__(self, conn, resp, timer):
        self._conn, self._resp, self._timer = conn, resp, timer
        self.status, self.headers = int(resp.status), resp.headers

    def read(self, n: int) -> bytes:
        return self._resp.read1(min(int(n), 65536))

    def close(self) -> None:
        self._timer.cancel()
        for c in (self._resp, self._conn):
            try:
                c.close()
            except Exception:   # noqa: BLE001
                pass


def pinned_open(url: str, ip: str, headers: dict, timeout: float, deadline: float, mono=_time.monotonic):
    """One GET to the checked ``ip`` for ``url`` (no redirects followed), killed at ``deadline``."""
    parts = urlsplit(url)
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    sockets: list = []

    def kill():
        for sk in sockets:
            try:
                sk.shutdown(socket.SHUT_RDWR)
                sk.close()
            except OSError:
                pass
    left = deadline - mono()
    if left <= 0:
        raise PaperError(502, "fetch_failed", "The paper download took longer than 30 s.")
    timer = threading.Timer(left, kill)
    timer.daemon = True
    timer.start()
    conn = _PinnedConnection(parts.hostname, ip, max(0.05, min(timeout, left)), sockets.append)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
    except BaseException:
        timer.cancel()
        conn.close()
        raise
    return _Resp(conn, resp, timer)


def _classify(ex: BaseException, data_type: str):
    if isinstance(ex, (socket.timeout, TimeoutError)) or "timed out" in str(ex).lower():
        return PaperError(502, "fetch_failed", "The paper server did not answer in time.")
    if isinstance(ex, ssl.SSLError):
        return PaperError(502, "fetch_failed", "The TLS connection to the paper server failed.")
    return PaperError(502, "fetch_failed", f"The paper could not be downloaded ({type(ex).__name__}).")


_GATE = None
_GATE_LOCK = threading.Lock()


def _gate():
    global _GATE
    from ..data import http_gate
    with _GATE_LOCK:
        if _GATE is None:
            _GATE = http_gate.HttpGate("papers", PAPERS_PER_MINUTE, capacity=2, max_attempts=1, acquire_timeout=10)
        return _GATE


class PaperFetcher:
    """``opener(url, ip, headers, timeout, deadline)`` -> resp (status, headers.get, read(n), close());
    ``resolver(host, timeout)`` -> [ip]. Defaults: ``pinned_open`` / ``resolve``."""

    def __init__(self, *, opener=None, resolver=None, gate=None, monotonic: Callable[[], float] = _time.monotonic,
                 total: float = FETCH_TOTAL_SECONDS, max_bytes: int = MAX_PDF_BYTES, max_redirects: int = MAX_REDIRECTS):
        self._open = opener or (lambda url, ip, headers, timeout, deadline:
                                pinned_open(url, ip, headers, timeout, deadline, mono=self._mono))
        self._resolve = resolver or resolve
        self._gate = gate
        self._mono = monotonic
        self.total, self.max_bytes, self.max_redirects = total, max_bytes, max_redirects
        self.requests: List[str] = []

    def _check(self, url: str, deadline: float) -> str:
        host = urlsplit(url).hostname
        left = deadline - self._mono()
        if left <= 0:
            raise PaperError(502, "fetch_failed", "The paper download took longer than 30 s.")
        addrs = self._resolve(host, left)
        if not addrs:
            raise PaperError(502, "fetch_failed", "The paper server name could not be resolved.")
        ips = []
        for a in addrs:
            try:
                ip = ipaddress.ip_address(str(a).split("%")[0])
            except ValueError:
                raise PaperError(400, "fetch_rejected", "The paper server resolved to an invalid address.") from None
            if not ip.is_global:
                raise PaperError(400, "fetch_rejected", "The paper server resolves to a private or local address.")
            ips.append(str(ip))
        v4 = [x for x in ips if ":" not in x]
        return (v4 or ips)[0]

    def get(self, url: str, types: Tuple[str, ...]) -> Tuple[str, bytes, str]:
        """(content type, body, final url) — through the ``papers`` gate."""
        gate = self._gate or _gate()
        return gate.call(lambda: self._get(url, types), data_type="papers", classify=_classify)

    def _get(self, url: str, types: Tuple[str, ...]) -> Tuple[str, bytes, str]:
        deadline = self._mono() + self.total
        current = _allowed_target(url)
        headers = {"User-Agent": USER_AGENT, "Accept": ", ".join(types), "Accept-Encoding": "identity",
                   "Connection": "close"}
        for hop in range(self.max_redirects + 1):
            ip = self._check(current, deadline)
            self.requests.append(current)
            resp = self._open(current, ip, dict(headers), min(FETCH_OP_TIMEOUT_SECONDS, deadline - self._mono()),
                              deadline)
            try:
                if 300 <= resp.status < 400:
                    loc = resp.headers.get("Location") if resp.headers is not None else None
                    if not loc:
                        raise PaperError(502, "fetch_failed", f"The paper server answered HTTP {resp.status}.")
                    if hop >= self.max_redirects:
                        raise PaperError(400, "fetch_rejected", f"More than {self.max_redirects} redirects.")
                    current = _allowed_target(urljoin(current, str(loc)))
                    continue
                if resp.status != 200:
                    raise PaperError(502, "fetch_failed", f"The paper server answered HTTP {resp.status}.")
                ctype = str((resp.headers.get("Content-Type") if resp.headers is not None else "") or "")
                ctype = ctype.split(";")[0].strip().lower()
                if ctype not in types:
                    return ctype, b"", current          # the caller decides (e.g. SSRN login page)
                length = resp.headers.get("Content-Length") if resp.headers is not None else None
                if length is not None and str(length).strip().isdigit() and int(length) > self.max_bytes:
                    raise PaperError(400, "too_large", f"The file is larger than {self.max_bytes // (1024 * 1024)} MB.")
                body = bytearray()
                while True:
                    if self._mono() > deadline:
                        raise PaperError(502, "fetch_failed", "The paper download took longer than 30 s.")
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise PaperError(400, "too_large",
                                         f"The file is larger than {self.max_bytes // (1024 * 1024)} MB.")
                return ctype, bytes(body), current
            finally:
                resp.close()
        raise PaperError(400, "fetch_rejected", f"More than {self.max_redirects} redirects.")


FETCHER: Optional[PaperFetcher] = None          # tests inject a fake-backed fetcher


def _fetcher() -> PaperFetcher:
    return FETCHER or PaperFetcher()


def fetch_link(url: str) -> Tuple[str, bytes, str]:
    """(source kind, pdf bytes, final url) for an allowed arXiv / SSRN link."""
    kind, target = classify_url(url)
    f = _fetcher()
    if kind == "arxiv":
        ctype, body, final = f.get(target, ("application/pdf",))
        if ctype != "application/pdf":
            raise PaperError(400, "fetch_rejected", f"arXiv did not return a PDF ({ctype[:40] or 'no content type'}).")
        return kind, body, final
    ctype, html, final = f.get(target, ("text/html",))
    if ctype != "text/html":
        raise PaperError(400, "fetch_rejected", f"SSRN returned an unexpected content type ({ctype[:40]}).")
    m = _SSRN_DELIVERY.search(html.decode("utf-8", "replace"))
    if not m:
        raise PaperError(502, "ssrn_login", SSRN_LOGIN_MESSAGE)
    pdf_url = _allowed_target(urljoin("https://papers.ssrn.com/sol3/", m.group(1).replace("&amp;", "&")))
    ctype, body, final = f.get(pdf_url, ("application/pdf",))
    if ctype != "application/pdf" or not body.startswith(b"%PDF-"):
        raise PaperError(502, "ssrn_login", SSRN_LOGIN_MESSAGE)
    return kind, body, final


# --- PDF -> text (subprocess) ------------------------------------------------------------------------------

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_worker.py")


def _worker_argv(path: str) -> List[str]:
    """Overridable in tests (a fake extractor script)."""
    return [sys.executable, "-I", WORKER, path, str(MAX_PAGES), str(MAX_STORED_CHARS), str(EXTRACT_MEMORY_MB)]


def worker_env() -> dict:
    """The PDF worker's environment: this process's minus every variable that looks like a credential (the same
    ``SECRET_ENV_RE`` the validation workers use, validation/parallel.py) — stage 10 security audit L5."""
    from ..validation.parallel import SECRET_ENV_RE
    return {k: v for k, v in os.environ.items() if not SECRET_ENV_RE.search(k)}


WORKER_ARGV: Callable[[str], List[str]] = _worker_argv


def extract_pdf(data: bytes, *, timeout: float = EXTRACT_TIMEOUT_SECONDS) -> dict:
    """{pages, text, title, authors, year, truncated} of a PDF via the worker process; PaperError on any
    failure. The temporary file is the app's own and is removed afterwards."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise PaperError(400, "pdf_invalid", "No PDF data was received.")
    if len(data) > MAX_PDF_BYTES:
        raise PaperError(400, "too_large", f"The PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB.")
    if not bytes(data[:1024]).lstrip().startswith(b"%PDF-"):
        raise PaperError(400, "pdf_invalid", "The file is not a PDF.")
    fd, path = tempfile.mkstemp(prefix="ufos-paper-", suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(bytes(data))
        try:
            proc = subprocess.run(WORKER_ARGV(path), capture_output=True, timeout=timeout, check=False,
                                  stdin=subprocess.DEVNULL, env=worker_env())
        except subprocess.TimeoutExpired:
            raise PaperError(504, "extract_timeout",
                             f"The PDF text extraction took longer than {int(timeout)} s.") from None
    finally:
        try:
            os.remove(path)                   # runtime cleanup of the app's own temp file (SPEC §14.1)
        except OSError:
            logger.warning("papers: the temporary PDF %s could not be removed", path)
    if proc.returncode == 3:
        raise PaperError(400, "pdf_invalid", "The PDF needs too much memory to extract.")
    try:
        out = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        out = {}
    if proc.returncode == 4 or out.get("error") == "missing_extractor":
        raise PaperError(503, "extractor_missing", "The PDF extractor (pypdf) is not installed — run the backend "
                                                   "requirements install.")
    if out.get("error"):
        raise PaperError(400, "pdf_invalid", str(out.get("message") or "The PDF could not be read.")[:300])
    if proc.returncode != 0 or not isinstance(out.get("text"), str):
        raise PaperError(400, "pdf_invalid", "The PDF could not be read.")
    if not out["text"].strip() or not re.search(r"[A-Za-z]{3}", out["text"]):
        raise PaperError(400, "pdf_invalid", "The PDF contains no extractable text (scans are not supported — "
                                             "no OCR).")
    return out


# --- storage ---------------------------------------------------------------------------------------------

def papers_dir() -> str:
    base = os.path.dirname(db.db_path() or db.default_path())
    d = os.path.join(base, "papers")
    os.makedirs(d, exist_ok=True)
    return d


def _text_path(sha: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("sha256")
    return os.path.join(papers_dir(), f"{sha}.txt")


def read_text(paper: dict) -> str:
    try:
        with open(_text_path(paper["sha256"]), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as ex:
        raise PaperError(503, "text_missing", "The stored paper text could not be read.") from ex


def _clean(text, limit: int) -> Optional[str]:
    if text is None:
        return None
    s = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text)).split())
    return s[:limit] or None


def _store(kind: str, text: str, sha: str, *, url=None, filename=None, title=None, authors=None, year=None,
           pages=None) -> dict:
    existing = db.s5b_select("papers", {"sha256": sha})
    if existing:
        return dict(existing[0], duplicate=True)
    path = _text_path(sha)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)
    row = {"id": "p-" + uuid.uuid4().hex[:12], "source_kind": kind, "url": url, "filename": filename, "sha256": sha,
           "title": _clean(title, TITLE_MAX), "authors": _clean(authors, TITLE_MAX),
           "year": year if isinstance(year, int) and 1900 <= year <= 2100 else None,
           "text_chars": len(text), "pages": pages}
    db.s5b_insert("papers", row, ("user", "paper_added", {"id": row["id"], "source_kind": kind, "sha256": sha,
                                                         "url": url, "filename": filename}))
    return dict(db.s5b_select("papers", {"id": row["id"]})[0], duplicate=False)


def add_pdf(data: bytes, filename: Optional[str]) -> dict:
    from ..lifecycle import registry as lc
    sha = hashlib.sha256(bytes(data)).hexdigest()
    existing = db.s5b_select("papers", {"sha256": sha})
    if existing:
        return dict(existing[0], duplicate=True)
    out = extract_pdf(data)
    return _store("pdf", out["text"], sha, filename=lc.clean_file_name(filename), title=out.get("title"),
                  authors=out.get("authors"), year=out.get("year"), pages=out.get("pages"))


def add_text(text: str, title: Optional[str]) -> dict:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise PaperError(400, "too_large", f"The text must have 1..{MAX_TEXT_CHARS:,} characters.")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return _store("text", text, sha, title=title)


def add_url(url: str) -> dict:
    kind, data, final = fetch_link(url)
    sha = hashlib.sha256(data).hexdigest()
    existing = db.s5b_select("papers", {"sha256": sha})
    if existing:
        return dict(existing[0], duplicate=True)
    out = extract_pdf(data)
    return _store(kind, out["text"], sha, url=url.strip(), title=out.get("title"), authors=out.get("authors"),
                  year=out.get("year"), pages=out.get("pages"))
