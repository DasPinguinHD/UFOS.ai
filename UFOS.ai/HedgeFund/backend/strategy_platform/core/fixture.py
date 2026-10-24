"""Sandbox fixture labels (SPEC "Sandbox profile" S4, user decision 2026-10-23).

The click-test fixture strategy and its synthetic evidence exist ONLY in a sandbox (``tools/sandbox.py
seed-incubation-test``). Its run cards carry ``metrics.fixture = true`` + ``metrics.fixture_note``; the API adds
``fixture: true`` + ``fixture_note`` to every payload built from them (run lists / details, validation / holdout
reports, strategy list / detail via the ``sandbox-fixture`` tag, incubation status, monitoring views), so a report
can never present the fixture as real validation. Payloads of anything else are unchanged (the keys are absent).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import json
from typing import Optional

TAG = "sandbox-fixture"
STRATEGY_NAME = "SANDBOX TEST — incubation fixture"
NOTE = "synthetic fixture for UI testing — no real validation"


def fields() -> dict:
    """The keys a fixture payload / metrics block carries."""
    return {"fixture": True, "fixture_note": NOTE}


def metrics_fields() -> dict:
    return fields()


def of_metrics(metrics) -> bool:
    return isinstance(metrics, dict) and metrics.get("fixture") is True


def card_fields(card: Optional[dict]) -> dict:
    """``fields()`` for a decoded run card whose metrics are a fixture, else {}."""
    return fields() if of_metrics((card or {}).get("metrics")) else {}


def spec_fields(spec: Optional[dict]) -> dict:
    """``fields()`` for a spec tagged ``sandbox-fixture``, else {}."""
    return fields() if TAG in ((spec or {}).get("tags") or []) else {}


def is_fixture_strategy(strategy_id: str) -> bool:
    """The strategy's current version is tagged ``sandbox-fixture`` (reads hedgefund.db)."""
    from ..storage import db
    row = db.strategy_get(strategy_id)
    if row is None:
        return False
    ver = db.strategy_version_get(strategy_id, int(row["current_version"]))
    return bool(spec_fields(json.loads(ver["spec_json"]) if ver else {}))


def strategy_fields(strategy_id: Optional[str]) -> dict:
    return fields() if strategy_id and is_fixture_strategy(strategy_id) else {}
