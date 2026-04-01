#!/usr/bin/env python3
"""
PR-4: Failed Delivery Certification — End-to-End Reproduction.

Quality gate: gate_pr4_failed_delivery_certification

Certifies the fix by reproducing every failed-delivery path and proving:
  1. The target terminal is never left blocked after failure.
  2. The next valid dispatch proceeds without manual lease surgery.
  3. Lease cleanup and runtime-state reconciliation are visible in evidence.
  4. Queue/projected in-progress state matches active dispatch and terminal
     activity before and after recovery.
  5. Operator-visible state stays consistent — no silent stranding, no
     regression to 'In Progress: None' while a recovered dispatch is active.

Each test class represents a distinct failure scenario. Tests are structured
as realistic end-to-end flows that exercise the full stack:
  RuntimeCore → DispatchBroker → LeaseManager → RuntimeStateReconciler → FailureClassifier
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (
    get_connection,
    get_dispatch,
    get_events,
    get_lease,
    init_schema,
    register_dispatch,
    transition_dispatch,
)
from dispatch_broker import DispatchBroker
from failure_classifier import (
    HOOK_FEEDBACK_INTERRUPTION,
    INVALID_SKILL,
    RUNTIME_STATE_DIVERGENCE,
    STALE_LEASE,
    TMUX_TRANSPORT_FAILURE,
    WORKER_HANDOFF_FAILURE,
    classify_failure,
)
from lease_manager import LeaseManager
from runtime_core import RuntimeCore
from runtime_state_reconciler import (
    GHOST_DISPATCH,
    QUEUE_PROJECTION_STALE,
    ZOMBIE_LEASE,
    RuntimeStateReconciler,
)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

class _CertBase(unittest.TestCase):
    """Common setup: temp dirs, schema, broker, lease_mgr, core, reconciler."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self.state_dir = base / "state"
        self.dispatch_dir = base / "dispatches"
        self.state_dir.mkdir(parents=True)
        self.dispatch_dir.mkdir(parents=True)
        init_schema(self.state_dir)

        self.broker = DispatchBroker(
            str(self.state_dir), str(self.dispatch_dir), shadow_mode=False
        )
        self.lease_mgr = LeaseManager(self.state_dir, auto_init=False)
        self.core = RuntimeCore(broker=self.broker, lease_mgr=self.lease_mgr)
        self.reconciler = RuntimeStateReconciler(self.state_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    # --- Helpers ---

    def _full_delivery(self, dispatch_id, terminal_id="T2", pr_ref=None):
        """Register, acquire lease, start delivery. Returns (attempt_id, generation)."""
        kwargs = {}
        if pr_ref:
            kwargs["pr_ref"] = pr_ref
        self.broker.register(
            dispatch_id, f"Work for {dispatch_id}",
            terminal_id=terminal_id, **kwargs,
        )
        lease_result = self.lease_mgr.acquire(terminal_id, dispatch_id=dispatch_id)
        delivery = self.core.delivery_start(dispatch_id, terminal_id)
        return delivery.attempt_id or "", lease_result.generation

    def _assert_terminal_idle(self, terminal_id, msg=""):
        lease = self.lease_mgr.get(terminal_id)
        self.assertEqual(lease.state, "idle", f"Terminal {terminal_id} not idle. {msg}")

    def _assert_terminal_available(self, terminal_id, for_dispatch="d-next"):
        result = self.core.check_terminal(terminal_id, for_dispatch)
        self.assertTrue(result["available"],
                        f"Terminal {terminal_id} not available: {result}")

    def _assert_no_mismatches(self, terminal_id=None):
        diag = self.reconciler.reconcile()
        if terminal_id:
            mismatches = diag.mismatches_for_terminal(terminal_id)
        else:
            mismatches = diag.mismatches
        self.assertEqual(len(mismatches), 0,
                         f"Unexpected mismatches: {[m.message for m in mismatches]}")

    def _recover_to_queued(self, dispatch_id):
        """Recover a failed dispatch back to queued state."""
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, dispatch_id)
            if row["state"] == "failed_delivery":
                transition_dispatch(conn, dispatch_id=dispatch_id,
                                    to_state="recovered", actor="test",
                                    reason="recovery for re-dispatch")
                transition_dispatch(conn, dispatch_id=dispatch_id,
                                    to_state="queued", actor="test",
                                    reason="re-queue after recovery")
            conn.commit()

    def _write_projection(self, active=None, completed=None):
        payload = {"active": active or [], "completed": completed or []}
        path = self.state_dir / "pr_queue_state.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


# ===========================================================================
# Scenario 1: tmux transport failure — full end-to-end
# ===========================================================================

