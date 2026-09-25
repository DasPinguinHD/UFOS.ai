"""AI helpers for the Financial Newsroom (OpenRouter via the `openai` package).

Moved with the insider-trades feature from Global Monitoring's assess.py on
2026-09-24 — same prompt, same OpenRouter call pattern, same labelling. Every
response that carries model output also carries provenance() — the
machine-readable AI marking required by the project's "AI content labelling"
convention (EU AI Act Art. 50); generator ids here are "UFOS.ai/HedgeFund/...".

_current_settings() re-reads .env on every call, so an API key added while
the backend is running takes effect on the next click without a restart.
"""
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import AsyncOpenAI

from config import OPENROUTER_API_KEY as _DEFAULT_API_KEY
from config import OPENROUTER_BASE_URL as _DEFAULT_BASE_URL
from config import OPENROUTER_MODEL as _DEFAULT_MODEL
from config import OPENROUTER_ANALYSIS_PREMIUM_MODEL as _DEFAULT_PREMIUM_MODEL

try:  # shared AI cost ledger (2026-09-25); ai.py is also loaded stand-alone by tests
    from newsroom import cost_ledger as _cost_ledger
except Exception:  # pragma: no cover
    _cost_ledger = None


def _record_cost(feature: str, feature_label: str, model: str, usage: dict) -> None:
    """Whoever makes the OpenRouter call records it in the shared ledger — never raises."""
    if _cost_ledger is None:
        return
    try:
        _cost_ledger.record(feature, feature_label, model, usage)
    except Exception:
        pass


def provenance(feature: str, model: str) -> dict:
    """Machine-readable marking for AI-generated text (EU AI Act Art. 50(2)).

    Attached as "provenance" to every HTTP response that carries LLM output, so
    the frontends can show "✦ AI-GENERATED · <model> · <time>" and anything that
    stores/forwards the text can still tell it was machine-generated. Same shape
    in every UFOS.ai backend — see the "AI content labelling" section in
    CLAUDE.md before changing it.
    """
    return {
        "ai_generated": True,
        "generator": f"UFOS.ai/HedgeFund/{feature}",
        "provider": "OpenRouter",
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "human_reviewed": False,
        "label": "AI-generated content (EU AI Act Art. 50)",
    }


def _current_settings() -> tuple[str, str, str]:
    load_dotenv(override=True)
    return (
        os.getenv("OPENROUTER_API_KEY", _DEFAULT_API_KEY),
        os.getenv("OPENROUTER_BASE_URL", _DEFAULT_BASE_URL),
        os.getenv("OPENROUTER_MODEL", _DEFAULT_MODEL),
    )


def is_configured() -> bool:
    api_key, _, _ = _current_settings()
    return bool(api_key)


def _short_error(ex: Exception) -> str:
    """OpenRouter/provider errors arrive as a huge nested JSON blob; the
    sidebar only needs one readable line. The full error still goes to
    backend.log via main.py's logger.warning()."""
    text = str(ex)
    status = getattr(ex, "status_code", None)
    lowered = text.lower()
    if "context length" in lowered or "maximum context" in lowered:
        reason = "request exceeded the model's context limit"
    elif status == 401 or "invalid api key" in lowered or "unauthorized" in lowered:
        reason = "OpenRouter rejected the API key"
    elif status == 402 or "credits" in lowered:
        reason = "OpenRouter account has insufficient credits"
    elif status == 429 or "rate limit" in lowered:
        reason = "OpenRouter is rate-limiting requests, try again shortly"
    elif "timed out" in lowered or "timeout" in lowered:
        reason = "the model did not answer in time"
    else:
        reason = text[:160] + ("…" if len(text) > 160 else "")
    return f"AI request failed ({status or 'error'}): {reason}. Details in backend.log."



# --- Insider Trades: "✦ Sum up most notable" ---

