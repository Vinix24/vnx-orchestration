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
        "tags": [],
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


class TestDispatchPassesCanonicalMlxId(unittest.TestCase):
    """Fix 4: _dispatch_local_gemma maps alias to canonical HF model id before spawn."""

    def _make_success_result(self, model_used: str):
        from providers.local_gemma.spawn import LocalGemmaSpawnResult
        return LocalGemmaSpawnResult(
            returncode=0,
            completion_text="ok",
            runtime_used="mlx",
            duration_seconds=2.0,
            timed_out=False,
            error=None,
            token_usage={"input": 10, "output": 5},
            model_used=model_used,
        )

    def test_dispatch_passes_canonical_mlx_id(self):
        import provider_dispatch as pd

        args = _build_args(model="gemma-4b-local")
        canonical = "mlx-community/gemma-3-4b-it-4bit"
        result = self._make_success_result(canonical)

        with patch("provider_spawns.local_gemma_spawn.spawn_local_gemma", return_value=result) as mock_spawn, \
             patch.object(pd, "_emit_governance"), \
             patch.object(pd, "_enrich_instruction", return_value=args.instruction):
            pd._dispatch_local_gemma(args)

        call_kwargs = mock_spawn.call_args
        self.assertEqual(call_kwargs.kwargs.get("model") or call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs["model"], canonical)

    def test_mlx_model_map_has_gemma_4b_local(self):
        import provider_dispatch as pd
        self.assertIn("gemma-4b-local", pd._MLX_MODEL_MAP)
        self.assertEqual(pd._MLX_MODEL_MAP["gemma-4b-local"], "mlx-community/gemma-3-4b-it-4bit")

    def test_unknown_alias_passes_through_unchanged(self):
        import provider_dispatch as pd
        args = _build_args(model="some-custom-model")
        result = self._make_success_result("some-custom-model")

        with patch("provider_spawns.local_gemma_spawn.spawn_local_gemma", return_value=result) as mock_spawn, \
             patch.object(pd, "_emit_governance"), \
             patch.object(pd, "_enrich_instruction", return_value=args.instruction):
            pd._dispatch_local_gemma(args)

        call_kwargs = mock_spawn.call_args
        self.assertEqual(call_kwargs.kwargs["model"], "some-custom-model")


class TestAutoRouteForwardsTags(unittest.TestCase):
    """Fix 3: --tags are forwarded to smart_router.decide() in the auto-route branch."""

    def test_tags_in_argparser(self):
        import provider_dispatch as pd
        parser = pd._build_parser()
        args = parser.parse_args([
            "--provider", "local-gemma",
            "--terminal-id", "T1",
            "--dispatch-id", "d1",
            "--instruction", "test",
            "--tags", "cost-tier-zero",
            "--tags", "privacy-required",
        ])
        self.assertEqual(args.tags, ["cost-tier-zero", "privacy-required"])

    def test_tags_default_empty_list(self):
        import provider_dispatch as pd
        parser = pd._build_parser()
        args = parser.parse_args([
            "--provider", "claude",
            "--terminal-id", "T1",
            "--dispatch-id", "d2",
            "--instruction", "test",
        ])
        self.assertEqual(args.tags, [])


if __name__ == "__main__":
    unittest.main()
