"""Financial Newsroom — "Analysis & Reasoning" overview (added 2026-09-25).

Turns everything the other newsroom sections already hold in memory (FRED
macro cards, derived indicators, the macro regime, SEC Form 4 filings and the
economic calendar) into one flat list of TILES the user can pick from, plus a
few preset TEMPLATES ("Recession check", ...). The picked tiles are rendered
into a compact plain-text context (build_analysis_context) that
POST /newsroom/analysis sends to the model (newsroom/ai.analyse_selection).

Pure functions only — no I/O, no network, no model calls — so the contract
below can be tested offline (tests/test_overview.py).

Contract of build_overview() (the WPF view codes against it):

  {"tiles": [Tile, ...], "templates": [Template, ...], "generated_at": iso-utc}

  Tile = {"id", "group", "title", "value", "trend", "change", "level",
          "subtitle", "as_of", "facts"}  — all strings
    group:  "Macro Regime" | "US Macro" | "Euro Area" | "Derived" |
            "Insider Trades" | "Calendar"
    trend:  "up" | "down" | "flat" | "none"
    level:  "ok" | "watch" | "alert" | "info"
    facts:  2-4 compact plain-text lines with every number the model needs
  Tile ids (stable): "regime", "macro:<SERIES_ID>" (US + euro area),
    "derived:<KEY>", "insider:sentiment", "insider:cluster:<n>",
    "insider:top:<n>", "calendar:<YYYY-MM-DD>:<SERIES_ID|fomc>"
  Template = {"id", "label", "description", "tile_ids": [...]} — only ids
    that exist; templates with fewer than 2 tiles are dropped.

Insider sentiment level (a judgement call, documented here): insiders are
structural net sellers (compensation, diversification), so a sell-heavy mix is
normal ("info"). The tile turns "watch" when open-market BUYING is unusually
strong in the loaded filings: at least 3 buy trades AND buys >= half the
number of sells, or at least 5 distinct buyers. Rule 10b5-1 planned trades are
excluded (scheduled months in advance, they express no current view). It is
never "alert" — insider buying is not a stress signal.
"""
from datetime import date, datetime, timezone

from . import fred_collector, macro_derived
from .insider_summary import find_clusters, _issuer_key, _owner_key, _parse_date

GROUP_REGIME = "Macro Regime"
GROUP_US = "US Macro"
GROUP_EUROPE = "Euro Area"
GROUP_DERIVED = "Derived"
GROUP_INSIDER = "Insider Trades"
GROUP_CALENDAR = "Calendar"

MAX_CLUSTER_TILES = 4
MAX_TOP_TRADES = 3
MAX_CALENDAR_RELEASES = 6
CONTEXT_DAYS = 30
CONTEXT_MAX_DATES = 8
# Above this many selected tiles the context drops the "About:" (how to read it)
# lines to stay compact; values, signals and dates are always kept.
CONTEXT_FULL_FACTS_MAX = 12

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_LEVELS = {"ok", "watch", "alert", "info"}
_ROUTINE_CODES = {"A", "F", "M", "X", "G", "C"}


# --- small formatting helpers -------------------------------------------------

def _month_label(iso: str) -> str:
    try:
        return f"{_MONTHS[int(iso[5:7]) - 1]} {iso[:4]}"
    except (ValueError, IndexError, TypeError):
        return iso or ""


def _day_label(iso: str) -> str:
    try:
        d = date.fromisoformat(iso[:10])
        return f"{_MONTHS[d.month - 1]} {d.day}"
    except (ValueError, TypeError):
        return iso or ""


def _fmt_value(v, unit: str) -> str:
    if v is None:
        return "n/a"
    if unit == "%":
        return f"{v:.2f}%"
    if unit == "pp":
        return f"{v:.2f} pp"
    if unit == "K":
        return f"{v:,.0f}K"
    if unit == "pts":
        return f"{v:.1f}"
    return f"{v:.2f} {unit}".strip()


