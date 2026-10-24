"""Daily AI caps of the LiveStreamAgent backend (task B, 2026-10-17; contract
UFOS.ai/docs/ai-budgets-spec.md §3/§4): ai_caps.DailyCaps over the real livestream.db (temp file),
llm_analyst checks BEFORE every call (openai + config stubbed — no network, no .env), the
"ai_budget" notifier and the main.py wiring. stdlib unittest only.

Run from LiveStreamAgent/backend:
    python -m unittest discover -s tests -t .
"""
import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_caps  # noqa: E402
import storage  # noqa: E402
from tests.test_storage import _import_main  # noqa: E402


@dataclass
class _Snapshot:
    quotes: dict = field(default_factory=dict)
    timestamp: str = "2026-10-17T12:00:00+00:00"


class _Usage:
    def __init__(self, cost):
        self.cost, self.prompt_tokens, self.completion_tokens = cost, 100, 20


class _Resp:
    def __init__(self, content, cost=0.0009):
        self.model = "test/model"
        self.usage = _Usage(cost)
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]


class _Client:
    """Fake AsyncOpenAI: ``behaviour`` = a response or an exception to raise."""
    instances = []

    def __init__(self, **kw):
        self.calls = []
        self.behaviour = _Resp('{"verdict": "LONG", "ticker": "QQQ", "confidence": 70, "reason": "x"}')
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))
        _Client.instances.append(self)

    async def _create(self, **kw):
        self.calls.append(kw)
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return self.behaviour


def _import_analyst():
    cfg = types.SimpleNamespace(MarketSnapshot=_Snapshot, OPENROUTER_API_KEY="", OPENROUTER_BASE_URL="https://x",
                                OPENROUTER_MODEL="test/model", SCENARIOS_DIR=os.path.join(BACKEND, "no-such-dir"),
                                iter_ticker_groups=lambda: [])
    with mock.patch.dict(sys.modules, {"openai": types.SimpleNamespace(AsyncOpenAI=_Client), "config": cfg}):
        sys.modules.pop("llm_analyst", None)
        module = importlib.import_module("llm_analyst")
    sys.modules.pop("llm_analyst", None)
    return module


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "livestream.db")
        self.store = storage.Storage(self.path)
        self.main = _import_main()
        self.caps = self.main.build_ai_caps(self.store)
        self._env = mock.patch.dict(os.environ, {"UFOS_AI_COST_LOG": os.path.join(self._tmp.name, "cost.jsonl")})
        self._env.start()
        import credstore      # stage 10 S10-E: keys come from the Credential Manager (a MemoryStore here)
        credstore.set_store(credstore.MemoryStore({credstore.TARGETS["openrouter-api-key"][0]: "test-key"}))
        self.addCleanup(credstore.set_store, None)

    def tearDown(self):
        self._env.stop()
        self.store.close()
        self._tmp.cleanup()


class CapsAndPayload(_Case):
    def test_defaults_and_payload(self):
        caps = {c.key: c for c in self.main.AI_CAPS}
        self.assertEqual((caps["analysis"].default_usd, caps["verdicts"].default_usd), (5.0, 1.0))
        self.assertEqual((caps["analysis"].estimate_usd, caps["verdicts"].estimate_usd), (0.005, 0.003))
        status, p = self.caps.handle_get()
        self.assertEqual((status, p["module"], p["label"]), (200, "livestream-agent", "LiveStream Agent"))
        self.assertEqual([b["key"] for b in p["budgets"]], ["analysis", "verdicts", "transcription"])
        self.assertEqual(p["budgets"][1]["cost_log"], [{"module": "LiveStreamAgent", "feature": "verdict"}])

    def test_put_persists_across_restart_and_refuses_origin(self):
        self.assertEqual(self.caps.handle_put({"key": "verdicts", "limit_usd": 2.5})[0], 200)
        self.assertEqual(self.caps.handle_put({"key": "verdicts", "limit_usd": 2}, origin="null")[0], 403)
        self.assertEqual(self.caps.handle_put({"key": "verdicts", "limit_usd": 51})[0], 400)
        self.store.close()
        self.store = storage.Storage(self.path)
        self.assertEqual(self.main.build_ai_caps(self.store).limit("verdicts"), 2.5)

    def test_in_memory_store_refuses(self):
        self.store.in_memory = True
        with self.assertRaises(ai_caps.BudgetBlocked) as cm:
            self.main.build_ai_caps(self.store).reserve("analysis")
        self.assertEqual(cm.exception.reason, "store")


