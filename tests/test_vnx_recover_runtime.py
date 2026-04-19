#!/usr/bin/env python3
"""
Tests for VNX Recover Runtime — PR-5 operator recovery engine.

Covers:
  - Healthy runtime: recovery is clean, no actions needed
  - Degraded runtime: expired leases, stuck dispatches, stale incidents
  - Blocked runtime: hard preflight blockers prevent recovery
  - Idempotency: repeated recovery runs produce no compound incidents
  - Lease reconciliation: expire stale, recover expired, release orphans
  - Dispatch reconciliation: timeout stuck, flag for review
  - Incident summary: correct aggregation from canonical state
  - tmux reconciliation: profile verification, remap detection
  - Cutover check: runtime core status reporting
  - Dry-run mode: detection without state mutation
  - Recovery event: coordination event emitted on completion (G-R3)

Quality gate: gate_pr5_recover_cutover_and_certification
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts/lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from vnx_recover_runtime import (
    RecoveryAction,
    RecoveryReport,
    run_recovery,
    _phase_preflight,
    _phase_lease_reconciliation,
    _phase_dispatch_reconciliation,
    _phase_incident_reconciliation,
    _phase_cutover_check,
    _now_utc,
    RECOVERY_ACTOR,
)
from runtime_coordination import (
    get_connection,
    init_schema,
    transition_dispatch,
    _append_event,
)
from lease_manager import LeaseManager
from incident_log import create_incident, consume_budget


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path):
    """Provide a temporary state directory with initialized schema."""
    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(sd)
    return sd


@pytest.fixture
def empty_state_dir(tmp_path):
    """Provide a temporary state directory with no database."""
    sd = tmp_path / "state"
    sd.mkdir()
    return sd


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _past_iso(seconds: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _future_iso(seconds: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _insert_dispatch(conn, dispatch_id, state="queued", terminal_id=None, updated_at=None):
    """Insert a dispatch row directly for testing."""
    now = updated_at or _now_iso()
    conn.execute(
        """
        INSERT OR REPLACE INTO dispatches
            (dispatch_id, state, terminal_id, priority, attempt_count,
             created_at, updated_at, metadata_json)
        VALUES (?, ?, ?, 'P1', 1, ?, ?, '{}')
        """,
        (dispatch_id, state, terminal_id, now, now),
    )
    conn.commit()


def _ensure_dispatch_exists(state_dir, dispatch_id, state="queued", terminal_id=None):
    """Ensure a dispatch row exists (needed for FK constraints on leases)."""
    with get_connection(state_dir) as conn:
        existing = conn.execute(
            "SELECT dispatch_id FROM dispatches WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        if not existing:
            _insert_dispatch(conn, dispatch_id, state=state, terminal_id=terminal_id)


def _make_lease_active(state_dir, terminal_id, dispatch_id, expires_seconds=600):
    """Create an active lease via canonical lease manager."""
    _ensure_dispatch_exists(state_dir, dispatch_id, terminal_id=terminal_id)
    mgr = LeaseManager(state_dir, auto_init=False)
    result = mgr.acquire(
        terminal_id, dispatch_id,
        lease_seconds=expires_seconds,
        actor="test",
    )
    return result


def _make_lease_expired(state_dir, terminal_id, dispatch_id):
    """Create a lease, then expire it."""
    _ensure_dispatch_exists(state_dir, dispatch_id, terminal_id=terminal_id)
    mgr = LeaseManager(state_dir, auto_init=False)
    # Set a very short TTL and manually expire
    result = mgr.acquire(terminal_id, dispatch_id, lease_seconds=1, actor="test")
    # Force expires_at into the past
    with get_connection(state_dir) as conn:
        conn.execute(
            "UPDATE terminal_leases SET expires_at = ? WHERE terminal_id = ?",
            (_past_iso(100), terminal_id),
        )
        conn.commit()
    mgr.expire(terminal_id, actor="test", reason="test_expired")
    return result


def _create_session_profile(state_dir, session_name="test-vnx"):
    """Create a minimal session profile for testing."""
    profile = {
        "schema_version": 1,
        "session_name": session_name,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "home_window": {
            "name": "main",
            "window_type": "home",
            "panes": [
                {"terminal_id": "T0", "role": "orchestrator", "pane_id": "%0",
                 "provider": "claude_code", "model": "default", "work_dir": "/tmp/T0"},
                {"terminal_id": "T1", "role": "worker", "pane_id": "%1",
                 "provider": "claude_code", "model": "default", "track": "A",
                 "work_dir": "/tmp/T1"},
                {"terminal_id": "T2", "role": "worker", "pane_id": "%2",
                 "provider": "claude_code", "model": "default", "track": "B",
                 "work_dir": "/tmp/T2"},
                {"terminal_id": "T3", "role": "deep", "pane_id": "%3",
                 "provider": "claude_code", "model": "default", "track": "C",
                 "work_dir": "/tmp/T3"},
            ],
        },
        "dynamic_windows": [],
    }
    path = state_dir / "session_profile.json"
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return profile


# ---------------------------------------------------------------------------
# Test: Healthy runtime — clean recovery
# ---------------------------------------------------------------------------

class TestHealthyRuntime:
    """Recovery on a healthy runtime should produce no actions."""

    def test_clean_recovery(self, state_dir):
        report = run_recovery(state_dir)
        assert report.overall_status == "clean"
        assert report.leases_reconciled == 0
        assert report.dispatches_reconciled == 0
        assert report.remaining_blockers == []

    def test_clean_recovery_dry_run(self, state_dir):
        report = run_recovery(state_dir, dry_run=True)
        assert report.overall_status == "clean"
        assert report.dry_run is True

    def test_preflight_passes(self, state_dir):
        report = RecoveryReport(run_at=_now_utc(), dry_run=False)
        can_proceed = _phase_preflight(state_dir, report)
        assert can_proceed is True
        assert report.preflight_status in ("pass", "warn")


# ---------------------------------------------------------------------------
# Test: Blocked runtime — preflight blockers
# ---------------------------------------------------------------------------

class TestBlockedRuntime:
    """Hard preflight blockers should prevent recovery."""

    def test_no_database(self, empty_state_dir):
        report = run_recovery(empty_state_dir)
        assert report.overall_status == "blocked"
        assert any("BLOCKER" in b for b in report.remaining_blockers)
        # No lease/dispatch/incident actions should have been attempted
        lease_actions = [a for a in report.actions if a.phase == "lease"]
        assert len(lease_actions) == 0


# ---------------------------------------------------------------------------
# Test: Lease reconciliation
# ---------------------------------------------------------------------------

class TestLeaseReconciliation:
    """Lease reconciliation should expire stale, recover expired, release orphans."""

    def test_expire_stale_lease(self, state_dir):
        # Create a lease with TTL in the past
        _make_lease_active(state_dir, "T1", "d-001", expires_seconds=1)
        with get_connection(state_dir) as conn:
            conn.execute(
                "UPDATE terminal_leases SET expires_at = ? WHERE terminal_id = 'T1'",
                (_past_iso(100),),
            )
            conn.commit()

        report = run_recovery(state_dir)
        assert report.leases_reconciled >= 1
        lease_actions = [a for a in report.actions if a.phase == "lease"]
        assert any("expire" in a.action for a in lease_actions)

        # Verify lease is now idle
        mgr = LeaseManager(state_dir, auto_init=False)
        lease = mgr.get("T1")
        assert lease.state == "idle"

    def test_recover_expired_lease(self, state_dir):
        _make_lease_expired(state_dir, "T2", "d-002")

        report = run_recovery(state_dir)
        assert report.leases_reconciled >= 1

        mgr = LeaseManager(state_dir, auto_init=False)
        lease = mgr.get("T2")
        assert lease.state == "idle"

    def test_release_orphan_lease(self, state_dir):
        # Create lease and then mark its dispatch as completed
        _make_lease_active(state_dir, "T1", "d-orphan", expires_seconds=3600)
        with get_connection(state_dir) as conn:
            _insert_dispatch(conn, "d-orphan", state="completed", terminal_id="T1")

        report = run_recovery(state_dir)
        orphan_actions = [a for a in report.actions if "orphan" in a.action]
        assert len(orphan_actions) >= 1

    def test_lease_dry_run_no_mutation(self, state_dir):
        _make_lease_expired(state_dir, "T3", "d-003")

        report = run_recovery(state_dir, dry_run=True)
        assert report.leases_reconciled >= 1

        # Verify lease was NOT changed
        mgr = LeaseManager(state_dir, auto_init=False)
        lease = mgr.get("T3")
        assert lease.state == "expired"

    def test_project_terminal_state(self, state_dir):
        """Recovery should project canonical lease state to terminal_state.json."""
        report = run_recovery(state_dir)
        ts_path = state_dir / "terminal_state.json"
        assert ts_path.exists()


# ---------------------------------------------------------------------------
# Test: Dispatch reconciliation
# ---------------------------------------------------------------------------

class TestDispatchReconciliation:
    """Dispatch reconciliation should timeout stuck dispatches."""

    def test_timeout_stuck_dispatch(self, state_dir):
        with get_connection(state_dir) as conn:
            _insert_dispatch(
                conn, "d-stuck", state="delivering",
                terminal_id="T1", updated_at=_past_iso(600),
            )

        report = run_recovery(state_dir)
        assert report.dispatches_reconciled >= 1
        dispatch_actions = [a for a in report.actions if a.phase == "dispatch"]
        assert any("timeout" in a.action for a in dispatch_actions)

    def test_stuck_dispatch_auto_recovered(self, state_dir):
        """Dispatches in recoverable state should be auto-recovered (postmortem §4.4)."""
        with get_connection(state_dir) as conn:
            _insert_dispatch(
                conn, "d-failed", state="failed_delivery",
                terminal_id="T2",
            )

        report = run_recovery(state_dir)
        # Should be auto-recovered, not flagged for manual review
        assert any(
            a.action == "auto_recovered" and a.target == "d-failed"
            for a in report.actions
        )
        assert report.dispatches_reconciled >= 1


# ---------------------------------------------------------------------------
# Test: Incident reconciliation
# ---------------------------------------------------------------------------

class TestIncidentReconciliation:
    """Incident reconciliation should generate summary and resolve stale crashes."""

    def test_generates_incident_summary(self, state_dir):
        report = run_recovery(state_dir)
        assert "total_open" in report.incident_summary

    def test_resolves_stale_process_crashes(self, state_dir):
        create_incident(
            state_dir,
            incident_class="process_crash",
            entity_type="terminal",
            entity_id="T1",
            terminal_id="T1",
            failure_detail="test crash",
        )

        report = run_recovery(state_dir)
        assert report.incidents_resolved >= 1

    def test_resets_exhausted_budgets(self, state_dir):
        create_incident(
            state_dir,
            incident_class="process_crash",
            entity_type="terminal",
            entity_id="T1",
            terminal_id="T1",
        )
        # Exhaust the budget
        for _ in range(4):
            consume_budget(
                state_dir,
                entity_type="terminal",
                entity_id="T1",
                incident_class="process_crash",
            )

        report = run_recovery(state_dir)
        assert report.budgets_reset >= 1

    def test_collects_pending_escalations(self, state_dir):
        # Create an escalation by using the workflow supervisor
        from workflow_supervisor import WorkflowSupervisor
        from incident_taxonomy import IncidentClass

        ws = WorkflowSupervisor(state_dir)
        with get_connection(state_dir) as conn:
            _insert_dispatch(conn, "d-esc", state="failed_delivery", terminal_id="T1")

        # Trigger enough incidents to cause escalation
        ws.handle_incident(
            incident_class=IncidentClass.DELIVERY_FAILURE,
            dispatch_id="d-esc",
            terminal_id="T1",
            reason="test escalation",
        )
        ws.handle_incident(
            incident_class=IncidentClass.DELIVERY_FAILURE,
            dispatch_id="d-esc",
            terminal_id="T1",
            reason="test escalation 2",
        )
        ws.handle_incident(
            incident_class=IncidentClass.DELIVERY_FAILURE,
            dispatch_id="d-esc",
            terminal_id="T1",
            reason="test escalation 3",
        )

        report = run_recovery(state_dir)
        # Should have escalation items
        assert len(report.escalation_items) >= 1


# ---------------------------------------------------------------------------
# Test: tmux reconciliation
# ---------------------------------------------------------------------------

class TestTmuxReconciliation:
    """tmux reconciliation should verify profile and detect drift."""

    def test_no_profile_skipped(self, state_dir):
        report = RecoveryReport(run_at=_now_utc(), dry_run=False)
        from vnx_recover_runtime import _phase_tmux_reconciliation
        _phase_tmux_reconciliation(state_dir, report, dry_run=False)

        tmux_actions = [a for a in report.actions if a.phase == "tmux"]
        assert any("skipped" in a.outcome for a in tmux_actions)

    def test_profile_with_no_tmux_session(self, state_dir):
        """Profile exists but tmux is not running — all panes are missing."""
        _create_session_profile(state_dir)

        report = RecoveryReport(run_at=_now_utc(), dry_run=False)
        from vnx_recover_runtime import _phase_tmux_reconciliation
        _phase_tmux_reconciliation(state_dir, report, dry_run=False)

        # Without tmux running, panes should show as missing
        tmux_actions = [a for a in report.actions if a.phase == "tmux"]
        # Either verified clean (if _list_live_panes returns empty and all drift to missing)
        # or some are missing
        assert len(tmux_actions) >= 1


# ---------------------------------------------------------------------------
# Test: Idempotency (A-R8)
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Repeated recovery runs must not compound incidents or create duplicate state."""

    def test_double_recovery_is_noop(self, state_dir):
        # First run with expired lease
        _make_lease_expired(state_dir, "T1", "d-idem")

        report1 = run_recovery(state_dir)
        assert report1.leases_reconciled >= 1

        # Second run should be clean
        report2 = run_recovery(state_dir)
        assert report2.overall_status == "clean"
        assert report2.leases_reconciled == 0

    def test_triple_recovery_still_clean(self, state_dir):
        """Even three runs produce consistent results."""
        _make_lease_expired(state_dir, "T2", "d-triple")

        run_recovery(state_dir)
        run_recovery(state_dir)
        report3 = run_recovery(state_dir)
        assert report3.overall_status == "clean"

    def test_no_compound_incidents(self, state_dir):
        """Recovery should not create new incidents from its own actions."""
        create_incident(
            state_dir,
            incident_class="process_crash",
            entity_type="terminal",
            entity_id="T1",
            terminal_id="T1",
        )

        run_recovery(state_dir)
        run_recovery(state_dir)

        # Check incident count hasn't grown
        from incident_log import get_incident_history
        incidents = get_incident_history(state_dir, terminal_id="T1")
        # Should have exactly 1 incident (the original one, now resolved)
        process_crashes = [i for i in incidents if i["incident_class"] == "process_crash"]
        assert len(process_crashes) == 1


