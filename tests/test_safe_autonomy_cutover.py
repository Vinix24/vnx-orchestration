#!/usr/bin/env python3
"""Tests for safe autonomy cutover — phase detection, prerequisites, rollback, certification."""

import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

from governance_evaluator import (
    DECISION_TYPE_REGISTRY,
    evaluate_policy,
    is_enforcement_enabled,
    transition_escalation,
)
from runtime_coordination import get_connection, init_schema
from safe_autonomy_cutover import (
    PHASE_FULL_ENFORCEMENT,
    PHASE_PROVENANCE_ONLY,
    PHASE_ROLLBACK,
    PHASE_SHADOW,
    CutoverStatus,
    detect_current_phase,
    execute_cutover,
    execute_rollback,
    get_cutover_status,
    prepare_cutover,
    validate_prerequisites,
    verify_autonomy_envelope,
)
from trace_token_validator import EnforcementMode, validate_trace_token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path):
    """Create a temporary state directory with all schema migrations."""
    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(str(sd))
    schema_dir = Path(__file__).resolve().parent.parent / "schemas"
    with get_connection(str(sd)) as conn:
        for v in (5, 6, 7):
            sql_path = schema_dir / f"runtime_coordination_v{v}.sql"
            if sql_path.exists():
                conn.executescript(sql_path.read_text())
        conn.commit()
    return str(sd)


@pytest.fixture
def conn(state_dir):
    with get_connection(state_dir) as c:
        yield c


@pytest.fixture
def hooks_dir(tmp_path):
    """Create mock git hooks directory."""
    hd = tmp_path / "hooks" / "git"
    hd.mkdir(parents=True)
    (hd / "prepare-commit-msg").write_text("#!/bin/sh\n")
    (hd / "commit-msg").write_text("#!/bin/sh\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Phase detection
# ---------------------------------------------------------------------------

class TestPhaseDetection:
    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "0", "VNX_PROVENANCE_ENFORCEMENT": "0"})
    def test_shadow_phase(self):
        assert detect_current_phase() == PHASE_SHADOW

    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "0", "VNX_PROVENANCE_ENFORCEMENT": "1"})
    def test_provenance_only_phase(self):
        assert detect_current_phase() == PHASE_PROVENANCE_ONLY

    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "1", "VNX_PROVENANCE_ENFORCEMENT": "1"})
    def test_full_enforcement_phase(self):
        assert detect_current_phase() == PHASE_FULL_ENFORCEMENT

    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "1", "VNX_PROVENANCE_ENFORCEMENT": "0"})
    def test_unusual_state_defaults_to_shadow(self):
        # Autonomy without provenance is unusual, defaults to shadow
        assert detect_current_phase() == PHASE_SHADOW


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

class TestPrerequisites:
    def test_policy_matrix_complete(self, conn):
        checks = validate_prerequisites(conn)
        matrix_check = next(c for c in checks if c.name == "policy_matrix_complete")
        assert matrix_check.passed

    def test_governance_schema_ready(self, conn):
        checks = validate_prerequisites(conn)
        schema_check = next(c for c in checks if c.name == "governance_schema_ready")
        assert schema_check.passed

    def test_provenance_registry_ready(self, conn):
        checks = validate_prerequisites(conn)
        registry_check = next(c for c in checks if c.name == "provenance_registry_ready")
        assert registry_check.passed

    def test_verification_table_ready(self, conn):
        checks = validate_prerequisites(conn)
        verify_check = next(c for c in checks if c.name == "verification_table_ready")
        assert verify_check.passed

    def test_authority_preserved(self, conn):
        checks = validate_prerequisites(conn)
        auth_check = next(c for c in checks if c.name == "authority_preserved")
        assert auth_check.passed

    def test_policy_classes_covered(self, conn):
        checks = validate_prerequisites(conn)
        coverage_check = next(c for c in checks if c.name == "policy_classes_covered")
        assert coverage_check.passed

    def test_no_blocking_escalations_clean(self, conn):
        checks = validate_prerequisites(conn)
        esc_check = next(c for c in checks if c.name == "no_blocking_escalations")
        assert esc_check.passed

    def test_no_blocking_escalations_with_hold(self, conn):
        transition_escalation(conn, entity_type="dispatch", entity_id="blocker-1", new_level="hold")
        checks = validate_prerequisites(conn)
        esc_check = next(c for c in checks if c.name == "no_blocking_escalations")
        assert not esc_check.passed

    def test_git_hooks_present(self, conn, hooks_dir):
        checks = validate_prerequisites(conn, repo_root=hooks_dir)
        hooks_check = next(c for c in checks if c.name == "git_hooks_present")
        assert hooks_check.passed

    def test_git_hooks_missing(self, conn, tmp_path):
        checks = validate_prerequisites(conn, repo_root=tmp_path)
        hooks_check = next(c for c in checks if c.name == "git_hooks_present")
        assert not hooks_check.passed


