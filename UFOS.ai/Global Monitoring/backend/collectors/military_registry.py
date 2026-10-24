"""Self-maintained heuristic for spotting government/military aircraft in the
OpenSky states/all feed. Neither OpenSky nor any other free API flags this for
us — it's a manually curated list of callsign prefixes, matched against the
(often padded/blank) callsign field. This needs periodic upkeep and is by
design a best-effort filter, not an authoritative registry; see
docs/global-monitoring-system-plan.md, "Übergreifende Grenzen".

2026-09-22: trimmed after the flights layer showed far too many markers. The
original list mixed genuinely distinctive special-mission callsigns (SAM,
DOOM, FORTE, ...) with several *generic national air force* prefixes (GAF,
FAF, HAF, PLF, KAF, CFC, IAM, SUI, CTM, RRR) that match literally any routine
transport/training flight of that country's air force — which is most of
what's actually airborne on a given day, not "notable" in any useful sense.
Those are removed; what's left is prefixes that specifically indicate a
strategic-airlift, VIP/diplomatic, or special-mission flight rather than
"any government aircraft of any kind." See also opensky_collector.py's
altitude filter, which is the other half of this pre-filtering.
"""

# Callsign prefixes for genuinely distinctive special-mission/VIP/strategic
# flights. Matched case-insensitively against the start of the (stripped)
# callsign. Kept deliberately short — broader prefixes turned "notable
# flights" into "most air traffic of a handful of countries."
# Prefix -> plain-English description of what that callsign generally means.
# This is what turns a flight marker's summary from a bare callsign + raw
# telemetry (which gives assess.py's AI nothing to actually say anything
# about — "input too sparse to derive substantive insight" was the observed
# result, 2026-09-24) into something with real operational context to
# reason over. Still a best-effort/self-maintained mapping, not an
# authoritative registry — see the module docstring.
MILITARY_CALLSIGN_INFO = {
    "RCH": "US Air Mobility Command strategic airlift (callsign family \"Reach\")",
    "SAM": "US Special Air Mission — VIP/diplomatic transport",
    "REACH": "US Air Mobility Command strategic airlift (alternate \"Reach\" callsign)",
    "FORTE": "US RC-135 Rivet Joint signals-intelligence reconnaissance aircraft",
    "DOOM": "US B-1B Lancer strategic bomber",
    "NATO": "NATO E-3A Sentry AWACS airborne early-warning aircraft",
    "ASCOT": "UK RAF strategic transport (Voyager/Atlas)",
    "GRZLY": "US special-missions aircraft",
    "VENUS": "VIP/government transport",
}

MILITARY_CALLSIGN_PREFIXES = tuple(MILITARY_CALLSIGN_INFO.keys())


def is_notable_military(callsign: str) -> bool:
    if not callsign:
        return False
    cs = callsign.strip().upper()
    return any(cs.startswith(prefix) for prefix in MILITARY_CALLSIGN_PREFIXES)


def describe_callsign(callsign: str):
    """Returns the plain-English description for a matching prefix, or None
    if the callsign doesn't match any known one (shouldn't normally happen
    for a callsign that already passed is_notable_military(), but a caller
    shouldn't assume that)."""
    if not callsign:
        return None
    cs = callsign.strip().upper()
    for prefix, description in MILITARY_CALLSIGN_INFO.items():
        if cs.startswith(prefix):
            return description
    return None
