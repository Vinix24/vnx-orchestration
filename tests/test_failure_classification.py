#!/usr/bin/env python3
"""
Tests for PR-3: Dispatch Failure Classification And Operator Visibility.

Quality gate: gate_pr3_failure_classification_visibility

Coverage:
  - Failure classification for all 6 failure classes:
    invalid_skill, stale_lease, runtime_state_divergence,
    worker_handoff_failure, hook_feedback_interruption, tmux_transport_failure
  - Retryable vs non-retryable distinction is deterministic
  - Operator summary is present and meaningful for every class
  - Rejected dispatches preserve actionable root-cause markers
  - Cleanup outcome is visible in release_on_delivery_failure result
  - check_terminal surfaces classification for zombie lease
  - T0 can distinguish retryable from non-retryable deterministically
  - Unknown reasons default to retryable (safe default)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from failure_classifier import (
    HOOK_FEEDBACK_INTERRUPTION,
    INVALID_SKILL,
    RUNTIME_STATE_DIVERGENCE,
    STALE_LEASE,
    TMUX_TRANSPORT_FAILURE,
    WORKER_HANDOFF_FAILURE,
    FailureClassification,
    classify_failure,
    is_retryable,
)
from runtime_coordination import (
    get_connection,
    get_dispatch,
    init_schema,
    transition_dispatch,
)
from dispatch_broker import DispatchBroker
from lease_manager import LeaseManager
from runtime_core import RuntimeCore


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

def _setup(tmp: tempfile.TemporaryDirectory):
    base = Path(tmp.name)
    state_dir = base / "state"
    dispatch_dir = base / "dispatches"
    state_dir.mkdir(parents=True)
    dispatch_dir.mkdir(parents=True)
    init_schema(state_dir)
    broker = DispatchBroker(str(state_dir), str(dispatch_dir), shadow_mode=False)
    lease_mgr = LeaseManager(state_dir, auto_init=False)
    core = RuntimeCore(broker=broker, lease_mgr=lease_mgr)
    return str(state_dir), str(dispatch_dir), broker, lease_mgr, core


def _full_delivery_setup(core, broker, lease_mgr, state_dir,
                         dispatch_id, terminal_id="T2"):
    broker.register(dispatch_id, f"Work for {dispatch_id}", terminal_id=terminal_id)
    lease_result = lease_mgr.acquire(terminal_id, dispatch_id=dispatch_id)
    generation = lease_result.generation
    delivery = core.delivery_start(dispatch_id, terminal_id)
    attempt_id = delivery.attempt_id or ""
    return attempt_id, generation


# ---------------------------------------------------------------------------
# TestClassifyFailure — unit tests for the classifier
# ---------------------------------------------------------------------------

class TestClassifyFailure(unittest.TestCase):
    """Pure classifier tests — no DB or runtime dependency."""

    def test_invalid_skill_from_skill_invalid_marker(self):
        c = classify_failure("SKILL_INVALID: skill '@backend-developer' not found")
        self.assertEqual(c.failure_class, INVALID_SKILL)
        self.assertFalse(c.retryable)

    def test_invalid_skill_from_not_found_in_registry(self):
        c = classify_failure("Skill '@reviewer' not found in registry")
        self.assertEqual(c.failure_class, INVALID_SKILL)
        self.assertFalse(c.retryable)

    def test_invalid_skill_from_skill_not_found(self):
        c = classify_failure("skill_not_found for role backend-developer")
        self.assertEqual(c.failure_class, INVALID_SKILL)
        self.assertFalse(c.retryable)

    def test_stale_lease_from_generation_mismatch(self):
        c = classify_failure("generation mismatch: expected 5, got 3")
        self.assertEqual(c.failure_class, STALE_LEASE)
        self.assertTrue(c.retryable)

    def test_stale_lease_from_lease_expired(self):
        c = classify_failure("lease_expired for T2")
        self.assertEqual(c.failure_class, STALE_LEASE)
        self.assertTrue(c.retryable)

    def test_stale_lease_from_stale_lease_keyword(self):
        c = classify_failure("stale_lease: generation guard rejected")
        self.assertEqual(c.failure_class, STALE_LEASE)
        self.assertTrue(c.retryable)

    def test_runtime_state_divergence(self):
        c = classify_failure("runtime_state_divergence:zombie_lease:completed")
        self.assertEqual(c.failure_class, RUNTIME_STATE_DIVERGENCE)
        self.assertFalse(c.retryable)

    def test_runtime_state_divergence_from_zombie(self):
        c = classify_failure("zombie_lease detected for T2")
        self.assertEqual(c.failure_class, RUNTIME_STATE_DIVERGENCE)
        self.assertFalse(c.retryable)

    def test_runtime_state_divergence_from_ghost(self):
        c = classify_failure("ghost_dispatch: dispatch active but no lease")
        self.assertEqual(c.failure_class, RUNTIME_STATE_DIVERGENCE)
        self.assertFalse(c.retryable)

    def test_worker_handoff_failure(self):
        c = classify_failure("rejected_execution_handoff")
        self.assertEqual(c.failure_class, WORKER_HANDOFF_FAILURE)
        self.assertTrue(c.retryable)

    def test_worker_handoff_from_worker_rejected(self):
        c = classify_failure("worker_rejected during handoff")
        self.assertEqual(c.failure_class, WORKER_HANDOFF_FAILURE)
        self.assertTrue(c.retryable)

    def test_hook_feedback_interruption(self):
        c = classify_failure("prompt_loop_interrupted_after_clear_context")
        self.assertEqual(c.failure_class, HOOK_FEEDBACK_INTERRUPTION)
        self.assertTrue(c.retryable)

    def test_hook_interruption_from_hook_failure(self):
        c = classify_failure("hook failure during feedback loop")
        self.assertEqual(c.failure_class, HOOK_FEEDBACK_INTERRUPTION)
        self.assertTrue(c.retryable)

    def test_hook_interruption_from_context_reset(self):
        c = classify_failure("context reset interrupted delivery")
        self.assertEqual(c.failure_class, HOOK_FEEDBACK_INTERRUPTION)
        self.assertTrue(c.retryable)

    def test_tmux_transport_failure(self):
        c = classify_failure("tmux delivery failed")
        self.assertEqual(c.failure_class, TMUX_TRANSPORT_FAILURE)
        self.assertTrue(c.retryable)

    def test_tmux_enter_failed(self):
        c = classify_failure("tmux Enter failed")
        self.assertEqual(c.failure_class, TMUX_TRANSPORT_FAILURE)
        self.assertTrue(c.retryable)

    def test_tmux_paste_buffer(self):
        c = classify_failure("paste-buffer failed for T2")
        self.assertEqual(c.failure_class, TMUX_TRANSPORT_FAILURE)
        self.assertTrue(c.retryable)

    def test_unknown_defaults_to_tmux_transport(self):
        c = classify_failure("some completely unknown failure reason")
        self.assertEqual(c.failure_class, TMUX_TRANSPORT_FAILURE)
        self.assertTrue(c.retryable)

    def test_classification_preserves_original_reason(self):
        reason = "rejected_execution_handoff: worker busy"
        c = classify_failure(reason)
        self.assertEqual(c.reason, reason)

    def test_operator_summary_always_present(self):
        reasons = [
            "skill_invalid", "stale_lease", "runtime_state_divergence",
            "rejected_execution_handoff", "prompt_loop_interrupted",
            "tmux delivery failed", "unknown reason",
        ]
        for reason in reasons:
            c = classify_failure(reason)
            self.assertIsInstance(c.operator_summary, str, f"Missing summary for {reason}")
            self.assertGreater(len(c.operator_summary), 10, f"Summary too short for {reason}")

    def test_to_dict_contains_all_fields(self):
        c = classify_failure("tmux delivery failed")
        d = c.to_dict()
        self.assertIn("failure_class", d)
        self.assertIn("retryable", d)
        self.assertIn("operator_summary", d)
        self.assertIn("reason", d)

    def test_is_retryable_helper(self):
        self.assertTrue(is_retryable(TMUX_TRANSPORT_FAILURE))
        self.assertTrue(is_retryable(STALE_LEASE))
        self.assertTrue(is_retryable(WORKER_HANDOFF_FAILURE))
        self.assertTrue(is_retryable(HOOK_FEEDBACK_INTERRUPTION))
        self.assertFalse(is_retryable(INVALID_SKILL))
        self.assertFalse(is_retryable(RUNTIME_STATE_DIVERGENCE))


# ---------------------------------------------------------------------------
# TestRetryableVsNonRetryable — deterministic distinction
# ---------------------------------------------------------------------------

class TestRetryableVsNonRetryable(unittest.TestCase):
    """T0 can distinguish retryable from non-retryable deterministically."""

    def test_non_retryable_classes(self):
        non_retryable_reasons = [
            ("SKILL_INVALID: not found", INVALID_SKILL),
            ("runtime_state_divergence:zombie", RUNTIME_STATE_DIVERGENCE),
        ]
        for reason, expected_class in non_retryable_reasons:
            c = classify_failure(reason)
            self.assertEqual(c.failure_class, expected_class)
            self.assertFalse(c.retryable, f"{reason} should be non-retryable")

    def test_retryable_classes(self):
        retryable_reasons = [
            ("stale_lease: gen mismatch", STALE_LEASE),
            ("rejected_execution_handoff", WORKER_HANDOFF_FAILURE),
            ("prompt_loop_interrupted_after_clear_context", HOOK_FEEDBACK_INTERRUPTION),
            ("tmux delivery failed", TMUX_TRANSPORT_FAILURE),
        ]
        for reason, expected_class in retryable_reasons:
            c = classify_failure(reason)
            self.assertEqual(c.failure_class, expected_class)
            self.assertTrue(c.retryable, f"{reason} should be retryable")


# ---------------------------------------------------------------------------
# TestReleaseOnFailureClassification — integration with RuntimeCore
# ---------------------------------------------------------------------------

class TestReleaseOnFailureClassification(unittest.TestCase):
    """release_on_delivery_failure result includes classification fields."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def _release_with_reason(self, dispatch_id, reason, terminal_id="T2"):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            dispatch_id, terminal_id,
        )
        return self.core.release_on_delivery_failure(
            dispatch_id=dispatch_id,
            attempt_id=attempt_id,
            terminal_id=terminal_id,
            generation=generation,
            reason=reason,
        )

    def test_tmux_failure_classified(self):
        result = self._release_with_reason("fc-tmux-001", "tmux delivery failed")
        self.assertEqual(result["failure_class"], TMUX_TRANSPORT_FAILURE)
        self.assertTrue(result["retryable"])
        self.assertIn("operator_summary", result)

    def test_invalid_skill_classified(self):
        result = self._release_with_reason("fc-skill-001", "SKILL_INVALID: not found")
        self.assertEqual(result["failure_class"], INVALID_SKILL)
        self.assertFalse(result["retryable"])

    def test_stale_lease_classified(self):
        result = self._release_with_reason("fc-stale-001", "stale_lease: generation mismatch")
        self.assertEqual(result["failure_class"], STALE_LEASE)
        self.assertTrue(result["retryable"])

    def test_worker_handoff_classified(self):
        result = self._release_with_reason("fc-handoff-001", "rejected_execution_handoff")
        self.assertEqual(result["failure_class"], WORKER_HANDOFF_FAILURE)
        self.assertTrue(result["retryable"])

    def test_hook_interruption_classified(self):
        result = self._release_with_reason(
            "fc-hook-001", "prompt_loop_interrupted_after_clear_context"
        )
        self.assertEqual(result["failure_class"], HOOK_FEEDBACK_INTERRUPTION)
        self.assertTrue(result["retryable"])

    def test_runtime_divergence_classified(self):
        result = self._release_with_reason(
            "fc-div-001", "runtime_state_divergence:zombie_lease:completed"
        )
        self.assertEqual(result["failure_class"], RUNTIME_STATE_DIVERGENCE)
        self.assertFalse(result["retryable"])

    def test_cleanup_outcome_visible_alongside_classification(self):
        result = self._release_with_reason("fc-vis-001", "tmux delivery failed")
        self.assertIn("lease_released", result)
        self.assertIn("failure_recorded", result)
        self.assertIn("cleanup_complete", result)
        self.assertIn("failure_class", result)
        self.assertIn("retryable", result)
        self.assertIn("operator_summary", result)
        self.assertTrue(result["cleanup_complete"])

    def test_classification_present_even_when_cleanup_fails(self):
        """Classification is always present regardless of cleanup outcome."""
        self.broker.register("fc-partial-001", "Work", terminal_id="T2")
        lease_result = self.lease_mgr.acquire("T2", dispatch_id="fc-partial-001")
        generation = lease_result.generation
        delivery = self.core.delivery_start("fc-partial-001", "T2")
        attempt_id = delivery.attempt_id or ""

        # Use stale generation so lease release fails
        result = self.core.release_on_delivery_failure(
            "fc-partial-001", attempt_id, "T2",
            generation - 1, "stale_lease: generation mismatch",
        )
        self.assertFalse(result["lease_released"])
        self.assertEqual(result["failure_class"], STALE_LEASE)
        self.assertTrue(result["retryable"])
        self.assertIsNotNone(result["operator_summary"])


