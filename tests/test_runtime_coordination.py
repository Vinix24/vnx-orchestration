#!/usr/bin/env python3
"""
Tests for runtime_coordination.py (PR-0)

Tests cover:
  - Schema initialization and idempotency
  - Dispatch registration and state transitions
  - Attempt creation and updates
  - Lease acquire, renew, release, expire, recover
  - Coordination event appends
  - Invalid state and transition validation
  - project_terminal_state() projection
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
    DISPATCH_STATES,
    DISPATCH_TRANSITIONS,
    LEASE_STATES,
    LEASE_TRANSITIONS,
    InvalidStateError,
    InvalidTransitionError,
    acquire_lease,
    create_attempt,
    expire_lease,
    get_connection,
    get_dispatch,
    get_events,
    get_lease,
    increment_attempt_count,
    init_schema,
    project_terminal_state,
    recover_lease,
    register_dispatch,
    release_lease,
    renew_lease,
    transition_dispatch,
    update_attempt,
    validate_dispatch_state,
    validate_dispatch_transition,
    validate_lease_state,
    validate_lease_transition,
)


class TestStateEnumerations(unittest.TestCase):
    def test_dispatch_states_complete(self):
        expected = {
            "queued", "claimed", "delivering", "accepted", "running",
            "completed", "timed_out", "failed_delivery", "expired", "recovered",
            "dead_letter",
        }
        self.assertEqual(DISPATCH_STATES, expected)

    def test_lease_states_complete(self):
        expected = {"idle", "leased", "expired", "recovering", "released"}
        self.assertEqual(LEASE_STATES, expected)

    def test_dispatch_transitions_coverage(self):
        # Every state must appear as a key in transitions
        for state in DISPATCH_STATES:
            self.assertIn(state, DISPATCH_TRANSITIONS, f"No transition entry for {state!r}")

    def test_lease_transitions_coverage(self):
        for state in LEASE_STATES:
            self.assertIn(state, LEASE_TRANSITIONS, f"No transition entry for {state!r}")

    def test_terminal_dispatch_states_have_no_outgoing(self):
        # completed and expired are terminal states
        self.assertEqual(DISPATCH_TRANSITIONS["completed"], frozenset())
        self.assertEqual(DISPATCH_TRANSITIONS["expired"], frozenset())

    def test_validate_dispatch_state_valid(self):
        for state in DISPATCH_STATES:
            validate_dispatch_state(state)  # must not raise

    def test_validate_dispatch_state_invalid(self):
        with self.assertRaises(InvalidStateError):
            validate_dispatch_state("banana")

    def test_validate_lease_state_valid(self):
        for state in LEASE_STATES:
            validate_lease_state(state)

    def test_validate_lease_state_invalid(self):
        with self.assertRaises(InvalidStateError):
            validate_lease_state("working")

    def test_validate_dispatch_transition_valid(self):
        validate_dispatch_transition("queued", "claimed")
        validate_dispatch_transition("delivering", "accepted")
        validate_dispatch_transition("running", "completed")

    def test_validate_dispatch_transition_invalid(self):
        with self.assertRaises(InvalidTransitionError):
            validate_dispatch_transition("completed", "running")
        with self.assertRaises(InvalidTransitionError):
            validate_dispatch_transition("queued", "completed")

    def test_validate_lease_transition_valid(self):
        validate_lease_transition("idle", "leased")
        validate_lease_transition("leased", "expired")
        validate_lease_transition("expired", "recovering")

    def test_validate_lease_transition_invalid(self):
        with self.assertRaises(InvalidTransitionError):
            validate_lease_transition("idle", "expired")
        with self.assertRaises(InvalidTransitionError):
            validate_lease_transition("leased", "idle")


class _DbTestCase(unittest.TestCase):
    """Base class: creates a temp dir and initializes schema before each test."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self._tmpdir.name
        init_schema(self.state_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def conn(self):
        # Returns a context manager — callers must use `with self.conn() as c:`
        return get_connection(self.state_dir)


class TestSchemaInit(_DbTestCase):
    def test_tables_exist(self):
        with self.conn() as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        required = {
            "runtime_schema_version", "dispatches", "dispatch_attempts",
            "terminal_leases", "coordination_events",
        }
        self.assertTrue(required.issubset(tables))

    def test_idempotent_reinit(self):
        # Second init must not raise or corrupt data
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state) VALUES ('d-idem', 'queued')"
            )
            conn.commit()

        init_schema(self.state_dir)  # re-run

        with self.conn() as conn:
            row = conn.execute(
                "SELECT dispatch_id FROM dispatches WHERE dispatch_id = 'd-idem'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_terminal_seed_rows(self):
        with self.conn() as conn:
            rows = conn.execute("SELECT terminal_id FROM terminal_leases ORDER BY terminal_id").fetchall()
        ids = [r[0] for r in rows]
        self.assertIn("T1", ids)
        self.assertIn("T2", ids)
        self.assertIn("T3", ids)

    def test_schema_version_record(self):
        with self.conn() as conn:
            row = conn.execute(
                "SELECT version FROM runtime_schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row[0], 1)


class TestDispatchRegistration(_DbTestCase):
    def test_register_creates_queued_dispatch(self):
        with self.conn() as conn:
            rec = register_dispatch(conn, dispatch_id="d-001", terminal_id="T1", track="A")
            conn.commit()
        self.assertEqual(rec["dispatch_id"], "d-001")
        self.assertEqual(rec["state"], "queued")
        self.assertEqual(rec["terminal_id"], "T1")

    def test_register_idempotent(self):
        with self.conn() as conn:
            r1 = register_dispatch(conn, dispatch_id="d-001")
            conn.commit()
            r2 = register_dispatch(conn, dispatch_id="d-001", terminal_id="T2")
            conn.commit()
        # Second call must not change terminal_id (bundle immutability G-R6)
        self.assertEqual(r2["terminal_id"], r1["terminal_id"])

    def test_register_appends_event(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-002")
            conn.commit()
            events = get_events(conn, entity_id="d-002")
        self.assertTrue(any(e["event_type"] == "dispatch_queued" for e in events))

    def test_get_dispatch_returns_none_for_missing(self):
        with self.conn() as conn:
            result = get_dispatch(conn, "no-such-dispatch")
        self.assertIsNone(result)


class TestDispatchTransitions(_DbTestCase):
    def _setup_dispatch(self, dispatch_id: str = "d-t1") -> None:
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id=dispatch_id, terminal_id="T1")
            conn.commit()

    def test_valid_transition_queued_to_claimed(self):
        self._setup_dispatch()
        with self.conn() as conn:
            rec = transition_dispatch(conn, dispatch_id="d-t1", to_state="claimed")
            conn.commit()
        self.assertEqual(rec["state"], "claimed")

    def test_invalid_transition_raises(self):
        self._setup_dispatch()
        with self.conn() as conn:
            with self.assertRaises(InvalidTransitionError):
                transition_dispatch(conn, dispatch_id="d-t1", to_state="completed")

    def test_transition_appends_event(self):
        self._setup_dispatch()
        with self.conn() as conn:
            transition_dispatch(conn, dispatch_id="d-t1", to_state="claimed")
            conn.commit()
            events = get_events(conn, entity_id="d-t1")
        event_types = [e["event_type"] for e in events]
        self.assertIn("dispatch_claimed", event_types)

    def test_transition_missing_dispatch_raises(self):
        with self.conn() as conn:
            with self.assertRaises(KeyError):
                transition_dispatch(conn, dispatch_id="ghost", to_state="claimed")

    def test_full_success_path(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-full", terminal_id="T1")
            transition_dispatch(conn, dispatch_id="d-full", to_state="claimed")
            transition_dispatch(conn, dispatch_id="d-full", to_state="delivering")
            transition_dispatch(conn, dispatch_id="d-full", to_state="accepted")
            transition_dispatch(conn, dispatch_id="d-full", to_state="running")
            rec = transition_dispatch(conn, dispatch_id="d-full", to_state="completed")
            conn.commit()
        self.assertEqual(rec["state"], "completed")

    def test_attempt_count_increment(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-cnt")
            count = increment_attempt_count(conn, "d-cnt")
            self.assertEqual(count, 1)
            count = increment_attempt_count(conn, "d-cnt")
            self.assertEqual(count, 2)
            conn.commit()


class TestDispatchAttempts(_DbTestCase):
    def setUp(self):
        super().setUp()
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-att", terminal_id="T2")
            conn.commit()

    def test_create_attempt(self):
        with self.conn() as conn:
            att = create_attempt(
                conn, dispatch_id="d-att", terminal_id="T2", attempt_number=1
            )
            conn.commit()
        self.assertEqual(att["dispatch_id"], "d-att")
        self.assertEqual(att["attempt_number"], 1)
        self.assertEqual(att["state"], "pending")

    def test_update_attempt_success(self):
        with self.conn() as conn:
            att = create_attempt(conn, dispatch_id="d-att", terminal_id="T2", attempt_number=1)
            conn.commit()
            updated = update_attempt(conn, attempt_id=att["attempt_id"], state="succeeded")
            conn.commit()
        self.assertEqual(updated["state"], "succeeded")

    def test_update_attempt_failure_records_reason(self):
        with self.conn() as conn:
            att = create_attempt(conn, dispatch_id="d-att", terminal_id="T2", attempt_number=1)
            conn.commit()
            updated = update_attempt(
                conn, attempt_id=att["attempt_id"],
                state="failed", failure_reason="tmux pane exited"
            )
            conn.commit()
        self.assertEqual(updated["failure_reason"], "tmux pane exited")

    def test_attempt_events_appended(self):
        with self.conn() as conn:
            att = create_attempt(conn, dispatch_id="d-att", terminal_id="T2", attempt_number=1)
            conn.commit()
            events = get_events(conn, entity_id=att["attempt_id"])
        self.assertTrue(len(events) >= 1)


class TestLeaseOperations(_DbTestCase):
    def test_acquire_lease(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-lease", terminal_id="T1")
            lease = acquire_lease(conn, terminal_id="T1", dispatch_id="d-lease")
            conn.commit()
        self.assertEqual(lease["state"], "leased")
        self.assertEqual(lease["dispatch_id"], "d-lease")
        self.assertGreater(lease["generation"], 1)

    def test_acquire_lease_appends_event(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-ev", terminal_id="T1")
            acquire_lease(conn, terminal_id="T1", dispatch_id="d-ev")
            conn.commit()
            events = get_events(conn, entity_id="T1")
        self.assertTrue(any(e["event_type"] == "lease_acquired" for e in events))

    def test_acquire_lease_on_busy_terminal_raises(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-a", terminal_id="T1")
            register_dispatch(conn, dispatch_id="d-b", terminal_id="T1")
            acquire_lease(conn, terminal_id="T1", dispatch_id="d-a")
            conn.commit()
            with self.assertRaises(InvalidTransitionError):
                acquire_lease(conn, terminal_id="T1", dispatch_id="d-b")

    def test_renew_lease(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-renew", terminal_id="T2")
            lease = acquire_lease(conn, terminal_id="T2", dispatch_id="d-renew")
            conn.commit()
            gen = lease["generation"]
            renewed = renew_lease(conn, terminal_id="T2", generation=gen, lease_seconds=300)
            conn.commit()
        self.assertIsNotNone(renewed["expires_at"])

    def test_renew_with_stale_generation_raises(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-stale", terminal_id="T3")
            acquire_lease(conn, terminal_id="T3", dispatch_id="d-stale")
            conn.commit()
            with self.assertRaises(ValueError):
                renew_lease(conn, terminal_id="T3", generation=0)

    def test_release_lease(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-rel", terminal_id="T1")
            lease = acquire_lease(conn, terminal_id="T1", dispatch_id="d-rel")
            conn.commit()
            gen = lease["generation"]
            released = release_lease(conn, terminal_id="T1", generation=gen)
            conn.commit()
        self.assertEqual(released["state"], "idle")
        self.assertIsNone(released["dispatch_id"])

    def test_release_with_stale_generation_raises(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-sg", terminal_id="T1")
            acquire_lease(conn, terminal_id="T1", dispatch_id="d-sg")
            conn.commit()
            with self.assertRaises(ValueError):
                release_lease(conn, terminal_id="T1", generation=0)

    def test_expire_lease(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-exp", terminal_id="T2")
            acquire_lease(conn, terminal_id="T2", dispatch_id="d-exp")
            conn.commit()
            expired = expire_lease(conn, terminal_id="T2")
            conn.commit()
        self.assertEqual(expired["state"], "expired")

    def test_recover_lease(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-rec", terminal_id="T3")
            acquire_lease(conn, terminal_id="T3", dispatch_id="d-rec")
            conn.commit()
            expire_lease(conn, terminal_id="T3")
            conn.commit()
            recovered = recover_lease(conn, terminal_id="T3")
            conn.commit()
        self.assertEqual(recovered["state"], "idle")
        self.assertIsNone(recovered["dispatch_id"])

    def test_recover_appends_events(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-rev", terminal_id="T1")
            acquire_lease(conn, terminal_id="T1", dispatch_id="d-rev")
            conn.commit()
            expire_lease(conn, terminal_id="T1")
            conn.commit()
            recover_lease(conn, terminal_id="T1")
            conn.commit()
            events = get_events(conn, entity_id="T1")
        event_types = {e["event_type"] for e in events}
        self.assertIn("lease_expired", event_types)
        self.assertIn("lease_recovering", event_types)
        self.assertIn("lease_recovered", event_types)

    def test_get_lease_returns_none_for_unknown(self):
        with self.conn() as conn:
            result = get_lease(conn, "T99")
        self.assertIsNone(result)


class TestCoordinationEvents(_DbTestCase):
    def test_events_are_append_only(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-ev1")
            register_dispatch(conn, dispatch_id="d-ev2")
            conn.commit()
            all_events = get_events(conn)
        # At minimum 2 queued events
        self.assertGreaterEqual(len(all_events), 2)

    def test_get_events_filter_by_entity(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-f1")
            register_dispatch(conn, dispatch_id="d-f2")
            conn.commit()
            events = get_events(conn, entity_id="d-f1")
        dispatch_ids = {e["entity_id"] for e in events}
        self.assertIn("d-f1", dispatch_ids)
        self.assertNotIn("d-f2", dispatch_ids)

    def test_events_have_unique_ids(self):
        with self.conn() as conn:
            for i in range(5):
                register_dispatch(conn, dispatch_id=f"d-uid-{i}")
            conn.commit()
            events = get_events(conn, limit=20)
        event_ids = [e["event_id"] for e in events]
        self.assertEqual(len(event_ids), len(set(event_ids)))


class TestProjectTerminalState(_DbTestCase):
    def test_projection_includes_all_terminals(self):
        with self.conn() as conn:
            projection = project_terminal_state(conn)
        terminals = projection["terminals"]
        self.assertIn("T1", terminals)
        self.assertIn("T2", terminals)
        self.assertIn("T3", terminals)

    def test_projection_idle_terminal(self):
        with self.conn() as conn:
            projection = project_terminal_state(conn)
        self.assertEqual(projection["terminals"]["T1"]["status"], "idle")

    def test_projection_leased_terminal_shows_working(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-proj", terminal_id="T2")
            acquire_lease(conn, terminal_id="T2", dispatch_id="d-proj")
            conn.commit()
            projection = project_terminal_state(conn)
        self.assertEqual(projection["terminals"]["T2"]["status"], "working")
        self.assertEqual(projection["terminals"]["T2"]["claimed_by"], "d-proj")

    def test_projection_schema_version(self):
        with self.conn() as conn:
            projection = project_terminal_state(conn)
        self.assertEqual(projection["schema_version"], 1)

    def test_projection_expired_shows_recovering_status(self):
        with self.conn() as conn:
            register_dispatch(conn, dispatch_id="d-expiry", terminal_id="T3")
            acquire_lease(conn, terminal_id="T3", dispatch_id="d-expiry")
            conn.commit()
            expire_lease(conn, terminal_id="T3")
            conn.commit()
            projection = project_terminal_state(conn)
        self.assertEqual(projection["terminals"]["T3"]["status"], "recovering")


if __name__ == "__main__":
    unittest.main(verbosity=2)
