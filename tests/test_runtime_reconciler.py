#!/usr/bin/env python3
"""
Tests for runtime_reconciler.py (PR-4)

Coverage:
  - Expired lease detection and transition
  - Auto-recovery of expired leases to idle
  - Orphaned attempt detection (pending/delivering past threshold)
  - Stuck dispatch detection and timeout
  - Over-attempted dispatch expiry
  - Auto-recovery of dispatches (timed_out/failed_delivery)
  - Flagging dispatches for operator review
  - Idempotency: repeated runs produce no duplicate actions
  - No silent deletion: all transitions leave audit trail
  - Dry-run mode: detection without modification
  - Summary output and serialization

Gate: gate_pr4_reconciliation_safe_recovery
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (
    create_attempt,
    get_connection,
    get_dispatch,
    get_events,
    get_lease,
    init_schema,
    register_dispatch,
    transition_dispatch,
    update_attempt,
)
from lease_manager import LeaseManager
from runtime_reconciler import (
    ReconcilerConfig,
    ReconciliationResult,
    RuntimeReconciler,
    load_reconciler,
)


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------

class _ReconcilerTestCase(unittest.TestCase):
    """Creates temp state dir with initialized schema."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self._tmpdir.name
        init_schema(self.state_dir)
        self.lease_mgr = LeaseManager(self.state_dir, auto_init=False)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _reg(self, dispatch_id: str, terminal_id: str = "T1", **kwargs) -> dict:
        with get_connection(self.state_dir) as conn:
            row = register_dispatch(
                conn, dispatch_id=dispatch_id, terminal_id=terminal_id, **kwargs
            )
            conn.commit()
        return row

    def _acquire(self, terminal_id: str, dispatch_id: str, lease_seconds: int = 600):
        self._reg(dispatch_id, terminal_id)
        return self.lease_mgr.acquire(terminal_id, dispatch_id, lease_seconds=lease_seconds)

    def _force_expires_at(self, terminal_id: str, seconds_ago: int):
        """Force-set expires_at to a past timestamp."""
        past = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE terminal_leases SET expires_at = ? WHERE terminal_id = ?",
                (past, terminal_id),
            )
            conn.commit()

    def _force_updated_at(self, dispatch_id: str, seconds_ago: int):
        """Force-set updated_at to a past timestamp."""
        past = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE dispatches SET updated_at = ? WHERE dispatch_id = ?",
                (past, dispatch_id),
            )
            conn.commit()

    def _force_attempt_started_at(self, attempt_id: str, seconds_ago: int):
        """Force-set started_at to a past timestamp."""
        past = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE dispatch_attempts SET started_at = ? WHERE attempt_id = ?",
                (past, attempt_id),
            )
            conn.commit()

    def _create_attempt(self, dispatch_id: str, terminal_id: str, attempt_number: int = 1) -> dict:
        with get_connection(self.state_dir) as conn:
            row = create_attempt(
                conn,
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                attempt_number=attempt_number,
            )
            conn.commit()
        return row

    def _transition(self, dispatch_id: str, to_state: str, **kwargs):
        with get_connection(self.state_dir) as conn:
            row = transition_dispatch(conn, dispatch_id=dispatch_id, to_state=to_state, **kwargs)
            conn.commit()
        return row

    def _set_attempt_count(self, dispatch_id: str, count: int):
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE dispatches SET attempt_count = ? WHERE dispatch_id = ?",
                (count, dispatch_id),
            )
            conn.commit()

    def _get_dispatch(self, dispatch_id: str) -> dict:
        with get_connection(self.state_dir) as conn:
            return get_dispatch(conn, dispatch_id)

    def _get_lease(self, terminal_id: str) -> dict:
        with get_connection(self.state_dir) as conn:
            return get_lease(conn, terminal_id)

    def _events(self, entity_id: str, entity_type: str = "lease", event_type: str | None = None) -> list:
        with get_connection(self.state_dir) as conn:
            return get_events(conn, entity_id=entity_id, entity_type=entity_type, event_type=event_type)


