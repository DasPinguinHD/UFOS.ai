"""Shared geo lookup tables used by collectors that only know a country/city
name (not a lat/lon) — e.g. a news headline mentions a country, a GDACS
alert names one.

These are deliberately coarse (country- or city-centroid, not precise
geocoding): good enough to place a marker on the globe, not good enough for
anything that needs real precision. Where an article's country cannot be
matched, the collector should drop the event rather than guess (0, 0).
"""
import hashlib
import math
import re

# Country name -> (lat, lon) centroid. Keys are matched case-insensitively
# against GDELT's `sourcecountry` field, which spells out full country names
# (sometimes with quirks, e.g. "Burma" instead of "Myanmar") — both common
# spellings are included where they differ.
COUNTRY_CENTROIDS = {
    "united states": (39.8, -98.6), "usa": (39.8, -98.6),
    "united kingdom": (54.0, -2.5), "uk": (54.0, -2.5),
    "russia": (61.5, 105.3), "ukraine": (48.4, 31.2),
    "china": (35.9, 104.2), "taiwan": (23.7, 121.0),
    "japan": (36.2, 138.3), "south korea": (36.5, 127.9), "north korea": (40.3, 127.5),
    "india": (22.0, 79.0), "pakistan": (30.4, 69.3), "afghanistan": (33.9, 67.7),
    "iran": (32.4, 53.7), "iraq": (33.2, 43.7), "syria": (34.8, 38.9),
    "israel": (31.0, 34.9), "palestine": (31.9, 35.2), "lebanon": (33.9, 35.9),
    "yemen": (15.6, 48.0), "saudi arabia": (23.9, 45.1), "jordan": (31.2, 36.9),
    "turkey": (38.9, 35.2), "egypt": (26.8, 30.8), "libya": (26.3, 17.2),
    "sudan": (12.9, 30.2), "south sudan": (7.9, 30.0), "ethiopia": (9.1, 40.5),
    "somalia": (5.2, 46.2), "kenya": (0.0, 37.9), "nigeria": (9.1, 8.7),
    "mali": (17.6, -4.0), "niger": (17.6, 8.1), "chad": (15.5, 18.7),
    "south africa": (-30.6, 22.9), "morocco": (31.8, -7.1), "algeria": (28.0, 1.7),
    "myanmar": (21.9, 95.9), "burma": (21.9, 95.9), "thailand": (15.9, 100.9),
    "vietnam": (14.1, 108.3), "philippines": (12.9, 121.8), "indonesia": (-0.8, 113.9),
    "malaysia": (4.2, 101.9), "bangladesh": (23.7, 90.4), "sri lanka": (7.9, 80.8),
    "germany": (51.2, 10.5), "france": (46.6, 2.2), "poland": (51.9, 19.1),
    "italy": (41.9, 12.6), "spain": (40.5, -3.7), "netherlands": (52.1, 5.3),
    "belgium": (50.5, 4.5), "sweden": (60.1, 18.6), "finland": (61.9, 25.7),
    "norway": (60.5, 8.5), "denmark": (56.3, 9.5), "greece": (39.1, 21.8),
    "romania": (45.9, 24.9), "hungary": (47.2, 19.5), "austria": (47.5, 14.6),
    "switzerland": (46.8, 8.2), "portugal": (39.4, -8.2), "ireland": (53.4, -8.2),
    "serbia": (44.0, 21.0), "georgia": (42.3, 43.4), "armenia": (40.1, 45.0),
    "azerbaijan": (40.1, 47.6), "belarus": (53.7, 27.9), "moldova": (47.4, 28.4),
    "kazakhstan": (48.0, 66.9), "uzbekistan": (41.4, 64.6),
    "canada": (56.1, -106.3), "mexico": (23.6, -102.5),
    "brazil": (-14.2, -51.9), "argentina": (-38.4, -63.6), "chile": (-35.7, -71.5),
    "colombia": (4.6, -74.3), "venezuela": (6.4, -66.6), "peru": (-9.2, -75.0),
    "ecuador": (-1.8, -78.2), "bolivia": (-16.3, -63.6), "cuba": (21.5, -77.8),
    "haiti": (18.9, -72.3),
    "democratic republic of the congo": (-4.0, 21.8), "dr congo": (-4.0, 21.8),
    "congo": (-4.0, 21.8), "republic of the congo": (-0.2, 15.8),
    "australia": (-25.3, 133.8), "new zealand": (-41.0, 174.9),
    "qatar": (25.4, 51.2), "united arab emirates": (23.4, 53.8), "uae": (23.4, 53.8),
    "kuwait": (29.3, 47.5), "oman": (21.5, 55.9), "bahrain": (26.0, 50.5),
    # Added 2026-09-24 for the RSS news feeds (no source-country fallback).
    "kashmir": (34.1, 74.8),
    "mozambique": (-18.7, 35.5), "zimbabwe": (-19.0, 29.2), "zambia": (-13.1, 27.8),
    "angola": (-11.2, 17.9), "uganda": (1.4, 32.3), "rwanda": (-1.9, 29.9), "burundi": (-3.4, 29.9),
    "tanzania": (-6.4, 34.9), "ghana": (7.9, -1.0), "senegal": (14.5, -14.5), "cameroon": (7.4, 12.4),
    "burkina faso": (12.2, -1.6), "eritrea": (15.2, 39.8), "madagascar": (-18.8, 46.9),
    "tunisia": (33.9, 9.5), "central african republic": (6.6, 20.9), "ivory coast": (7.5, -5.5),
    "nepal": (28.4, 84.1), "cambodia": (12.6, 104.9), "laos": (19.9, 102.5), "mongolia": (46.9, 103.8),
    "singapore": (1.35, 103.8), "tajikistan": (38.9, 71.3), "kyrgyzstan": (41.2, 74.8),
    "turkmenistan": (38.97, 59.6),
    "kosovo": (42.6, 20.9), "bosnia": (43.9, 17.7), "croatia": (45.1, 15.2), "slovakia": (48.7, 19.7),
    "czech republic": (49.8, 15.5), "lithuania": (55.2, 23.9), "latvia": (56.9, 24.6),
    "estonia": (58.6, 25.0), "iceland": (64.96, -19.0), "north macedonia": (41.6, 21.7),
    "albania": (41.2, 20.2), "bulgaria": (42.7, 25.5), "slovenia": (46.2, 14.99), "cyprus": (35.1, 33.4),
    "guatemala": (15.8, -90.2), "honduras": (15.2, -86.2), "nicaragua": (12.9, -85.2),
    "el salvador": (13.8, -88.9), "panama": (8.5, -80.8), "dominican republic": (18.7, -70.2),
    "jamaica": (18.1, -77.3), "paraguay": (-23.4, -58.4), "uruguay": (-32.5, -55.8),
}