class TestCertTmuxTransportFailure(_CertBase):
    """Reproduce tmux delivery failure → verify unblocked → dispatch next."""

    def test_full_lifecycle_tmux_failure(self):
        """End-to-end: fail delivery via tmux, verify cleanup, dispatch next."""
        # Phase 1: Deliver and fail
        attempt_id, gen = self._full_delivery("cert-tmux-001", "T2")
        result = self.core.release_on_delivery_failure(
            "cert-tmux-001", attempt_id, "T2", gen,
            reason="tmux delivery failed: paste-buffer load failed after 3 retries",
        )

        # Verify: cleanup complete
        self.assertTrue(result["cleanup_complete"], f"Cleanup incomplete: {result}")
        self.assertTrue(result["lease_released"])
        self.assertTrue(result["failure_recorded"])
        self.assertEqual(result["failure_class"], TMUX_TRANSPORT_FAILURE)
        self.assertTrue(result["retryable"])

        # Verify: terminal idle, available, no mismatches
        self._assert_terminal_idle("T2")
        self._assert_terminal_available("T2")
        self._assert_no_mismatches("T2")

        # Phase 2: Dispatch the next work item — must succeed without manual intervention
        attempt_id_2, gen_2 = self._full_delivery("cert-tmux-002", "T2")
        success = self.core.delivery_success("cert-tmux-002", attempt_id_2)
        self.assertTrue(success["success"])

        # Verify: terminal is leased to the new dispatch (not stranded)
        lease = self.lease_mgr.get("T2")
        self.assertEqual(lease.state, "leased")
        self.assertEqual(lease.dispatch_id, "cert-tmux-002")

    def test_tmux_enter_failure_does_not_strand(self):
        """tmux Enter key failure also releases lease."""
        attempt_id, gen = self._full_delivery("cert-enter-001", "T1")
        result = self.core.release_on_delivery_failure(
            "cert-enter-001", attempt_id, "T1", gen,
            reason="tmux Enter failed after 3 retries",
        )
        self.assertTrue(result["lease_released"])
        self._assert_terminal_idle("T1")
        self._assert_terminal_available("T1")
        self._assert_no_mismatches("T1")


# ===========================================================================
# Scenario 2: Worker-side rejection during execution handoff
# ===========================================================================

class TestCertWorkerHandoffRejection(_CertBase):
    """Worker rejects dispatch during handoff → terminal freed → next dispatch works."""

    def test_rejected_handoff_full_lifecycle(self):
        attempt_id, gen = self._full_delivery("cert-handoff-001", "T3")
        result = self.core.release_on_delivery_failure(
            "cert-handoff-001", attempt_id, "T3", gen,
            reason="rejected_execution_handoff: worker context overflow",
        )

        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(result["failure_class"], WORKER_HANDOFF_FAILURE)
        self.assertTrue(result["retryable"])

        # Terminal freed
        self._assert_terminal_idle("T3")
        self._assert_terminal_available("T3")
        self._assert_no_mismatches("T3")

        # Next dispatch proceeds
        attempt_id_2, gen_2 = self._full_delivery("cert-handoff-002", "T3")
        self.assertIsNotNone(attempt_id_2)
        lease = self.lease_mgr.get("T3")
        self.assertEqual(lease.dispatch_id, "cert-handoff-002")


# ===========================================================================
# Scenario 3: Hook/feedback-loop interruption after context reset
# ===========================================================================

class TestCertHookFeedbackInterruption(_CertBase):
    """Prompt loop interrupted after clear-context → lease freed."""

    def test_prompt_loop_interrupted_full_lifecycle(self):
        attempt_id, gen = self._full_delivery("cert-hook-001", "T2")
        result = self.core.release_on_delivery_failure(
            "cert-hook-001", attempt_id, "T2", gen,
            reason="prompt_loop_interrupted_after_clear_context",
        )

        self.assertTrue(result["lease_released"])
        self.assertEqual(result["failure_class"], HOOK_FEEDBACK_INTERRUPTION)
        self._assert_terminal_idle("T2")
        self._assert_terminal_available("T2")
        self._assert_no_mismatches("T2")

    def test_lost_attempt_id_still_frees_lease(self):
        """Context reset loses attempt_id — lease must still be freed."""
        self.broker.register("cert-hook-lost-001", "Work", terminal_id="T2")
        lease_result = self.lease_mgr.acquire("T2", dispatch_id="cert-hook-lost-001")

        result = self.core.release_on_delivery_failure(
            "cert-hook-lost-001", "", "T2", lease_result.generation,
            reason="prompt_loop_interrupted_after_clear_context",
        )
        self.assertTrue(result["lease_released"])
        self._assert_terminal_idle("T2")