class AnalystChecks(_Case):
    def setUp(self):
        super().setUp()
        self.mod = _import_analyst()
        self.analyst = self.mod.LLMAnalyst(store=self.store, caps=self.caps)
        self.assertIsNone(self.analyst._key_error())      # builds the client with the key (stage 10 S10-E)
        self.client = _Client.instances[-1]

    def test_verdict_blocked_before_the_call(self):
        self.caps.put("verdicts", 0.0)
        out = asyncio.run(self.analyst.generate_verdict(_Snapshot()))
        self.assertEqual((out["error"], out["budget_key"], out["reason"]), ("budget", "verdicts", "off"))
        self.assertIn("switched off", out["detail"])
        self.assertEqual(self.client.calls, [])

    def test_verdict_books_reported_cost(self):
        out = asyncio.run(self.analyst.generate_verdict(_Snapshot()))
        self.assertEqual(out["verdict"], "LONG")
        self.assertAlmostEqual(self.caps.spent("verdicts"), 0.0009)
        self.client.behaviour = _Resp("not json", cost=0.0004)       # paid, then unusable -> still booked once
        out = asyncio.run(self.analyst.generate_verdict(_Snapshot()))
        self.assertEqual(out["error"], "llm_failed")
        self.assertAlmostEqual(self.caps.spent("verdicts"), 0.0013)

    def test_analysis_blocked_keeps_transcript_and_makes_no_call(self):
        self.store.set_meta("ai_cap_spend:analysis:" + self.caps.day(), 5.0)
        out = asyncio.run(self.analyst.analyze("rates stay higher for longer", _Snapshot()))
        self.assertEqual((out["error"], out["reason"], out["categories"]), ("budget", "reached", {}))
        self.assertEqual(self.client.calls, [])
        self.assertIn("rates stay higher for longer", list(self.analyst._recent_transcript))

    def test_analysis_timeout_books_estimate_and_reraises(self):
        class APITimeoutError(Exception):
            pass

        self.client.behaviour = APITimeoutError("Request timed out.")
        with self.assertRaises(APITimeoutError):
            asyncio.run(self.analyst.analyze("segment", _Snapshot()))
        self.assertAlmostEqual(self.caps.spent("analysis"), 0.005)
        self.client.behaviour = RuntimeError("401 unauthorized")
        with self.assertRaises(RuntimeError):
            asyncio.run(self.analyst.analyze("segment", _Snapshot()))
        self.assertAlmostEqual(self.caps.spent("analysis"), 0.005)
        self.assertEqual(self.caps._reserved["analysis"], 0.0)

    def test_without_caps_unchanged(self):
        analyst = self.mod.LLMAnalyst(store=self.store)
        out = asyncio.run(analyst.generate_verdict(_Snapshot()))
        self.assertEqual(out["verdict"], "LONG")