def _fmt_change(c, unit: str) -> str:
    if c is None:
        return ""
    if unit in ("%", "pp"):
        return f"{c:+.2f} pp"
    if unit == "K":
        return f"{c:+,.0f}K"
    if unit == "pts":
        return f"{c:+.1f}"
    return f"{c:+.2f} {unit}".strip()


def _trend(c, unit: str) -> str:
    if c is None:
        return "none"
    eps = 0.5 if unit == "K" else 0.005
    return "up" if c > eps else "down" if c < -eps else "flat"


def _money(v) -> str:
    if v is None:
        return "n/a"
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:,.0f}"


def _level(x) -> str:
    return x if x in _LEVELS else "info"


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    cut = text.find(". ")
    return text if cut < 0 else text[:cut + 1]


def _tile(tid, group, title, value, trend="none", change="", level="info", subtitle="", as_of="", facts="") -> dict:
    return {"id": tid, "group": group, "title": title, "value": value, "trend": trend,
            "change": change, "level": _level(level), "subtitle": subtitle, "as_of": as_of, "facts": facts}


# --- macro ----------------------------------------------------------------------

def _is_monthly(series_id: str, group: str) -> bool:
    return group == "derived" or series_id in fred_collector.MONTHLY_SERIES


def _macro_tile(tid: str, group_label: str, title: str, m: dict) -> dict:
    sid, unit = m.get("series_id", ""), m.get("unit", "")
    value, change = m.get("value"), m.get("change")
    monthly = _is_monthly(sid, m.get("group", ""))
    as_of = _month_label(m.get("date", "")) if monthly else (m.get("date") or "")
    sig = m.get("signal") or {}
    ctx = (m.get("context") or {}).get("text", "")
    level = sig.get("level") or "info"
    if level in ("watch", "alert") and sig.get("reason"):
        subtitle = sig["reason"]
    elif ctx:
        subtitle = ctx
    else:
        subtitle = _first_sentence(m.get("note", ""))

    line1 = f"Value {_fmt_value(value, unit)} ({as_of})"
    if m.get("previous") is not None:
        prev_date = m.get("previous_date") or ""
        line1 += (f"; previous {_fmt_value(m['previous'], unit)} "
                  f"({_month_label(prev_date) if monthly else prev_date}); change {_fmt_change(change, unit)}")
    lines = [line1]
    line2 = []
    if ctx:
        line2.append(f"History: {ctx}")
    if sig.get("level"):
        line2.append(f"Signal: {sig['level']} — {sig.get('reason') or ''}".rstrip(" —"))
    if line2:
        lines.append(" · ".join(line2))
    nr = m.get("next_release") or {}
    extra = []
    if nr.get("date"):
        extra.append(f"Next release {nr['date']}" + (f" ({nr['release']})" if nr.get("release") else ""))
    if m.get("formula"):
        extra.append(f"Formula: {m['formula']}")
    if extra:
        lines.append(" · ".join(extra))
    if m.get("note"):
        lines.append(f"About: {m['note']}")
    return _tile(tid, group_label, title, _fmt_value(value, unit), _trend(change, unit),
                 f"{_fmt_change(change, unit)} vs. prev." if change is not None else "",
                 level, subtitle, as_of, "\n".join(lines))


def _regime_tile(reg: dict) -> dict:
    n = reg.get("months_in_regime") or 0
    change = f"{n} month{'s' if n != 1 else ''} in this regime" if n else ""
    if reg.get("borderline"):
        change += (" · " if change else "") + "near a boundary"
    facts = [
        f"Regime {reg.get('label', '?')} ({reg.get('quadrant', '?')}) as of {_month_label(reg.get('date', ''))}; "
        f"{n} month(s) in this quadrant; borderline: {'yes' if reg.get('borderline') else 'no'}",
        f"{reg.get('growth_text', '')} · {reg.get('inflation_text', '')}".strip(" ·"),
        f"About: {reg.get('description', '')}",
    ]
    return _tile("regime", GROUP_REGIME, "Macro Regime", reg.get("label") or "?", "none", change,
                 reg.get("level") or "info", _first_sentence(reg.get("description", "")),
                 _month_label(reg.get("date", "")), "\n".join(f for f in facts if f.strip()))


