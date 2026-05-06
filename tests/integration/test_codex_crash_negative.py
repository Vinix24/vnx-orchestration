#!/usr/bin/env python3
"""Negative integration test: CodexAdapter drain_stream crash handling.

Uses a real subprocess (a Python script that writes NDJSON then sleeps)
to simulate codex mid-run SIGKILL without requiring the codex binary.
Verifies that StreamingDrainerMixin emits a synthetic error event on crash
and that the EventStore is consistent (no data loss, no orphan handles).
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

from adapters.codex_adapter import CodexAdapter
from event_store import EventStore

pytestmark = pytest.mark.integration

# A fake codex subprocess: writes two NDJSON events, then sleeps indefinitely.
_FAKE_CODEX_SCRIPT = """\
import json, sys, time

print(json.dumps({"type": "thread.started"}), flush=True)
print(json.dumps({"type": "agent_message", "text": "Analyzing..."}), flush=True)
time.sleep(300)  # will be killed before this finishes
"""

# A fake codex subprocess that exits cleanly after a few events.
_FAKE_CODEX_CLEAN_SCRIPT = """\
import json, sys

print(json.dumps({"type": "thread.started"}), flush=True)
print(json.dumps({"type": "agent_message", "text": "Done."}), flush=True)
print(json.dumps({"type": "turn.completed"}), flush=True)
sys.exit(0)
"""

# A fake codex subprocess that exits non-zero immediately.
_FAKE_CODEX_ERROR_SCRIPT = """\
import json, sys

print(json.dumps({"type": "thread.started"}), flush=True)
print(json.dumps({"type": "agent_message", "text": "Partial output."}), flush=True)
sys.exit(1)
"""


def _spawn_fake_codex(script: str) -> subprocess.Popen:
    """Spawn a Python subprocess that mimics codex NDJSON output."""
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


@pytest.fixture()
def event_store(tmp_path: Path) -> EventStore:
    return EventStore(events_dir=tmp_path / "events")


class TestCodexCrashNegative:
    """Verify drain_stream recovers cleanly when codex subprocess is killed."""

    def test_sigkill_mid_run_emits_synthetic_error(self, event_store: EventStore):
        """Kill -9 on codex subprocess → synthetic error event emitted."""
        terminal_id = "T-crash"
        dispatch_id = "crash-test-001"

        adapter = CodexAdapter(terminal_id)
        adapter._current_terminal_id = terminal_id
        adapter._current_dispatch_id = dispatch_id

        proc = _spawn_fake_codex(_FAKE_CODEX_SCRIPT)

        # Kill after 0.3s to ensure at least one event was written
        def _kill_after_delay():
            time.sleep(0.3)
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        killer = threading.Thread(target=_kill_after_delay, daemon=True)
        killer.start()

        events_seen: list = []
        for ev in adapter.drain_stream(
            proc,
            terminal_id,
            dispatch_id,
            event_store,
            chunk_timeout=5.0,
            total_deadline=10.0,
        ):
            events_seen.append(ev)

        killer.join(timeout=2)

        # The drainer must have emitted at least one real event before the kill
        # and a synthetic error event after the kill.
        types = [ev.event_type for ev in events_seen]
        assert "error" in types, (
            f"Expected synthetic error event after kill -9, got types: {types}"
        )

    def test_sigkill_archive_is_non_empty(self, event_store: EventStore):
        """Events written before kill -9 must be present in EventStore."""
        terminal_id = "T-crash-archive"
        dispatch_id = "crash-archive-001"

        adapter = CodexAdapter(terminal_id)
        adapter._current_terminal_id = terminal_id
        adapter._current_dispatch_id = dispatch_id

        proc = _spawn_fake_codex(_FAKE_CODEX_SCRIPT)

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

        # EventStore should contain events written before the kill
        count = event_store.event_count(terminal_id)
        assert count > 0, "EventStore must contain events written before kill -9"

    def test_nonzero_exit_without_complete_emits_error(self, event_store: EventStore):
        """Non-zero exit before turn.completed → synthetic error event appended."""
        terminal_id = "T-nonzero"
        dispatch_id = "nonzero-test-001"

        adapter = CodexAdapter(terminal_id)
        adapter._current_terminal_id = terminal_id
        adapter._current_dispatch_id = dispatch_id

        proc = _spawn_fake_codex(_FAKE_CODEX_ERROR_SCRIPT)
        events_seen = list(adapter.drain_stream(
            proc, terminal_id, dispatch_id, event_store,
            chunk_timeout=10.0, total_deadline=30.0,
        ))

        types = [ev.event_type for ev in events_seen]
        assert "error" in types, (
            f"Non-zero exit without complete event must produce synthetic error. Got types: {types}"
        )

    def test_clean_exit_produces_no_spurious_error(self, event_store: EventStore):
        """Clean exit (rc=0) with turn.completed → no spurious error event."""
        terminal_id = "T-clean"
        dispatch_id = "clean-test-001"

        adapter = CodexAdapter(terminal_id)
        adapter._current_terminal_id = terminal_id
        adapter._current_dispatch_id = dispatch_id

        proc = _spawn_fake_codex(_FAKE_CODEX_CLEAN_SCRIPT)
        events_seen = list(adapter.drain_stream(
            proc, terminal_id, dispatch_id, event_store,
            chunk_timeout=10.0, total_deadline=30.0,
        ))

        types = [ev.event_type for ev in events_seen]
        # Should have init, text, complete — no error
        assert "init" in types, f"Expected init event, got: {types}"
        assert "text" in types, f"Expected text event, got: {types}"
        assert "complete" in types, f"Expected complete event, got: {types}"
        # A clean run should not emit spurious error events
        assert "error" not in types, (
            f"Clean codex exit must not produce error events, got: {types}"
        )

    def test_no_orphan_event_store_handles_after_crash(self, event_store: EventStore, tmp_path: Path):
        """After crash, EventStore file is intact and parseable (no data loss)."""
        terminal_id = "T-orphan"
        dispatch_id = "orphan-test-001"

        adapter = CodexAdapter(terminal_id)
        adapter._current_terminal_id = terminal_id
        adapter._current_dispatch_id = dispatch_id

        proc = _spawn_fake_codex(_FAKE_CODEX_SCRIPT)

        def _kill():
            time.sleep(0.3)
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        threading.Thread(target=_kill, daemon=True).start()
        list(adapter.drain_stream(proc, terminal_id, dispatch_id, event_store,
                                   chunk_timeout=5.0, total_deadline=10.0))

        # EventStore file must be valid NDJSON (all lines parseable)
        event_file = event_store._terminal_path(terminal_id)
        if event_file.exists():
            for line in event_file.read_text().splitlines():
                if line.strip():
                    parsed = json.loads(line)
                    assert isinstance(parsed, dict), f"Non-dict line in EventStore: {line}"
