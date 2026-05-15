#!/usr/bin/env python3
"""Wave 7 PR-7.5 — provider_dispatch contract integration tests.

Covers:
- _build_lane_key constructs correct lane keys for all known sub-providers.
- Dispatching to deepseek lane passes cache_control_supported=False (via contract).
- spawn_litellm receives lane + tool_call_shape from contract.
- Mismatched tool shape triggers structured error (not raised exception).
- normalize_litellm_event enriches CanonicalEvent with sub_provider + lane.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch
from provider_dispatch import _build_lane_key
from providers.behavior_contracts import get_contract
from provider_spawns.litellm_spawn import (
    LiteLLMSpawnResult,
    _validate_tool_shape,
    normalize_litellm_event,
    spawn_litellm,
)


# ---------------------------------------------------------------------------
# _build_lane_key
# ---------------------------------------------------------------------------

class TestBuildLaneKey:
    def test_deepseek_default_alias(self):
        assert _build_lane_key("deepseek", None) == "litellm:deepseek:deepseek-v4-pro"

    def test_deepseek_explicit_alias(self):
        assert _build_lane_key("deepseek", "deepseek-v4-pro") == "litellm:deepseek:deepseek-v4-pro"

    def test_moonshot_default_alias(self):
        assert _build_lane_key("moonshot", None) == "litellm:moonshot:kimi-k2-0905-default"

    def test_moonshot_kimi_k2_6(self):
        assert _build_lane_key("moonshot", "kimi-k2-6") == "litellm:moonshot:kimi-k2-6"

    def test_zai_default_alias(self):
        assert _build_lane_key("zai", None) == "litellm:zai:glm-5.1-default"

    def test_unknown_sub_provider_uses_default_sentinel(self):
        result = _build_lane_key("bedrock", None)
        assert result == "litellm:bedrock:default"


# ---------------------------------------------------------------------------
# Dispatch disables cache_control for deepseek
# ---------------------------------------------------------------------------

class TestDispatchDisablesCacheControlForDeepSeek:
    def test_deepseek_contract_says_no_cache_control(self):
        """The contract for the deepseek lane explicitly marks cache_control=False."""
        contract = get_contract("litellm:deepseek:deepseek-v4-pro")
        assert contract.cache_control_supported is False

    def test_dispatch_litellm_passes_lane_to_spawn(self):
        """_dispatch_litellm passes lane= to spawn_litellm when deepseek is selected."""
        captured_kwargs: dict = {}

        def fake_spawn(**kwargs):
            captured_kwargs.update(kwargs)
            return LiteLLMSpawnResult(
                returncode=0, completion_text="ok", events_written=1,
                session_id=None, timed_out=False,
            )

        argv = [
            "--provider", "litellm:deepseek",
            "--terminal-id", "T1",
            "--dispatch-id", "test-contract-01",
            "--instruction", "echo hello",
        ]

        # spawn_litellm is a local import inside _dispatch_litellm — patch at module level
        with patch("provider_spawns.litellm_spawn.spawn_litellm", fake_spawn), \
             patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
            provider_dispatch.main(argv)

        assert "lane" in captured_kwargs, "spawn_litellm was not called with lane= kwarg"
        assert captured_kwargs["lane"] == "litellm:deepseek:deepseek-v4-pro"

    def test_dispatch_litellm_passes_tool_shape_to_spawn(self):
        """_dispatch_litellm passes tool_call_shape from contract to spawn_litellm."""
        captured_kwargs: dict = {}

        def fake_spawn(**kwargs):
            captured_kwargs.update(kwargs)
            return LiteLLMSpawnResult(
                returncode=0, completion_text="ok", events_written=1,
                session_id=None, timed_out=False,
            )

        argv = [
            "--provider", "litellm:deepseek",
            "--terminal-id", "T1",
            "--dispatch-id", "test-contract-02",
            "--instruction", "echo hello",
        ]

        # spawn_litellm is a local import inside _dispatch_litellm — patch at module level
        with patch("provider_spawns.litellm_spawn.spawn_litellm", fake_spawn), \
             patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
            provider_dispatch.main(argv)

        assert captured_kwargs.get("tool_call_shape") == "openai_tools"


# ---------------------------------------------------------------------------
# Tool shape validation
# ---------------------------------------------------------------------------

class TestDispatchValidatesToolShape:
    def test_validate_tool_shape_noop_when_no_shape(self):
        """No-op when tool_call_shape is None (contract not found path)."""
        _validate_tool_shape("any prompt content", None)  # must not raise

    def test_validate_tool_shape_noop_for_anthropic_tools(self):
        """anthropic_tools shape does not reject any prompt content."""
        prompt_with_input_schema = 'tool: {"input_schema": {"type": "object"}}'
        _validate_tool_shape(prompt_with_input_schema, "anthropic_tools")  # must not raise

    def test_validate_tool_shape_raises_for_openai_lane_with_anthropic_markers(self):
        """Prompt with Anthropic tool markers rejected for openai_tools lanes."""
        bad_prompt = 'Use this tool: {"input_schema": {"type": "object", "properties": {}}}'
        with pytest.raises(ValueError, match="input_schema"):
            _validate_tool_shape(bad_prompt, "openai_tools")

    def test_validate_tool_shape_allows_clean_prompt_for_openai_lane(self):
        """Normal prompt without tool markers passes openai_tools validation."""
        clean_prompt = "Refactor the authentication module and add tests."
        _validate_tool_shape(clean_prompt, "openai_tools")  # must not raise

    def test_spawn_litellm_returns_structured_error_on_shape_mismatch(self):
        """spawn_litellm returns error result (not raised exception) on tool shape mismatch."""
        bad_prompt = 'call tool with {"input_schema": {"type": "object"}}'
        result = spawn_litellm(
            prompt=bad_prompt,
            model="deepseek/deepseek-v3.2",
            dispatch_id="test-shape-mismatch",
            terminal_id="T1",
            tool_call_shape="openai_tools",
        )
        assert result.error is not None
        assert "input_schema" in result.error
        assert result.returncode == 64


# ---------------------------------------------------------------------------
# normalize_litellm_event audit enrichment
# ---------------------------------------------------------------------------

class TestNormalizeLiteLLMEventAuditEnrichment:
    def _text_chunk(self) -> dict:
        return {
            "choices": [{"delta": {"content": "hello"}, "finish_reason": None}]
        }

    def test_sub_provider_propagated_to_canonical_event(self):
        event = normalize_litellm_event(
            self._text_chunk(), "T1", "dispatch-001",
            sub_provider="deepseek",
        )
        assert event.sub_provider == "deepseek"

    def test_lane_propagated_to_provider_meta(self):
        event = normalize_litellm_event(
            self._text_chunk(), "T1", "dispatch-001",
            lane="litellm:deepseek:deepseek-v4-pro",
        )
        assert event.provider_meta.get("lane") == "litellm:deepseek:deepseek-v4-pro"

    def test_both_enrichment_fields_set(self):
        event = normalize_litellm_event(
            self._text_chunk(), "T1", "dispatch-001",
            sub_provider="moonshot",
            lane="litellm:moonshot:kimi-k2-0905-default",
        )
        assert event.sub_provider == "moonshot"
        assert event.provider_meta.get("lane") == "litellm:moonshot:kimi-k2-0905-default"

    def test_no_enrichment_when_not_provided(self):
        """Existing callers without sub_provider/lane get None/empty — backward compat."""
        event = normalize_litellm_event(self._text_chunk(), "T1", "dispatch-001")
        assert event.sub_provider is None
        assert event.provider_meta == {}

    def test_provider_is_always_litellm(self):
        event = normalize_litellm_event(
            self._text_chunk(), "T1", "dispatch-001",
            sub_provider="zai", lane="litellm:zai:glm-5.1-default",
        )
        assert event.provider == "litellm"