def _macro_tiles(macro_latest: dict) -> list:
    by_sid = {}
    regime = None
    for e in (macro_latest or {}).values():
        m = (e or {}).get("macro") or {}
        if m.get("group") == "regime":
            regime = m.get("regime")
        elif m.get("series_id"):
            by_sid[m["series_id"]] = (e, m)

    tiles = []
    if regime:
        tiles.append(_regime_tile(regime))
    labels = {sid: label for sid, label, *_ in fred_collector.SERIES}
    us, eu = [], []
    for sid, *_ in fred_collector.SERIES:
        if sid not in by_sid or by_sid[sid][1].get("value") is None:
            continue
        e, m = by_sid[sid]
        is_eu = (m.get("group") or fred_collector.GROUP.get(sid, "us")) == "europe"
        t = _macro_tile(f"macro:{sid}", GROUP_EUROPE if is_eu else GROUP_US, e.get("title") or labels[sid], m)
        (eu if is_eu else us).append(t)
    tiles += us + eu
    for key, (label, *_rest) in macro_derived.DERIVED.items():
        if key not in by_sid or by_sid[key][1].get("value") is None:
            continue
        e, m = by_sid[key]
        tiles.append(_macro_tile(f"derived:{key}", GROUP_DERIVED, e.get("title") or label, m))
    return tiles


# --- insider --------------------------------------------------------------------

def _open_market_trades(insider_events: list) -> list:
    """One entry per (filing, direction) with open-market non-derivative
    transactions (P = buy, S = sell), 10b5-1 planned filings excluded and
    amendments (4/A repeating the same transactions) de-duplicated."""
    seen = set()
    trades = []
    for e in insider_events or []:
        d = (e or {}).get("details")
        if not d or d.get("plan_10b5_1"):
            continue
        for code, kind in (("P", "buy"), ("S", "sell")):
            txs = [t for t in d.get("transactions") or [] if t.get("code") == code and not t.get("derivative")]
            if not txs:
                continue
            key = (_issuer_key(d), _owner_key(d), kind,
                   tuple(sorted((t.get("date") or "", t.get("shares") or 0, t.get("price") or 0) for t in txs)))
            if key in seen:
                continue
            seen.add(key)
            dates = sorted(t.get("date")[:10] for t in txs if t.get("date"))
            trades.append({
                "kind": kind, "details": d,
                "value": sum(t.get("value_usd") or 0 for t in txs),
                "shares": sum(t.get("shares") or 0 for t in txs),
                "date": dates[-1] if dates else (e.get("filed_at") or "")[:10],
                "owner": _owner_key(d),
            })
    return trades


def _who(d: dict) -> str:
    roles = d.get("owner_roles") or []
    return f"{d.get('owner_name') or '?'} ({roles[0] if roles else 'insider'})"


def _company(d: dict) -> str:
    name, ticker = d.get("issuer_name") or "", d.get("issuer_ticker") or ""
    return f"{name} ({ticker})" if name and ticker else (name or ticker or "?")


