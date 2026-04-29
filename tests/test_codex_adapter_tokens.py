#!/usr/bin/env python3
"""Unit tests for CodexAdapter token usage parsing logic."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB_DIR = Path(__file__).parent.parent / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(LIB_DIR / "adapters"))

from adapters.codex_adapter import CodexAdapter


class TestParseTokenUsageTextFormat:
    """Parse 'Tokens: N input / M output' text lines."""

    def test_basic_text_format(self):
        raw = "Tokens: 1200 input / 350 output"
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result == {
            "input_tokens": 1200,
            "output_tokens": 350,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }

    def test_text_format_with_total(self):
        raw = "Tokens: 800 input / 200 output / 1000 total"
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 800
        assert result["output_tokens"] == 200

    def test_text_format_case_insensitive(self):
        raw = "TOKENS: 500 INPUT / 100 OUTPUT"
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 500
        assert result["output_tokens"] == 100

    def test_text_format_embedded_in_multiline(self):
        raw = "Starting review...\nTokens: 999 input / 111 output\nDone."
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 999
        assert result["output_tokens"] == 111

    def test_zero_cache_fields(self):
        raw = "Tokens: 100 input / 50 output"
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result["cache_creation_tokens"] == 0
        assert result["cache_read_tokens"] == 0


class TestParseTokenUsageJsonEventFormat:
    """Parse {"type":"token_usage","input_tokens":N,"output_tokens":M} NDJSON events."""

    def test_explicit_token_usage_event(self):
        raw = json.dumps({"type": "token_usage", "input_tokens": 1200, "output_tokens": 350})
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result == {
            "input_tokens": 1200,
            "output_tokens": 350,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }

    def test_token_usage_event_in_ndjson_stream(self):
        events = [
            {"type": "message", "content": "Analyzing..."},
            {"type": "token_usage", "input_tokens": 2000, "output_tokens": 500},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 2000
        assert result["output_tokens"] == 500


class TestParseTokenUsageOpenAIFormat:
    """Parse OpenAI-compatible {"usage":{"prompt_tokens":N,"completion_tokens":M}} blocks."""

    def test_openai_prompt_completion_tokens(self):
        raw = json.dumps({
            "id": "chatcmpl-abc",
            "usage": {"prompt_tokens": 300, "completion_tokens": 150},
        })
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 300
        assert result["output_tokens"] == 150

    def test_openai_input_output_tokens(self):
        raw = json.dumps({
            "usage": {"input_tokens": 400, "output_tokens": 200},
        })
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 400
        assert result["output_tokens"] == 200

    def test_usage_block_in_ndjson_stream(self):
        events = [
            {"type": "start"},
            {"type": "result", "content": "ok", "usage": {"prompt_tokens": 100, "completion_tokens": 40}},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 40


class TestParseTokenUsageMissingInfo:
    """Returns None when no token info is present."""

    def test_empty_string_returns_none(self):
        assert CodexAdapter._parse_token_usage_from_output("") is None

    def test_plain_text_no_tokens_returns_none(self):
        raw = "All findings look good. No issues detected."
        assert CodexAdapter._parse_token_usage_from_output(raw) is None

    def test_json_without_usage_returns_none(self):
        raw = json.dumps({"type": "message", "content": "hello"})
        assert CodexAdapter._parse_token_usage_from_output(raw) is None

    def test_malformed_json_lines_skipped(self):
        raw = "not-json\n{broken\nTokens: 77 input / 33 output"
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 77


class TestGetTokenUsageStateCache:
    """get_token_usage reads from the per-terminal state cache file."""

    def test_reads_cached_usage(self, tmp_path: Path):
        cache_dir = tmp_path / "token_cache"
        cache_dir.mkdir()
        usage = {"input_tokens": 500, "output_tokens": 120, "cache_creation_tokens": 0, "cache_read_tokens": 0}
        (cache_dir / "T3_usage.json").write_text(json.dumps(usage))
        result = CodexAdapter.get_token_usage("T3", state_dir=tmp_path)
        assert result == usage

    def test_returns_none_when_cache_missing(self, tmp_path: Path):
        assert CodexAdapter.get_token_usage("T3", state_dir=tmp_path) is None

    def test_returns_none_on_malformed_cache(self, tmp_path: Path):
        cache_dir = tmp_path / "token_cache"
        cache_dir.mkdir()
        (cache_dir / "T1_usage.json").write_text("not-json")
        assert CodexAdapter.get_token_usage("T1", state_dir=tmp_path) is None

    def test_returns_none_when_state_dir_missing(self):
        assert CodexAdapter.get_token_usage("T1", state_dir=None) is None

    def test_write_then_read_roundtrip(self, tmp_path: Path):
        adapter = CodexAdapter("T2")
        usage = {"input_tokens": 300, "output_tokens": 80, "cache_creation_tokens": 0, "cache_read_tokens": 0}
        adapter._write_token_cache(usage, state_dir=tmp_path)
        result = CodexAdapter.get_token_usage("T2", state_dir=tmp_path)
        assert result == usage


# ── Round-2 codex regate (PR #307): parse `event_msg.payload.type=='token_count'` ──


class TestParseTokenCountWrappedEvent:
    """Round-2 fix: real `codex exec --json` emits token_count under event_msg.payload."""

    def test_event_msg_payload_token_count(self):
        """Primary shape flagged by codex regate: event_msg.payload.type=='token_count'."""
        event = {
            "id": "evt-1",
            "event_msg": {
                "payload": {
                    "type": "token_count",
                    "input_tokens": 1500,
                    "cached_input_tokens": 200,
                    "output_tokens": 400,
                    "reasoning_output_tokens": 50,
                    "total_tokens": 2150,
                }
            },
        }
        raw = json.dumps(event)
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None, (
            "Round-2 regression: event_msg.payload.type=='token_count' must be parsed. "
            "Pre-fix _parse_token_usage_from_output returned None for this format."
        )
        assert result["input_tokens"] == 1500
        assert result["output_tokens"] == 400
        assert result["cache_read_tokens"] == 200

    def test_event_msg_token_count_directly_under_event_msg(self):
        """Variant: event_msg.type=='token_count' (no payload wrapper)."""
        event = {
            "event_msg": {
                "type": "token_count",
                "input_tokens": 800,
                "output_tokens": 300,
            }
        }
        result = CodexAdapter._parse_token_usage_from_output(json.dumps(event))
        assert result is not None
        assert result["input_tokens"] == 800
        assert result["output_tokens"] == 300

    def test_msg_wrapped_token_count(self):
        """Variant: msg.type=='token_count' (older Codex shape)."""
        event = {"id": "abc", "msg": {"type": "token_count", "input_tokens": 600, "output_tokens": 200}}
        result = CodexAdapter._parse_token_usage_from_output(json.dumps(event))
        assert result is not None
        assert result["input_tokens"] == 600
        assert result["output_tokens"] == 200

    def test_token_count_takes_last_event(self):
        """Codex emits running totals — keep the LAST token_count seen."""
        events = [
            {"event_msg": {"payload": {"type": "token_count",
                                        "input_tokens": 100, "output_tokens": 20}}},
            {"event_msg": {"payload": {"type": "token_count",
                                        "input_tokens": 250, "output_tokens": 60}}},
            {"event_msg": {"payload": {"type": "token_count",
                                        "input_tokens": 412, "output_tokens": 91}}},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 412, (
            "Codex emits running totals; final token_count event is the authoritative usage."
        )
        assert result["output_tokens"] == 91

    def test_token_count_inside_realistic_ndjson_stream(self):
        """End-to-end: a realistic NDJSON stream from `codex exec` with mixed events."""
        events = [
            {"event_msg": {"payload": {"type": "session_start"}}},
            {"event_msg": {"payload": {"type": "agent_message", "text": "Reviewing files..."}}},
            {"event_msg": {"payload": {
                "type": "token_count",
                "input_tokens": 2048,
                "cached_input_tokens": 512,
                "output_tokens": 600,
                "cache_creation_input_tokens": 0,
            }}},
            {"event_msg": {"payload": {"type": "agent_message", "text": "Done."}}},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        result = CodexAdapter._parse_token_usage_from_output(raw)
        assert result is not None
        assert result["input_tokens"] == 2048
        assert result["output_tokens"] == 600
        assert result["cache_read_tokens"] == 512

    def test_token_count_with_only_prompt_completion_keys(self):
        """OpenAI-style key names inside a token_count payload are accepted too."""
        event = {"event_msg": {"payload": {
            "type": "token_count",
            "prompt_tokens": 700,
            "completion_tokens": 220,
        }}}
        result = CodexAdapter._parse_token_usage_from_output(json.dumps(event))
        assert result is not None
        assert result["input_tokens"] == 700
        assert result["output_tokens"] == 220

    def test_zero_token_count_returns_none(self):
        """A token_count with both fields zero is not useful — return None so we don't
        clobber a real usage value already cached."""
        event = {"event_msg": {"payload": {
            "type": "token_count",
            "input_tokens": 0,
            "output_tokens": 0,
        }}}
        result = CodexAdapter._parse_token_usage_from_output(json.dumps(event))
        assert result is None


class TestExtractTokenCountPayloadHelper:
    """Direct unit tests for the wrapper-extraction helper."""

    def test_extract_event_msg_payload(self):
        event = {"event_msg": {"payload": {"type": "token_count", "input_tokens": 1}}}
        payload = CodexAdapter._extract_token_count_payload(event)
        assert payload == {"type": "token_count", "input_tokens": 1}

    def test_extract_msg_wrapper(self):
        event = {"msg": {"type": "token_count", "x": 1}}
        assert CodexAdapter._extract_token_count_payload(event) == {"type": "token_count", "x": 1}

    def test_extract_top_level(self):
        event = {"type": "token_count", "input_tokens": 5}
        assert CodexAdapter._extract_token_count_payload(event) == event

    def test_extract_returns_none_for_other_types(self):
        assert CodexAdapter._extract_token_count_payload(
            {"event_msg": {"payload": {"type": "agent_message"}}}
        ) is None
        assert CodexAdapter._extract_token_count_payload({"msg": {"type": "session_start"}}) is None
        assert CodexAdapter._extract_token_count_payload({"unrelated": 1}) is None
        assert CodexAdapter._extract_token_count_payload(None) is None  # type: ignore[arg-type]