INSIDER_SYSTEM_PROMPT = """You summarize recent SEC Form 4 insider filings for a \
monitoring dashboard. Readers are not finance experts: every item must say plainly WHO did \
WHAT with WHICH security of WHICH company, and how much. You get one line per filing \
(sorted by dollar value, largest first) with flags computed in code.

Output format — plain text, no markdown symbols except the bullet "•", no tables:

LARGEST TRADES
• <Person> (<role>) <bought|sold|received|gave up> <number> shares of <Company full name> \
(<TICKER>, <security, e.g. "Class A common stock" / "stock options">) for about <$ total> \
(<$ per share>) on <date>. Now owns <number>.
Example: "Eric Affeldt (Director) bought 42,857 shares of AIAI Holdings (AIAI, Class A common \
stock) for about $150K ($3.50 per share) on Sep 22, 2026. Now owns 47,857."
The traded stock is ALWAYS the stock of the company in that line — always name the company in \
full, never just "shares of common stock".
(2-3 bullets)

MOST UNUSUAL
• Same sentence shape, then " — why notable: <one short reason>".
(1-3 bullets; skip the section with "Nothing unusual in this batch." if nothing qualifies)

Rules: plain text only — no "#", "**", "*", "---" or other markdown. Say "bought"/"sold" in words — never transaction codes like (P) or (S). Round money \
sensibly ($48K, $1.2M). If a person made several transactions in one filing, combine them into \
one bullet with the total. Treat as unusual: insider clusters (CLUSTER-BUY / CLUSTER-SELL: \
several different insiders of the same company buying — or selling — on the open market within \
a few days; clusters of buyers are among the strongest signals in this data, so list them first \
under MOST UNUSUAL and name everyone involved), open-market purchases (insiders rarely buy with \
their own money), CROSS-COMPANY (the same person is an insider at several companies in this \
data, e.g. a CEO of one company trading another company's stock), BIG-STAKE-CHANGE, 10% owners. \
Trades flagged PRE-PLANNED-10b5-1 were scheduled months in advance under a Rule 10b5-1 trading \
plan: mention them only as context (e.g. in LARGEST TRADES, saying "pre-planned sale" in words), \
never as a notable or unusual signal, however large. \
Grants, tax withholding and option exercises are routine pay — only mention them if very large. \
Use only the given data; never invent prices, motives or context. Not investment advice."""

INSIDER_SUMMARY_MAX_TOKENS = 900


async def summarize_insider_trades(table_text: str) -> tuple[str, str, dict]:
    """Returns (summary text, model id OpenRouter actually used, usage_info())."""
    api_key, base_url, model = _current_settings()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": INSIDER_SYSTEM_PROMPT},
                {"role": "user", "content": table_text},
            ],
            max_tokens=INSIDER_SUMMARY_MAX_TOKENS,
            timeout=60,
        )
    except Exception as ex:
        raise RuntimeError(_short_error(ex)) from ex
    text = _strip_markdown((response.choices[0].message.content or "").strip())
    used_model = getattr(response, "model", None) or model
    usage = usage_info(response)
    _record_cost("insider-summary", "Insider Trades · Sum up most notable", used_model, usage)
    return text, used_model, usage


# --- Analysis & Reasoning (2026-09-25): "✦ Analyse selection" ---

