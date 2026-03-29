#!/usr/bin/env python3
"""
Tests for VNX tmux Session Profile — PR-3 declarative session layout model.

Covers:
  - PaneProfile / WindowProfile / SessionProfile construction
  - generate_session_profile from panes.json
  - save_session_profile / load_session_profile round-trip
  - profile_to_panes_json adapter-compatible output
  - verify_profile_integrity: correct / stale / missing detection
  - remap_pane_in_profile: update pane_id without touching identity
  - add_dynamic_window / remove_dynamic_window
  - save_profile_from_panes_json (start.sh integration path)
  - remap_pane (adapter-level panes.json update)
  - reheal_panes with mocked tmux output
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Resolve scripts/lib on the path
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts" / "lib"))

from tmux_session_profile import (
    HOME_TERMINALS,
    PROFILE_FILENAME,
    PaneProfile,
    ProfileDrift,
    SessionProfile,
    WindowProfile,
    add_dynamic_window,
    generate_session_profile,
    load_session_profile,
    profile_to_panes_json,
    remap_pane_in_profile,
    remove_dynamic_window,
    save_profile_from_panes_json,
    save_session_profile,
    verify_profile_integrity,
)
from tmux_adapter import RemapResult, remap_pane, reheal_panes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    return sd


@pytest.fixture
def sample_panes_json():
    return {
        "session": "vnx-test",
        "t0": {"pane_id": "%0", "role": "orchestrator", "do_not_target": True,
               "model": "opus", "provider": "claude_code"},
        "T0": {"pane_id": "%0", "role": "orchestrator", "do_not_target": True,
               "model": "opus", "provider": "claude_code"},
        "T1": {"pane_id": "%1", "track": "A", "model": "sonnet", "provider": "claude_code"},
        "T2": {"pane_id": "%2", "track": "B", "model": "sonnet", "provider": "claude_code"},
        "T3": {"pane_id": "%3", "track": "C", "model": "default", "role": "deep",
               "provider": "claude_code"},
        "tracks": {
            "A": {"pane_id": "%1", "track": "A", "model": "sonnet", "provider": "claude_code"},
            "B": {"pane_id": "%2", "track": "B", "model": "sonnet", "provider": "claude_code"},
            "C": {"pane_id": "%3", "track": "C", "model": "default", "provider": "claude_code"},
        },
    }


@pytest.fixture
def sample_profile(sample_panes_json):
    return generate_session_profile(
        session_name="vnx-test",
        panes_json=sample_panes_json,
        project_root="/projects/test",
    )


# ---------------------------------------------------------------------------
# generate_session_profile
# ---------------------------------------------------------------------------

class TestGenerateSessionProfile:
    def test_creates_home_window_with_four_panes(self, sample_panes_json):
        profile = generate_session_profile("vnx-test", sample_panes_json)
        assert profile.session_name == "vnx-test"
        assert profile.home_window.window_type == "home"
        assert profile.home_window.name == "main"
        assert len(profile.home_window.panes) == 4

    def test_terminal_ids_match_home_terminals(self, sample_panes_json):
        profile = generate_session_profile("vnx-test", sample_panes_json)
        ids = {p.terminal_id for p in profile.home_window.panes}
        assert ids == set(HOME_TERMINALS)

    def test_pane_ids_populated(self, sample_panes_json):
        profile = generate_session_profile("vnx-test", sample_panes_json)
        pane_map = {p.terminal_id: p.pane_id for p in profile.home_window.panes}
        assert pane_map["T0"] == "%0"
        assert pane_map["T1"] == "%1"
        assert pane_map["T2"] == "%2"
        assert pane_map["T3"] == "%3"

    def test_roles_assigned_correctly(self, sample_panes_json):
        profile = generate_session_profile("vnx-test", sample_panes_json)
        pane_map = {p.terminal_id: p for p in profile.home_window.panes}
        assert pane_map["T0"].role == "orchestrator"
        assert pane_map["T0"].track is None
        assert pane_map["T1"].role == "worker"
        assert pane_map["T1"].track == "A"
        assert pane_map["T2"].track == "B"
        assert pane_map["T3"].role == "deep"
        assert pane_map["T3"].track == "C"

    def test_work_dirs_derived_from_project_root(self, sample_panes_json):
        profile = generate_session_profile(
            "vnx-test", sample_panes_json, project_root="/projects/test"
        )
        pane_map = {p.terminal_id: p for p in profile.home_window.panes}
        assert pane_map["T0"].work_dir == "/projects/test/.claude/terminals/T0"
        assert pane_map["T2"].work_dir == "/projects/test/.claude/terminals/T2"

    def test_empty_panes_json_produces_empty_pane_ids(self):
        profile = generate_session_profile("vnx-empty", {})
        for pane in profile.home_window.panes:
            assert pane.pane_id == ""

    def test_no_dynamic_windows_by_default(self, sample_panes_json):
        profile = generate_session_profile("vnx-test", sample_panes_json)
        assert profile.dynamic_windows == []


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------

class TestSaveLoad:
    def test_round_trip_preserves_all_fields(self, state_dir, sample_profile):
        save_session_profile(sample_profile, state_dir)
        loaded = load_session_profile(state_dir)
        assert loaded is not None
        assert loaded.session_name == sample_profile.session_name
        assert loaded.schema_version == sample_profile.schema_version
        assert len(loaded.home_window.panes) == 4
        for orig, reloaded in zip(sample_profile.home_window.panes, loaded.home_window.panes):
            assert orig.terminal_id == reloaded.terminal_id
            assert orig.pane_id == reloaded.pane_id
            assert orig.work_dir == reloaded.work_dir
            assert orig.role == reloaded.role
            assert orig.track == reloaded.track

    def test_save_sets_updated_at(self, state_dir, sample_profile):
        sample_profile.updated_at = ""
        save_session_profile(sample_profile, state_dir)
        loaded = load_session_profile(state_dir)
        assert loaded.updated_at != ""

    def test_load_returns_none_when_file_absent(self, state_dir):
        result = load_session_profile(state_dir)
        assert result is None

    def test_load_returns_none_on_corrupt_json(self, state_dir):
        (state_dir / PROFILE_FILENAME).write_text("not json")
        result = load_session_profile(state_dir)
        assert result is None

    def test_dynamic_windows_preserved(self, state_dir, sample_profile):
        add_dynamic_window(sample_profile, "ops", "ops")
        save_session_profile(sample_profile, state_dir)
        loaded = load_session_profile(state_dir)
        assert len(loaded.dynamic_windows) == 1
        assert loaded.dynamic_windows[0].name == "ops"
        assert loaded.dynamic_windows[0].window_type == "ops"


# ---------------------------------------------------------------------------
# profile_to_panes_json
# ---------------------------------------------------------------------------

class TestProfileToPanesJson:
    def test_produces_terminal_entries(self, sample_profile):
        panes = profile_to_panes_json(sample_profile)
        for tid in HOME_TERMINALS:
            assert tid in panes
        assert "T0" in panes
        assert "t0" in panes  # lowercase alias

    def test_pane_ids_match(self, sample_profile):
        panes = profile_to_panes_json(sample_profile)
        assert panes["T1"]["pane_id"] == "%1"
        assert panes["T2"]["pane_id"] == "%2"

    def test_orchestrator_has_do_not_target(self, sample_profile):
        panes = profile_to_panes_json(sample_profile)
        assert panes["T0"].get("do_not_target") is True

    def test_tracks_section_present(self, sample_profile):
        panes = profile_to_panes_json(sample_profile)
        assert "tracks" in panes
        assert "A" in panes["tracks"]
        assert panes["tracks"]["A"]["pane_id"] == "%1"

    def test_session_name_in_output(self, sample_profile):
        panes = profile_to_panes_json(sample_profile)
        assert panes["session"] == "vnx-test"

    def test_work_dir_preserved(self, sample_panes_json):
        profile = generate_session_profile(
            "vnx-test", sample_panes_json, project_root="/projects/test"
        )
        panes = profile_to_panes_json(profile)
        assert panes["T1"]["work_dir"] == "/projects/test/.claude/terminals/T1"


# ---------------------------------------------------------------------------
# verify_profile_integrity
# ---------------------------------------------------------------------------

class TestVerifyProfileIntegrity:
    def _make_mock_panes(self, mapping: dict) -> str:
        """Build tmux list-panes output string."""
        return "\n".join(f"{pid} {path}" for pid, path in mapping.items())

    def test_all_correct_when_pane_ids_match(self, sample_profile):
        # Simulate live tmux having exactly the declared pane IDs
        live_output = self._make_mock_panes({
            "%0": "/projects/test/.claude/terminals/T0",
            "%1": "/projects/test/.claude/terminals/T1",
            "%2": "/projects/test/.claude/terminals/T2",
            "%3": "/projects/test/.claude/terminals/T3",
        })
        with patch("tmux_session_profile._list_live_panes") as mock_list:
            mock_list.return_value = {
                "%0": "/projects/test/.claude/terminals/T0",
                "%1": "/projects/test/.claude/terminals/T1",
                "%2": "/projects/test/.claude/terminals/T2",
                "%3": "/projects/test/.claude/terminals/T3",
            }
            drift = verify_profile_integrity(sample_profile, session_name="vnx-test")

        assert drift.is_clean
        assert set(drift.correct) == set(HOME_TERMINALS)
        assert drift.stale == []
        assert drift.missing == []

    def test_stale_pane_detected_when_id_changed(self, sample_panes_json):
        profile = generate_session_profile(
            "vnx-test", sample_panes_json, project_root="/projects/test"
        )
        # T1 pane_id changed from %1 to %5, but work_dir still matches
        with patch("tmux_session_profile._list_live_panes") as mock_list:
            mock_list.return_value = {
                "%0": "/projects/test/.claude/terminals/T0",
                "%5": "/projects/test/.claude/terminals/T1",  # T1 remapped
                "%2": "/projects/test/.claude/terminals/T2",
                "%3": "/projects/test/.claude/terminals/T3",
            }
            drift = verify_profile_integrity(profile, session_name="vnx-test")

        assert not drift.is_clean
        assert "T1" in drift.stale
        assert drift.remap_candidates["T1"] == "%5"
        assert "T1" not in drift.missing

    def test_missing_when_workdir_not_found(self, sample_panes_json):
        profile = generate_session_profile(
            "vnx-test", sample_panes_json, project_root="/projects/test"
        )
        # T2 is completely gone from live tmux
        with patch("tmux_session_profile._list_live_panes") as mock_list:
            mock_list.return_value = {
                "%0": "/projects/test/.claude/terminals/T0",
                "%1": "/projects/test/.claude/terminals/T1",
                "%3": "/projects/test/.claude/terminals/T3",
                # no T2 at all
            }
            drift = verify_profile_integrity(profile, session_name="vnx-test")

        assert "T2" in drift.missing
        assert "T2" not in drift.stale
        assert not drift.is_clean

    def test_empty_live_panes_marks_all_missing(self, sample_panes_json):
        profile = generate_session_profile(
            "vnx-test", sample_panes_json, project_root="/projects/test"
        )
        with patch("tmux_session_profile._list_live_panes") as mock_list:
            mock_list.return_value = {}
            drift = verify_profile_integrity(profile)

        assert set(drift.missing) == set(HOME_TERMINALS)
        assert drift.correct == []
        assert drift.stale == []


# ---------------------------------------------------------------------------
# remap_pane_in_profile
# ---------------------------------------------------------------------------

class TestRemapPaneInProfile:
    def test_remap_updates_pane_id(self, sample_profile):
        ok = remap_pane_in_profile(sample_profile, "T1", "%9")
        assert ok
        pane = sample_profile.get_pane("T1")
        assert pane.pane_id == "%9"

    def test_remap_does_not_change_identity(self, sample_profile):
        remap_pane_in_profile(sample_profile, "T2", "%8")
        pane = sample_profile.get_pane("T2")
        assert pane.terminal_id == "T2"
        assert pane.track == "B"
        assert pane.role == "worker"

    def test_remap_unknown_terminal_returns_false(self, sample_profile):
        ok = remap_pane_in_profile(sample_profile, "T9", "%9")
        assert not ok

    def test_remap_in_dynamic_window(self, sample_profile):
        win = add_dynamic_window(sample_profile, "ops", "ops")
        win.panes.append(PaneProfile(
            terminal_id="T0", role="orchestrator", pane_id="%10",
            work_dir="/projects/test/.claude/terminals/T0"
        ))
        ok = remap_pane_in_profile(sample_profile, "T0", "%15")
        # Should find in home_window first (T0 is in home)
        assert ok
        home_t0 = sample_profile.get_pane("T0")
        assert home_t0.pane_id == "%15"


# ---------------------------------------------------------------------------
# add_dynamic_window / remove_dynamic_window
# ---------------------------------------------------------------------------

class TestDynamicWindows:
    def test_add_new_window(self, sample_profile):
        win = add_dynamic_window(sample_profile, "ops", "ops")
        assert win.name == "ops"
        assert win.window_type == "ops"
        assert len(sample_profile.dynamic_windows) == 1

    def test_add_existing_returns_same_window(self, sample_profile):
        win1 = add_dynamic_window(sample_profile, "recovery", "recovery")
        win2 = add_dynamic_window(sample_profile, "recovery", "recovery")
        assert win1 is win2
        assert len(sample_profile.dynamic_windows) == 1

    def test_remove_existing_window(self, sample_profile):
        add_dynamic_window(sample_profile, "events", "events")
        removed = remove_dynamic_window(sample_profile, "events")
        assert removed
        assert len(sample_profile.dynamic_windows) == 0

    def test_remove_nonexistent_returns_false(self, sample_profile):
        removed = remove_dynamic_window(sample_profile, "nonexistent")
        assert not removed


# ---------------------------------------------------------------------------
# save_profile_from_panes_json (start.sh integration)
# ---------------------------------------------------------------------------

class TestSaveProfileFromPanesJson:
    def test_creates_profile_from_panes_json(self, state_dir, sample_panes_json):
        panes_path = state_dir / "panes.json"
        panes_path.write_text(json.dumps(sample_panes_json))
        profile = save_profile_from_panes_json(state_dir, "vnx-test", "/projects/test")
        assert profile.session_name == "vnx-test"
        assert len(profile.home_window.panes) == 4

    def test_profile_written_to_disk(self, state_dir, sample_panes_json):
        (state_dir / "panes.json").write_text(json.dumps(sample_panes_json))
        save_profile_from_panes_json(state_dir, "vnx-test")
        assert (state_dir / PROFILE_FILENAME).exists()

    def test_preserves_dynamic_windows_on_update(self, state_dir, sample_panes_json):
        (state_dir / "panes.json").write_text(json.dumps(sample_panes_json))
        # First save: creates profile
        profile = save_profile_from_panes_json(state_dir, "vnx-test")
        add_dynamic_window(profile, "ops", "ops")
        save_session_profile(profile, state_dir)
        # Second save: should preserve ops window
        profile2 = save_profile_from_panes_json(state_dir, "vnx-test")
        assert any(w.name == "ops" for w in profile2.dynamic_windows)

    def test_preserves_created_at_timestamp(self, state_dir, sample_panes_json):
        (state_dir / "panes.json").write_text(json.dumps(sample_panes_json))
        p1 = save_profile_from_panes_json(state_dir, "vnx-test")
        created_at = p1.created_at
        p2 = save_profile_from_panes_json(state_dir, "vnx-test")
        assert p2.created_at == created_at

    def test_works_with_absent_panes_json(self, state_dir):
        profile = save_profile_from_panes_json(state_dir, "vnx-empty")
        assert profile.session_name == "vnx-empty"
        assert len(profile.home_window.panes) == 4


# ---------------------------------------------------------------------------
# remap_pane (adapter-level panes.json update)
# ---------------------------------------------------------------------------

class TestAdapterRemapPane:
    def _write_panes(self, state_dir: Path, data: dict) -> Path:
        path = state_dir / "panes.json"
        path.write_text(json.dumps(data, indent=2))
        return path

    def _make_panes(self) -> dict:
        return {
            "session": "vnx-test",
            "T0": {"pane_id": "%0", "provider": "claude_code"},
            "T1": {"pane_id": "%1", "provider": "claude_code"},
            "T2": {"pane_id": "%2", "provider": "claude_code"},
            "T3": {"pane_id": "%3", "provider": "claude_code"},
            "tracks": {
                "A": {"pane_id": "%1", "track": "A"},
                "B": {"pane_id": "%2", "track": "B"},
                "C": {"pane_id": "%3", "track": "C"},
            },
        }

    def test_remap_updates_panes_json(self, state_dir):
        panes = self._make_panes()
        path = self._write_panes(state_dir, panes)
        ok = remap_pane("T1", "%9", path)
        assert ok
        reloaded = json.loads(path.read_text())
        assert reloaded["T1"]["pane_id"] == "%9"

    def test_remap_updates_tracks_entry(self, state_dir):
        panes = self._make_panes()
        path = self._write_panes(state_dir, panes)
        remap_pane("T1", "%9", path)
        reloaded = json.loads(path.read_text())
        assert reloaded["tracks"]["A"]["pane_id"] == "%9"

    def test_remap_unknown_terminal_returns_false(self, state_dir):
        panes = self._make_panes()
        path = self._write_panes(state_dir, panes)
        ok = remap_pane("T9", "%9", path)
        assert not ok

    def test_remap_with_nonexistent_panes_file(self, state_dir):
        ok = remap_pane("T1", "%9", state_dir / "nonexistent.json")
        assert not ok

    def test_remap_is_idempotent(self, state_dir):
        panes = self._make_panes()
        path = self._write_panes(state_dir, panes)
        remap_pane("T2", "%8", path)
        remap_pane("T2", "%8", path)
        reloaded = json.loads(path.read_text())
        assert reloaded["T2"]["pane_id"] == "%8"


# ---------------------------------------------------------------------------
# reheal_panes (adapter-level, with mocked tmux)
# ---------------------------------------------------------------------------

class TestRehealPanes:
    def _write_panes(self, state_dir: Path, data: dict) -> None:
        (state_dir / "panes.json").write_text(json.dumps(data, indent=2))

    def _make_panes_with_work_dirs(self, project_root: str) -> dict:
        return {
            "session": "vnx-test",
            "T0": {"pane_id": "%0", "provider": "claude_code",
                   "work_dir": f"{project_root}/.claude/terminals/T0"},
            "T1": {"pane_id": "%1", "provider": "claude_code",
                   "work_dir": f"{project_root}/.claude/terminals/T1"},
            "T2": {"pane_id": "%2", "provider": "claude_code",
                   "work_dir": f"{project_root}/.claude/terminals/T2"},
            "T3": {"pane_id": "%3", "provider": "claude_code",
                   "work_dir": f"{project_root}/.claude/terminals/T3"},
            "tracks": {},
        }

    def _mock_tmux_output(self, mapping: dict) -> str:
        return "\n".join(f"{pid} {path}" for pid, path in mapping.items())

    def test_all_unchanged_when_pane_ids_correct(self, state_dir, tmp_path):
        root = str(tmp_path)
        panes = self._make_panes_with_work_dirs(root)
        self._write_panes(state_dir, panes)
        live_output = self._mock_tmux_output({
            "%0": f"{root}/.claude/terminals/T0",
            "%1": f"{root}/.claude/terminals/T1",
            "%2": f"{root}/.claude/terminals/T2",
            "%3": f"{root}/.claude/terminals/T3",
        })
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=live_output)
            result = reheal_panes(state_dir, "vnx-test", root)

        assert result.remapped == []
        assert result.missing == []
        assert set(result.unchanged) == {"T0", "T1", "T2", "T3"}

    def test_stale_pane_remapped_by_workdir(self, state_dir, tmp_path):
        root = str(tmp_path)
        panes = self._make_panes_with_work_dirs(root)
        self._write_panes(state_dir, panes)
        # T1 pane_id changed to %5
        live_output = self._mock_tmux_output({
            "%0": f"{root}/.claude/terminals/T0",
            "%5": f"{root}/.claude/terminals/T1",
            "%2": f"{root}/.claude/terminals/T2",
            "%3": f"{root}/.claude/terminals/T3",
        })
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=live_output)
            result = reheal_panes(state_dir, "vnx-test", root)

        assert "T1" in result.remapped
        assert result.panes_json_updated
        # panes.json should reflect new pane_id
        reloaded = json.loads((state_dir / "panes.json").read_text())
        assert reloaded["T1"]["pane_id"] == "%5"

    def test_missing_terminal_reported(self, state_dir, tmp_path):
        root = str(tmp_path)
        panes = self._make_panes_with_work_dirs(root)
        self._write_panes(state_dir, panes)
        # T2 completely absent from live tmux
        live_output = self._mock_tmux_output({
            "%0": f"{root}/.claude/terminals/T0",
            "%1": f"{root}/.claude/terminals/T1",
            "%3": f"{root}/.claude/terminals/T3",
        })
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=live_output)
            result = reheal_panes(state_dir, "vnx-test", root)

        assert "T2" in result.missing
        assert "T2" not in result.remapped

    def test_tmux_unavailable_returns_all_missing(self, state_dir, tmp_path):
        root = str(tmp_path)
        panes = self._make_panes_with_work_dirs(root)
        self._write_panes(state_dir, panes)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = reheal_panes(state_dir, "vnx-test", root)

        # All pane_ids are stale because tmux returned no data
        assert set(result.missing) | set(result.unchanged) | set(result.remapped) == \
               {"T0", "T1", "T2", "T3"}

    def test_reheal_uses_project_root_for_workdir_derivation(self, state_dir, tmp_path):
        root = str(tmp_path)
        # panes.json WITHOUT explicit work_dir — should derive from project_root
        panes = {
            "session": "vnx-test",
            "T0": {"pane_id": "%0", "provider": "claude_code"},
            "T1": {"pane_id": "%1", "provider": "claude_code"},
            "T2": {"pane_id": "%2", "provider": "claude_code"},
            "T3": {"pane_id": "%3", "provider": "claude_code"},
            "tracks": {},
        }
        self._write_panes(state_dir, panes)
        # T1 remapped to %6, tmux reports work_dir via derived path
        live_output = self._mock_tmux_output({
            "%0": f"{root}/.claude/terminals/T0",
            "%6": f"{root}/.claude/terminals/T1",
            "%2": f"{root}/.claude/terminals/T2",
            "%3": f"{root}/.claude/terminals/T3",
        })
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=live_output)
            result = reheal_panes(state_dir, "vnx-test", root)

        assert "T1" in result.remapped


# ---------------------------------------------------------------------------
# get_pane / all_panes helpers
# ---------------------------------------------------------------------------

class TestProfileHelpers:
    def test_get_pane_returns_correct_pane(self, sample_profile):
        pane = sample_profile.get_pane("T2")
        assert pane is not None
        assert pane.terminal_id == "T2"

    def test_get_pane_unknown_returns_none(self, sample_profile):
        assert sample_profile.get_pane("T9") is None

    def test_all_panes_includes_home(self, sample_profile):
        all_panes = sample_profile.all_panes()
        tids = {p.terminal_id for p in all_panes}
        assert set(HOME_TERMINALS).issubset(tids)

    def test_all_panes_includes_dynamic(self, sample_profile):
        win = add_dynamic_window(sample_profile, "ops", "ops")
        win.panes.append(PaneProfile(
            terminal_id="T0-ops", role="orchestrator", pane_id="%10", work_dir=""
        ))
        all_panes = sample_profile.all_panes()
        tids = {p.terminal_id for p in all_panes}
        assert "T0-ops" in tids
