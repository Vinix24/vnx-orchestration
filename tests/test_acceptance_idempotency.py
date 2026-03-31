#!/usr/bin/env python3
"""
Tests for dispatch acceptance idempotency guard (PR-0).

Quality gate coverage (gate_pr0_acceptance_idempotency):
  - Duplicate acceptance for an already terminal dispatch is rejected or
    no-op classified with explicit evidence
  - Forward-only valid dispatch transitions still pass without regression
  - Existing dispatch lifecycle tests and new duplicate-acceptance tests pass
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (
    ACCEPTED_OR_BEYOND_STATES,
    TERMINAL_DISPATCH_STATES,
    DuplicateTransitionError,
    InvalidTransitionError,
    get_connection,
    get_dispatch,
    get_events,
    init_schema,
    is_accepted_or_beyond,
    is_terminal_dispatch_state,
    transition_dispatch,
    transition_dispatch_idempotent,
    validate_dispatch_transition,
)
from dispatch_broker import (
    BrokerError,
    DispatchBroker,
)


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _setup_dirs(tmp: tempfile.TemporaryDirectory) -> tuple[str, str]:
    base = Path(tmp.name)
    state_dir = str(base / "state")
    dispatch_dir = str(base / "dispatches")
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    Path(dispatch_dir).mkdir(parents=True, exist_ok=True)
    init_schema(state_dir)
    return state_dir, dispatch_dir


def _make_broker(state_dir: str, dispatch_dir: str) -> DispatchBroker:
    return DispatchBroker(state_dir, dispatch_dir, shadow_mode=False)


def _register_and_accept(broker: DispatchBroker, dispatch_id: str, state_dir: str) -> str:
    """Register, claim, deliver_start, deliver_success. Returns attempt_id."""
    broker.register(dispatch_id, "test prompt")
    claim = broker.claim(dispatch_id, "T1")
    broker.deliver_start(dispatch_id, claim.attempt_id)
    broker.deliver_success(dispatch_id, claim.attempt_id)
    return claim.attempt_id


# ---------------------------------------------------------------------------
# TestTerminalStateConstants
# ---------------------------------------------------------------------------

class TestTerminalStateConstants(unittest.TestCase):
    """Verify terminal state constants are correctly defined."""

    def test_terminal_states_are_subset_of_dispatch_states(self) -> None:
        from runtime_coordination import DISPATCH_STATES
        self.assertTrue(TERMINAL_DISPATCH_STATES.issubset(DISPATCH_STATES))

    def test_terminal_states_have_no_outgoing_transitions(self) -> None:
        from runtime_coordination import DISPATCH_TRANSITIONS
        for state in TERMINAL_DISPATCH_STATES:
            self.assertEqual(
                DISPATCH_TRANSITIONS[state],
                frozenset(),
                f"Terminal state {state!r} should have no outgoing transitions",
            )

    def test_accepted_or_beyond_includes_accepted(self) -> None:
        self.assertIn("accepted", ACCEPTED_OR_BEYOND_STATES)

    def test_accepted_or_beyond_includes_running(self) -> None:
        self.assertIn("running", ACCEPTED_OR_BEYOND_STATES)

    def test_accepted_or_beyond_includes_completed(self) -> None:
        self.assertIn("completed", ACCEPTED_OR_BEYOND_STATES)

    def test_is_terminal_dispatch_state_true(self) -> None:
        self.assertTrue(is_terminal_dispatch_state("completed"))
        self.assertTrue(is_terminal_dispatch_state("expired"))
        self.assertTrue(is_terminal_dispatch_state("dead_letter"))

    def test_is_terminal_dispatch_state_false(self) -> None:
        self.assertFalse(is_terminal_dispatch_state("queued"))
        self.assertFalse(is_terminal_dispatch_state("accepted"))
        self.assertFalse(is_terminal_dispatch_state("running"))

    def test_is_accepted_or_beyond_true(self) -> None:
        self.assertTrue(is_accepted_or_beyond("accepted"))
        self.assertTrue(is_accepted_or_beyond("running"))
        self.assertTrue(is_accepted_or_beyond("completed"))

    def test_is_accepted_or_beyond_false(self) -> None:
        self.assertFalse(is_accepted_or_beyond("queued"))
        self.assertFalse(is_accepted_or_beyond("claimed"))
        self.assertFalse(is_accepted_or_beyond("delivering"))


# ---------------------------------------------------------------------------
# TestDuplicateTransitionError
# ---------------------------------------------------------------------------

class TestDuplicateTransitionError(unittest.TestCase):
    """Verify DuplicateTransitionError is a proper subclass."""

    def test_is_subclass_of_invalid_transition_error(self) -> None:
        self.assertTrue(issubclass(DuplicateTransitionError, InvalidTransitionError))

    def test_caught_by_invalid_transition_error_handler(self) -> None:
        with self.assertRaises(InvalidTransitionError):
            raise DuplicateTransitionError(
                "test",
                dispatch_id="d-1",
                current_state="completed",
                requested_state="accepted",
            )

    def test_attributes_preserved(self) -> None:
        err = DuplicateTransitionError(
            "test message",
            dispatch_id="d-1",
            current_state="completed",
            requested_state="accepted",
        )
        self.assertEqual(err.dispatch_id, "d-1")
        self.assertEqual(err.current_state, "completed")
        self.assertEqual(err.requested_state, "accepted")


# ---------------------------------------------------------------------------
# TestTransitionDispatchIdempotent
# ---------------------------------------------------------------------------

class TestTransitionDispatchIdempotent(unittest.TestCase):
    """Test the idempotent transition function at the coordination layer."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.broker = _make_broker(self.state_dir, self.dispatch_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _register(self, dispatch_id: str) -> None:
        self.broker.register(dispatch_id, "test prompt")

    def test_same_state_is_noop(self) -> None:
        """Requesting the current state returns the row unchanged."""
        self._register("td-001")
        with get_connection(self.state_dir) as conn:
            row = transition_dispatch_idempotent(
                conn, dispatch_id="td-001", to_state="queued", actor="test",
            )
            conn.commit()
        self.assertEqual(row["state"], "queued")

    def test_same_state_appends_noop_event(self) -> None:
        self._register("td-002")
        with get_connection(self.state_dir) as conn:
            transition_dispatch_idempotent(
                conn, dispatch_id="td-002", to_state="queued", actor="test",
            )
            conn.commit()
            events = get_events(conn, entity_id="td-002", event_type="dispatch_noop")
        self.assertTrue(len(events) >= 1)

    def test_terminal_state_raises_duplicate_transition_error(self) -> None:
        self._register("td-003")
        # Move to completed via the normal path
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="td-003", to_state="claimed", actor="test")
            transition_dispatch(conn, dispatch_id="td-003", to_state="delivering", actor="test")
            transition_dispatch(conn, dispatch_id="td-003", to_state="accepted", actor="test")
            transition_dispatch(conn, dispatch_id="td-003", to_state="running", actor="test")
            transition_dispatch(conn, dispatch_id="td-003", to_state="completed", actor="test")
            conn.commit()

        with get_connection(self.state_dir) as conn:
            with self.assertRaises(DuplicateTransitionError) as ctx:
                transition_dispatch_idempotent(
                    conn, dispatch_id="td-003", to_state="accepted", actor="test",
                )
            self.assertEqual(ctx.exception.current_state, "completed")

    def test_accepted_requesting_accepted_is_noop(self) -> None:
        self._register("td-004")
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="td-004", to_state="claimed", actor="test")
            transition_dispatch(conn, dispatch_id="td-004", to_state="delivering", actor="test")
            transition_dispatch(conn, dispatch_id="td-004", to_state="accepted", actor="test")
            conn.commit()

        with get_connection(self.state_dir) as conn:
            row = transition_dispatch_idempotent(
                conn, dispatch_id="td-004", to_state="accepted", actor="test",
            )
            conn.commit()
        self.assertEqual(row["state"], "accepted")

    def test_running_requesting_accepted_is_noop(self) -> None:
        """Already past accepted → no-op, not error."""
        self._register("td-005")
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="td-005", to_state="claimed", actor="test")
            transition_dispatch(conn, dispatch_id="td-005", to_state="delivering", actor="test")
            transition_dispatch(conn, dispatch_id="td-005", to_state="accepted", actor="test")
            transition_dispatch(conn, dispatch_id="td-005", to_state="running", actor="test")
            conn.commit()

        with get_connection(self.state_dir) as conn:
            row = transition_dispatch_idempotent(
                conn, dispatch_id="td-005", to_state="accepted", actor="test",
            )
            conn.commit()
        self.assertEqual(row["state"], "running")

    def test_valid_forward_transition_still_works(self) -> None:
        self._register("td-006")
        with get_connection(self.state_dir) as conn:
            row = transition_dispatch_idempotent(
                conn, dispatch_id="td-006", to_state="claimed", actor="test",
            )
            conn.commit()
        self.assertEqual(row["state"], "claimed")

    def test_nonexistent_dispatch_raises_key_error(self) -> None:
        with get_connection(self.state_dir) as conn:
            with self.assertRaises(KeyError):
                transition_dispatch_idempotent(
                    conn, dispatch_id="nonexistent", to_state="accepted", actor="test",
                )


