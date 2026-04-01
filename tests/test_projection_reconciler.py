#!/usr/bin/env python3
"""
Tests for scripts/lib/projection_reconciler.py

Coverage:
  - scan_active_dispatches: files present, empty dir, missing dir, no-track files
  - FC-P1 (forbidden): active dispatch but progress shows idle → detect and report
  - FC-P1 with repair=True → auto_resolved, progress_state.yaml updated atomically
  - FC-P1 dispatch_id mismatch → still detected (stale dispatch_id in projection)
  - FC-P2 (warning): no active dispatch but progress shows working
  - FC-Q1 (forbidden): active dispatch but queue shows queued → duplicate dispatch risk
  - FC-Q1: queue shows blocked → also detected
  - Clean state: no mismatches, is_clean=True
  - Idempotency: repair twice produces same result
  - Mismatch events written to consistency_checks/projection_mismatches.ndjson
  - ReconcileResult helpers: has_forbidden, is_clean, forbidden_mismatches, warning_mismatches
  - Multi-track: Track B has FC-P1, Track C is clean

Scenario coverage: success (clean state), FC-P1 detect-only, FC-P1 repair,
                   FC-P2 warning, FC-Q1 forbidden, idempotent repair,
                   duplicate-dispatch prevention (FC-Q1 blocks T0 redispatch).
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

# Ensure scripts/lib is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from projection_reconciler import (
    FC_P1,
    FC_P2,
    FC_Q1,
    ActiveDispatch,
    ProjectionReconciler,
    ReconcileResult,
    _parse_dispatch_metadata,
    scan_active_dispatches,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _make_dispatch_file(
    active_dir: Path,
    dispatch_id: str,
    track: str,
    pr_id: str = "PR-1",
    gate: str = "gate_pr1_test",
) -> Path:
    """Write a minimal dispatch .md file with Track/PR-ID/Gate/Dispatch-ID metadata."""
    content = f"""\
# Dispatch: {dispatch_id}

Dispatch-ID: {dispatch_id}
Track: {track}
PR-ID: {pr_id}
Gate: {gate}

