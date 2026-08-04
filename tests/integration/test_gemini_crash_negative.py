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
from canonical_event import VALID_EVENT_TYPES
from event_store import EventStore

pytestmark = pytest.mark.integration

# Fake gemini subprocess: writes events then sleeps indefinitely (will be killed).
_FAKE_GEMINI_HANG_SCRIPT = """\
import json, sys, time, os

print(json.dumps({"type": "session_start"}), flush=True)
print(json.dumps({"type": "message", "text": "Analyzing..."}), flush=True)
# Signal readiness: write sentinel so test knows events have been flushed
_ready = os.environ.get("VNX_GEMINI_READY_FILE")
if _ready:
    open(_ready, "w").close()
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


def _spawn_fake_gemini(script: str, extra_env: dict | None = None) -> subprocess.Popen:
    env = {**os.environ}
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )


def _wait_for_readiness(ready_file: Path, deadline: float = 10.0) -> None:
    """Poll for readiness sentinel file; raise TimeoutError if not seen."""
    deadline_ts = time.time() + deadline
    while not ready_file.exists():
        if time.time() >= deadline_ts:
            raise TimeoutError(
                f"Fake gemini did not write readiness marker at {ready_file} within {deadline}s"
            )
        time.sleep(0.05)


_READINESS_DEADLINE = 10.0


def _spawn_kill_when_ready(
    get_proc,
    ready_file: Path,
    deadline: float = _READINESS_DEADLINE,
) -> tuple[threading.Thread, list[BaseException]]:
    """Start a daemon thread that SIGKILLs the fake gemini once the readiness
    marker appears.

    Returns (thread, errors). A readiness timeout (marker never written) is
    recorded in `errors` instead of swallowed, so the main thread can fail the
    test with the real cause rather than a bare 'got: [None]'. (OI-979)
    """

    errors: list[BaseException] = []

    def _kill_when_ready() -> None:
        try:
            _wait_for_readiness(ready_file, deadline=deadline)
        except TimeoutError as exc:
            errors.append(exc)
            return
        proc = get_proc()
        if proc is None:
            return
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    killer = threading.Thread(target=_kill_when_ready, daemon=True)
    killer.start()
    return killer, errors


def _join_kill_when_ready(
    killer: threading.Thread, errors: list[BaseException]
) -> None:
    """Join the killer for the full readiness window, then surface a missing
    marker as a real test failure.

    The join must cover the same upper bound as the waiter: a shorter join can
    return while the killer is still waiting, which silently reintroduces the
    wall-clock race. (OI-979)
    """
    killer.join(timeout=_READINESS_DEADLINE)
    if errors:
        pytest.fail(
            f"Fake gemini never became ready; readiness marker was not written: {errors[0]}"
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

    def test_sigkill_mid_run_emits_synthetic_error(
        self, tmp_path: Path, event_store: EventStore
    ):
        """kill -9 on gemini subprocess → synthetic error event emitted."""
        terminal_id = "T-crash"
        dispatch_id = "gemini-crash-001"
        adapter = self._make_adapter(terminal_id, dispatch_id)
        ready_file = tmp_path / "ready_sigkill"
        proc = _spawn_fake_gemini(
            _FAKE_GEMINI_HANG_SCRIPT,
            extra_env={"VNX_GEMINI_READY_FILE": str(ready_file)},
        )

        killer, kill_errors = _spawn_kill_when_ready(lambda: proc, ready_file)

        events_seen = list(adapter.drain_stream(
            proc,
            terminal_id,
            dispatch_id,
            event_store,
            chunk_timeout=5.0,
            total_deadline=10.0,
        ))
        _join_kill_when_ready(killer, kill_errors)

        types = [ev.event_type for ev in events_seen]
        assert "error" in types, (
            f"Expected synthetic error after kill -9, got: {types}"
        )

    def test_sigkill_archive_is_non_empty(
        self, tmp_path: Path, event_store: EventStore
    ):
        """Events written before kill -9 must be present in EventStore."""
        terminal_id = "T-crash-archive"
        dispatch_id = "gemini-crash-archive-001"
        adapter = self._make_adapter(terminal_id, dispatch_id)
        ready_file = tmp_path / "ready_archive"
        proc = _spawn_fake_gemini(
            _FAKE_GEMINI_HANG_SCRIPT,
            extra_env={"VNX_GEMINI_READY_FILE": str(ready_file)},
        )

        killer, kill_errors = _spawn_kill_when_ready(lambda: proc, ready_file)

        list(adapter.drain_stream(
            proc, terminal_id, dispatch_id, event_store,
            chunk_timeout=5.0, total_deadline=10.0,
        ))
        _join_kill_when_ready(killer, kill_errors)

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

    def test_no_orphan_event_store_handles_after_crash(
        self, tmp_path: Path, event_store: EventStore
    ):
        """After crash, EventStore file is intact and parseable (no data loss)."""
        terminal_id = "T-orphan"
        dispatch_id = "gemini-orphan-001"
        adapter = self._make_adapter(terminal_id, dispatch_id)
        ready_file = tmp_path / "ready_orphan"
        proc = _spawn_fake_gemini(
            _FAKE_GEMINI_HANG_SCRIPT,
            extra_env={"VNX_GEMINI_READY_FILE": str(ready_file)},
        )

        killer, kill_errors = _spawn_kill_when_ready(lambda: proc, ready_file)
        list(adapter.drain_stream(proc, terminal_id, dispatch_id, event_store,
                                   chunk_timeout=5.0, total_deadline=10.0))
        _join_kill_when_ready(killer, kill_errors)

        event_file = event_store._terminal_path(terminal_id)
        if event_file.exists():
            for line in event_file.read_text().splitlines():
                if line.strip():
                    parsed = json.loads(line)
                    assert isinstance(parsed, dict), (
                        f"Non-dict line in EventStore: {line}"
                    )

    def test_stream_events_crash_mid_run_recoverable(
        self, tmp_path: Path, monkeypatch, event_store: EventStore
    ):
        """stream_events() crash mid-run: receipt is recoverable (no unhandled exception)."""
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        import adapters.gemini_adapter as ga_mod

        terminal_id = "T-se-crash"
        dispatch_id = "gemini-se-crash-001"
        adapter = GeminiAdapter(terminal_id)
        adapter._current_terminal_id = terminal_id
        adapter._current_dispatch_id = dispatch_id

        ready_file = tmp_path / "ready_se_crash"
        proc_holder: list = []
        original_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            if cmd and "gemini" in str(cmd[0]):
                child_env = {**os.environ, "VNX_GEMINI_READY_FILE": str(ready_file)}
                p = original_popen(
                    [sys.executable, "-c", _FAKE_GEMINI_HANG_SCRIPT],
                    stdin=kwargs.get("stdin", subprocess.PIPE),
                    stdout=kwargs.get("stdout", subprocess.PIPE),
                    stderr=kwargs.get("stderr", subprocess.PIPE),
                    start_new_session=kwargs.get("start_new_session", True),
                    env=child_env,
                )
                proc_holder.append(p)
                return p
            return original_popen(cmd, **kwargs)

        monkeypatch.setattr(ga_mod.subprocess, "Popen", fake_popen)

        killer, kill_errors = _spawn_kill_when_ready(
            lambda: proc_holder[0] if proc_holder else None, ready_file
        )

        ctx = {
            "terminal_id": terminal_id,
            "dispatch_id": dispatch_id,
            "event_store": event_store,
            "chunk_timeout": 5.0,
            "total_deadline": 10.0,
        }

        # Must not raise; crash is recovered as error events
        events = list(adapter.stream_events("test prompt", ctx))
        _join_kill_when_ready(killer, kill_errors)

        types = [ev.get("event_type") for ev in events]
        assert "error" in types, (
            f"stream_events() crash recovery must emit error event, got: {types}"
        )

    # ------------------------------------------------------------------
    # OI-980 regression: crash-path error yields must carry event_type
    # ------------------------------------------------------------------

    def test_stream_events_oserror_yields_error_with_event_type(self, monkeypatch):
        """Adapter Popen OSError → error event carrying event_type.

        Regression for OI-980: the pre-#1338 crash path yielded a bare dict
        ({"type": "error", ...}) with no event_type key. Consumers reading
        event_type saw nothing exactly on the path that matters most.
        """
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        import adapters.gemini_adapter as ga_mod

        def raise_oserror(*args, **kwargs):
            raise OSError("gemini binary launch failed")

        monkeypatch.setattr(ga_mod.subprocess, "Popen", raise_oserror)

        adapter = GeminiAdapter("T-oserror")
        ctx = {
            "terminal_id": "T-oserror",
            "dispatch_id": "gemini-oserror-001",
            "changed_files": ["__nonexistent__.txt"],
        }
        events = list(adapter.stream_events("test prompt", ctx))
        assert len(events) == 1, f"Expected exactly one error event, got: {events}"
        ev = events[0]
        assert ev.get("event_type") == "error", (
            f"Missing event_type on Popen OSError event: {ev}"
        )
        assert ev["event_type"] in VALID_EVENT_TYPES
        assert "gemini binary launch failed" in str(ev.get("data", {}).get("reason", ""))

    def test_stream_events_broken_pipe_yields_error_with_event_type(self, monkeypatch):
        """Adapter stdin.write BrokenPipeError → error event carrying event_type."""
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        import adapters.gemini_adapter as ga_mod

        class _BrokenStdin:
            def write(self, data):
                raise BrokenPipeError("pipe closed")

            def close(self):
                pass

        class _FakeProc:
            stdin = _BrokenStdin()

        monkeypatch.setattr(ga_mod.subprocess, "Popen", lambda *a, **k: _FakeProc())

        adapter = GeminiAdapter("T-brokenpipe")
        ctx = {
            "terminal_id": "T-brokenpipe",
            "dispatch_id": "gemini-brokenpipe-001",
            "changed_files": ["__nonexistent__.txt"],
        }
        events = list(adapter.stream_events("test prompt", ctx))
        assert len(events) == 1, f"Expected exactly one error event, got: {events}"
        ev = events[0]
        assert ev.get("event_type") == "error", (
            f"Missing event_type on BrokenPipeError event: {ev}"
        )
        assert ev["event_type"] in VALID_EVENT_TYPES
        assert "BrokenPipeError" in str(ev.get("data", {}).get("reason", ""))

    # ------------------------------------------------------------------
    # OI-979 regression: readiness timeout must be surfaced, not swallowed
    # ------------------------------------------------------------------

    def test_missing_readiness_marker_is_surfaced_not_swallowed(self, tmp_path: Path):
        """A missing readiness marker is recorded as a test failure cause.

        The pre-fix harness swallowed TimeoutError and killed anyway: the
        wall-clock race returned, and a regression showed up as a bare
        'got: [None]' with no indication the marker never appeared.
        """
        never_ready = tmp_path / "never_ready"
        killer, errors = _spawn_kill_when_ready(
            lambda: None, never_ready, deadline=0.3
        )
        killer.join(timeout=2.0)
        assert not killer.is_alive()
        assert errors, "Readiness TimeoutError was swallowed by the kill thread"
        assert isinstance(errors[0], TimeoutError)
