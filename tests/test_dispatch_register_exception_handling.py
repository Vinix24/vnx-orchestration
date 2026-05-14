"""Tests for narrowed silent-except handling in dispatch_register.py (OI-1437).

Covers:
  1. test_runs_clean_on_default_env  — module imports and append_event returns True
     in a clean isolated environment; no unexpected exceptions bubble up.
  2. test_corrupt_state_logs_warning — corrupt NDJSON lines are skipped and a
     debug message is emitted rather than silently swallowed without trace.
"""

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_register
from dispatch_register import append_event, read_events


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Route all register I/O into a fresh tmp dir; disable central-DB path."""
    state_dir = tmp_path / ".vnx-data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    return state_dir


# ---------------------------------------------------------------------------
# 1. Clean default env — module is importable and append works without raising
# ---------------------------------------------------------------------------

class TestRunsCleanOnDefaultEnv:
    def test_append_returns_true_for_valid_event(self):
        """append_event succeeds in a clean environment and does not raise."""
        result = append_event(
            "dispatch_created",
            dispatch_id="test-clean-env-001",
            terminal="T1",
            feature_id="f99",
        )
        assert result is True

    def test_read_events_returns_appended_record(self):
        """read_events finds the record written by append_event."""
        append_event(
            "dispatch_created",
            dispatch_id="test-clean-read-001",
            terminal="T1",
            feature_id="f99",
        )
        events = read_events()
        ids = [e.get("dispatch_id") for e in events]
        assert "test-clean-read-001" in ids

    def test_invalid_event_returns_false_without_raising(self):
        """append_event returns False for unknown events and does not raise."""
        result = append_event("not_a_valid_event", dispatch_id="x")
        assert result is False

    def test_append_without_identifying_field_returns_false(self):
        """append_event returns False when no identifying field is provided."""
        result = append_event("dispatch_created")
        assert result is False


# ---------------------------------------------------------------------------
# 2. Corrupt state — debug message emitted, not silently swallowed
# ---------------------------------------------------------------------------

class TestCorruptStateLogsWarning:
    def test_corrupt_ndjson_lines_are_skipped_by_read_events(self, isolated_env):
        """read_events skips corrupt NDJSON and still returns valid records."""
        reg_file = isolated_env / "dispatch_register.ndjson"
        good_record = json.dumps({
            "timestamp": "2026-01-01T00:00:00Z",
            "event": "dispatch_created",
            "dispatch_id": "good-001",
        })
        reg_file.write_text(
            "not valid json at all\n"
            + good_record + "\n"
            + '{"broken": }\n',
            encoding="utf-8",
        )
        events = read_events()
        assert len(events) == 1
        assert events[0]["dispatch_id"] == "good-001"

    def test_oserror_on_central_resolution_logs_debug(self, monkeypatch, caplog):
        """OSError during central path resolution is logged at DEBUG, not silently dropped."""
        # project_id must be non-empty to enter the central-read block in read_events
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        with caplog.at_level(logging.DEBUG, logger="dispatch_register"):
            with patch.object(
                dispatch_register,
                "_resolve_central_data_dir",
                side_effect=OSError("simulated central path failure"),
            ):
                events = read_events()
                assert isinstance(events, list)

        debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("Central register read skipped" in m for m in debug_messages)

    def test_import_error_on_central_resolution_logs_debug(self, monkeypatch, caplog):
        """ImportError from _resolve_central_data_dir is logged at DEBUG, not silently dropped."""
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        with caplog.at_level(logging.DEBUG, logger="dispatch_register"):
            with patch.object(
                dispatch_register,
                "_resolve_central_data_dir",
                side_effect=ImportError("vnx_paths unavailable"),
            ):
                events = read_events()
                assert isinstance(events, list)

        debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("Central register read skipped" in m for m in debug_messages)

    def test_project_id_from_state_dir_oserror_logs_debug(self, caplog):
        """OSError in _project_id_from_state_dir is logged at DEBUG and returns empty string."""
        with caplog.at_level(logging.DEBUG, logger="dispatch_register"):
            bad_path = MagicMock(spec=Path)
            bad_path.resolve.side_effect = OSError("simulated resolve failure")
            result = dispatch_register._project_id_from_state_dir(bad_path)

        assert result == ""
        debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("_project_id_from_state_dir" in m for m in debug_messages)