ANALYSIS_SYSTEM_PROMPT = """You are a cautious macro analyst writing for a monitoring \
dashboard. The user selected several data items (macro indicators, derived indicators, the \
macro regime, SEC insider-trading signals, calendar dates); each comes with its value, change, \
signal level and facts computed in code. Explain what they mean TOGETHER for a reader who is \
interested but not a professional.

Output format — plain text, English, no markdown symbols (no "#", "**", "*", "---", no \
tables); the only bullet is "•". Use exactly these section headings, uppercase, each on its \
own line, in this order:

BIG PICTURE
2-4 sentences: what the selected data say overall about where the economy and markets stand. \
If the signals are mixed, say so plainly.

HOW THE SIGNALS FIT TOGETHER
• One bullet per link between selected items — name the items and their numbers. Cover both \
confirmations (items pointing the same way) and contradictions (items pointing different ways).

SCENARIOS (NEXT 3–12 MONTHS)
• Base case (~NN%): 1-2 sentences. Would be confirmed by: <concrete data to watch>.
• Upside (~NN%): 1-2 sentences. Would be confirmed by: <concrete data to watch>.
• Downside (~NN%): 1-2 sentences. Would be confirmed by: <concrete data to watch>.
The three rough probabilities must add up to about 100% and are judgement, not model output.

WHAT TO WATCH
• <date> — <event>: why it matters for the scenarios above. Use only dates from the \
provided UPCOMING KEY DATES list or from the selected items; never invent dates.

ASSET-CLASS TENDENCIES
• How government bonds, equities (and sectors), USD/EUR and credit have historically tended \
to behave in a comparable setting — phrased as historical tendencies ("has tended to", \
"could"), hedged. Mention single stocks only if they appear in the provided insider data, and \
only descriptively. NEVER give buy/sell/hold recommendations, allocations or price targets.

UNCERTAINTIES & LIMITS
• Data lags and revisions, what is missing from the selection, and why this reading could be wrong.

Rules: use only the provided data plus general background knowledge that you clearly mark as \
such ("historically …"). Never invent numbers, dates, forecasts or consensus figures; if \
something is not in the data, say it is not available. Prefer cautious wording ("tends to", \
"could", "suggests"). Do not end with a disclaimer paragraph — the app shows its own caveat."""

ANALYSIS_MAX_TOKENS = 1600
ANALYSIS_TIMEOUT_SECONDS = 120


def analysis_models() -> dict:
    """Model ids per tier, read fresh from .env like _current_settings():
    "standard" = OPENROUTER_MODEL (same model as the other AI buttons),
    "premium"  = OPENROUTER_ANALYSIS_PREMIUM_MODEL (only the Premium switch)."""
    _, _, standard = _current_settings()
    return {
        "standard": standard,
        "premium": os.getenv("OPENROUTER_ANALYSIS_PREMIUM_MODEL", _DEFAULT_PREMIUM_MODEL) or _DEFAULT_PREMIUM_MODEL,
    }


def usage_info(response) -> dict:
    """Token counts and the cost OpenRouter charged for this request.

    OpenRouter always returns a `usage` object; besides the standard token
    counts it contains `cost` — the amount charged, in OpenRouter credits
    (1 credit = 1 USD) — see openrouter.ai/docs/use-cases/usage-accounting.
    The `openai` client keeps such non-standard fields as extra attributes.
    Missing values stay None (e.g. a provider that doesn't report cost)."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"cost_usd": None, "prompt_tokens": None, "completion_tokens": None}
    extra = getattr(usage, "model_extra", None) or {}
    cost = getattr(usage, "cost", None)
    if cost is None:
        cost = extra.get("cost")
    try:
        cost = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost = None
    return {
        "cost_usd": cost,
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
    }


async def analyse_selection(context_text: str, tier: str) -> tuple[str, str, dict]:
    """Returns (analysis text, model id OpenRouter actually used, usage_info())."""
    api_key, base_url, _ = _current_settings()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    models = analysis_models()
    model = models.get(tier) or models["standard"]
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": context_text},
            ],
            max_tokens=ANALYSIS_MAX_TOKENS,
            timeout=ANALYSIS_TIMEOUT_SECONDS,
        )
    except Exception as ex:
        raise RuntimeError(_short_error(ex)) from ex
    text = _strip_markdown((response.choices[0].message.content or "").strip())
    used_model = getattr(response, "model", None) or model
    usage = usage_info(response)
    _record_cost("analysis", "Analysis & Reasoning", used_model, usage)
    return text, used_model, usage


def _strip_markdown(text: str) -> str:
    """The popup shows this in a plain WPF TextBlock, which renders markdown
    literally ("### **MOST UNUSUAL**", "---"). Models don't always follow
    "no markdown", so strip the common syntax as a safety net."""
    import re
    lines = []
    for line in text.splitlines():
        if re.fullmatch(r"[-*_]{3,}", line.strip()):
            continue                                            # horizontal rules
        line = re.sub(r"^\s*#{1,6}\s*", "", line)               # headings
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)            # bold
        line = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", line)  # italics
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)           # "- item" -> bullet
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
