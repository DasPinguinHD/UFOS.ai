"""Errors that must reach the user as a clear message.

Project rule "No unrequested fallbacks" (CLAUDE.md, 2026-10-02): when a data
source, key or budget is unavailable, the affected function STOPS and the user
is told what happened and what to do — no stand-in data, no cached/older value,
no alternative source. These exceptions carry that user-facing message.
"""


class UserFacingError(Exception):
    """Base: `user_message` is safe to show in the UI (no secrets, no stack)."""

    def __init__(self, user_message: str, *, detail: str = ""):
        super().__init__(user_message if not detail else f"{user_message} ({detail})")
        self.user_message = user_message
        self.detail = detail  # for backend.log only


class SecretMissing(UserFacingError):
    """A required key is not stored in the Windows Credential Manager."""

    def __init__(self, name: str, label: str):
        super().__init__(f"{label} is not set — enter it in Hedge Fund → Settings → Keys.")
        self.name = name
        self.label = label


class SecretStoreUnavailable(UserFacingError):
    """The Credential Manager could not be read (not Windows, API error)."""


class DataSourceUnavailable(UserFacingError):
    """The ONE configured source for a data type failed. No fallback."""

    def __init__(self, source: str, data_type: str, reason: str):
        super().__init__(
            f"{source} is unavailable for {data_type.replace('_', ' ')}: {reason}. "
            "This function is paused until the source works again — no other source is used.",
            detail=reason,
        )
        self.source = source
        self.data_type = data_type
        self.reason = reason


class InvalidRequest(UserFacingError):
    """The caller asked for something invalid (bad symbol, timeframe, range):
    a 400-style error — the data source itself did not fail, so its status is
    not marked as errored."""


class RequestTooLarge(InvalidRequest):
    """A data request exceeds the per-request limits (400-style: the caller
    must ask for less). Never retried, never truncated silently."""


class BudgetExceeded(UserFacingError):
    """An AI budget is used up. No cheaper model, no rule-based stand-in."""
