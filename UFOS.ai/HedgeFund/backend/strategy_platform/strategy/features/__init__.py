"""External feature providers (SPEC §7.8).

An ``ExtFeature`` is a named, point-in-time input a strategy reads with
``{"ext": name}``. ``load(start, end)`` returns ``{date_iso: value}`` (scope
``global``) or ``{key: {date_iso: value}}`` (scope ``asset_class``; the key of
a symbol comes from ``key_for(symbol)``). A date without a value is simply
absent (NaN in a panel) — never a made-up number. ``load`` raises a
``UserFacingError`` (``DataSourceUnavailable``, ``SecretMissing``, …) when its
source fails; there is no stand-in.

Timing convention (all providers): the value for date ``d`` uses only data
known BEFORE ``d`` (first published on or before ``d - 1``) unless the provider
documents otherwise (``calendar.event_risk``: release schedules are published
in advance, so the flag for ``d`` is known before ``d``).

Registry: ``get(name)`` (KeyError for unknown names), ``names()``,
``catalogue()`` -> ``[{name, description, point_in_time, scope, source}]``.
Providers import nothing heavy at module level (main.py imports this package).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

SCOPES = ("global", "asset_class")


@dataclass(frozen=True)
class ExtFeature:
    name: str
    description: str
    load: Callable  # (start, end) -> {date: value} | {key: {date: value}}
    point_in_time: bool = True
    scope: str = "global"
    source: str = ""
    key_for: Optional[Callable[[str], str]] = None   # scope asset_class: symbol -> key of load()'s result
    keys: tuple = field(default_factory=tuple)        # scope asset_class: all keys load() can return

    def __post_init__(self):
        if self.scope not in SCOPES:
            raise ValueError(f"scope: one of {SCOPES}")
        if self.scope == "asset_class" and self.key_for is None:
            raise ValueError("an asset_class feature needs key_for(symbol)")

    def values_for(self, symbols, start, end) -> Dict[str, Dict[str, float]]:
        """{symbol: {date_iso: value}} for a list of symbols (one load call)."""
        data = self.load(start, end)
        if self.scope == "global":
            return {s: data for s in symbols}
        return {s: data.get(self.key_for(s), {}) for s in symbols}

    def info(self) -> dict:
        return {"name": self.name, "description": self.description, "point_in_time": self.point_in_time,
                "scope": self.scope, "source": self.source}


_REGISTRY: Dict[str, ExtFeature] = {}


def register(feature: ExtFeature, *, replace: bool = False) -> ExtFeature:
    if feature.name in _REGISTRY and not replace:
        raise ValueError(f"external feature {feature.name!r} is already registered")
    _REGISTRY[feature.name] = feature
    return feature


def unregister(name: str) -> None:
    """Tests only."""
    _REGISTRY.pop(name, None)


def get(name: str) -> ExtFeature:
    """The feature called ``name``; KeyError when there is none."""
    _ensure_builtin()
    return _REGISTRY[name]


def names() -> List[str]:
    _ensure_builtin()
    return sorted(_REGISTRY)


def catalogue() -> List[dict]:
    _ensure_builtin()
    return [_REGISTRY[n].info() for n in sorted(_REGISTRY)]


_builtin_loaded = False


def _ensure_builtin() -> None:
    global _builtin_loaded
    if _builtin_loaded:
        return
    _builtin_loaded = True
    from . import event_risk, macro_regime
    for feat in (macro_regime.FEATURE, event_risk.FEATURE):
        if feat.name not in _REGISTRY:
            _REGISTRY[feat.name] = feat
