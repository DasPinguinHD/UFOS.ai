"""Non-secret backend settings in ``backend/data/settings.json`` (stage 10, S10-E — they lived in .env before).

The file is optional: a missing file or key means the code default. It is strict otherwise — not JSON, not an
object, an unknown key, a wrong type, a value out of range, or a key that looks like a secret → ``SettingsError``
(config.py turns that into a refused start with the message; no silent fallback). Secrets never go here: they live
in the Windows Credential Manager (⚙ Keys & data).

Schema: ``{key: (kind, default, *args)}`` with kind
- ``"int"`` (default, lo, hi) — a whole number (not a bool), lo <= v <= hi;
- ``"float"`` (default, lo, hi) — a finite number, lo <= v <= hi;
- ``"str"`` (default, max_len) — a non-empty single-line string;
- ``"openrouter_url"`` (default) — https on openrouter.ai, default port, no user info / query / fragment;
- ``"youtube_url"`` (default) — "" (not set) or https on youtube.com / www. / m. / youtu.be, no user info.

The same file is used by every backend (HedgeFund, Global Monitoring, LiveStreamAgent; a test keeps the copies
byte-identical). Stdlib only. Part of UFOS.ai, a proof-of-concept portfolio project.
"""
import json
import math
import os
import re
import sys
from urllib.parse import urlsplit

FILE_NAME = "settings.json"
SECRET_LIKE = re.compile(r"key|secret|token|passw|credential", re.IGNORECASE)
OPENROUTER_HOST = "openrouter.ai"
YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
MAX_URL = 500


class SettingsError(Exception):
    """A user-facing reason: "data/settings.json: <key>: <reason>"."""


def openrouter_url_problem(value):
    """None when ``value`` is an acceptable OpenRouter base URL, else the reason."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_URL:
        return "an https://openrouter.ai/... URL"
    try:
        p = urlsplit(value.strip())
        port = p.port
    except ValueError:
        return "not a valid URL"
    if (p.scheme.lower() != "https" or (p.hostname or "").lower() != OPENROUTER_HOST or p.username is not None
            or p.password is not None or port not in (None, 443) or p.query or p.fragment):
        return "must be https on openrouter.ai (default port, no user info, query or fragment)"
    return None


def youtube_url_problem(value):
    """None when ``value`` is "" (not set) or an acceptable YouTube URL, else the reason."""
    if value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_URL or any(c.isspace() for c in value):
        return "a https://www.youtube.com/... URL"
    try:
        p = urlsplit(value)
        port = p.port
    except ValueError:
        return "not a valid URL"
    if (p.scheme.lower() != "https" or (p.hostname or "").lower() not in YOUTUBE_HOSTS or p.username is not None
            or p.password is not None or port not in (None, 443)):
        return "must be https on youtube.com or youtu.be (no user info)"
    return None


def _check(key, value, spec):
    kind = spec[0]
    if kind in ("int", "float"):
        lo, hi = spec[2], spec[3]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{key}: a number")
        if kind == "int" and not isinstance(value, int):
            raise SettingsError(f"{key}: a whole number")
        if isinstance(value, float) and not math.isfinite(value):
            raise SettingsError(f"{key}: a finite number")
        if not lo <= value <= hi:
            raise SettingsError(f"{key}: {lo}..{hi}")
        return int(value) if kind == "int" else float(value)
    if kind == "str":
        if not isinstance(value, str) or not value.strip() or len(value) > spec[2] or any(ord(c) < 32 for c in value):
            raise SettingsError(f"{key}: a non-empty single-line text of at most {spec[2]} characters")
        return value.strip()
    if kind == "openrouter_url":
        why = openrouter_url_problem(value)
        if why:
            raise SettingsError(f"{key}: {why}")
        return value.strip()
    if kind == "youtube_url":
        why = youtube_url_problem(value)
        if why:
            raise SettingsError(f"{key}: {why}")
        return value
    raise SettingsError(f"{key}: unknown schema kind {kind!r}")


def defaults(schema) -> dict:
    return {k: spec[1] for k, spec in schema.items()}


def load(path, schema) -> dict:
    """The settings (defaults overlaid by the file); SettingsError with the reason."""
    values = defaults(schema)
    if not os.path.exists(path):
        return values
    try:
        with open(path, encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as ex:
        raise SettingsError(f"data/{FILE_NAME}: not readable JSON ({type(ex).__name__})") from None
    if not isinstance(raw, dict):
        raise SettingsError(f"data/{FILE_NAME}: must be a JSON object")
    for key, value in raw.items():
        if not isinstance(key, str) or SECRET_LIKE.search(key):
            raise SettingsError(f"data/{FILE_NAME}: {key}: secrets never go into this file — keys live in the "
                                "Windows Credential Manager (⚙ Keys & data)")
        if key not in schema:
            raise SettingsError(f"data/{FILE_NAME}: {key}: unknown setting")
        try:
            values[key] = _check(key, value, schema[key])
        except SettingsError as ex:
            raise SettingsError(f"data/{FILE_NAME}: {ex}") from None
    return values


def load_or_exit(path, schema) -> dict:
    """``load`` for config.py at import: a SettingsError stops the process with the reason (exit code 2)."""
    try:
        return load(path, schema)
    except SettingsError as ex:
        print(f"REFUSED to start: {ex}", file=sys.stderr)
        raise SystemExit(2)
