#!/usr/bin/env python3
"""Negative integration test: GeminiAdapter drain_stream crash handling.

Uses real Python subprocesses to simulate gemini mid-run SIGKILL without
requiring the gemini binary. Verifies that StreamingDrainerMixin emits a
synthetic error event on crash and that EventStore is consistent (no data loss).

Only tests the streaming path (VNX_GEMINI_STREAM=1); the legacy buffered path
is not affected by drain_stream crash recovery.

BILLING SAFETY: No Anthropic SDK. subprocess-only.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

LIB_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(LIB_DIR / "adapters"))

from adapters.gemini_adapter import GeminiAdapter
from event_store import EventStore

pytestmark = pytest.mark.integration

# Fake gemini subprocess: writes events then sleeps indefinitely (will be killed).
_FAKE_GEMINI_HANG_SCRIPT = """\
import json, sys, time

print(json.dumps({"type": "session_start"}), flush=True)
print(json.dumps({"type": "message", "text": "Analyzing..."}), flush=True)
time.sleep(300)  # will be killed before this finishes
"""

# Fake gemini subprocess that exits cleanly.
_FAKE_GEMINI_CLEAN_SCRIPT = """\
import json, sys

print(json.dumps({"type": "session_start"}), flush=True)
print(json.dumps({"type": "message", "text": "Done."}), flush=True)
print(json.dumps({"type": "result", "text": "Complete."}), flush=True)
sys.exit(0)
"""

# Fake gemini subprocess that exits non-zero without emitting a complete event.
_FAKE_GEMINI_ERROR_SCRIPT = """\
import json, sys

