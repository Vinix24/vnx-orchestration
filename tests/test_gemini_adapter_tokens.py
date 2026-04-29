#!/usr/bin/env python3
"""Unit tests for GeminiAdapter token usage parsing logic."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB_DIR = Path(__file__).parent.parent / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(LIB_DIR / "adapters"))

from adapters.gemini_adapter import GeminiAdapter


class TestExtractUsageMetadata:
    """_extract_usage_metadata parses promptTokenCount/candidatesTokenCount."""

    def test_basic_usage_metadata(self):
        data = {"usageMetadata": {"promptTokenCount": 400, "candidatesTokenCount": 150}}
        result = GeminiAdapter._extract_usage_metadata(data)
        assert result == {
            "input_tokens": 400,
            "output_tokens": 150,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }

    def test_missing_usage_metadata_returns_none(self):
        assert GeminiAdapter._extract_usage_metadata({"response": "hello"}) is None

    def test_non_dict_usage_metadata_returns_none(self):
        assert GeminiAdapter._extract_usage_metadata({"usageMetadata": "bad"}) is None

    def test_zero_counts_returns_none(self):
        data = {"usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0}}
        assert GeminiAdapter._extract_usage_metadata(data) is None

    def test_partial_counts_accepted(self):
        # prompt=100 with no output is valid data (input-only inference or cached answer)
        data = {"usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 0}}
        result = GeminiAdapter._extract_usage_metadata(data)
        assert result is not None
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 0

    def test_nonzero_candidates_only(self):
        # candidates=50 with no prompt tokens is valid data — return it
        data = {"usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 50}}
        result = GeminiAdapter._extract_usage_metadata(data)
        assert result is not None
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 50

    def test_nonzero_prompt_and_candidates(self):
        data = {"usageMetadata": {"promptTokenCount": 300, "candidatesTokenCount": 75}}
        result = GeminiAdapter._extract_usage_metadata(data)
        assert result is not None
        assert result["input_tokens"] == 300
        assert result["output_tokens"] == 75


class TestParseTokenUsageFromResponse:
    """_parse_token_usage_from_response handles JSON and NDJSON Gemini output."""

    def test_top_level_json_with_usage_metadata(self):
        raw = json.dumps({
            "response": "Here are findings...",
            "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 200},
        })
        result = GeminiAdapter._parse_token_usage_from_response(raw)
        assert result is not None
        assert result["input_tokens"] == 500
        assert result["output_tokens"] == 200

    def test_ndjson_stream_with_usage_metadata(self):
        lines = [
            json.dumps({"type": "chunk", "text": "partial"}),
            json.dumps({"usageMetadata": {"promptTokenCount": 600, "candidatesTokenCount": 180}}),
        ]
        raw = "\n".join(lines)
        result = GeminiAdapter._parse_token_usage_from_response(raw)
        assert result is not None
        assert result["input_tokens"] == 600
        assert result["output_tokens"] == 180

    def test_empty_response_returns_none(self):
        assert GeminiAdapter._parse_token_usage_from_response("") is None

    def test_plain_text_no_metadata_returns_none(self):
        assert GeminiAdapter._parse_token_usage_from_response("Here are the findings.") is None

    def test_json_without_usage_metadata_returns_none(self):
        raw = json.dumps({"response": "no metadata here"})
        assert GeminiAdapter._parse_token_usage_from_response(raw) is None

    def test_malformed_json_top_level_falls_through_to_ndjson(self):
        bad_header = "not-json\n"
        valid_line = json.dumps({"usageMetadata": {"promptTokenCount": 123, "candidatesTokenCount": 45}})
        raw = bad_header + valid_line
        result = GeminiAdapter._parse_token_usage_from_response(raw)
        assert result is not None
        assert result["input_tokens"] == 123

    def test_cache_fields_always_zero(self):
        raw = json.dumps({"usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 30}})
        result = GeminiAdapter._parse_token_usage_from_response(raw)
        assert result["cache_creation_tokens"] == 0
        assert result["cache_read_tokens"] == 0


class TestGetTokenUsageStateCache:
    """get_token_usage reads from the per-terminal state cache file."""

    def test_reads_cached_usage(self, tmp_path: Path):
        cache_dir = tmp_path / "token_cache"
        cache_dir.mkdir()
        usage = {"input_tokens": 700, "output_tokens": 210, "cache_creation_tokens": 0, "cache_read_tokens": 0}
        (cache_dir / "GEMINI-1_usage.json").write_text(json.dumps(usage))
        result = GeminiAdapter.get_token_usage("GEMINI-1", state_dir=tmp_path)
        assert result == usage

    def test_returns_none_when_cache_missing(self, tmp_path: Path):
        assert GeminiAdapter.get_token_usage("GEMINI-1", state_dir=tmp_path) is None

    def test_returns_none_on_malformed_cache(self, tmp_path: Path):
        cache_dir = tmp_path / "token_cache"
        cache_dir.mkdir()
        (cache_dir / "GEMINI-1_usage.json").write_text("{bad json}")
        assert GeminiAdapter.get_token_usage("GEMINI-1", state_dir=tmp_path) is None

    def test_returns_none_when_state_dir_none(self):
        assert GeminiAdapter.get_token_usage("GEMINI-1", state_dir=None) is None

    def test_write_then_read_roundtrip(self, tmp_path: Path):
        adapter = GeminiAdapter("GEMINI-1")
        usage = {"input_tokens": 400, "output_tokens": 100, "cache_creation_tokens": 0, "cache_read_tokens": 0}
        adapter._write_token_cache(usage, state_dir=tmp_path)
        result = GeminiAdapter.get_token_usage("GEMINI-1", state_dir=tmp_path)
        assert result == usage
