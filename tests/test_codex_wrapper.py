"""test_codex_wrapper.py — Unit tests for codex_wrapper.py.

Tests:
- codex_exec calls subprocess with correct argv
- Token usage is extracted from NDJSON stdout and passed to emit_provider_cost
- emit_provider_cost is called with correct provider/model
- TimeoutExpired propagates
- Non-zero returncode raises RuntimeError
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import codex_wrapper


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CODEX_NDJSON_WITH_TOKENS = (
    '{"type":"session_started","session_id":"s1"}\n'
    '{"type":"token_count","input_tokens":1500,"output_tokens":400}\n'
    '{"type":"completed"}\n'
)

_CODEX_NDJSON_NO_TOKENS = (
    '{"type":"session_started","session_id":"s1"}\n'
    '{"type":"completed"}\n'
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

class TestCodexExec:
    def test_subprocess_called_with_correct_argv(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        with patch("codex_wrapper.subprocess.run", return_value=_make_run_result()) as mock_run, \
             patch("provider_costs.emit_provider_cost") as mock_emit, \
             patch("provider_costs._compute_cost_from_rates", return_value=(0.0001, False)):

            codex_wrapper.codex_exec("hello world", model="gpt-5.5", dispatch_id="d-001")

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd == ["codex", "exec", "--json", "--model", "gpt-5.5"]
        assert kwargs["input"] == "hello world"
        assert kwargs["text"] is True
        assert kwargs["start_new_session"] is True

    def test_returns_stdout(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        expected_stdout = '{"type":"completed"}\n'

        with patch("codex_wrapper.subprocess.run", return_value=_make_run_result(stdout=expected_stdout)), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_costs._compute_cost_from_rates", return_value=(0.0, False)):

            result = codex_wrapper.codex_exec("test prompt", dispatch_id="d-002")

        assert result == expected_stdout

    def test_emit_called_with_correct_provider(self, monkeypatch):
        # Post-2026-06-24: the wrapper forwards the caller's project_id verbatim;
        # the env fallback now lives in emit_provider_cost (best-effort), not here.
        with patch("codex_wrapper.subprocess.run", return_value=_make_run_result(stdout=_CODEX_NDJSON_NO_TOKENS)), \
             patch("provider_costs.emit_provider_cost") as mock_emit, \
             patch("provider_costs._compute_cost_from_rates", return_value=(0.001, False)):

            codex_wrapper.codex_exec(
                "test prompt", model="gpt-5.5", dispatch_id="d-003", project_id="test-proj"
            )

        mock_emit.assert_called_once()
        call_kwargs = mock_emit.call_args.kwargs
        assert call_kwargs["provider"] == "codex"
        assert call_kwargs["model"] == "gpt-5.5"
        assert call_kwargs["dispatch_id"] == "d-003"
        assert call_kwargs["project_id"] == "test-proj"

    def test_timeout_propagates(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        with patch("codex_wrapper.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1)):
            with pytest.raises(subprocess.TimeoutExpired):
                codex_wrapper.codex_exec("test prompt", timeout=1)

    def test_nonzero_returncode_raises_runtime_error(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        with patch("codex_wrapper.subprocess.run", return_value=_make_run_result(returncode=1)), \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_costs._compute_cost_from_rates", return_value=(None, False)):

            with pytest.raises(RuntimeError, match="codex_exec failed"):
                codex_wrapper.codex_exec("test prompt", dispatch_id="d-004")


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

class TestCodexTokenParsing:
    def test_parse_no_tokens_returns_none(self):
        result = codex_wrapper._parse_codex_token_usage(_CODEX_NDJSON_NO_TOKENS)
        assert result is None

    def test_parse_empty_string_returns_none(self):
        result = codex_wrapper._parse_codex_token_usage("")
        assert result is None

    def test_parse_invalid_json_returns_none(self):
        result = codex_wrapper._parse_codex_token_usage("not json\n")
        assert result is None