class Notifier(_Case):
    def test_broadcasts_on_change_only(self):
        sent = []

        class _B:
            async def broadcast(self, kind, payload):
                sent.append((kind, payload))

        n = self.main.BudgetNotifier(self.caps, _B())
        blocked = {"error": "budget", "reason": "reached", "detail": "Daily AI cap reached"}
        self.assertTrue(asyncio.run(n.update("analysis", blocked)))
        self.assertTrue(asyncio.run(n.update("analysis", blocked)))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], ("ai_budget", {"key": "analysis", "label": "Live analysis (per transcript segment)",
                                                 "blocked": True, "reason": "reached",
                                                 "message": "Daily AI cap reached"}))
        self.assertEqual(n.current()[0]["key"], "analysis")
        self.assertFalse(asyncio.run(n.update("analysis", {"error": "llm_failed"})))   # passed the cap
        self.assertEqual(sent[-1][1]["blocked"], False)
        self.assertEqual(n.current(), [])
        self.assertFalse(asyncio.run(n.update("verdicts", {"verdict": "LONG"})))
        self.assertEqual(len(sent), 2)

    def test_main_wiring(self):
        with open(os.path.join(BACKEND, "main.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("LLMAnalyst(store=store, caps=budgets)", src)
        self.assertIn("add_get('/ai/budgets', _ai_budgets_get)", src)
        self.assertIn("add_put('/ai/budgets', _ai_budgets_put)", src)
        self.assertIn('if notifier is not None and await notifier.update("analysis", analysis):', src)
        self.assertIn("notifier.current()", src)
        self.assertIn("ai_caps.ORIGIN_REFUSED", src)


class _Bcast:
    def __init__(self):
        self.sent = []

    async def broadcast(self, kind, payload):
        self.sent.append((kind, payload))


class RefreshAfterPut(_Case):
    def test_refresh_clears_a_block_at_once(self):
        b = _Bcast()
        n = self.main.BudgetNotifier(self.caps, b)
        self.caps.put("analysis", 0.0)
        asyncio.run(n.update("analysis", {"error": "budget", "reason": "off", "detail": "off"}))
        self.assertEqual(self.caps.handle_put({"key": "analysis", "limit_usd": 5})[0], 200)
        asyncio.run(n.refresh())
        self.assertEqual(b.sent[-1], ("ai_budget", {"key": "analysis", "label": "Live analysis (per transcript segment)",
                                                    "blocked": False, "reason": None, "message": None}))
        before = len(b.sent)
        asyncio.run(n.refresh())                           # nothing changed -> nothing sent
        self.assertEqual(len(b.sent), before)
        self.caps.put("verdicts", 0.0)                       # lowering a cap is announced too
        asyncio.run(n.refresh())
        self.assertEqual((b.sent[-1][1]["key"], b.sent[-1][1]["blocked"]), ("verdicts", True))
        with open(os.path.join(BACKEND, "main.py"), encoding="utf-8") as f:
            self.assertIn("if status == 200:\n            await notifier.refresh()", f.read())


class Transcription(_Case):
    def meter(self, ledger=None):
        return self.main.TranscriptionMeter(self.caps, usd_per_minute=0.01, interval_minutes=5,
                                            bytes_per_minute=100, ledger=ledger)

    def test_defaults(self):
        cap = {c.key: c for c in self.main.AI_CAPS}["transcription"]
        self.assertEqual((cap.default_usd, cap.estimate_usd, cap.max_usd), (3.0, 0.05, 50.0))
        self.assertEqual(self.main.TRANSCRIPTION_USD_PER_MINUTE, 0.01)
        self.assertEqual(self.main.TRANSCRIPTION_BYTES_PER_MINUTE, 1920000)
        self.assertIn("transcription", [b["key"] for b in self.caps.payload()["budgets"]])

    def test_meter_books_intervals_and_blocks(self):
        lines = []
        self.caps.put("transcription", 0.12)
        m = self.meter(lambda *a, **k: lines.append((a, k)))
        m.start()
        self.assertTrue(m.add(499))
        self.assertTrue(m.add(1))                           # interval 1 booked, interval 2 reserved
        self.assertAlmostEqual(self.caps.spent("transcription"), 0.05)
        self.assertFalse(m.add(500))                        # interval 2 booked; interval 3 does not fit
        self.assertEqual(m.blocked.reason, "reached")
        self.assertAlmostEqual(self.caps.spent("transcription"), 0.10)
        m.finish()
        m.finish()
        self.assertAlmostEqual(self.caps.spent("transcription"), 0.10)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0][0][:3], ("transcription", "deepgram/nova-2", {"cost_usd": 0.05}))
        self.assertEqual(lines[0][1], {"status": "estimated", "provider": "Deepgram"})

    def test_partial_interval_booked_and_logged(self):
        m = self.meter()                                    # real cost_ledger (temp file via env)
        m.start()
        m.add(250)
        m.finish()
        self.assertAlmostEqual(self.caps.spent("transcription"), 0.025)
        import json as _json
        with open(os.environ["UFOS_AI_COST_LOG"], encoding="utf-8") as f:
            line = _json.loads(f.read().splitlines()[-1])
        self.assertEqual((line["feature"], line["provider"], line["status"], line["cost_usd"]),
                         ("transcription", "Deepgram", "estimated", 0.025))

    def test_start_blocked(self):
        self.caps.put("transcription", 0.0)
        with self.assertRaises(ai_caps.BudgetBlocked):
            self.meter().start()

    def test_run_stops_cleanly_and_resumes_after_raise(self):
        self.caps.put("transcription", 0.06)               # one 5-minute interval fits, the second not
        sessions, closed = [], []

        def source():
            n = len(sessions)
            sessions.append([])

            async def gen():
                try:
                    for _ in range(50 if n == 0 else 3):
                        yield b"x" * 100
                finally:
                    closed.append(n)                           # "ffmpeg stopped"
            return gen()

        async def transcribe(stream, queue):
            async for chunk in stream:
                sessions[-1].append(chunk)

        caps = self.caps

        class _Notifier:
            def __init__(self):
                self.updates = []

            async def update(self, key, result):
                self.updates.append((key, result.get("reason")))
                if result.get("error") == "budget":
                    caps.put("transcription", 5.0)            # the user raises the cap
                return result.get("error") == "budget"

        notifier = _Notifier()
        with mock.patch.object(self.main.TranscriptionMeter, "__init__",
                               lambda s, c, **kw: TranscriptionMeterInit(s, c, self.main)):
            asyncio.run(self.main._run_transcription(asyncio.Queue(), self.caps, notifier, source=source,
                                                     transcribe=transcribe, recheck_seconds=0))
        self.assertEqual([len(x) for x in sessions], [4, 3])  # stopped before the blocked chunk, resumed
        self.assertEqual(closed, [0, 1])
        self.assertEqual(notifier.updates, [("transcription", None), ("transcription", "reached"),
                                            ("transcription", None)])


def TranscriptionMeterInit(self, caps, main):
    """Small units for the run test: 100 bytes per minute, $0.01 per minute, 5-minute intervals."""
    self.caps, self.usd_per_minute, self.interval_bytes, self.bytes_per_minute = caps, 0.01, 500, 100
    self.interval_usd, self.ledger, self.blocked, self._res, self._bytes, self.done = (
        0.05, lambda *a, **k: None, None, None, 0, False)


if __name__ == "__main__":
    unittest.main()