# ---------------------------------------------------------------------------
# TestRejectedDispatchReasonPreservation
# ---------------------------------------------------------------------------

class TestRejectedDispatchReasonPreservation(unittest.TestCase):
    """Rejected dispatches preserve actionable root-cause markers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_failure_reason_preserved_in_attempt_row(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "reject-reason-001", "T2",
        )
        self.core.release_on_delivery_failure(
            "reject-reason-001", attempt_id, "T2", generation,
            reason="SKILL_INVALID: @reviewer not found in registry",
        )
        with get_connection(self.state_dir) as conn:
            attempt_row = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE dispatch_id = ?",
                ("reject-reason-001",),
            ).fetchone()
        self.assertIsNotNone(attempt_row)
        self.assertIn("SKILL_INVALID", attempt_row["failure_reason"])
        self.assertIn("@reviewer", attempt_row["failure_reason"])

    def test_failure_reason_preserved_for_handoff_rejection(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "reject-reason-002", "T1",
        )
        self.core.release_on_delivery_failure(
            "reject-reason-002", attempt_id, "T1", generation,
            reason="rejected_execution_handoff: worker context overflow",
        )
        with get_connection(self.state_dir) as conn:
            attempt_row = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE dispatch_id = ?",
                ("reject-reason-002",),
            ).fetchone()
        self.assertIn("rejected_execution_handoff", attempt_row["failure_reason"])
        self.assertIn("worker context overflow", attempt_row["failure_reason"])

    def test_dispatch_state_is_failed_delivery_with_reason(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "reject-reason-003", "T2",
        )
        self.core.release_on_delivery_failure(
            "reject-reason-003", attempt_id, "T2", generation,
            reason="hook_feedback_interruption after terminal reset",
        )
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "reject-reason-003")
        self.assertEqual(row["state"], "failed_delivery")


# ---------------------------------------------------------------------------
# TestCheckTerminalClassification
# ---------------------------------------------------------------------------

class TestCheckTerminalClassification(unittest.TestCase):
    """check_terminal surfaces failure classification for zombie lease."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
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

    def tearDown(self):
        self._tmp.cleanup()

    def _create_zombie(self, dispatch_id, terminal_id, end_state):
        from runtime_coordination import register_dispatch
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal_id)
            conn.commit()
        self.lease_mgr.acquire(terminal_id, dispatch_id)
        # Transition to end_state to create zombie (lease not released)
        with get_connection(self.state_dir) as conn:
            states = {
                "completed": ["claimed", "delivering", "accepted", "running", "completed"],
                "failed_delivery": ["claimed", "delivering", "failed_delivery"],
                "expired": ["claimed", "expired"],
            }
            for state in states.get(end_state, [end_state]):
                try:
                    transition_dispatch(conn, dispatch_id=dispatch_id,
                                        to_state=state, actor="test")
                except Exception:
                    pass
            conn.commit()

    def test_zombie_lease_includes_classification(self):
        self._create_zombie("zombie-class-001", "T2", "failed_delivery")
        result = self.core.check_terminal("T2", "d-new")
        self.assertFalse(result["available"])
        self.assertEqual(result["failure_class"], RUNTIME_STATE_DIVERGENCE)
        self.assertFalse(result["retryable"])
        self.assertIn("operator_summary", result)

    def test_zombie_lease_operator_summary_is_readable(self):
        self._create_zombie("zombie-class-002", "T2", "completed")
        result = self.core.check_terminal("T2", "d-new")
        self.assertIsInstance(result["operator_summary"], str)
        self.assertGreater(len(result["operator_summary"]), 20)

    def test_normal_block_has_no_classification(self):
        """Active lease with live dispatch does not include failure classification."""
        from runtime_coordination import register_dispatch
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id="active-001", terminal_id="T2")
            conn.commit()
        self.lease_mgr.acquire("T2", "active-001")
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="active-001",
                                to_state="claimed", actor="test")
            transition_dispatch(conn, dispatch_id="active-001",
                                to_state="delivering", actor="test")
            conn.commit()

        result = self.core.check_terminal("T2", "d-other")
        self.assertFalse(result["available"])
        self.assertNotIn("failure_class", result)


# ---------------------------------------------------------------------------
# TestDeliveryFailureClassification — delivery_failure method
# ---------------------------------------------------------------------------

class TestDeliveryFailureClassification(unittest.TestCase):
    """delivery_failure method includes classification in result."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_delivery_failure_includes_classification(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "df-class-001", "T2",
        )
        result = self.core.delivery_failure("df-class-001", attempt_id,
                                             reason="tmux delivery failed")
        self.assertEqual(result["failure_class"], TMUX_TRANSPORT_FAILURE)
        self.assertTrue(result["retryable"])
        self.assertIn("operator_summary", result)

    def test_delivery_failure_non_retryable(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "df-class-002", "T2",
        )
        result = self.core.delivery_failure(
            "df-class-002", attempt_id,
            reason="SKILL_INVALID: @missing-skill not found",
        )
        self.assertEqual(result["failure_class"], INVALID_SKILL)
        self.assertFalse(result["retryable"])


if __name__ == "__main__":
    unittest.main()
