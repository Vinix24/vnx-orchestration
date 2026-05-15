"""Tests for claude_sdk_spawn fail-closed behaviour.

Wave 4.6 PR-4.6.7. Verifies that the spawn handler returns structured error
results (never raises) for all pre-flight guard conditions and control-flow
exits, without making any real API calls.
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from typing import Any, Generator, Optional
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/lib is importable
_LIB = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_module():
    """Re-import claude_sdk_spawn with a fresh module state for each test."""
    if "provider_spawns.claude_sdk_spawn" in sys.modules:
        del sys.modules["provider_spawns.claude_sdk_spawn"]
    from provider_spawns.claude_sdk_spawn import spawn_claude_sdk, ClaudeSDKSpawnResult
    return spawn_claude_sdk, ClaudeSDKSpawnResult


def _make_mock_anthropic(text_chunks: list[str], raise_exc: Optional[Exception] = None):
    """Return a minimal mock anthropic module."""
    mock_anthropic = MagicMock()
    mock_anthropic.APIError = Exception

    mock_stream = MagicMock()
    if raise_exc is not None:
        mock_stream.__enter__ = MagicMock(side_effect=raise_exc)
    else:
        mock_stream.__enter__ = MagicMock(return_value=mock_stream)
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.text_stream = iter(text_chunks)
        final_message = MagicMock()
        final_message.usage.input_tokens = 10
        final_message.usage.output_tokens = len(text_chunks) * 3
        mock_stream.get_final_message = MagicMock(return_value=final_message)

    mock_client = MagicMock()
    mock_client.messages.stream.return_value = mock_stream
    mock_anthropic.Anthropic.return_value = mock_client

    return mock_anthropic


# ---------------------------------------------------------------------------
# Guard 1: SDK not installed
# ---------------------------------------------------------------------------

def test_returns_error_when_sdk_not_installed(monkeypatch):
    """returncode=127 and error message when anthropic is not installed."""
    # Remove module from cache to force re-evaluation with patched None
    if "provider_spawns.claude_sdk_spawn" in sys.modules:
        del sys.modules["provider_spawns.claude_sdk_spawn"]

    with patch.dict(sys.modules, {"anthropic": None}):
        from provider_spawns.claude_sdk_spawn import spawn_claude_sdk
        result = spawn_claude_sdk(
            prompt="hello",
            model="claude-sonnet-4-6",
            dispatch_id="test-dispatch",
            terminal_id="T1",
        )

    assert result.returncode == 127
    assert result.error is not None
    assert "anthropic" in result.error.lower()
    assert result.timed_out is False
    assert result.events_written == 0


# ---------------------------------------------------------------------------
# Guard 2: API key missing
# ---------------------------------------------------------------------------

def test_returns_error_when_api_key_missing(monkeypatch):
    """returncode=78 when ANTHROPIC_API_KEY is not set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    if "provider_spawns.claude_sdk_spawn" in sys.modules:
        del sys.modules["provider_spawns.claude_sdk_spawn"]

    mock_anthropic = MagicMock()
    mock_anthropic.APIError = Exception
    with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
        from provider_spawns.claude_sdk_spawn import spawn_claude_sdk
        result = spawn_claude_sdk(
            prompt="hello",
            model="claude-sonnet-4-6",
            dispatch_id="test-dispatch",
            terminal_id="T1",
        )

    assert result.returncode == 78
    assert result.error is not None
    assert "ANTHROPIC_API_KEY" in result.error
    assert result.timed_out is False


# ---------------------------------------------------------------------------
# Guard 3: OAuth token refused
# ---------------------------------------------------------------------------

def test_refuses_oauth_token(monkeypatch):
    """returncode=78 and OAuth-specific error when API key starts with sk-ant-oat."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat-test-credential")

    if "provider_spawns.claude_sdk_spawn" in sys.modules:
        del sys.modules["provider_spawns.claude_sdk_spawn"]

    mock_anthropic = MagicMock()
    mock_anthropic.APIError = Exception
    with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
        from provider_spawns.claude_sdk_spawn import spawn_claude_sdk
        result = spawn_claude_sdk(
            prompt="hello",
            model="claude-sonnet-4-6",
            dispatch_id="test-dispatch",
            terminal_id="T1",
        )

    assert result.returncode == 78
    assert result.error is not None
    assert "OAuth" in result.error or "ADR-003" in result.error


# ---------------------------------------------------------------------------
# on_event stops stream early
# ---------------------------------------------------------------------------

def test_on_event_stops_stream(monkeypatch):
    """stopped_early=True when on_event callback returns False."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-valid-key")

    if "provider_spawns.claude_sdk_spawn" in sys.modules:
        del sys.modules["provider_spawns.claude_sdk_spawn"]

    mock_anthropic = _make_mock_anthropic(["chunk1", "chunk2", "chunk3"])
    with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
        from provider_spawns.claude_sdk_spawn import spawn_claude_sdk

        call_count = 0

        def stop_on_second(event):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return False
            return True

        result = spawn_claude_sdk(
            prompt="test",
            model="claude-sonnet-4-6",
            dispatch_id="test-dispatch",
            terminal_id="T1",
            on_event=stop_on_second,
        )

    assert result.stopped_early is True
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.events_written >= 2


# ---------------------------------------------------------------------------
# total_deadline breach
# ---------------------------------------------------------------------------

def test_total_deadline_breach(monkeypatch):
    """timed_out=True and returncode=124 when total_deadline is exceeded."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-valid-key")

    if "provider_spawns.claude_sdk_spawn" in sys.modules:
        del sys.modules["provider_spawns.claude_sdk_spawn"]

    # Stream with 5 chunks; we set total_deadline=0.0 so first chunk overflows
    mock_anthropic = _make_mock_anthropic(["a", "b", "c", "d", "e"])
    with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
        from provider_spawns.claude_sdk_spawn import spawn_claude_sdk

        result = spawn_claude_sdk(
            prompt="test",
            model="claude-sonnet-4-6",
            dispatch_id="test-dispatch",
            terminal_id="T1",
            total_deadline=0.0,  # immediately expired
        )

    assert result.timed_out is True
    assert result.returncode == 124