# ---------------------------------------------------------------------------
# Lease expiry tests
# ---------------------------------------------------------------------------

class TestLeaseExpiry(_ReconcilerTestCase):

    def test_expired_lease_detected_and_transitioned(self):
        """Reconciliation detects expired leases using canonical state."""
        self._acquire("T1", "d-001", lease_seconds=600)
        self._force_expires_at("T1", seconds_ago=60)

        reconciler = RuntimeReconciler(
            self.state_dir,
            config=ReconcilerConfig(auto_recover_expired_leases=False),
        )
        result = reconciler.run()

        self.assertEqual(len(result.expired_leases), 1)
        self.assertEqual(result.expired_leases[0].entity_id, "T1")
        self.assertEqual(result.expired_leases[0].from_state, "leased")
        self.assertEqual(result.expired_leases[0].to_state, "expired")

        lease = self._get_lease("T1")
        self.assertEqual(lease["state"], "expired")

    def test_fresh_lease_not_expired(self):
        """Leases with future expiry are not touched."""
        self._acquire("T1", "d-001", lease_seconds=600)

        reconciler = RuntimeReconciler(self.state_dir)
        result = reconciler.run()

        self.assertEqual(len(result.expired_leases), 0)
        lease = self._get_lease("T1")
        self.assertEqual(lease["state"], "leased")

    def test_idle_lease_not_affected(self):
        """Idle terminals are not touched."""
        reconciler = RuntimeReconciler(self.state_dir)
        result = reconciler.run()
        self.assertTrue(result.is_clean)

    def test_multiple_expired_leases(self):
        """All expired leases are detected in a single pass."""
        self._acquire("T1", "d-001", lease_seconds=600)
        self._acquire("T2", "d-002", lease_seconds=600)
        self._force_expires_at("T1", seconds_ago=60)
        self._force_expires_at("T2", seconds_ago=30)

        reconciler = RuntimeReconciler(
            self.state_dir,
            config=ReconcilerConfig(auto_recover_expired_leases=False),
        )
        result = reconciler.run()

        self.assertEqual(len(result.expired_leases), 2)
        expired_ids = {a.entity_id for a in result.expired_leases}
        self.assertEqual(expired_ids, {"T1", "T2"})

    def test_expired_lease_emits_events(self):
        """Expiry actions append durable runtime events."""
        self._acquire("T2", "d-002", lease_seconds=600)
        self._force_expires_at("T2", seconds_ago=60)

        reconciler = RuntimeReconciler(
            self.state_dir,
            config=ReconcilerConfig(auto_recover_expired_leases=False),
        )
        reconciler.run()

        events = self._events("T2", "lease", "lease_expired")
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "reconciler")


# ---------------------------------------------------------------------------
# Lease auto-recovery tests
# ---------------------------------------------------------------------------

