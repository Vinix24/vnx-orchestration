#!/usr/bin/env python3
"""Tests for OI-1100: receipt processor releases lease after completion receipt.

Scenario being verified:
  1. Dispatcher acquires lease for a worker (state=leased, TTL=N seconds).
  2. Worker takes longer than the TTL or crashes — reconciler eventually
     transitions the lease to state=expired so future check_terminal calls
     return `lease_expired_not_cleaned`.
  3. Worker eventually delivers a task_complete receipt.
  4. The receipt processor must release/recover the lease so the next
     dispatch is not blocked.

Before the fix, step 4 raised `InvalidTransitionError` because the
underlying release_lease() helper only accepts state="leased" or
"recovering". The fix adds an expired-state branch that calls
LeaseManager.recover() so expired -> recovering -> idle, leaving the
terminal reusable for the next dispatch.

The shell-level call site (`_auto_release_lease_on_receipt` in
receipt_processor_v4.sh) drives `runtime_core_cli.py release-on-receipt`,
so verifying the Python API also verifies the receipt-processor pipeline
behavior end-to-end.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (
    get_connection,
    get_events,
    init_schema,
    register_dispatch,
)
from lease_manager import LeaseManager
from dispatch_broker import DispatchBroker
from runtime_core import RuntimeCore


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
    return state_dir, lease_mgr, core


def _acquire(lease_mgr: LeaseManager, state_dir: Path, terminal: str, dispatch_id: str) -> int:
    with get_connection(state_dir) as conn:
        register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal)
        conn.commit()
    return lease_mgr.acquire(terminal, dispatch_id=dispatch_id).generation


class TestReceiptProcessorLeaseReleaseAfterCrash(unittest.TestCase):
    """Worker crashes, reconciler expires the lease, completion receipt
    arrives later — the receipt processor must clear the lease."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.lease_mgr, self.core = _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_completion_receipt_recovers_expired_lease(self):
        """OI-1100 root-cause regression test.

        Pre-fix: release_on_receipt raised InvalidTransitionError because
        release_lease only accepts state in {leased, recovering}. The
        terminal therefore stayed in `expired`, and check_terminal kept
        returning `lease_expired_not_cleaned` — blocking every subsequent
        dispatch on that terminal until a manual chain-closeout.
        """
        _acquire(self.lease_mgr, self.state_dir, "T1", "d-crash-001")

        # Reconciler simulates TTL elapse: leased -> expired.
        self.lease_mgr.expire("T1", actor="reconciler", reason="TTL elapsed")
        self.assertEqual(self.lease_mgr.get("T1").state, "expired")

        # Completion receipt finally arrives — receipt processor calls this.
        result = self.core.release_on_receipt("T1", dispatch_id="d-crash-001")

        self.assertTrue(result["released"], f"expected released=True: {result}")
        self.assertTrue(result.get("recovered"), f"expected recovered=True: {result}")
        self.assertEqual(self.lease_mgr.get("T1").state, "idle")

    def test_terminal_reacquirable_after_expired_recovery(self):
        """After OI-1100 recovery, the terminal must accept a fresh dispatch."""
        _acquire(self.lease_mgr, self.state_dir, "T2", "d-crash-002")
        self.lease_mgr.expire("T2", actor="reconciler", reason="TTL elapsed")
        self.core.release_on_receipt("T2", dispatch_id="d-crash-002")

        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id="d-next-002", terminal_id="T2")
            conn.commit()
        new_lease = self.lease_mgr.acquire("T2", dispatch_id="d-next-002")
        self.assertEqual(new_lease.state, "leased")
        self.assertEqual(new_lease.dispatch_id, "d-next-002")

    def test_expired_recovery_emits_auditable_events(self):
        """Recovery path must leave a paper trail (lease_recovering /
        lease_recovered) just like a normal release emits lease_released."""
        _acquire(self.lease_mgr, self.state_dir, "T3", "d-crash-003")
        self.lease_mgr.expire("T3", actor="reconciler", reason="TTL elapsed")

        self.core.release_on_receipt("T3", dispatch_id="d-crash-003")

        with get_connection(self.state_dir) as conn:
            event_types = {
                e["event_type"]
                for e in get_events(conn, entity_id="T3", entity_type="lease")
            }
        self.assertIn("lease_expired", event_types)
        self.assertIn("lease_recovering", event_types)
        self.assertIn("lease_recovered", event_types)

    def test_expired_recovery_idempotent_when_called_twice(self):
        """Re-delivering the completion receipt must not error.

        After the first recovery the lease is idle; the second invocation
        must hit the existing already_idle short-circuit.
        """
        _acquire(self.lease_mgr, self.state_dir, "T1", "d-crash-004")
        self.lease_mgr.expire("T1", actor="reconciler", reason="TTL elapsed")

        first = self.core.release_on_receipt("T1", dispatch_id="d-crash-004")
        self.assertTrue(first["released"])
        self.assertTrue(first.get("recovered"))

        second = self.core.release_on_receipt("T1", dispatch_id="d-crash-004")
        self.assertTrue(second["released"], f"second call must be no-op: {second}")
        self.assertTrue(second.get("skipped"), f"expected skipped=True: {second}")

    def test_expired_recovery_honors_ownership_mismatch(self):
        """Even when expired, a wrong-owner receipt must not steal the lease.

        Ownership guard runs before the state-based dispatch, so an
        expired lease owned by `d-real` is not silently recovered by a
        receipt claiming dispatch_id `d-attacker`.
        """
        _acquire(self.lease_mgr, self.state_dir, "T2", "d-real-005")
        self.lease_mgr.expire("T2", actor="reconciler", reason="TTL elapsed")

        result = self.core.release_on_receipt("T2", dispatch_id="d-attacker-005")
        self.assertFalse(result["released"], f"mismatch must not recover: {result}")
        self.assertIn("ownership_mismatch", result.get("reason", ""))
        self.assertEqual(self.lease_mgr.get("T2").state, "expired")


