"""World news (RSS) — the "Geopolitics & Conflict" layer.

Replaces the GDELT collector (2026-09-24; see news_feeds.py for why). Every
NEWS_POLL_SECONDS it fetches the world-news RSS feeds in news_feeds.FEEDS and
turns relevant, recent, locatable stories into "geopolitics" events:

- recent: published within NEWS_MAX_AGE_HOURS (feeds keep older items);
- relevant: relevance() scores headline + description for security AND
  economy/markets terms (world feeds also carry sport, culture, religion);
- locatable: geo.detect_mentioned_country() finds the country the story is
  ABOUT in the headline (falling back to the description). RSS items carry
  no country field at all, so a story that can't be located is left off the
  globe rather than guessed.

Per poll, at most MAX_PER_COUNTRY_PER_POLL stories per country, so one big
story (many outlets covering the same event) can't fill the whole map.
"""
import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp

import news_feeds
from config import NEWS_POLL_SECONDS, NEWS_MAX_AGE_HOURS
from events import make_event
from geo import country_point, detect_mentioned_country

logger = logging.getLogger(__name__)

MAX_PER_COUNTRY_PER_POLL = 4
SUMMARY_MAX_CHARS = 320

# --- Relevance scoring (2026-09-24, replaces a single broad keyword regex) ---
# One weak hit used to be enough: "war" in "prisoner of war" let a Nazi-era
# trial through, "rebel" a story about a Catholic sect, and a pop-star
# profile slipped in too. The layer is meant for what moves markets and
# security: geopolitics, conflict AND macro/economy. Scoring:
#   strong term in headline = 4, strong in teaser = 2,
#   medium term in headline = 2, medium in teaser = 1.
# Kept if score >= KEEP_SCORE and at least one strong term appears.
# Excluded topics (culture, sport, religion, celebrity, WWII-era history)
# are dropped unless the HEADLINE itself carries a strong term.

_STRONG = re.compile(
    r"\b(wars?|warfare|invasion|invade[sd]?|airstrikes?|air strikes?|missiles?|drone strikes?|drone attacks?|"
    r"shelling|bombardment|troops|military|army|navy|ceasefire|truce|coup|sanctions?|embargo|blockade|"
    r"nuclear|insurgen\w+|militants?|militias?|rebels|rebel (?:forces|fighters|groups?|army)|jihadists?|terror\w*|hostages?|annex\w*|"
    r"martial law|state of emergency|genocide|ethnic cleansing|"
    r"tariffs?|trade war|export (?:ban|controls?)|central bank|interest rates?|rate (?:cut|hike)s?|"
    r"inflation|recession|gdp|default(?:ed|s)?|debt crisis|bailout|devaluation|currency|bond yields?|"
    r"oil prices?|crude|opec|gas supply|pipelines?|supply chains?|stock markets?|sell-?off|"
    r"elections?|referendum|mass protests?|general strike|uprising|refugee crisis)\b",
    re.IGNORECASE,
)
_MEDIUM = re.compile(
    r"\b(government|president|prime minister|parliament|ministers?|cabinet|talks|summit|diplomat\w*|"
    r"embassy|protests?|protesters|killed|dead|attacks?|clashes|border|economy|economic|markets?|"
    r"stocks?|shares|trade|budget|deficit|investment|investors|energy|shortages?|crisis|"
    r"nato|united nations|un general assembly|general assembly|security council|european union|eu|imf|world bank|"
    r"g7|g20|opposition|regime)\b",
    re.IGNORECASE,
)
_EXCLUDE = re.compile(
    r"\b(singers?|songs?|albums?|music|musicians?|rappers?|concerts?|festivals?|films?|movies?|actors?|actress|"
    r"celebrity|celebrities|superstar|grammy|oscars?|fashion|tv series|netflix|"
    r"football|soccer|tennis|cricket|rugby|olympic\w*|league|championship|tournament|match|goals?|coach|"
    r"pope|vatican|church|bishops?|catholic|sect|excommunicat\w*|saints?|"
    r"museum|exhibition|art|artists?|recipes?|royal wedding|obituary|dies aged|anniversary|"
    r"nazi|holocaust|auschwitz|world war (?:i|ii|one|two|1|2)|wwii|ww2)\b",
    re.IGNORECASE,
)
# Phrases where a strong word doesn't mean a current conflict.
_NEUTRALIZE = re.compile(r"\b(prisoners? of war|world war (?:i|ii|one|two|1|2)|cold war|war crimes trials?|"
                         r"war memorial|price war|culture war|star wars)\b", re.IGNORECASE)
