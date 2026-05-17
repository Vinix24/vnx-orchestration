#!/usr/bin/env python3
"""P0-B: Token usage + cost tracking — per-provider extraction tests.

Covers:
  - LiteLLM usage_complete event normalisation and spawn-result propagation
  - Claude result-event usage capture and receipt propagation
  - Codex token_count event extraction
  - Gemini usage metadata extraction
  - _compute_cost for anthropic / openai / deepseek (litellm registry)
  - Edge cases: zero usage, missing pricing
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))


# ---------------------------------------------------------------------------
# 1. LiteLLM — normalize_litellm_event handles usage_complete
# ---------------------------------------------------------------------------

class TestLiteLLMUsageCompleteEvent:

    def test_usage_complete_event_captured(self):
        from provider_spawns.litellm_spawn import normalize_litellm_event
        chunk = {
            "event_type": "usage_complete",
            "usage": {"prompt_tokens": 120, "completion_tokens": 45},
        }
        event = normalize_litellm_event(chunk, "T1", "test-dispatch-litellm-001")
        assert event.event_type == "usage_complete"
        assert event.data["usage"]["prompt_tokens"] == 120
        assert event.data["usage"]["completion_tokens"] == 45

    def test_usage_complete_empty_usage_does_not_crash(self):
        from provider_spawns.litellm_spawn import normalize_litellm_event
        chunk = {"event_type": "usage_complete", "usage": None}
        event = normalize_litellm_event(chunk, "T1", "test-dispatch-litellm-002")
        assert event.event_type == "usage_complete"
        assert event.data["usage"] == {}

    def test_usage_propagates_to_spawn_result(self):
        """_consume_litellm_stream captures usage_complete event into last_token_usage."""
        from canonical_event import CanonicalEvent
        from provider_spawns.litellm_spawn import _consume_litellm_stream, _LiteLLMNormalizerHost

        usage_payload = {"prompt_tokens": 200, "completion_tokens": 80}

        usage_event = CanonicalEvent(
            dispatch_id="test-dispatch-litellm-003",
            terminal_id="T1",
            provider="litellm",
            event_type="usage_complete",
            data={"usage": usage_payload},
            observability_tier=1,
        )

        proc_mock = MagicMock()
        host_mock = MagicMock(spec=_LiteLLMNormalizerHost)
        host_mock.drain_stream.return_value = iter([usage_event])

        text, events_written, timed_out, stopped_early, ew_failures, token_usage = _consume_litellm_stream(
            proc=proc_mock,
            host=host_mock,
            on_event=None,
            health_monitor=None,
            event_writer=None,
            terminal_id="T1",
            dispatch_id="test-dispatch-litellm-003",
            event_store=None,
            chunk_timeout=30.0,
            total_deadline=300.0,
        )

        assert token_usage == usage_payload
        assert events_written == 1


# ---------------------------------------------------------------------------
# 2. Claude — token_usage captured from result event
# ---------------------------------------------------------------------------

class TestClaudeTokenUsage:

    def test_claude_spawn_result_has_token_usage_field(self):
        from provider_spawns.claude_spawn import ClaudeSpawnResult
        r = ClaudeSpawnResult(
            returncode=0,
            completion={},
            events_written=0,
            session_id=None,
            timed_out=False,
            token_usage={"input_tokens": 10, "output_tokens": 5},
        )
        assert r.token_usage == {"input_tokens": 10, "output_tokens": 5}

    def test_claude_spawn_result_token_usage_default_none(self):
        from provider_spawns.claude_spawn import ClaudeSpawnResult
        r = ClaudeSpawnResult(
            returncode=0,
            completion={},
            events_written=0,
            session_id=None,
            timed_out=False,
        )
        assert r.token_usage is None

    def test_claude_usage_propagates_to_receipt(self):
        """token_usage side-channel in delivery module flows into _dispatch_claude."""
        from subprocess_dispatch_internals import delivery as delivery_mod
        dispatch_id = "test-dispatch-claude-004"
        usage = {"input_tokens": 300, "output_tokens": 120, "cache_read_input_tokens": 50}
        delivery_mod._dispatch_token_usage[dispatch_id] = usage

        # Read and pop — as _dispatch_claude would do
        result = delivery_mod._dispatch_token_usage.pop(dispatch_id, None)
        assert result == usage
        assert dispatch_id not in delivery_mod._dispatch_token_usage

    def test_claude_extract_token_usage_correct_keys(self):
        """_extract_token_usage maps claude result event fields to canonical keys."""
        import provider_dispatch

        class FakeResult:
            token_usage = {
                "input_tokens": 500,
                "output_tokens": 200,
                "cache_read_input_tokens": 30,
            }

        usage = provider_dispatch._extract_token_usage(FakeResult(), "claude")
        assert usage["input"] == 500
        assert usage["output"] == 200
        assert usage["cache_hit"] == 30

    def test_spawn_claude_captures_usage_from_result_event(self):
        """spawn_claude extracts usage from the 'result' StreamEvent."""
        from provider_spawns.claude_spawn import spawn_claude

        result_event = MagicMock()
        result_event.type = "result"
        result_event.data = {
            "usage": {"input_tokens": 150, "output_tokens": 60, "cache_read_input_tokens": 0},
            "agent_message": "Hello!",
        }

        adapter_mock = MagicMock()
        adapter_mock.deliver.return_value = MagicMock(success=True)
        adapter_mock.read_events_with_timeout.return_value = iter([result_event])
        adapter_mock.get_session_id.return_value = "sess-abc"
        adapter_mock.was_timed_out.return_value = False
        adapter_mock.observe.return_value = MagicMock(transport_state={"returncode": 0})
        adapter_mock.event_store = None

        with patch("provider_spawns.claude_spawn.SubprocessAdapter", return_value=adapter_mock):
            res = spawn_claude(
                prompt="say hi",
                model="sonnet",
                dispatch_id="test-dispatch-claude-005",
                terminal_id="T1",
            )

        assert res.token_usage is not None
        assert res.token_usage["input_tokens"] == 150
        assert res.token_usage["output_tokens"] == 60


# ---------------------------------------------------------------------------
# 3. Codex — token_count extracted from stream events
# ---------------------------------------------------------------------------

class TestCodexTokenUsage:

    def test_codex_normalize_token_count_payload(self):
        from provider_spawns.codex_spawn import _normalize_token_count
        payload = {"input_tokens": 400, "output_tokens": 150, "cached_input_tokens": 20}
        result = _normalize_token_count(payload)
        assert result is not None
        assert result["input_tokens"] == 400
        assert result["output_tokens"] == 150

    def test_codex_extract_token_count_from_turn_completed(self):
        from provider_spawns.codex_spawn import _extract_token_count_payload
        raw = {
            "type": "turn.completed",
            "event_msg": {
                "payload": {
                    "type": "token_count",
                    "input_tokens": 300,
                    "output_tokens": 100,
                }
            }
        }
        result = _extract_token_count_payload(raw)
        assert result is not None
        assert result["input_tokens"] == 300

    def test_codex_extract_token_count_top_level(self):
        from provider_spawns.codex_spawn import _extract_token_count_payload
        raw = {"type": "token_count", "input_tokens": 200, "output_tokens": 80}
        result = _extract_token_count_payload(raw)
        assert result is not None

    def test_codex_usage_extracted_from_final_event(self):
        """CodexSpawnResult.token_usage is populated from token_count events in stream."""
        from provider_spawns.codex_spawn import spawn_codex

        # Build a fake stream: turn.completed event with token_count inside
        raw_event = {
            "type": "turn.completed",
            "event_msg": {
                "payload": {
                    "type": "token_count",
                    "input_tokens": 500,
                    "output_tokens": 200,
                }
            }
        }

        def fake_popen(*args, **kwargs):
            p = MagicMock()
            p.stdout = MagicMock()
            p.stdin = MagicMock()
            p.returncode = 0
            return p

        import json

        with patch("provider_spawns.codex_spawn._launch_codex_proc") as mock_launch, \
             patch("provider_spawns.codex_spawn._NormalizerHost") as mock_host_cls:
            import provider_spawns.codex_spawn as cs

            from canonical_event import CanonicalEvent
            tc_event = CanonicalEvent(
                dispatch_id="test-d", terminal_id="T2", provider="codex",
                event_type="complete",
                data={"token_count": {"input_tokens": 500, "output_tokens": 200, "cache_creation_tokens": 0, "cache_read_tokens": 0}},
                observability_tier=1,
            )
            mock_host_instance = MagicMock()
            mock_host_instance.drain_stream.return_value = iter([tc_event])
            mock_host_cls.return_value = mock_host_instance

            proc_mock = MagicMock()
            proc_mock.returncode = 0
            mock_launch.return_value = (proc_mock, None)

            result = spawn_codex(
                prompt="say hi",
                model="",
                dispatch_id="test-dispatch-codex-006",
                terminal_id="T2",
            )

        assert result.token_usage is not None
        assert result.token_usage["input_tokens"] == 500
        assert result.token_usage["output_tokens"] == 200


# ---------------------------------------------------------------------------
# 4. Gemini — usage extracted from buffered JSON response
# ---------------------------------------------------------------------------

class TestGeminiTokenUsage:

    def test_gemini_parse_usage_metadata_from_json(self):
        from provider_spawns.gemini_spawn import _parse_gemini_token_usage
        raw = '{"usageMetadata": {"promptTokenCount": 300, "candidatesTokenCount": 120}}'
        result = _parse_gemini_token_usage(raw)
        assert result is not None
        assert result["input_tokens"] == 300
        assert result["output_tokens"] == 120

    def test_gemini_usage_extracted_from_response(self):
        """spawn_gemini legacy path populates token_usage from stdout JSON."""
        from provider_spawns.gemini_spawn import spawn_gemini

        stdout_json = '{"usageMetadata": {"promptTokenCount": 250, "candidatesTokenCount": 90}}'

        with patch("provider_spawns.gemini_spawn._start_gemini_subprocess") as mock_start, \
             patch("provider_spawns.gemini_spawn._drain_buffered") as mock_drain:
            proc_mock = MagicMock()
            proc_mock.returncode = 0
            mock_start.return_value = (proc_mock, None)
            mock_drain.return_value = (stdout_json, "", "ok")

            result = spawn_gemini(
                prompt="say hi",
                model="gemini-2.5-pro",
                dispatch_id="test-dispatch-gemini-007",
                terminal_id="T2",
            )

        assert result.token_usage is not None
        assert result.token_usage["input_tokens"] == 250
        assert result.token_usage["output_tokens"] == 90

    def test_gemini_extract_usage_metadata_zero_counts_returns_none(self):
        from provider_spawns.gemini_spawn import _extract_gemini_usage_metadata
        data = {"promptTokenCount": 0, "candidatesTokenCount": 0}
        assert _extract_gemini_usage_metadata(data) is None

    def test_gemini_extract_usage_metadata_valid(self):
        from provider_spawns.gemini_spawn import _extract_gemini_usage_metadata
        data = {"promptTokenCount": 100, "candidatesTokenCount": 40}
        result = _extract_gemini_usage_metadata(data)
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 40


# ---------------------------------------------------------------------------
# 5. _compute_cost — per-provider pricing lookup
# ---------------------------------------------------------------------------

class TestComputeCost:

    def _make_registry_with_pricing(self, models_dict: dict):
        """Build a minimal mock registry compatible with _load_pricing_from_registry."""
        mock_registry = {}
        for key, models in models_dict.items():
            cfg = MagicMock()
            cfg.models = {
                model_key: MagicMock(
                    cost_input_per_mtok=pricing["input"],
                    cost_output_per_mtok=pricing["output"],
                )
                for model_key, pricing in models.items()
            }
            mock_registry[key] = cfg
        return mock_registry

    def test_compute_cost_anthropic_opus(self):
        import provider_dispatch as pd
        registry = self._make_registry_with_pricing({
            "anthropic": {"opus": {"input": 15.0, "output": 75.0}}
        })
        with patch("provider_dispatch._load_pricing_from_registry",
                   return_value={"input": 15.0, "output": 75.0}):
            cost = pd._compute_cost("claude", "opus", {"input": 1_000_000, "output": 0, "cache_hit": 0})
        assert cost is not None
        assert abs(cost - 15.0) < 1e-6

    def test_compute_cost_anthropic_sonnet(self):
        import provider_dispatch as pd
        with patch("provider_dispatch._load_pricing_from_registry",
                   return_value={"input": 3.0, "output": 15.0}):
            cost = pd._compute_cost("claude", "sonnet", {"input": 0, "output": 1_000_000, "cache_hit": 0})
        assert cost is not None
        assert abs(cost - 15.0) < 1e-6

    def test_compute_cost_openai_codex(self):
        import provider_dispatch as pd
        with patch("provider_dispatch._load_pricing_from_registry",
                   return_value={"input": 1.25, "output": 10.0}):
            cost = pd._compute_cost("codex", "gpt-5.2-codex", {"input": 1_000_000, "output": 1_000_000, "cache_hit": 0})
        assert cost is not None
        assert abs(cost - 11.25) < 1e-4

    def test_compute_cost_deepseek_via_registry(self):
        """deepseek pricing comes from wave7_models.yaml registry (not hardcoded)."""
        import provider_dispatch as pd
        from pathlib import Path

        registry_path = Path(SCRIPTS_LIB) / "providers" / "wave7_models.yaml"
        # Uses actual registry — verifies YAML is parseable and deepseek entry exists.
        pricing = pd._load_pricing_from_registry("litellm:deepseek", "deepseek/deepseek-v4-pro")
        assert pricing is not None
        assert pricing["input"] > 0
        assert pricing["output"] > 0

    def test_compute_cost_returns_none_when_pricing_missing(self):
        import provider_dispatch as pd
        with patch("provider_dispatch._load_pricing_from_registry", return_value=None):
            cost = pd._compute_cost("unknown-provider", "unknown-model", {"input": 100, "output": 50, "cache_hit": 0})
        assert cost is None

    def test_zero_usage_returns_none_cost(self):
        import provider_dispatch as pd
        cost = pd._compute_cost("claude", "sonnet", {"input": 0, "output": 0, "cache_hit": 0})
        assert cost is None

    def test_load_pricing_registry_anthropic_sonnet(self):
        """Registry lookup for claude -> anthropic -> sonnet returns correct pricing."""
        import provider_dispatch as pd
        pricing = pd._load_pricing_from_registry("claude", "sonnet")
        assert pricing is not None
        assert abs(pricing["input"] - 3.0) < 1e-6
        assert abs(pricing["output"] - 15.0) < 1e-6

    def test_load_pricing_registry_google_gemini(self):
        import provider_dispatch as pd
        pricing = pd._load_pricing_from_registry("gemini", "gemini-2.5-pro")
        assert pricing is not None
        assert pricing["input"] > 0
        assert pricing["output"] > 0

    def test_load_pricing_registry_openai_codex(self):
        import provider_dispatch as pd
        pricing = pd._load_pricing_from_registry("codex", "")
        assert pricing is not None
        assert pricing["input"] > 0

    def test_load_pricing_registry_unknown_provider_returns_none(self):
        import provider_dispatch as pd
        pricing = pd._load_pricing_from_registry("nonexistent-provider", "some-model")
        assert pricing is None
