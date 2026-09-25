# Fed Agent

> Part of UFOS.ai, a proof-of-concept portfolio project (not a published product) — see the repo root README.

Autonomous pipeline that listens to a live Federal Reserve press conference / FOMC stream,
transcribes it in real time, and asks an LLM to interpret the remarks alongside live market
data to produce a structured long/short read across key asset categories.

## Pipeline

```
YouTube live stream (yt-dlp)
    -> ffmpeg (PCM16 mono, 16kHz)
    -> Deepgram streaming transcription
    -> finalized transcript segments
    -> DeepSeek v3 (via OpenRouter) + live market snapshot + scenario playbooks
    -> structured long/short/neutral stance per category
    -> WebSocket broadcast + JSONL log (for a future frontend)
```

Market data (Yahoo Finance quote pages) is scraped in the background on a fixed interval and
cached; each LLM call uses the latest cached snapshot.

## Asset categories & tickers

| Category | Tickers |
|---|---|
| Treasury yields | ^TNX, ^TYX |
| Bond proxies | TLT, AGG |
| REITs | VNQ |
| Utilities | XLU |
| Homebuilders | XHB, ITB |
| Tech giants | QQQ, MAGS |
| Unprofitable growth | ARKK |
| Financials | XLF |

## Setup

1. Install [ffmpeg](https://ffmpeg.org/) and ensure it's on your `PATH`.
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in:
   - `DEEPGRAM_API_KEY` — Deepgram streaming transcription API key
   - `OPENROUTER_API_KEY` — OpenRouter API key (used to call DeepSeek v3)
   - `YOUTUBE_URL` — URL of the live Fed press conference / FOMC stream

## Running

```
python main.py
```

- Transcript, market, and analysis events are logged as JSON lines to `data/logs/<date>.jsonl`.
- The same events are broadcast live over a WebSocket at `ws://<WS_HOST>:<WS_PORT>` (default
  `ws://0.0.0.0:8765`) for a future frontend to consume.

## Scenario playbooks

`data/scenarios/*.md` contain few-shot reference material (hawkish surprise, dovish pivot,
rate-decision surprises, balance sheet/QT commentary) describing historical market reaction
patterns. These are included in every LLM prompt to ground its interpretation.

## AI cost ledger (2026-09-25)

Every OpenRouter call this backend makes (the periodic **verdict** and the per-segment
**analysis**) is appended to the shared UFOS.ai cost ledger
`%LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl` (override with `UFOS_AI_COST_LOG` = full path) by
`cost_ledger.py` - one JSON line per call with `ts, module, feature, feature_label, provider,
model, cost_usd, prompt_tokens, completion_tokens, status`. `cost_usd` is OpenRouter's
`usage.cost` (credits; 1 credit = 1 USD). Writing never raises; failures are logged as warnings.
The line format is identical in every UFOS.ai module (each keeps its own small writer copy).

Verdict (and analysis) payloads now also carry an optional
`"usage": {"cost_usd", "prompt_tokens", "completion_tokens"}`; the WPF window shows it as a
small grey line under the reason ("Cost: $0.0021 · 812 in / 190 out tokens"), kept per entry in
the verdict history. Tests: `python -m unittest tests.test_cost_ledger -v` (from `backend/`).

## Notes / known limitations

- Yahoo Finance scraping has no fallback data source; if Yahoo changes its page structure or
  blocks requests, market snapshots will silently go stale for the affected tickers (see logs).
- No frontend is included yet — the WebSocket server and JSONL logs are the integration surface.