# Major financial centers, used to place market-quote markers.
FINANCIAL_CENTERS = {
    "^GSPC": {"name": "New York", "lat": 40.7, "lon": -74.0, "label": "S&P 500"},
    "^FTSE": {"name": "London", "lat": 51.5, "lon": -0.1, "label": "FTSE 100"},
    "000001.SS": {"name": "Shanghai", "lat": 31.2, "lon": 121.5, "label": "SSE Composite"},
    "^N225": {"name": "Tokyo", "lat": 35.7, "lon": 139.7, "label": "Nikkei 225"},
    "^GDAXI": {"name": "Frankfurt", "lat": 50.1, "lon": 8.7, "label": "DAX"},
}


def country_centroid(name: str):
    if not name:
        return None
    return COUNTRY_CENTROIDS.get(name.strip().lower())


# --- Headline-based location detection ---
#
# GDELT's DOC 2.0 API (artlist/json mode, what gdelt_collector.py and
# localnews.py use) gives only the source OUTLET's country, never a real
# per-article location — so placing every article at its source country's
# point conflates "who reported this" with "where it happened." In practice
# that's frequently wrong: e.g. a story headlined "Renewed fighting in north
# Ethiopia shatters peace deal..." picked up from a Syria-flagged outlet
# landed as a marker near Syria, nowhere close to Ethiopia (seen 2026-09-24).
#
# This is a coarse keyword scan over the headline text, not real NLP or
# geocoding — GDELT's richer Global Knowledge Graph (GKG) API does carry
# per-article locations, but switching the whole collector to a different
# GDELT API surface is a bigger change than this warrants right now. Checked
# in priority order (current conflict hotspots first): a headline mentioning
# "Ukraine" or "Gaza" is almost always actually about that place, and a
# hotspot mislabeled is a more visible failure than a large, low-salience
# country being missed. Falls back to the source country (the old behavior)
# when nothing in the headline matches anything known.
_LOCATION_KEYWORDS: list[tuple[str, str]] = [
    ("yemen", "yemen"), ("houthi", "yemen"), ("sanaa", "yemen"),
    ("gaza", "palestine"), ("west bank", "palestine"), ("rafah", "palestine"),
    ("israel", "israel"), ("israeli", "israel"), ("tel aviv", "israel"),
    ("hezbollah", "lebanon"), ("lebanon", "lebanon"), ("lebanese", "lebanon"), ("beirut", "lebanon"),
    ("iran", "iran"), ("iranian", "iran"), ("tehran", "iran"),
    ("ukraine", "ukraine"), ("ukrainian", "ukraine"), ("kyiv", "ukraine"), ("zelensky", "ukraine"),
    ("russia", "russia"), ("russian", "russia"), ("moscow", "russia"), ("kremlin", "russia"),
    ("south sudan", "south sudan"),
    ("sudan", "sudan"), ("khartoum", "sudan"),
    ("syria", "syria"), ("syrian", "syria"), ("damascus", "syria"),
    ("myanmar", "myanmar"), ("burma", "myanmar"), ("rohingya", "myanmar"),
    ("ethiopia", "ethiopia"), ("ethiopian", "ethiopia"), ("tigray", "ethiopia"), ("addis ababa", "ethiopia"),
    ("somalia", "somalia"), ("somali", "somalia"), ("mogadishu", "somalia"),
    ("haiti", "haiti"), ("haitian", "haiti"), ("port-au-prince", "haiti"),
    ("north korea", "north korea"), ("pyongyang", "north korea"), ("kim jong", "north korea"),
    ("taiwan", "taiwan"), ("taiwanese", "taiwan"), ("taipei", "taiwan"),
    ("kashmir", "kashmir"),
    ("sahel", "mali"),
    ("mali", "mali"), ("malian", "mali"), ("bamako", "mali"),
    ("nigeria", "nigeria"), ("nigerian", "nigeria"),
    ("niger", "niger"), ("niamey", "niger"),
    ("democratic republic of congo", "democratic republic of the congo"),
    ("dr congo", "democratic republic of the congo"), ("drc", "democratic republic of the congo"),
    ("kinshasa", "democratic republic of the congo"),
    ("congo", "congo"),
    ("venezuela", "venezuela"), ("venezuelan", "venezuela"), ("caracas", "venezuela"),
    ("afghanistan", "afghanistan"), ("afghan", "afghanistan"), ("kabul", "afghanistan"),
    ("pakistan", "pakistan"), ("pakistani", "pakistan"), ("islamabad", "pakistan"),
    ("china", "china"), ("chinese", "china"), ("beijing", "china"),
    ("india", "india"), ("indian", "india"), ("new delhi", "india"),
]

