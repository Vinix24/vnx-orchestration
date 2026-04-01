#!/usr/bin/env python3
"""
Tests for PR-1: Release Canonical Lease On Delivery Failure.

Quality gate: gate_pr1_release_lease_on_failure

Coverage:
  - tmux transport failure       → lease always released before dispatch exits
  - rejected execution handoff   → lease always released before dispatch exits
  - prompt-loop interruption      → lease always released after explicit clear-context
  - repeated retry + failure      → no stale lease accumulation across attempts
  - cleanup failure is explicit   → lease_error visible in result, not silently dropped
  - cleanup works when bookkeeping partially fails
    (delivery_failure recording error does NOT prevent lease release)
  - terminal and canonical lease cleanup remain paired under test
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (
    InvalidTransitionError,
    get_connection,
    get_dispatch,
    get_events,
    get_lease,
    init_schema,
    register_dispatch,
    transition_dispatch,
)
from dispatch_broker import DispatchBroker
from lease_manager import LeaseManager
from runtime_core import RuntimeCore


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _setup(tmp: tempfile.TemporaryDirectory):
    """Return (state_dir, dispatch_dir, broker, lease_mgr, core)."""
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


def _full_delivery_setup(core: RuntimeCore, broker: DispatchBroker,
                          lease_mgr: LeaseManager, state_dir: str,
                          dispatch_id: str, terminal_id: str = "T2"):
    """Register dispatch, acquire lease, start delivery. Returns attempt_id + generation."""
    broker.register(dispatch_id, f"Work for {dispatch_id}", terminal_id=terminal_id)
    lease_result = lease_mgr.acquire(terminal_id, dispatch_id=dispatch_id)
    generation = lease_result.generation

    delivery = core.delivery_start(dispatch_id, terminal_id)
    attempt_id = delivery.attempt_id or ""
    return attempt_id, generation


# ---------------------------------------------------------------------------
# TestTmuxTransportFailure
# ---------------------------------------------------------------------------

class TestTmuxTransportFailure(unittest.TestCase):
    """Simulate the path where tmux send-keys / paste-buffer fails."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lease_released_after_transport_failure(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "tmux-fail-001", "T2",
        )
        result = self.core.release_on_delivery_failure(
            dispatch_id="tmux-fail-001",
            attempt_id=attempt_id,
            terminal_id="T2",
            generation=generation,
            reason="tmux delivery failed",
        )
        self.assertTrue(result["lease_released"], f"lease not released: {result}")
        lease = self.lease_mgr.get("T2")
        self.assertEqual(lease.state, "idle")

    def test_dispatch_transitions_to_failed_delivery(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "tmux-fail-002", "T2",
        )
        self.core.release_on_delivery_failure(
            dispatch_id="tmux-fail-002",
            attempt_id=attempt_id,
            terminal_id="T2",
            generation=generation,
            reason="tmux delivery failed",
        )
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "tmux-fail-002")
        self.assertEqual(row["state"], "failed_delivery")

    def test_claim_and_lease_released_together(self):
        """Lease is idle after release_on_delivery_failure (pairing invariant)."""
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "tmux-fail-003", "T1",
        )
        result = self.core.release_on_delivery_failure(
            "tmux-fail-003", attempt_id, "T1", generation, "tmux load-buffer failed"
        )
        self.assertTrue(result["lease_released"])
        self.assertTrue(result["failure_recorded"])
        self.assertTrue(result["cleanup_complete"])
        lease = self.lease_mgr.get("T1")
        self.assertEqual(lease.state, "idle")
        self.assertIsNone(lease.dispatch_id)

    def test_terminal_can_accept_next_dispatch_after_failure(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "tmux-fail-004", "T2",
        )
        self.core.release_on_delivery_failure(
            "tmux-fail-004", attempt_id, "T2", generation, "tmux delivery failed"
        )
        # Terminal must be available for a new dispatch
        self.broker.register("tmux-fail-004b", "Next work", terminal_id="T2")
        result = self.lease_mgr.acquire("T2", dispatch_id="tmux-fail-004b")
        self.assertEqual(result.state, "leased")
        self.assertEqual(result.dispatch_id, "tmux-fail-004b")


# ---------------------------------------------------------------------------
# TestRejectedExecutionHandoff
# ---------------------------------------------------------------------------

