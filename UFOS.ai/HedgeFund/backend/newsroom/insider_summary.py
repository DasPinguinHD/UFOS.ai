"""Builds the compact, pre-ranked table the Insider Trades popup's
"Sum up most notable" button sends to OpenRouter (assess.summarize_insider_trades).

Deliberately does the objective work in code instead of leaving it to the
model: dollar values are computed, filings are sorted by size, and the
"unusual" signals are flagged explicitly —

- OPEN-MARKET-BUY: transaction code P. Insiders buying with their own money
  is far rarer (and more telling) than sales, grants or exercises.
- CROSS-COMPANY: the same reporting owner (by CIK) shows up as an insider
  of two or more DIFFERENT issuers within the loaded data — e.g. the CEO of
  company A also filing for a trade in company B. Only detectable within
  the window of filings currently in memory (the last ~50-100 Form 4s),
  not across EDGAR's full history.
- BIG-STAKE-CHANGE: a single filing changes the person's holdings by 50%+.
- 10%-OWNER: reporting owner holds 10%+ of the issuer (often a fund or
  another company rather than a person).
- PRE-PLANNED-10b5-1 (2026-09-25): the filing says the trade was made under
  a Rule 10b5-1 trading plan (checkbox or footnote) — scheduled months in
  advance, so it is context, not a signal. Such sales never get
  BIG-STAKE-CHANGE, i.e. a large planned sale is not presented as unusual.
- CLUSTER-BUY / CLUSTER-SELL (2026-09-25): see find_clusters() — several
  different insiders of the same company buying (>= 2) or selling (>= 3,
  unplanned only) on the open market within 14 days. Insider clusters are
  among the strongest signals in Form 4 data.

The table is compact on purpose — one line per filing, capped — so the
request stays small and cheap regardless of how many filings are loaded.
"""

from datetime import date, timedelta

MAX_ROWS = 60
_ROUTINE_CODES = {"A", "F", "M", "X", "G", "C"}

CLUSTER_WINDOW_DAYS = 14
_CLUSTER_MIN_INSIDERS = {"buy": 2, "sell": 3}


def _parse_date(text):
    """Form 4 dates are "YYYY-MM-DD" (occasionally with a UTC offset
    appended, e.g. "2026-09-10-05:00"); the feed's filed_at is ISO 8601."""
    try:
        return date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def _issuer_key(d: dict):
    return d.get("issuer_cik") or (d.get("issuer_ticker") or "").upper() or d.get("issuer_name") or None


def _owner_key(d: dict):
    return d.get("owner_cik") or (d.get("owner_name") or "").strip().upper() or None


def find_clusters(insider_events: list, window_days: int = CLUSTER_WINDOW_DAYS) -> list:
    """Issuers where several DIFFERENT reporting owners traded on the open
    market in the same direction within `window_days` (transaction dates):

    - kind "buy":  code P, >= 2 insiders
    - kind "sell": code S, >= 3 insiders, filings under a Rule 10b5-1 plan
      excluded (pre-scheduled sales don't express a current view)

    Returns [{"issuer_name", "issuer_ticker", "kind", "insiders": [names],
    "count", "total_usd", "first_date", "last_date"}], buys first, then by
    number of insiders and dollar total. Only covers the filings currently
    in memory (the last ~40-100 Form 4s), not EDGAR's full history.
    """
    # (issuer, kind) -> {"meta": (name, ticker), "tx": {dedupe_key: (date, owner_key, owner_name, value)}}
    groups: dict = {}
    for e in insider_events:
        d = e.get("details")
        if not d:
            continue
        issuer, owner = _issuer_key(d), _owner_key(d)
        if not issuer or not owner:
            continue
        fallback_date = _parse_date(e.get("filed_at"))
        for t in d.get("transactions") or []:
            if t.get("derivative"):
                continue
            code = t.get("code")
            if code == "P":
                kind = "buy"
            elif code == "S" and not d.get("plan_10b5_1"):
                kind = "sell"
            else:
                continue
            when = _parse_date(t.get("date")) or fallback_date
            if when is None:
                continue
            g = groups.setdefault((issuer, kind), {"meta": (d.get("issuer_name") or "", d.get("issuer_ticker") or ""), "tx": {}})
            # An amendment (4/A) repeats the original transactions — count each once.
            dedupe = (owner, when, t.get("shares"), t.get("price"))
            g["tx"][dedupe] = (when, owner, d.get("owner_name") or owner, t.get("value_usd") or 0)

    clusters = []
    for (issuer, kind), g in groups.items():
        txs = sorted(g["tx"].values(), key=lambda x: x[0])
        best = None
        for i, (start, *_rest) in enumerate(txs):
            window = [x for x in txs[i:] if x[0] <= start + timedelta(days=window_days)]
            owners = {x[1] for x in window}
            total = sum(x[3] for x in window)
            if best is None or (len(owners), total) > (best[0], best[1]):
                best = (len(owners), total, window)
        if best is None or best[0] < _CLUSTER_MIN_INSIDERS[kind]:
            continue
        count, total, window = best
        names = []
        for x in window:
            if x[2] not in names:
                names.append(x[2])
        name, ticker = g["meta"]
        clusters.append({
            "issuer_name": name,
            "issuer_ticker": ticker,
            "kind": kind,
            "insiders": names,
            # issuer_key / owner_keys: CIKs (or name fallbacks) so callers can
            # match filings to a cluster; the UI only needs the fields above.
            "issuer_key": issuer,
            "owner_keys": sorted({x[1] for x in window}),
            "count": count,
            "total_usd": round(total, 2),
            "first_date": window[0][0].isoformat(),
            "last_date": window[-1][0].isoformat(),
        })
    clusters.sort(key=lambda c: (c["kind"] != "buy", -c["count"], -c["total_usd"]))
    return clusters


