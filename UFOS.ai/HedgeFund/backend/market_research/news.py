"""MR6 news (SPEC §4.2, §13 "MR6 backend notes"): REST news per symbol, the news stream hook, the store
(7 days, de-dup by Alpaca id), the thumbnail setting and the thumbnail proxy.

- REST: ``AlpacaProvider.news`` (Benzinga via Alpaca) through ``sources.news`` — the caller runs it on the
  Research pool inside ``http_gate.research_priority()`` (SPEC §5.1, §7.5). The first page of a symbol is
  asked from Alpaca at most every ``NEWS_REFRESH_SECONDS`` (else the answer comes from the store, which also
  holds the stream items); a failure is a 503 "News unavailable: …" — never stored news instead.
- Stream: the market-data hub routes ``n`` messages to the ``news:<SYM>`` subscribers; ``on_stream_item``
  (the hub's ``news_hook``) drops repeats (same id + updated_at), queues the item for the store and returns
  the public item. The supervised task ``news_stream_store`` writes the queue.
- Everything is sanitised by ``news_text`` (plain text, capped, http/https links, https image URLs).
- Thumbnails: items carry ``images: [{size, path}]`` — ``path`` is the backend proxy route
  (``/research/news/images/<sha256 of the URL>``); the WPF never fetches third-party URLs. Setting
  ``thumbnails`` (default on, meta ``news_thumbnails``): off → ``images: []`` and the proxy refuses (409),
  so no image request leaves the backend.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time as _time
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from strategy_platform.core.errors import UserFacingError

from . import limits
from . import store as default_store
from .executor import run_blocking
from .media import ImageFetcher, ImageRejected
from .news_text import sanitize_item

logger = logging.getLogger(__name__)

META_THUMBNAILS = "news_thumbnails"
IMAGE_PATH = "/research/news/images/"
THUMBNAIL_NOTE = ("News thumbnails are loaded by the HedgeFund backend (never by the app) from the image servers "
                  "named in the Alpaca/Benzinga news data; those servers see your IP address when the backend "
                  "fetches an image. Switch thumbnails off and no image request is made.")
NEWS_NOTE = ("News: Benzinga via Alpaca. Headlines and summaries are shown as plain text; links open the original "
             "article in your browser.")
STORE_RETRY_SECONDS = 30


class ThumbnailsOff(UserFacingError):
    def __init__(self):
        super().__init__("News thumbnails are switched off (Research settings) — no image is loaded.")


class UnknownImage(UserFacingError):
    def __init__(self):
        super().__init__("Unknown news image.")


class ThumbnailUnavailable(UserFacingError):
    def __init__(self, reason: str):
        super().__init__(limits.clip(f"Thumbnail unavailable: {reason}"))
        self.reason = reason


def image_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def public_item(item: dict, thumbnails: bool) -> dict:
    """The item as REST and ``/research/ws`` send it (exact keys, SPEC §13 MR6 notes)."""
    return {"id": item["id"], "headline": item["headline"], "summary": item.get("summary"),
            "publisher": item.get("publisher"), "source_label": limits.NEWS_SOURCE_LABEL, "url": item.get("url"),
            "created_at": item["created_at"], "updated_at": item.get("updated_at"),
            "symbols": list(item.get("symbols") or []),
            "images": [{"size": im["size"], "path": IMAGE_PATH + image_key(im["url"])}
                       for im in (item.get("images") or [])] if thumbnails else []}


class NewsService:
    def __init__(self, data_fn: Callable[[], object], *, store=default_store, fetcher: Optional[ImageFetcher] = None,
                 monotonic: Callable[[], float] = _time.monotonic):
        self._data_fn = data_fn
        self.store = store
        self.fetcher = fetcher or ImageFetcher()
        self._mono = monotonic
        self._lock = threading.Lock()
        self._fresh: Dict[str, Tuple[float, int, str]] = {}     # symbol -> (monotonic, limit, fetched_at)
        self.thumbnails = True                                  # cached for the stream hook; store = authority
        self._seen: "OrderedDict[int, str]" = OrderedDict()     # stream de-dup: id -> updated_at/created_at
        self._backlog: deque = deque()
        self._wake: Optional[asyncio.Event] = None
        self.dropped = 0
        self._image_locks: Dict[str, threading.Lock] = {}
        self.logos = None                                       # LogoService (settings payload), set by the service

    @property
    def data(self):
        return self._data_fn()

    # --- settings -------------------------------------------------------------------------------------
    def thumbnails_enabled(self) -> bool:
        on = self.store.meta_get(META_THUMBNAILS) != "off"
        self.thumbnails = on
        return on

    def settings(self) -> dict:
        return {"thumbnails": self.thumbnails_enabled(), "note": THUMBNAIL_NOTE,
                "source_label": limits.NEWS_SOURCE_LABEL,
                "logos": self.logos.status() if self.logos is not None else None}

    def set_thumbnails(self, on: bool) -> dict:
        before = self.thumbnails_enabled()
        self.store.meta_set(META_THUMBNAILS, "on" if on else "off")
        self.thumbnails = bool(on)
        if before != bool(on):
            self.store.add_audit("user", "news_thumbnails_set", {"thumbnails": bool(on)})
        return self.settings()

    # --- REST (blocking; caller: Research pool + research priority) ---------------------------------------
    def symbol_news(self, symbol: str, limit: int, before: Optional[datetime]) -> dict:
        from strategy_platform.data.providers.alpaca import news_symbol
        data = self.data
        a = data.asset(symbol)                                  # SymbolNotCovered → 404
        sym = a["symbol"]
        tag = news_symbol(sym)
        with self._lock:
            fresh = self._fresh.get(sym)
        mono = self._mono()
        if before is None and fresh and mono - fresh[0] < limits.NEWS_REFRESH_SECONDS and fresh[1] >= limit:
            fetched_at = fresh[2]
        else:
            try:
                raw = data.sources.news(sym, end=(before - timedelta(seconds=1)) if before else None, limit=limit)
            except UserFacingError as ex:
                raise NewsUnavailable(ex) from ex
            items = [i for i in (sanitize_item(r) for r in raw or []) if i is not None]
            if len(items) < len(raw or []):
                logger.info("Research news %s: %d unusable item(s) skipped", sym, len(raw) - len(items))
            fetched_at = _iso(data.now())
            self.store.news_upsert(items, fetched_at, "rest", image_key)
            if before is None:
                with self._lock:
                    self._fresh[sym] = (mono, limit, fetched_at)
        rows = self.store.news_for_symbol(tag, limit, _iso(before) if before else None)
        thumbs = self.thumbnails_enabled()
        return {"symbol": sym, "source": limits.NEWS_SOURCE, "source_label": limits.NEWS_SOURCE_LABEL,
                "items": [public_item(r, thumbs) for r in rows], "thumbnails": thumbs, "fetched_at": fetched_at,
                "has_more": len(rows) == limit, "next_before": rows[-1]["created_at"] if rows else None,
                "note": NEWS_NOTE}

    # --- stream (event loop; the hub's news_hook) --------------------------------------------------------
    def on_stream_item(self, item: dict, symbols: List[str]) -> Optional[dict]:
        """None = a repeat (same id and update time) → not sent again."""
        stamp = item.get("updated_at") or item["created_at"]
        if self._seen.get(item["id"]) == stamp:
            return None
        self._seen[item["id"]] = stamp
        self._seen.move_to_end(item["id"])
        while len(self._seen) > limits.NEWS_SEEN_MAX:
            self._seen.popitem(last=False)
        if len(self._backlog) >= limits.NEWS_STREAM_QUEUE_MAX:
            self.dropped += 1
            logger.warning("Research news stream: store queue full — item %s delivered but not stored", item["id"])
        else:
            self._backlog.append(item)
            if self._wake is not None:
                self._wake.set()
        return public_item(item, self.thumbnails)

    def _write(self, batch: List[dict]) -> None:
        self.store.news_upsert(batch, self.store.utc_now_iso(), "stream", image_key)

    async def run(self) -> None:
        """Supervised task ``news_stream_store``: writes queued stream items, refreshes the cached setting."""
        from .hub.aio import within
        self._wake = asyncio.Event()
        while True:
            try:
                await run_blocking(self.thumbnails_enabled)
                while self._backlog:
                    batch = [self._backlog.popleft() for _ in range(min(200, len(self._backlog)))]
                    try:
                        await run_blocking(self._write, batch)
                    except BaseException:
                        self._backlog.extendleft(reversed(batch))   # kept (bounded) for the next attempt
                        raise
                wait = None
            except self.store.StoreUnavailable as ex:
                logger.warning("Research news stream store: %s — next attempt in %d s", ex.user_message,
                               STORE_RETRY_SECONDS)
                wait = STORE_RETRY_SECONDS
            self._wake.clear()
            try:
                await within(self._wake.wait(), wait if wait is not None else 60)
            except asyncio.TimeoutError:
                pass

    # --- thumbnail proxy (blocking; Research pool, NOT the Alpaca budget) --------------------------------
    def image(self, key: str) -> Tuple[str, bytes]:
        if not self.thumbnails_enabled():
            raise ThumbnailsOff()
        with self._lock:
            lk = self._image_locks.setdefault(key, threading.Lock())
        with lk:
            try:
                return self._image(key)
            finally:
                with self._lock:
                    if len(self._image_locks) > 256:
                        self._image_locks.clear()

    def _image(self, key: str) -> Tuple[str, bytes]:
        row = self.store.news_image_get(key)
        if row is None:
            raise UnknownImage()
        if row["status"] == "ok" and row["data"] is not None:
            return row["content_type"], row["data"]
        if row["status"] == "rejected":
            raise ThumbnailUnavailable(row["reason"] or "the image was rejected")
        now = datetime.now(timezone.utc)
        if row["status"] == "failed" and row["fetched_at"]:
            try:
                age = (now - datetime.fromisoformat(row["fetched_at"])).total_seconds()
            except ValueError:
                age = limits.NEWS_IMAGE_RETRY_SECONDS
            if age < limits.NEWS_IMAGE_RETRY_SECONDS:
                raise ThumbnailUnavailable(row["reason"] or "the last download failed")
        try:
            mime, data = self.fetcher.fetch(row["url"])
        except ImageRejected as ex:
            self.store.news_image_set(key, "rejected", _iso(now), reason=ex.user_message)
            raise ThumbnailUnavailable(ex.user_message) from ex
        except UserFacingError as ex:
            self.store.news_image_set(key, "failed", _iso(now), reason=limits.clip(ex.user_message))
            raise ThumbnailUnavailable(ex.user_message) from ex
        self.store.news_image_set(key, "ok", _iso(now), content_type=mime, data=data)
        return mime, data


class NewsUnavailable(UserFacingError):
    """Alpaca news could not be loaded — 503 "News unavailable: <reason>" (SPEC §8)."""

    def __init__(self, cause: UserFacingError):
        super().__init__(limits.clip(f"News unavailable: {cause.user_message}", 600))
        self.cause_kind = type(cause).__name__
