#!/usr/bin/env python3
"""The silence heartbeat stops when deliver_via_subprocess returns (TSK-1).

The teardown used to only join the silence heartbeat thread without ever
setting its stop event; the thread waits up to 30 s on that event, so every
delivery paid the full 5 s join timeout (and kept a live thread afterwards).

Red on the old delivery.py: _start_heartbeat returns a 3-tuple and the
silence stop event is never set.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import subprocess_dispatch  # noqa: E402
import subprocess_dispatch_internals.delivery as _delivery  # noqa: E402
from provider_spawns.claude_spawn import ClaudeSpawnResult  # noqa: E402


class _FakeEventStore:
    def clear(self, terminal_id, archive_dispatch_id=None):
        pass


class _FakeAdapter:
    """Stands in for the SubprocessAdapter the teardown uses after the spawn."""

    def __init__(self):
        self.event_store = _FakeEventStore()

    def trigger_report_pipeline(self, *args, **kwargs):
        pass


def _spawn_result() -> ClaudeSpawnResult:
    return ClaudeSpawnResult(
        returncode=0,
        completion={},
        events_written=0,
        session_id="sess-tsk1",
        timed_out=False,
        completion_text="ok",
        token_usage=None,
        _adapter=_FakeAdapter(),
    )


@pytest.fixture
def delivery_env(tmp_path):
    """Deliver on tmp_path with the same patches tests/test_pool_worker_runner.py uses."""
    data_dir = tmp_path / ".vnx-data"
    state_dir = tmp_path / "state"
    data_dir.mkdir()
    state_dir.mkdir()
    manifest = tmp_path / "manifest.json"
    captured: dict = {}

    real_start_heartbeat = _delivery._start_heartbeat

    def _capture_start_heartbeat(*args, **kwargs):
        started = real_start_heartbeat(*args, **kwargs)
        captured["heartbeat"] = started
        return started

    def _fake_spawn(*args, **kwargs):
        return _spawn_result()

    patches = [
        patch.dict(
            os.environ,
            {"VNX_DATA_DIR": str(data_dir), "VNX_STATE_DIR": str(state_dir)},
            clear=False,
        ),
        patch("vnx_paths.resolve_paths", return_value={"VNX_DATA_DIR": str(data_dir)}),
        patch.object(subprocess_dispatch, "_build_intelligence_section", lambda *a, **k: ""),
        patch.object(subprocess_dispatch, "_resolve_agent_cwd", lambda role: None),
        patch.object(subprocess_dispatch, "_write_manifest", lambda **kw: manifest),
        patch.object(subprocess_dispatch, "_promote_manifest", lambda *a, **kw: None),
        patch.object(_delivery, "_build_worker_identity_env", return_value={}),
        patch.object(_delivery, "apply_runtime_overrides", return_value=(300.0, 900.0)),
        patch.object(
            _delivery,
            "_prepare_dispatch",
            return_value=("instruction", None, None, str(manifest)),
        ),
        patch.object(_delivery, "_load_resume_session", return_value=None),
        patch.object(_delivery, "_start_heartbeat", side_effect=_capture_start_heartbeat),
        patch("provider_spawns.claude_spawn.spawn_claude", side_effect=_fake_spawn),
    ]
    with contextlib.ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        yield captured


def _deliver(dispatch_id: str):
    return _delivery.deliver_via_subprocess(
        terminal_id="T1",
        instruction="test",
        model="sonnet",
        dispatch_id=dispatch_id,
    )


def test_delivery_stops_silence_heartbeat_on_success(delivery_env):
    result = _deliver("tsk1-ok")

    assert result.success is True
    _, _, silence_stop, silence_thread = delivery_env["heartbeat"]
    assert silence_stop.is_set()
    assert not silence_thread.is_alive()


def test_delivery_stops_silence_heartbeat_when_spawn_raises(delivery_env):
    with patch(
        "provider_spawns.claude_spawn.spawn_claude",
        side_effect=RuntimeError("spawn exploded"),
    ):
        with pytest.raises(RuntimeError, match="spawn exploded"):
            _deliver("tsk1-error")

    _, _, silence_stop, silence_thread = delivery_env["heartbeat"]
    assert silence_stop.is_set()
    assert not silence_thread.is_alive()


def test_start_heartbeat_returns_the_event_its_silence_thread_waits_on():
    started = _delivery._start_heartbeat(
        "T1", "tsk1-hb", None, 300.0, process_cell=[None],
    )
    lease_stop, lease_thread, silence_stop, silence_thread = started
    try:
        assert lease_stop is None
        assert lease_thread is None
        assert isinstance(silence_stop, threading.Event)
        assert silence_stop.is_set() is False
        # Identity: the event handed back is the one the thread blocks on.
        assert silence_stop is silence_thread._args[2]
    finally:
        silence_stop.set()
        silence_thread.join(timeout=5)