class TestRejectedExecutionHandoff(unittest.TestCase):
    """Worker-side rejection during execution handoff — delivery fails after transport."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lease_released_on_rejected_handoff(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "rejected-001", "T2",
        )
        result = self.core.release_on_delivery_failure(
            "rejected-001", attempt_id, "T2", generation,
            reason="rejected_execution_handoff",
        )
        self.assertTrue(result["lease_released"])
        lease = self.lease_mgr.get("T2")
        self.assertEqual(lease.state, "idle")

    def test_failure_reason_preserved_for_rejected_handoff(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "rejected-002", "T2",
        )
        self.core.release_on_delivery_failure(
            "rejected-002", attempt_id, "T2", generation,
            reason="rejected_execution_handoff",
        )
        with get_connection(self.state_dir) as conn:
            attempt_row = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE dispatch_id = ?",
                ("rejected-002",),
            ).fetchone()
        self.assertIsNotNone(attempt_row)
        self.assertIn("rejected_execution_handoff", attempt_row["failure_reason"])

    def test_failed_delivery_event_recorded_for_rejection(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "rejected-003", "T2",
        )
        self.core.release_on_delivery_failure(
            "rejected-003", attempt_id, "T2", generation,
            reason="rejected_execution_handoff",
        )
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="rejected-003")
        event_types = {e["event_type"] for e in events}
        self.assertIn("dispatch_failed_delivery", event_types)


# ---------------------------------------------------------------------------
# TestPromptLoopInterruption
# ---------------------------------------------------------------------------

class TestPromptLoopInterruption(unittest.TestCase):
    """Claude feedback / prompt-loop interruption after explicit clear-context."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lease_released_after_prompt_loop_interruption(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "prompt-loop-001", "T2",
        )
        result = self.core.release_on_delivery_failure(
            "prompt-loop-001", attempt_id, "T2", generation,
            reason="prompt_loop_interrupted_after_clear_context",
        )
        self.assertTrue(result["lease_released"])
        lease = self.lease_mgr.get("T2")
        self.assertEqual(lease.state, "idle")

    def test_cleanup_complete_after_prompt_loop_failure(self):
        attempt_id, generation = _full_delivery_setup(
            self.core, self.broker, self.lease_mgr, self.state_dir,
            "prompt-loop-002", "T3",
        )
        result = self.core.release_on_delivery_failure(
            "prompt-loop-002", attempt_id, "T3", generation,
            reason="prompt_loop_interrupted_after_clear_context",
        )
        self.assertTrue(result["cleanup_complete"], f"cleanup incomplete: {result}")
        self.assertIsNone(result["lease_error"])

    def test_lease_released_even_with_empty_attempt_id(self):
        """Clear-context scenario may lose the attempt_id; lease must still be freed."""
        self.broker.register("prompt-loop-003", "Work", terminal_id="T2")
        lease_result = self.lease_mgr.acquire("T2", dispatch_id="prompt-loop-003")
        generation = lease_result.generation

        # attempt_id is empty — simulates lost state after context reset
        result = self.core.release_on_delivery_failure(
            "prompt-loop-003", "", "T2", generation,
            reason="prompt_loop_interrupted_after_clear_context",
        )
        # failure_recorded may be False (no attempt_id), but lease MUST be released
        self.assertTrue(result["lease_released"], f"lease not released: {result}")
        lease = self.lease_mgr.get("T2")
        self.assertEqual(lease.state, "idle")


# ---------------------------------------------------------------------------
# TestRepeatedRetryThenFailure
# ---------------------------------------------------------------------------

