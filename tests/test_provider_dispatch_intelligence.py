#!/usr/bin/env python3
"""Integration tests for intelligence injection across provider_dispatch.py handlers (P0-A).

Verifies that codex/gemini/litellm dispatch handlers call _enrich_instruction
before spawn, and that the enriched prompt reaches the spawn function.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import provider_dispatch
import intelligence_injection


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------

@dataclass
class _SpawnResult:
    returncode: int = 0
    completion_text: str = "OK"
    events_written: int = 0
    session_id: Optional[str] = None
    timed_out: bool = False
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    event_writer_failures: int = 0


def _make_args(provider: str, instruction: str = "base instruction") -> MagicMock:
    args = MagicMock()
    args.provider = provider
    args.dispatch_id = "d-integ-intel-001"
    args.terminal_id = "T1"
    args.instruction = instruction
    args.model = "sonnet"
    args.pr_id = None
    args.dispatch_paths = ""
    args.role = "backend-developer"
    args.no_auto_commit = True
    args.max_retries = 1
    args.gate = ""
    return args


def _make_intelligence_item():
    from intelligence_selector import IntelligenceItem
    return IntelligenceItem(
        item_id="intel-test",
        item_class="failure_prevention",
        title="TestAntipattern",
        content="always test before push",
        confidence=0.9,
        evidence_count=3,
        last_seen="2026-05-17T00:00:00.000000Z",
        scope_tags=["backend-developer"],
    )


def _make_result_with_item():
    from intelligence_selector import InjectionResult
    return InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-05-17T00:00:00.000000Z",
        items=[_make_intelligence_item()],
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id="d-integ-intel-001",
    )


def _make_result_empty():
    from intelligence_selector import InjectionResult
    return InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-05-17T00:00:00.000000Z",
        items=[],
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id="d-integ-intel-001",
    )


def _patch_selector(result):
    import intelligence_selector as _mod
    mock_cls = MagicMock()
    instance = MagicMock()
    instance.select.return_value = result
    mock_cls.return_value = instance
    return patch.object(_mod, "IntelligenceSelector", mock_cls)


def _patch_governance():
    """Suppress governance emit so tests don't need real state dirs."""
    return patch.object(provider_dispatch, "_emit_governance")


# ---------------------------------------------------------------------------
# _enrich_instruction helper tests
# ---------------------------------------------------------------------------

class TestEnrichInstructionHelper:

    def test_returns_enriched_instruction_when_intelligence_available(self, tmp_path):
        args = _make_args("codex")
        result = _make_result_with_item()
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with _patch_selector(result):
                enriched = provider_dispatch._enrich_instruction(args)
        assert "Relevant Intelligence" in enriched
        assert "TestAntipattern" in enriched
        assert "base instruction" in enriched

    def test_returns_original_instruction_when_no_intelligence(self, tmp_path):
        args = _make_args("gemini", instruction="original work")
        result = _make_result_empty()
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with _patch_selector(result):
                enriched = provider_dispatch._enrich_instruction(args)
        assert enriched == "original work"

    def test_returns_original_on_intelligence_injection_import_failure(self):
        args = _make_args("litellm:deepseek")
        with patch.dict(sys.modules, {"intelligence_injection": None}):
            enriched = provider_dispatch._enrich_instruction(args)
        assert enriched == "base instruction"

    def test_dispatch_paths_parsed_from_args(self, tmp_path):
        args = _make_args("codex")
        args.dispatch_paths = "scripts/lib/foo.py,tests/test_foo.py"
        result = _make_result_empty()
        import intelligence_selector as _mod
        mock_cls = MagicMock()
        instance = MagicMock()
        instance.select.return_value = result
        mock_cls.return_value = instance
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with patch.object(_mod, "IntelligenceSelector", mock_cls):
                provider_dispatch._enrich_instruction(args)
        call_kwargs = instance.select.call_args[1]
        assert "scripts/lib/foo.py" in call_kwargs.get("dispatch_paths", [])
        assert "tests/test_foo.py" in call_kwargs.get("dispatch_paths", [])


# ---------------------------------------------------------------------------
# _dispatch_codex intelligence wiring
# ---------------------------------------------------------------------------

class TestCodexIntelligenceWiring:

    def test_codex_spawn_receives_enriched_prompt(self, tmp_path):
        args = _make_args("codex")
        result = _make_result_with_item()
        spawn_result = _SpawnResult()
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with _patch_selector(result):
                with patch("provider_spawns.codex_spawn.spawn_codex", return_value=spawn_result) as mock_spawn:
                    with _patch_governance():
                        with patch("event_store.EventStore"):
                            provider_dispatch._dispatch_codex(args)
        prompt_used = mock_spawn.call_args[1].get("prompt") or mock_spawn.call_args[0][0]
        assert "TestAntipattern" in prompt_used
        assert "base instruction" in prompt_used

    def test_codex_spawn_receives_original_when_no_intelligence(self, tmp_path):
        args = _make_args("codex", instruction="plain codex task")
        result = _make_result_empty()
        spawn_result = _SpawnResult()
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with _patch_selector(result):
                with patch("provider_spawns.codex_spawn.spawn_codex", return_value=spawn_result) as mock_spawn:
                    with _patch_governance():
                        with patch("event_store.EventStore"):
                            provider_dispatch._dispatch_codex(args)
        prompt_used = mock_spawn.call_args[1].get("prompt") or mock_spawn.call_args[0][0]
        assert prompt_used == "plain codex task"


# ---------------------------------------------------------------------------
# _dispatch_gemini intelligence wiring
# ---------------------------------------------------------------------------

