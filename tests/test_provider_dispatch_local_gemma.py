"""Tests for the local-gemma provider routing in provider_dispatch.py (Smart Lanes PR-1)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def _build_args(**overrides):
    import argparse
    defaults = {
        "provider": "local-gemma",
        "terminal_id": "T1",
        "dispatch_id": "test-dispatch-local-gemma-01",
        "instruction": "Classify task: add a button to dashboard",
        "model": "gemma-4b-local",
        "max_retries": 3,
        "no_auto_commit": False,
        "gate": "",
        "dispatch_paths": "",
        "pr_id": None,
        "role": "classifier",
        "no_repo_map": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestProviderDispatchLocalGemmaArgParser(unittest.TestCase):
    def test_local_gemma_accepted_in_implemented_providers(self):
        import provider_dispatch as pd
        self.assertIn("local-gemma", pd._IMPLEMENTED_PROVIDERS)

    def test_local_gemma_in_registry_key_map(self):
        import provider_dispatch as pd
        self.assertEqual(pd._PROVIDER_TO_REGISTRY_KEY.get("local-gemma"), "local_gemma")


class TestDispatchLocalGemmaSuccess(unittest.TestCase):
    def _make_success_result(self):
        from providers.local_gemma.spawn import LocalGemmaSpawnResult
        return LocalGemmaSpawnResult(
            returncode=0,
            completion_text="classification: UI feature",
            runtime_used="mlx",
            duration_seconds=3.5,
            timed_out=False,
            error=None,
            token_usage={"input": 20, "output": 8},
            model_used="mlx-community/gemma-3-4b-it-4bit",
        )

    def test_dispatch_local_gemma_calls_spawn(self):
        import provider_dispatch as pd

        args = _build_args()
        result = self._make_success_result()

        with patch("provider_spawns.local_gemma_spawn.spawn_local_gemma", return_value=result), \
             patch.object(pd, "_emit_governance") as mock_emit, \
             patch.object(pd, "_enrich_instruction", return_value=args.instruction):
            exit_code = pd._dispatch_local_gemma(args)

        self.assertEqual(exit_code, 0)
        mock_emit.assert_called_once()
        call_kwargs = mock_emit.call_args
        self.assertEqual(call_kwargs.args[1], "local-gemma")
        self.assertEqual(call_kwargs.args[-1], "success")

    def test_dispatch_local_gemma_failure_returns_1(self):
        import provider_dispatch as pd
        from providers.local_gemma.spawn import LocalGemmaSpawnResult

        args = _build_args()
        result = LocalGemmaSpawnResult(
            returncode=1,
            completion_text="",
            runtime_used="ollama",
            duration_seconds=10.0,
            timed_out=False,
            error="both runtimes failed",
            token_usage={"input": 5, "output": 0},
            model_used="gemma-4b-local",
        )

        with patch("provider_spawns.local_gemma_spawn.spawn_local_gemma", return_value=result), \
             patch.object(pd, "_emit_governance"), \
             patch.object(pd, "_enrich_instruction", return_value=args.instruction):
            exit_code = pd._dispatch_local_gemma(args)

        self.assertEqual(exit_code, 1)


class TestExtractTokenUsageLocalGemma(unittest.TestCase):
    def test_local_gemma_token_extraction(self):
        import provider_dispatch as pd
        from providers.local_gemma.spawn import LocalGemmaSpawnResult

        result = LocalGemmaSpawnResult(
            returncode=0,
            completion_text="output",
            runtime_used="mlx",
            duration_seconds=1.0,
            timed_out=False,
            error=None,
            token_usage={"input": 42, "output": 17},
            model_used="gemma-4b-local",
        )

        usage = pd._extract_token_usage(result, "local-gemma")
        self.assertEqual(usage["input"], 42)
        self.assertEqual(usage["output"], 17)
        self.assertEqual(usage["cache_hit"], 0)

    def test_cost_zero_for_local_gemma(self):
        import provider_dispatch as pd

        # Local gemma should always compute $0 cost
        cost = pd._compute_cost("local-gemma", "gemma-4b-local", {"input": 100, "output": 50})
        # Either None (registry miss + provider_costs miss) or 0.0
        self.assertIn(cost, (None, 0.0))


if __name__ == "__main__":
    unittest.main()