# ---------------------------------------------------------------------------
# TestBrokerDeliverSuccessIdempotency
# ---------------------------------------------------------------------------

class TestBrokerDeliverSuccessIdempotency(unittest.TestCase):
    """Test deliver_success idempotency at the broker layer."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.broker = _make_broker(self.state_dir, self.dispatch_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_deliver_success_transitions_normally(self) -> None:
        self.broker.register("ds-001", "prompt")
        claim = self.broker.claim("ds-001", "T1")
        self.broker.deliver_start("ds-001", claim.attempt_id)
        result = self.broker.deliver_success("ds-001", claim.attempt_id)
        self.assertTrue(result["transitioned"])
        self.assertFalse(result["noop"])
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "ds-001")
        self.assertEqual(row["state"], "accepted")

    def test_duplicate_deliver_success_is_noop(self) -> None:
        self.broker.register("ds-002", "prompt")
        claim = self.broker.claim("ds-002", "T1")
        self.broker.deliver_start("ds-002", claim.attempt_id)
        self.broker.deliver_success("ds-002", claim.attempt_id)

        # Second call — should be no-op, not error
        result = self.broker.deliver_success("ds-002", claim.attempt_id)
        self.assertFalse(result["transitioned"])
        self.assertTrue(result["noop"])
        self.assertEqual(result["current_state"], "accepted")

    def test_duplicate_deliver_success_does_not_mutate_state(self) -> None:
        self.broker.register("ds-003", "prompt")
        claim = self.broker.claim("ds-003", "T1")
        self.broker.deliver_start("ds-003", claim.attempt_id)
        self.broker.deliver_success("ds-003", claim.attempt_id)

        with get_connection(self.state_dir) as conn:
            row_before = get_dispatch(conn, "ds-003")

        self.broker.deliver_success("ds-003", claim.attempt_id)

        with get_connection(self.state_dir) as conn:
            row_after = get_dispatch(conn, "ds-003")

        self.assertEqual(row_before["state"], row_after["state"])
        self.assertEqual(row_before["updated_at"], row_after["updated_at"])

    def test_deliver_success_on_running_dispatch_is_noop(self) -> None:
        """Dispatch already past accepted (in running) → no-op."""
        self.broker.register("ds-004", "prompt")
        claim = self.broker.claim("ds-004", "T1")
        self.broker.deliver_start("ds-004", claim.attempt_id)
        self.broker.deliver_success("ds-004", claim.attempt_id)

        # Move to running
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="ds-004", to_state="running", actor="test")
            conn.commit()

        result = self.broker.deliver_success("ds-004", claim.attempt_id)
        self.assertTrue(result["noop"])
        self.assertEqual(result["current_state"], "running")

    def test_deliver_success_on_completed_dispatch_raises(self) -> None:
        """Terminal state (completed) → DuplicateTransitionError."""
        self.broker.register("ds-005", "prompt")
        claim = self.broker.claim("ds-005", "T1")
        self.broker.deliver_start("ds-005", claim.attempt_id)
        self.broker.deliver_success("ds-005", claim.attempt_id)

        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="ds-005", to_state="running", actor="test")
            transition_dispatch(conn, dispatch_id="ds-005", to_state="completed", actor="test")
            conn.commit()

        with self.assertRaises(DuplicateTransitionError) as ctx:
            self.broker.deliver_success("ds-005", claim.attempt_id)
        self.assertEqual(ctx.exception.current_state, "completed")

    def test_deliver_success_on_expired_dispatch_raises(self) -> None:
        self.broker.register("ds-006", "prompt")
        claim = self.broker.claim("ds-006", "T1")
        self.broker.deliver_start("ds-006", claim.attempt_id)
        self.broker.deliver_success("ds-006", claim.attempt_id)

        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="ds-006", to_state="running", actor="test")
            transition_dispatch(conn, dispatch_id="ds-006", to_state="timed_out", actor="test")
            transition_dispatch(conn, dispatch_id="ds-006", to_state="expired", actor="test")
            conn.commit()

        with self.assertRaises(DuplicateTransitionError) as ctx:
            self.broker.deliver_success("ds-006", claim.attempt_id)
        self.assertEqual(ctx.exception.current_state, "expired")

    def test_deliver_success_on_dead_letter_dispatch_raises(self) -> None:
        self.broker.register("ds-007", "prompt")
        claim = self.broker.claim("ds-007", "T1")
        self.broker.deliver_start("ds-007", claim.attempt_id)
        self.broker.deliver_success("ds-007", claim.attempt_id)

        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="ds-007", to_state="running", actor="test")
            transition_dispatch(conn, dispatch_id="ds-007", to_state="timed_out", actor="test")
            transition_dispatch(conn, dispatch_id="ds-007", to_state="dead_letter", actor="test")
            conn.commit()

        with self.assertRaises(DuplicateTransitionError) as ctx:
            self.broker.deliver_success("ds-007", claim.attempt_id)
        self.assertEqual(ctx.exception.current_state, "dead_letter")

    def test_deliver_success_noop_appends_audit_event(self) -> None:
        """Duplicate acceptance must leave an audit trail."""
        self.broker.register("ds-008", "prompt")
        claim = self.broker.claim("ds-008", "T1")
        self.broker.deliver_start("ds-008", claim.attempt_id)
        self.broker.deliver_success("ds-008", claim.attempt_id)
        self.broker.deliver_success("ds-008", claim.attempt_id)

        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="ds-008", event_type="dispatch_noop")
        self.assertTrue(len(events) >= 1, "No dispatch_noop event found for duplicate acceptance")

    def test_deliver_success_terminal_rejection_appends_audit_event(self) -> None:
        """Terminal-state rejection must leave an audit trail."""
        self.broker.register("ds-009", "prompt")
        claim = self.broker.claim("ds-009", "T1")
        self.broker.deliver_start("ds-009", claim.attempt_id)
        self.broker.deliver_success("ds-009", claim.attempt_id)

        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="ds-009", to_state="running", actor="test")
            transition_dispatch(conn, dispatch_id="ds-009", to_state="completed", actor="test")
            conn.commit()

        try:
            self.broker.deliver_success("ds-009", claim.attempt_id)
        except DuplicateTransitionError:
            pass

        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="ds-009", event_type="dispatch_acceptance_rejected")
        self.assertTrue(
            len(events) >= 1,
            "No dispatch_acceptance_rejected event found for terminal-state rejection",
        )

    def test_deliver_success_nonexistent_dispatch_raises_broker_error(self) -> None:
        with self.assertRaises(BrokerError):
            self.broker.deliver_success("nonexistent", "fake-attempt")


# ---------------------------------------------------------------------------
# TestForwardTransitionsUnchanged
# ---------------------------------------------------------------------------

class TestForwardTransitionsUnchanged(unittest.TestCase):
    """Verify that valid forward transitions are not regressed by the guard."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.broker = _make_broker(self.state_dir, self.dispatch_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_happy_path_queued_to_completed(self) -> None:
        self.broker.register("fwd-001", "prompt")
        claim = self.broker.claim("fwd-001", "T1")
        self.broker.deliver_start("fwd-001", claim.attempt_id)
        result = self.broker.deliver_success("fwd-001", claim.attempt_id)
        self.assertTrue(result["transitioned"])

        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="fwd-001", to_state="running", actor="test")
            transition_dispatch(conn, dispatch_id="fwd-001", to_state="completed", actor="test")
            conn.commit()
            row = get_dispatch(conn, "fwd-001")
        self.assertEqual(row["state"], "completed")

    def test_delivery_failure_path(self) -> None:
        self.broker.register("fwd-002", "prompt")
        claim = self.broker.claim("fwd-002", "T1")
        self.broker.deliver_start("fwd-002", claim.attempt_id)
        self.broker.deliver_failure("fwd-002", claim.attempt_id, "pane gone")

        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "fwd-002")
        self.assertEqual(row["state"], "failed_delivery")

    def test_recovery_and_requeue_path(self) -> None:
        self.broker.register("fwd-003", "prompt")
        claim = self.broker.claim("fwd-003", "T1")
        self.broker.deliver_start("fwd-003", claim.attempt_id)
        self.broker.deliver_failure("fwd-003", claim.attempt_id, "timeout")

        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="fwd-003", to_state="recovered", actor="test")
            transition_dispatch(conn, dispatch_id="fwd-003", to_state="queued", actor="test")
            conn.commit()
            row = get_dispatch(conn, "fwd-003")
        self.assertEqual(row["state"], "queued")

    def test_claim_still_rejects_non_queued(self) -> None:
        self.broker.register("fwd-004", "prompt")
        self.broker.claim("fwd-004", "T1")
        with self.assertRaises(BrokerError):
            self.broker.claim("fwd-004", "T2")

    def test_strict_transition_dispatch_still_raises_for_invalid(self) -> None:
        """The strict transition_dispatch is not affected by idempotency changes."""
        self.broker.register("fwd-005", "prompt")
        with get_connection(self.state_dir) as conn:
            with self.assertRaises(InvalidTransitionError):
                transition_dispatch(conn, dispatch_id="fwd-005", to_state="accepted", actor="test")


# ---------------------------------------------------------------------------
# TestTripleAcceptance
# ---------------------------------------------------------------------------

class TestTripleAcceptance(unittest.TestCase):
    """Stress test: call deliver_success 3 times on the same dispatch."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.broker = _make_broker(self.state_dir, self.dispatch_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_triple_deliver_success(self) -> None:
        self.broker.register("tri-001", "prompt")
        claim = self.broker.claim("tri-001", "T1")
        self.broker.deliver_start("tri-001", claim.attempt_id)

        r1 = self.broker.deliver_success("tri-001", claim.attempt_id)
        r2 = self.broker.deliver_success("tri-001", claim.attempt_id)
        r3 = self.broker.deliver_success("tri-001", claim.attempt_id)

        self.assertTrue(r1["transitioned"])
        self.assertTrue(r2["noop"])
        self.assertTrue(r3["noop"])

        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "tri-001")
        self.assertEqual(row["state"], "accepted")

        # Exactly 2 no-op events
        with get_connection(self.state_dir) as conn:
            noops = get_events(conn, entity_id="tri-001", event_type="dispatch_noop")
        self.assertEqual(len(noops), 2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
