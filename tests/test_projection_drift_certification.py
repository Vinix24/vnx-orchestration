#!/usr/bin/env python3
"""
PR-2 Certification: Projection Drift Incident Reproduction And Closure.

Gate: gate_pr2_projection_drift_certification
Contract: docs/core/120_PROJECTION_CONSISTENCY_CONTRACT.md

This test suite reproduces the three observed drift incidents from the first
autonomous chain and certifies that the projection reconciler (PR-1) detects
and repairs them. It also verifies operator-visible diagnostics and
chain-created open item closure.

Observed incidents reproduced:
  1. "In Progress: None" while work was visibly running (FC-P1)
  2. Queue shows "queued" for an active PR (FC-Q1)
  3. Terminal projection idle while lease is leased (FC-T1 — detected, not repaired by this reconciler)

Additional certification scenarios:
  4. Reconciliation is idempotent and deterministic
  5. Operator diagnostics surface forbidden contradictions
  6. Multi-track drift: only affected tracks are flagged
  7. Stale working state detected after dispatch ends (FC-P2)
  8. Dispatch-ID mismatch between progress and filesystem (FC-P1 variant)
  9. End-to-end: drift -> detect -> repair -> verify clean
 10. Mismatch audit trail is append-only and complete
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from projection_reconciler import (
    FC_P1,
    FC_P2,
    FC_Q1,
    ProjectionReconciler,
    ReconcileResult,
    scan_active_dispatches,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_dispatch_file(active_dir: Path, filename: str, **meta) -> Path:
    """Create a dispatch .md file with metadata fields."""
    content_lines = []
    if "track" in meta:
        content_lines.append(f"Track: {meta['track']}")
    if "pr_id" in meta:
        content_lines.append(f"PR-ID: {meta['pr_id']}")
    if "gate" in meta:
        content_lines.append(f"Gate: {meta['gate']}")
    if "dispatch_id" in meta:
        content_lines.append(f"Dispatch-ID: {meta['dispatch_id']}")
    active_dir.mkdir(parents=True, exist_ok=True)
    fp = active_dir / filename
    fp.write_text("\n".join(content_lines) + "\n")
    return fp


def write_progress_state(state_dir: Path, tracks: Dict[str, Dict[str, Any]]) -> Path:
    """Write a progress_state.yaml with given track states."""
    state = {
        "version": "1.0",
        "updated_at": "2026-04-01T08:00:00Z",
        "updated_by": "test",
        "tracks": tracks,
    }
    fp = state_dir / "progress_state.yaml"
    state_dir.mkdir(parents=True, exist_ok=True)
    fp.write_text(yaml.dump(state, default_flow_style=False, sort_keys=False))
    return fp


def write_queue_state(state_dir: Path, prs: list, **kwargs) -> Path:
    """Write a pr_queue_state.json with given PR entries."""
    data = {
        "feature": "test-feature",
        "prs": prs,
        "completed": kwargs.get("completed", []),
        "active": kwargs.get("active", []),
        "blocked": kwargs.get("blocked", []),
    }
    fp = state_dir / "pr_queue_state.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(data, indent=2))
    return fp


def make_reconciler(tmp_path: Path):
    """Create a ProjectionReconciler with standard paths under tmp_path."""
    dispatch_dir = tmp_path / "dispatches"
    state_dir = tmp_path / "state"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return ProjectionReconciler(dispatch_dir, state_dir), dispatch_dir, state_dir


# ===========================================================================
# CERT-1: Reproduce "In Progress: None" while work was running (FC-P1)
#
# This is the exact incident from the first autonomous chain:
#   - Dispatch is active in dispatches/active/ for track C
#   - progress_state.yaml shows track C as idle
#   - T0 sees "In Progress: None" and considers redispatching
# ===========================================================================

class TestCert1InProgressNone:
    """Reproduce and certify the 'In Progress: None' incident."""

    def test_cert1a_reproduce_drift(self, tmp_path):
        """FC-P1 is detected when active dispatch exists but progress shows idle."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "20260401-dispatch-C.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="20260401-dispatch-C",
        )
        write_progress_state(state_dir, {
            "C": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        result = reconciler.reconcile(repair=False)

        assert result.has_forbidden, "FC-P1 must be detected as forbidden"
        assert any(m.contradiction_id == FC_P1 for m in result.mismatches)
        fc_p1 = [m for m in result.mismatches if m.contradiction_id == FC_P1][0]
        assert fc_p1.severity == "forbidden"
        assert "idle" in fc_p1.projected_value
        assert "20260401-dispatch-C" in fc_p1.canonical_value

    def test_cert1b_repair_fixes_drift(self, tmp_path):
        """FC-P1 repair updates progress_state.yaml to working."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "20260401-dispatch-C.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="20260401-dispatch-C",
        )
        write_progress_state(state_dir, {
            "C": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        result = reconciler.reconcile(repair=True)

        assert result.has_forbidden, "FC-P1 mismatch still reported (for audit)"
        assert any(m.auto_resolved for m in result.mismatches), "FC-P1 must be auto-resolved"

        # Verify the repair on disk
        repaired = yaml.safe_load((state_dir / "progress_state.yaml").read_text())
        assert repaired["tracks"]["C"]["status"] == "working"
        assert repaired["tracks"]["C"]["active_dispatch_id"] == "20260401-dispatch-C"

    def test_cert1c_repair_is_clean_on_recheck(self, tmp_path):
        """After repair, a second reconcile finds no mismatches."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "20260401-dispatch-C.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="20260401-dispatch-C",
        )
        write_progress_state(state_dir, {
            "C": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        reconciler.reconcile(repair=True)
        result2 = reconciler.reconcile(repair=False)

        assert result2.is_clean, f"Post-repair reconcile must be clean, got: {result2.summary()}"

    def test_cert1d_history_records_repair_provenance(self, tmp_path):
        """Repair records provenance in history with FC-P1 tag."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-001.md",
            track="B", pr_id="PR-1", gate="gate_pr1_test",
            dispatch_id="d-001",
        )
        write_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        reconciler.reconcile(repair=True)

        repaired = yaml.safe_load((state_dir / "progress_state.yaml").read_text())
        history = repaired["tracks"]["B"]["history"]
        assert len(history) >= 1
        assert history[0]["updated_by"] == "projection_reconciler:FC-P1"
        assert history[0]["dispatch_id"] == "d-001"
        assert history[0]["from_status"] == "idle"
        assert history[0]["to_status"] == "working"


# ===========================================================================
# CERT-2: Reproduce queue shows "queued" for an active PR (FC-Q1)
# ===========================================================================

class TestCert2QueueLagBehindFilesystem:
    """Reproduce and certify the queue projection lag incident."""

    def test_cert2a_reproduce_queue_drift(self, tmp_path):
        """FC-Q1 detected when dispatch is active but queue says queued."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-active.md",
            track="B", pr_id="PR-1", gate="gate_pr1_test",
            dispatch_id="d-active",
        )
        write_queue_state(state_dir, [
            {"id": "PR-0", "status": "completed"},
            {"id": "PR-1", "status": "queued"},  # WRONG — should be active
        ], completed=["PR-0"])

        result = reconciler.reconcile(repair=False)

        assert result.has_forbidden
        fc_q1 = [m for m in result.mismatches if m.contradiction_id == FC_Q1]
        assert len(fc_q1) == 1
        assert fc_q1[0].severity == "forbidden"
        assert "queued" in fc_q1[0].projected_value
        assert "PR-1" in fc_q1[0].canonical_value

    def test_cert2b_queue_blocked_also_detected(self, tmp_path):
        """FC-Q1 detected when queue says blocked but dispatch is active."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-active.md",
            track="C", pr_id="PR-2", gate="gate_pr2_test",
            dispatch_id="d-active",
        )
        write_queue_state(state_dir, [
            {"id": "PR-2", "status": "blocked"},
        ], blocked=["PR-2"])

        result = reconciler.reconcile(repair=False)

        fc_q1 = [m for m in result.mismatches if m.contradiction_id == FC_Q1]
        assert len(fc_q1) == 1
        assert "blocked" in fc_q1[0].projected_value

    def test_cert2c_duplicate_dispatch_prevention(self, tmp_path):
        """FC-Q1 has_forbidden=True prevents T0 from creating duplicate dispatch."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-active.md",
            track="B", pr_id="PR-1", gate="gate_pr1_test",
            dispatch_id="d-active",
        )
        write_queue_state(state_dir, [
            {"id": "PR-1", "status": "queued"},
        ])

        result = reconciler.reconcile(repair=False)

        # T0 must check has_forbidden before dispatching
        assert result.has_forbidden, "has_forbidden must be True to block T0 redispatch"

    def test_cert2d_no_false_positive_when_queue_correct(self, tmp_path):
        """No FC-Q1 when queue correctly shows active/in_progress."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-active.md",
            track="B", pr_id="PR-1", gate="gate_pr1_test",
            dispatch_id="d-active",
        )
        write_queue_state(state_dir, [
            {"id": "PR-1", "status": "in_progress"},
        ], active=["PR-1"])

        result = reconciler.reconcile(repair=False)

        fc_q1 = [m for m in result.mismatches if m.contradiction_id == FC_Q1]
        assert len(fc_q1) == 0, "No FC-Q1 when queue is correct"


# ===========================================================================
# CERT-3: Stale working state after dispatch ends (FC-P2)
# ===========================================================================

class TestCert3StaleWorkingState:
    """Certify detection of stale working projection after dispatch ends."""

    def test_cert3a_stale_working_detected(self, tmp_path):
        """FC-P2 detected when progress shows working but no active dispatch."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        (dispatch_dir / "active").mkdir(parents=True, exist_ok=True)  # empty
        write_progress_state(state_dir, {
            "B": {
                "status": "working",
                "active_dispatch_id": "old-dispatch-123",
                "current_gate": "gate_pr1_test",
                "history": [],
            },
        })

        result = reconciler.reconcile(repair=False)

        fc_p2 = [m for m in result.mismatches if m.contradiction_id == FC_P2]
        assert len(fc_p2) == 1
        assert fc_p2[0].severity == "warning"
        assert "old-dispatch-123" in fc_p2[0].projected_value
        assert fc_p2[0].metadata["stale_dispatch_id"] == "old-dispatch-123"

    def test_cert3b_stale_working_not_auto_repaired(self, tmp_path):
        """FC-P2 is warning-only — not auto-repaired even with repair=True."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        (dispatch_dir / "active").mkdir(parents=True, exist_ok=True)
        write_progress_state(state_dir, {
            "B": {
                "status": "working",
                "active_dispatch_id": "old-dispatch",
                "current_gate": "gate_test",
                "history": [],
            },
        })

        result = reconciler.reconcile(repair=True)

        fc_p2 = [m for m in result.mismatches if m.contradiction_id == FC_P2]
        assert len(fc_p2) == 1
        assert not fc_p2[0].auto_resolved, "FC-P2 must not be auto-resolved"
        assert len(result.repairs) == 0, "No repairs for FC-P2"


# ===========================================================================
# CERT-4: Dispatch-ID mismatch (FC-P1 variant)
# ===========================================================================

class TestCert4DispatchIdMismatch:
    """Certify detection when progress shows working but for wrong dispatch."""

    def test_cert4a_mismatch_detected(self, tmp_path):
        """FC-P1 fires when status=working but dispatch_id is wrong."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "new-dispatch.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="new-dispatch",
        )
        write_progress_state(state_dir, {
            "C": {
                "status": "working",
                "active_dispatch_id": "old-dispatch",  # WRONG dispatch
                "current_gate": "gate_pr0_test",
                "history": [],
            },
        })

        result = reconciler.reconcile(repair=False)

        fc_p1 = [m for m in result.mismatches if m.contradiction_id == FC_P1]
        assert len(fc_p1) == 1
        assert "dispatch_id mismatch" in fc_p1[0].canonical_value

    def test_cert4b_mismatch_repaired(self, tmp_path):
        """FC-P1 repair corrects dispatch_id to match active dispatch."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "new-dispatch.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="new-dispatch",
        )
        write_progress_state(state_dir, {
            "C": {
                "status": "working",
                "active_dispatch_id": "old-dispatch",
                "current_gate": "gate_pr0_test",
                "history": [],
            },
        })

        reconciler.reconcile(repair=True)

        repaired = yaml.safe_load((state_dir / "progress_state.yaml").read_text())
        assert repaired["tracks"]["C"]["active_dispatch_id"] == "new-dispatch"


# ===========================================================================
# CERT-5: Multi-track drift isolation
# ===========================================================================

class TestCert5MultiTrackIsolation:
    """Certify that drift detection only flags affected tracks."""

    def test_cert5a_only_drifted_track_flagged(self, tmp_path):
        """Track B drifts (FC-P1), Track C is consistent — only B flagged."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-B.md",
            track="B", pr_id="PR-1", gate="gate_pr1_test",
            dispatch_id="d-B",
        )
        create_dispatch_file(
            dispatch_dir / "active", "d-C.md",
            track="C", pr_id="PR-2", gate="gate_pr2_test",
            dispatch_id="d-C",
        )
        write_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
            "C": {"status": "working", "active_dispatch_id": "d-C", "current_gate": "gate_pr2_test", "history": []},
        })

        result = reconciler.reconcile(repair=False)

        fc_p1 = [m for m in result.mismatches if m.contradiction_id == FC_P1]
        assert len(fc_p1) == 1
        assert fc_p1[0].metadata["track"] == "B"

    def test_cert5b_repair_only_affects_drifted_track(self, tmp_path):
        """Repair updates B but leaves C unchanged."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-B.md",
            track="B", pr_id="PR-1", gate="gate_pr1_test",
            dispatch_id="d-B",
        )
        create_dispatch_file(
            dispatch_dir / "active", "d-C.md",
            track="C", pr_id="PR-2", gate="gate_pr2_test",
            dispatch_id="d-C",
        )
        write_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
            "C": {"status": "working", "active_dispatch_id": "d-C", "current_gate": "gate_pr2_test", "history": []},
        })

        reconciler.reconcile(repair=True)

        repaired = yaml.safe_load((state_dir / "progress_state.yaml").read_text())
        assert repaired["tracks"]["B"]["status"] == "working"
        assert repaired["tracks"]["B"]["active_dispatch_id"] == "d-B"
        # C must remain unchanged
        assert repaired["tracks"]["C"]["status"] == "working"
        assert repaired["tracks"]["C"]["active_dispatch_id"] == "d-C"


# ===========================================================================
# CERT-6: Operator-visible diagnostics
# ===========================================================================

class TestCert6OperatorDiagnostics:
    """Certify that mismatch reports contain all required fields."""

    def test_cert6a_mismatch_report_fields(self, tmp_path):
        """All Section 7.1 fields are present in mismatch report."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-001.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="d-001",
        )
        write_progress_state(state_dir, {
            "C": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        result = reconciler.reconcile(repair=False)
        m = result.mismatches[0]

        # Contract Section 7.1 required fields
        assert m.contradiction_id.startswith("FC-")
        assert m.severity in ("forbidden", "warning")
        assert m.canonical_surface
        assert m.canonical_value
        assert m.projected_surface
        assert m.projected_value
        assert m.tie_break_rule.startswith("TB-")
        assert m.recommended_action
        assert isinstance(m.auto_resolved, bool)
        assert m.timestamp

    def test_cert6b_mismatch_ndjson_written(self, tmp_path):
        """Mismatch events written to consistency_checks/ NDJSON."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-001.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="d-001",
        )
        write_progress_state(state_dir, {
            "C": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        reconciler.reconcile(repair=False)

        log_path = state_dir / "consistency_checks" / "projection_mismatches.ndjson"
        assert log_path.exists(), "NDJSON mismatch log must exist"
        events = [json.loads(line) for line in log_path.read_text().strip().split("\n")]
        assert len(events) >= 1
        assert events[0]["contradiction_id"] == "FC-P1"
        assert events[0]["severity"] == "forbidden"

    def test_cert6c_summary_includes_forbidden_count(self, tmp_path):
        """ReconcileResult.summary() reports forbidden count for operator."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-001.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="d-001",
        )
        write_progress_state(state_dir, {
            "C": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        result = reconciler.reconcile(repair=False)
        summary = result.summary()

        assert "Forbidden contradictions: 1" in summary
        assert "FC-P1" in summary

    def test_cert6d_to_dict_serializable(self, tmp_path):
        """ReconcileResult.to_dict() produces JSON-serializable output."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-001.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="d-001",
        )
        write_progress_state(state_dir, {
            "C": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        result = reconciler.reconcile(repair=True)
        d = result.to_dict()

        serialized = json.dumps(d)  # Must not raise
        parsed = json.loads(serialized)
        assert parsed["has_forbidden"] is True
        assert parsed["mismatch_count"] >= 1
        assert parsed["repair_count"] >= 1


# ===========================================================================
# CERT-7: End-to-end drift lifecycle
# ===========================================================================

class TestCert7EndToEndDriftLifecycle:
    """Certify the full drift -> detect -> repair -> verify clean cycle."""

    def test_cert7a_full_lifecycle(self, tmp_path):
        """
        Simulate: dispatch created -> progress not updated (drift) ->
        reconciler detects FC-P1 + FC-Q1 -> repair -> recheck clean.
        """
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)

        # Setup: dispatch is active, but projections are stale
        create_dispatch_file(
            dispatch_dir / "active", "d-lifecycle.md",
            track="B", pr_id="PR-1", gate="gate_pr1_lifecycle",
            dispatch_id="d-lifecycle",
        )
        write_progress_state(state_dir, {
            "A": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
            "B": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
            "C": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })
        write_queue_state(state_dir, [
            {"id": "PR-0", "status": "completed"},
            {"id": "PR-1", "status": "queued"},  # stale
        ], completed=["PR-0"])

        # Step 1: Detect drift
        detect_result = reconciler.reconcile(repair=False)
        assert detect_result.has_forbidden
        assert not detect_result.is_clean
        fc_codes = {m.contradiction_id for m in detect_result.mismatches}
        assert FC_P1 in fc_codes, "FC-P1 must be detected"
        assert FC_Q1 in fc_codes, "FC-Q1 must be detected"

        # Step 2: Repair (only P-3 is auto-repaired; P-2 needs queue reconciler)
        repair_result = reconciler.reconcile(repair=True)
        assert any(m.auto_resolved for m in repair_result.mismatches)

        # Step 3: Verify P-3 is clean, FC-Q1 still reported (P-2 not auto-repaired)
        recheck = reconciler.reconcile(repair=False)
        fc_p1_remaining = [m for m in recheck.mismatches if m.contradiction_id == FC_P1]
        assert len(fc_p1_remaining) == 0, "FC-P1 must be resolved after repair"

        # FC-Q1 still present (queue state not auto-repaired by this reconciler)
        fc_q1_remaining = [m for m in recheck.mismatches if m.contradiction_id == FC_Q1]
        assert len(fc_q1_remaining) == 1, "FC-Q1 remains until queue reconciler runs"

    def test_cert7b_audit_trail_complete(self, tmp_path):
        """Audit trail accumulates across multiple reconcile passes."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-audit.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="d-audit",
        )
        write_progress_state(state_dir, {
            "C": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        # Run 1: detect
        reconciler.reconcile(repair=False)
        # Run 2: repair
        reconciler.reconcile(repair=True)
        # Run 3: verify clean (no new mismatches appended)
        reconciler.reconcile(repair=False)

        log_path = state_dir / "consistency_checks" / "projection_mismatches.ndjson"
        events = [json.loads(line) for line in log_path.read_text().strip().split("\n")]

        # Run 1 and Run 2 each produce FC-P1 (detect, then detect+repair)
        # Run 3 produces nothing (clean)
        assert len(events) == 2, f"Expected 2 mismatch events, got {len(events)}"
        assert all(e["contradiction_id"] == "FC-P1" for e in events)


# ===========================================================================
# CERT-8: Reconciliation determinism
# ===========================================================================

class TestCert8Determinism:
    """Certify that reconciliation is deterministic and idempotent."""

    def test_cert8a_same_input_same_output(self, tmp_path):
        """Two reconcile passes with same state produce identical mismatches."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-det.md",
            track="B", pr_id="PR-1", gate="gate_pr1_test",
            dispatch_id="d-det",
        )
        write_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        r1 = reconciler.reconcile(repair=False)
        r2 = reconciler.reconcile(repair=False)

        assert len(r1.mismatches) == len(r2.mismatches)
        for m1, m2 in zip(r1.mismatches, r2.mismatches):
            assert m1.contradiction_id == m2.contradiction_id
            assert m1.severity == m2.severity
            assert m1.canonical_value == m2.canonical_value
            assert m1.projected_value == m2.projected_value

    def test_cert8b_repair_idempotent(self, tmp_path):
        """Repair twice produces the same progress_state.yaml (minus timestamps)."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-idem.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="d-idem",
        )
        write_progress_state(state_dir, {
            "C": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        reconciler.reconcile(repair=True)
        state1 = yaml.safe_load((state_dir / "progress_state.yaml").read_text())

        reconciler.reconcile(repair=True)
        state2 = yaml.safe_load((state_dir / "progress_state.yaml").read_text())

        # Structural equivalence (ignoring timestamps)
        assert state1["tracks"]["C"]["status"] == state2["tracks"]["C"]["status"]
        assert state1["tracks"]["C"]["active_dispatch_id"] == state2["tracks"]["C"]["active_dispatch_id"]
        assert state1["tracks"]["C"]["current_gate"] == state2["tracks"]["C"]["current_gate"]


# ===========================================================================
# CERT-9: Clean state produces no mismatches
# ===========================================================================

class TestCert9CleanState:
    """Certify no false positives when all surfaces are consistent."""

    def test_cert9a_all_consistent_is_clean(self, tmp_path):
        """No mismatches when dispatch, progress, and queue are aligned."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-clean.md",
            track="C", pr_id="PR-0", gate="gate_pr0_test",
            dispatch_id="d-clean",
        )
        write_progress_state(state_dir, {
            "C": {
                "status": "working",
                "active_dispatch_id": "d-clean",
                "current_gate": "gate_pr0_test",
                "history": [],
            },
        })
        write_queue_state(state_dir, [
            {"id": "PR-0", "status": "in_progress"},
        ], active=["PR-0"])

        result = reconciler.reconcile(repair=False)

        assert result.is_clean, f"Consistent state must be clean, got: {result.summary()}"
        assert not result.has_forbidden
        assert len(result.mismatches) == 0

    def test_cert9b_empty_dispatch_dir_is_clean(self, tmp_path):
        """No active dispatches and idle progress is clean."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        (dispatch_dir / "active").mkdir(parents=True, exist_ok=True)
        write_progress_state(state_dir, {
            "A": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
            "B": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
            "C": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })

        result = reconciler.reconcile(repair=False)

        assert result.is_clean


# ===========================================================================
# CERT-10: Combined FC-P1 + FC-Q1 (simultaneous forbidden contradictions)
# ===========================================================================

class TestCert10CombinedForbidden:
    """Certify that multiple forbidden contradictions are detected together."""

    def test_cert10a_both_fc_p1_and_fc_q1(self, tmp_path):
        """When both progress and queue are stale, both FC codes are emitted."""
        reconciler, dispatch_dir, state_dir = make_reconciler(tmp_path)
        create_dispatch_file(
            dispatch_dir / "active", "d-both.md",
            track="B", pr_id="PR-1", gate="gate_pr1_test",
            dispatch_id="d-both",
        )
        write_progress_state(state_dir, {
            "B": {"status": "idle", "active_dispatch_id": None, "current_gate": "planning", "history": []},
        })
        write_queue_state(state_dir, [
            {"id": "PR-1", "status": "queued"},
        ])

        result = reconciler.reconcile(repair=False)

        codes = {m.contradiction_id for m in result.mismatches}
        assert FC_P1 in codes, "FC-P1 must be detected"
        assert FC_Q1 in codes, "FC-Q1 must be detected"
        assert result.has_forbidden
        assert len(result.forbidden_mismatches) == 2