# 2026-09-24: with RSS feeds replacing GDELT there is no source-country
# fallback anymore (an RSS item has no country field at all), so detection
# has to cover the whole world, not just the hotspots above. Appended AFTER
# the hotspot list: every country in COUNTRY_CENTROIDS by name, plus common
# adjectives/capitals. Ambiguous short keys ("us", "uk" as the table's own
# aliases) are skipped or mapped explicitly; "uk" and "u.s." get explicit
# entries below so they match only as whole words.
_EXTRA_KEYWORDS: list[tuple[str, str]] = [
    ("u.s.", "united states"), ("united states", "united states"), ("american", "united states"),
    ("washington", "united states"), ("pentagon", "united states"), ("white house", "united states"),
    ("uk", "united kingdom"), ("britain", "united kingdom"), ("british", "united kingdom"),
    ("london", "united kingdom"), ("england", "united kingdom"), ("scotland", "united kingdom"),
    ("french", "france"), ("paris", "france"), ("german", "germany"), ("berlin", "germany"),
    ("italian", "italy"), ("rome", "italy"), ("spanish", "spain"), ("madrid", "spain"),
    ("polish", "poland"), ("warsaw", "poland"), ("dutch", "netherlands"), ("greek", "greece"),
    ("swedish", "sweden"), ("norwegian", "norway"), ("finnish", "finland"), ("danish", "denmark"),
    ("hungarian", "hungary"), ("romanian", "romania"), ("serbian", "serbia"), ("belarusian", "belarus"),
    ("turkish", "turkey"), ("turkiye", "turkey"), ("türkiye", "turkey"), ("ankara", "turkey"), ("istanbul", "turkey"),
    ("japanese", "japan"), ("tokyo", "japan"), ("south korea", "south korea"), ("south korean", "south korea"),
    ("seoul", "south korea"), ("north korean", "north korea"),
    ("saudi", "saudi arabia"), ("riyadh", "saudi arabia"), ("egyptian", "egypt"), ("cairo", "egypt"),
    ("iraqi", "iraq"), ("baghdad", "iraq"), ("jordanian", "jordan"), ("qatari", "qatar"), ("doha", "qatar"),
    ("emirati", "united arab emirates"), ("dubai", "united arab emirates"), ("libyan", "libya"),
    ("kenyan", "kenya"), ("nairobi", "kenya"), ("south african", "south africa"), ("moroccan", "morocco"),
    ("algerian", "algeria"), ("chadian", "chad"),
    ("indonesian", "indonesia"), ("jakarta", "indonesia"), ("philippine", "philippines"), ("manila", "philippines"),
    ("vietnamese", "vietnam"), ("hanoi", "vietnam"), ("thai", "thailand"), ("bangkok", "thailand"),
    ("malaysian", "malaysia"), ("bangladeshi", "bangladesh"), ("dhaka", "bangladesh"), ("sri lankan", "sri lanka"),
    ("mexican", "mexico"), ("brazilian", "brazil"), ("brasilia", "brazil"), ("argentine", "argentina"),
    ("chilean", "chile"), ("colombian", "colombia"), ("bogota", "colombia"), ("peruvian", "peru"),
    ("ecuadorian", "ecuador"), ("bolivian", "bolivia"), ("cuban", "cuba"), ("havana", "cuba"),
    ("canadian", "canada"), ("ottawa", "canada"), ("australian", "australia"), ("canberra", "australia"),
    ("new zealand", "new zealand"), ("armenian", "armenia"), ("azerbaijani", "azerbaijan"),
    ("georgian", "georgia"), ("moldovan", "moldova"), ("kazakh", "kazakhstan"), ("uzbek", "uzbekistan"),
    ("czech", "czech republic"), ("cote d'ivoire", "ivory coast"), ("côte d'ivoire", "ivory coast"),
    ("bosnian", "bosnia"), ("croatian", "croatia"), ("cypriot", "cyprus"), ("mozambican", "mozambique"),
    ("ugandan", "uganda"), ("rwandan", "rwanda"), ("ghanaian", "ghana"), ("senegalese", "senegal"),
    ("cameroonian", "cameroon"), ("eritrean", "eritrea"), ("tunisian", "tunisia"), ("nepali", "nepal"),
    ("cambodian", "cambodia"), ("mongolian", "mongolia"), ("guatemalan", "guatemala"),
    ("honduran", "honduras"), ("nicaraguan", "nicaragua"), ("salvadoran", "el salvador"),
    ("panamanian", "panama"), ("jamaican", "jamaica"),
]
_already = {kw for kw, _ in _LOCATION_KEYWORDS} | {kw for kw, _ in _EXTRA_KEYWORDS}
_EXTRA_KEYWORDS += [(name, name) for name in COUNTRY_CENTROIDS
                    if name not in _already and name not in ("us", "usa", "uk", "uae")]