class TestLeaseRecovery(_ReconcilerTestCase):

    def test_auto_recover_expired_leases(self):
        """Expired leases are recovered to idle when auto_recover_expired_leases=True."""
        self._acquire("T1", "d-001", lease_seconds=600)
        self._force_expires_at("T1", seconds_ago=60)

        reconciler = RuntimeReconciler(
            self.state_dir,
            config=ReconcilerConfig(auto_recover_expired_leases=True),
        )
        result = reconciler.run()

        self.assertEqual(len(result.expired_leases), 1)
        self.assertEqual(len(result.recovered_leases), 1)
        self.assertEqual(result.recovered_leases[0].entity_id, "T1")
        self.assertEqual(result.recovered_leases[0].to_state, "idle")

        lease = self._get_lease("T1")
        self.assertEqual(lease["state"], "idle")

    def test_no_recover_when_disabled(self):
        """Expired leases stay expired when auto_recover_expired_leases=False."""
        self._acquire("T1", "d-001", lease_seconds=600)
        self._force_expires_at("T1", seconds_ago=60)

        reconciler = RuntimeReconciler(
            self.state_dir,
            config=ReconcilerConfig(auto_recover_expired_leases=False),
        )
        result = reconciler.run()

        self.assertEqual(len(result.expired_leases), 1)
        self.assertEqual(len(result.recovered_leases), 0)

        lease = self._get_lease("T1")
        self.assertEqual(lease["state"], "expired")

    def test_recovery_emits_events(self):
        """Recovery actions append durable runtime events with timestamps."""
        self._acquire("T3", "d-003", lease_seconds=600)
        self._force_expires_at("T3", seconds_ago=60)

        reconciler = RuntimeReconciler(
            self.state_dir,
            config=ReconcilerConfig(auto_recover_expired_leases=True),
        )
        reconciler.run()

        events = self._events("T3", "lease", "lease_recovered")
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "reconciler")


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------

class TestIdempotency(_ReconcilerTestCase):

    def test_repeated_reconciliation_is_idempotent(self):
        """Running reconciliation twice produces no duplicate state transitions."""
        self._acquire("T1", "d-001", lease_seconds=600)
        self._force_expires_at("T1", seconds_ago=60)

        config = ReconcilerConfig(auto_recover_expired_leases=True)
        reconciler = RuntimeReconciler(self.state_dir, config=config)

        result1 = reconciler.run()
        self.assertEqual(len(result1.expired_leases), 1)
        self.assertEqual(len(result1.recovered_leases), 1)

        # Second run should find nothing to do
        result2 = reconciler.run()
        self.assertEqual(len(result2.expired_leases), 0)
        self.assertEqual(len(result2.recovered_leases), 0)
        self.assertTrue(result2.is_clean)

    def test_idempotent_expiry_only(self):
        """Expiry-only reconciliation is idempotent."""
        self._acquire("T2", "d-002", lease_seconds=600)
        self._force_expires_at("T2", seconds_ago=60)

        config = ReconcilerConfig(auto_recover_expired_leases=False)
        reconciler = RuntimeReconciler(self.state_dir, config=config)

        result1 = reconciler.run()
        self.assertEqual(len(result1.expired_leases), 1)

        result2 = reconciler.run()
        self.assertEqual(len(result2.expired_leases), 0)

    def test_idempotent_dispatch_timeout(self):
        """Dispatch timeout/expiry is idempotent."""
        # Use 'delivering' state which supports timed_out transition
        self._reg("d-stuck", "T1")
        self._transition("d-stuck", "claimed")
        self._transition("d-stuck", "delivering")
        self._force_updated_at("d-stuck", seconds_ago=700)

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)

        result1 = reconciler.run()
        self.assertEqual(len(result1.timed_out_dispatches), 1)

        # Now in timed_out — second run should not time it out again
        result2 = reconciler.run()
        self.assertEqual(len(result2.timed_out_dispatches), 0)

    def test_idempotent_claimed_dispatch_expiry(self):
        """Claimed dispatch expiry is idempotent."""
        self._reg("d-claimed", "T1")
        self._transition("d-claimed", "claimed")
        self._force_updated_at("d-claimed", seconds_ago=700)

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)

        result1 = reconciler.run()
        self.assertEqual(len(result1.expired_dispatches), 1)

        # Now in expired (terminal) — second run should not touch it
        result2 = reconciler.run()
        self.assertEqual(len(result2.expired_dispatches), 0)

    def test_idempotent_attempt_failure(self):
        """Orphaned attempt failure is idempotent."""
        self._reg("d-attempt", "T1")
        attempt = self._create_attempt("d-attempt", "T1")
        self._force_attempt_started_at(attempt["attempt_id"], seconds_ago=400)

        config = ReconcilerConfig(attempt_stale_seconds=300)
        reconciler = RuntimeReconciler(self.state_dir, config=config)

        result1 = reconciler.run()
        self.assertEqual(len(result1.failed_attempts), 1)

        result2 = reconciler.run()
        self.assertEqual(len(result2.failed_attempts), 0)