KEEP_SCORE = 4


def relevance(title: str, summary: str) -> tuple[int, bool]:
    """Returns (score, keep)."""
    t = _NEUTRALIZE.sub(" ", title or "")
    d = _NEUTRALIZE.sub(" ", summary or "")
    strong_t = len(_STRONG.findall(t))
    strong_d = len(_STRONG.findall(d))
    score = 4 * strong_t + 2 * strong_d + 2 * len(_MEDIUM.findall(t)) + len(_MEDIUM.findall(d))
    if _EXCLUDE.search(title or "") and strong_t == 0:
        return score, False
    return score, (strong_t + strong_d) > 0 and score >= KEEP_SCORE


def _event_id(url: str) -> str:
    return "news-" + hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:16]


async def _poll_once(session: aiohttp.ClientSession, emit, report_status) -> None:
    items, results = await news_feeds.fetch_all(session)
    ok_feeds = [name for name, count, err in results if err is None]
    failed = [f"{name}: {err}" for name, _, err in results if err is not None]

    if not ok_feeds:
        await report_status(False, "all news feeds failed — " + "; ".join(failed)[:300])
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_MAX_AGE_HOURS)
    # newest first, so the per-country cap keeps the freshest stories
    items.sort(key=lambda i: i["published"] or cutoff, reverse=True)

    seen = set()
    per_country: dict = {}
    placed = skipped_old = skipped_topic = skipped_location = 0
    for item in items:
        eid = _event_id(item["url"])
        if eid in seen:
            continue
        seen.add(eid)

        if item["published"] and item["published"] < cutoff:
            skipped_old += 1
            continue
        _, keep = relevance(item["title"], item["summary"])
        if not keep:
            skipped_topic += 1
            continue
        country = detect_mentioned_country(item["title"]) or detect_mentioned_country(item["summary"])
        if not country:
            skipped_location += 1
            continue
        if per_country.get(country, 0) >= MAX_PER_COUNTRY_PER_POLL:
            continue
        point = country_point(country, item["url"])
        if not point:
            skipped_location += 1
            continue

        summary = item["summary"]
        if len(summary) > SUMMARY_MAX_CHARS:
            summary = summary[:SUMMARY_MAX_CHARS - 1].rsplit(" ", 1)[0] + "…"
        event = make_event(
            event_id=eid,
            category="geopolitics",
            title=item["title"],
            summary=summary or item["title"],
            source=item["source"],
            lat=point[0], lon=point[1],
            url=item["url"],
        )
        if item["published"]:
            event["published"] = item["published"].isoformat()
        emit(event)
        placed += 1
        per_country[country] = per_country.get(country, 0) + 1

    msg = (f"{placed} stories placed from {len(ok_feeds)}/{len(results)} feeds "
           f"(skipped: {skipped_topic} off-topic, {skipped_location} no location, {skipped_old} older than {NEWS_MAX_AGE_HOURS:.0f}h)")
    if failed:
        msg += " — failed: " + ", ".join(name for name, _, err in results if err is not None)
    await report_status(True, msg)


async def run(emit, report_status) -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await _poll_once(session, emit, report_status)
            except Exception as ex:
                logger.exception("News poll crashed")
                await report_status(False, f"{type(ex).__name__}: {ex}")
            await asyncio.sleep(NEWS_POLL_SECONDS)