## Description
Test dispatch for track {track}.
"""
    path = active_dir / f"{dispatch_id}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _make_progress_state(state_dir: Path, tracks: dict) -> None:
    """Write a progress_state.yaml with the given per-track state dict."""
    state = {
        "updated_at": "2026-04-01T10:00:00Z",
        "tracks": tracks,
    }
    path = state_dir / "progress_state.yaml"
    path.write_text(yaml.dump(state, default_flow_style=False), encoding="utf-8")


def _make_queue_state(state_dir: Path, prs: list, active: list = None, completed: list = None, blocked: list = None) -> None:
    """Write a pr_queue_state.json."""
    data = {
        "prs": prs,
        "active": active or [],
        "completed": completed or [],
        "blocked": blocked or [],
    }
    path = state_dir / "pr_queue_state.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def _read_mismatch_log(consistency_dir: Path) -> list:
    """Read projection_mismatches.ndjson and return list of event dicts."""
    log_path = consistency_dir / "projection_mismatches.ndjson"
    if not log_path.exists():
        return []
    events = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


@pytest.fixture
def tmp_dirs(tmp_path):
    """Return (dispatch_dir, state_dir, consistency_dir) as temp paths."""
    dispatch_dir = tmp_path / "dispatches"
    state_dir = tmp_path / "state"
    consistency_dir = tmp_path / "state" / "consistency_checks"

    (dispatch_dir / "active").mkdir(parents=True)
    state_dir.mkdir(parents=True)
    consistency_dir.mkdir(parents=True)

    return dispatch_dir, state_dir, consistency_dir


# ---------------------------------------------------------------------------
# scan_active_dispatches
# ---------------------------------------------------------------------------

class TestScanActiveDispatches:
    def test_empty_active_dir_returns_empty_list(self, tmp_dirs):
        dispatch_dir, _, _ = tmp_dirs
        result = scan_active_dispatches(dispatch_dir)
        assert result == []

    def test_missing_active_dir_returns_empty_list(self, tmp_path):
        # dispatch_dir exists but has no active/ subdir
        dispatch_dir = tmp_path / "dispatches"
        dispatch_dir.mkdir()
        result = scan_active_dispatches(dispatch_dir)
        assert result == []

    def test_parses_dispatch_with_all_metadata(self, tmp_dirs):
        dispatch_dir, _, _ = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="20260401-010101-test-dispatch-B",
            track="B",
            pr_id="PR-1",
            gate="gate_pr1_test",
        )
        records = scan_active_dispatches(dispatch_dir)
        assert len(records) == 1
        d = records[0]
        assert d.dispatch_id == "20260401-010101-test-dispatch-B"
        assert d.track == "B"
        assert d.pr_id == "PR-1"
        assert d.gate == "gate_pr1_test"

    def test_skips_files_without_track(self, tmp_dirs):
        dispatch_dir, _, _ = tmp_dirs
        active_dir = dispatch_dir / "active"
        # Write a dispatch file with no Track: field
        path = active_dir / "no-track.md"
        path.write_text("# No track metadata\n\nDispatch-ID: some-id\n", encoding="utf-8")
        result = scan_active_dispatches(dispatch_dir)
        assert result == []

    def test_skips_non_md_files(self, tmp_dirs):
        dispatch_dir, _, _ = tmp_dirs
        active_dir = dispatch_dir / "active"
        (active_dir / "README.txt").write_text("not a dispatch", encoding="utf-8")
        result = scan_active_dispatches(dispatch_dir)
        assert result == []

    def test_multiple_dispatches_different_tracks(self, tmp_dirs):
        dispatch_dir, _, _ = tmp_dirs
        active_dir = dispatch_dir / "active"
        _make_dispatch_file(active_dir, "dispatch-a-001", "A", "PR-1")
        _make_dispatch_file(active_dir, "dispatch-b-001", "B", "PR-1")
        records = scan_active_dispatches(dispatch_dir)
        assert len(records) == 2
        tracks = {r.track for r in records}
        assert tracks == {"A", "B"}


# ---------------------------------------------------------------------------
# _parse_dispatch_metadata
# ---------------------------------------------------------------------------

class TestParseDispatchMetadata:
    def test_extracts_all_fields(self, tmp_path):
        f = tmp_path / "dispatch.md"
        f.write_text(
            "Dispatch-ID: disp-001\nTrack: C\nPR-ID: PR-2\nGate: gate_pr2_test\n",
            encoding="utf-8",
        )
        meta = _parse_dispatch_metadata(f)
        assert meta["dispatch_id"] == "disp-001"
        assert meta["track"] == "C"
        assert meta["pr_id"] == "PR-2"
        assert meta["gate"] == "gate_pr2_test"

    def test_fallback_to_target_header(self, tmp_path):
        f = tmp_path / "dispatch.md"
        f.write_text("[[TARGET:B]]\n# Dispatch\nDispatch-ID: d-002\n", encoding="utf-8")
        meta = _parse_dispatch_metadata(f)
        assert meta["track"] == "B"

    def test_missing_file_returns_none_values(self, tmp_path):
        f = tmp_path / "nonexistent.md"
        meta = _parse_dispatch_metadata(f)
        assert meta["track"] is None
        assert meta["dispatch_id"] is None


# ---------------------------------------------------------------------------
# FC-P1: Active dispatch but progress shows idle
# ---------------------------------------------------------------------------

class TestFcP1Detection:
    """
    Scenario: active-dispatch-but-no-in-progress
    The dispatch filesystem has a live dispatch for Track B but progress_state.yaml
    reports Track B as idle.
    """

    def test_detects_fc_p1_when_progress_idle(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile(repair=False)

        assert result.has_forbidden
        assert len(result.mismatches) == 1
        assert result.mismatches[0].contradiction_id == FC_P1
        assert result.mismatches[0].severity == "forbidden"
        assert result.mismatches[0].auto_resolved is False
        assert len(result.repairs) == 0

    def test_fc_p1_not_flagged_when_consistent(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {
                "status": "working",
                "active_dispatch_id": "dispatch-b-001",
                "history": [],
            },
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile(repair=False)

        assert result.is_clean
        assert len(result.mismatches) == 0

    def test_detects_dispatch_id_mismatch(self, tmp_dirs):
        """FC-P1: status=working but dispatch_id points to different dispatch."""
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-NEW",
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {
                "status": "working",
                "active_dispatch_id": "dispatch-b-OLD",  # stale
                "history": [],
            },
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile(repair=False)

        # dispatch_id mismatch = FC-P1
        assert result.has_forbidden
        fc_p1 = [m for m in result.mismatches if m.contradiction_id == FC_P1]
        assert len(fc_p1) == 1
        assert "dispatch_id mismatch" in fc_p1[0].canonical_value

    def test_fc_p1_writes_mismatch_event(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        reconciler.reconcile(repair=False)

        events = _read_mismatch_log(consistency_dir)
        assert len(events) == 1
        assert events[0]["contradiction_id"] == FC_P1
        assert events[0]["severity"] == "forbidden"
        assert events[0]["metadata"]["track"] == "B"


class TestFcP1Repair:
    """FC-P1 with repair=True → progress_state.yaml updated atomically."""

    def test_repair_sets_status_to_working(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        dispatch_id = "dispatch-b-repair-001"
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id=dispatch_id,
            track="B",
            pr_id="PR-1",
            gate="gate_pr1_test",
        )
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile(repair=True)

        # Mismatch should be detected and marked auto_resolved
        assert result.has_forbidden  # still present in mismatches
        assert result.mismatches[0].auto_resolved is True
        assert len(result.repairs) == 1
        assert result.repairs[0]["after_status"] == "working"
        assert result.repairs[0]["after_dispatch_id"] == dispatch_id

        # progress_state.yaml must be updated on disk
        progress = yaml.safe_load((state_dir / "progress_state.yaml").read_text())
        track_b = progress["tracks"]["B"]
        assert track_b["status"] == "working"
        assert track_b["active_dispatch_id"] == dispatch_id
        assert track_b["current_gate"] == "gate_pr1_test"

    def test_repair_creates_track_section_if_missing(self, tmp_dirs):
        """Repair when Track B has no entry in progress_state.yaml at all."""
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        dispatch_id = "dispatch-b-new-track"
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id=dispatch_id,
            track="B",
            pr_id="PR-1",
            gate="gate_pr1_new",
        )
        # progress_state.yaml has no tracks key at all
        (state_dir / "progress_state.yaml").write_text(
            yaml.dump({"updated_at": "2026-04-01T00:00:00Z"}),
            encoding="utf-8",
        )

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile(repair=True)

        assert result.mismatches[0].auto_resolved is True
        progress = yaml.safe_load((state_dir / "progress_state.yaml").read_text())
        assert progress["tracks"]["B"]["status"] == "working"
        assert progress["tracks"]["B"]["active_dispatch_id"] == dispatch_id

    def test_repair_records_history_entry(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-hist",
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        reconciler.reconcile(repair=True)

        progress = yaml.safe_load((state_dir / "progress_state.yaml").read_text())
        history = progress["tracks"]["B"]["history"]
        assert len(history) >= 1
        assert history[0]["to_status"] == "working"
        assert history[0]["updated_by"] == "projection_reconciler:FC-P1"

    def test_repair_idempotent(self, tmp_dirs):
        """
        Running repair twice on the same state produces the same outcome.
        Second run: status=working, dispatch_id matches → no FC-P1 contradiction.
        """
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        dispatch_id = "dispatch-b-idem"
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id=dispatch_id,
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result1 = reconciler.reconcile(repair=True)
        assert result1.has_forbidden
        assert result1.mismatches[0].auto_resolved is True

        # Second run: projection is now consistent
        result2 = reconciler.reconcile(repair=True)
        assert result2.is_clean
        assert len(result2.repairs) == 0

    def test_repair_without_progress_file_creates_file(self, tmp_dirs):
        """Repair when progress_state.yaml does not exist at all."""
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        dispatch_id = "dispatch-b-no-file"
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id=dispatch_id,
            track="B",
            pr_id="PR-1",
        )
        # No progress_state.yaml written

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile(repair=True)

        assert result.mismatches[0].auto_resolved is True
        progress_path = state_dir / "progress_state.yaml"
        assert progress_path.exists()
        progress = yaml.safe_load(progress_path.read_text())
        assert progress["tracks"]["B"]["status"] == "working"


# ---------------------------------------------------------------------------
# FC-P2: No active dispatch but progress shows working
# ---------------------------------------------------------------------------

class TestFcP2:
    """
    Scenario: working-terminal-but-idle-projection (inverted).
    The progress_state.yaml claims Track B is working but the dispatch
    filesystem has no active dispatch for Track B.
    """

    def test_detects_fc_p2_stale_working_state(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        # Empty active dir, but progress shows working
        _make_progress_state(state_dir, {
            "B": {
                "status": "working",
                "active_dispatch_id": "dispatch-b-stale",
                "history": [],
            },
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile(repair=False)

        assert not result.is_clean
        fc_p2 = [m for m in result.mismatches if m.contradiction_id == FC_P2]
        assert len(fc_p2) == 1
        assert fc_p2[0].severity == "warning"
        assert fc_p2[0].auto_resolved is False  # Not auto-repaired

    def test_fc_p2_not_flagged_when_active_dispatch_present(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-active",
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {
                "status": "working",
                "active_dispatch_id": "dispatch-b-active",
                "history": [],
            },
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile(repair=False)
        fc_p2 = [m for m in result.mismatches if m.contradiction_id == FC_P2]
        assert len(fc_p2) == 0

    def test_fc_p2_not_flagged_when_idle(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()
        fc_p2 = [m for m in result.mismatches if m.contradiction_id == FC_P2]
        assert len(fc_p2) == 0

    def test_fc_p2_records_stale_dispatch_id_in_metadata(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_progress_state(state_dir, {
            "B": {
                "status": "working",
                "active_dispatch_id": "dispatch-b-stale-xyz",
                "history": [],
            },
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        fc_p2 = [m for m in result.mismatches if m.contradiction_id == FC_P2]
        assert fc_p2[0].metadata["stale_dispatch_id"] == "dispatch-b-stale-xyz"


# ---------------------------------------------------------------------------
# FC-Q1: Active dispatch but queue shows queued/blocked (duplicate dispatch risk)
# ---------------------------------------------------------------------------

class TestFcQ1:
    """
    Scenario: active dispatch for PR-1 in C-3, but P-2 still shows PR-1 as queued.
    This is the primary duplicate dispatch risk: T0 sees 'queued' and may
    issue a second dispatch to the same PR.
    """

    def test_detects_fc_q1_queue_shows_queued(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_queue_state(state_dir, prs=[{"id": "PR-1", "status": "queued"}])

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        assert result.has_forbidden
        fc_q1 = [m for m in result.mismatches if m.contradiction_id == FC_Q1]
        assert len(fc_q1) == 1
        assert fc_q1[0].severity == "forbidden"
        assert "PR-1" in fc_q1[0].canonical_value

    def test_detects_fc_q1_queue_shows_blocked(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_queue_state(state_dir, prs=[{"id": "PR-1", "status": "blocked"}])

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        fc_q1 = [m for m in result.mismatches if m.contradiction_id == FC_Q1]
        assert len(fc_q1) == 1
        assert fc_q1[0].metadata["projected_status"] == "blocked"

    def test_no_fc_q1_when_queue_shows_active(self, tmp_dirs):
        """When queue already reflects active state, no contradiction."""
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_queue_state(
            state_dir,
            prs=[{"id": "PR-1", "status": "in_progress"}],
            active=["PR-1"],
        )

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        fc_q1 = [m for m in result.mismatches if m.contradiction_id == FC_Q1]
        assert len(fc_q1) == 0

    def test_fc_q1_not_checked_when_no_queue_file(self, tmp_dirs):
        """When pr_queue_state.json is absent, FC-Q1 is not flagged."""
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        # No queue state file written

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        fc_q1 = [m for m in result.mismatches if m.contradiction_id == FC_Q1]
        assert len(fc_q1) == 0

    def test_fc_q1_recommends_queue_reconcile(self, tmp_dirs):
        """FC-Q1 recommended action must reference reconcile_queue_state.py."""
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_queue_state(state_dir, prs=[{"id": "PR-1", "status": "queued"}])

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        fc_q1 = [m for m in result.mismatches if m.contradiction_id == FC_Q1]
        assert "reconcile_queue_state.py" in fc_q1[0].recommended_action

    def test_fc_q1_prevents_duplicate_dispatch(self, tmp_dirs):
        """
        Verify the FC-Q1 detection mechanism that T0 must check before redispatch.

        If a forbidden FC-Q1 mismatch is present (active dispatch + queue queued),
        the queue projection cannot be trusted for dispatch decisions. T0 must
        treat has_forbidden=True as a gate that prevents new dispatch for the PR.
        """
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_queue_state(state_dir, prs=[{"id": "PR-1", "status": "queued"}])

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        # T0 dispatch gate: if has_forbidden, do not dispatch again
        assert result.has_forbidden is True
        # Specifically FC-Q1 must be present (not just any forbidden)
        assert any(m.contradiction_id == FC_Q1 for m in result.forbidden_mismatches)


# ---------------------------------------------------------------------------
# Clean state
# ---------------------------------------------------------------------------

class TestCleanState:
    def test_empty_dispatch_dir_is_clean(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_progress_state(state_dir, {
            "A": {"status": "idle", "active_dispatch_id": None, "history": []},
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
            "C": {"status": "idle", "active_dispatch_id": None, "history": []},
        })
        _make_queue_state(state_dir, prs=[{"id": "PR-1", "status": "queued"}])

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        assert result.is_clean
        assert not result.has_forbidden
        assert len(result.active_dispatches) == 0

    def test_no_mismatch_log_on_clean_run(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        assert result.is_clean
        events = _read_mismatch_log(consistency_dir)
        assert len(events) == 0


# ---------------------------------------------------------------------------
# Multi-track scenarios
# ---------------------------------------------------------------------------

class TestMultiTrack:
    def test_fc_p1_only_for_affected_track(self, tmp_dirs):
        """Track B has FC-P1, Track C is clean. Only B is flagged."""
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
            "C": {
                "status": "idle",
                "active_dispatch_id": None,
                "history": [],
            },  # No active dispatch for C — consistent
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        assert result.has_forbidden
        fc_p1 = [m for m in result.mismatches if m.contradiction_id == FC_P1]
        assert len(fc_p1) == 1
        assert fc_p1[0].metadata["track"] == "B"

    def test_fc_p2_only_for_stale_track(self, tmp_dirs):
        """Track B shows working with no active dispatch; Track C is idle. Only B is warned."""
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_progress_state(state_dir, {
            "B": {
                "status": "working",
                "active_dispatch_id": "dispatch-b-stale",
                "history": [],
            },
            "C": {"status": "idle", "active_dispatch_id": None, "history": []},
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        fc_p2 = [m for m in result.mismatches if m.contradiction_id == FC_P2]
        assert len(fc_p2) == 1
        assert fc_p2[0].metadata["track"] == "B"

    def test_repair_only_affects_fc_p1_track(self, tmp_dirs):
        """Repair updates Track B only; Track C's idle state is untouched."""
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        dispatch_id = "dispatch-b-multi"
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id=dispatch_id,
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
            "C": {"status": "idle", "active_dispatch_id": None, "history": []},
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        reconciler.reconcile(repair=True)

        progress = yaml.safe_load((state_dir / "progress_state.yaml").read_text())
        assert progress["tracks"]["B"]["status"] == "working"
        assert progress["tracks"]["C"]["status"] == "idle"  # Unchanged


