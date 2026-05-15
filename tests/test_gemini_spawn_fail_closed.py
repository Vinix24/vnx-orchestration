#!/usr/bin/env python3
"""Wave 4.6 PR-4.6.4 — fail-closed tests for spawn_gemini.

Covers:
- spawn_gemini raises FileNotFoundError when `gemini` binary is missing.
- chunk_timeout breach (simulated via drain_stream error event) returns timed_out=True.
- on_event returning False stops the stream early and sets stopped_early=True.
- BrokenPipeError on stdin write returns a structured failure, not an exception.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_LIB / "adapters"))

from provider_spawns.gemini_spawn import spawn_gemini, GeminiSpawnResult
from canonical_event import CanonicalEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeStdin:
    """Minimal stdin that succeeds or raises on write."""

    def __init__(self, raise_broken_pipe: bool = False) -> None:
        self._broken = raise_broken_pipe

    def write(self, data: bytes) -> None:
        if self._broken:
            raise BrokenPipeError("fake broken pipe")

    def close(self) -> None:
        pass


class _FakeProc:
    """Minimal subprocess.Popen stand-in."""

    def __init__(
        self,
        returncode: int = 0,
        raise_broken_pipe: bool = False,
    ) -> None:
        self.stdin = _FakeStdin(raise_broken_pipe=raise_broken_pipe)
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self.returncode = returncode
        self.pid = 99999

    def wait(self, timeout=None) -> int:
        return self.returncode

    def kill(self) -> None:
        pass

    def poll(self) -> int:
        return self.returncode


def _make_canonical(event_type: str, data: dict) -> CanonicalEvent:
    return CanonicalEvent(
        dispatch_id="d1",
        terminal_id="T3",
        provider="gemini",
        event_type=event_type,
        data=data,
        observability_tier=1,
    )


# ---------------------------------------------------------------------------
# Test: binary missing → structured GeminiSpawnResult (returncode=127)
# ---------------------------------------------------------------------------

class TestBinaryMissingReturnsStructuredResult:

    def test_streaming_path_returns_structured_result_on_fnfe(self):
        """spawn_gemini returns GeminiSpawnResult(returncode=127) when gemini binary absent (streaming)."""
        with patch("subprocess.Popen", side_effect=FileNotFoundError("No such file: gemini")), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "1"}):
            result = spawn_gemini("prompt", "gemini-2.5-pro", "d1", "T3")
        assert isinstance(result, GeminiSpawnResult)
        assert result.returncode == 127
        assert "not found" in (result.error or "").lower()

    def test_legacy_path_returns_structured_result_on_fnfe(self):
        """spawn_gemini returns GeminiSpawnResult(returncode=127) when gemini binary absent (legacy)."""
        with patch("subprocess.Popen", side_effect=FileNotFoundError("No such file: gemini")), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "0"}):
            result = spawn_gemini("prompt", "gemini-2.5-pro", "d1", "T3")
        assert isinstance(result, GeminiSpawnResult)
        assert result.returncode == 127
        assert "not found" in (result.error or "").lower()

    def test_generic_oserror_returns_structured_failure(self):
        """Non-FileNotFoundError OSError returns GeminiSpawnResult with error, not raised."""
        with patch("subprocess.Popen", side_effect=OSError("permission denied")), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "1"}):
            result = spawn_gemini("prompt", "gemini-2.5-pro", "d1", "T3")
        assert result.returncode != 0
        assert result.error is not None
        assert "permission denied" in (result.error or "")

    def test_spawn_returns_structured_result_when_binary_missing(self):
        """With a PATH containing no gemini binary, spawn_gemini returns returncode=127, not FileNotFoundError."""
        import tempfile
        with tempfile.TemporaryDirectory() as empty_bin:
            result = spawn_gemini(
                "prompt", "gemini-2.5-pro", "d1", "T3",
                extra_env={"PATH": empty_bin},
            )
        assert isinstance(result, GeminiSpawnResult)
        assert result.returncode == 127
        assert result.error is not None
        assert "not found" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Test: chunk_timeout → timed_out=True
# ---------------------------------------------------------------------------

class TestChunkTimeoutReturnsTrueFlag:

    def test_streaming_timeout_event_sets_timed_out(self):
        """Synthetic timeout error event from drain_stream sets timed_out=True."""
        timeout_event = _make_canonical(
            "error",
            {"reason": "chunk timeout 60.0s exceeded"},
        )

        with patch("subprocess.Popen", return_value=_FakeProc()), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "1"}), \
             patch(
                 "provider_spawns.gemini_spawn._GeminiNormalizerHost.drain_stream",
                 return_value=iter([timeout_event]),
             ):
            result = spawn_gemini("prompt", "gemini-2.5-pro", "d1", "T3")

        assert result.timed_out is True

    def test_total_deadline_event_sets_timed_out(self):
        """Synthetic deadline error event from drain_stream sets timed_out=True."""
        deadline_event = _make_canonical(
            "error",
            {"reason": "total deadline 600s exceeded"},
        )

        with patch("subprocess.Popen", return_value=_FakeProc()), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "1"}), \
             patch(
                 "provider_spawns.gemini_spawn._GeminiNormalizerHost.drain_stream",
                 return_value=iter([deadline_event]),
             ):
            result = spawn_gemini("prompt", "gemini-2.5-pro", "d1", "T3")

        assert result.timed_out is True

    def test_legacy_timeout_returns_timed_out(self):
        """Legacy path: _drain_buffered returning 'timeout' status sets timed_out=True."""
        with patch("subprocess.Popen", return_value=_FakeProc()), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "0"}), \
             patch(
                 "provider_spawns.gemini_spawn._drain_buffered",
                 return_value=("", "", "timeout"),
             ):
            result = spawn_gemini("prompt", "gemini-2.5-pro", "d1", "T3")

        assert result.timed_out is True
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Test: on_event returning False stops stream early
# ---------------------------------------------------------------------------

class TestOnEventFalseStopsStreamEarly:

    def test_stopped_early_set_after_on_event_false(self):
        """on_event returning False sets stopped_early=True after first event."""
        events = [
            _make_canonical("text", {"text": "partial output"}),
            _make_canonical("complete", {"text": "should not reach here"}),
        ]
        calls = []

        def on_event(ev: CanonicalEvent):
            calls.append(ev.event_type)
            return False  # stop after first event

        with patch("subprocess.Popen", return_value=_FakeProc()), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "1"}), \
             patch(
                 "provider_spawns.gemini_spawn._GeminiNormalizerHost.drain_stream",
                 return_value=iter(events),
             ):
            result = spawn_gemini(
                "prompt", "gemini-2.5-pro", "d1", "T3",
                on_event=on_event,
            )

        assert result.stopped_early is True
        assert len(calls) == 1
        assert calls[0] == "text"

    def test_on_event_true_continues_stream(self):
        """on_event returning True (or None) does not stop the stream."""
        events = [
            _make_canonical("text", {"text": "part 1"}),
            _make_canonical("complete", {"text": "done"}),
        ]
        calls = []

        def on_event(ev: CanonicalEvent):
            calls.append(ev.event_type)
            return True  # continue

        with patch("subprocess.Popen", return_value=_FakeProc()), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "1"}), \
             patch(
                 "provider_spawns.gemini_spawn._GeminiNormalizerHost.drain_stream",
                 return_value=iter(events),
             ):
            result = spawn_gemini(
                "prompt", "gemini-2.5-pro", "d1", "T3",
                on_event=on_event,
            )

        assert result.stopped_early is False
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# Test: BrokenPipeError on stdin write → structured failure
# ---------------------------------------------------------------------------

class TestBrokenPipeReturnsStructuredFailure:

    def test_streaming_broken_pipe_returns_error_result(self):
        """BrokenPipeError on stdin.write returns GeminiSpawnResult with error."""
        with patch("subprocess.Popen", return_value=_FakeProc(raise_broken_pipe=True)), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "1"}):
            result = spawn_gemini("prompt", "gemini-2.5-pro", "d1", "T3")

        assert result.returncode != 0
        assert "BrokenPipeError" in (result.error or "")

    def test_legacy_broken_pipe_returns_error_result(self):
        """BrokenPipeError on stdin.write (legacy path) returns GeminiSpawnResult."""
        with patch("subprocess.Popen", return_value=_FakeProc(raise_broken_pipe=True)), \
             patch.dict(os.environ, {"VNX_GEMINI_STREAM": "0"}):
            result = spawn_gemini("prompt", "gemini-2.5-pro", "d1", "T3")

        assert result.returncode != 0
        assert "BrokenPipeError" in (result.error or "")