# ---------------------------------------------------------------------------
# No silent deletion tests
# ---------------------------------------------------------------------------

class TestNoSilentDeletion(_ReconcilerTestCase):

    def test_expired_lease_not_deleted(self):
        """No dispatch or lease is silently deleted during reconciliation."""
        self._acquire("T1", "d-001", lease_seconds=600)
        self._force_expires_at("T1", seconds_ago=60)

        reconciler = RuntimeReconciler(self.state_dir)
        reconciler.run()

        # Lease row must still exist
        lease = self._get_lease("T1")
        self.assertIsNotNone(lease)

        # Dispatch row must still exist
        dispatch = self._get_dispatch("d-001")
        self.assertIsNotNone(dispatch)

    def test_timed_out_dispatch_not_deleted(self):
        """Timed-out dispatches remain in the database."""
        self._reg("d-stuck", "T1")
        self._transition("d-stuck", "claimed")
        self._transition("d-stuck", "delivering")
        self._force_updated_at("d-stuck", seconds_ago=700)

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        reconciler.run()

        dispatch = self._get_dispatch("d-stuck")
        self.assertIsNotNone(dispatch)
        self.assertEqual(dispatch["state"], "timed_out")

    def test_expired_dispatch_not_deleted(self):
        """Expired dispatches remain in the database."""
        self._reg("d-over", "T1")
        self._transition("d-over", "claimed")
        self._transition("d-over", "delivering")
        self._transition("d-over", "failed_delivery", reason="test")
        self._set_attempt_count("d-over", 5)

        config = ReconcilerConfig(max_dispatch_attempts=3)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        reconciler.run()

        dispatch = self._get_dispatch("d-over")
        self.assertIsNotNone(dispatch)
        self.assertEqual(dispatch["state"], "expired")

    def test_failed_attempt_not_deleted(self):
        """Failed attempts remain in the database."""
        self._reg("d-attempt", "T1")
        attempt = self._create_attempt("d-attempt", "T1")
        attempt_id = attempt["attempt_id"]
        self._force_attempt_started_at(attempt_id, seconds_ago=400)

        config = ReconcilerConfig(attempt_stale_seconds=300)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        reconciler.run()

        with get_connection(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "failed")


# ---------------------------------------------------------------------------
# Orphaned attempt tests
# ---------------------------------------------------------------------------

class TestOrphanedAttempts(_ReconcilerTestCase):

    def test_orphaned_pending_attempt(self):
        """Pending attempts past threshold are marked as failed."""
        self._reg("d-001", "T1")
        attempt = self._create_attempt("d-001", "T1")
        self._force_attempt_started_at(attempt["attempt_id"], seconds_ago=400)

        config = ReconcilerConfig(attempt_stale_seconds=300)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.failed_attempts), 1)
        self.assertEqual(result.failed_attempts[0].entity_id, attempt["attempt_id"])
        self.assertEqual(result.failed_attempts[0].from_state, "pending")
        self.assertEqual(result.failed_attempts[0].to_state, "failed")

    def test_orphaned_delivering_attempt(self):
        """Delivering attempts past threshold are marked as failed."""
        self._reg("d-002", "T2")
        attempt = self._create_attempt("d-002", "T2")
        # Use direct SQL to set state without setting ended_at (update_attempt sets ended_at)
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE dispatch_attempts SET state = 'delivering' WHERE attempt_id = ?",
                (attempt["attempt_id"],),
            )
            conn.commit()
        self._force_attempt_started_at(attempt["attempt_id"], seconds_ago=400)

        config = ReconcilerConfig(attempt_stale_seconds=300)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.failed_attempts), 1)
        self.assertEqual(result.failed_attempts[0].from_state, "delivering")

    def test_fresh_attempt_not_affected(self):
        """Recent attempts are not touched."""
        self._reg("d-003", "T1")
        self._create_attempt("d-003", "T1")

        config = ReconcilerConfig(attempt_stale_seconds=300)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.failed_attempts), 0)

    def test_completed_attempt_not_affected(self):
        """Completed attempts are not touched."""
        self._reg("d-004", "T1")
        attempt = self._create_attempt("d-004", "T1")
        with get_connection(self.state_dir) as conn:
            update_attempt(conn, attempt_id=attempt["attempt_id"], state="succeeded")
            conn.commit()
        self._force_attempt_started_at(attempt["attempt_id"], seconds_ago=9999)

        config = ReconcilerConfig(attempt_stale_seconds=300)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.failed_attempts), 0)


