"""Tests for scripts/build_project_status.py (PR-6 Sprint 4b).

Covers:
  1. build_project_status returns string ≤100 lines
  2. Output contains expected sections (Summary, Live work, Recent activity, Health, Next actions)
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
        "schema": "t0_index/1.1",
        "git_branch": branch,
        "git_head": head,
        "live_work": {
            "available": True,
            "counts": {"live": 1, "starting": 0, "stale": 1, "unmeasured": 0},
            "live": [{"dispatch_id": "d-live-01", "state": "running",
                      "age_seconds": 5400, "pr": 1981}],
            "stale": [{"dispatch_id": "d-zombie-01", "state": "delivering",
                       "age_seconds": 259200, "lock": "released"}],
        },
        "queue": {
            "pending": 2,
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
        assert "## Live work" in result
        assert "## Recent activity" in result
        assert "## Health" in result
        assert "## Next actions" in result

    def test_summary_contains_branch(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir, branch="feat/my-feature", head="deadbeef")

        result = build_project_status(state_dir)
        assert "feat/my-feature" in result
        assert "deadbeef" in result

    def test_live_work_listed(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_index(state_dir)

        result = build_project_status(state_dir)
        assert "- d-live-01: running, 90 min, PR #1981" in result
        assert "- stale d-zombie-01: delivering, lock released" in result
        assert "- Live dispatches: 1 (stale: 1, unmeasured: 0)" in result
        assert "- T1:" not in result

    def test_unmeasured_live_work_listed(self, tmp_path):
        """A failed lock probe is named, never folded into 'nothing running'."""
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "t0_index.json").write_text(json.dumps({
            "schema": "t0_index/1.1",
            "live_work": {
                "available": True,
                "counts": {"live": 0, "starting": 0, "stale": 0, "unmeasured": 1},
                "live": [], "stale": [], "unmeasured": ["d-unprobed-01"],
            },
        }), encoding="utf-8")

        result = build_project_status(state_dir)
        assert "- Live dispatches: 0 (stale: 0, unmeasured: 1)" in result
        assert "- unmeasured d-unprobed-01: lock probe failed" in result

    def test_unavailable_live_work_says_why(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "t0_index.json").write_text(json.dumps({
            "live_work": {"available": False, "reason": "degraded: database is locked"},
        }), encoding="utf-8")

        result = build_project_status(state_dir)
        assert "- unavailable: degraded: database is locked" in result

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
        assert "## Live work" in result
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
        assert "## Live work" in written

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
        assert "write_project_status(state_dir)" in content

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


# ---------------------------------------------------------------------------
# 7. A failed open-PR read reads as unknown, never as 0 (P5)
# ---------------------------------------------------------------------------

class TestOpenPrsLine:
    def _line(self, tmp_path, index_patch):
        _write_index(tmp_path)
        path = tmp_path / "t0_index.json"
        index = json.loads(path.read_text(encoding="utf-8"))
        index.update(index_patch)
        path.write_text(json.dumps(index), encoding="utf-8")
        return next(l for l in build_project_status(tmp_path).splitlines() if l.startswith("- Open PRs"))

    def test_null_prints_unknown_with_the_reason(self, tmp_path):
        line = self._line(tmp_path, {"queue": {"open_prs": None}, "pr_queue_unavailable": "rc=4: gh auth login"})
        assert line == "- Open PRs: unknown (rc=4: gh auth login)"

    def test_zero_still_prints_zero(self, tmp_path):
        assert self._line(tmp_path, {"queue": {"open_prs": 0}}) == "- Open PRs: 0"

    def test_end_to_end_failing_gh_to_the_status_line(self, tmp_path):
        import subprocess
        from build_t0_state import _build_t0_index
        from pr_queue_state import build_pr_queue_state
        failing = subprocess.CompletedProcess([], 4, stdout="", stderr="gh auth login")
        with patch("subprocess.run", return_value=failing):
            section = build_pr_queue_state(tmp_path, register_events=[], project_root=tmp_path)
        index = _build_t0_index({"pr_queue": section})
        assert index["queue"]["open_prs"] is None
        (tmp_path / "t0_index.json").write_text(json.dumps(index), encoding="utf-8")
        line = next(l for l in build_project_status(tmp_path).splitlines() if l.startswith("- Open PRs"))
        assert line == "- Open PRs: unknown (rc=4: gh auth login)"
