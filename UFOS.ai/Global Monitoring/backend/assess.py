"""AI assessment for the "Assess"/"Sentiment" button on a globe marker.

Same OpenRouter call pattern as UFOS.ai/Services/OpenRouterClient.cs (the
root app's "Summarize with AI" button) and LiveStreamAgent/backend/llm_analyst.py
(same env vars, same default model) — via the `openai` package pointed at
OpenRouter's OpenAI-compatible endpoint, not a hand-rolled HTTP client.

This replaces the frontend's old canned/illustrative per-category text with
an actual model call over the specific marker's real data (title, summary,
source, category) — still clearly a short, best-effort read, not investment
advice, which the prompt says explicitly so the model doesn't overstate its
own certainty.

2026-09-22: is_configured()/assess_marker() re-read .env on every call
instead of trusting config.py's OPENROUTER_* constants, which are captured
once at process start. The natural way to hit "the button still doesn't
work despite a key being set" is to add the key to .env *after* the backend
is already running (e.g. right after first seeing the "no key configured"
message) — with the old import-time snapshot, that needs a full backend
restart to take effect; with a fresh env read per call, the very next click
after saving .env just works.
"""
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import AsyncOpenAI

from config import OPENROUTER_API_KEY as _DEFAULT_API_KEY
from config import OPENROUTER_BASE_URL as _DEFAULT_BASE_URL
from config import OPENROUTER_MODEL as _DEFAULT_MODEL
import cost_ledger

# 2026-09-24: the old prompt ("you have no information beyond what is given
# ... if the input is too thin, say so") plus a one-line teaser as the only
# input made "not enough context" the most common answer. The model now gets
# real context (assess_context.py: full article text, location, related live
# events) and may use its general background knowledge — clearly marked as
# such, never as invented specifics about this event.
SYSTEM_PROMPT = """You are an analyst assisting a real-time global monitoring dashboard. \
You get one event (category, headline, summary, source) plus supporting material gathered by \
the dashboard: structured facts, often the full article text, the approximate location, and a \
list of CANDIDATE related events.

Write 3-5 plain sentences (no markdown, no headers, no bullet points) about THIS event:
what is happening, how serious it is, and why it matters (for markets, security, or the region).

Rules:
1. Candidate related events are unfiltered. Mention one only if it is clearly connected to this \
event (same place AND same topic, or a plausible direct link). Never list, summarize or comment \
on the others — do not say that something is unrelated; just leave it out.
2. Use only facts from the provided material for this event. If figures or dates in the material \
contradict each other, say so briefly instead of picking one. Prefer structured facts (ISO dates) \
over dates written inside prose.
3. Do not draw conclusions from missing information (not: "the lack of details suggests a small impact").
4. Background knowledge is allowed only if it is specific and informative for this event (e.g. \
what a GDACS alert level means, what a callsign family is used for, a conflict's recent history). \
Phrase it as background ("typically", "as of my training data"). No generic filler (not: \
"floods can strain infrastructure").
5. Do not open with a complaint about limited context; if something essential is genuinely \
missing, name it in one short clause at the end.
This is an informational read, not investment or safety advice."""


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
        "generator": f"UFOS.ai/GlobalMonitoring/{feature}",
        "provider": "OpenRouter",
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "human_reviewed": False,
        "label": "AI-generated content (EU AI Act Art. 50)",
    }


def usage_info(response) -> dict:
    """Token counts and the cost OpenRouter charged for this request
    (2026-09-25, copy of HedgeFund/backend/newsroom/ai.py's usage_info).

    OpenRouter always returns a `usage` object; besides the standard token
    counts it contains `cost` — the amount charged, in OpenRouter credits
    (1 credit = 1 USD). The `openai` client keeps such non-standard fields
    as extra attributes. Missing values stay None."""
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