def _insider_tiles(insider_events: list) -> list:
    events = [e for e in insider_events or [] if (e or {}).get("details")]
    if not events:
        return []
    tiles = []
    trades = _open_market_trades(events)
    planned = sum(1 for e in events if e["details"].get("plan_10b5_1"))
    buys = [t for t in trades if t["kind"] == "buy"]
    sells = [t for t in trades if t["kind"] == "sell"]
    nb, ns = len(buys), len(sells)
    buy_usd, sell_usd = sum(t["value"] for t in buys), sum(t["value"] for t in sells)
    buyers = len({t["owner"] for t in buys})
    buy_cos = len({_issuer_key(t["details"]) for t in buys})
    unusual = (nb >= 3 and nb * 2 >= ns) or buyers >= 5
    trend = "none" if nb + ns == 0 else "up" if nb > ns else "down" if ns > nb else "flat"
    filed = sorted((e.get("filed_at") or "")[:10] for e in events if e.get("filed_at"))
    span = f"{filed[0]} – {filed[-1]}" if filed and filed[0] != filed[-1] else (filed[-1] if filed else "")
    tiles.append(_tile(
        "insider:sentiment", GROUP_INSIDER, "Insider buying vs. selling",
        f"{nb} buy{'s' if nb != 1 else ''} / {ns} sell{'s' if ns != 1 else ''}", trend,
        f"{_money(buy_usd)} bought vs. {_money(sell_usd)} sold",
        "watch" if unusual else "info",
        (f"{buyers} distinct buyer{'s' if buyers != 1 else ''} in {buy_cos} compan{'ies' if buy_cos != 1 else 'y'}; "
         "planned 10b5-1 trades excluded"),
        f"filings {span}" if span else "",
        "\n".join([
            f"Open-market trades in the {len(events)} loaded Form 4 filings (filed {span or 'n/a'}): "
            f"{nb} purchase(s) totalling {_money(buy_usd)} by {buyers} distinct insider(s) in {buy_cos} "
            f"compan{'ies' if buy_cos != 1 else 'y'}; {ns} sale(s) totalling {_money(sell_usd)}.",
            f"{planned} filing(s) under Rule 10b5-1 trading plans excluded (pre-scheduled). Grants, option "
            "exercises and tax withholding not counted. Insiders are structural net sellers, so sells "
            "outnumbering buys is normal; unusually many buys is the notable case.",
        ])))

    for i, c in enumerate(find_clusters(events)[:MAX_CLUSTER_TILES], start=1):
        buy = c["kind"] == "buy"
        who = c.get("issuer_ticker") or c.get("issuer_name") or "?"
        names = ", ".join(c.get("insiders") or [])
        tiles.append(_tile(
            f"insider:cluster:{i}", GROUP_INSIDER, f"Cluster {'buy' if buy else 'sell'}: {who}",
            f"{c['count']} insiders", "up" if buy else "down", f"{_money(c['total_usd'])} total", "watch",
            names if len(names) <= 90 else names[:89] + "…",
            f"{c['first_date']} – {c['last_date']}" if c["first_date"] != c["last_date"] else c["last_date"],
            "\n".join([
                f"{c['count']} different insiders of {c.get('issuer_name') or who}"
                f"{' (' + c['issuer_ticker'] + ')' if c.get('issuer_ticker') else ''} "
                f"{'bought' if buy else 'sold'} on the open market between {c['first_date']} and {c['last_date']}, "
                f"together {_money(c['total_usd'])}.",
                f"Insiders: {names}." + ("" if buy else " Planned 10b5-1 sales excluded."),
            ])))

    ranked = (sorted(buys, key=lambda t: -t["value"]) + sorted(sells, key=lambda t: -t["value"]))
    for i, t in enumerate(ranked[:MAX_TOP_TRADES], start=1):
        d, buy = t["details"], t["kind"] == "buy"
        ticker = d.get("issuer_ticker") or d.get("issuer_name") or "?"
        avg = t["value"] / t["shares"] if t["shares"] else None
        change = f"{t['shares']:,.0f} shares" + (f" @ ${avg:,.2f}" if avg else "")
        tiles.append(_tile(
            f"insider:top:{i}", GROUP_INSIDER, f"{_who(d)} {'bought' if buy else 'sold'} {ticker}",
            _money(t["value"]), "up" if buy else "down", change, "watch" if buy else "info",
            f"{d.get('issuer_name') or ticker} · open-market {'purchase' if buy else 'sale'} (not pre-planned)",
            t["date"],
            "\n".join([
                f"{_who(d)} {'bought' if buy else 'sold'} {change} of {_company(d)} on the open market "
                f"on {t['date']}, about {_money(t['value'])}.",
                f"Roles: {', '.join(d.get('owner_roles') or []) or 'insider'}. Not under a Rule 10b5-1 plan. "
                + ("Open-market purchases with own money are rarer and more telling than sales."
                   if buy else "Largest unplanned open-market sale in the loaded filings (sales have many motives)."),
            ])))
    return tiles