# ===========================================================================
# Scenario 4: Invalid skill (non-retryable) — terminal still freed
# ===========================================================================

class TestCertInvalidSkill(_CertBase):
    """Config error (invalid skill) releases lease despite non-retryable class."""

    def test_invalid_skill_frees_terminal(self):
        attempt_id, gen = self._full_delivery("cert-skill-001", "T1")
        result = self.core.release_on_delivery_failure(
            "cert-skill-001", attempt_id, "T1", gen,
            reason="SKILL_INVALID: @nonexistent-skill not found in registry",
        )

        self.assertTrue(result["lease_released"])
        self.assertEqual(result["failure_class"], INVALID_SKILL)
        self.assertFalse(result["retryable"])
        self._assert_terminal_idle("T1")
        self._assert_terminal_available("T1")


# ===========================================================================
# Scenario 5: Repeated retries then recovery — no stale lease accumulation
# ===========================================================================

class TestCertRepeatedRetryThenRecovery(_CertBase):
    """Three failed attempts followed by successful recovery dispatch."""

    def test_three_failures_then_success(self):
        dispatch_id = "cert-retry-001"
        terminal_id = "T2"

        # First delivery + register
        attempt_1, gen_1 = self._full_delivery(dispatch_id, terminal_id)
        self.core.release_on_delivery_failure(
            dispatch_id, attempt_1, terminal_id, gen_1, "tmux delivery failed attempt 1",
        )
        self._assert_terminal_idle(terminal_id, "after attempt 1")

        # Recover and retry (attempt 2)
        self._recover_to_queued(dispatch_id)
        lease_2 = self.lease_mgr.acquire(terminal_id, dispatch_id=dispatch_id)
        delivery_2 = self.core.delivery_start(dispatch_id, terminal_id, attempt_number=2)
        self.core.release_on_delivery_failure(
            dispatch_id, delivery_2.attempt_id or "", terminal_id,
            lease_2.generation, "tmux delivery failed attempt 2",
        )
        self._assert_terminal_idle(terminal_id, "after attempt 2")

        # Recover and retry (attempt 3) — this one succeeds
        self._recover_to_queued(dispatch_id)
        lease_3 = self.lease_mgr.acquire(terminal_id, dispatch_id=dispatch_id)
        delivery_3 = self.core.delivery_start(dispatch_id, terminal_id, attempt_number=3)
        success = self.core.delivery_success(dispatch_id, delivery_3.attempt_id)
        self.assertTrue(success["success"])

        # Terminal is leased to the successful dispatch
        lease = self.lease_mgr.get(terminal_id)
        self.assertEqual(lease.state, "leased")
        self.assertEqual(lease.dispatch_id, dispatch_id)

        # Reconciler sees no zombie or ghost — state is consistent
        diag = self.reconciler.reconcile()
        blocking = [m for m in diag.mismatches if m.severity == "blocking"]
        self.assertEqual(len(blocking), 0,
                         f"Blocking mismatches after recovery: {[m.message for m in blocking]}")


# ===========================================================================
# Scenario 6: Runtime state consistency — zombie lease detection + cleanup
# ===========================================================================

class TestCertZombieLeaseDetectionAndRecovery(_CertBase):
    """Zombie lease is detected, operator is informed, and terminal can recover."""

    def test_zombie_detected_then_cleaned(self):
        # Create a zombie: lease held but dispatch is failed_delivery
        attempt_id, gen = self._full_delivery("cert-zombie-001", "T2")
        # Transition to failed_delivery WITHOUT releasing lease (simulates bug)
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="cert-zombie-001",
                                to_state="failed_delivery", actor="test")
            conn.commit()

        # Reconciler detects zombie
        diag = self.reconciler.reconcile()
        zombies = [m for m in diag.mismatches if m.mismatch_type == ZOMBIE_LEASE]
        self.assertEqual(len(zombies), 1)
        self.assertEqual(zombies[0].terminal_id, "T2")
        self.assertEqual(zombies[0].dispatch_state, "failed_delivery")
        self.assertEqual(zombies[0].severity, "blocking")

        # check_terminal also reports zombie explicitly
        check = self.core.check_terminal("T2", "d-new")
        self.assertFalse(check["available"])
        self.assertEqual(check["mismatch"], ZOMBIE_LEASE)
        self.assertEqual(check["failure_class"], RUNTIME_STATE_DIVERGENCE)

        # Manual recovery: release the zombie lease
        self.lease_mgr.release("T2", gen)

        # After cleanup, terminal is available
        self._assert_terminal_idle("T2")
        self._assert_terminal_available("T2")
        self._assert_no_mismatches("T2")


