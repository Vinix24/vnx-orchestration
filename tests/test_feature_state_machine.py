#!/usr/bin/env python3
"""Tests for scripts/lib/feature_state_machine.py.

Covers:
- parse_feature_plan(): completed PR detection (all [x])
- parse_feature_plan(): pending PR detection (any [ ])
- parse_feature_plan(): all PRs completed → status "completed"
- parse_feature_plan(): mixed state → status "in_progress", correct current_pr
- get_next_dispatchable(): returns correct terminal/track/role for available terminal
- get_next_dispatchable(): returns None when target terminal is leased
- Legacy PR-N: header format support
- Missing FEATURE_PLAN.md → safe empty result
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

_LIB = Path(__file__).parent.parent / "scripts" / "lib"
sys.path.insert(0, str(_LIB))

from feature_state_machine import (
    FeatureState,
    get_next_dispatchable,
    parse_feature_plan,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _write_plan(tmp_path: Path, content: str) -> Path:
    fp = tmp_path / "FEATURE_PLAN.md"
    fp.write_text(textwrap.dedent(content), encoding="utf-8")
    return fp


PLAN_ALL_PENDING = """\
    # F46–F50 Feature Plan

    ## F46: Some Feature

    ### F46-PR1: Learning Loop DB Bridge
    **Track**: A (T1 backend-developer)
    **Status**: Planned

    Modify learning_loop.py to persist patterns.

    **Success criteria**:
    - [ ] SELECT COUNT(*) FROM success_patterns > 0
    - [ ] Approved rules appear in prevention_rules table

    ### F46-PR2: Conversation Analyzer DB Bridge
    **Track**: A (T1 backend-developer)
    **Status**: Planned

    Modify conversation_analyzer.py.

    **Success criteria**:
    - [ ] success_patterns contain session_analysis entries
    - [ ] No regression in session_analytics writes
"""

PLAN_FIRST_COMPLETED = """\
    # F46–F50 Feature Plan

    ## F46: Some Feature

    ### F46-PR1: Learning Loop DB Bridge
    **Track**: A (T1 backend-developer)
    **Status**: Completed

    Modify learning_loop.py.

    **Success criteria**:
    - [x] SELECT COUNT(*) FROM success_patterns > 0
    - [x] Approved rules appear in prevention_rules table

    ### F46-PR2: Conversation Analyzer DB Bridge
    **Track**: A (T1 backend-developer)
    **Status**: Planned

    Modify conversation_analyzer.py.

    **Success criteria**:
    - [ ] success_patterns contain session_analysis entries
    - [ ] No regression in session_analytics writes
"""

PLAN_ALL_COMPLETED = """\
    # F46–F50 Feature Plan

    ## F46: Some Feature

    ### F46-PR1: Learning Loop DB Bridge
    **Track**: A (T1 backend-developer)

    Modify learning_loop.py.

    **Success criteria**:
    - [x] Criterion one
    - [x] Criterion two

    ### F46-PR2: Conversation Analyzer DB Bridge
    **Track**: B (T2 test-engineer)

    Modify conversation_analyzer.py.

    **Success criteria**:
    - [x] Criterion three
    - [x] Criterion four
"""

PLAN_TRACK_B = """\
    # F47 Plan

    ### F47-PR3: State Loop Integration Tests
    **Track**: B (T2 test-engineer)
    **Status**: Planned

    New tests/test_state_feedback_loop.py.

    **Success criteria**:
    - [ ] All 5 tests pass
    - [ ] Dry-run loop completes without errors
"""

PLAN_LEGACY_FORMAT = """\
    # Feature: Legacy Queue Test

    ## PR-0: Foundation Work
    **Track**: C
    **Priority**: P1

    Description of PR-0.

    - [ ] Criterion one
    - [ ] Criterion two

    ## PR-1: Secondary Work
    **Track**: A (T1 backend-developer)

    Description of PR-1.

    - [ ] Another criterion
