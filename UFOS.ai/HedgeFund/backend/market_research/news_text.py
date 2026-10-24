"""Untrusted news text and links -> plain text (SPEC §9, §13 "MR6 backend notes").

News headlines / summaries / publishers come from Alpaca (Benzinga content) and are UNTRUSTED: they may
contain HTML markup, entities, control characters or very long text. Everything that leaves this module is
plain text — the WPF renders it in a TextBlock, the chart page via ``textContent`` — never markup:

- ``plain_text(value, limit)``: HTML tags removed (``<script>``/``<style>`` content dropped, block tags
  become a space), entities decoded, a second pass when the decoded text still contains tags
  (``&lt;b&gt;`` → ``<b>`` → removed), control / format characters (Unicode C*, line/paragraph
  separators) removed, whitespace collapsed, capped with "…".
- ``safe_link(url)``: http/https only, a host name, no user:password, no whitespace/control characters,
  ≤ ``MAX_URL_CHARS`` — else ``None`` (links are opened in the default browser).
- ``safe_image_url(url)``: https only, a DNS host name (no IP literal, no ``localhost``/``.local``), port
  443 — else ``None`` (the backend image proxy fetches only these, ``media.py``).
- ``sanitize_item(raw)``: one Alpaca news item (REST or stream) -> the internal item
  ``{id, headline, summary, publisher, url, created_at, updated_at, symbols, images: [{size, url}]}``
  (``None`` when it has no positive integer id, no headline or no created_at).

Stdlib only. Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import urlsplit

from . import limits

MAX_URL_CHARS = 1000
IMAGE_SIZES = ("thumb", "small")          # Alpaca news image sizes passed on (large ones exceed the size cap)
_TAG_LIKE = re.compile(r"<\s*[/!?A-Za-z]")
_OPEN_TAG_AT_END = re.compile(r"<[/!?A-Za-z][^<>]*$")
_SYMBOL_TAG = re.compile(r"^[A-Z0-9][A-Z0-9./-]{0,19}$")
_BLOCK_TAGS = {"p", "br", "div", "li", "ul", "ol", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
               "blockquote", "section", "article", "header", "footer", "table", "hr"}
_DROP_CONTENT = {"script", "style", "noscript", "template", "iframe", "object", "svg", "math"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._drop = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT:
            self._drop += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT:
            self._drop = max(0, self._drop - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._drop:
            self.parts.append(data)


def _strip_tags(text: str) -> str:
    text = _OPEN_TAG_AT_END.sub("", text)          # a tag cut off by an upstream length cap
    p = _Text()
    try:
        p.feed(text)
        p.close()
    except Exception:   # noqa: BLE001 — HTMLParser is lenient; anything odd → drop every <…> instead
        return re.sub(r"<[^>]*>", " ", text)
    return "".join(p.parts)


def _visible(ch: str) -> str:
    """Control characters / line separators → a space; format, private-use, unassigned → removed."""
    if ch in "\t\n\r ":
        return ch
    cat = unicodedata.category(ch)
    if cat in ("Cc", "Zl", "Zp"):
        return " "
    return "" if cat.startswith("C") else ch


def plain_text(value, limit: int) -> Optional[str]:
    """Plain one-line text of at most ``limit`` characters, or None when nothing is left."""
    if not isinstance(value, str) or not value:
        return None
    text = value[: max(limit * 8, 4096)]           # bound the parser's work on huge input
    text = _strip_tags(text)
    if _TAG_LIKE.search(text):                     # entity-encoded markup decoded into tags: strip once more
        text = _strip_tags(text)
    text = "".join(_visible(ch) for ch in text)
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _clean_url(url) -> Optional[str]:
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u or len(u) > MAX_URL_CHARS or any(not ch.isprintable() or ch.isspace() for ch in u):
        return None
    return u


def safe_link(url) -> Optional[str]:
    u = _clean_url(url)
    if u is None:
        return None
    try:
        parts = urlsplit(u)
        host = parts.hostname
        parts.port                                 # noqa: B018 — raises ValueError on a bad port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host or "@" in parts.netloc:
        return None
    return u


def safe_image_url(url) -> Optional[str]:
    u = _clean_url(url)
    if u is None:
        return None
    try:
        parts = urlsplit(u)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not host or "@" in parts.netloc or port not in (None, 443):
        return None
    host = host.rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        return None                                # IP literals are never fetched
    except ValueError:
        pass
    if "." not in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        return None
    return u


def _iso(value) -> Optional[str]:
    from strategy_platform.data.providers.base import parse_ts
    if value is None or value == "":
        return None
    try:
        return parse_ts(value).isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError):
        return None


def news_id(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value < 2 ** 63 else None
    if isinstance(value, str) and value.isdigit() and len(value) <= 18:
        return int(value) or None
    return None


def symbol_tags(values) -> List[str]:
    out: List[str] = []
    for s in values if isinstance(values, (list, tuple)) else []:
        if isinstance(s, str):
            t = s.strip().upper()
            if _SYMBOL_TAG.match(t) and t not in out:
                out.append(t)
        if len(out) >= limits.MAX_NEWS_SYMBOLS:
            break
    return out


def images(values) -> List[dict]:
    """[{size, url}] — only sizes ``IMAGE_SIZES``, only https image URLs that pass ``safe_image_url``."""
    out: List[dict] = []
    for it in values if isinstance(values, (list, tuple)) else []:
        if not isinstance(it, dict):
            continue
        size, url = it.get("size"), safe_image_url(it.get("url"))
        if size in IMAGE_SIZES and url and all(o["size"] != size for o in out):
            out.append({"size": size, "url": url})
    return out


def sanitize_item(raw) -> Optional[dict]:
    """Alpaca news item (REST dict from the provider, or a stream ``n`` message) -> internal item."""
    if not isinstance(raw, dict):
        return None
    nid = news_id(raw.get("id"))
    headline = plain_text(raw.get("headline"), limits.MAX_NEWS_HEADLINE_CHARS)
    created = _iso(raw.get("created_at"))
    if nid is None or not headline or created is None:
        return None
    publisher = raw.get("publisher") if "publisher" in raw else raw.get("source")
    return {"id": nid, "headline": headline,
            "summary": plain_text(raw.get("summary"), limits.MAX_NEWS_SUMMARY_CHARS),
            "publisher": plain_text(publisher, limits.MAX_NEWS_PUBLISHER_CHARS),
            "url": safe_link(raw.get("url")), "created_at": created, "updated_at": _iso(raw.get("updated_at")),
            "symbols": symbol_tags(raw.get("symbols")), "images": images(raw.get("images"))}