_LOCATION_KEYWORDS = _LOCATION_KEYWORDS + _EXTRA_KEYWORDS
_LOCATION_PATTERN = None


def detect_mentioned_country(text: str):
    """Scans free text (typically a headline) for the first hotspot/country
    it mentions, checked in the priority order in `_LOCATION_KEYWORDS`, and
    returns a `COUNTRY_CENTROIDS` key if found, else `None`. Word-boundary
    matching (so "niger" doesn't also match inside "nigerian") via a single
    compiled regex, built once and cached.
    """
    global _LOCATION_PATTERN
    if not text:
        return None
    if _LOCATION_PATTERN is None:
        alternatives = "|".join(re.escape(kw) for kw, _ in _LOCATION_KEYWORDS)
        _LOCATION_PATTERN = re.compile(r"\b(" + alternatives + r")\b", re.IGNORECASE)
    matches = list(_LOCATION_PATTERN.finditer(text))
    if not matches:
        return None
    # The LAST mention wins, not the first: English headlines are
    # overwhelmingly "[actor] does [action] in/on/to [place]"
    # ("Russian strikes on Ukraine", "Israel hits Hezbollah targets in
    # Lebanon") — the actor is usually named first, the actual location of
    # the event usually last. Picking the first match would put "Russian
    # strikes on Ukraine" in Russia, which is backwards.
    hit = matches[-1].group(1).lower()
    for keyword, country in _LOCATION_KEYWORDS:
        if keyword == hit:
            return country
    return None


