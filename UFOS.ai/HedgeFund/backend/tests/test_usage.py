"""usage_info(): cost + tokens from an OpenRouter response (offline)."""
import importlib.util
import os
import sys
import types
import unittest

_NEWSROOM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "newsroom")


def _load_ai():
    stubs = {"openai": types.ModuleType("openai"), "dotenv": types.ModuleType("dotenv"),
             "config": types.ModuleType("config")}
    stubs["openai"].AsyncOpenAI = object
    stubs["dotenv"].load_dotenv = lambda *a, **k: None
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL", "OPENROUTER_ANALYSIS_PREMIUM_MODEL"):
        setattr(stubs["config"], name, "x")
    saved = {n: sys.modules.get(n) for n in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location("ai_under_test", os.path.join(_NEWSROOM, "ai.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m


ai = _load_ai()


class _Usage:
    def __init__(self, extra, **kw):
        self.model_extra = extra
        for k, v in kw.items():
            setattr(self, k, v)


class UsageInfoTest(unittest.TestCase):
    def test_cost_from_extra_fields(self):
        r = types.SimpleNamespace(usage=_Usage({"cost": 0.0123}, prompt_tokens=1500, completion_tokens=900))
        self.assertEqual(ai.usage_info(r), {"cost_usd": 0.0123, "prompt_tokens": 1500, "completion_tokens": 900})

    def test_cost_as_attribute(self):
        r = types.SimpleNamespace(usage=_Usage({}, cost="0.5", prompt_tokens=1, completion_tokens=2))
        self.assertEqual(ai.usage_info(r)["cost_usd"], 0.5)

    def test_missing_usage(self):
        self.assertIsNone(ai.usage_info(types.SimpleNamespace())["cost_usd"])
        r = types.SimpleNamespace(usage=_Usage(None, prompt_tokens=3, completion_tokens=4))
        self.assertIsNone(ai.usage_info(r)["cost_usd"])


if __name__ == "__main__":
    unittest.main()
