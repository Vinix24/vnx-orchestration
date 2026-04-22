#!/usr/bin/env python3
"""Tests for latency PR-2: session persistence via --resume.

Covers:
  1. SessionStore.load() returns None when no file exists
  2. SessionStore.save() persists session_id to disk
  3. SessionStore.load() returns stored session_id after save
  4. SessionStore.clear() removes a terminal's entry
  5. SessionStore.save() is a no-op for empty session_id
  6. SessionStore.all_sessions() returns all stored sessions
  7. deliver_via_subprocess passes saved session_id as resume_session when VNX_SESSION_RESUME=1
  8. deliver_via_subprocess does NOT pass resume_session when VNX_SESSION_RESUME is unset
  9. deliver_via_subprocess saves new session_id after successful delivery
  10. deliver_via_subprocess does not crash when SessionStore fails (non-fatal)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path):
    from session_store import SessionStore
    return SessionStore(state_dir=tmp_path)


# ---------------------------------------------------------------------------
# 1–6: SessionStore unit tests
# ---------------------------------------------------------------------------

class TestSessionStoreLoad:
    def test_returns_none_when_file_absent(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.load("T1") is None

    def test_returns_none_for_unknown_terminal(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("T1", "sess-aaa", "dispatch-001")
        assert store.load("T2") is None

    def test_returns_none_for_empty_session_id(self, tmp_path):
        # Write a malformed entry directly
        path = tmp_path / "subprocess_sessions.json"
        path.write_text(json.dumps({
            "terminals": {"T1": {"session_id": "", "dispatch_id": "d1", "updated_at": "x"}}
        }))
        store = _make_store(tmp_path)
        assert store.load("T1") is None

    def test_returns_none_on_corrupt_file(self, tmp_path):
        path = tmp_path / "subprocess_sessions.json"
        path.write_text("NOT VALID JSON {{{")
        store = _make_store(tmp_path)
        assert store.load("T1") is None


class TestSessionStoreSave:
    def test_save_creates_file(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("T1", "sess-123", "d-001")
        path = tmp_path / "subprocess_sessions.json"
        assert path.exists()

    def test_save_and_load_roundtrip(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("T1", "sess-abc", "d-123")
        assert store.load("T1") == "sess-abc"

    def test_save_overwrites_prior_entry(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("T1", "sess-old", "d-001")
        store.save("T1", "sess-new", "d-002")
        assert store.load("T1") == "sess-new"

    def test_save_multiple_terminals(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("T1", "sess-t1", "d-001")
        store.save("T2", "sess-t2", "d-002")
        assert store.load("T1") == "sess-t1"
        assert store.load("T2") == "sess-t2"

    def test_save_noop_for_empty_session_id(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("T1", "", "d-001")
        assert store.load("T1") is None
        # File should not be created
        assert not (tmp_path / "subprocess_sessions.json").exists()

    def test_save_stores_dispatch_id(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("T1", "sess-xyz", "dispatch-999")
        raw = json.loads((tmp_path / "subprocess_sessions.json").read_text())
        assert raw["terminals"]["T1"]["dispatch_id"] == "dispatch-999"


class TestSessionStoreClear:
    def test_clear_removes_terminal(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("T1", "sess-aaa", "d-001")
        store.clear("T1")
        assert store.load("T1") is None

    def test_clear_preserves_other_terminals(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("T1", "sess-t1", "d-001")
        store.save("T2", "sess-t2", "d-002")
        store.clear("T1")
        assert store.load("T1") is None
        assert store.load("T2") == "sess-t2"

    def test_clear_noop_when_terminal_absent(self, tmp_path):
        store = _make_store(tmp_path)
        store.clear("T1")  # should not raise


class TestSessionStoreAllSessions:
    def test_all_sessions_empty(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.all_sessions() == {}

    def test_all_sessions_returns_all(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("T1", "sess-t1", "d-1")
        store.save("T2", "sess-t2", "d-2")
        result = store.all_sessions()
        assert result == {"T1": "sess-t1", "T2": "sess-t2"}


# ---------------------------------------------------------------------------
# 7–10: deliver_via_subprocess integration
# ---------------------------------------------------------------------------

def _make_deliver_mocks(session_id_returned: str | None = "sess-from-init"):
    """Build the minimal mock set for deliver_via_subprocess unit tests."""
    deliver_result = MagicMock()
    deliver_result.success = True
    deliver_result.terminal_id = "T1"
    deliver_result.dispatch_id = "d-test"
    deliver_result.pane_id = None
    deliver_result.path_used = "subprocess"

    obs_result = MagicMock()
    obs_result.transport_state = {"returncode": 0}

    mock_adapter = MagicMock()
    mock_adapter.deliver.return_value = deliver_result
    mock_adapter.read_events_with_timeout.return_value = iter([])
    mock_adapter.get_session_id.return_value = session_id_returned
    mock_adapter.observe.return_value = obs_result
    mock_adapter._get_event_store.return_value = None

    return mock_adapter


_COMMON_PATCHES = [
    ("subprocess_dispatch.SubprocessAdapter", None),  # filled per-test
    ("subprocess_dispatch._inject_skill_context", lambda *a, **kw: "instr"),
    ("subprocess_dispatch._inject_permission_profile", lambda *a, **kw: "instr"),
    ("subprocess_dispatch._resolve_agent_cwd", lambda *a, **kw: None),
    ("subprocess_dispatch._write_manifest", lambda *a, **kw: Path("/tmp/m.json")),
    ("subprocess_dispatch._promote_manifest", lambda *a, **kw: "/tmp/done.json"),
    ("subprocess_dispatch._capture_dispatch_parameters", lambda *a, **kw: None),
    ("subprocess_dispatch._capture_dispatch_outcome", lambda *a, **kw: None),
]


class TestSessionResumePassthrough:
    def test_resume_session_passed_when_env_set(self, monkeypatch, tmp_path):
        """When VNX_SESSION_RESUME=1 and a session is stored, deliver() receives resume_session."""
        import subprocess_dispatch as sd
        from session_store import SessionStore

        monkeypatch.setenv("VNX_SESSION_RESUME", "1")

        store = SessionStore(state_dir=tmp_path)
        store.save("T1", "prior-session-id", "d-prev")

        mock_adapter = _make_deliver_mocks("new-session-id")

        with patch("subprocess_dispatch.SubprocessAdapter", return_value=mock_adapter), \
             patch("subprocess_dispatch._inject_skill_context", return_value="instr"), \
             patch("subprocess_dispatch._inject_permission_profile", return_value="instr"), \
             patch("subprocess_dispatch._resolve_agent_cwd", return_value=None), \
             patch("subprocess_dispatch._write_manifest", return_value=Path("/tmp/m.json")), \
             patch("subprocess_dispatch._promote_manifest", return_value="/tmp/done.json"), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"), \
             patch("session_store.SessionStore", return_value=store):
            sd.deliver_via_subprocess("T1", "instruction", "sonnet", "d-test")

        _, kwargs = mock_adapter.deliver.call_args
        assert kwargs.get("resume_session") == "prior-session-id", (
            f"Expected prior-session-id, got {kwargs.get('resume_session')!r}"
        )

    def test_no_resume_when_env_unset(self, monkeypatch, tmp_path):
        """When VNX_SESSION_RESUME is not set, deliver() receives resume_session=None."""
        import subprocess_dispatch as sd

        monkeypatch.delenv("VNX_SESSION_RESUME", raising=False)

        mock_adapter = _make_deliver_mocks("new-session-id")

        with patch("subprocess_dispatch.SubprocessAdapter", return_value=mock_adapter), \
             patch("subprocess_dispatch._inject_skill_context", return_value="instr"), \
             patch("subprocess_dispatch._inject_permission_profile", return_value="instr"), \
             patch("subprocess_dispatch._resolve_agent_cwd", return_value=None), \
             patch("subprocess_dispatch._write_manifest", return_value=Path("/tmp/m.json")), \
             patch("subprocess_dispatch._promote_manifest", return_value="/tmp/done.json"), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"):
            sd.deliver_via_subprocess("T1", "instruction", "sonnet", "d-test")

        _, kwargs = mock_adapter.deliver.call_args
        assert kwargs.get("resume_session") is None, (
            f"Expected None, got {kwargs.get('resume_session')!r}"
        )

    def test_resume_none_when_no_prior_session(self, monkeypatch, tmp_path):
        """VNX_SESSION_RESUME=1 but no prior session → resume_session=None."""
        import subprocess_dispatch as sd
        from session_store import SessionStore

        monkeypatch.setenv("VNX_SESSION_RESUME", "1")
        store = SessionStore(state_dir=tmp_path)  # empty store

        mock_adapter = _make_deliver_mocks("new-session-id")

        with patch("subprocess_dispatch.SubprocessAdapter", return_value=mock_adapter), \
             patch("subprocess_dispatch._inject_skill_context", return_value="instr"), \
             patch("subprocess_dispatch._inject_permission_profile", return_value="instr"), \
             patch("subprocess_dispatch._resolve_agent_cwd", return_value=None), \
             patch("subprocess_dispatch._write_manifest", return_value=Path("/tmp/m.json")), \
             patch("subprocess_dispatch._promote_manifest", return_value="/tmp/done.json"), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"), \
             patch("session_store.SessionStore", return_value=store):
            sd.deliver_via_subprocess("T1", "instruction", "sonnet", "d-test")

        _, kwargs = mock_adapter.deliver.call_args
        assert kwargs.get("resume_session") is None


class TestSessionPersistenceAfterDelivery:
    def test_session_id_saved_after_success(self, monkeypatch, tmp_path):
        """After successful delivery, captured session_id is saved to SessionStore."""
        import subprocess_dispatch as sd
        from session_store import SessionStore

        monkeypatch.setenv("VNX_SESSION_RESUME", "1")
        store = SessionStore(state_dir=tmp_path)

        mock_adapter = _make_deliver_mocks("fresh-session-xyz")

        with patch("subprocess_dispatch.SubprocessAdapter", return_value=mock_adapter), \
             patch("subprocess_dispatch._inject_skill_context", return_value="instr"), \
             patch("subprocess_dispatch._inject_permission_profile", return_value="instr"), \
             patch("subprocess_dispatch._resolve_agent_cwd", return_value=None), \
             patch("subprocess_dispatch._write_manifest", return_value=Path("/tmp/m.json")), \
             patch("subprocess_dispatch._promote_manifest", return_value="/tmp/done.json"), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"), \
             patch("session_store.SessionStore", return_value=store):
            result = sd.deliver_via_subprocess("T1", "instruction", "sonnet", "d-new")

        assert result.session_id == "fresh-session-xyz"
        assert store.load("T1") == "fresh-session-xyz"

    def test_session_id_not_saved_when_env_unset(self, monkeypatch, tmp_path):
        """Without VNX_SESSION_RESUME=1, session_id is NOT written to disk."""
        import subprocess_dispatch as sd
        from session_store import SessionStore

        monkeypatch.delenv("VNX_SESSION_RESUME", raising=False)
        store = SessionStore(state_dir=tmp_path)

        mock_adapter = _make_deliver_mocks("some-session-id")

        with patch("subprocess_dispatch.SubprocessAdapter", return_value=mock_adapter), \
             patch("subprocess_dispatch._inject_skill_context", return_value="instr"), \
             patch("subprocess_dispatch._inject_permission_profile", return_value="instr"), \
             patch("subprocess_dispatch._resolve_agent_cwd", return_value=None), \
             patch("subprocess_dispatch._write_manifest", return_value=Path("/tmp/m.json")), \
             patch("subprocess_dispatch._promote_manifest", return_value="/tmp/done.json"), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"):
            sd.deliver_via_subprocess("T1", "instruction", "sonnet", "d-test")

        # store was never used so it's still empty
        assert store.load("T1") is None
        assert not (tmp_path / "subprocess_sessions.json").exists()

    def test_no_crash_when_session_store_raises(self, monkeypatch):
        """SessionStore failures are non-fatal — deliver_via_subprocess still succeeds."""
        import subprocess_dispatch as sd

        monkeypatch.setenv("VNX_SESSION_RESUME", "1")

        mock_adapter = _make_deliver_mocks("session-id-ok")

        broken_store = MagicMock()
        broken_store.load.side_effect = RuntimeError("disk full")
        broken_store.save.side_effect = RuntimeError("disk full")

        with patch("subprocess_dispatch.SubprocessAdapter", return_value=mock_adapter), \
             patch("subprocess_dispatch._inject_skill_context", return_value="instr"), \
             patch("subprocess_dispatch._inject_permission_profile", return_value="instr"), \
             patch("subprocess_dispatch._resolve_agent_cwd", return_value=None), \
             patch("subprocess_dispatch._write_manifest", return_value=Path("/tmp/m.json")), \
             patch("subprocess_dispatch._promote_manifest", return_value="/tmp/done.json"), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"), \
             patch("session_store.SessionStore", return_value=broken_store):
            result = sd.deliver_via_subprocess("T1", "instruction", "sonnet", "d-test")

        # Delivery succeeds despite store failure
        assert result.success is True
