"""test_gemini_wrapper.py — Unit tests for gemini_wrapper.py.

Tests:
- gemini_exec calls subprocess with correct argv
- Prompt passed via stdin (input= kwarg)
- Token usage extracted from usageMetadata in stream-json
- emit_provider_cost called with correct provider/model/cost
- TimeoutExpired propagates
- Non-zero returncode raises RuntimeError
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import gemini_wrapper


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_GEMINI_NDJSON_WITH_USAGE = (
    '{"candidates":[{"content":{"parts":[{"text":"Hello"}]}}],'
    '"usageMetadata":{"promptTokenCount":500,"candidatesTokenCount":150}}\n'
)

_GEMINI_NDJSON_NO_USAGE = (
    '{"candidates":[{"content":{"parts":[{"text":"Hello"}]}}]}\n'
)


def _make_run_result(stdout="", returncode=0):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = stdout
    result.stderr = ""
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# Basic invocation
# ---------------------------------------------------------------------------

class TestGeminiExec:
    def test_subprocess_called_with_correct_argv(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        with patch("gemini_wrapper.subprocess.run", return_value=_make_run_result()) as mock_run, \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_costs._compute_cost_from_rates", return_value=(0.001, False)):

            gemini_wrapper.gemini_exec("my prompt", model="gemini-2.5-pro", dispatch_id="d-g001")

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd == ["gemini", "--model", "gemini-2.5-pro", "--output-format", "stream-json"]
        assert kwargs["input"] == "my prompt"
        assert kwargs["text"] is True
        assert kwargs["start_new_session"] is True

    def test_returns_stdout(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        expected = _GEMINI_NDJSON_WITH_USAGE

        with patch("gemini_wrapper.subprocess.run", return_value=_make_run_result(stdout=expected)), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_costs._compute_cost_from_rates", return_value=(0.001, False)):

            result = gemini_wrapper.gemini_exec("prompt", dispatch_id="d-g002")

        assert result == expected

    def test_emit_called_with_correct_provider(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        with patch("gemini_wrapper.subprocess.run", return_value=_make_run_result()), \
             patch("provider_costs.emit_provider_cost") as mock_emit, \
             patch("provider_costs._compute_cost_from_rates", return_value=(0.002, False)):

            gemini_wrapper.gemini_exec("prompt", model="gemini-2.5-pro", dispatch_id="d-g003")

        mock_emit.assert_called_once()
        kwargs = mock_emit.call_args.kwargs
        assert kwargs["provider"] == "gemini"
        assert kwargs["model"] == "gemini-2.5-pro"
        assert kwargs["dispatch_id"] == "d-g003"
        assert kwargs["project_id"] == "test-proj"

    def test_emit_cost_reflects_computed_value(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        with patch("gemini_wrapper.subprocess.run", return_value=_make_run_result()), \
             patch("provider_costs.emit_provider_cost") as mock_emit, \
             patch("provider_costs._compute_cost_from_rates", return_value=(0.00175, False)):

            gemini_wrapper.gemini_exec("prompt", model="gemini-2.5-pro", dispatch_id="d-g004")

        kwargs = mock_emit.call_args.kwargs
        assert kwargs["cost_usd_estimate"] == 0.00175

    def test_timeout_propagates(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        with patch("gemini_wrapper.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gemini", timeout=1)):
            with pytest.raises(subprocess.TimeoutExpired):
                gemini_wrapper.gemini_exec("prompt", timeout=1)

    def test_nonzero_returncode_raises(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        with patch("gemini_wrapper.subprocess.run", return_value=_make_run_result(returncode=1)), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_costs._compute_cost_from_rates", return_value=(None, False)):

            with pytest.raises(RuntimeError, match="gemini_exec failed"):
                gemini_wrapper.gemini_exec("prompt", dispatch_id="d-g005")


# ---------------------------------------------------------------------------
# Token parsing
# ---------------------------------------------------------------------------

class TestGeminiTokenParsing:
    def test_parse_usage_metadata(self):
        result = gemini_wrapper._parse_gemini_token_usage(_GEMINI_NDJSON_WITH_USAGE)
        assert result is not None
        assert result["input_tokens"] == 500
        assert result["output_tokens"] == 150

    def test_parse_no_usage_returns_none(self):
        result = gemini_wrapper._parse_gemini_token_usage(_GEMINI_NDJSON_NO_USAGE)
        assert result is None

    def test_parse_empty_string_returns_none(self):
        result = gemini_wrapper._parse_gemini_token_usage("")
        assert result is None

    def test_parse_top_level_token_counts(self):
        ndjson = '{"promptTokenCount":400,"candidatesTokenCount":120}\n'
        result = gemini_wrapper._parse_gemini_token_usage(ndjson)
        assert result is not None
        assert result["input_tokens"] == 400
        assert result["output_tokens"] == 120

    def test_parse_invalid_json_returns_none(self):
        result = gemini_wrapper._parse_gemini_token_usage("not json\n")
        assert result is None

    def test_parse_multiple_lines_uses_last_nonzero(self):
        # Multiple streaming events; last usageMetadata wins
        ndjson = (
            '{"usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":30}}\n'
            '{"usageMetadata":{"promptTokenCount":450,"candidatesTokenCount":160}}\n'
        )
        result = gemini_wrapper._parse_gemini_token_usage(ndjson)
        assert result is not None
        assert result["input_tokens"] == 450
        assert result["output_tokens"] == 160