class TestRepeatedRetryThenFailure(unittest.TestCase):
    """Multiple delivery attempts all fail — no stale lease accumulation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def _attempt_and_fail(self, dispatch_id: str, terminal_id: str,
                          attempt_num: int) -> None:
        """Drive one attempt-to-failure cycle, leaving terminal idle.

        The valid re-queue path after failed_delivery is:
          failed_delivery -> recovered -> queued
        """
        # Re-acquire lease for each attempt
        lease_result = self.lease_mgr.acquire(terminal_id, dispatch_id=dispatch_id)
        generation = lease_result.generation

        # Transition dispatch back to queued via the proper recovery path
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, dispatch_id)
            state = row["state"]
            if state == "failed_delivery":
                transition_dispatch(conn, dispatch_id=dispatch_id,
                                    to_state="recovered", actor="test",
                                    reason="recovering for retry")
                transition_dispatch(conn, dispatch_id=dispatch_id,
                                    to_state="queued", actor="test",
                                    reason="re-queue for retry")
                conn.commit()
            elif state == "recovered":
                transition_dispatch(conn, dispatch_id=dispatch_id,
                                    to_state="queued", actor="test",
                                    reason="re-queue for retry")
                conn.commit()

        delivery = self.core.delivery_start(dispatch_id, terminal_id,
                                             attempt_number=attempt_num)
        attempt_id = delivery.attempt_id or ""

        result = self.core.release_on_delivery_failure(
            dispatch_id, attempt_id, terminal_id,
            generation, f"failure attempt {attempt_num}",
        )
        self.assertTrue(result["lease_released"],
                        f"attempt {attempt_num} left stale lease: {result}")

    def test_no_stale_lease_after_three_failed_attempts(self):
        dispatch_id = "retry-fail-001"
        self.broker.register(dispatch_id, "Work", terminal_id="T2")

        for attempt in range(1, 4):
            self._attempt_and_fail(dispatch_id, "T2", attempt)
            lease = self.lease_mgr.get("T2")
            self.assertEqual(
                lease.state, "idle",
                f"T2 not idle after attempt {attempt}: {lease.state}",
            )

    def test_retry_attempt_count_increments_correctly(self):
        dispatch_id = "retry-fail-002"
        self.broker.register(dispatch_id, "Work", terminal_id="T1")

        for attempt in range(1, 3):
            self._attempt_and_fail(dispatch_id, "T1", attempt)

        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, dispatch_id)
        self.assertGreaterEqual(row["attempt_count"], 2)

    def test_terminal_idle_between_every_retry(self):
        """Between each failed attempt the terminal must return to idle."""
        dispatch_id = "retry-fail-003"
        self.broker.register(dispatch_id, "Work", terminal_id="T3")

        for attempt in range(1, 4):
            self._attempt_and_fail(dispatch_id, "T3", attempt)
            # Terminal must be idle before the next attempt can acquire the lease
            lease = self.lease_mgr.get("T3")
            self.assertEqual(lease.state, "idle",
                             f"T3 not idle before attempt {attempt + 1}")


# ---------------------------------------------------------------------------
# TestCleanupFailureExplicitInAudit
# ---------------------------------------------------------------------------

class TestCleanupFailureExplicitInAudit(unittest.TestCase):
    """Cleanup failures must be explicit in the result, not silently dropped."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stale_generation_produces_explicit_lease_error(self):
        """Wrong generation → lease_released=False and lease_error is populated."""
        self.broker.register("cleanup-audit-001", "Work", terminal_id="T1")
        lease_result = self.lease_mgr.acquire("T1", dispatch_id="cleanup-audit-001")
        generation = lease_result.generation

        delivery = self.core.delivery_start("cleanup-audit-001", "T1")
        attempt_id = delivery.attempt_id or ""

        stale_generation = generation - 1
        result = self.core.release_on_delivery_failure(
            "cleanup-audit-001", attempt_id, "T1",
            stale_generation, "tmux failed",
        )
        self.assertFalse(result["lease_released"],
                         "stale generation should not release lease")
        self.assertIsNotNone(result["lease_error"],
                             "lease_error must be populated, not None")

    def test_cleanup_complete_false_when_lease_release_fails(self):
        self.broker.register("cleanup-audit-002", "Work", terminal_id="T2")
        lease_result = self.lease_mgr.acquire("T2", dispatch_id="cleanup-audit-002")
        generation = lease_result.generation

        delivery = self.core.delivery_start("cleanup-audit-002", "T2")
        attempt_id = delivery.attempt_id or ""

        result = self.core.release_on_delivery_failure(
            "cleanup-audit-002", attempt_id, "T2",
            generation - 1, "tmux failed",
        )
        self.assertFalse(result["cleanup_complete"])

    def test_lease_released_even_when_failure_recording_errors(self):
        """deliver_failure recording error must NOT prevent lease release."""
        self.broker.register("cleanup-audit-003", "Work", terminal_id="T2")
        lease_result = self.lease_mgr.acquire("T2", dispatch_id="cleanup-audit-003")
        generation = lease_result.generation

        delivery = self.core.delivery_start("cleanup-audit-003", "T2")
        attempt_id = delivery.attempt_id or ""

        # Patch broker.deliver_failure to raise, simulating partial failure
        original = self.broker.deliver_failure

        def _raise(*_args, **_kwargs):
            raise RuntimeError("simulated broker failure_recording error")

        self.broker.deliver_failure = _raise
        try:
            result = self.core.release_on_delivery_failure(
                "cleanup-audit-003", attempt_id, "T2",
                generation, "tmux failed",
            )
        finally:
            self.broker.deliver_failure = original

        # Lease MUST be released even though failure_recorded=False
        self.assertFalse(result["failure_recorded"])
        self.assertTrue(result["lease_released"],
                        f"lease not released despite broker error: {result}")
        self.assertIsNotNone(result["failure_error"])
        lease = self.lease_mgr.get("T2")
        self.assertEqual(lease.state, "idle")

    def test_result_structure_always_contains_required_keys(self):
        """Every result must have the full set of audit keys."""
        self.broker.register("cleanup-audit-004", "Work", terminal_id="T1")
        lease_result = self.lease_mgr.acquire("T1", dispatch_id="cleanup-audit-004")
        generation = lease_result.generation

        delivery = self.core.delivery_start("cleanup-audit-004", "T1")
        attempt_id = delivery.attempt_id or ""

        result = self.core.release_on_delivery_failure(
            "cleanup-audit-004", attempt_id, "T1",
            generation, "tmux failed",
        )
        required_keys = {
            "dispatch_id", "terminal_id",
            "failure_recorded", "lease_released",
            "cleanup_complete", "failure_error", "lease_error",
        }
        self.assertEqual(required_keys, required_keys & result.keys(),
                         f"Missing keys in result: {required_keys - result.keys()}")


