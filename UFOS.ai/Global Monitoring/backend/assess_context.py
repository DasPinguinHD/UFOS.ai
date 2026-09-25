"""Builds the context the Assess/Sentiment button sends to the model.

2026-09-24: the model kept answering "not enough context" — because it only
ever saw the marker's own headline + one-line teaser (or, for a flight, a
callsign and some telemetry), and the old prompt told it to say so whenever
that was thin. This module gathers what a human analyst would look at next:

1. FULL ARTICLE TEXT for news markers: the article page is fetched and its
   paragraphs extracted (stdlib HTMLParser, no new dependency), capped at
   ARTICLE_MAX_CHARS. Cached per URL. Falls back silently to the teaser if the
   page can't be fetched (paywall, JS-only page, timeout).
2. LOCATION: the country (or "open water near ...") under the marker, from
   geo.nearest_country() — flights and disasters only come with lat/lon.
3. RELATED EVENTS: candidates filtered per category (_RELATED_RULES — e.g.
   a flood gets nearby disasters + news about the same country, never
   flights), newest first, one line each with distance. For markets: the
   other market quotes plus the latest geopolitics headlines as backdrop.
4. STRUCTURED FACTS where the collector provides them (GDACS: type,
   country, alert level, ISO start/end dates).

build_context() returns (context_text, used_labels); used_labels is shown to
the user as "Based on: ..." so it's visible what the assessment rests on.
"""
import logging
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

import aiohttp

import news_feeds
from geo import detect_mentioned_country, haversine_km, nearest_country

logger = logging.getLogger(__name__)

ARTICLE_MAX_CHARS = 6000
ARTICLE_CACHE_SECONDS = 3600
RELATED_MAX = 5

# Which other events count as "related", per category of the assessed event:
# {other category: max distance in km}. Plus, for news-type matches, "same
# country by headline" always counts. 2026-09-24: a single 1,500 km radius
# for everything fed a Sicilian flood assessment US military flights and
# unrelated politics — the model then spent sentences dismissing them.
_RELATED_RULES = {
    "disaster":    {"disaster": 1000, "geopolitics_same_country": True},
    "flight":      {"flight": 500, "geopolitics": 800},
    "geopolitics": {"disaster": 500, "flight": 500, "geopolitics_same_country": True},
}

_article_cache: dict[str, tuple[float, str]] = {}


class _ParagraphExtractor(HTMLParser):
    """Collects the text of <p> elements, preferring those inside <article>
    or <main>; skips script/style/nav/footer/aside/header/form content."""
    _SKIP = {"script", "style", "nav", "footer", "aside", "header", "form", "noscript", "figure"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth_skip = 0
        self.depth_article = 0
        self.in_p = False
        self.buf = []
        self.paragraphs = []      # (in_article, text)

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.depth_skip += 1
        elif tag in ("article", "main"):
            self.depth_article += 1
        elif tag == "p" and not self.depth_skip:
            self.in_p, self.buf = True, []

    def handle_endtag(self, tag):
        if tag in self._SKIP and self.depth_skip:
            self.depth_skip -= 1
        elif tag in ("article", "main") and self.depth_article:
            self.depth_article -= 1
        elif tag == "p" and self.in_p:
            text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if len(text) >= 40:  # skip captions, bylines, "Share this" etc.
                self.paragraphs.append((self.depth_article > 0, text))
            self.in_p = False

    def handle_data(self, data):
        if self.in_p and not self.depth_skip:
            self.buf.append(data)


def extract_article_text(html_text: str) -> str:
    parser = _ParagraphExtractor()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    in_article = [t for inside, t in parser.paragraphs if inside]
    paragraphs = in_article if len(" ".join(in_article)) >= 300 else [t for _, t in parser.paragraphs]
    text = "\n".join(paragraphs)
    if len(text) < 300:
        # Fallback (2026-09-24, a Guardian article came back empty): the
        # structural parser can lose track on some pages (an unclosed
        # <header>/<form> hides everything after it). A plain regex over all
        # <p> elements is cruder but can't get stuck that way.
        raw_ps = re.findall(r"<p\b[^>]*>(.*?)</p>", html_text, re.IGNORECASE | re.DOTALL)
        cleaned = [news_feeds.clean_text(p) for p in raw_ps]
        cleaned = [p for p in cleaned if len(p) >= 60]
        if len("\n".join(cleaned)) > len(text):
            text = "\n".join(cleaned)
    if len(text) > ARTICLE_MAX_CHARS:
        text = text[:ARTICLE_MAX_CHARS].rsplit(" ", 1)[0] + " …[truncated]"
    return text


async def fetch_article_text(session: aiohttp.ClientSession, url: str) -> str:
    if not url or not url.startswith(("http://", "https://")):
        return ""
    cached = _article_cache.get(url)
    if cached and time.time() - cached[0] < ARTICLE_CACHE_SECONDS:
        return cached[1]
    text = ""
    try:
        headers = dict(news_feeds.HEADERS, Accept="text/html,application/xhtml+xml")
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            resp.raise_for_status()
            raw = await resp.content.read(3_000_000)  # cap: nobody needs more than 3 MB of HTML
            html_text = raw.decode(resp.charset or "utf-8", errors="replace")
        text = extract_article_text(html_text)
        if not text:
            logger.info("Article text empty for %s (HTML %d chars, %d <p> tags) — page may render via JavaScript",
                        url, len(html_text), html_text.lower().count("<p"))
    except Exception as ex:
        logger.info("Article text unavailable for %s: %s", url, str(ex) or type(ex).__name__)
    _article_cache[url] = (time.time(), text)
    return text


def _age(ts: str) -> str:
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ""
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f}h ago" if hours >= 1 else f"{delta.total_seconds() / 60:.0f}min ago"