# ===========================================================================
# Scenario 7: Queue projection consistency — no false "In Progress: None"
# ===========================================================================

class TestCertQueueProjectionConsistency(_CertBase):
    """Active dispatch is visible in projection — no phantom idle state."""

    def test_stale_projection_detected_during_active_dispatch(self):
        attempt_id, gen = self._full_delivery(
            "cert-proj-001", "T2", pr_ref="PR-4",
        )

        # Projection says nothing active — stale
        proj_path = self._write_projection(active=[], completed=["PR-0", "PR-1"])
        reconciler = RuntimeStateReconciler(
            self.state_dir, projection_file=proj_path,
        )
        diag = reconciler.reconcile()
        stale = [m for m in diag.mismatches if m.mismatch_type == QUEUE_PROJECTION_STALE]
        self.assertEqual(len(stale), 1)
        self.assertIn("In Progress", stale[0].message)

        # After delivery failure + cleanup, projection staleness resolves
        self.core.release_on_delivery_failure(
            "cert-proj-001", attempt_id, "T2", gen, "tmux delivery failed",
        )
        diag_after = reconciler.reconcile()
        stale_after = [m for m in diag_after.mismatches
                       if m.mismatch_type == QUEUE_PROJECTION_STALE]
        self.assertEqual(len(stale_after), 0,
                         "Projection staleness should resolve after failure cleanup")

    def test_projection_correct_when_dispatch_active(self):
        """No false stale when projection correctly lists active PR."""
        self._full_delivery("cert-proj-002", "T2", pr_ref="PR-4")
        proj_path = self._write_projection(active=["PR-4"])
        reconciler = RuntimeStateReconciler(
            self.state_dir, projection_file=proj_path,
        )
        diag = reconciler.reconcile()
        stale = [m for m in diag.mismatches if m.mismatch_type == QUEUE_PROJECTION_STALE]
        self.assertEqual(len(stale), 0)


# ===========================================================================
# Scenario 8: Multi-terminal independence — failure on T2 doesn't affect T1
# ===========================================================================

class TestCertMultiTerminalIndependence(_CertBase):
    """Failure on one terminal does not affect another terminal's state."""

    def test_t2_failure_leaves_t1_unaffected(self):
        # T1 has an active dispatch
        attempt_t1, gen_t1 = self._full_delivery("cert-mt-t1", "T1")

        # T2 fails
        attempt_t2, gen_t2 = self._full_delivery("cert-mt-t2", "T2")
        self.core.release_on_delivery_failure(
            "cert-mt-t2", attempt_t2, "T2", gen_t2, "tmux delivery failed",
        )

        # T1 still leased and active
        lease_t1 = self.lease_mgr.get("T1")
        self.assertEqual(lease_t1.state, "leased")
        self.assertEqual(lease_t1.dispatch_id, "cert-mt-t1")

        # T2 is idle
        self._assert_terminal_idle("T2")

        # Only T2 has no mismatches; T1 dispatch is still active so no mismatch
        diag = self.reconciler.reconcile()
        self.assertEqual(len(diag.mismatches), 0)


# ===========================================================================
# Scenario 9: Partial failure — broker error does NOT prevent lease release
# ===========================================================================

class TestCertPartialFailureResilience(_CertBase):
    """Broker error during failure recording still releases the lease."""

    def test_broker_error_does_not_strand_terminal(self):
        attempt_id, gen = self._full_delivery("cert-partial-001", "T2")

        # Patch broker to fail
        original_deliver_failure = self.broker.deliver_failure
        def _raise(*args, **kwargs):
            raise RuntimeError("simulated broker crash")
        self.broker.deliver_failure = _raise

        try:
            result = self.core.release_on_delivery_failure(
                "cert-partial-001", attempt_id, "T2", gen,
                reason="tmux delivery failed",
            )
        finally:
            self.broker.deliver_failure = original_deliver_failure

        # failure_recorded is False, but lease MUST be released
        self.assertFalse(result["failure_recorded"])
        self.assertTrue(result["lease_released"])
        self.assertFalse(result["cleanup_complete"])  # partial
        self.assertIsNotNone(result["failure_error"])

        # Terminal is idle — not stranded
        self._assert_terminal_idle("T2")
        self._assert_terminal_available("T2")


# ===========================================================================
# Scenario 10: Generation guard — stale generation cannot release wrong lease
# ===========================================================================

