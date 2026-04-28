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