# ---------------------------------------------------------------------------
# Dispatch reconciliation tests
# ---------------------------------------------------------------------------

class TestDispatchReconciliation(_ReconcilerTestCase):

    def test_stuck_claimed_dispatch_expired(self):
        """Dispatches stuck in 'claimed' past threshold are expired.

        'claimed' -> 'timed_out' is not a valid transition;
        'claimed' -> 'expired' is the correct recovery path.
        """
        self._reg("d-stuck", "T1")
        self._transition("d-stuck", "claimed")
        self._force_updated_at("d-stuck", seconds_ago=700)

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.expired_dispatches), 1)
        self.assertEqual(result.expired_dispatches[0].from_state, "claimed")
        self.assertEqual(result.expired_dispatches[0].to_state, "expired")

        dispatch = self._get_dispatch("d-stuck")
        self.assertEqual(dispatch["state"], "expired")

    def test_stuck_delivering_dispatch_timed_out(self):
        """Dispatches stuck in 'delivering' past threshold are timed out."""
        self._reg("d-delivering", "T1")
        self._transition("d-delivering", "claimed")
        self._transition("d-delivering", "delivering")
        self._force_updated_at("d-delivering", seconds_ago=700)

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.timed_out_dispatches), 1)
        self.assertEqual(result.timed_out_dispatches[0].from_state, "delivering")

    def test_stuck_accepted_dispatch_timed_out(self):
        """Dispatches stuck in 'accepted' past threshold are timed out."""
        self._reg("d-accepted", "T1")
        self._transition("d-accepted", "claimed")
        self._transition("d-accepted", "delivering")
        self._transition("d-accepted", "accepted")
        self._force_updated_at("d-accepted", seconds_ago=700)

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.timed_out_dispatches), 1)
        self.assertEqual(result.timed_out_dispatches[0].from_state, "accepted")

    def test_stuck_running_dispatch_timed_out(self):
        """Dispatches stuck in 'running' past threshold are timed out."""
        self._reg("d-running", "T1")
        self._transition("d-running", "claimed")
        self._transition("d-running", "delivering")
        self._transition("d-running", "accepted")
        self._transition("d-running", "running")
        self._force_updated_at("d-running", seconds_ago=700)

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.timed_out_dispatches), 1)
        self.assertEqual(result.timed_out_dispatches[0].from_state, "running")

    def test_fresh_dispatch_not_timed_out(self):
        """Recent dispatches are not timed out."""
        self._reg("d-fresh", "T1")
        self._transition("d-fresh", "claimed")

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.timed_out_dispatches), 0)

    def test_timeout_emits_events(self):
        """Timeout actions append durable runtime events with timestamps and reasons."""
        self._reg("d-stuck", "T1")
        self._transition("d-stuck", "claimed")
        self._transition("d-stuck", "delivering")
        self._force_updated_at("d-stuck", seconds_ago=700)

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        reconciler.run()

        events = self._events("d-stuck", "dispatch", "dispatch_timed_out")
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "reconciler")

    def test_claimed_expiry_emits_events(self):
        """Claimed dispatch expiry appends durable runtime events."""
        self._reg("d-claimed", "T1")
        self._transition("d-claimed", "claimed")
        self._force_updated_at("d-claimed", seconds_ago=700)

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        reconciler.run()

        events = self._events("d-claimed", "dispatch", "dispatch_expired")
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "reconciler")