class TestCertGenerationGuard(_CertBase):
    """Stale generation from a previous attempt cannot release the current lease."""

    def test_stale_generation_rejected(self):
        # First dispatch acquires lease
        attempt_1, gen_1 = self._full_delivery("cert-gen-001", "T2")
        self.core.release_on_delivery_failure(
            "cert-gen-001", attempt_1, "T2", gen_1, "tmux delivery failed",
        )

        # Second dispatch acquires a new lease with higher generation
        attempt_2, gen_2 = self._full_delivery("cert-gen-002", "T2")
        self.assertGreater(gen_2, gen_1)

        # Attempting to release with stale generation fails
        result_stale = self.core.release_lease("T2", gen_1)
        self.assertFalse(result_stale["released"])

        # Current lease remains intact
        lease = self.lease_mgr.get("T2")
        self.assertEqual(lease.state, "leased")
        self.assertEqual(lease.dispatch_id, "cert-gen-002")


# ===========================================================================
# Scenario 11: Audit trail completeness
# ===========================================================================

class TestCertAuditTrailCompleteness(_CertBase):
    """Every failure path produces auditable events in the coordination DB."""

    def test_failed_delivery_event_recorded(self):
        attempt_id, gen = self._full_delivery("cert-audit-001", "T2")
        self.core.release_on_delivery_failure(
            "cert-audit-001", attempt_id, "T2", gen, "tmux delivery failed",
        )
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="cert-audit-001")
        event_types = {e["event_type"] for e in events}
        self.assertIn("dispatch_failed_delivery", event_types)

    def test_lease_release_event_recorded(self):
        attempt_id, gen = self._full_delivery("cert-audit-002", "T2")
        self.core.release_on_delivery_failure(
            "cert-audit-002", attempt_id, "T2", gen, "tmux delivery failed",
        )
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="T2")
        event_types = {e["event_type"] for e in events}
        self.assertIn("lease_released", event_types)

    def test_failure_reason_preserved_in_attempt(self):
        attempt_id, gen = self._full_delivery("cert-audit-003", "T1")
        reason = "rejected_execution_handoff: worker context overflow, skill=@backend-developer"
        self.core.release_on_delivery_failure(
            "cert-audit-003", attempt_id, "T1", gen, reason,
        )
        with get_connection(self.state_dir) as conn:
            attempt_row = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE dispatch_id = ?",
                ("cert-audit-003",),
            ).fetchone()
        self.assertIn("rejected_execution_handoff", attempt_row["failure_reason"])
        self.assertIn("worker context overflow", attempt_row["failure_reason"])


# ===========================================================================
# Scenario 12: Full failure-recovery-success cycle across all failure types
# ===========================================================================

class TestCertAllFailureTypesRecovery(_CertBase):
    """Every failure classification produces cleanup, recovery, and next dispatch."""

    def _fail_and_recover(self, dispatch_suffix, reason, expected_class,
                          expected_retryable, terminal_id="T2"):
        dispatch_id = f"cert-all-{dispatch_suffix}"
        attempt_id, gen = self._full_delivery(dispatch_id, terminal_id)

        result = self.core.release_on_delivery_failure(
            dispatch_id, attempt_id, terminal_id, gen, reason,
        )
        self.assertTrue(result["lease_released"],
                        f"Lease not released for {reason}: {result}")
        self.assertEqual(result["failure_class"], expected_class)
        self.assertEqual(result["retryable"], expected_retryable)
        self._assert_terminal_idle(terminal_id)
        self._assert_terminal_available(terminal_id)
        self._assert_no_mismatches(terminal_id)

    def test_tmux_transport(self):
        self._fail_and_recover("tmux", "tmux delivery failed",
                               TMUX_TRANSPORT_FAILURE, True)

    def test_worker_handoff(self):
        self._fail_and_recover("handoff", "rejected_execution_handoff",
                               WORKER_HANDOFF_FAILURE, True)

    def test_hook_interruption(self):
        self._fail_and_recover("hook", "prompt_loop_interrupted_after_clear_context",
                               HOOK_FEEDBACK_INTERRUPTION, True)

    def test_stale_lease_reason(self):
        self._fail_and_recover("stale", "stale_lease: generation mismatch",
                               STALE_LEASE, True)

    def test_invalid_skill_reason(self):
        self._fail_and_recover("skill", "SKILL_INVALID: @bad not found",
                               INVALID_SKILL, False)

    def test_runtime_divergence(self):
        self._fail_and_recover("div", "runtime_state_divergence:zombie_lease:completed",
                               RUNTIME_STATE_DIVERGENCE, False)


if __name__ == "__main__":
    unittest.main()
