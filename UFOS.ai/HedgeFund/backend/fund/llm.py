"""OpenRouter JSON calls + the daily AI budget for the HedgeFund loop
(fund/SPEC.md §0, §7 sentiment, §8 judge).

Proof of concept, paper trading only — model output is never investment advice.

- ``chat_json(messages, model, max_tokens, feature, feature_label,
  temperature=0.2) -> (parsed_json, raw_text, used_model, usage)``: one chat
  completion via the ``openai`` AsyncOpenAI client against OpenRouter, the
  same call pattern as ``newsroom/ai.py`` (its ``_current_settings()``,
  ``usage_info()`` and ``_record_cost()`` are reused, so the key is re-read
  from .env on every call and each completion lands in the shared cost ledger
  with module "HedgeFund"). The answer is parsed as JSON (code fences are
  stripped, the first JSON array/object is extracted). Failures raise
  ``LlmUnavailable`` with ``reason`` in ``no_key|error|bad_json`` and — when
  the call itself went through — ``usage``/``model`` so the caller can still
  book the cost against the budget. ``attempted=True`` marks a request that
  was sent but failed without usage (timeout / network error): OpenRouter may
  still bill it, so callers charge the pre-call estimate (``charge_failed_call``).
  The AsyncOpenAI client is closed after every call.
- ``TRANSPORT``: module-level hook. ``None`` = the real OpenRouter call; tests
  set an ``async fn(messages, model, max_tokens, temperature) -> (raw_text,
  used_model, usage)`` so no network is touched. (Judge and sentiment also
  take an ``llm=`` argument, so tests can replace ``chat_json`` entirely.)
- ``Budget``: the fund's daily AI budget (``ai_daily_budget_usd``), wired by
  the engine to ``store.ai_spend_today`` / ``store.add_ai_spend``.
- ``provenance(feature, model)`` / ``models()``: newsroom.ai's versions when
  importable (production), otherwise an identical local fallback (offline
  tests without the ``openai`` package).

``openai`` / ``newsroom.ai`` are imported lazily inside functions only.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

FEATURE_LABELS = {
    "fund-judge": "Hedge Fund · Judge",
    "fund-sentiment": "Hedge Fund · Sentiment",
}
REQUEST_TIMEOUT_SECONDS = 90

# Rough price assumptions (USD per 1M tokens) used ONLY to estimate whether a
# call still fits the daily budget before it is made; the actual cost booked
# afterwards is OpenRouter's usage.cost. Deliberately on the high side.
EST_PRICE_PER_MTOK = {
    "standard": {"in": 1.0, "out": 4.0},
    "premium": {"in": 15.0, "out": 75.0},
}

# Test hook — see module docstring.
TRANSPORT: Optional[Callable] = None


class LlmUnavailable(RuntimeError):
    """The LLM could not deliver a usable answer.

    reason: "no_key" (OPENROUTER_API_KEY missing), "error" (request failed),
    "bad_json" (answer was not valid JSON), "budget" (not called: budget).
    usage/model are set when the request itself completed (cost was incurred)."""

    def __init__(self, reason: str, message: str, usage: Optional[dict] = None, model: Optional[str] = None,
                 raw_text: Optional[str] = None, attempted: bool = False):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.usage = usage
        self.model = model
        self.raw_text = raw_text
        # True: the request was sent but failed without a usage report (timeout,
        # connection reset, 5xx) — the provider may still bill it.
        self.attempted = bool(attempted) or usage is not None


# --- newsroom.ai reuse (lazy) --------------------------------------------------

def _newsroom_ai():
    """newsroom.ai or None (it imports openai/dotenv at module level, which the
    offline tests don't have)."""
    try:
        from newsroom import ai as newsroom_ai  # noqa: WPS433 (lazy on purpose)
        return newsroom_ai
    except Exception:
        return None


def provenance(feature: str, model: str) -> dict:
    """Machine-readable AI marking (EU AI Act Art. 50) — newsroom.ai.provenance()."""
    ai = _newsroom_ai()
    if ai is not None:
        return ai.provenance(feature, model)
    return {
        "ai_generated": True,
        "generator": f"UFOS.ai/HedgeFund/{feature}",
        "provider": "OpenRouter",
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "human_reviewed": False,
        "label": "AI-generated content (EU AI Act Art. 50)",
    }


def models() -> Dict[str, str]:
    """{"standard": OPENROUTER_MODEL, "premium": OPENROUTER_ANALYSIS_PREMIUM_MODEL},
    read fresh like newsroom.ai.analysis_models()."""
    ai = _newsroom_ai()
    if ai is not None:
        try:
            return dict(ai.analysis_models())
        except Exception:
            pass
    return {"standard": os.environ.get("OPENROUTER_MODEL") or "deepseek/deepseek-chat-v3-0324",
            "premium": os.environ.get("OPENROUTER_ANALYSIS_PREMIUM_MODEL") or "anthropic/claude-opus-5.5"}


def _record_cost(feature: str, model: str, usage: dict) -> None:
    """Cost ledger entry (never raises)."""
    label = FEATURE_LABELS.get(feature, "Hedge Fund")
    ai = _newsroom_ai()
    try:
        if ai is not None:
            ai._record_cost(feature, label, model, usage)
        else:
            from newsroom import cost_ledger
            cost_ledger.record(feature, label, model, usage)
    except Exception:
        pass


# --- JSON extraction -------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Parses a model answer as JSON: the whole text, else the content of a
    ```json fence, else the first balanced JSON array/object in the text.
    Raises ValueError when nothing parses."""
    if text is None:
        raise ValueError("empty answer")
    s = str(text).strip()
    if not s:
        raise ValueError("empty answer")
    candidates = [s]
    candidates += [m.strip() for m in _FENCE_RE.findall(s)]
    for c in candidates:
        try:
            return json.loads(c)
        except ValueError:
            pass
    decoder = json.JSONDecoder()
    for c in candidates:
        for i, ch in enumerate(c):
            if ch in "[{":
                try:
                    value, _end = decoder.raw_decode(c[i:])
                    return value
                except ValueError:
                    continue
    raise ValueError("no JSON found in the answer")


# --- the call ---------------------------------------------------------------------

def _short_error(ex: Exception) -> str:
    ai = _newsroom_ai()
    if ai is not None:
        try:
            return ai._short_error(ex)
        except Exception:
            pass
    return f"AI request failed: {str(ex)[:160]}"


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


def safe_base_url(value) -> str:
    """The https-checked OpenRouter base URL: config.safe_openrouter_base_url()
    (the same check newsroom.ai._current_settings() applies) when config is
    importable, else an identical stdlib check. The API key is never sent
    over plain http."""
    try:
        from config import safe_openrouter_base_url  # lazy (config imports dotenv)
        return safe_openrouter_base_url(value)
    except ImportError:
        pass
    from urllib.parse import urlsplit
    text = str(value or "").strip()
    if not text:
        return DEFAULT_BASE_URL
    try:
        parts = urlsplit(text)
        ok = parts.scheme.lower() == "https" and bool(parts.hostname)
    except ValueError:
        ok = False
    if not ok:
        logger.warning("OPENROUTER_BASE_URL is not an https URL — ignored, using %s", DEFAULT_BASE_URL)
        return DEFAULT_BASE_URL
    return text


async def _close_client(client) -> None:
    """Closes the AsyncOpenAI client (its httpx connection pool); never raises."""
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception:
        logger.debug("closing the OpenRouter client failed", exc_info=True)


async def _openrouter(messages: List[dict], model: str, max_tokens: int, temperature: float,
                      feature: str) -> Tuple[str, str, dict]:
    ai = _newsroom_ai()
    if ai is None:
        raise LlmUnavailable("error", "AI client not available (openai package missing)")
    api_key, base_url, _standard = ai._current_settings()
    if not api_key:
        raise LlmUnavailable("no_key", "OPENROUTER_API_KEY is not set")
    # newsroom.ai already returns the checked URL; checked again so the fund
    # never depends on that (idempotent for a valid https URL)
    base_url = safe_base_url(base_url)
    from openai import AsyncOpenAI  # lazy
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    try:
        response = await client.chat.completions.create(model=model, messages=messages, max_tokens=int(max_tokens),
                                                        temperature=float(temperature),
                                                        timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception as ex:
        logger.warning("OpenRouter call for %s failed: %s", feature, ex)
        raise LlmUnavailable("error", _short_error(ex), attempted=True) from ex
    finally:
        await _close_client(client)
    raw = (response.choices[0].message.content or "") if response.choices else ""
    used_model = getattr(response, "model", None) or model
    usage = ai.usage_info(response)
    _record_cost(feature, used_model, usage)
    return raw, used_model, usage


async def chat_json(messages: List[dict], model: str, max_tokens: int, feature: str,
                    feature_label: Optional[str] = None, temperature: float = 0.2):
    """-> (parsed_json, raw_text, used_model, usage). Raises LlmUnavailable.
    ``feature_label`` is accepted for symmetry with the ledger; the label is
    taken from FEATURE_LABELS for known features."""
    if feature_label and feature not in FEATURE_LABELS:
        FEATURE_LABELS[feature] = feature_label
    if TRANSPORT is not None:
        try:
            raw, used_model, usage = await TRANSPORT(messages, model, max_tokens, temperature)
        except LlmUnavailable:
            raise
        except Exception as ex:
            raise LlmUnavailable("error", f"AI request failed: {ex}", attempted=True) from ex
    else:
        raw, used_model, usage = await _openrouter(messages, model, max_tokens, temperature, feature)
    usage = dict(usage or {})
    try:
        parsed = extract_json(raw)
    except ValueError as ex:
        raise LlmUnavailable("bad_json", f"AI answer was not valid JSON ({ex})", usage=usage,
                             model=used_model, raw_text=raw)
    return parsed, raw, used_model or model, usage


# --- cost estimate + budget -----------------------------------------------------------

def estimate_cost(messages: List[dict], max_tokens: int, tier: str = "standard") -> float:
    """Conservative USD estimate of one call (chars/4 ≈ tokens; output assumed
    to use max_tokens). Only for the pre-call budget check."""
    chars = sum(len(str(m.get("content") or "")) for m in messages or [])
    price = EST_PRICE_PER_MTOK.get(tier, EST_PRICE_PER_MTOK["standard"])
    return (chars / 4.0) / 1e6 * price["in"] + float(max_tokens) / 1e6 * price["out"]


def usage_cost(usage: Optional[dict], fallback: float = 0.0) -> float:
    """usage["cost_usd"] as float; ``fallback`` when the provider did not report it."""
    try:
        v = (usage or {}).get("cost_usd")
        return float(v) if v is not None else float(fallback)
    except (TypeError, ValueError):
        return float(fallback)


class Budget:
    """Daily AI budget shared by sentiment + judge.

    ``spent_fn()`` returns today's spend (UTC day) and ``add_fn(usd)`` books a
    call — the engine passes ``store.ai_spend_today`` / ``store.add_ai_spend``.
    Without them the budget tracks spend in memory (tests, backtests).
    ``run_spent`` is what this Budget object booked (the run's AI cost).

    Fail closed: the spend is ``max(store value, what this object booked)``;
    when the store read fails (``spent_fn`` raises or returns None) the budget
    counts as exhausted — no LLM call without a readable spend (logged once
    per Budget object)."""

    def __init__(self, limit_usd: float, spent_fn: Optional[Callable[[], float]] = None,
                 add_fn: Optional[Callable[[float], Any]] = None):
        try:
            self.limit_usd = max(0.0, float(limit_usd or 0.0))
        except (TypeError, ValueError):
            self.limit_usd = 0.0
        self._spent_fn = spent_fn
        self._add_fn = add_fn
        self._local = 0.0
        self.run_spent = 0.0
        self.unreadable = False         # the last store read failed (fail closed)
        self._logged_unreadable = False

    def _store_spent(self) -> Optional[float]:
        """Today's spend from the store; None when it cannot be read."""
        try:
            v = self._spent_fn()
            v = None if v is None else float(v)
        except Exception:
            v = None
        if v is not None and v != v:        # NaN
            v = None
        self.unreadable = v is None
        if v is None and not self._logged_unreadable:
            self._logged_unreadable = True
            logger.error("AI budget: today's spend is unreadable — treating the budget as exhausted (fail closed)")
        return v

    def spent(self) -> float:
        if self._spent_fn is None:
            return self._local
        v = self._store_spent()
        return self._local if v is None else max(v, self._local)

    def remaining(self) -> float:
        if self._spent_fn is not None and self._store_spent() is None:
            return 0.0
        return max(0.0, self.limit_usd - self.spent())

    def can_spend(self, estimate: float = 0.0) -> bool:
        if self.limit_usd <= 0:
            return False
        if self._spent_fn is not None and self._store_spent() is None:
            return False
        try:
            est = max(0.0, float(estimate or 0.0))
        except (TypeError, ValueError, OverflowError):
            return False
        return self.spent() + est <= self.limit_usd

    def add(self, usd) -> float:
        try:
            usd = max(0.0, float(usd or 0.0))
        except (TypeError, ValueError):
            usd = 0.0
        self._local += usd
        self.run_spent += usd
        if self._add_fn is not None:
            try:
                self._add_fn(usd)
            except Exception:
                logger.warning("AI budget: could not book %.6f USD", usd)
        return self.spent()

    @property
    def exhausted(self) -> bool:
        if self.limit_usd <= 0:
            return True
        if self._spent_fn is not None and self._store_spent() is None:
            return True
        return self.spent() >= self.limit_usd

    def to_dict(self) -> dict:
        exhausted = self.exhausted
        return {"limit_usd": self.limit_usd, "spent_today_usd": round(self.spent(), 6),
                "exhausted": exhausted, "spend_unreadable": bool(self._spent_fn is not None and self.unreadable)}


def charge_failed_call(budget, ex: "LlmUnavailable", estimate: float) -> float:
    """Books a failed call against the budget: its reported cost when the
    call completed (usage present), else the pre-call ``estimate`` when the
    request was sent (timeout / network error — the provider may bill it).
    Nothing for calls that never left (no key, client missing). -> USD booked."""
    if budget is None or ex is None:
        return 0.0
    if getattr(ex, "usage", None) is not None:
        usd = usage_cost(ex.usage, estimate)
    elif getattr(ex, "attempted", False):
        usd = max(0.0, float(estimate or 0.0))
    else:
        return 0.0
    budget.add(usd)
    return usd


def split_usage(usage: Optional[dict], n: int, fallback_cost: Optional[float] = None) -> dict:
    """One symbol's share of a batch call's usage (for display; the run total
    is the sum of the shares). Adds batch_size and batch_cost_usd."""
    usage = usage or {}
    n = max(1, int(n))
    cost = usage.get("cost_usd")
    try:
        cost = float(cost) if cost is not None else (float(fallback_cost) if fallback_cost is not None else None)
    except (TypeError, ValueError):
        cost = None

    def _share(v):
        try:
            return int(round(float(v) / n)) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {"cost_usd": (cost / n) if cost is not None else None,
            "prompt_tokens": _share(usage.get("prompt_tokens")),
            "completion_tokens": _share(usage.get("completion_tokens")),
            "batch_size": n, "batch_cost_usd": cost,
            "cost_estimated": usage.get("cost_usd") is None and cost is not None}


def merge_usage(a: Optional[dict], b: Optional[dict]) -> Optional[dict]:
    """Sum of two per-symbol usage shares (premium_on_flip: standard + premium)."""
    if not a:
        return dict(b) if b else None
    if not b:
        return dict(a)
    out = dict(a)
    for k in ("cost_usd", "prompt_tokens", "completion_tokens"):
        va, vb = a.get(k), b.get(k)
        out[k] = None if va is None and vb is None else (va or 0) + (vb or 0)
    out["calls"] = int(a.get("calls", 1)) + int(b.get("calls", 1))
    out["batch_cost_usd"] = None
    return out


def sanitize_untrusted(text, limit: int = 240) -> str:
    """Untrusted text (headlines, filer names) for a prompt data block: no
    control characters, no block delimiters, single line, truncated."""
    if text is None:
        return ""
    s = "".join(ch if ch.isprintable() else " " for ch in str(text))
    s = s.replace("<<<", "‹‹‹").replace(">>>", "›››").replace("```", "'''")
    s = " ".join(s.split())
    return s[:limit]