# ---------------------------------------------------------------------------
# Cutover status
# ---------------------------------------------------------------------------

class TestCutoverStatus:
    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "0", "VNX_PROVENANCE_ENFORCEMENT": "0"})
    def test_status_in_shadow(self, conn):
        status = get_cutover_status(conn)
        assert status.phase == PHASE_SHADOW
        assert not status.autonomy_enforcement
        assert not status.provenance_enforcement

    def test_status_has_residual_risks(self, conn):
        status = get_cutover_status(conn)
        assert len(status.residual_risks) >= 3

    def test_status_serializable(self, conn):
        status = get_cutover_status(conn)
        d = status.to_dict()
        assert "phase" in d
        assert "prerequisites" in d
        assert "residual_risks" in d


# ---------------------------------------------------------------------------
# Prepare cutover
# ---------------------------------------------------------------------------

class TestPrepareCutover:
    def test_prepare_returns_readiness(self, conn):
        report = prepare_cutover(conn)
        assert "ready" in report
        assert "recommendation" in report
        assert "current_phase" in report

    def test_prepare_emits_event(self, conn):
        prepare_cutover(conn)
        conn.commit()
        events = conn.execute(
            "SELECT * FROM coordination_events WHERE event_type = 'cutover_prepared'"
        ).fetchall()
        assert len(events) >= 1


# ---------------------------------------------------------------------------
# Execute cutover
# ---------------------------------------------------------------------------

class TestExecuteCutover:
    def test_cutover_requires_authority(self, conn):
        result = execute_cutover(conn, actor="runtime", justification="test")
        assert not result["success"]

    def test_cutover_requires_justification(self, conn):
        result = execute_cutover(conn, actor="t0", justification="")
        assert not result["success"]

    def test_cutover_records_event(self, conn):
        result = execute_cutover(conn, actor="t0", justification="FP-D certification complete")
        assert result["success"]
        assert result["target_phase"] == PHASE_FULL_ENFORCEMENT
        assert "flag_instructions" in result

        conn.commit()
        events = conn.execute(
            "SELECT * FROM coordination_events WHERE event_type = 'cutover_executed'"
        ).fetchall()
        assert len(events) >= 1

    def test_cutover_returns_flag_instructions(self, conn):
        result = execute_cutover(conn, actor="t0", justification="test")
        flags = result["flag_instructions"]
        assert flags["VNX_AUTONOMY_EVALUATION"] == "1"
        assert flags["VNX_PROVENANCE_ENFORCEMENT"] == "1"


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

