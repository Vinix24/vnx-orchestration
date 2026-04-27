"""Regression tests for _scan_dispatches deduplication logic.

Covers:
  1. Same dispatch in completed/ AND rejected/ → returns exactly 1 entry (done)
  2. Same dispatch in pending/ AND done/ → returns only pending (higher priority)
  3. Same dispatch in rejected/ AND done/ → returns done (higher priority)
  4. No duplicates → passes through unchanged
  5. Stage sort order preserved (most-recent first within stage)
"""

import sys
import time
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = VNX_ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))


@pytest.fixture()
def dispatch_env(tmp_path, monkeypatch):
    """Set up a temp dispatches directory and patch module-level paths."""
    dispatches_dir = tmp_path / "dispatches"
    reports_dir = tmp_path / "unified_reports"
    dispatches_dir.mkdir()
    reports_dir.mkdir()

    import api_operator
    monkeypatch.setattr(api_operator, "DISPATCHES_DIR", dispatches_dir)
    monkeypatch.setattr(api_operator, "REPORTS_DIR", reports_dir)

    return dispatches_dir


def _write_dispatch_md(directory: Path, dispatch_id: str, extra_header: str = "") -> Path:
    """Write a minimal dispatch markdown file to directory/<dispatch_id>.md."""
    directory.mkdir(parents=True, exist_ok=True)
    content = (
        "[[TARGET:A]]\n"
        f"Dispatch-ID: {dispatch_id}\n"
        "Track: A\n"
        "Terminal: T1\n"
        "Role: backend-developer\n"
        "Gate: test-gate\n"
        f"{extra_header}"
        "\n## Context\nTest dispatch.\n"
    )
    path = directory / f"{dispatch_id}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Test 1: completed/ AND rejected/ → exactly 1 entry with stage='done'
# ---------------------------------------------------------------------------

def test_dedup_completed_and_rejected(dispatch_env):
    """Dispatch in both completed/ and rejected/ must yield exactly 1 done entry."""
    dispatch_id = "20260407-030002-f36-refactor-B"
    _write_dispatch_md(dispatch_env / "completed", dispatch_id)
    _write_dispatch_md(dispatch_env / "rejected", dispatch_id)

    import api_operator
    result = api_operator._scan_dispatches()

    # Count total entries with this id
    all_entries = []
    for stage_entries in result["stages"].values():
        all_entries.extend(e for e in stage_entries if e["id"] == dispatch_id)

    assert len(all_entries) == 1, (
        f"Expected 1 entry for {dispatch_id}, got {len(all_entries)}: "
        f"{[(e['stage'], e['dir']) for e in all_entries]}"
    )
    assert all_entries[0]["stage"] == "done", (
        f"Expected stage='done', got '{all_entries[0]['stage']}'"
    )


# ---------------------------------------------------------------------------
# Test 2: pending/ AND done/ → returns only pending (higher priority)
# ---------------------------------------------------------------------------

def test_dedup_pending_beats_done(dispatch_env):
    """Dispatch in both pending/ and completed/ must yield exactly 1 pending entry."""
    dispatch_id = "20260427-000001-test-pending-priority"
    _write_dispatch_md(dispatch_env / "pending", dispatch_id)
    _write_dispatch_md(dispatch_env / "completed", dispatch_id)

    import api_operator
    result = api_operator._scan_dispatches()

    all_entries = []
    for stage_entries in result["stages"].values():
        all_entries.extend(e for e in stage_entries if e["id"] == dispatch_id)

    assert len(all_entries) == 1, (
        f"Expected 1 entry for {dispatch_id}, got {len(all_entries)}"
    )
    assert all_entries[0]["stage"] == "pending", (
        f"Expected stage='pending', got '{all_entries[0]['stage']}'"
    )


# ---------------------------------------------------------------------------
# Test 3: rejected/ AND done/ → returns done (higher priority than rejected)
# ---------------------------------------------------------------------------

def test_dedup_done_beats_rejected(dispatch_env):
    """Dispatch in both rejected/ and completed/ must yield exactly 1 done entry."""
    dispatch_id = "20260427-000002-test-done-vs-rejected"
    _write_dispatch_md(dispatch_env / "rejected", dispatch_id)
    _write_dispatch_md(dispatch_env / "completed", dispatch_id)

    import api_operator
    result = api_operator._scan_dispatches()

    all_entries = []
    for stage_entries in result["stages"].values():
        all_entries.extend(e for e in stage_entries if e["id"] == dispatch_id)

    assert len(all_entries) == 1, (
        f"Expected 1 entry for {dispatch_id}, got {len(all_entries)}"
    )
    assert all_entries[0]["stage"] == "done", (
        f"Expected stage='done', got '{all_entries[0]['stage']}'"
    )


# ---------------------------------------------------------------------------
# Test 4: No duplicates → passthrough unchanged
# ---------------------------------------------------------------------------

def test_no_false_dedup(dispatch_env):
    """Distinct dispatch IDs must all be returned without being dropped."""
    ids = [
        "20260427-000010-alpha",
        "20260427-000011-beta",
        "20260427-000012-gamma",
    ]
    for dispatch_id in ids:
        _write_dispatch_md(dispatch_env / "completed", dispatch_id)

    import api_operator
    result = api_operator._scan_dispatches()

    returned_ids = {e["id"] for stage_entries in result["stages"].values() for e in stage_entries}
    for dispatch_id in ids:
        assert dispatch_id in returned_ids, f"Dispatch {dispatch_id} was incorrectly dropped"

    assert result["total"] == len(ids)


# ---------------------------------------------------------------------------
# Test 5: Stage sort order — most-recent (smallest duration_secs) first
# ---------------------------------------------------------------------------

def test_stage_sort_order(dispatch_env):
    """Within a stage, entries must be sorted most-recent first (ascending duration_secs)."""
    completed_dir = dispatch_env / "completed"
    completed_dir.mkdir()

    dispatch_ids = ["20260427-000020-older", "20260427-000021-newer"]
    paths = []
    for i, dispatch_id in enumerate(dispatch_ids):
        p = _write_dispatch_md(completed_dir, dispatch_id)
        # Stagger mtimes: older gets an older mtime
        mtime = time.time() - (len(dispatch_ids) - i) * 10
        import os
        os.utime(str(p), (mtime, mtime))
        paths.append(p)

    import api_operator
    result = api_operator._scan_dispatches()

    done_entries = result["stages"]["done"]
    assert len(done_entries) == 2

    # duration_secs should be ascending (smaller = more recent = first)
    durations = [e["duration_secs"] for e in done_entries]
    assert durations == sorted(durations), (
        f"Expected ascending duration_secs, got {durations}"
    )