_CATEGORY_LABELS = {"geopolitics": "news", "disaster": "disaster", "flight": "flight",
                    "markets": "market"}


def _line(ev: dict, km=None) -> str:
    cat = _CATEGORY_LABELS.get(ev.get("category"), ev.get("category", ""))
    summary = (ev.get("summary") or "")[:160]
    where = f", ~{km:.0f} km away" if km is not None else ""
    return f"- [{cat}{where}, {_age(ev.get('published') or ev.get('timestamp', ''))}] {ev.get('title', '')} — {summary}"


def _event_country(ev: dict):
    if ev.get("category") == "disaster":
        country = ((ev.get("facts") or {}).get("country") or "").strip().lower()
        return country or None
    if ev.get("category") == "geopolitics":
        return detect_mentioned_country(ev.get("title", "")) or detect_mentioned_country(ev.get("summary", ""))
    return None


def related_events(event: dict, all_events: list) -> list:
    """Candidate related events as (event, distance_km_or_None), newest
    first, at most RELATED_MAX — filtered per category by _RELATED_RULES."""
    others = [e for e in all_events if e.get("id") != event.get("id")]
    cat = event.get("category")

    if cat == "markets":
        markets = [(e, None) for e in others if e.get("category") == "markets"]
        news = [(e, None) for e in others if e.get("category") == "geopolitics"][:3]
        return (markets + news)[:RELATED_MAX + 3]

    rules = _RELATED_RULES.get(cat, {})
    country = _event_country(event)
    lat, lon = event.get("lat"), event.get("lon")
    picked = []
    for e in others:
        other_cat = e.get("category")
        km = None
        if None not in (lat, lon, e.get("lat"), e.get("lon")):
            km = haversine_km(lat, lon, e["lat"], e["lon"])
        max_km = rules.get(other_cat)
        near = isinstance(max_km, (int, float)) and km is not None and km <= max_km
        same_country = (other_cat == "geopolitics" and rules.get("geopolitics_same_country")
                        and country is not None and _event_country(e) == country)
        if near or same_country:
            picked.append((e, km))
    picked.sort(key=lambda p: p[0].get("published") or p[0].get("timestamp") or "", reverse=True)
    return picked[:RELATED_MAX]


def _facts_block(event: dict) -> str:
    facts = event.get("facts") or {}
    lines = [f"- {k.replace('_', ' ')}: {v}" for k, v in facts.items() if v]
    return ("Structured facts (dates are ISO yyyy-mm-dd):\n" + "\n".join(lines)) if lines else ""


async def build_context(session: aiohttp.ClientSession, event: dict, all_events: list) -> tuple[str, list]:
    parts, used = [], []

    facts = _facts_block(event)
    if facts:
        parts.append(facts)
        used.append("structured facts")

    lat, lon = event.get("lat"), event.get("lon")
    if lat is not None and lon is not None and event.get("category") in ("flight", "disaster", "geopolitics"):
        where = nearest_country(lat, lon)
        if where:
            parts.append(f"Location: {where} (lat {lat:.1f}, lon {lon:.1f}).")
            used.append("location")

    if event.get("category") == "geopolitics" and event.get("url"):
        article = await fetch_article_text(session, event["url"])
        if article:
            parts.append("Full article text (fetched from the source page):\n" + article)
            used.append("full article text")

    related = related_events(event, all_events)
    if related:
        label = ("Other market quotes and latest news headlines" if event.get("category") == "markets"
                 else "CANDIDATE related events (nearby or same country) — use only if clearly connected, "
                      "otherwise ignore them silently")
        parts.append(label + ":\n" + "\n".join(_line(e, km) for e, km in related))
        used.append(f"{len(related)} candidate related event{'s' if len(related) != 1 else ''}")

    return "\n\n".join(parts), used