"""


# ---------------------------------------------------------------------------
# parse_feature_plan() tests
# ---------------------------------------------------------------------------

class TestParseCompletedPR:
    """PR with all [x] checkboxes is detected as completed."""

    def test_first_pr_completed_second_pending(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_FIRST_COMPLETED)
        state = parse_feature_plan(fp)

        assert state.total_prs == 2
        assert state.completed_prs == 1
        assert state.current_pr == "F46-PR2"
        assert state.status == "in_progress"

    def test_completed_pr_not_current(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_FIRST_COMPLETED)
        state = parse_feature_plan(fp)
        # PR1 is done; current must be PR2
        assert state.current_pr != "F46-PR1"

    def test_completion_pct_with_one_of_two(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_FIRST_COMPLETED)
        state = parse_feature_plan(fp)
        assert state.completion_pct == 50


class TestParsePendingPR:
    """PR with any [ ] checkbox is detected as pending."""

    def test_all_pending_first_is_current(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_ALL_PENDING)
        state = parse_feature_plan(fp)

        assert state.completed_prs == 0
        assert state.current_pr == "F46-PR1"
        assert state.status == "planned"

    def test_pending_pr_extracts_track(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_ALL_PENDING)
        state = parse_feature_plan(fp)

        assert state.assigned_track == "A"
        assert state.assigned_role == "backend-developer"

    def test_pending_pr_next_task_contains_pr_id(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_ALL_PENDING)
        state = parse_feature_plan(fp)

        assert state.next_task is not None
        assert "F46-PR1" in state.next_task

    def test_completion_pct_zero_when_all_pending(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_ALL_PENDING)
        state = parse_feature_plan(fp)
        assert state.completion_pct == 0


class TestAllCompleted:
    """Returns status 'completed' when all PRs are done."""

    def test_status_completed(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_ALL_COMPLETED)
        state = parse_feature_plan(fp)

        assert state.status == "completed"

    def test_no_current_pr_when_all_done(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_ALL_COMPLETED)
        state = parse_feature_plan(fp)

        assert state.current_pr is None
        assert state.next_task is None

    def test_completion_pct_100(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_ALL_COMPLETED)
        state = parse_feature_plan(fp)

        assert state.completion_pct == 100

    def test_completed_count_matches_total(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_ALL_COMPLETED)
        state = parse_feature_plan(fp)

        assert state.completed_prs == state.total_prs == 2


class TestTrackExtraction:
    """Track and role are correctly extracted for each track letter."""

    def test_track_b_test_engineer(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_TRACK_B)
        state = parse_feature_plan(fp)

        assert state.assigned_track == "B"
        assert state.assigned_role == "test-engineer"

    def test_track_c_no_role_annotation(self, tmp_path: Path) -> None:
        # PLAN_ALL_COMPLETED has PR2 with Track B; PR1 has Track A
        fp = _write_plan(tmp_path, PLAN_ALL_COMPLETED)
        # All done → no current track
        state = parse_feature_plan(fp)
        assert state.assigned_track is None


class TestLegacyFormat:
    """Legacy ## PR-N: headers are supported."""

    def test_legacy_pr_headers_parsed(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_LEGACY_FORMAT)
        state = parse_feature_plan(fp)

        assert state.total_prs == 2
        assert state.current_pr == "PR-0"

    def test_legacy_track_without_role(self, tmp_path: Path) -> None:
        fp = _write_plan(tmp_path, PLAN_LEGACY_FORMAT)
        state = parse_feature_plan(fp)

        # PR-0 has "**Track**: C" with no role annotation
        assert state.assigned_track == "C"
        assert state.assigned_role is None


class TestMissingFile:
    """Missing FEATURE_PLAN.md returns safe empty state."""

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        state = parse_feature_plan(tmp_path / "FEATURE_PLAN.md")

        assert state.total_prs == 0
        assert state.current_pr is None
        assert state.status == "planned"
        assert state.completion_pct == 0


# ---------------------------------------------------------------------------
# get_next_dispatchable() tests
# ---------------------------------------------------------------------------

def _write_t0_state(state_dir: Path, terminal_lease: str, terminal: str = "T1") -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    t0_state = {
        "schema_version": "2.0",
        "terminals": {
            "T1": {"lease_state": "idle", "status": "idle"},
            "T2": {"lease_state": "idle", "status": "idle"},
            "T3": {"lease_state": "idle", "status": "idle"},
        },
    }
    t0_state["terminals"][terminal]["lease_state"] = terminal_lease
    (state_dir / "t0_state.json").write_text(
        json.dumps(t0_state), encoding="utf-8"
    )


class TestNextDispatchable:
    """get_next_dispatchable() returns correct terminal/track/role."""

    def test_returns_correct_terminal_for_track_a(self, tmp_path: Path) -> None:
        # FEATURE_PLAN.md in project root (two levels up from state_dir)
        project_root = tmp_path / "project"
        project_root.mkdir()
        state_dir = project_root / ".vnx-data" / "state"
        _write_t0_state(state_dir, "idle", "T1")
        _write_plan(project_root, PLAN_ALL_PENDING)

        result = get_next_dispatchable(state_dir)

        assert result is not None
        assert result["terminal"] == "T1"
        assert result["track"] == "A"
        assert result["pr_id"] == "F46-PR1"
        assert result["role"] == "backend-developer"

    def test_returns_none_when_terminal_leased(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        state_dir = project_root / ".vnx-data" / "state"
        # T1 is leased — Track A cannot proceed
        _write_t0_state(state_dir, "leased", "T1")
        _write_plan(project_root, PLAN_ALL_PENDING)

        result = get_next_dispatchable(state_dir)

        assert result is None

    def test_returns_none_when_all_completed(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        state_dir = project_root / ".vnx-data" / "state"
        _write_t0_state(state_dir, "idle", "T1")
        _write_plan(project_root, PLAN_ALL_COMPLETED)

        result = get_next_dispatchable(state_dir)

        assert result is None

    def test_task_description_present(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        state_dir = project_root / ".vnx-data" / "state"
        _write_t0_state(state_dir, "idle", "T1")
        _write_plan(project_root, PLAN_ALL_PENDING)

        result = get_next_dispatchable(state_dir)

        assert result is not None
        assert result["task_description"] is not None
        assert len(result["task_description"]) > 0

    def test_no_t0_state_file_still_returns_result(self, tmp_path: Path) -> None:
        # When t0_state.json doesn't exist, proceed optimistically
        project_root = tmp_path / "project"
        project_root.mkdir()
        state_dir = project_root / ".vnx-data" / "state"
        state_dir.mkdir(parents=True)
        _write_plan(project_root, PLAN_ALL_PENDING)
        # No t0_state.json written

        result = get_next_dispatchable(state_dir)

        assert result is not None
        assert result["terminal"] == "T1"