# --- calendar -------------------------------------------------------------------

def _days_text(n: int) -> str:
    return "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"


def _calendar_selection(events: list, today: date, max_releases: int = MAX_CALENDAR_RELEASES) -> list:
    """Next MAX_CALENDAR_RELEASES high-importance releases + every FOMC decision
    in the window, date order."""
    upcoming = []
    for ev in events or []:
        try:
            d = date.fromisoformat((ev.get("date") or "")[:10])
        except ValueError:
            continue
        if d >= today:
            upcoming.append((d, ev))
    upcoming.sort(key=lambda x: (x[0], x[1].get("time_hint") or "99"))
    releases = [x for x in upcoming if x[1].get("kind") == "release" and x[1].get("importance") == "high"]
    fomc = [x for x in upcoming if x[1].get("kind") == "fomc"]
    picked = releases[:max_releases] + fomc
    picked.sort(key=lambda x: (x[0], x[1].get("time_hint") or "99"))
    return picked


def _calendar_tiles(calendar: dict, today: date) -> list:
    tiles, seen = [], set()
    for d, ev in _calendar_selection((calendar or {}).get("events") or [], today):
        suffix = "fomc" if ev.get("kind") == "fomc" else (ev.get("series_id") or "release")
        tid = f"calendar:{d.isoformat()}:{suffix}"
        if tid in seen:
            continue
        seen.add(tid)
        n = (d - today).days
        last = ev.get("last_display") or ""
        facts = [f"{ev.get('name') or '?'} on {d.isoformat()} ({d.strftime('%a')})"
                 + (f", typically {ev['time_hint']}" if ev.get("time_hint") else "")
                 + f" — {_days_text(n)}."
                 + (" Includes the Summary of Economic Projections (dot plot)." if ev.get("sep") else "")]
        if last:
            facts.append(f"Last value: {last}.")
        if ev.get("why"):
            facts.append(f"Why it matters: {ev['why']}")
        tiles.append(_tile(tid, GROUP_CALENDAR, ev.get("name") or "?", _day_label(d.isoformat()), "none",
                           f"last: {last}" if last else "", "info", ev.get("why") or "",
                           f"{_days_text(n)} · {_day_label(d.isoformat())}", "\n".join(facts)))
    return tiles


# --- templates ------------------------------------------------------------------

def _templates(tiles: list) -> list:
    ids = [t["id"] for t in tiles]
    have = set(ids)

    def first(pred):
        return next((i for i in ids if pred(i)), None)

    cal_inflation = first(lambda i: i.startswith("calendar:") and i.split(":")[-1] in ("CPIAUCSL", "PCEPILFE"))
    cal_fomc = first(lambda i: i.startswith("calendar:") and i.endswith(":fomc"))
    specs = [
        ("recession", "Recession check",
         "Yield curve, Sahm rule, jobless claims, unemployment and credit spreads — do the classic "
         "recession warnings agree?",
         ["macro:T10Y3M", "macro:T10Y2Y", "derived:CURVE_UNINVERSION", "derived:SAHM_RULE", "macro:ICSA",
          "macro:UNRATE", "macro:BAMLH0A0HYM2", "regime"]),
        ("inflation", "Inflation check",
         "Headline and core inflation against the policy rate — is the Fed restrictive enough?",
         ["macro:CPIAUCSL", "macro:PCEPILFE", "derived:REAL_POLICY_RATE", "macro:FEDFUNDS", "macro:DGS2",
          "regime", cal_inflation]),
        ("rates", "Rates & credit stress",
         "Policy rate, Treasury curve, credit spreads and volatility — where is financial stress building?",
         ["macro:FEDFUNDS", "macro:DGS2", "macro:DGS10", "macro:T10Y2Y", "macro:BAMLH0A0HYM2", "macro:VIXCLS",
          "derived:REAL_POLICY_RATE", cal_fomc]),
        ("europe", "Euro area vs. US",
         "ECB vs. Fed, Bund vs. Treasury, HICP vs. CPI — how far apart are the two cycles?",
         ["macro:ECBDFR", "macro:IRLTLT01DEM156N", "macro:CP0000EZ19M086NEST", "macro:FEDFUNDS", "macro:DGS10",
          "macro:CPIAUCSL"]),
        ("insiders", "Insider signals in context",
         "Insider buying/selling, clusters and the largest trades next to market fear and the macro regime.",
         [i for i in ids if i.startswith("insider:")] + ["macro:VIXCLS", "regime"]),
    ]
    out = []
    for tid, label, description, wanted in specs:
        tile_ids = []
        for i in wanted:
            if i and i in have and i not in tile_ids:
                tile_ids.append(i)
        if len(tile_ids) >= 2:
            out.append({"id": tid, "label": label, "description": description, "tile_ids": tile_ids})
    return out