# ---------------------------------------------------------------------------
# Over-attempted dispatch expiry tests
# ---------------------------------------------------------------------------

class TestOverAttemptedDispatches(_ReconcilerTestCase):

    def test_over_attempted_dispatch_expired(self):
        """Dispatches exceeding max attempts are expired."""
        self._reg("d-over", "T1")
        self._transition("d-over", "claimed")
        self._transition("d-over", "delivering")
        self._transition("d-over", "failed_delivery", reason="test")
        self._set_attempt_count("d-over", 5)

        config = ReconcilerConfig(max_dispatch_attempts=3)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.expired_dispatches), 1)
        self.assertEqual(result.expired_dispatches[0].from_state, "failed_delivery")
        self.assertEqual(result.expired_dispatches[0].to_state, "expired")

        dispatch = self._get_dispatch("d-over")
        self.assertEqual(dispatch["state"], "expired")

    def test_under_limit_dispatch_not_expired(self):
        """Dispatches under attempt limit are not expired."""
        self._reg("d-under", "T1")
        self._transition("d-under", "claimed")
        self._transition("d-under", "delivering")
        self._transition("d-under", "failed_delivery", reason="test")
        self._set_attempt_count("d-under", 1)

        config = ReconcilerConfig(max_dispatch_attempts=3, auto_recover_dispatches=False)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.expired_dispatches), 0)
        dispatch = self._get_dispatch("d-under")
        self.assertEqual(dispatch["state"], "failed_delivery")


# ---------------------------------------------------------------------------
# Dispatch auto-recovery tests
# ---------------------------------------------------------------------------

class TestDispatchAutoRecovery(_ReconcilerTestCase):

    def test_auto_recover_timed_out_dispatch(self):
        """Timed-out dispatches are recovered when auto_recover_dispatches=True."""
        self._reg("d-timeout", "T1")
        self._transition("d-timeout", "claimed")
        self._transition("d-timeout", "delivering")
        self._transition("d-timeout", "timed_out")

        config = ReconcilerConfig(auto_recover_dispatches=True, max_dispatch_attempts=3)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.recovered_dispatches), 1)
        self.assertEqual(result.recovered_dispatches[0].to_state, "recovered")

        dispatch = self._get_dispatch("d-timeout")
        self.assertEqual(dispatch["state"], "recovered")

    def test_auto_recover_failed_delivery_dispatch(self):
        """Failed delivery dispatches are recovered when auto_recover_dispatches=True."""
        self._reg("d-fail", "T1")
        self._transition("d-fail", "claimed")
        self._transition("d-fail", "delivering")
        self._transition("d-fail", "failed_delivery", reason="test")

        config = ReconcilerConfig(auto_recover_dispatches=True, max_dispatch_attempts=3)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.recovered_dispatches), 1)

    def test_no_auto_recover_flags_for_review(self):
        """When auto_recover_dispatches=False, recoverable dispatches are flagged."""
        self._reg("d-review", "T1")
        self._transition("d-review", "claimed")
        self._transition("d-review", "delivering")
        self._transition("d-review", "timed_out")

        config = ReconcilerConfig(auto_recover_dispatches=False, max_dispatch_attempts=3)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.recovered_dispatches), 0)
        self.assertEqual(len(result.needs_review), 1)
        self.assertEqual(result.needs_review[0].entity_id, "d-review")
        self.assertIn("operator review", result.needs_review[0].reason)


# ---------------------------------------------------------------------------
# Recovery summary and needs_review tests
# ---------------------------------------------------------------------------