# ---------------------------------------------------------------------------
# Test: Cutover check
# ---------------------------------------------------------------------------

class TestCutoverCheck:
    """Cutover check should report runtime core status."""

    def test_runtime_primary_active(self, state_dir):
        report = RecoveryReport(run_at=_now_utc(), dry_run=False)
        with patch.dict(os.environ, {"VNX_RUNTIME_PRIMARY": "1"}):
            _phase_cutover_check(state_dir, report)

        cutover_actions = [a for a in report.actions if a.phase == "cutover"]
        assert any("ACTIVE" in a.detail for a in cutover_actions)
        assert any("rollback" in a.detail.lower() for a in cutover_actions)

    def test_runtime_primary_inactive(self, state_dir):
        report = RecoveryReport(run_at=_now_utc(), dry_run=False)
        with patch.dict(os.environ, {"VNX_RUNTIME_PRIMARY": "0"}):
            _phase_cutover_check(state_dir, report)

        cutover_actions = [a for a in report.actions if a.phase == "cutover"]
        assert any("INACTIVE" in a.detail for a in cutover_actions)


# ---------------------------------------------------------------------------
# Test: Recovery event emission (G-R3)
# ---------------------------------------------------------------------------

class TestRecoveryEvent:
    """Recovery should emit a coordination event for audit trail."""

    def test_recovery_event_emitted(self, state_dir):
        run_recovery(state_dir)

        with get_connection(state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE event_type = 'recovery_completed'"
            ).fetchall()

        assert len(events) >= 1
        event = dict(events[0])
        assert event["actor"] == RECOVERY_ACTOR
        assert event["entity_type"] == "runtime"

    def test_no_event_on_dry_run(self, state_dir):
        run_recovery(state_dir, dry_run=True)

        with get_connection(state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE event_type = 'recovery_completed'"
            ).fetchall()

        assert len(events) == 0


