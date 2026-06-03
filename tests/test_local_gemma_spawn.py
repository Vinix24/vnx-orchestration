"""Tests for local_gemma spawn handler — MLX primary + Ollama fallback paths.

All external subprocesses are mocked so no real MLX or Ollama is needed.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


class TestLocalGemmaSpawnMLXPrimary(unittest.TestCase):
    """spawn_local_gemma uses MLX when mlx_available() returns True."""

    def _make_mlx_success(self):
        m = MagicMock()
        m.returncode = 0
        m.stdout = "Hello from Gemma!"
        m.stderr = ""
        return m

    def test_mlx_primary_success(self):
        from providers.local_gemma.spawn import spawn_local_gemma

        with patch("providers.local_gemma.runtime_mlx.mlx_available", return_value=True), \
             patch("providers.local_gemma.runtime_mlx.run_mlx",
                   return_value=("Hello from Gemma!", True, None)):
            result = spawn_local_gemma(
                instruction="Say hello",
                dispatch_id="test-mlx-01",
                project_id="test",
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.runtime_used, "mlx")
        self.assertEqual(result.completion_text, "Hello from Gemma!")
        self.assertIsNone(result.error)
        self.assertEqual(result.frontmatter_fields()["cost_usd"], 0.0)

    def test_mlx_primary_failure_falls_back_to_ollama(self):
        from providers.local_gemma.spawn import spawn_local_gemma

        with patch("providers.local_gemma.runtime_mlx.mlx_available", return_value=True), \
             patch("providers.local_gemma.runtime_mlx.run_mlx",
                   return_value=("", False, "mlx_lm failed: model not found")), \
             patch("providers.local_gemma.spawn._run_ollama_fallback",
                   return_value=("Ollama response", True, None)):
            result = spawn_local_gemma(
                instruction="Say hello",
                dispatch_id="test-fallback-01",
                project_id="test",
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.runtime_used, "ollama")
        self.assertEqual(result.completion_text, "Ollama response")
        self.assertIsNotNone(result.error)  # mlx error preserved as warning
        self.assertIn("MLX primary failed", result.error)

    def test_mlx_unavailable_goes_straight_to_ollama(self):
        from providers.local_gemma.spawn import spawn_local_gemma

        with patch("providers.local_gemma.runtime_mlx.mlx_available", return_value=False), \
             patch("providers.local_gemma.spawn._run_ollama_fallback",
                   return_value=("Ollama output", True, None)):
            result = spawn_local_gemma(
                instruction="Classify: add a button",
                dispatch_id="test-no-mlx-01",
                project_id="test",
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.runtime_used, "ollama")

    def test_both_paths_fail_returns_failure(self):
        from providers.local_gemma.spawn import spawn_local_gemma

        with patch("providers.local_gemma.runtime_mlx.mlx_available", return_value=True), \
             patch("providers.local_gemma.runtime_mlx.run_mlx",
                   return_value=("", False, "mlx error")), \
             patch("providers.local_gemma.spawn._run_ollama_fallback",
                   return_value=("", False, "ollama not found")):
            result = spawn_local_gemma(
                instruction="Test",
                dispatch_id="test-both-fail-01",
                project_id="test",
            )

        self.assertEqual(result.returncode, 1)
        self.assertIsNotNone(result.error)
        self.assertIn("MLX primary failed", result.error)
        self.assertIn("Ollama fallback failed", result.error)


class TestLocalGemmaSpawnResult(unittest.TestCase):
    """LocalGemmaSpawnResult shape and frontmatter_fields() contract."""

    def test_frontmatter_fields_provider(self):
        from providers.local_gemma.spawn import LocalGemmaSpawnResult

        r = LocalGemmaSpawnResult(
            returncode=0,
            completion_text="Hi",
            runtime_used="mlx",
            duration_seconds=1.5,
            timed_out=False,
            error=None,
            token_usage={"input": 10, "output": 5},
            model_used="mlx-community/gemma-3-4b-it-4bit",
        )
        fm = r.frontmatter_fields()
        self.assertEqual(fm["provider"], "local-gemma")
        self.assertEqual(fm["cost_usd"], 0.0)
        self.assertEqual(fm["exit_code"], 0)
        self.assertEqual(fm["token_usage"]["input"], 10)
        self.assertEqual(fm["token_usage"]["cache_read"], 0)

    def test_frontmatter_fields_failure(self):
        from providers.local_gemma.spawn import LocalGemmaSpawnResult

        r = LocalGemmaSpawnResult(
            returncode=1,
            completion_text="",
            runtime_used="ollama",
            duration_seconds=5.0,
            timed_out=False,
            error="both runtimes failed",
            token_usage=None,
            model_used="gemma-4b-local",
        )
        fm = r.frontmatter_fields()
        self.assertEqual(fm["exit_code"], 1)
        self.assertEqual(fm["token_usage"]["input"], 0)

    def test_cost_always_zero(self):
        from providers.local_gemma.spawn import LocalGemmaSpawnResult

        r = LocalGemmaSpawnResult(
            returncode=0,
            completion_text="text",
            runtime_used="mlx",
            duration_seconds=2.0,
            timed_out=False,
            error=None,
            token_usage={"input": 100, "output": 200},
            model_used="mlx-community/gemma-3-4b-it-4bit",
        )
        self.assertEqual(r.frontmatter_fields()["cost_usd"], 0.0)


class TestOllamaFallback(unittest.TestCase):
    """Direct tests for _run_ollama_fallback."""

    def test_ollama_success(self):
        from providers.local_gemma.spawn import _run_ollama_fallback

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Sure, here is the answer."
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            output, success, error = _run_ollama_fallback("Say hi", "gemma3:4b", 60.0)

        self.assertTrue(success)
        self.assertEqual(output, "Sure, here is the answer.")
        self.assertIsNone(error)

    def test_ollama_binary_not_found(self):
        from providers.local_gemma.spawn import _run_ollama_fallback
        import subprocess

        with patch("subprocess.run", side_effect=FileNotFoundError):
            output, success, error = _run_ollama_fallback("Prompt", "gemma3:4b", 60.0)

        self.assertFalse(success)
        self.assertIn("ollama binary not found", error)

    def test_ollama_nonzero_exit(self):
        from providers.local_gemma.spawn import _run_ollama_fallback

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "model not found"

        with patch("subprocess.run", return_value=mock_result):
            output, success, error = _run_ollama_fallback("Prompt", "bad-model", 60.0)

        self.assertFalse(success)
        self.assertIn("ollama failed", error)


class TestShimImport(unittest.TestCase):
    """provider_spawns.local_gemma_spawn shim re-exports canonical spawn."""

    def test_shim_exports_spawn_local_gemma(self):
        from provider_spawns.local_gemma_spawn import spawn_local_gemma as spawn_shim
        from providers.local_gemma.spawn import spawn_local_gemma as spawn_canonical

        self.assertIs(spawn_shim, spawn_canonical)

    def test_shim_exports_result_class(self):
        from provider_spawns.local_gemma_spawn import LocalGemmaSpawnResult as shim_cls
        from providers.local_gemma.spawn import LocalGemmaSpawnResult as canonical_cls

        self.assertIs(shim_cls, canonical_cls)


if __name__ == "__main__":
    unittest.main()
