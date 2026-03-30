#!/usr/bin/env python3
"""
Tests for PR-1: Headless Run Registry, Heartbeats, and Output Timestamps.

Gate: gate_pr1_headless_registry
Covers:
  - Headless run identity is durable and inspectable
  - Heartbeat and last-output timestamps are persisted
  - Runtime state is sufficient for operator inspection
  - Tests cover active, idle, and completed states

Contract reference: docs/HEADLESS_RUN_CONTRACT.md
"""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

# Add scripts/lib to path
SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from runtime_coordination import (
    init_schema,
    get_connection,
    register_dispatch,
    create_attempt,
)
from headless_run_registry import (
    HeadlessRunRegistry,
    HeadlessRun,
    InvalidRunStateError,
    InvalidRunTransitionError,
    RunNotFoundError,
    InvalidFailureClassError,
    RUN_STATES,
    TERMINAL_STATES,
    RUN_TRANSITIONS,
    FAILURE_CLASSES,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_OUTPUT_HANG_THRESHOLD,
)


class _RegistryTestCase(unittest.TestCase):
    """Base class that sets up a temp DB with all schema migrations."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.state_dir = Path(self._tmpdir) / "state"
        self.state_dir.mkdir()

        schemas_dir = Path(__file__).resolve().parent.parent / "schemas"
        init_schema(self.state_dir, schemas_dir / "runtime_coordination.sql")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _create_dispatch_and_attempt(self, dispatch_id="d-test-1"):
        """Helper: register a dispatch and create an attempt, return (dispatch_id, attempt_id)."""
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id=dispatch_id)
            attempt = create_attempt(
                conn,
                dispatch_id=dispatch_id,
                terminal_id="T2",
                attempt_number=1,
            )
            conn.commit()
        return dispatch_id, attempt["attempt_id"]

    def _create_run(self, registry=None, dispatch_id="d-test-1", **kwargs):
        """Helper: create a dispatch, attempt, and run. Returns (registry, run)."""
        if registry is None:
            registry = HeadlessRunRegistry(self.state_dir)
        did, aid = self._create_dispatch_and_attempt(dispatch_id)
        defaults = {
            "dispatch_id": did,
            "attempt_id": aid,
            "target_id": "headless_claude_cli_T2",
            "target_type": "headless_claude_cli",
            "task_class": "research_structured",
        }
        defaults.update(kwargs)
        run = registry.create_run(**defaults)
        return registry, run


# ============================================================================
# RUN CREATION TESTS
# ============================================================================

class TestRunCreation(_RegistryTestCase):

    def test_create_run_returns_init_state(self):
        """Run starts in 'init' state per Section 2.1."""
        reg, run = self._create_run()
        self.assertEqual(run.state, "init")
        self.assertFalse(run.is_terminal)
        self.assertFalse(run.is_running)

    def test_create_run_has_unique_id(self):
        """I-1: run_id is assigned exactly once and never reused."""
        reg = HeadlessRunRegistry(self.state_dir)
        _, run1 = self._create_run(reg, "d-1")
        _, run2 = self._create_run(reg, "d-2")
        self.assertNotEqual(run1.run_id, run2.run_id)

    def test_create_run_persists_identity_fields(self):
        """Section 1.2: all identity fields are persisted."""
        reg, run = self._create_run(
            dispatch_id="d-identity-1",
            terminal_id="T2",
        )
        self.assertEqual(run.dispatch_id, "d-identity-1")
        self.assertEqual(run.target_id, "headless_claude_cli_T2")
        self.assertEqual(run.target_type, "headless_claude_cli")
        self.assertEqual(run.task_class, "research_structured")
        self.assertEqual(run.terminal_id, "T2")
        self.assertIsNotNone(run.started_at)

    def test_create_run_links_dispatch_and_attempt(self):
        """I-2: run links to exactly one dispatch_id and attempt_id."""
        reg, run = self._create_run()
        self.assertIsNotNone(run.dispatch_id)
        self.assertIsNotNone(run.attempt_id)

    def test_create_run_emits_coordination_event(self):
        """Section 2.3: each transition emits a coordination_event."""
        reg, run = self._create_run()
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE entity_id = ? AND event_type = 'headless_run_transition'",
                (run.run_id,),
            ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["from_state"])
        self.assertEqual(events[0]["to_state"], "init")

    def test_create_run_with_metadata(self):
        reg, run = self._create_run(
            dispatch_id="d-meta-1",
            metadata={"custom_key": "custom_value"},
        )
        parsed = json.loads(run.metadata_json)
        self.assertEqual(parsed["custom_key"], "custom_value")

    def test_run_is_retrievable_by_id(self):
        """Operator can inspect run by run_id."""
        reg, run = self._create_run()
        fetched = reg.get(run.run_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.run_id, run.run_id)
        self.assertEqual(fetched.dispatch_id, run.dispatch_id)

    def test_get_nonexistent_returns_none(self):
        reg = HeadlessRunRegistry(self.state_dir)
        self.assertIsNone(reg.get("nonexistent"))

    def test_get_or_raise_nonexistent(self):
        reg = HeadlessRunRegistry(self.state_dir)
        with self.assertRaises(RunNotFoundError):
            reg.get_or_raise("nonexistent")


# ============================================================================
# STATE TRANSITION TESTS
# ============================================================================

class TestRunTransitions(_RegistryTestCase):

    def test_init_to_running(self):
        """init -> running sets subprocess_started_at, heartbeat_at, last_output_at."""
        reg, run = self._create_run()
        updated = reg.transition(
            run.run_id, "running",
            pid=12345, pgid=12345,
            reason="subprocess spawned",
        )
        self.assertEqual(updated.state, "running")
        self.assertTrue(updated.is_running)
        self.assertEqual(updated.pid, 12345)
        self.assertEqual(updated.pgid, 12345)
        self.assertIsNotNone(updated.subprocess_started_at)
        self.assertIsNotNone(updated.heartbeat_at)
        self.assertIsNotNone(updated.last_output_at)

    def test_running_to_completing(self):
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=100)
        updated = reg.transition(
            run.run_id, "completing",
            exit_code=0,
            reason="exit code 0",
        )
        self.assertEqual(updated.state, "completing")
        self.assertEqual(updated.exit_code, 0)

    def test_completing_to_succeeded(self):
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=100)
        reg.transition(run.run_id, "completing", exit_code=0)
        updated = reg.transition(
            run.run_id, "succeeded",
            duration_seconds=42.5,
            log_artifact_path="/logs/run.txt",
            output_artifact_path="/output/result.txt",
            receipt_id="rcpt-001",
        )
        self.assertEqual(updated.state, "succeeded")
        self.assertTrue(updated.is_terminal)
        self.assertIsNotNone(updated.completed_at)
        self.assertEqual(updated.duration_seconds, 42.5)
        self.assertEqual(updated.log_artifact_path, "/logs/run.txt")
        self.assertEqual(updated.output_artifact_path, "/output/result.txt")
        self.assertEqual(updated.receipt_id, "rcpt-001")

    def test_running_to_failing(self):
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=100)
        updated = reg.transition(
            run.run_id, "failing",
            exit_code=1,
            reason="non-zero exit",
        )
        self.assertEqual(updated.state, "failing")
        self.assertEqual(updated.exit_code, 1)

    def test_failing_to_failed(self):
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=100)
        reg.transition(run.run_id, "failing", exit_code=1)
        updated = reg.transition(
            run.run_id, "failed",
            failure_class="TOOL_FAIL",
            duration_seconds=10.2,
            log_artifact_path="/logs/err.txt",
        )
        self.assertEqual(updated.state, "failed")
        self.assertTrue(updated.is_terminal)
        self.assertEqual(updated.failure_class, "TOOL_FAIL")
        self.assertIsNotNone(updated.completed_at)

    def test_full_success_lifecycle(self):
        """Full lifecycle: init -> running -> completing -> succeeded."""
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=200, pgid=200)
        reg.transition(run.run_id, "completing", exit_code=0)
        final = reg.transition(
            run.run_id, "succeeded",
            duration_seconds=30.0,
            receipt_id="rcpt-ok",
        )
        self.assertEqual(final.state, "succeeded")
        self.assertTrue(final.is_terminal)

        # Verify all events were emitted (I-4)
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE entity_id = ? ORDER BY occurred_at",
                (run.run_id,),
            ).fetchall()
        states = [(e["from_state"], e["to_state"]) for e in events]
        self.assertEqual(states, [
            (None, "init"),
            ("init", "running"),
            ("running", "completing"),
            ("completing", "succeeded"),
        ])

    def test_full_failure_lifecycle(self):
        """Full lifecycle: init -> running -> failing -> failed."""
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=300)
        reg.transition(run.run_id, "failing", exit_code=-9)
        final = reg.transition(
            run.run_id, "failed",
            failure_class="TIMEOUT",
            duration_seconds=600.0,
        )
        self.assertEqual(final.state, "failed")
        self.assertEqual(final.failure_class, "TIMEOUT")

    def test_no_backward_transitions(self):
        """Section 2.2: no backward transitions."""
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=100)

        with self.assertRaises(InvalidRunTransitionError):
            reg.transition(run.run_id, "init")

    def test_terminal_states_block_further_transitions(self):
        """Terminal states allow no further transitions."""
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=100)
        reg.transition(run.run_id, "completing", exit_code=0)
        reg.transition(run.run_id, "succeeded")

        with self.assertRaises(InvalidRunTransitionError):
            reg.transition(run.run_id, "running")

    def test_invalid_skip_transition(self):
        """Cannot skip states (e.g., init -> completing)."""
        reg, run = self._create_run()
        with self.assertRaises(InvalidRunTransitionError):
            reg.transition(run.run_id, "completing")

    def test_invalid_failure_class_rejected(self):
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=100)
        reg.transition(run.run_id, "failing", exit_code=1)
        with self.assertRaises(InvalidFailureClassError):
            reg.transition(run.run_id, "failed", failure_class="NOT_A_CLASS")

    def test_transition_nonexistent_run(self):
        reg = HeadlessRunRegistry(self.state_dir)
        with self.assertRaises(RunNotFoundError):
            reg.transition("nonexistent", "running")

    def test_all_failure_classes_accepted(self):
        """All 8 failure classes from Section 4.1 are valid."""
        for fc in FAILURE_CLASSES:
            reg, run = self._create_run(dispatch_id=f"d-fc-{fc}")
            reg.transition(run.run_id, "running", pid=100)
            reg.transition(run.run_id, "failing", exit_code=1)
            updated = reg.transition(run.run_id, "failed", failure_class=fc)
            self.assertEqual(updated.failure_class, fc)


# ============================================================================
# HEARTBEAT TESTS
# ============================================================================

class TestHeartbeat(_RegistryTestCase):

    def test_heartbeat_updates_timestamp(self):
        """Section 3.2: heartbeat updates heartbeat_at."""
        reg, run = self._create_run()
        running = reg.transition(run.run_id, "running", pid=100)
        original_hb = running.heartbeat_at

        time.sleep(0.05)
        updated = reg.update_heartbeat(run.run_id)
        self.assertGreater(updated.heartbeat_at, original_hb)

    def test_heartbeat_only_while_running(self):
        """Heartbeat rejected if not in running state."""
        reg, run = self._create_run()
        with self.assertRaises(InvalidRunStateError):
            reg.update_heartbeat(run.run_id)

    def test_heartbeat_rejected_after_terminal(self):
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=100)
        reg.transition(run.run_id, "completing", exit_code=0)
        reg.transition(run.run_id, "succeeded")
        with self.assertRaises(InvalidRunStateError):
            reg.update_heartbeat(run.run_id)

    def test_heartbeat_nonexistent_run(self):
        reg = HeadlessRunRegistry(self.state_dir)
        with self.assertRaises(RunNotFoundError):
            reg.update_heartbeat("nonexistent")


# ============================================================================
# OUTPUT TIMESTAMP TESTS
# ============================================================================

class TestOutputTimestamp(_RegistryTestCase):

    def test_output_updates_timestamp(self):
        """Section 3.3: last_output_at updates on output."""
        reg, run = self._create_run()
        running = reg.transition(run.run_id, "running", pid=100)
        original_out = running.last_output_at

        time.sleep(0.05)
        updated = reg.update_last_output(run.run_id)
        self.assertGreater(updated.last_output_at, original_out)

    def test_output_only_while_running(self):
        reg, run = self._create_run()
        with self.assertRaises(InvalidRunStateError):
            reg.update_last_output(run.run_id)

    def test_output_nonexistent_run(self):
        reg = HeadlessRunRegistry(self.state_dir)
        with self.assertRaises(RunNotFoundError):
            reg.update_last_output("nonexistent")


# ============================================================================
# QUERY TESTS — OPERATOR INSPECTION (O-1 through O-10)
# ============================================================================

class TestActiveIdleCompletedQueries(_RegistryTestCase):
    """Gate criterion: tests cover active, idle, and completed states."""

    def test_list_active_returns_running_runs(self):
        """O-1: list active headless runs (state = running)."""
        reg, run1 = self._create_run(dispatch_id="d-active-1")
        reg.transition(run1.run_id, "running", pid=100)

        _, run2 = self._create_run(reg, dispatch_id="d-active-2")
        reg.transition(run2.run_id, "running", pid=200)

        # Idle run (init state)
        _, run3 = self._create_run(reg, dispatch_id="d-idle-1")

        active = reg.list_active()
        active_ids = {r.run_id for r in active}
        self.assertIn(run1.run_id, active_ids)
        self.assertIn(run2.run_id, active_ids)
        self.assertNotIn(run3.run_id, active_ids)

    def test_list_by_state_init(self):
        """Idle runs are in 'init' state."""
        reg, run = self._create_run()
        idle_runs = reg.list_by_state("init")
        self.assertEqual(len(idle_runs), 1)
        self.assertEqual(idle_runs[0].run_id, run.run_id)

    def test_list_by_state_succeeded(self):
        """Completed runs are in 'succeeded' state."""
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=100)
        reg.transition(run.run_id, "completing", exit_code=0)
        reg.transition(run.run_id, "succeeded")

        completed = reg.list_by_state("succeeded")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].state, "succeeded")

    def test_list_by_state_failed(self):
        reg, run = self._create_run()
        reg.transition(run.run_id, "running", pid=100)
        reg.transition(run.run_id, "failing", exit_code=1)
        reg.transition(run.run_id, "failed", failure_class="TOOL_FAIL")

        failed = reg.list_by_state("failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].failure_class, "TOOL_FAIL")

    def test_list_by_dispatch(self):
        """O-8: trace runs back to dispatch."""
        reg, run1 = self._create_run(dispatch_id="d-trace-1")
        _, run2 = self._create_run(reg, dispatch_id="d-trace-2")

        runs = reg.list_by_dispatch("d-trace-1")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].run_id, run1.run_id)

    def test_list_recent(self):
        reg, run1 = self._create_run(dispatch_id="d-recent-1")
        _, run2 = self._create_run(reg, dispatch_id="d-recent-2")
        _, run3 = self._create_run(reg, dispatch_id="d-recent-3")

        recent = reg.list_recent(limit=2)
        self.assertEqual(len(recent), 2)

    def test_list_by_state_validates_input(self):
        reg = HeadlessRunRegistry(self.state_dir)
        with self.assertRaises(InvalidRunStateError):
            reg.list_by_state("bogus")


# ============================================================================
# STALENESS AND HANG DETECTION TESTS
# ============================================================================

class TestStalenessDetection(_RegistryTestCase):

    def test_stale_detection_via_query(self):
        """O-4: detect stale runs via heartbeat_at threshold (Section 3.2)."""
        reg = HeadlessRunRegistry(self.state_dir, heartbeat_interval=1)
        _, run = self._create_run(reg)
        reg.transition(run.run_id, "running", pid=100)

        # Manually backdate heartbeat_at to simulate staleness
        stale_time = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE headless_runs SET heartbeat_at = ? WHERE run_id = ?",
                (stale_time, run.run_id),
            )
            conn.commit()

        stale = reg.list_stale()
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].run_id, run.run_id)

    def test_fresh_run_not_stale(self):
        """A run with recent heartbeat is not stale."""
        reg = HeadlessRunRegistry(self.state_dir, heartbeat_interval=30)
        _, run = self._create_run(reg)
        reg.transition(run.run_id, "running", pid=100)
        reg.update_heartbeat(run.run_id)

        stale = reg.list_stale()
        self.assertEqual(len(stale), 0)

    def test_completed_run_not_stale(self):
        """Terminal runs are never stale."""
        reg = HeadlessRunRegistry(self.state_dir, heartbeat_interval=1)
        _, run = self._create_run(reg)
        reg.transition(run.run_id, "running", pid=100)
        reg.transition(run.run_id, "completing", exit_code=0)
        reg.transition(run.run_id, "succeeded")

        stale = reg.list_stale()
        self.assertEqual(len(stale), 0)


class TestHangDetection(_RegistryTestCase):

    def test_hung_detection_via_query(self):
        """O-3: detect hung runs via last_output_at threshold (Section 3.3)."""
        reg = HeadlessRunRegistry(self.state_dir, output_hang_threshold=1)
        _, run = self._create_run(reg)
        reg.transition(run.run_id, "running", pid=100)

        # Backdate last_output_at to simulate no-output hang
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE headless_runs SET last_output_at = ? WHERE run_id = ?",
                (old_time, run.run_id),
            )
            conn.commit()

        hung = reg.list_hung()
        self.assertEqual(len(hung), 1)
        self.assertEqual(hung[0].run_id, run.run_id)

    def test_active_output_not_hung(self):
        """A run with recent output is not hung."""
        reg = HeadlessRunRegistry(self.state_dir, output_hang_threshold=120)
        _, run = self._create_run(reg)
        reg.transition(run.run_id, "running", pid=100)
        reg.update_last_output(run.run_id)

        hung = reg.list_hung()
        self.assertEqual(len(hung), 0)


# ============================================================================
# OPERATOR INSPECTION — PROCESS CONTROL (O-10)
# ============================================================================

class TestProcessControl(_RegistryTestCase):

    def test_pid_and_pgid_persisted(self):
        """O-10: PID/PGID in run state for operator signal delivery."""
        reg, run = self._create_run()
        updated = reg.transition(run.run_id, "running", pid=54321, pgid=54320)
        self.assertEqual(updated.pid, 54321)
        self.assertEqual(updated.pgid, 54320)

        fetched = reg.get(run.run_id)
        self.assertEqual(fetched.pid, 54321)
        self.assertEqual(fetched.pgid, 54320)


# ============================================================================
# HEADLESS RUN DATA CLASS PROPERTIES
# ============================================================================

class TestHeadlessRunProperties(unittest.TestCase):

    def test_is_terminal(self):
        run = HeadlessRun(
            run_id="r1", dispatch_id="d1", attempt_id="a1",
            target_id="t1", target_type="headless_claude_cli",
            task_class="research_structured", state="succeeded",
        )
        self.assertTrue(run.is_terminal)

    def test_is_not_terminal(self):
        run = HeadlessRun(
            run_id="r1", dispatch_id="d1", attempt_id="a1",
            target_id="t1", target_type="headless_claude_cli",
            task_class="research_structured", state="running",
        )
        self.assertFalse(run.is_terminal)
        self.assertTrue(run.is_running)


# ============================================================================
# SCHEMA MIGRATION TEST
# ============================================================================

class TestSchemaMigration(_RegistryTestCase):

    def test_headless_runs_table_exists(self):
        """v8 migration creates headless_runs table."""
        with get_connection(self.state_dir) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='headless_runs'"
            ).fetchall()
        self.assertEqual(len(tables), 1)

    def test_schema_version_recorded(self):
        with get_connection(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM runtime_schema_version WHERE version = 8"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("run registry", row["description"])

    def test_indexes_exist(self):
        with get_connection(self.state_dir) as conn:
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_headless_run_%'"
            ).fetchall()
        index_names = {i["name"] for i in indexes}
        self.assertIn("idx_headless_run_state", index_names)
        self.assertIn("idx_headless_run_dispatch", index_names)
        self.assertIn("idx_headless_run_target", index_names)
        self.assertIn("idx_headless_run_heartbeat", index_names)


if __name__ == "__main__":
    unittest.main()
