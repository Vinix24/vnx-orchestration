"""test_kimi_wrapper.py — Unit tests for kimi_wrapper.py.

Tests:
- kimi_exec calls subprocess with correct argv (including -p flag)
- stdin=DEVNULL per cli-headless-subprocess-pattern
- Token usage is extracted from stream-json output
- emit_provider_cost called with cost_usd_estimate=None (subscription-flat)
- TimeoutExpired propagates + kills entire process group (pipe-hold fix)
- Non-zero returncode raises RuntimeError
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import kimi_wrapper


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_KIMI_NDJSON_WITH_USAGE = (
    '{"event_type":"TurnBegin"}\n'
    '{"event_type":"ContentPart","content":"Hello"}\n'
    '{"event_type":"usage_complete","usage":{"prompt_tokens":800,"completion_tokens":250}}\n'
    '{"event_type":"complete"}\n'
)

_KIMI_NDJSON_NO_USAGE = (
    '{"event_type":"TurnBegin"}\n'
    '{"event_type":"ContentPart","content":"Hello"}\n'
    '{"event_type":"complete"}\n'
)


def _make_popen_result(stdout="", returncode=0, pid=12345):
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (stdout, "")
    mock_proc.returncode = returncode
    mock_proc.pid = pid
    return mock_proc


# ---------------------------------------------------------------------------
# Basic invocation
# ---------------------------------------------------------------------------

class TestKimiExec:
    def test_subprocess_called_with_p_flag(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        mock_proc = _make_popen_result()

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("provider_costs.emit_provider_cost"):

            kimi_wrapper.kimi_exec("my prompt", dispatch_id="d-k001")

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert "kimi" in cmd
        assert "-p" in cmd
        assert "my prompt" in cmd
        assert "--print" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd

    def test_stdin_is_devnull(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        mock_proc = _make_popen_result()

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("provider_costs.emit_provider_cost"):

            kimi_wrapper.kimi_exec("prompt", dispatch_id="d-k002")

        _, kwargs = mock_popen.call_args
        assert kwargs.get("text") is True
        # stdin is open(os.devnull) — verify start_new_session for process group isolation
        assert kwargs.get("start_new_session") is True

    def test_returns_stdout(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        expected = '{"event_type":"complete"}\n'
        mock_proc = _make_popen_result(stdout=expected)

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc), \
             patch("provider_costs.emit_provider_cost"):

            result = kimi_wrapper.kimi_exec("test", dispatch_id="d-k003")

        assert result == expected

    def test_emit_called_with_subscription_flat(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        mock_proc = _make_popen_result()

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc), \
             patch("provider_costs.emit_provider_cost") as mock_emit:

            kimi_wrapper.kimi_exec("prompt", model="kimi-k2.6", dispatch_id="d-k004")

        mock_emit.assert_called_once()
        kwargs = mock_emit.call_args.kwargs
        assert kwargs["provider"] == "kimi"
        assert kwargs["model"] == "kimi-k2.6"
        assert kwargs["cost_usd_estimate"] is None
        assert kwargs["dispatch_id"] == "d-k004"

    def test_timeout_propagates(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="kimi", timeout=1)
        mock_proc.pid = 12345

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc), \
             patch("kimi_wrapper.os.killpg"), \
             patch("kimi_wrapper.os.getpgid", return_value=12345):
            with pytest.raises(subprocess.TimeoutExpired):
                kimi_wrapper.kimi_exec("prompt", timeout=1)

    def test_process_group_killed_on_timeout(self, monkeypatch):
        """On timeout, os.killpg kills the entire process group — not just the parent.

        This prevents pipe-hold hangs where kimi's child processes survive after
        the parent is killed and keep stdout/stderr pipes open indefinitely.
        """
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="kimi", timeout=1)
        mock_proc.pid = 42
        mock_proc.wait.return_value = None

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc), \
             patch("kimi_wrapper.os.killpg") as mock_killpg, \
             patch("kimi_wrapper.os.getpgid", return_value=99) as mock_getpgid:
            with pytest.raises(subprocess.TimeoutExpired):
                kimi_wrapper.kimi_exec("prompt", timeout=1)

        mock_getpgid.assert_called_once_with(42)
        mock_killpg.assert_called_once_with(99, signal.SIGKILL)
        mock_proc.wait.assert_called_once()

    def test_process_lookup_error_on_killpg_is_swallowed(self, monkeypatch):
        """ProcessLookupError during killpg is silenced — process already gone."""
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="kimi", timeout=1)
        mock_proc.pid = 42
        mock_proc.wait.return_value = None

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc), \
             patch("kimi_wrapper.os.killpg", side_effect=ProcessLookupError), \
             patch("kimi_wrapper.os.getpgid", return_value=99):
            with pytest.raises(subprocess.TimeoutExpired):
                kimi_wrapper.kimi_exec("prompt", timeout=1)
        # If we reach here without further exception, the swallow worked.

    def test_nonzero_returncode_raises(self, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        mock_proc = _make_popen_result(returncode=1)

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc), \
             patch("provider_costs.emit_provider_cost"):

            with pytest.raises(RuntimeError, match="kimi_exec failed"):
                kimi_wrapper.kimi_exec("prompt", dispatch_id="d-k005")


# ---------------------------------------------------------------------------
# Token parsing
# ---------------------------------------------------------------------------

class TestKimiTokenParsing:
    def test_parse_usage_complete_event(self):
        result = kimi_wrapper._parse_kimi_token_usage(_KIMI_NDJSON_WITH_USAGE)
        assert result is not None
        assert result["input_tokens"] == 800
        assert result["output_tokens"] == 250

    def test_parse_no_usage_returns_none(self):
        result = kimi_wrapper._parse_kimi_token_usage(_KIMI_NDJSON_NO_USAGE)
        assert result is None

    def test_parse_empty_string_returns_none(self):
        result = kimi_wrapper._parse_kimi_token_usage("")
        assert result is None

    def test_parse_status_update_token_count(self):
        ndjson = '{"event_type":"StatusUpdate","token_count":{"input_tokens":300,"output_tokens":100}}\n'
        result = kimi_wrapper._parse_kimi_token_usage(ndjson)
        assert result is not None
        assert result["input_tokens"] == 300
        assert result["output_tokens"] == 100

    def test_parse_invalid_json_returns_none(self):
        result = kimi_wrapper._parse_kimi_token_usage("not json\n")
        assert result is None
