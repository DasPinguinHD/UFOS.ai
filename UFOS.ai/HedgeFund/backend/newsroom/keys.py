"""Keys for the Financial Newsroom collectors (2026-10-02).

User decision: every HedgeFund key (FRED API key, SEC EDGAR user agent,
OpenRouter key) comes ONLY from the Windows Credential Manager via
``strategy_platform.secrets``, read at use time — a key entered in Hedge Fund
→ Settings → Keys applies without a restart. ``.env`` values for these names
are ignored (no fallback).

- ``wait_for_secret(name, report_status, ...)``: the collectors' gate. Returns
  the value; while it is missing (or the Credential Manager can't be read) it
  reports the user-facing message as the source status (ok=False), logs it
  once per change, and re-checks every ``recheck_seconds``.
- ``redact(text)``: removes key values and ``api_key=`` query parameters from
  error texts (aiohttp's HTTP errors include the request URL, and FRED takes
  its key as a query parameter) before they reach logs or the UI.
"""
import asyncio
import logging
import re

from strategy_platform import secrets
from strategy_platform.core.errors import UserFacingError

logger = logging.getLogger(__name__)

KEY_RECHECK_SECONDS = 60
_API_KEY_PARAM = re.compile(r"(api_key=)[^&\s'\")]+", re.IGNORECASE)


def redact(text, *values) -> str:
    """``text`` with the given secret values and any ``api_key=...`` parameter masked."""
    out = str(text or "")
    for v in values:
        if v and len(str(v)) >= 4:
            out = out.replace(str(v), "***")
    return _API_KEY_PARAM.sub(r"\1***", out)


def secret_error(name: str):
    """None when the secret is available, else the UserFacingError explaining why not."""
    try:
        secrets.require_secret(name)
        return None
    except UserFacingError as ex:
        return ex


async def wait_for_secret(name: str, report_status, *, recheck_seconds: float = KEY_RECHECK_SECONDS,
                          suffix: str = "", on_missing=None, sleep=asyncio.sleep) -> str:
    """The secret's value, waiting (and reporting) while it is missing.
    ``suffix`` is appended to the status message (e.g. what still works);
    ``on_missing(message)`` is called on every check that finds it missing."""
    last_logged = None
    while True:
        try:
            return secrets.require_secret(name)
        except UserFacingError as ex:
            message = ex.user_message + suffix
            if message != last_logged:
                logger.error("%s", message)
                last_logged = message
            if on_missing is not None:
                on_missing(message)
            await report_status(False, message)
        await sleep(recheck_seconds)