class TestRollback:
    def test_rollback_requires_authority(self, conn):
        result = execute_rollback(conn, actor="runtime", justification="test")
        assert not result.success

    def test_rollback_requires_justification(self, conn):
        result = execute_rollback(conn, actor="t0", justification="")
        assert not result.success

    def test_rollback_records_event(self, conn):
        result = execute_rollback(conn, actor="t0", justification="Reverting due to issue")
        assert result.success
        assert result.new_phase == PHASE_ROLLBACK
        assert len(result.actions_taken) > 0

        conn.commit()
        events = conn.execute(
            "SELECT * FROM coordination_events WHERE event_type = 'cutover_rollback'"
        ).fetchall()
        assert len(events) >= 1

    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "1", "VNX_PROVENANCE_ENFORCEMENT": "1"})
    def test_rollback_from_enforcement_warns(self, conn):
        result = execute_rollback(conn, actor="t0", justification="Emergency rollback")
        assert result.success
        assert len(result.warnings) > 0


# ---------------------------------------------------------------------------
# Autonomy envelope verification
# ---------------------------------------------------------------------------

class TestAutonomyEnvelope:
    def test_envelope_passes(self, conn):
        result = verify_autonomy_envelope(conn)
        assert result["passed"]
        assert result["automatic_count"] > 0
        assert result["gated_count"] > 0
        assert result["forbidden_count"] > 0

    def test_merge_is_forbidden(self, conn):
        result = verify_autonomy_envelope(conn)
        merge_findings = [f for f in result["findings"] if f.get("type") == "merge_authority_violation"]
        assert len(merge_findings) == 0

    def test_completion_is_gated(self, conn):
        result = verify_autonomy_envelope(conn)
        completion_findings = [f for f in result["findings"] if f.get("type") == "completion_authority_violation"]
        assert len(completion_findings) == 0


# ---------------------------------------------------------------------------
# Certification runner
# ---------------------------------------------------------------------------

class TestCertification:
    def test_full_certification_passes(self):
        from fpd_certification import run_certification
        report = run_certification()
        assert report.certified, f"Failed rows: {[r.row_id for r in report.rows if r.status == 'fail']}"

    def test_certification_section_7(self):
        from fpd_certification import run_certification
        report = run_certification(sections=[7])
        assert report.passed > 0
        section_7_rows = [r for r in report.rows if r.section == 7]
        assert len(section_7_rows) == 8

    def test_certification_report_serializable(self):
        from fpd_certification import run_certification
        report = run_certification(sections=[1])
        d = report.to_dict()
        assert "certified" in d
        assert "rows" in d
        import json
        json.dumps(d)  # Must not raise


# ---------------------------------------------------------------------------
# Integration: enforcement mode behavior
# ---------------------------------------------------------------------------

class TestEnforcementIntegration:
    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "1"})
    def test_enforcement_blocks_forbidden_action(self, conn):
        from governance_evaluator import ForbiddenActionError, check_action
        with pytest.raises(ForbiddenActionError):
            check_action(conn, action="branch_merge", actor="runtime",
                context={"dispatch_id": "enforce-test"})

    @patch.dict(os.environ, {"VNX_AUTONOMY_EVALUATION": "0"})
    def test_shadow_does_not_block(self, conn):
        from governance_evaluator import check_action
        result = check_action(conn, action="branch_merge", actor="runtime",
            context={"dispatch_id": "shadow-test"})
        assert result["outcome"] == "forbidden"
        assert not result["enforcement"]

    def test_provenance_enforcement_blocks_no_token(self):
        r = validate_trace_token("fix: no token", EnforcementMode.ENFORCED)
        assert not r.valid
        assert r.severity.value == "error"

    def test_provenance_shadow_warns_no_token(self):
        r = validate_trace_token("fix: no token", EnforcementMode.SHADOW)
        assert not r.valid
        assert r.severity.value == "warning"

    def test_high_risk_always_gated(self, conn):
        """Verify G-R4: completion, merge, config actions stay gated/forbidden."""
        for action in ("dispatch_complete", "pr_close", "feature_certify"):
            r = evaluate_policy(action=action, actor="runtime", conn=conn)
            assert r["outcome"] == "gated", f"{action} should be gated"

        for action in ("branch_merge", "force_push"):
            r = evaluate_policy(action=action, actor="runtime", conn=conn)
            assert r["outcome"] == "forbidden", f"{action} should be forbidden"