class TestRecoverySummary(_ReconcilerTestCase):

    def test_needs_review_identifies_terminals(self):
        """Recovery summaries identify which dispatches/terminals require review."""
        self._reg("d-review-1", "T1")
        self._transition("d-review-1", "claimed")
        self._transition("d-review-1", "delivering")
        self._transition("d-review-1", "timed_out")

        self._reg("d-review-2", "T2")
        self._transition("d-review-2", "claimed")
        self._transition("d-review-2", "delivering")
        self._transition("d-review-2", "failed_delivery", reason="test")

        config = ReconcilerConfig(auto_recover_dispatches=False, max_dispatch_attempts=5)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertEqual(len(result.needs_review), 2)
        review_ids = {item.entity_id for item in result.needs_review}
        self.assertEqual(review_ids, {"d-review-1", "d-review-2"})

    def test_summary_includes_all_categories(self):
        """Summary text includes counts for all action categories."""
        reconciler = RuntimeReconciler(self.state_dir)
        result = reconciler.run()
        summary = result.summary()

        self.assertIn("Expired leases:", summary)
        self.assertIn("Recovered leases:", summary)
        self.assertIn("Timed-out dispatches:", summary)
        self.assertIn("Needs operator review:", summary)

    def test_to_dict_serializable(self):
        """Result can be serialized to JSON."""
        self._acquire("T1", "d-001", lease_seconds=600)
        self._force_expires_at("T1", seconds_ago=60)

        reconciler = RuntimeReconciler(self.state_dir)
        result = reconciler.run()
        data = result.to_dict()

        # Must be JSON-serializable
        json_str = json.dumps(data)
        parsed = json.loads(json_str)
        self.assertIn("expired_leases", parsed)
        self.assertIn("run_at", parsed)
        self.assertIn("total_actions", parsed)

    def test_is_clean_when_nothing_to_do(self):
        """Result.is_clean is True when reconciliation finds no issues."""
        reconciler = RuntimeReconciler(self.state_dir)
        result = reconciler.run()
        self.assertTrue(result.is_clean)


# ---------------------------------------------------------------------------
# Dry-run mode tests
# ---------------------------------------------------------------------------

class TestDryRun(_ReconcilerTestCase):

    def test_dry_run_detects_without_modifying(self):
        """Dry-run detects expired leases but does not modify state."""
        self._acquire("T1", "d-001", lease_seconds=600)
        self._force_expires_at("T1", seconds_ago=60)

        reconciler = RuntimeReconciler(self.state_dir)
        result = reconciler.run(dry_run=True)

        self.assertEqual(len(result.expired_leases), 1)
        self.assertTrue(result.dry_run)

        # State should be unchanged
        lease = self._get_lease("T1")
        self.assertEqual(lease["state"], "leased")

    def test_dry_run_detects_orphaned_attempts(self):
        """Dry-run detects orphaned attempts without modifying them."""
        self._reg("d-dry", "T1")
        attempt = self._create_attempt("d-dry", "T1")
        self._force_attempt_started_at(attempt["attempt_id"], seconds_ago=400)

        config = ReconcilerConfig(attempt_stale_seconds=300)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run(dry_run=True)

        self.assertEqual(len(result.failed_attempts), 1)

        # Attempt should still be in original state
        with get_connection(self.state_dir) as conn:
            row = conn.execute(
                "SELECT state FROM dispatch_attempts WHERE attempt_id = ?",
                (attempt["attempt_id"],),
            ).fetchone()
        self.assertEqual(row["state"], "pending")

    def test_dry_run_detects_stuck_dispatches(self):
        """Dry-run detects stuck dispatches without modifying them."""
        self._reg("d-stuck", "T1")
        self._transition("d-stuck", "claimed")
        self._transition("d-stuck", "delivering")
        self._force_updated_at("d-stuck", seconds_ago=700)

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run(dry_run=True)

        self.assertEqual(len(result.timed_out_dispatches), 1)

        dispatch = self._get_dispatch("d-stuck")
        self.assertEqual(dispatch["state"], "delivering")

    def test_dry_run_detects_stuck_claimed_dispatches(self):
        """Dry-run detects stuck claimed dispatches without modifying them."""
        self._reg("d-claimed", "T1")
        self._transition("d-claimed", "claimed")
        self._force_updated_at("d-claimed", seconds_ago=700)

        config = ReconcilerConfig(dispatch_stuck_seconds=600)
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run(dry_run=True)

        self.assertEqual(len(result.expired_dispatches), 1)

        dispatch = self._get_dispatch("d-claimed")
        self.assertEqual(dispatch["state"], "claimed")


