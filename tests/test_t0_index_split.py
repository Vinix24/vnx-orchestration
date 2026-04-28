"""Tests for _build_t0_index() and _write_detail_files() in build_t0_state.py (PR-5).

Covers:
  1. _build_t0_index returns ≤50 keys at top level
  2. Index has all required fields (schema, timestamp, terminals, queue, recent_receipts)
  3. _write_detail_files creates t0_detail/ with per-section files
  4. Detail files contain exactly the section content
  5. Index size in bytes <5KB (cheap-load constraint)
  6. Detail files NOT loaded when index is loaded (separate files, separate read paths)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from build_t0_state import _build_t0_index, _write_detail_files, build_t0_state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_full_state(
    *,
    branch: str = "main",
    head_commit: str = "abc1234 some message",
    terminal_count: int = 3,
    pending: int = 2,
    active: int = 1,
    blocker_count: int = 5,
    receipts_count: int = 5,
) -> dict:
    terminals = {
        f"T{i}": {"status": "idle", "lease_expires_at": None, "track": "A"}
        for i in range(1, terminal_count + 1)
    }
    receipts = [
        {
            "terminal": f"T{i}",
            "status": "ok",
            "event_type": "task_complete",
            "timestamp": f"2026-04-28T00:00:0{i}Z",
            "dispatch_id": f"d-{i:04d}",
            "gate": None,
        }
        for i in range(receipts_count)
    ]
    return {
        "schema_version": "2.0",
        "generated_at": "2026-04-28T12:00:00+00:00",
        "staleness_seconds": 0,
        "git_context": {
            "branch": branch,
            "last_5_commits": [head_commit, "older1 msg", "older2 msg"],
            "uncommitted_changes": False,
        },
        "terminals": terminals,
        "queues": {
            "pending_count": pending,
            "active_count": active,
            "completed_last_hour": 3,
            "conflict_count": 0,
        },
        "pr_progress": {
            "in_progress": ["PR-4", "PR-5"],
            "total": 8,
            "completed": 6,
        },
        "open_items": {
            "open_count": 12,
            "blocker_count": blocker_count,
            "top_blockers": [{"id": "OI-1", "title": "Critical bug"}],
        },
        "active_work": [
            {"dispatch_id": "d-active-01", "track": "A", "gate": "codex"},
        ],
        "recent_receipts": receipts,
        "feature_state": {
            "source": "dispatch_register",
            "dispatches": {"d-001": {"status": "completed"}},
            "pr_status": {},
            "feature_status": {},
            "register_event_count": 5,
        },
        "quality_digest": {
            "operational_defects": 2,
            "total_items": 10,
            "generated_at": "2026-04-28T11:00:00Z",
        },
        "dispatch_register_events": [
            {"event": "dispatch_created", "dispatch_id": "d-001", "timestamp": "2026-04-28T10:00:00Z"}
        ],
        "system_health": {
            "status": "healthy",
            "db_initialized": True,
            "uptime_seconds": 3600,
        },
        "_build_seconds": 1.42,
    }


# ---------------------------------------------------------------------------
# 1. _build_t0_index returns ≤50 keys at top level
# ---------------------------------------------------------------------------

class TestIndexKeyCount:
    def test_top_level_keys_at_most_50(self):
        state = _make_full_state()
        index = _build_t0_index(state)
        assert len(index) <= 50, (
            f"Index has {len(index)} top-level keys, expected ≤50; keys: {list(index)}"
        )

    def test_empty_state_still_within_50(self):
        index = _build_t0_index({})
        assert len(index) <= 50


# ---------------------------------------------------------------------------
# 2. Index has all required fields
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("schema", "timestamp", "terminals", "queue", "recent_receipts",
                    "git_branch", "git_head", "active_dispatches", "health",
                    "last_rebuild_seconds")


class TestIndexRequiredFields:
    def test_all_required_fields_present(self):
        index = _build_t0_index(_make_full_state())
        missing = [f for f in _REQUIRED_FIELDS if f not in index]
        assert not missing, f"Missing required fields: {missing}"

    def test_schema_value(self):
        index = _build_t0_index(_make_full_state())
        assert index["schema"] == "t0_index/1.0"

    def test_timestamp_from_generated_at(self):
        state = _make_full_state()
        index = _build_t0_index(state)
        assert index["timestamp"] == state["generated_at"]

    def test_git_branch(self):
        state = _make_full_state(branch="feat/my-feature")
        index = _build_t0_index(state)
        assert index["git_branch"] == "feat/my-feature"

    def test_git_head_7_chars(self):
        state = _make_full_state(head_commit="abc1234 some commit message")
        index = _build_t0_index(state)
        assert index["git_head"] == "abc1234"

    def test_terminals_status_present(self):
        index = _build_t0_index(_make_full_state())
        for tid, tdata in index["terminals"].items():
            assert "status" in tdata, f"Terminal {tid} missing 'status'"
            assert "lease_expires" in tdata, f"Terminal {tid} missing 'lease_expires'"

    def test_queue_has_four_subfields(self):
        index = _build_t0_index(_make_full_state())
        q = index["queue"]
        for field in ("pending", "active", "open_prs", "blocking_open_items"):
            assert field in q, f"queue missing '{field}'"

    def test_queue_values_correct(self):
        state = _make_full_state(pending=3, active=2, blocker_count=7)
        index = _build_t0_index(state)
        assert index["queue"]["pending"] == 3
        assert index["queue"]["active"] == 2
        assert index["queue"]["blocking_open_items"] == 7

    def test_recent_receipts_at_most_3(self):
        state = _make_full_state(receipts_count=10)
        index = _build_t0_index(state)
        assert len(index["recent_receipts"]) <= 3

    def test_active_dispatches_list(self):
        index = _build_t0_index(_make_full_state())
        assert isinstance(index["active_dispatches"], list)
        assert "d-active-01" in index["active_dispatches"]

    def test_last_rebuild_seconds(self):
        state = _make_full_state()
        index = _build_t0_index(state)
        assert index["last_rebuild_seconds"] == pytest.approx(1.42)

    def test_empty_state_no_crash(self):
        index = _build_t0_index({})
        assert index["schema"] == "t0_index/1.0"
        assert index["terminals"] == {}
        assert index["active_dispatches"] == []
        assert index["recent_receipts"] == []


# ---------------------------------------------------------------------------
# 3. _write_detail_files creates t0_detail/ with per-section files
# ---------------------------------------------------------------------------

class TestWriteDetailFilesCreation:
    def test_creates_detail_dir(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        assert not detail_dir.exists()
        _write_detail_files(_make_full_state(), detail_dir)
        assert detail_dir.is_dir()

    def test_creates_feature_state_file(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        _write_detail_files(_make_full_state(), detail_dir)
        assert (detail_dir / "feature_state.json").exists()

    def test_creates_quality_digest_file(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        _write_detail_files(_make_full_state(), detail_dir)
        assert (detail_dir / "quality_digest.json").exists()

    def test_creates_open_items_file(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        _write_detail_files(_make_full_state(), detail_dir)
        assert (detail_dir / "open_items.json").exists()

    def test_creates_dispatch_register_file(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        _write_detail_files(_make_full_state(), detail_dir)
        assert (detail_dir / "dispatch_register.json").exists()

    def test_skips_missing_sections(self, tmp_path):
        state = {"feature_state": {"source": "register"}}
        detail_dir = tmp_path / "t0_detail"
        manifest = _write_detail_files(state, detail_dir)
        assert "feature_state" in manifest
        assert "quality_digest" not in manifest

    def test_returns_manifest_with_paths(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        manifest = _write_detail_files(_make_full_state(), detail_dir)
        assert isinstance(manifest, dict)
        for key, path_str in manifest.items():
            assert Path(path_str).exists(), f"Manifest path for {key} does not exist: {path_str}"

    def test_no_tmp_files_left_behind(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        _write_detail_files(_make_full_state(), detail_dir)
        tmp_files = list(detail_dir.glob("*.tmp.*"))
        assert tmp_files == [], f"Leftover tmp files: {tmp_files}"


# ---------------------------------------------------------------------------
# 4. Detail files contain exactly the section content
# ---------------------------------------------------------------------------

class TestDetailFileContent:
    def test_feature_state_content_matches(self, tmp_path):
        state = _make_full_state()
        detail_dir = tmp_path / "t0_detail"
        _write_detail_files(state, detail_dir)
        on_disk = json.loads((detail_dir / "feature_state.json").read_text())
        assert on_disk == state["feature_state"]

    def test_quality_digest_content_matches(self, tmp_path):
        state = _make_full_state()
        detail_dir = tmp_path / "t0_detail"
        _write_detail_files(state, detail_dir)
        on_disk = json.loads((detail_dir / "quality_digest.json").read_text())
        assert on_disk == state["quality_digest"]

    def test_open_items_content_matches(self, tmp_path):
        state = _make_full_state()
        detail_dir = tmp_path / "t0_detail"
        _write_detail_files(state, detail_dir)
        on_disk = json.loads((detail_dir / "open_items.json").read_text())
        assert on_disk == state["open_items"]

    def test_dispatch_register_content_matches(self, tmp_path):
        state = _make_full_state()
        detail_dir = tmp_path / "t0_detail"
        _write_detail_files(state, detail_dir)
        on_disk = json.loads((detail_dir / "dispatch_register.json").read_text())
        assert on_disk == state["dispatch_register_events"]


# ---------------------------------------------------------------------------
# 5. Index size in bytes <5KB
# ---------------------------------------------------------------------------

class TestIndexSizeConstraint:
    def test_index_under_5kb(self):
        state = _make_full_state(receipts_count=10, terminal_count=3)
        index = _build_t0_index(state)
        serialized = json.dumps(index, indent=2, default=str).encode("utf-8")
        assert len(serialized) < 5 * 1024, (
            f"Index is {len(serialized)} bytes, expected < 5120"
        )

    def test_index_written_to_disk_under_5kb(self, tmp_path):
        state = _make_full_state(receipts_count=10)
        index_path = tmp_path / "t0_index.json"
        index_path.write_text(
            json.dumps(_build_t0_index(state), indent=2, default=str),
            encoding="utf-8",
        )
        assert index_path.stat().st_size < 5 * 1024, (
            f"Index file is {index_path.stat().st_size} bytes, expected < 5120"
        )


# ---------------------------------------------------------------------------
# 6. Detail files NOT loaded when index is loaded (separate read paths)
# ---------------------------------------------------------------------------

class TestIndexDetailSeparation:
    def test_index_file_does_not_contain_feature_state_data(self, tmp_path):
        state = _make_full_state()
        index = _build_t0_index(state)
        index_text = json.dumps(index, indent=2, default=str)
        # The deep register event count should only appear in feature_state detail
        assert "register_event_count" not in index_text

    def test_index_file_does_not_contain_dispatch_register_events(self, tmp_path):
        state = _make_full_state()
        index = _build_t0_index(state)
        index_text = json.dumps(index, indent=2, default=str)
        # dispatch_register_events content (event key) only appears in detail file
        assert "dispatch_created" not in index_text

    def test_index_and_detail_are_separate_files(self, tmp_path):
        state = _make_full_state()
        index = _build_t0_index(state)
        detail_dir = tmp_path / "t0_detail"
        manifest = _write_detail_files(state, detail_dir)

        # Index read path: only reads the index dict (no detail sections)
        index_keys = set(index.keys())
        assert "feature_state" not in index_keys
        assert "quality_digest" not in index_keys
        assert "dispatch_register_events" not in index_keys

        # Detail read path: only reads section files
        detail_keys = set(manifest.keys())
        assert "schema" not in detail_keys
        assert "terminals" not in detail_keys
        assert "queue" not in detail_keys

    def test_reading_index_without_detail_dir_is_possible(self, tmp_path):
        state = _make_full_state()
        index = _build_t0_index(state)
        # Index builds without needing t0_detail/ to exist
        assert index["schema"] == "t0_index/1.0"
        assert not (tmp_path / "t0_detail").exists()


# ---------------------------------------------------------------------------
# Integration: build_t0_state produces state compatible with both functions
# ---------------------------------------------------------------------------

class TestIntegrationWithBuildT0State:
    def test_build_t0_state_output_feeds_index(self, tmp_path):
        state_dir = tmp_path / "state"
        dispatch_dir = tmp_path / "dispatches"
        state_dir.mkdir(parents=True)
        for sub in ("pending", "active", "conflicts"):
            (dispatch_dir / sub).mkdir(parents=True)

        state = build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)
        index = _build_t0_index(state)

        assert index["schema"] == "t0_index/1.0"
        assert len(index) <= 50
        serialized = json.dumps(index, indent=2, default=str).encode("utf-8")
        assert len(serialized) < 5 * 1024

    def test_build_t0_state_output_feeds_detail_files(self, tmp_path):
        state_dir = tmp_path / "state"
        dispatch_dir = tmp_path / "dispatches"
        state_dir.mkdir(parents=True)
        for sub in ("pending", "active", "conflicts"):
            (dispatch_dir / sub).mkdir(parents=True)

        state = build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)
        detail_dir = tmp_path / "t0_detail"
        manifest = _write_detail_files(state, detail_dir)

        # At minimum feature_state should always be written (fallback returns dict)
        assert isinstance(manifest, dict)
        for key, path_str in manifest.items():
            assert Path(path_str).exists(), f"{key} detail file not found: {path_str}"