class TestGeminiIntelligenceWiring:

    def test_gemini_spawn_receives_enriched_prompt(self, tmp_path):
        args = _make_args("gemini")
        result = _make_result_with_item()
        spawn_result = _SpawnResult()
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with _patch_selector(result):
                with patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=spawn_result) as mock_spawn:
                    with _patch_governance():
                        with patch("event_store.EventStore"):
                            provider_dispatch._dispatch_gemini(args)
        prompt_used = mock_spawn.call_args[1].get("prompt") or mock_spawn.call_args[0][0]
        assert "TestAntipattern" in prompt_used

    def test_gemini_spawn_receives_original_when_no_intelligence(self, tmp_path):
        args = _make_args("gemini", instruction="plain gemini task")
        result = _make_result_empty()
        spawn_result = _SpawnResult()
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with _patch_selector(result):
                with patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=spawn_result) as mock_spawn:
                    with _patch_governance():
                        with patch("event_store.EventStore"):
                            provider_dispatch._dispatch_gemini(args)
        prompt_used = mock_spawn.call_args[1].get("prompt") or mock_spawn.call_args[0][0]
        assert prompt_used == "plain gemini task"


# ---------------------------------------------------------------------------
# _dispatch_litellm intelligence wiring
# ---------------------------------------------------------------------------

class TestLitellmIntelligenceWiring:

    def _mock_registry(self):
        mock_rec = MagicMock()
        mock_rec.litellm_name = "deepseek/deepseek-v4-pro"
        mock_registry = MagicMock()
        mock_registry.get_default_model.return_value = mock_rec
        return mock_registry

    def test_litellm_spawn_receives_enriched_prompt(self, tmp_path):
        args = _make_args("litellm:deepseek")
        args.provider = "litellm:deepseek"
        result = _make_result_with_item()
        spawn_result = _SpawnResult()
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with _patch_selector(result):
                with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=spawn_result) as mock_spawn:
                    with _patch_governance():
                        with patch("event_store.EventStore"):
                            with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
                                with patch("providers.provider_registry.get_default_model", return_value=self._mock_registry().get_default_model.return_value):
                                    provider_dispatch._dispatch_litellm(args)
        prompt_used = mock_spawn.call_args[1].get("prompt") or mock_spawn.call_args[0][0]
        assert "TestAntipattern" in prompt_used
        assert "base instruction" in prompt_used

    def test_litellm_spawn_receives_original_when_no_intelligence(self, tmp_path):
        args = _make_args("litellm:deepseek", instruction="plain litellm task")
        args.provider = "litellm:deepseek"
        result = _make_result_empty()
        spawn_result = _SpawnResult()
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with _patch_selector(result):
                with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=spawn_result) as mock_spawn:
                    with _patch_governance():
                        with patch("event_store.EventStore"):
                            with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
                                with patch("providers.provider_registry.get_default_model", return_value=self._mock_registry().get_default_model.return_value):
                                    provider_dispatch._dispatch_litellm(args)
        prompt_used = mock_spawn.call_args[1].get("prompt") or mock_spawn.call_args[0][0]
        assert prompt_used == "plain litellm task"


# ---------------------------------------------------------------------------
# _dispatch_kimi intelligence wiring
# ---------------------------------------------------------------------------

class TestKimiIntelligenceWiring:

    def test_kimi_spawn_receives_enriched_prompt(self, tmp_path):
        args = _make_args("kimi")
        result = _make_result_with_item()
        spawn_result = _SpawnResult()
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with _patch_selector(result):
                with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=spawn_result) as mock_spawn:
                    with _patch_governance():
                        with patch("event_store.EventStore"):
                            provider_dispatch._dispatch_kimi(args)
        prompt_used = mock_spawn.call_args[1].get("prompt") or mock_spawn.call_args[0][0]
        assert "TestAntipattern" in prompt_used
        assert "base instruction" in prompt_used

    def test_kimi_spawn_receives_original_when_no_intelligence(self, tmp_path):
        args = _make_args("kimi", instruction="plain kimi task")
        result = _make_result_empty()
        spawn_result = _SpawnResult()
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with _patch_selector(result):
                with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=spawn_result) as mock_spawn:
                    with _patch_governance():
                        with patch("event_store.EventStore"):
                            provider_dispatch._dispatch_kimi(args)
        prompt_used = mock_spawn.call_args[1].get("prompt") or mock_spawn.call_args[0][0]
        assert prompt_used == "plain kimi task"

    def test_kimi_enrich_called_once_no_double_enrichment(self, tmp_path):
        args = _make_args("kimi")
        result = _make_result_empty()
        spawn_result = _SpawnResult()
        with patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path):
            with _patch_selector(result):
                with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=spawn_result):
                    with _patch_governance():
                        with patch("event_store.EventStore"):
                            with patch.object(provider_dispatch, "_enrich_instruction", wraps=provider_dispatch._enrich_instruction) as mock_enrich:
                                provider_dispatch._dispatch_kimi(args)
        assert mock_enrich.call_count == 1


# ---------------------------------------------------------------------------
# Claude path does NOT get double-injected
# ---------------------------------------------------------------------------

class TestClaudePathNotDoubleInjected:

    def test_enrich_instruction_not_called_in_dispatch_claude(self):
        """_dispatch_claude must NOT call _enrich_instruction (subprocess_dispatch handles it)."""
        args = _make_args("claude")
        with patch.object(provider_dispatch, "_enrich_instruction") as mock_enrich:
            with patch("subprocess_dispatch.deliver_with_recovery", return_value=True):
                with patch("subprocess_dispatch._extract_role_from_instruction", return_value=None):
                    with _patch_governance():
                        provider_dispatch._dispatch_claude(args)
        mock_enrich.assert_not_called()
