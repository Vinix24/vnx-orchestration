"""Tests for Phase 6 P3 dual-write in dispatch_register.

Verifies that append_event writes to BOTH the per-project path AND the
central ~/.vnx-data/<project_id>/state/dispatch_register.ndjson path.
Per-project remains source-of-truth; central write is best-effort.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_register
from dispatch_register import append_event, read_events, _write_central_register, _central_register_path


@pytest.fixture(autouse=True)
def isolated_dirs(monkeypatch, tmp_path):
    data_dir = tmp_path / ".vnx-data"
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    return data_dir


def _reg_path(data_dir: Path) -> Path:
    return data_dir / "state" / "dispatch_register.ndjson"


class TestCentralRegisterPath:
    def test_returns_path_under_home_vnx_data(self, tmp_path, monkeypatch):
        central_home = tmp_path / "fake_home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))
        result = _central_register_path("vnx-dev")
        assert result is not None
        assert result.name == "dispatch_register.ndjson"
        assert "vnx-dev" in str(result)

    def test_returns_none_on_invalid_project_id(self):
        result = _central_register_path("INVALID-ID")
        assert result is None

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        central_home = tmp_path / "fake_home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))
        result = _central_register_path("vnx-dev")
        assert result is not None
        assert result.parent.is_dir()


class TestDualWrite:
    def test_per_project_path_always_written(self, isolated_dirs):
        ok = append_event("dispatch_created", dispatch_id="test-dispatch-001", terminal="T1")
        assert ok is True
        reg = _reg_path(isolated_dirs)
        assert reg.exists()
        lines = [l for l in reg.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "dispatch_created"

    def test_central_path_written_when_project_id_in_env(self, tmp_path, monkeypatch, isolated_dirs):
        central_home = tmp_path / "central_home"
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))

        ok = append_event("dispatch_created", dispatch_id="test-dispatch-002", terminal="T1")
        assert ok is True

        central_path = central_home / ".vnx-data" / "vnx-dev" / "state" / "dispatch_register.ndjson"
        assert central_path.exists(), "Central path should have been written"
        lines = [l for l in central_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "dispatch_created"

    def test_both_paths_have_same_record(self, tmp_path, monkeypatch, isolated_dirs):
        central_home = tmp_path / "central_home"
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))

        append_event("dispatch_started", dispatch_id="test-dispatch-003", terminal="T2")

        per_project = _reg_path(isolated_dirs)
        central_path = central_home / ".vnx-data" / "vnx-dev" / "state" / "dispatch_register.ndjson"

        per_record = json.loads(per_project.read_text().strip().splitlines()[-1])
        central_record = json.loads(central_path.read_text().strip().splitlines()[-1])

        assert per_record["event"] == central_record["event"]
        assert per_record["dispatch_id"] == central_record["dispatch_id"]

    def test_main_write_succeeds_when_central_fails(self, monkeypatch, isolated_dirs):
        # Simulate central write failure — per-project write must still succeed
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

        def _fail(*args, **kwargs):
            raise OSError("simulated central write failure")

        with patch.object(dispatch_register, "_write_central_register", side_effect=_fail):
            ok = append_event("dispatch_completed", dispatch_id="test-dispatch-004", terminal="T1")

        # Main write succeeded
        assert ok is True
        reg = _reg_path(isolated_dirs)
        assert reg.exists()

    def test_central_write_failure_does_not_break_return_value(self, tmp_path, monkeypatch, isolated_dirs):
        central_home = tmp_path / "no_permission_home"
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))
        # Make home unwriteable to trigger central path failure
        central_home.mkdir()
        central_home.chmod(0o555)
        try:
            ok = append_event("dispatch_failed", dispatch_id="test-dispatch-005", terminal="T1")
            assert ok is True
        finally:
            central_home.chmod(0o755)

    def test_write_central_register_noop_when_no_project_id(self, monkeypatch):
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        record = {"event": "dispatch_created", "dispatch_id": "test-x"}
        # Should not raise
        _write_central_register(record, None)

    def test_central_path_uses_project_id_from_record(self, tmp_path, monkeypatch, isolated_dirs):
        central_home = tmp_path / "central_home2"
        monkeypatch.setenv("VNX_PROJECT_ID", "mc")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))

        append_event(
            "gate_passed",
            dispatch_id="test-dispatch-006",
            gate="my-gate",
            project_id="mc",
        )

        central_path = central_home / ".vnx-data" / "mc" / "state" / "dispatch_register.ndjson"
        assert central_path.exists()

    def test_multiple_events_accumulate_in_both_paths(self, tmp_path, monkeypatch, isolated_dirs):
        central_home = tmp_path / "central_home3"
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))

        for i in range(3):
            append_event("dispatch_created", dispatch_id=f"batch-{i}", terminal="T1")

        per_project = _reg_path(isolated_dirs)
        central_path = central_home / ".vnx-data" / "vnx-dev" / "state" / "dispatch_register.ndjson"

        assert len(per_project.read_text().strip().splitlines()) == 3
        assert len(central_path.read_text().strip().splitlines()) == 3


class TestNoDoubleLogAtP5Cutover:
    """Regression: at P5 cutover, _resolve_register_path() points to central.
    append_event must write exactly once — not once via primary + once via mirror.
    """

    def test_single_record_when_primary_is_central(self, tmp_path, monkeypatch):
        """Simulate P5 cutover: route primary path to the central file."""
        central_home = tmp_path / "central_home"
        central_state = central_home / ".vnx-data" / "vnx-dev" / "state"
        central_state.mkdir(parents=True)
        central_reg = central_state / "dispatch_register.ndjson"

        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))

        # Make _resolve_register_path() return the central file directly,
        # simulating the P5 environment where per-project IS central.
        with patch.object(dispatch_register, "_resolve_register_path", return_value=central_reg):
            ok = append_event("dispatch_created", dispatch_id="p5-test-001", terminal="T1")

        assert ok is True
        lines = [l for l in central_reg.read_text().splitlines() if l.strip()]
        assert len(lines) == 1, (
            f"Expected exactly 1 record at P5 cutover (got {len(lines)}): double-log bug"
        )
        record = json.loads(lines[0])
        assert record["dispatch_id"] == "p5-test-001"

    def test_single_record_when_central_and_primary_differ(self, tmp_path, monkeypatch, isolated_dirs):
        """Normal dual-write: per-project ≠ central → both files get one record each."""
        central_home = tmp_path / "central_home"
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))

        ok = append_event("dispatch_created", dispatch_id="normal-dual-001", terminal="T1")
        assert ok is True

        per_project = _reg_path(isolated_dirs)
        central_path = central_home / ".vnx-data" / "vnx-dev" / "state" / "dispatch_register.ndjson"

        assert len(per_project.read_text().strip().splitlines()) == 1
        assert len(central_path.read_text().strip().splitlines()) == 1