def _money(v):
    if v is None:
        return "n/a"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def build_table(insider_events: list) -> tuple[str, int]:
    """Returns (table_text, number_of_filings_used)."""
    filings = [e for e in insider_events if e.get("details")]
    if not filings:
        return "", 0

    # owner CIK -> set of issuer names they filed for (cross-company signal)
    issuers_by_owner: dict = {}
    for e in filings:
        d = e["details"]
        if d.get("owner_cik"):
            issuers_by_owner.setdefault(d["owner_cik"], set()).add(d.get("issuer_name") or d.get("issuer_cik"))

    clusters = find_clusters(filings)
    # (issuer key, owner key) -> ["CLUSTER-BUY (3 insiders)", ...]
    cluster_flags: dict = {}
    for c in clusters:
        label = f"CLUSTER-{c['kind'].upper()} ({c['count']} insiders within {CLUSTER_WINDOW_DAYS} days)"
        code = "P" if c["kind"] == "buy" else "S"
        for e in filings:
            d = e["details"]
            if (_issuer_key(d) == c["issuer_key"] and _owner_key(d) in c["owner_keys"]
                    and not (code == "S" and d.get("plan_10b5_1"))
                    and any(t.get("code") == code for t in d.get("transactions") or [])):
                labels = cluster_flags.setdefault((_issuer_key(d), _owner_key(d)), [])
                if label not in labels:
                    labels.append(label)

    rows = []
    for e in filings:
        d = e["details"]
        txs = d.get("transactions") or []
        total_value = sum(t["value_usd"] or 0 for t in txs)
        codes = sorted({t["code"] for t in txs if t.get("code")})
        planned = bool(d.get("plan_10b5_1"))

        flags = []
        if planned:
            flags.append("PRE-PLANNED-10b5-1")
        if "P" in codes:
            flags.append("OPEN-MARKET-BUY")
        flags.extend(cluster_flags.get((_issuer_key(d), _owner_key(d)), []))
        other_issuers = issuers_by_owner.get(d.get("owner_cik"), set()) - {d.get("issuer_name") or d.get("issuer_cik")}
        if other_issuers:
            flags.append("CROSS-COMPANY (also insider at: " + ", ".join(sorted(str(x) for x in other_issuers)[:3]) + ")")
        if "10% owner" in (d.get("owner_roles") or []):
            flags.append("10%-OWNER")
        for t in txs:
            shares, after = t.get("shares"), t.get("shares_after")
            # A big pre-planned sale is still just a scheduled sale — not unusual.
            if planned and t.get("acquired_disposed") != "A":
                continue
            if shares and after is not None:
                before = after - shares if t.get("acquired_disposed") == "A" else after + shares
                if before > 0 and shares / before >= 0.5:
                    flags.append("BIG-STAKE-CHANGE")
                    break
        if codes and set(codes) <= _ROUTINE_CODES and not [f for f in flags if f != "PRE-PLANNED-10b5-1"]:
            flags.append("routine-compensation")

        def _direction(t):
            if t.get("code") == "P":
                return "BOUGHT on open market"
            if t.get("code") == "S":
                return "SOLD on open market" + (" (pre-planned, Rule 10b5-1 plan)" if planned else "")
            verb = "received" if t.get("acquired_disposed") == "A" else "gave up"
            return f"{verb} via {t['type']}"

        tx_text = "; ".join(
            f"{t.get('date') or '?'}: {_direction(t)} {t['shares']:,.0f} "
            f"x {t.get('security') or 'shares'} of {d.get('issuer_name') or d.get('issuer_ticker') or 'the issuer'}"
            + (f" at ${t['price']:,.2f}" if t.get("price") else "")
            + (f" = {_money(t['value_usd'])}" if t.get("value_usd") else "")
            + (f", owns {t['shares_after']:,.0f} afterwards" if t.get("shares_after") is not None else "")
            for t in txs if t.get("shares") is not None
        ) or "no transactions listed"

        ticker = f" ({d['issuer_ticker']})" if d.get("issuer_ticker") else ""
        rows.append((total_value, (
            f"{d.get('issuer_name') or '?'}{ticker} | {d.get('owner_name') or '?'} "
            f"[{', '.join(d.get('owner_roles') or []) or 'insider'}] | total {_money(total_value)} | "
            f"{tx_text} | flags: {', '.join(flags) or '-'}"
        )))

    rows.sort(key=lambda r: r[0], reverse=True)
    lines = [f"{i + 1}. {text}" for i, (_, text) in enumerate(rows[:MAX_ROWS])]
    header = (
        f"{len(filings)} recent Form 4 filings (showing {len(lines)}, sorted by total dollar value). "
        "Each line: company (ticker) | person [role at that company] | total value | transactions | flags.\n"
    )
    if clusters:
        header += "Insider clusters detected in code (several different insiders of one company, same direction, open market):\n"
        header += "\n".join(
            f"- CLUSTER-{c['kind'].upper()}: {c['count']} insiders {'bought' if c['kind'] == 'buy' else 'sold'} "
            f"{c['issuer_name'] or '?'}{' (' + c['issuer_ticker'] + ')' if c['issuer_ticker'] else ''} "
            f"between {c['first_date']} and {c['last_date']}, together {_money(c['total_usd'])}: {', '.join(c['insiders'])}"
            for c in clusters
        ) + "\n\nFilings:\n"
    return header + "\n".join(lines), len(lines)
