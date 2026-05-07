"""Dual-write and central-mirror-skip tests for dispatch_register (Phase 6 P3)."""

from __future__ import annotations

import json
import sys
import os
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_register
from dispatch_register import append_event, read_events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch, tmp_path):
    """Route primary register I/O into a fresh tmp dir."""
    state_dir = tmp_path / ".vnx-data" / "state"
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    # Clear project_id so mirror doesn't find ambient identity.
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.delenv("VNX_OPERATOR_ID", raising=False)
    return tmp_path


def _reg_path(data_dir: Path) -> Path:
    return data_dir / ".vnx-data" / "state" / "dispatch_register.ndjson"


def _central_reg_path(central_base: Path, project_id: str) -> Path:
    return central_base / project_id / "state" / "dispatch_register.ndjson"


# ---------------------------------------------------------------------------
# Dual-write: event lands in both primary and central when project_id known
# ---------------------------------------------------------------------------

class TestDualWrite:
    def test_primary_path_written(self, isolated_data_dir):
        result = append_event("dispatch_created", dispatch_id="d-001", project_id="vnx-dev")
        assert result is True
        assert _reg_path(isolated_data_dir).exists()

    def test_central_path_written_when_project_id_provided(self, monkeypatch, isolated_data_dir, tmp_path):
        central_base = tmp_path / "home_vnx"
        from unittest.mock import patch

        def _patched_resolve(pid):
            return central_base / pid

        with patch.object(dispatch_register, "_resolve_central_data_dir", _patched_resolve):
            append_event("dispatch_created", dispatch_id="d-002", project_id="proj-x")

        central_path = central_base / "proj-x" / "state" / "dispatch_register.ndjson"
        assert central_path.exists(), "central path not written"
        rec = json.loads(central_path.read_text().strip())
        assert rec["dispatch_id"] == "d-002"

    def test_central_event_has_envelope_fields(self, monkeypatch, isolated_data_dir, tmp_path):
        from unittest.mock import patch

        central_base = tmp_path / "home_vnx"

        def _patched_resolve(pid):
            return central_base / pid

        with patch.object(dispatch_register, "_resolve_central_data_dir", _patched_resolve):
            append_event(
                "gate_passed",
                dispatch_id="d-envelope",
                project_id="vnx-dev",
                operator_id="op-x",
            )

        central_path = central_base / "vnx-dev" / "state" / "dispatch_register.ndjson"
        rec = json.loads(central_path.read_text().strip())
        assert rec["project_id"] == "vnx-dev"
        assert rec["operator_id"] == "op-x"


# ---------------------------------------------------------------------------
# Mirror-skip: when primary == central, no double-write
# ---------------------------------------------------------------------------

class TestCentralMirrorSkip:
    def test_no_double_write_when_primary_is_central(self, monkeypatch, isolated_data_dir, tmp_path):
        """Critical fix: when primary path resolves to the same file as central, skip mirror."""
        from unittest.mock import patch

        # Point central at the SAME directory as the primary state dir.
        primary_state = isolated_data_dir / ".vnx-data" / "state"
        primary_state.mkdir(parents=True, exist_ok=True)

        def _patched_resolve(pid):
            # central "happens to be" the primary state dir's parent
            return isolated_data_dir / ".vnx-data"

        with patch.object(dispatch_register, "_resolve_central_data_dir", _patched_resolve):
            result = append_event("dispatch_created", dispatch_id="d-skip", project_id="vnx-dev")

        assert result is True
        # Only one line in primary (the mirror skipped because central == primary).
        reg = _reg_path(isolated_data_dir)
        assert reg.exists()
        lines = [l for l in reg.read_text().splitlines() if l.strip()]
        assert len(lines) == 1, f"Expected 1 line (no double-write), got {len(lines)}"

    def test_mirror_skipped_when_project_id_absent(self, monkeypatch, isolated_data_dir, tmp_path):
        """No project_id means no central mirror attempt."""
        written_paths = []

        original_write = dispatch_register._write_event_locked

        def tracking_write(path, record):
            written_paths.append(path)
            return original_write(path, record)

        monkeypatch.setattr(dispatch_register, "_write_event_locked", tracking_write)

        # No project_id in record AND env cleared by fixture.
        append_event("dispatch_created", dispatch_id="d-no-pid")

        # Only the primary path was written.
        assert len(written_paths) == 1


# ---------------------------------------------------------------------------
# State_dir override: read_events respects state_dir arg for central lookup
# ---------------------------------------------------------------------------

class TestStateDirOverride:
    def test_read_events_uses_passed_state_dir(self, isolated_data_dir, tmp_path):
        """read_events(state_dir=X) reads from X, not from the ambient VNX_STATE_DIR."""
        alt_state = tmp_path / "alt_state"
        alt_state.mkdir(parents=True)
        reg = alt_state / "dispatch_register.ndjson"
        reg.write_text(
            json.dumps({"timestamp": "2026-01-01T00:00:00.000000Z",
                        "event": "dispatch_created", "dispatch_id": "alt-d-001"}) + "\n"
        )

        events = read_events(state_dir=alt_state)
        assert len(events) == 1
        assert events[0]["dispatch_id"] == "alt-d-001"

    def test_read_events_no_central_cross_contamination(self, monkeypatch, isolated_data_dir, tmp_path):
        """When state_dir is a test dir that happens to have NO central twin, no extra events appear."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        test_state = tmp_path / "test_state"
        test_state.mkdir(parents=True)
        (test_state / "dispatch_register.ndjson").write_text(
            json.dumps({"timestamp": "2026-05-01T00:00:00.000000Z",
                        "event": "dispatch_created", "dispatch_id": "isolated-d"}) + "\n"
        )

        events = read_events(state_dir=test_state)
        assert len(events) == 1
        assert events[0]["dispatch_id"] == "isolated-d"


# ---------------------------------------------------------------------------
# Merge-read deduplication: central wins over primary
# ---------------------------------------------------------------------------

class TestMergeReadCentralWins:
    def test_central_record_overwrites_primary_on_same_key(self, monkeypatch, isolated_data_dir, tmp_path):
        """When central and primary share the same (ts, event, dispatch_id), central wins."""
        ts = "2026-05-01T12:00:00.000000Z"
        primary_state = tmp_path / "primary_state"
        primary_state.mkdir(parents=True)
        (primary_state / "dispatch_register.ndjson").write_text(
            json.dumps({"timestamp": ts, "event": "dispatch_created",
                        "dispatch_id": "d-dup", "source": "primary"}) + "\n"
        )

        central_base = tmp_path / "central"
        central_state = central_base / "test-merge" / "state"
        central_state.mkdir(parents=True)
        (central_state / "dispatch_register.ndjson").write_text(
            json.dumps({"timestamp": ts, "event": "dispatch_created",
                        "dispatch_id": "d-dup", "source": "central",
                        "project_id": "test-merge"}) + "\n"
        )

        def _patched_resolve(pid):
            return central_base / pid

        monkeypatch.setenv("VNX_PROJECT_ID", "test-merge")

        with patch.object(dispatch_register, "_resolve_central_data_dir", _patched_resolve):
            events = read_events(state_dir=primary_state)

        assert len(events) == 1
        assert events[0]["source"] == "central"


# ---------------------------------------------------------------------------
# Helper — not a test
# ---------------------------------------------------------------------------

def _make_patched_home(original_path_class, central_base):
    """Not needed for all tests; some tests patch resolve_central_data_dir directly."""
    return original_path_class
