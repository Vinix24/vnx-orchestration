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
        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        mock_proc = _make_popen_result()

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc), \
             patch("provider_costs.emit_provider_cost") as mock_emit:

            kimi_wrapper.kimi_exec("prompt", model="kimi-k3", dispatch_id="d-k004")

        mock_emit.assert_called_once()
        kwargs = mock_emit.call_args.kwargs
        assert kwargs["provider"] == "kimi"
        # Emit carries the resolved registry KEY (same label the provider lane
        # emits), never the raw CLI arg.
        assert kwargs["model"] == "kimi-k3"
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
# Model resolution (OI-1077)
# ---------------------------------------------------------------------------

class TestKimiModelResolution:
    """kimi_exec routes model resolution through provider_dispatch's kimi
    resolver — the same chain the provider lane uses — instead of the old
    hardcoded 'kimi-k2.6' default that passed raw registry keys to the CLI
    (rc=1 'LLM not set' on kimi-cli 1.46.0)."""

    def test_resolver_is_consulted(self, monkeypatch):
        """Both resolver stages run, and the CLI arg the resolver returns is
        what lands in argv. kimi_exec imports the resolver lazily inside the
        call, so patching the provider_dispatch module namespace intercepts
        the call-time binding."""
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        mock_proc = _make_popen_result()

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("provider_costs.emit_provider_cost"), \
             patch("provider_dispatch._kimi_resolve_requested_key",
                   return_value="resolved-key") as mock_key, \
             patch("provider_dispatch._kimi_resolve_cli_model_arg",
                   return_value="managed/cli-arg") as mock_cli:

            kimi_wrapper.kimi_exec("prompt", model="anything", dispatch_id="d-k010")

        mock_key.assert_called_once_with("anything")
        mock_cli.assert_called_once_with("resolved-key")
        cmd = mock_popen.call_args.args[0]
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "managed/cli-arg"

    def test_default_model_resolves_registry_default(self, monkeypatch):
        """No explicit model -> registry default key (kimi_cli.default_model)
        resolved to its slash-form CLI arg, ALWAYS passed via -m (the old code
        omitted -m and silently rode ~/.kimi/config.toml's default)."""
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        mock_proc = _make_popen_result()

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("provider_costs.emit_provider_cost") as mock_emit:

            kimi_wrapper.kimi_exec("prompt", dispatch_id="d-k011")

        cmd = mock_popen.call_args.args[0]
        assert "-m" in cmd
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "kimi-code/k3"
        assert mock_emit.call_args.kwargs["model"] == "kimi-k3"

    def test_explicit_registry_key_maps_to_cli_arg(self, monkeypatch):
        """A registry key ('kimi-k3') never reaches the CLI raw — it maps to
        the cli_model_arg form the CLI actually accepts."""
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        mock_proc = _make_popen_result()

        with patch("kimi_wrapper.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("provider_costs.emit_provider_cost"):

            kimi_wrapper.kimi_exec("prompt", model="kimi-k3", dispatch_id="d-k012")

        cmd = mock_popen.call_args.args[0]
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "kimi-code/k3"
        assert "kimi-k3" not in cmd

    def test_dead_model_fails_loud_before_spawn(self, monkeypatch):
        """The retired 'kimi-k2.6' (the module's OWN old default) is refused by
        the resolver before any subprocess is spawned — fail-loud, no silent
        pass-through of a stale string the CLI rejects with 'LLM not set'."""
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        from provider_dispatch import KimiModelResolutionError

        with patch("kimi_wrapper.subprocess.Popen") as mock_popen:
            with pytest.raises(KimiModelResolutionError):
                kimi_wrapper.kimi_exec("prompt", model="kimi-k2.6", dispatch_id="d-k013")

        mock_popen.assert_not_called()


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
