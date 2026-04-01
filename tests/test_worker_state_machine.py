#!/usr/bin/env python3
"""
Tests for worker_state_manager.py (Feature 12, PR-1)

Quality gate coverage (gate_pr1_state_machine_and_heartbeat):
  - All runtime state machine and heartbeat tests pass
  - Launch, output, clean exit, bad exit, and interruption transitions are explicit
  - Heartbeat and last-output timestamps persist in canonical runtime state
  - T0-readable state surface exists without scraping terminal behavior
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (
    get_connection,
    get_events,
    init_schema,
    register_dispatch,
    acquire_lease,
)
from worker_state_manager import (
    WORKER_STATES,
    WORKER_TRANSITIONS,
    TERMINAL_WORKER_STATES,
    ACTIVE_WORKER_STATES,
    DEFAULT_HEARTBEAT_STALE_THRESHOLD,
    DEFAULT_HEARTBEAT_DEAD_THRESHOLD,
    WorkerStateManager,
    WorkerStateResult,
    InvalidWorkerStateError,
    InvalidWorkerTransitionError,
    classify_heartbeat,
    validate_worker_state,
    validate_worker_transition,
    is_terminal_worker_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _setup_state_dir(tmp_path: str) -> str:
    """Initialize schema and return state_dir."""
    state_dir = os.path.join(tmp_path, "state")
    os.makedirs(state_dir, exist_ok=True)
    init_schema(state_dir)
    return state_dir


def _register_and_lease(state_dir: str, terminal_id: str = "T1", dispatch_id: str = "d-001"):
    """Register a dispatch and acquire a lease — prerequisites for worker state."""
    with get_connection(state_dir) as conn:
        register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal_id, track="B")
        acquire_lease(conn, terminal_id=terminal_id, dispatch_id=dispatch_id)
        conn.commit()


# ===========================================================================
# Test: State validation
# ===========================================================================

class TestWorkerStateValidation(unittest.TestCase):

    def test_all_states_defined(self):
        expected = {
            "initializing", "working", "idle_between_tasks", "stalled",
            "blocked", "awaiting_input", "exited_clean", "exited_bad",
            "resume_unsafe",
        }
        self.assertEqual(WORKER_STATES, expected)

    def test_terminal_states(self):
        self.assertEqual(TERMINAL_WORKER_STATES, {"exited_clean", "exited_bad", "resume_unsafe"})

    def test_active_states(self):
        self.assertEqual(ACTIVE_WORKER_STATES, WORKER_STATES - TERMINAL_WORKER_STATES)

    def test_validate_valid_state(self):
        for state in WORKER_STATES:
            validate_worker_state(state)

    def test_validate_invalid_state_raises(self):
        with self.assertRaises(InvalidWorkerStateError):
            validate_worker_state("nonexistent")

    def test_is_terminal(self):
        for state in TERMINAL_WORKER_STATES:
            self.assertTrue(is_terminal_worker_state(state))
        for state in ACTIVE_WORKER_STATES:
            self.assertFalse(is_terminal_worker_state(state))


# ===========================================================================
# Test: Transition matrix (§3.2)
# ===========================================================================

class TestTransitionMatrix(unittest.TestCase):

    def test_terminal_states_have_no_outgoing(self):
        """W-T1: Terminal states have no outgoing transitions."""
        for state in TERMINAL_WORKER_STATES:
            self.assertEqual(WORKER_TRANSITIONS[state], frozenset())

    def test_initializing_cannot_reach_idle_between_tasks(self):
        """W-T4: initializing cannot reach idle_between_tasks."""
        self.assertNotIn("idle_between_tasks", WORKER_TRANSITIONS["initializing"])

    def test_initializing_cannot_reach_awaiting_input(self):
        """W-T4: initializing cannot reach awaiting_input."""
        self.assertNotIn("awaiting_input", WORKER_TRANSITIONS["initializing"])

    def test_stalled_cannot_reach_idle_between_tasks(self):
        """W-T2: stalled can only recover to working or exit."""
        allowed = WORKER_TRANSITIONS["stalled"]
        self.assertNotIn("idle_between_tasks", allowed)
        self.assertIn("working", allowed)

    def test_blocked_cannot_reach_idle_between_tasks(self):
        """W-T3: blocked can only recover to working or exit."""
        self.assertNotIn("idle_between_tasks", WORKER_TRANSITIONS["blocked"])

    def test_awaiting_input_cannot_reach_idle_between_tasks(self):
        """W-T3: awaiting_input can only recover to working or exit."""
        self.assertNotIn("idle_between_tasks", WORKER_TRANSITIONS["awaiting_input"])

    def test_valid_transition_passes(self):
        validate_worker_transition("initializing", "working")
        validate_worker_transition("working", "exited_clean")

    def test_invalid_transition_raises(self):
        with self.assertRaises(InvalidWorkerTransitionError):
            validate_worker_transition("exited_clean", "working")

    def test_all_transitions_reference_valid_states(self):
        for from_state, to_states in WORKER_TRANSITIONS.items():
            self.assertIn(from_state, WORKER_STATES)
            for to_state in to_states:
                self.assertIn(to_state, WORKER_STATES)


# ===========================================================================
# Test: Worker state lifecycle
# ===========================================================================

class TestWorkerStateLifecycle(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = _setup_state_dir(self._tmp.name)
        _register_and_lease(self.state_dir)
        self.mgr = WorkerStateManager(self.state_dir, auto_init=False)

    def tearDown(self):
        self._tmp.cleanup()

    # -- Initialize --

    def test_initialize_creates_worker_state(self):
        result = self.mgr.initialize("T1", "d-001")
        self.assertEqual(result.terminal_id, "T1")
        self.assertEqual(result.dispatch_id, "d-001")
        self.assertEqual(result.state, "initializing")
        self.assertIsNone(result.last_output_at)
        self.assertEqual(result.stall_count, 0)
        self.assertIsNone(result.blocked_reason)

    def test_initialize_replaces_existing(self):
        self.mgr.initialize("T1", "d-001")
        _register_and_lease_second(self.state_dir)
        result = self.mgr.initialize("T1", "d-002")
        self.assertEqual(result.dispatch_id, "d-002")
        self.assertEqual(result.state, "initializing")

    # -- Launch → Working (first output) --

    def test_launch_to_working(self):
        self.mgr.initialize("T1", "d-001")
        result = self.mgr.transition("T1", "working", reason="first stdout output")
        self.assertEqual(result.state, "working")

    # -- Working → Clean exit --

    def test_working_to_exited_clean(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        result = self.mgr.transition("T1", "exited_clean", reason="exit code 0")
        self.assertEqual(result.state, "exited_clean")

    # -- Working → Bad exit --

    def test_working_to_exited_bad(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        result = self.mgr.transition("T1", "exited_bad", reason="exit code 1")
        self.assertEqual(result.state, "exited_bad")

    # -- Interruption → resume_unsafe --

    def test_working_to_resume_unsafe(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        result = self.mgr.transition("T1", "resume_unsafe", reason="forced termination")
        self.assertEqual(result.state, "resume_unsafe")

    def test_stalled_to_resume_unsafe(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "stalled")
        result = self.mgr.transition("T1", "resume_unsafe", reason="TTL expiry during stall")
        self.assertEqual(result.state, "resume_unsafe")

    # -- Working → idle_between_tasks → working --

    def test_idle_between_tasks_cycle(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        result = self.mgr.transition("T1", "idle_between_tasks", reason="sub-task boundary")
        self.assertEqual(result.state, "idle_between_tasks")
        result = self.mgr.transition("T1", "working", reason="next sub-task started")
        self.assertEqual(result.state, "working")

    # -- Blocked --

    def test_blocked_with_reason(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        result = self.mgr.transition(
            "T1", "blocked",
            blocked_reason="MCP tool timeout: brave_web_search",
        )
        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.blocked_reason, "MCP tool timeout: brave_web_search")

    def test_blocked_to_working_clears_reason(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "blocked", blocked_reason="test")
        result = self.mgr.transition("T1", "working", reason="unblocked")
        self.assertIsNone(result.blocked_reason)

    # -- Awaiting input --

    def test_awaiting_input_from_working(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        result = self.mgr.transition("T1", "awaiting_input")
        self.assertEqual(result.state, "awaiting_input")

    # -- Stall count --

    def test_stall_count_increments(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        r1 = self.mgr.transition("T1", "stalled")
        self.assertEqual(r1.stall_count, 1)
        self.mgr.transition("T1", "working")
        r2 = self.mgr.transition("T1", "stalled")
        self.assertEqual(r2.stall_count, 2)

    # -- Terminal states block further transitions --

    def test_terminal_state_blocks_transition(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "exited_clean")
        with self.assertRaises(InvalidWorkerTransitionError):
            self.mgr.transition("T1", "working")

    # -- Missing worker state --

    def test_transition_without_initialize_raises(self):
        with self.assertRaises(KeyError):
            self.mgr.transition("T1", "working")

    # -- Illegal transitions --

    def test_initializing_to_idle_between_tasks_raises(self):
        self.mgr.initialize("T1", "d-001")
        with self.assertRaises(InvalidWorkerTransitionError):
            self.mgr.transition("T1", "idle_between_tasks")

    def test_initializing_to_awaiting_input_raises(self):
        self.mgr.initialize("T1", "d-001")
        with self.assertRaises(InvalidWorkerTransitionError):
            self.mgr.transition("T1", "awaiting_input")

    def test_stalled_to_idle_between_tasks_raises(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "stalled")
        with self.assertRaises(InvalidWorkerTransitionError):
            self.mgr.transition("T1", "idle_between_tasks")


# ===========================================================================
# Test: Output tracking and heartbeat persistence
# ===========================================================================

class TestOutputAndHeartbeatPersistence(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = _setup_state_dir(self._tmp.name)
        _register_and_lease(self.state_dir)
        self.mgr = WorkerStateManager(self.state_dir, auto_init=False)

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_output_updates_last_output_at(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        result = self.mgr.record_output("T1")
        self.assertIsNotNone(result.last_output_at)

    def test_record_output_does_not_change_state(self):
        """H-2: output events update last_output_at, not heartbeat or state."""
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        result = self.mgr.record_output("T1")
        self.assertEqual(result.state, "working")

    def test_last_output_at_persists_across_reads(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.record_output("T1")
        state = self.mgr.get("T1")
        self.assertIsNotNone(state.last_output_at)

    def test_state_entered_at_updates_on_transition(self):
        self.mgr.initialize("T1", "d-001")
        r1 = self.mgr.get("T1")
        entered_init = r1.state_entered_at
        r2 = self.mgr.transition("T1", "working")
        self.assertNotEqual(r2.state_entered_at, entered_init)

    def test_heartbeat_in_lease_persists(self):
        """Heartbeat is tracked in terminal_leases.last_heartbeat_at (existing)."""
        with get_connection(self.state_dir) as conn:
            row = conn.execute(
                "SELECT last_heartbeat_at FROM terminal_leases WHERE terminal_id = 'T1'"
            ).fetchone()
        self.assertIsNotNone(row["last_heartbeat_at"])


# ===========================================================================
# Test: Heartbeat classification (§4.2)
# ===========================================================================

class TestHeartbeatClassification(unittest.TestCase):

    def _ts(self, seconds_ago: float) -> str:
        ts = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        return ts.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    def test_fresh(self):
        self.assertEqual(classify_heartbeat(self._ts(10)), "fresh")

    def test_stale(self):
        self.assertEqual(classify_heartbeat(self._ts(100)), "stale")

    def test_dead(self):
        self.assertEqual(classify_heartbeat(self._ts(400)), "dead")

    def test_none_is_dead(self):
        self.assertEqual(classify_heartbeat(None), "dead")

    def test_boundary_fresh_stale(self):
        self.assertEqual(classify_heartbeat(self._ts(89)), "fresh")
        self.assertEqual(classify_heartbeat(self._ts(91)), "stale")

    def test_boundary_stale_dead(self):
        self.assertEqual(classify_heartbeat(self._ts(299)), "stale")
        self.assertEqual(classify_heartbeat(self._ts(301)), "dead")

    def test_custom_thresholds(self):
        result = classify_heartbeat(
            self._ts(50),
            stale_threshold=30,
            dead_threshold=60,
        )
        self.assertEqual(result, "stale")


# ===========================================================================
# Test: Cleanup (§8.3 step 4)
# ===========================================================================

class TestCleanup(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = _setup_state_dir(self._tmp.name)
        _register_and_lease(self.state_dir)
        self.mgr = WorkerStateManager(self.state_dir, auto_init=False)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cleanup_removes_worker_state(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.cleanup("T1")
        self.assertIsNone(self.mgr.get("T1"))

    def test_cleanup_noop_for_missing(self):
        self.mgr.cleanup("T1")  # no error

    def test_cleanup_emits_event(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.cleanup("T1")
        with get_connection(self.state_dir) as conn:
            events = get_events(
                conn, entity_id="T1", event_type="worker_state_cleaned"
            )
        self.assertGreaterEqual(len(events), 1)


# ===========================================================================
# Test: T0-readable state surface (§6)
# ===========================================================================

class TestStateSurface(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = _setup_state_dir(self._tmp.name)
        _register_and_lease(self.state_dir)
        self.mgr = WorkerStateManager(self.state_dir, auto_init=False)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_returns_none_for_uninitialized(self):
        self.assertIsNone(self.mgr.get("T1"))

    def test_get_returns_state(self):
        self.mgr.initialize("T1", "d-001")
        result = self.mgr.get("T1")
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "initializing")

    def test_get_all_states(self):
        self.mgr.initialize("T1", "d-001")
        states = self.mgr.get_all_states()
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].terminal_id, "T1")

    def test_get_state_summary(self):
        self.mgr.initialize("T1", "d-001")
        summary = self.mgr.get_state_summary()
        self.assertIn("T1", summary["terminals"])
        t1 = summary["terminals"]["T1"]
        self.assertEqual(t1["worker_state"], "initializing")
        self.assertIn("heartbeat_classification", t1)
        self.assertIn("is_terminal", t1)
        self.assertFalse(t1["is_terminal"])

    def test_summary_shows_terminal_state(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "exited_clean")
        summary = self.mgr.get_state_summary()
        self.assertTrue(summary["terminals"]["T1"]["is_terminal"])


# ===========================================================================
# Test: Coordination events (§8.2)
# ===========================================================================

class TestCoordinationEvents(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = _setup_state_dir(self._tmp.name)
        _register_and_lease(self.state_dir)
        self.mgr = WorkerStateManager(self.state_dir, auto_init=False)

    def tearDown(self):
        self._tmp.cleanup()

    def test_initialize_emits_event(self):
        self.mgr.initialize("T1", "d-001")
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="T1", entity_type="worker")
        state_events = [e for e in events if e["event_type"] == "worker_state_changed"]
        self.assertGreaterEqual(len(state_events), 1)
        self.assertEqual(state_events[0]["to_state"], "initializing")

    def test_stall_detection_event_type(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "stalled")
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="T1", event_type="worker_stall_detected")
        self.assertGreaterEqual(len(events), 1)

    def test_exit_event_type(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "exited_bad")
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="T1", event_type="worker_exited")
        self.assertGreaterEqual(len(events), 1)

    def test_blocked_event_type(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "blocked", blocked_reason="test")
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="T1", event_type="worker_blocked")
        self.assertGreaterEqual(len(events), 1)

    def test_output_detected_event(self):
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.record_output("T1")
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="T1", event_type="worker_output_detected")
        self.assertGreaterEqual(len(events), 1)


# ===========================================================================
# Test: Full lifecycle scenarios
# ===========================================================================

class TestFullLifecycleScenarios(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = _setup_state_dir(self._tmp.name)
        _register_and_lease(self.state_dir)
        self.mgr = WorkerStateManager(self.state_dir, auto_init=False)

    def tearDown(self):
        self._tmp.cleanup()

    def test_happy_path_clean_exit(self):
        """initializing → working → exited_clean → cleanup"""
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working", reason="first output")
        self.mgr.record_output("T1")
        self.mgr.transition("T1", "exited_clean", reason="exit code 0")
        self.mgr.cleanup("T1")
        self.assertIsNone(self.mgr.get("T1"))

    def test_bad_exit_from_initializing(self):
        """initializing → exited_bad (crash during startup)"""
        self.mgr.initialize("T1", "d-001")
        result = self.mgr.transition("T1", "exited_bad", reason="crash during context load")
        self.assertEqual(result.state, "exited_bad")

    def test_stall_recovery_path(self):
        """working → stalled → working → exited_clean"""
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "stalled", reason="no output for 180s")
        self.mgr.transition("T1", "working", reason="output resumed")
        result = self.mgr.transition("T1", "exited_clean")
        self.assertEqual(result.state, "exited_clean")
        self.assertEqual(result.stall_count, 1)

    def test_blocked_recovery_path(self):
        """working → blocked → working → exited_clean"""
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "blocked", blocked_reason="MCP timeout")
        self.mgr.transition("T1", "working", reason="unblocked")
        result = self.mgr.transition("T1", "exited_clean")
        self.assertEqual(result.state, "exited_clean")

    def test_forced_termination_from_blocked(self):
        """working → blocked → resume_unsafe"""
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "blocked", blocked_reason="resource unavailable")
        result = self.mgr.transition("T1", "resume_unsafe", reason="operator kill")
        self.assertEqual(result.state, "resume_unsafe")

    def test_idle_between_tasks_stall(self):
        """working → idle_between_tasks → stalled → exited_bad"""
        self.mgr.initialize("T1", "d-001")
        self.mgr.transition("T1", "working")
        self.mgr.transition("T1", "idle_between_tasks")
        self.mgr.transition("T1", "stalled", reason="idle grace exceeded")
        result = self.mgr.transition("T1", "exited_bad", reason="dead heartbeat")
        self.assertEqual(result.state, "exited_bad")


# ---------------------------------------------------------------------------
# Helper for multi-dispatch tests
# ---------------------------------------------------------------------------

def _register_and_lease_second(state_dir: str):
    """Register a second dispatch and re-lease T1 (simulates new dispatch after release)."""
    with get_connection(state_dir) as conn:
        register_dispatch(conn, dispatch_id="d-002", terminal_id="T1", track="B")
        # Release current lease first
        row = conn.execute(
            "SELECT generation FROM terminal_leases WHERE terminal_id = 'T1'"
        ).fetchone()
        gen = row["generation"]
        conn.execute(
            """UPDATE terminal_leases
               SET state = 'idle', dispatch_id = NULL, expires_at = NULL,
                   released_at = datetime('now')
               WHERE terminal_id = 'T1'"""
        )
        acquire_lease(conn, terminal_id="T1", dispatch_id="d-002")
        conn.commit()


if __name__ == "__main__":
    unittest.main()