# ---------------------------------------------------------------------------
# Test: Report formatting
# ---------------------------------------------------------------------------

class TestReportFormatting:
    """Recovery report should have correct structure and formatting."""

    def test_report_to_dict(self, state_dir):
        report = run_recovery(state_dir)
        d = report.to_dict()
        assert "overall_status" in d
        assert "actions" in d
        assert "incident_summary" in d
        assert "escalation_items" in d
        assert "remaining_blockers" in d

    def test_summary_text(self, state_dir):
        report = run_recovery(state_dir)
        text = report.summary_text()
        assert "VNX Recovery Report" in text
        assert "Leases reconciled" in text
        assert "Dispatches reconciled" in text

    def test_overall_status_values(self, state_dir):
        # Clean
        report = run_recovery(state_dir)
        assert report.overall_status in ("clean", "recovered")

    def test_blocked_status(self, empty_state_dir):
        report = run_recovery(empty_state_dir)
        assert report.overall_status == "blocked"


# ---------------------------------------------------------------------------
# Test: Full integration — degraded scenario
# ---------------------------------------------------------------------------

class TestDegradedScenario:
    """Full integration test with multiple degraded conditions."""

    def test_full_degraded_recovery(self, state_dir):
        """Simulate a runtime with expired lease, stuck dispatch, and stale incident."""
        # Expired lease
        _make_lease_expired(state_dir, "T1", "d-degrade-1")

        # Stuck dispatch
        with get_connection(state_dir) as conn:
            _insert_dispatch(
                conn, "d-degrade-2", state="delivering",
                terminal_id="T2", updated_at=_past_iso(700),
            )

        # Stale incident
        create_incident(
            state_dir,
            incident_class="process_crash",
            entity_type="terminal",
            entity_id="T3",
            terminal_id="T3",
        )

        report = run_recovery(state_dir)

        # Should have recovered from all issues
        assert report.overall_status in ("recovered", "clean")
        assert report.leases_reconciled >= 1
        assert report.dispatches_reconciled >= 1
        assert report.incidents_resolved >= 1

        # Verify idempotency
        report2 = run_recovery(state_dir)
        assert report2.overall_status == "clean"
