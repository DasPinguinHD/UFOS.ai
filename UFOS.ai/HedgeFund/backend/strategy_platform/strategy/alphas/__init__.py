"""Alpha library v1 (SPEC §7.9): one module per family; ``LIBRARY`` is the
list of entries ``{"key", "spec", "metadata": {key, family, reference,
expected_holding_days, notes}}`` (every spec valid with 0 errors).

``library()`` returns deep copies (callers may modify them), ``get(key)``
one entry, ``metadata(key)`` its metadata, ``keys()`` all keys in order.
Paper-trading research aids — not investment advice.
"""
from __future__ import annotations

import copy
from typing import Dict, List, Optional

from . import alpha101, composite, momentum, reversal, trend, volatility

LIBRARY: List[dict] = (momentum.ENTRIES + reversal.ENTRIES + trend.ENTRIES + volatility.ENTRIES
                       + composite.ENTRIES + alpha101.ENTRIES)
_BY_KEY: Dict[str, dict] = {e["key"]: e for e in LIBRARY}
if len(_BY_KEY) != len(LIBRARY):   # pragma: no cover - guarded by tests
    raise RuntimeError("duplicate alpha library keys")


def library() -> List[dict]:
    return copy.deepcopy(LIBRARY)


def keys() -> List[str]:
    return [e["key"] for e in LIBRARY]


def get(key: str) -> Optional[dict]:
    e = _BY_KEY.get(key)
    return copy.deepcopy(e) if e else None


def metadata(key: str) -> Optional[dict]:
    e = _BY_KEY.get(key)
    return dict(e["metadata"]) if e else None