# Approximate half-extent (degrees) to spread multiple same-country events
# across roughly the country's real footprint instead of stacking them all on
# one point. GDELT only gives a source *country*, never a real per-article
# location, so a single centroid per country (the old behavior) meant every
# US story — and GDELT skews heavily toward US/UK sources simply because
# that's where most English-language reporting comes from — landed on the
# exact same pixel, reading as one big cluster instead of many stories. This
# is deliberately coarse (three size tiers, not real borders): good enough to
# turn "one blob" into "a spread across the country," not a real geocode.
_SPREAD_LARGE = 9.0    # very large countries
_SPREAD_MEDIUM = 3.5   # mid-sized countries
_SPREAD_SMALL = 1.0    # compact countries (default for anything not listed)

_LARGE_COUNTRIES = (
    "united states", "usa", "russia", "canada", "china", "brazil", "australia",
    "india", "kazakhstan", "argentina", "algeria",
)
_MEDIUM_COUNTRIES = (
    "iran", "saudi arabia", "mexico", "indonesia", "libya", "sudan", "chad",
    "niger", "mali", "south africa", "egypt", "turkey", "ukraine", "pakistan",
    "afghanistan", "mongolia", "peru", "bolivia", "colombia", "venezuela",
    "nigeria", "ethiopia", "somalia", "myanmar", "burma", "france", "spain",
    "sweden", "norway", "finland", "poland", "germany", "japan",
    "democratic republic of the congo",
)


def _country_spread_degrees(name: str) -> float:
    key = name.strip().lower()
    if key in _LARGE_COUNTRIES:
        return _SPREAD_LARGE
    if key in _MEDIUM_COUNTRIES:
        return _SPREAD_MEDIUM
    return _SPREAD_SMALL


def country_point(name: str, seed: str):
    """Like country_centroid(), but offsets the point deterministically
    (based on `seed`, e.g. the article's URL or id — not random, so the same
    article always lands at the same spot rather than jumping around on
    every poll) within a size-appropriate radius of the country's centroid.
    Returns None if the country isn't in COUNTRY_CENTROIDS.
    """
    centroid = country_centroid(name)
    if not centroid:
        return None
    lat, lon = centroid
    spread = _country_spread_degrees(name)

    digest = hashlib.sha1((seed or "").encode("utf-8", "ignore")).digest()
    lat_frac = (digest[0] / 255.0) * 2 - 1   # deterministic value in [-1, 1)
    lon_frac = (digest[1] / 255.0) * 2 - 1

    lat_offset = lat_frac * spread
    # Longitude degrees get narrower (in real distance) the further from the
    # equator; widen the offset accordingly so the spread looks roughly even
    # on the globe instead of squashed near high latitudes.
    lon_offset = lon_frac * spread / max(0.15, math.cos(math.radians(lat)))

    return (lat + lat_offset, lon + lon_offset)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# Alias keys in COUNTRY_CENTROIDS that duplicate another entry's point.
_CENTROID_ALIASES = {"usa", "uk", "uae", "burma", "dr congo", "congo"}


def nearest_country(lat: float, lon: float):
    """Coarse "where is this?" for markers that only have lat/lon (flights,
    disasters), used as context for the Assess button. Based on country
    CENTROIDS, not borders — so it's phrased as approximate, and for large
    countries (Russia, Canada, ...) a point near a border can resolve to the
    neighbour. Returns e.g. "approximately over/near Poland" or
    "open water, ~900 km from the nearest listed country (Greece)"."""
    best, best_d = None, None
    for name, (clat, clon) in COUNTRY_CENTROIDS.items():
        if name in _CENTROID_ALIASES:
            continue
        d = haversine_km(lat, lon, clat, clon)
        if best_d is None or d < best_d:
            best, best_d = name, d
    if best is None:
        return None
    pretty = " ".join(w if w in ("of", "the", "and") else w.capitalize() for w in best.split())
    if best_d <= 700 or best in _LARGE_COUNTRIES:
        return f"approximately over/near {pretty}"
    return f"possibly open water or a smaller country; nearest listed country: {pretty} (~{best_d:.0f} km from its centre)"