# --- public API -----------------------------------------------------------------

def build_overview(macro_latest: dict, insider_events: list, calendar: dict, today: date = None) -> dict:
    """See the module docstring for the contract. Missing data is skipped."""
    today = today or date.today()
    tiles = (_macro_tiles(macro_latest)
             + _insider_tiles([e for e in insider_events or [] if (e or {}).get("category", "insider") == "insider"])
             + _calendar_tiles(calendar, today))
    return {
        "tiles": tiles,
        "templates": _templates(tiles),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_analysis_context(tiles_by_id: dict, selected_ids: list, calendar_events: list, today: date = None) -> str:
    """Compact plain-text user message for ai.analyse_selection()."""
    today = today or date.today()
    selected = [tiles_by_id[i] for i in selected_ids if i in tiles_by_id]
    full = len(selected) <= CONTEXT_FULL_FACTS_MAX
    out = [f"Today: {today.isoformat()} ({today.strftime('%A')}). Data from FRED, SEC EDGAR Form 4 filings and "
           "the FRED/FOMC release calendar, as currently loaded in the dashboard.", "",
           f"SELECTED ITEMS ({len(selected)}):"]
    for n, t in enumerate(selected, start=1):
        head = f"{n}. [{t['group']}] {t['title']}: {t['value']}"
        if t.get("change"):
            head += f" ({t['change']})"
        head += f" · level {t['level']}"
        if t.get("as_of"):
            head += f" · as of {t['as_of']}"
        out.append(head)
        if t.get("subtitle"):
            out.append(f"   {t['subtitle']}")
        for line in (t.get("facts") or "").splitlines():
            if line.strip() and (full or not line.startswith("About:")):
                out.append(f"   {line}")

    out += ["", f"UPCOMING KEY DATES (always included, next {CONTEXT_DAYS} days):"]
    dates = []
    for d, ev in _calendar_selection(calendar_events or [], today, CONTEXT_MAX_DATES):
        if (d - today).days <= CONTEXT_DAYS:
            dates.append((d, ev))
    if not dates:
        out.append("- none in the loaded calendar")
    for d, ev in dates[:CONTEXT_MAX_DATES]:
        line = f"- {d.isoformat()} ({d.strftime('%a')}, {_days_text((d - today).days)}): {ev.get('name') or '?'}"
        if ev.get("last_display"):
            line += f" — last {ev['last_display']}"
        if ev.get("why"):
            line += f" — {_first_sentence(ev['why'])}"
        out.append(line)

    out += ["", "Answer with the sections BIG PICTURE, HOW THE SIGNALS FIT TOGETHER, SCENARIOS (NEXT 3–12 MONTHS), "
                "WHAT TO WATCH, ASSET-CLASS TENDENCIES, UNCERTAINTIES & LIMITS — using only these data plus "
                "clearly marked historical background."]
    return "\n".join(out)