# ---------------------------------------------------------------------------
# Mixed scenario tests
# ---------------------------------------------------------------------------

class TestMixedScenarios(_ReconcilerTestCase):

    def test_full_reconciliation_pass(self):
        """Full reconciliation pass handles leases, attempts, and dispatches together."""
        # Expired lease on T1
        self._acquire("T1", "d-001", lease_seconds=600)
        self._force_expires_at("T1", seconds_ago=60)

        # Orphaned attempt
        self._reg("d-orphan", "T2")
        attempt = self._create_attempt("d-orphan", "T2")
        self._force_attempt_started_at(attempt["attempt_id"], seconds_ago=400)

        # Stuck dispatch (delivering supports timed_out transition)
        self._reg("d-stuck", "T3")
        self._transition("d-stuck", "claimed")
        self._transition("d-stuck", "delivering")
        self._force_updated_at("d-stuck", seconds_ago=700)

        config = ReconcilerConfig(
            auto_recover_expired_leases=True,
            attempt_stale_seconds=300,
            dispatch_stuck_seconds=600,
        )
        reconciler = RuntimeReconciler(self.state_dir, config=config)
        result = reconciler.run()

        self.assertGreater(result.total_actions, 0)
        self.assertFalse(result.is_clean)
        self.assertEqual(len(result.expired_leases), 1)
        self.assertEqual(len(result.recovered_leases), 1)
        self.assertEqual(len(result.failed_attempts), 1)
        self.assertEqual(len(result.timed_out_dispatches), 1)

    def test_full_pass_then_clean(self):
        """After a full pass, a second run finds nothing to do."""
        self._acquire("T1", "d-001", lease_seconds=600)
        self._force_expires_at("T1", seconds_ago=60)

        self._reg("d-stuck", "T2")
        self._transition("d-stuck", "claimed")
        self._transition("d-stuck", "delivering")
        self._force_updated_at("d-stuck", seconds_ago=700)

        config = ReconcilerConfig(
            auto_recover_expired_leases=True,
            dispatch_stuck_seconds=600,
            auto_recover_dispatches=False,
        )
        reconciler = RuntimeReconciler(self.state_dir, config=config)

        result1 = reconciler.run()
        self.assertGreater(result1.total_actions, 0)

        result2 = reconciler.run()
        # Only needs_review items may remain (not actionable)
        self.assertEqual(result2.total_actions, 0)


# ---------------------------------------------------------------------------
# Factory function tests
# ---------------------------------------------------------------------------

class TestFactory(_ReconcilerTestCase):

    def test_load_reconciler(self):
        """load_reconciler returns a RuntimeReconciler."""
        reconciler = load_reconciler(self.state_dir)
        self.assertIsInstance(reconciler, RuntimeReconciler)

    def test_load_reconciler_with_config(self):
        """load_reconciler accepts custom config."""
        config = ReconcilerConfig(attempt_stale_seconds=999)
        reconciler = load_reconciler(self.state_dir, config=config)
        result = reconciler.run()
        self.assertEqual(result.config["attempt_stale_seconds"], 999)


if __name__ == "__main__":
    unittest.main()