class TestReceiptProcessorLeaseReleaseRegressionGuard(unittest.TestCase):
    """Verify the existing `leased` -> `idle` happy path still works
    after the expired-state branch was added."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.lease_mgr, self.core = _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_normal_completion_still_releases_leased(self):
        _acquire(self.lease_mgr, self.state_dir, "T1", "d-normal-001")
        result = self.core.release_on_receipt("T1", dispatch_id="d-normal-001")
        self.assertTrue(result["released"])
        self.assertNotIn("recovered", result)
        self.assertEqual(self.lease_mgr.get("T1").state, "idle")

    def test_lease_released_on_success_receipt(self):
        """task_complete path: lease must transition leased -> idle."""
        _acquire(self.lease_mgr, self.state_dir, "T1", "d-success-001")
        self.assertEqual(self.lease_mgr.get("T1").state, "leased")

        result = self.core.release_on_receipt("T1", dispatch_id="d-success-001")

        self.assertTrue(result["released"], f"expected released=True: {result}")
        self.assertNotIn("recovered", result)
        self.assertEqual(self.lease_mgr.get("T1").state, "idle")

    def test_lease_released_on_failure_receipt(self):
        """task_failed path: lease must still be released (not only on success)."""
        _acquire(self.lease_mgr, self.state_dir, "T2", "d-failure-001")
        self.assertEqual(self.lease_mgr.get("T2").state, "leased")

        result = self.core.release_on_receipt("T2", dispatch_id="d-failure-001")

        self.assertTrue(result["released"], f"failure receipt must release lease: {result}")
        self.assertNotIn("recovered", result)
        self.assertEqual(self.lease_mgr.get("T2").state, "idle")

    def test_lease_released_on_timeout_receipt(self):
        """task_timeout path (non-no_confirmation): lease must be released."""
        _acquire(self.lease_mgr, self.state_dir, "T3", "d-timeout-001")
        self.assertEqual(self.lease_mgr.get("T3").state, "leased")

        result = self.core.release_on_receipt("T3", dispatch_id="d-timeout-001")

        self.assertTrue(result["released"], f"timeout receipt must release lease: {result}")
        self.assertNotIn("recovered", result)
        self.assertEqual(self.lease_mgr.get("T3").state, "idle")


if __name__ == "__main__":
    unittest.main()
