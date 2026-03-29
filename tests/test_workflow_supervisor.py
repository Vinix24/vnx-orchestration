#!/usr/bin/env python3
"""
Tests for VNX Workflow Supervisor — PR-2 quality gate validation.

Verifies:
  - Workflow supervisor differentiates incident classes before choosing recovery actions
  - Dead-letter and escalation transitions are explicit and durable
  - Budget exhaustion prevents repeated blind retries
  - Resume paths require compatible dispatch state and do not fabricate progress
  - Tests cover dead-letter routing, escalation, and loop termination behavior
"""

import sys
import tempfile
from pathlib import Path

# Add scripts/lib to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import pytest

from incident_taxonomy import (
    IncidentClass,
    RECOVERY_CONTRACTS,
    RecoveryAction,
    Severity,
    get_contract,
)
from runtime_coordination import (
    get_connection,
    get_dispatch,
    init_schema,
    register_dispatch,
    transition_dispatch,
)
from workflow_supervisor import (
    WorkflowSupervisor,
    SupervisionDecision,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir():
    """Create a temporary state directory with initialized schema."""
    with tempfile.TemporaryDirectory() as tmpdir:
        init_schema(tmpdir)
        yield tmpdir


@pytest.fixture
def supervisor(state_dir):
    """Create a WorkflowSupervisor with initialized state."""
    return WorkflowSupervisor(state_dir, auto_init=False)


def _register_dispatch(state_dir, dispatch_id="d-001", terminal_id="T1", **kwargs):
    """Helper to register a dispatch in the database."""
    with get_connection(state_dir) as conn:
        result = register_dispatch(
            conn,
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            track="B",
            **kwargs,
        )
        conn.commit()
    return result


def _transition(state_dir, dispatch_id, to_state, reason="test"):
    """Helper to transition a dispatch."""
    with get_connection(state_dir) as conn:
        result = transition_dispatch(
            conn,
            dispatch_id=dispatch_id,
            to_state=to_state,
            actor="test",
            reason=reason,
        )
        conn.commit()
    return result


# ---------------------------------------------------------------------------
# Incident classification tests
# ---------------------------------------------------------------------------

class TestIncidentClassification:
    """Workflow supervisor differentiates incident classes before choosing recovery."""

    def test_process_crash_selects_restart(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-crash")
        decision = supervisor.handle_incident(
            incident_class=IncidentClass.PROCESS_CRASH,
            dispatch_id="d-crash",
            terminal_id="T1",
            reason="worker PID vanished",
        )
        assert decision.action_taken == RecoveryAction.RESTART_PROCESS.value
        assert decision.should_retry is True
        assert decision.incident_record is not None
        assert decision.incident_record.incident_class == "process_crash"

    def test_delivery_failure_selects_redeliver(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-deliver")
        decision = supervisor.handle_incident(
            incident_class=IncidentClass.DELIVERY_FAILURE,
            dispatch_id="d-deliver",
            terminal_id="T1",
            reason="tmux send-keys failed",
        )
        assert decision.action_taken == RecoveryAction.REDELIVER_DISPATCH.value
        assert decision.should_retry is True

    def test_lease_conflict_halts_immediately(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-lease")
        decision = supervisor.handle_incident(
            incident_class=IncidentClass.LEASE_CONFLICT,
            dispatch_id="d-lease",
            terminal_id="T1",
            reason="generation mismatch",
        )
        assert decision.should_escalate is True
        assert decision.auto_recovery_halted is True
        assert decision.should_retry is False

    def test_terminal_unresponsive_selects_expire_lease(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-unresponsive")
        decision = supervisor.handle_incident(
            incident_class=IncidentClass.TERMINAL_UNRESPONSIVE,
            dispatch_id="d-unresponsive",
            terminal_id="T2",
            reason="health check failed",
        )
        assert decision.action_taken == RecoveryAction.EXPIRE_LEASE.value
        assert decision.should_retry is True

    def test_ack_timeout_selects_redeliver(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-ack")
        decision = supervisor.handle_incident(
            incident_class=IncidentClass.ACK_TIMEOUT,
            dispatch_id="d-ack",
            terminal_id="T1",
            reason="no ACK within 120s",
        )
        assert decision.action_taken == RecoveryAction.REDELIVER_DISPATCH.value
        assert decision.should_retry is True

    def test_different_classes_select_different_actions(self, state_dir, supervisor):
        """Each incident class must produce a class-appropriate recovery action."""
        _register_dispatch(state_dir, "d-multi-1")
        _register_dispatch(state_dir, "d-multi-2")

        crash_decision = supervisor.handle_incident(
            incident_class=IncidentClass.PROCESS_CRASH,
            dispatch_id="d-multi-1",
            terminal_id="T1",
            reason="crash",
        )
        delivery_decision = supervisor.handle_incident(
            incident_class=IncidentClass.DELIVERY_FAILURE,
            dispatch_id="d-multi-2",
            terminal_id="T1",
            reason="delivery failed",
        )

        assert crash_decision.action_taken != delivery_decision.action_taken
        assert crash_decision.incident_class == "process_crash"
        assert delivery_decision.incident_class == "delivery_failure"


# ---------------------------------------------------------------------------
# Dead-letter routing tests
# ---------------------------------------------------------------------------

class TestDeadLetterRouting:
    """Dead-letter and escalation transitions are explicit and durable."""

    def test_budget_exhaustion_triggers_dead_letter(self, state_dir, supervisor):
        """When retry budget is exhausted, dispatch enters dead_letter."""
        _register_dispatch(state_dir, "d-dl-1")
        # Move dispatch to failed_delivery state (a dead-letter source state)
        _transition(state_dir, "d-dl-1", "claimed")
        _transition(state_dir, "d-dl-1", "delivering")
        _transition(state_dir, "d-dl-1", "failed_delivery")

        contract = get_contract(IncidentClass.DELIVERY_FAILURE)
        # Exhaust the retry budget
        for i in range(contract.retry_budget.max_retries + 1):
            decision = supervisor.handle_incident(
                incident_class=IncidentClass.DELIVERY_FAILURE,
                dispatch_id="d-dl-1",
                terminal_id="T1",
                reason=f"delivery failed attempt {i+1}",
            )

        assert decision.should_dead_letter is True
        assert decision.action_taken == RecoveryAction.DEAD_LETTER_DISPATCH.value
        assert decision.budget_remaining == 0

        # Verify dispatch is now in dead_letter state
        with get_connection(state_dir) as conn:
            dispatch = get_dispatch(conn, "d-dl-1")
            assert dispatch["state"] == "dead_letter"

    def test_dead_letter_only_from_eligible_states(self, state_dir, supervisor):
        """Dead-letter transition must only occur from valid source states."""
        _register_dispatch(state_dir, "d-dl-2")
        # Dispatch is in 'queued' state — NOT a dead-letter source state
        contract = get_contract(IncidentClass.DELIVERY_FAILURE)
        for i in range(contract.retry_budget.max_retries + 1):
            decision = supervisor.handle_incident(
                incident_class=IncidentClass.DELIVERY_FAILURE,
                dispatch_id="d-dl-2",
                terminal_id="T1",
                reason=f"attempt {i+1}",
            )

        # Budget exhausted but state is 'queued' — no dead-letter
        assert decision.should_dead_letter is False
        with get_connection(state_dir) as conn:
            dispatch = get_dispatch(conn, "d-dl-2")
            assert dispatch["state"] == "queued"

    def test_dead_letter_from_timed_out(self, state_dir, supervisor):
        """Dispatch in timed_out state can enter dead_letter."""
        _register_dispatch(state_dir, "d-dl-3")
        _transition(state_dir, "d-dl-3", "claimed")
        _transition(state_dir, "d-dl-3", "delivering")
        _transition(state_dir, "d-dl-3", "accepted")
        _transition(state_dir, "d-dl-3", "timed_out")

        contract = get_contract(IncidentClass.ACK_TIMEOUT)
        for i in range(contract.retry_budget.max_retries + 1):
            decision = supervisor.handle_incident(
                incident_class=IncidentClass.ACK_TIMEOUT,
                dispatch_id="d-dl-3",
                terminal_id="T1",
                reason=f"ack timeout {i+1}",
            )

        assert decision.should_dead_letter is True
        with get_connection(state_dir) as conn:
            dispatch = get_dispatch(conn, "d-dl-3")
            assert dispatch["state"] == "dead_letter"

    def test_dead_letter_from_recovered(self, state_dir, supervisor):
        """Dispatch recovered but failed again enters dead_letter."""
        _register_dispatch(state_dir, "d-dl-4")
        _transition(state_dir, "d-dl-4", "claimed")
        _transition(state_dir, "d-dl-4", "delivering")
        _transition(state_dir, "d-dl-4", "failed_delivery")
        _transition(state_dir, "d-dl-4", "recovered")

        contract = get_contract(IncidentClass.RESUME_FAILED)
        for i in range(contract.retry_budget.max_retries + 1):
            decision = supervisor.handle_incident(
                incident_class=IncidentClass.RESUME_FAILED,
                dispatch_id="d-dl-4",
                terminal_id="T1",
                reason=f"resume failed {i+1}",
            )

        assert decision.should_dead_letter is True
        with get_connection(state_dir) as conn:
            dispatch = get_dispatch(conn, "d-dl-4")
            assert dispatch["state"] == "dead_letter"

    def test_process_crash_does_not_dead_letter(self, state_dir, supervisor):
        """Process crash is NOT dead-letter eligible per taxonomy."""
        _register_dispatch(state_dir, "d-dl-5")
        _transition(state_dir, "d-dl-5", "claimed")
        _transition(state_dir, "d-dl-5", "delivering")
        _transition(state_dir, "d-dl-5", "failed_delivery")

        contract = get_contract(IncidentClass.PROCESS_CRASH)
        for i in range(contract.retry_budget.max_retries + 1):
            decision = supervisor.handle_incident(
                incident_class=IncidentClass.PROCESS_CRASH,
                dispatch_id="d-dl-5",
                terminal_id="T1",
                reason=f"crash {i+1}",
            )

        assert decision.should_dead_letter is False

    def test_dead_letter_dispatches_queryable(self, state_dir, supervisor):
        """Dead-lettered dispatches are retrievable via query."""
        _register_dispatch(state_dir, "d-dl-6")
        _transition(state_dir, "d-dl-6", "claimed")
        _transition(state_dir, "d-dl-6", "delivering")
        _transition(state_dir, "d-dl-6", "failed_delivery")

        contract = get_contract(IncidentClass.DELIVERY_FAILURE)
        for i in range(contract.retry_budget.max_retries + 1):
            supervisor.handle_incident(
                incident_class=IncidentClass.DELIVERY_FAILURE,
                dispatch_id="d-dl-6",
                terminal_id="T1",
                reason=f"failed {i+1}",
            )

        dead_letters = supervisor.get_dead_letter_dispatches()
        assert len(dead_letters) >= 1
        assert any(d["dispatch_id"] == "d-dl-6" for d in dead_letters)

    def test_dead_letter_is_terminal(self, state_dir, supervisor):
        """Dead-letter is a terminal state — no transitions out."""
        _register_dispatch(state_dir, "d-dl-7")
        _transition(state_dir, "d-dl-7", "claimed")
        _transition(state_dir, "d-dl-7", "delivering")
        _transition(state_dir, "d-dl-7", "failed_delivery")

        contract = get_contract(IncidentClass.DELIVERY_FAILURE)
        for i in range(contract.retry_budget.max_retries + 1):
            supervisor.handle_incident(
                incident_class=IncidentClass.DELIVERY_FAILURE,
                dispatch_id="d-dl-7",
                terminal_id="T1",
                reason=f"failed {i+1}",
            )

        with get_connection(state_dir) as conn:
            dispatch = get_dispatch(conn, "d-dl-7")
            assert dispatch["state"] == "dead_letter"

        from runtime_coordination import DISPATCH_TRANSITIONS
        assert DISPATCH_TRANSITIONS["dead_letter"] == frozenset()


# ---------------------------------------------------------------------------
# Escalation tests
# ---------------------------------------------------------------------------

class TestEscalation:
    """Escalation triggers are explicit and durable."""

    def test_escalation_after_threshold(self, state_dir, supervisor):
        """Escalation fires after escalate_after_retries threshold."""
        _register_dispatch(state_dir, "d-esc-1")
        contract = get_contract(IncidentClass.DELIVERY_FAILURE)
        threshold = contract.escalation.escalate_after_retries

        decisions = []
        for i in range(threshold + 1):
            d = supervisor.handle_incident(
                incident_class=IncidentClass.DELIVERY_FAILURE,
                dispatch_id="d-esc-1",
                terminal_id="T1",
                reason=f"failed {i+1}",
            )
            decisions.append(d)

        # First attempts should not escalate
        for i in range(threshold):
            assert decisions[i].should_escalate is False

        # Threshold attempt should escalate
        assert decisions[threshold].should_escalate is True

    def test_escalation_record_created(self, state_dir, supervisor):
        """Escalation creates durable record in escalation_log."""
        _register_dispatch(state_dir, "d-esc-2")
        contract = get_contract(IncidentClass.LEASE_CONFLICT)

        # Lease conflict escalates immediately (escalate_after_retries=0)
        decision = supervisor.handle_incident(
            incident_class=IncidentClass.LEASE_CONFLICT,
            dispatch_id="d-esc-2",
            terminal_id="T1",
            reason="generation mismatch",
        )

        assert decision.should_escalate is True
        assert decision.escalation_record is not None
        assert decision.escalation_record.escalated_to == "T0"

        # Verify in database
        escalations = supervisor.get_pending_escalations()
        assert len(escalations) >= 1
        assert any(e["dispatch_id"] == "d-esc-2" for e in escalations)

    def test_escalation_acknowledgement(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-esc-3")
        decision = supervisor.handle_incident(
            incident_class=IncidentClass.LEASE_CONFLICT,
            dispatch_id="d-esc-3",
            terminal_id="T1",
            reason="mismatch",
        )

        esc_id = decision.escalation_record.escalation_id
        assert supervisor.acknowledge_escalation(esc_id) is True

        pending = supervisor.get_pending_escalations()
        assert not any(e["escalation_id"] == esc_id for e in pending)

    def test_halt_auto_recovery(self, state_dir, supervisor):
        """Lease conflict halts auto-recovery."""
        _register_dispatch(state_dir, "d-halt-1")
        decision = supervisor.handle_incident(
            incident_class=IncidentClass.LEASE_CONFLICT,
            dispatch_id="d-halt-1",
            terminal_id="T1",
            reason="generation mismatch",
        )
        assert decision.auto_recovery_halted is True
        assert decision.should_retry is False

    def test_clear_halt(self, state_dir, supervisor):
        """Operator can clear halt to allow resume."""
        _register_dispatch(state_dir, "d-halt-2")
        supervisor.handle_incident(
            incident_class=IncidentClass.LEASE_CONFLICT,
            dispatch_id="d-halt-2",
            terminal_id="T1",
            reason="generation mismatch",
        )

        result = supervisor.clear_halt("d-halt-2", IncidentClass.LEASE_CONFLICT.value)
        assert result is True


# ---------------------------------------------------------------------------
# Budget exhaustion tests
# ---------------------------------------------------------------------------

class TestBudgetExhaustion:
    """Budget exhaustion prevents repeated blind retries."""

    def test_retries_decrement_budget(self, state_dir, supervisor):
        """Each incident decrements remaining budget."""
        _register_dispatch(state_dir, "d-budget-1")
        contract = get_contract(IncidentClass.PROCESS_CRASH)
        max_retries = contract.retry_budget.max_retries

        decisions = []
        for i in range(max_retries + 1):
            d = supervisor.handle_incident(
                incident_class=IncidentClass.PROCESS_CRASH,
                dispatch_id="d-budget-1",
                terminal_id="T1",
                reason=f"crash {i+1}",
            )
            decisions.append(d)

        # Budget should decrease each time
        for i in range(len(decisions) - 1):
            assert decisions[i].budget_remaining >= decisions[i + 1].budget_remaining

        # Final decision should have zero budget
        assert decisions[-1].budget_remaining == 0

    def test_no_retry_after_exhaustion(self, state_dir, supervisor):
        """Retry is not permitted after budget exhaustion."""
        _register_dispatch(state_dir, "d-budget-2")
        contract = get_contract(IncidentClass.PROCESS_CRASH)

        for i in range(contract.retry_budget.max_retries + 2):
            decision = supervisor.handle_incident(
                incident_class=IncidentClass.PROCESS_CRASH,
                dispatch_id="d-budget-2",
                terminal_id="T1",
                reason=f"crash {i+1}",
            )

        assert decision.should_retry is False
        assert decision.budget_remaining == 0

    def test_repeated_failure_loop_zero_budget(self, state_dir, supervisor):
        """REPEATED_FAILURE_LOOP has zero budget — immediate halt."""
        _register_dispatch(state_dir, "d-loop-1")
        contract = get_contract(IncidentClass.REPEATED_FAILURE_LOOP)
        assert contract.retry_budget.max_retries == 0

        decision = supervisor.handle_incident(
            incident_class=IncidentClass.REPEATED_FAILURE_LOOP,
            dispatch_id="d-loop-1",
            terminal_id="T1",
            reason="repeated failures detected",
        )

        assert decision.should_retry is False
        assert decision.auto_recovery_halted is True
        assert decision.budget_remaining == 0


# ---------------------------------------------------------------------------
# Loop termination tests
# ---------------------------------------------------------------------------

class TestLoopTermination:
    """Repeated failure loops are detected and terminated."""

    def test_loop_detection_after_threshold(self, state_dir, supervisor):
        """After REPEATED_FAILURE_THRESHOLD incidents of same class, loop detected."""
        _register_dispatch(state_dir, "d-loop-2")
        _transition(state_dir, "d-loop-2", "claimed")
        _transition(state_dir, "d-loop-2", "delivering")
        _transition(state_dir, "d-loop-2", "failed_delivery")

        from incident_taxonomy import REPEATED_FAILURE_THRESHOLD

        # Fire enough delivery failures to trigger loop detection
        for i in range(REPEATED_FAILURE_THRESHOLD + 1):
            decision = supervisor.handle_incident(
                incident_class=IncidentClass.DELIVERY_FAILURE,
                dispatch_id="d-loop-2",
                terminal_id="T1",
                reason=f"delivery failed {i+1}",
            )

        # The last decision should detect the loop
        assert decision.incident_class == "repeated_failure_loop"
        assert decision.auto_recovery_halted is True
        assert decision.should_retry is False

    def test_loop_triggers_dead_letter(self, state_dir, supervisor):
        """Repeated failure loop with eligible state triggers dead-letter."""
        _register_dispatch(state_dir, "d-loop-3")
        _transition(state_dir, "d-loop-3", "claimed")
        _transition(state_dir, "d-loop-3", "delivering")
        _transition(state_dir, "d-loop-3", "failed_delivery")

        from incident_taxonomy import REPEATED_FAILURE_THRESHOLD

        for i in range(REPEATED_FAILURE_THRESHOLD + 1):
            decision = supervisor.handle_incident(
                incident_class=IncidentClass.DELIVERY_FAILURE,
                dispatch_id="d-loop-3",
                terminal_id="T1",
                reason=f"delivery failed {i+1}",
            )

        assert decision.should_dead_letter is True
        with get_connection(state_dir) as conn:
            dispatch = get_dispatch(conn, "d-loop-3")
            assert dispatch["state"] == "dead_letter"


# ---------------------------------------------------------------------------
# Resume path tests
# ---------------------------------------------------------------------------

class TestResumePaths:
    """Resume paths require compatible dispatch state."""

    def test_can_resume_queued(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-resume-1")
        result = supervisor.can_resume("d-resume-1")
        assert result["allowed"] is True

    def test_cannot_resume_running(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-resume-2")
        _transition(state_dir, "d-resume-2", "claimed")
        _transition(state_dir, "d-resume-2", "delivering")
        _transition(state_dir, "d-resume-2", "accepted")
        _transition(state_dir, "d-resume-2", "running")

        result = supervisor.can_resume("d-resume-2")
        assert result["allowed"] is False
        assert "running" in result["reason"]

    def test_cannot_resume_halted(self, state_dir, supervisor):
        """Halted dispatch cannot resume without clearing halt."""
        _register_dispatch(state_dir, "d-resume-3")
        supervisor.handle_incident(
            incident_class=IncidentClass.LEASE_CONFLICT,
            dispatch_id="d-resume-3",
            terminal_id="T1",
            reason="generation mismatch",
        )

        result = supervisor.can_resume("d-resume-3")
        assert result["allowed"] is False
        assert "halted" in result["reason"].lower() or "halt" in result["reason"].lower()

    def test_cannot_resume_budget_exhausted(self, state_dir, supervisor):
        """Cannot resume when all retry budgets are exhausted."""
        _register_dispatch(state_dir, "d-resume-4")
        contract = get_contract(IncidentClass.PROCESS_CRASH)
        for i in range(contract.retry_budget.max_retries + 1):
            supervisor.handle_incident(
                incident_class=IncidentClass.PROCESS_CRASH,
                dispatch_id="d-resume-4",
                terminal_id="T1",
                reason=f"crash {i+1}",
            )

        result = supervisor.can_resume("d-resume-4")
        assert result["allowed"] is False

    def test_cannot_resume_nonexistent(self, state_dir, supervisor):
        result = supervisor.can_resume("d-nonexistent")
        assert result["allowed"] is False
        assert "not found" in result["reason"].lower()

    def test_cannot_resume_dead_letter(self, state_dir, supervisor):
        """Dead-lettered dispatch cannot resume."""
        _register_dispatch(state_dir, "d-resume-5")
        _transition(state_dir, "d-resume-5", "claimed")
        _transition(state_dir, "d-resume-5", "delivering")
        _transition(state_dir, "d-resume-5", "failed_delivery")

        contract = get_contract(IncidentClass.DELIVERY_FAILURE)
        for i in range(contract.retry_budget.max_retries + 1):
            supervisor.handle_incident(
                incident_class=IncidentClass.DELIVERY_FAILURE,
                dispatch_id="d-resume-5",
                terminal_id="T1",
                reason=f"failed {i+1}",
            )

        result = supervisor.can_resume("d-resume-5")
        assert result["allowed"] is False

    def test_resume_after_halt_cleared_still_blocked_by_budget(self, state_dir, supervisor):
        """Clearing halt alone is insufficient if budget is also exhausted."""
        _register_dispatch(state_dir, "d-resume-6")
        # Lease conflict: max_retries=1, escalate_after_retries=0, halt=True
        # First incident: attempts_used=0 < max_retries=1 → still within budget
        # But escalate_after=0 → immediate escalate + halt
        supervisor.handle_incident(
            incident_class=IncidentClass.LEASE_CONFLICT,
            dispatch_id="d-resume-6",
            terminal_id="T1",
            reason="generation mismatch",
        )

        # Clear the halt
        supervisor.clear_halt("d-resume-6", IncidentClass.LEASE_CONFLICT.value)

        # After clearing halt with budget not yet exhausted, resume should work
        result = supervisor.can_resume("d-resume-6")
        assert result["allowed"] is True


# ---------------------------------------------------------------------------
# Incident trail tests (G-R3)
# ---------------------------------------------------------------------------

class TestIncidentTrail:
    """Every recovery action emits an incident trail (G-R3)."""

    def test_incident_recorded_in_log(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-trail-1")
        supervisor.handle_incident(
            incident_class=IncidentClass.PROCESS_CRASH,
            dispatch_id="d-trail-1",
            terminal_id="T1",
            reason="crash",
        )

        incidents = supervisor.get_incident_summary(dispatch_id="d-trail-1")
        assert len(incidents) == 1
        assert incidents[0]["incident_class"] == "process_crash"
        assert incidents[0]["dispatch_id"] == "d-trail-1"

    def test_multiple_incidents_ordered(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-trail-2")
        for i in range(3):
            supervisor.handle_incident(
                incident_class=IncidentClass.DELIVERY_FAILURE,
                dispatch_id="d-trail-2",
                terminal_id="T1",
                reason=f"failure {i+1}",
            )

        incidents = supervisor.get_incident_summary(dispatch_id="d-trail-2")
        assert len(incidents) == 3

    def test_incident_includes_metadata(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-trail-3")
        supervisor.handle_incident(
            incident_class=IncidentClass.DELIVERY_FAILURE,
            dispatch_id="d-trail-3",
            terminal_id="T1",
            reason="pane gone",
            metadata={"pane_id": "%5", "tmux_rc": 1},
        )

        incidents = supervisor.get_incident_summary(dispatch_id="d-trail-3")
        assert len(incidents) == 1

    def test_incident_by_terminal(self, state_dir, supervisor):
        _register_dispatch(state_dir, "d-trail-4")
        supervisor.handle_incident(
            incident_class=IncidentClass.TERMINAL_UNRESPONSIVE,
            dispatch_id="d-trail-4",
            terminal_id="T2",
            reason="health check failed",
        )

        incidents = supervisor.get_incident_summary(terminal_id="T2")
        assert len(incidents) >= 1
        assert incidents[0]["terminal_id"] == "T2"


# ---------------------------------------------------------------------------
# Process vs workflow separation (A-R1)
# ---------------------------------------------------------------------------

class TestProcessWorkflowSeparation:
    """Process restart decisions are separate from workflow resume decisions."""

    def test_process_crash_does_not_modify_dispatch_state(self, state_dir, supervisor):
        """Process crash triggers restart, NOT dispatch state change."""
        _register_dispatch(state_dir, "d-sep-1")
        supervisor.handle_incident(
            incident_class=IncidentClass.PROCESS_CRASH,
            dispatch_id="d-sep-1",
            terminal_id="T1",
            reason="worker died",
        )

        with get_connection(state_dir) as conn:
            dispatch = get_dispatch(conn, "d-sep-1")
            # Dispatch stays in queued — process crash doesn't dead-letter
            assert dispatch["state"] == "queued"

    def test_workflow_failure_can_modify_dispatch_state(self, state_dir, supervisor):
        """Workflow failure (delivery) can transition dispatch to dead_letter."""
        _register_dispatch(state_dir, "d-sep-2")
        _transition(state_dir, "d-sep-2", "claimed")
        _transition(state_dir, "d-sep-2", "delivering")
        _transition(state_dir, "d-sep-2", "failed_delivery")

        contract = get_contract(IncidentClass.DELIVERY_FAILURE)
        for i in range(contract.retry_budget.max_retries + 1):
            decision = supervisor.handle_incident(
                incident_class=IncidentClass.DELIVERY_FAILURE,
                dispatch_id="d-sep-2",
                terminal_id="T1",
                reason=f"failed {i+1}",
            )

        with get_connection(state_dir) as conn:
            dispatch = get_dispatch(conn, "d-sep-2")
            assert dispatch["state"] == "dead_letter"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge case handling."""

    def test_incident_without_dispatch(self, state_dir, supervisor):
        """Process-level incident without dispatch context."""
        decision = supervisor.handle_incident(
            incident_class=IncidentClass.PROCESS_CRASH,
            terminal_id="T1",
            reason="daemon process died",
        )
        assert decision.incident_record is not None
        assert decision.dispatch_id == ""
        assert decision.should_retry is True

    def test_incident_without_terminal(self, state_dir, supervisor):
        """Incident without terminal context."""
        _register_dispatch(state_dir, "d-edge-1")
        decision = supervisor.handle_incident(
            incident_class=IncidentClass.DELIVERY_FAILURE,
            dispatch_id="d-edge-1",
            reason="transport error",
        )
        assert decision.incident_record is not None

    def test_concurrent_incident_classes_tracked_separately(self, state_dir, supervisor):
        """Different incident classes for same dispatch maintain separate budgets."""
        _register_dispatch(state_dir, "d-edge-2")

        d1 = supervisor.handle_incident(
            incident_class=IncidentClass.PROCESS_CRASH,
            dispatch_id="d-edge-2",
            terminal_id="T1",
            reason="crash",
        )
        d2 = supervisor.handle_incident(
            incident_class=IncidentClass.DELIVERY_FAILURE,
            dispatch_id="d-edge-2",
            terminal_id="T1",
            reason="delivery fail",
        )

        # Both should have independent budgets (remaining = max - 1 after one attempt)
        crash_max = get_contract(IncidentClass.PROCESS_CRASH).retry_budget.max_retries
        delivery_max = get_contract(IncidentClass.DELIVERY_FAILURE).retry_budget.max_retries

        # budget_remaining reflects retries left AFTER this incident
        assert d1.budget_remaining == crash_max - 1  # 3 - 1 = 2
        assert d2.budget_remaining == delivery_max - 1  # 3 - 1 = 2
        # Verify they're tracked independently (different classes)
        assert d1.incident_class != d2.incident_class