# ---------------------------------------------------------------------------
# Mismatch event logging
# ---------------------------------------------------------------------------

class TestMismatchEventLogging:
    def test_mismatch_event_fields(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-log-001",
            track="B",
            pr_id="PR-1",
            gate="gate_pr1_log",
        )
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        reconciler.reconcile()

        events = _read_mismatch_log(consistency_dir)
        assert len(events) == 1
        event = events[0]
        required_fields = [
            "contradiction_id", "severity", "canonical_surface",
            "canonical_value", "projected_surface", "projected_value",
            "tie_break_rule", "recommended_action", "auto_resolved",
            "timestamp", "metadata",
        ]
        for field in required_fields:
            assert field in event, f"Missing field: {field}"

    def test_multiple_mismatches_appended_to_log(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
        })
        _make_queue_state(state_dir, prs=[{"id": "PR-1", "status": "queued"}])

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        reconciler.reconcile()

        events = _read_mismatch_log(consistency_dir)
        codes = {e["contradiction_id"] for e in events}
        assert FC_P1 in codes
        assert FC_Q1 in codes

    def test_two_reconcile_runs_append_not_overwrite(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        reconciler.reconcile()
        reconciler.reconcile()  # Second run still has FC-P1 (no repair)

        events = _read_mismatch_log(consistency_dir)
        assert len(events) == 2


# ---------------------------------------------------------------------------
# ReconcileResult helpers
# ---------------------------------------------------------------------------

class TestReconcileResultHelpers:
    def test_has_forbidden_false_for_warnings_only(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        # FC-P2 is a warning only
        _make_progress_state(state_dir, {
            "B": {
                "status": "working",
                "active_dispatch_id": "stale",
                "history": [],
            },
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()

        assert not result.has_forbidden
        assert not result.is_clean
        assert len(result.warning_mismatches) >= 1
        assert len(result.forbidden_mismatches) == 0

    def test_summary_includes_count_lines(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()
        summary = result.summary()
        assert "Active dispatches found:" in summary
        assert "Forbidden contradictions:" in summary

    def test_to_dict_round_trips(self, tmp_dirs):
        dispatch_dir, state_dir, consistency_dir = tmp_dirs
        _make_dispatch_file(
            dispatch_dir / "active",
            dispatch_id="dispatch-b-001",
            track="B",
            pr_id="PR-1",
        )
        _make_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "history": []},
        })

        reconciler = ProjectionReconciler(dispatch_dir, state_dir, consistency_dir)
        result = reconciler.reconcile()
        d = result.to_dict()

        assert d["has_forbidden"] is True
        assert d["is_clean"] is False
        assert d["mismatch_count"] >= 1
        assert isinstance(d["mismatches"], list)
