"""Tests verifying backward-compat import paths still work after Smart Lanes PR-1.

Old wrapper imports must continue to work without breaking changes.
New package paths must also resolve to the same objects.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


class TestCodexWrapperBackwardCompat(unittest.TestCase):
    def test_old_codex_wrapper_import(self):
        from codex_wrapper import codex_exec, DEFAULT_CODEX_MODEL
        self.assertIsNotNone(codex_exec)
        self.assertIsInstance(DEFAULT_CODEX_MODEL, str)

    def test_new_provider_lanes_codex_import(self):
        from providers.provider_lanes.codex import codex_exec as new_exec, DEFAULT_CODEX_MODEL as new_model
        self.assertIsNotNone(new_exec)
        self.assertIsInstance(new_model, str)

    def test_old_and_new_same_object(self):
        from codex_wrapper import codex_exec as old
        from providers.provider_lanes.codex import codex_exec as new
        # Both import the same function from the same canonical module
        self.assertIs(old, new)


class TestKimiWrapperBackwardCompat(unittest.TestCase):
    def test_old_kimi_wrapper_import(self):
        from kimi_wrapper import kimi_exec, DEFAULT_KIMI_MODEL
        self.assertIsNotNone(kimi_exec)
        self.assertIsInstance(DEFAULT_KIMI_MODEL, str)

    def test_new_provider_lanes_kimi_import(self):
        from providers.provider_lanes.kimi import kimi_exec as new_exec
        self.assertIsNotNone(new_exec)

    def test_old_and_new_same_object(self):
        from kimi_wrapper import kimi_exec as old
        from providers.provider_lanes.kimi import kimi_exec as new
        self.assertIs(old, new)


class TestGeminiWrapperBackwardCompat(unittest.TestCase):
    def test_old_gemini_wrapper_import(self):
        from gemini_wrapper import gemini_exec, DEFAULT_GEMINI_MODEL
        self.assertIsNotNone(gemini_exec)
        self.assertIsInstance(DEFAULT_GEMINI_MODEL, str)

    def test_new_provider_lanes_gemini_import(self):
        from providers.provider_lanes.gemini import gemini_exec as new_exec
        self.assertIsNotNone(new_exec)

    def test_old_and_new_same_object(self):
        from gemini_wrapper import gemini_exec as old
        from providers.provider_lanes.gemini import gemini_exec as new
        self.assertIs(old, new)


class TestSmartRouterBackwardCompat(unittest.TestCase):
    def test_old_smart_router_import(self):
        from smart_router import classify_task, decide, recommend, parse_route_model_id
        self.assertIsNotNone(classify_task)
        self.assertIsNotNone(decide)
        self.assertIsNotNone(recommend)
        self.assertIsNotNone(parse_route_model_id)

    def test_new_providers_smart_router_import(self):
        from providers.smart_router.classifier import (
            classify_task as new_classify,
            decide as new_decide,
        )
        self.assertIsNotNone(new_classify)
        self.assertIsNotNone(new_decide)

    def test_old_and_new_same_functions(self):
        from smart_router import classify_task as old_cls, decide as old_decide
        from providers.smart_router.classifier import classify_task as new_cls, decide as new_decide
        self.assertIs(old_cls, new_cls)
        self.assertIs(old_decide, new_decide)

    def test_providers_smart_router_init_import(self):
        from providers.smart_router import classify_task, decide
        self.assertIsNotNone(classify_task)
        self.assertIsNotNone(decide)


class TestLocalGemmaNewImport(unittest.TestCase):
    def test_providers_local_gemma_import(self):
        from providers.local_gemma import spawn_local_gemma, LocalGemmaSpawnResult
        self.assertIsNotNone(spawn_local_gemma)
        self.assertIsNotNone(LocalGemmaSpawnResult)

    def test_provider_spawns_shim_import(self):
        from provider_spawns.local_gemma_spawn import spawn_local_gemma as shim
        self.assertIsNotNone(shim)

    def test_shim_and_canonical_same(self):
        from providers.local_gemma.spawn import spawn_local_gemma as canonical
        from provider_spawns.local_gemma_spawn import spawn_local_gemma as shim
        self.assertIs(canonical, shim)


if __name__ == "__main__":
    unittest.main()