print(json.dumps({"type": "session_start"}), flush=True)
print(json.dumps({"type": "message", "text": "Partial output."}), flush=True)
sys.exit(1)
"""


def _spawn_fake_gemini(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


@pytest.fixture()
def event_store(tmp_path: Path) -> EventStore:
    return EventStore(events_dir=tmp_path / "events")


class TestGeminiCrashNegative:
    """Verify drain_stream recovers cleanly when gemini subprocess is killed."""

    def _make_adapter(self, terminal_id: str, dispatch_id: str) -> GeminiAdapter:
        adapter = GeminiAdapter(terminal_id)
        adapter._current_terminal_id = terminal_id
        adapter._current_dispatch_id = dispatch_id
        return adapter

    def test_sigkill_mid_run_emits_synthetic_error(self, event_store: EventStore):
        """kill -9 on gemini subprocess → synthetic error event emitted."""
        terminal_id = "T-crash"
        dispatch_id = "gemini-crash-001"
        adapter = self._make_adapter(terminal_id, dispatch_id)
        proc = _spawn_fake_gemini(_FAKE_GEMINI_HANG_SCRIPT)

        def _kill_after_delay():
            time.sleep(0.3)
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        killer = threading.Thread(target=_kill_after_delay, daemon=True)
        killer.start()

        events_seen = list(adapter.drain_stream(
            proc,
            terminal_id,
            dispatch_id,
            event_store,
            chunk_timeout=5.0,
            total_deadline=10.0,
        ))
        killer.join(timeout=2)

        types = [ev.event_type for ev in events_seen]
        assert "error" in types, (
            f"Expected synthetic error after kill -9, got: {types}"
        )

    def test_sigkill_archive_is_non_empty(self, event_store: EventStore):
        """Events written before kill -9 must be present in EventStore."""
        terminal_id = "T-crash-archive"
        dispatch_id = "gemini-crash-archive-001"
        adapter = self._make_adapter(terminal_id, dispatch_id)
        proc = _spawn_fake_gemini(_FAKE_GEMINI_HANG_SCRIPT)

        def _kill_after_delay():
            time.sleep(0.4)
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        killer = threading.Thread(target=_kill_after_delay, daemon=True)
        killer.start()

        list(adapter.drain_stream(
            proc, terminal_id, dispatch_id, event_store,
            chunk_timeout=5.0, total_deadline=10.0,
        ))
        killer.join(timeout=2)

        count = event_store.event_count(terminal_id)
        assert count > 0, (
            f"EventStore must contain events written before kill -9, got count={count}"
        )

    def test_nonzero_exit_without_complete_emits_error(self, event_store: EventStore):
        """Non-zero exit before result event → synthetic error event appended."""
        terminal_id = "T-nonzero"
        dispatch_id = "gemini-nonzero-001"
        adapter = self._make_adapter(terminal_id, dispatch_id)
        proc = _spawn_fake_gemini(_FAKE_GEMINI_ERROR_SCRIPT)

        events_seen = list(adapter.drain_stream(
            proc, terminal_id, dispatch_id, event_store,
            chunk_timeout=10.0, total_deadline=30.0,
        ))
        types = [ev.event_type for ev in events_seen]
        assert "error" in types, (
            f"Non-zero exit without complete must produce synthetic error, got: {types}"
        )

    def test_clean_exit_produces_no_spurious_error(self, event_store: EventStore):
        """Clean exit (rc=0) with result event → no spurious error event."""
        terminal_id = "T-clean"
        dispatch_id = "gemini-clean-001"
        adapter = self._make_adapter(terminal_id, dispatch_id)
        proc = _spawn_fake_gemini(_FAKE_GEMINI_CLEAN_SCRIPT)

        events_seen = list(adapter.drain_stream(
            proc, terminal_id, dispatch_id, event_store,
            chunk_timeout=10.0, total_deadline=30.0,
        ))
        types = [ev.event_type for ev in events_seen]
        assert "init" in types, f"Expected init event, got: {types}"
        assert "text" in types, f"Expected text event, got: {types}"
        assert "complete" in types, f"Expected complete event, got: {types}"
        assert "error" not in types, (
            f"Clean exit must not produce error events, got: {types}"
        )

    def test_no_orphan_event_store_handles_after_crash(self, event_store: EventStore):
        """After crash, EventStore file is intact and parseable (no data loss)."""
        terminal_id = "T-orphan"
        dispatch_id = "gemini-orphan-001"
        adapter = self._make_adapter(terminal_id, dispatch_id)
        proc = _spawn_fake_gemini(_FAKE_GEMINI_HANG_SCRIPT)

        def _kill():
            time.sleep(0.3)
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        threading.Thread(target=_kill, daemon=True).start()
        list(adapter.drain_stream(proc, terminal_id, dispatch_id, event_store,
                                   chunk_timeout=5.0, total_deadline=10.0))

        event_file = event_store._terminal_path(terminal_id)
        if event_file.exists():
            for line in event_file.read_text().splitlines():
                if line.strip():
                    parsed = json.loads(line)
                    assert isinstance(parsed, dict), (
                        f"Non-dict line in EventStore: {line}"
                    )

    def test_stream_events_crash_mid_run_recoverable(
        self, monkeypatch, event_store: EventStore
    ):
        """stream_events() crash mid-run: receipt is recoverable (no unhandled exception)."""
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        import adapters.gemini_adapter as ga_mod

        terminal_id = "T-se-crash"
        dispatch_id = "gemini-se-crash-001"
        adapter = GeminiAdapter(terminal_id)
        adapter._current_terminal_id = terminal_id
        adapter._current_dispatch_id = dispatch_id

        proc_holder: list = []
        original_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            if cmd and "gemini" in str(cmd[0]):
                p = original_popen(
                    [sys.executable, "-c", _FAKE_GEMINI_HANG_SCRIPT],
                    stdin=kwargs.get("stdin", subprocess.PIPE),
                    stdout=kwargs.get("stdout", subprocess.PIPE),
                    stderr=kwargs.get("stderr", subprocess.PIPE),
                    start_new_session=kwargs.get("start_new_session", True),
                )
                proc_holder.append(p)
                return p
            return original_popen(cmd, **kwargs)

        monkeypatch.setattr(ga_mod.subprocess, "Popen", fake_popen)

        def _kill_after_delay():
            time.sleep(0.3)
            if proc_holder:
                try:
                    os.kill(proc_holder[0].pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        killer = threading.Thread(target=_kill_after_delay, daemon=True)
        killer.start()

        ctx = {
            "terminal_id": terminal_id,
            "dispatch_id": dispatch_id,
            "event_store": event_store,
            "chunk_timeout": 5.0,
            "total_deadline": 10.0,
        }

        # Must not raise; crash is recovered as error events
        events = list(adapter.stream_events("test prompt", ctx))
        killer.join(timeout=2)

        types = [ev.get("event_type") for ev in events]
        assert "error" in types, (
            f"stream_events() crash recovery must emit error event, got: {types}"
        )
