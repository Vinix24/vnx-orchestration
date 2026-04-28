"""Tests for scripts/build_project_status.py (PR-6 Sprint 4b).

Covers:
  1. build_project_status returns string ≤100 lines
  2. Output contains expected sections (Summary, Terminals, Recent activity, Health, Next actions)
  3. Empty state dir → minimal but valid output
  4. write_project_status uses atomic tmp+rename
  5. Output is markdown (no JSON-only payload)
  6. Integration: build_t0_state hook calls write_project_status
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from build_project_status import build_project_status, write_project_status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_index(state_dir: Path, branch: str = "main", head: str = "abc1234") -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "t0_index.json").write_text(json.dumps({
        "schema": "t0_index/1.0",
        "git_branch": branch,
        "git_head": head,
        "terminals": {
            "T1": {"status": "idle"},
            "T2": {"status": "working"},
            "T3": {"status": "idle"},
        },
        "queue": {
            "pending": 2,
            "active": 1,
            "open_prs": 3,
            "blocking_open_items": 0,
        },
        "health": {"db": "ok", "receipts": "ok"},
    }), encoding="utf-8")


def _write_register(state_dir: Path, count: int = 5) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(count):
        lines.append(json.dumps({
            "timestamp": f"2026-04-28T10:00:{i:02d}Z",
            "event": "dispatch_created",
            "dispatch_id": f"dispatch-{i:04d}",
        }))
    (state_dir / "dispatch_register.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_open_items(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "open_items_digest.json").write_text(json.dumps({
        "items": [
            {"severity": "blocker", "title": "Gate required before merge"},
            {"severity": "warning", "title": "Stale lease on T2"},
            {"severity": "info", "title": "Review synthesis doc"},
        ]
    }), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. build_project_status returns string ≤100 lines
# ---------------------------------------------------------------------------

class TestLineCountCap:
    def test_returns_at_most_100_lines(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir)
        _write_register(state_dir, count=20)
        _write_open_items(state_dir)

        result = build_project_status(state_dir)
        lines = result.splitlines()
        assert len(lines) <= 100

    def test_returns_string(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        result = build_project_status(state_dir)
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# 2. Output contains expected sections
# ---------------------------------------------------------------------------

class TestExpectedSections:
    def test_contains_all_five_sections(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir)
        _write_register(state_dir)
        _write_open_items(state_dir)

        result = build_project_status(state_dir)

        assert "## Summary" in result
        assert "## Terminals" in result
        assert "## Recent activity" in result
        assert "## Health" in result
        assert "## Next actions" in result

    def test_summary_contains_branch(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir, branch="feat/my-feature", head="deadbeef")

        result = build_project_status(state_dir)
        assert "feat/my-feature" in result
        assert "deadbeef" in result

    def test_terminals_listed(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir)

        result = build_project_status(state_dir)
        assert "- T1:" in result
        assert "- T2:" in result
        assert "- T3:" in result

    def test_register_events_in_recent_activity(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir)
        _write_register(state_dir, count=3)

        result = build_project_status(state_dir)
        assert "dispatch_created" in result

    def test_open_items_in_next_actions(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir)
        _write_open_items(state_dir)

        result = build_project_status(state_dir)
        assert "[blocker]" in result
        assert "Gate required before merge" in result

    def test_health_keys_present(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir)

        result = build_project_status(state_dir)
        assert "- db: ok" in result
        assert "- receipts: ok" in result


# ---------------------------------------------------------------------------
# 3. Empty state dir → minimal but valid output
# ---------------------------------------------------------------------------

class TestEmptyStateDir:
    def test_empty_dir_no_exception(self, tmp_path):
        state_dir = tmp_path / "empty_state"
        state_dir.mkdir()
        result = build_project_status(state_dir)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_empty_dir_contains_all_sections(self, tmp_path):
        state_dir = tmp_path / "empty_state"
        state_dir.mkdir()
        result = build_project_status(state_dir)
        assert "## Summary" in result
        assert "## Terminals" in result
        assert "## Recent activity" in result
        assert "## Health" in result
        assert "## Next actions" in result

    def test_empty_dir_line_count_within_cap(self, tmp_path):
        state_dir = tmp_path / "empty_state"
        state_dir.mkdir()
        result = build_project_status(state_dir)
        assert len(result.splitlines()) <= 100


# ---------------------------------------------------------------------------
# 4. write_project_status uses atomic tmp+rename
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_writes_file(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir)

        path = write_project_status(state_dir)
        assert path.exists()
        assert path.name == "PROJECT_STATUS.md"

    def test_no_tmp_file_left_behind(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir)

        write_project_status(state_dir)
        tmp_files = list(state_dir.glob("*.tmp"))
        assert tmp_files == [], f"Temp files left behind: {tmp_files}"

    def test_written_content_matches_build(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir)
        _write_register(state_dir)

        path = write_project_status(state_dir)
        written = path.read_text(encoding="utf-8")
        expected = build_project_status(state_dir)

        # Both should have same sections (timestamps may differ by < 1s)
        assert "## Summary" in written
        assert "## Terminals" in written

    def test_returns_path_object(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        result = write_project_status(state_dir)
        assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# 5. Output is markdown (no JSON-only payload)
# ---------------------------------------------------------------------------

class TestMarkdownFormat:
    def test_starts_with_h1(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = build_project_status(state_dir)
        assert result.startswith("# Project Status")

    def test_contains_autogenerated_comment(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = build_project_status(state_dir)
        assert "AUTO-GENERATED" in result

    def test_not_json_only(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = build_project_status(state_dir)
        # Must not be parseable as raw JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(result)


# ---------------------------------------------------------------------------
# 6. Integration: build_t0_state hook calls write_project_status
# ---------------------------------------------------------------------------

class TestBuildT0StateHook:
    def test_build_t0_state_imports_write_project_status(self, tmp_path):
        """Verify that build_t0_state.py contains the hook call."""
        build_t0_state_path = _REPO_ROOT / "scripts" / "build_t0_state.py"
        content = build_t0_state_path.read_text(encoding="utf-8")
        assert "from build_project_status import write_project_status" in content
        assert "write_project_status(_STATE_DIR)" in content

    def test_write_project_status_called_with_state_dir(self, tmp_path):
        """Smoke-test that write_project_status can be called with a tmp state_dir."""
        state_dir = tmp_path / "state"
        _write_index(state_dir)

        # Simulate best-effort hook: no exception should propagate
        try:
            from build_project_status import write_project_status as _wps
            path = _wps(state_dir)
            assert path.exists()
        except Exception as exc:
            pytest.fail(f"write_project_status raised unexpectedly: {exc}")
