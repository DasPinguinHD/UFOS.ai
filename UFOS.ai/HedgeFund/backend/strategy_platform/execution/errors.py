"""Errors of the stage-7 trading layer (SPEC §17.10). Part of UFOS.ai; paper trading only."""
from __future__ import annotations

from ..core.errors import UserFacingError


class TradingError(UserFacingError):
    """A refused trading request: HTTP ``status`` with ``{error, code, **extra}``."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.extra = extra

    def payload(self) -> dict:
        out = {"error": self.user_message, "code": self.code}
        out.update(self.extra)
        return out


class BrokerUnavailable(UserFacingError):
    """A transient broker/adapter failure (retry policy, SPEC §17.7)."""
