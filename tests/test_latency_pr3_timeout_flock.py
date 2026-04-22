"""Tests for latency PR-3: timeout misclassification + SessionStore flock.

- was_timed_out() signal propagation from adapter into deliver_via_subprocess
- SessionStore concurrent save() preserves all terminal entries via fcntl.flock
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

import subprocess_dispatch
from session_store import SessionStore
from subprocess_dispatch import deliver_via_subprocess


@pytest.fixture
def mock_adapter():
    with patch("subprocess_dispatch.SubprocessAdapter") as cls:
        instance = MagicMock()
        instance.was_timed_out.return_value = False
        deliver_result = MagicMock()
        deliver_result.success = True
        deliver_result.session_id = "sess-abc"
        deliver_result.terminal_id = "T1"
        deliver_result.dispatch_id = "d-1"
        deliver_result.pane_id = None
        deliver_result.path_used = "subprocess"
        instance.deliver.return_value = deliver_result
        instance.read_events_with_timeout.return_value = iter([])
        obs = MagicMock()
        obs.transport_state = {"returncode": 0}
        instance.observe.return_value = obs
        instance._get_event_store.return_value = None
        instance.get_session_id.return_value = "sess-abc"
        cls.return_value = instance
        yield instance


class TestTimeoutClassification:
    def test_was_timed_out_marks_failure(self, mock_adapter):
        mock_adapter.was_timed_out.return_value = True
        result = deliver_via_subprocess("T1", "do stuff", "sonnet", "d-1")
        assert result.success is False

    def test_happy_path_not_timed_out(self, mock_adapter):
        mock_adapter.was_timed_out.return_value = False
        result = deliver_via_subprocess("T1", "do stuff", "sonnet", "d-1")
        assert result.success is True


class TestSessionStoreFlock:
    def test_concurrent_save_preserves_all_entries(self, tmp_path):
        store_dir = tmp_path / "state"
        store = SessionStore(state_dir=store_dir)

        def save_terminal(terminal_id: str, session_id: str):
            for i in range(5):
                store.save(terminal_id, f"{session_id}-{i}", dispatch_id=f"d-{i}")

        threads = [
            threading.Thread(target=save_terminal, args=("T1", "s1")),
            threading.Thread(target=save_terminal, args=("T2", "s2")),
            threading.Thread(target=save_terminal, args=("T3", "s3")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        sessions_path = store_dir / "subprocess_sessions.json"
        data = json.loads(sessions_path.read_text())
        terminals = data.get("terminals", {})
        assert "T1" in terminals
        assert "T2" in terminals
        assert "T3" in terminals

    def test_flock_degrades_gracefully_when_fcntl_fails(self, tmp_path, monkeypatch):
        store = SessionStore(state_dir=tmp_path / "state")
        import fcntl
        def broken_flock(*args, **kwargs):
            raise OSError("mocked flock failure")
        monkeypatch.setattr(fcntl, "flock", broken_flock)
        store.save("T1", "sess-abc")
        assert store.load("T1") == "sess-abc"