# ---------------------------------------------------------------------------
# TestLeasePairingInvariant
# ---------------------------------------------------------------------------

class TestLeasePairingInvariant(unittest.TestCase):
    """Terminal claim and canonical lease cleanup remain paired under test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.broker, self.lease_mgr, self.core = \
            _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lease_returns_to_idle_for_every_failed_delivery_path(self):
        """All four failure reasons leave the lease in idle state."""
        failure_reasons = [
            "tmux delivery failed",
            "rejected_execution_handoff",
            "prompt_loop_interrupted_after_clear_context",
            "tmux Enter failed",
        ]
        terminal_id = "T1"

        for i, reason in enumerate(failure_reasons):
            dispatch_id = f"pairing-{i:03d}"
            self.broker.register(dispatch_id, "Work", terminal_id=terminal_id)
            lease_result = self.lease_mgr.acquire(terminal_id, dispatch_id=dispatch_id)
            generation = lease_result.generation

            delivery = self.core.delivery_start(dispatch_id, terminal_id)
            attempt_id = delivery.attempt_id or ""

            result = self.core.release_on_delivery_failure(
                dispatch_id, attempt_id, terminal_id, generation, reason,
            )
            self.assertTrue(
                result["lease_released"],
                f"Lease not released for reason={reason!r}: {result}",
            )
            lease = self.lease_mgr.get(terminal_id)
            self.assertEqual(
                lease.state, "idle",
                f"Terminal not idle after reason={reason!r}: {lease.state}",
            )

    def test_lease_generation_advances_after_release_and_reacquire(self):
        """Generation strictly increases after each release, preventing stale renewal."""
        dispatch_id = "pairing-gen-001"
        terminal_id = "T2"
        self.broker.register(dispatch_id, "Work", terminal_id=terminal_id)

        lease_r1 = self.lease_mgr.acquire(terminal_id, dispatch_id=dispatch_id)
        gen1 = lease_r1.generation

        delivery = self.core.delivery_start(dispatch_id, terminal_id)
        self.core.release_on_delivery_failure(
            dispatch_id, delivery.attempt_id or "", terminal_id, gen1, "failed"
        )

        # Re-register for second attempt via proper recovery path
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id=dispatch_id,
                                to_state="recovered", actor="test", reason="recover")
            transition_dispatch(conn, dispatch_id=dispatch_id,
                                to_state="queued", actor="test", reason="re-queue")
            conn.commit()
        self.broker.register(f"{dispatch_id}-b", "Work", terminal_id=terminal_id)
        lease_r2 = self.lease_mgr.acquire(terminal_id, dispatch_id=f"{dispatch_id}-b")
        gen2 = lease_r2.generation

        self.assertGreater(gen2, gen1,
                           "Generation must increment to invalidate stale heartbeats")


if __name__ == "__main__":
    unittest.main()