async def assess_marker(category_label: str, title: str, summary: str, source: str,
                        context: str = "") -> tuple[str, str, dict]:
    """Returns (assessment text, model id OpenRouter actually used, usage_info()).
    Records the call in the shared AI cost ledger (cost_ledger.py)."""
    api_key, base_url, model = _current_settings()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    user_content = (
        f"Category: {category_label}\n"
        f"Headline: {title}\n"
        f"Summary: {summary}\n"
        f"Source: {source}"
    )
    if context:
        user_content += "\n\n--- Supporting context ---\n" + context

    # A fresh client per call is deliberate here (not a cached singleton):
    # AsyncOpenAI's constructor does no I/O, and this is what makes a
    # just-edited API key/base URL/model take effect immediately.
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            # 2026-09-24: without an explicit max_tokens, some OpenRouter
            # upstream providers (seen: GMICloud) default the completion
            # budget to the model's ENTIRE context window (131072), so
            # input + completion always exceeds the limit and the request
            # fails with a 400 — intermittently, depending on which provider
            # OpenRouter happens to route to. The answer is 2-3 sentences;
            # 400 tokens is generous.
            max_tokens=ASSESS_MAX_TOKENS,
            timeout=30,
        )
    except Exception as ex:
        raise RuntimeError(_short_error(ex)) from ex
    content = response.choices[0].message.content
    used_model = getattr(response, "model", None) or model
    usage = usage_info(response)
    cost_ledger.record("assess", "Globe · AI Assess / Sentiment", used_model, usage)
    return (content or "").strip(), used_model, usage


ASSESS_MAX_TOKENS = 500


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


def _strip_markdown(text: str) -> str:
    """AI summaries are shown as plain text, which renders markdown literally
    ("### **HEADLINE**", "---"). Models don't always follow "no markdown",
    so strip the common syntax as a safety net. (The Insider Trades summary
    that used this first moved to HedgeFund/backend/newsroom/ai.py on
    2026-09-24, with its own copy.)"""
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


# --- City panel: "✦ AI Sum up local news" (2026-09-24) ---

LOCALNEWS_SYSTEM_PROMPT = """You summarize the current local news for one city for a global \
monitoring dashboard. You get a list of recent headlines (last ~3 days) with outlet and time; \
some have a one-line teaser. That is ALL you have — you have not read the articles.

Write 3-6 plain sentences (no markdown, no headers, no bullet points):
- Start with the 1-3 most significant developments (politics, security, economy, disasters, \
major public events), then briefly the other recurring themes.
- Group headlines that cover the same story; mention how many outlets report it when that \
shows importance.
- Name the city/country context only where it helps; skip trivia (sports results, celebrity \
news) unless it dominates the list.
Rules: use only what the headlines/teasers say — never invent details, numbers or causes; if a \
headline is ambiguous, say what it reports, not what it might mean. Background knowledge only \
if specific and phrased as background ("as of my training data"). No opening remark about \
limited information. Not investment or safety advice."""


async def summarize_local_news(city: str, country: str, articles: list) -> tuple[str, str, dict]:
    """Returns (summary text, model id actually used, usage_info()).
    Records the call in the shared AI cost ledger (cost_ledger.py)."""
    api_key, base_url, model = _current_settings()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    lines = []
    for i, a in enumerate(articles, 1):
        line = f"{i}. {a.get('title', '')} — {a.get('domain', '')}"
        if a.get("seendate"):
            line += f", {a['seendate']}"
        if a.get("summary"):
            line += f"\n   {a['summary'][:300]}"
        lines.append(line)
    user_content = f"City: {city}, {country}\nRecent headlines ({len(articles)}):\n" + "\n".join(lines)

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": LOCALNEWS_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=ASSESS_MAX_TOKENS + 100,
            timeout=45,
        )
    except Exception as ex:
        raise RuntimeError(_short_error(ex)) from ex
    text = _strip_markdown((response.choices[0].message.content or "").strip())
    used_model = getattr(response, "model", None) or model
    usage = usage_info(response)
    cost_ledger.record("localnews-summary", "Globe · AI Sum up local news", used_model, usage)
    return text, used_model, usage
