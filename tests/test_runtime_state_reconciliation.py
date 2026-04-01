#!/usr/bin/env python3
"""
Tests for PR-2: Runtime Truth Reconciliation Between LeaseManager And Runtime Core.

Quality gate: gate_pr2_runtime_state_reconciliation

Coverage:
  - Zombie lease: lease held but dispatch in terminal/failed state
  - Ghost dispatch: dispatch in active delivery state but lease idle
  - Queue projection stale: active dispatch in DB but projection shows nothing in progress
  - Generation snapshot drift: recorded generation differs from DB generation
  - Clean state: no mismatches when lease and dispatch agree
  - check_terminal: reports zombie_lease mismatch instead of opaque block
  - check_terminal: dispatch safety checks use reconciled runtime truth
  - Dispatch with same dispatch_id as held lease is always available (existing behavior)
  - Idle terminal is always available (existing behavior)
  - Reconcile idempotency: repeated calls produce no state changes
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
    get_lease,
    init_schema,
    register_dispatch,
    transition_dispatch,
)
from lease_manager import LeaseManager
from dispatch_broker import DispatchBroker
from runtime_core import RuntimeCore
from runtime_state_reconciler import (
    GENERATION_SNAPSHOT_DRIFT,
    GHOST_DISPATCH,
    QUEUE_PROJECTION_STALE,
    ZOMBIE_LEASE,
    RuntimeStateDiagnostic,
    RuntimeStateMismatch,
    RuntimeStateReconciler,
    load_reconciler,
)


# ---------------------------------------------------------------------------
# Base test fixture
# ---------------------------------------------------------------------------

class _Base(unittest.TestCase):
    """Creates temp state dir with initialized schema, broker, lease_mgr, core."""

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

    # Helpers

    def _register(self, dispatch_id: str, terminal_id: str = "T2", **kwargs):
        with get_connection(self.state_dir) as conn:
            row = register_dispatch(conn, dispatch_id=dispatch_id,
                                    terminal_id=terminal_id, **kwargs)
            conn.commit()
        return row

    def _acquire(self, terminal_id: str, dispatch_id: str) -> int:
        """Register + acquire lease. Returns generation."""
        self._register(dispatch_id, terminal_id)
        result = self.lease_mgr.acquire(terminal_id, dispatch_id)
        return result.generation

    def _transition(self, dispatch_id: str, to_state: str):
        """Transition dispatch to a new state, traversing intermediates."""
        state_path = {
            "claimed":         ["claimed"],
            "delivering":      ["claimed", "delivering"],
            "accepted":        ["claimed", "delivering", "accepted"],
            "running":         ["claimed", "delivering", "accepted", "running"],
            "completed":       ["claimed", "delivering", "accepted", "running", "completed"],
            "failed_delivery": ["claimed", "delivering", "failed_delivery"],
            "timed_out":       ["claimed", "delivering", "timed_out"],
            "expired":         ["claimed", "expired"],
        }
        path = state_path.get(to_state, [to_state])
        with get_connection(self.state_dir) as conn:
            for state in path:
                try:
                    transition_dispatch(conn, dispatch_id=dispatch_id, to_state=state,
                                        actor="test")
                except Exception:
                    pass
            conn.commit()

    def _write_projection(self, active=None, completed=None, prs=None):
        """Write a pr_queue_state.json to state_dir."""
        payload = {
            "active": active or [],
            "completed": completed or [],
            "prs": prs or [],
        }
        path = self.state_dir / "pr_queue_state.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# TestZombieLease
# ---------------------------------------------------------------------------

class TestZombieLease(_Base):
    """Lease is held but dispatch has already ended."""

    def test_detects_zombie_when_dispatch_completed(self):
        gen = self._acquire("T2", "d-zombie-complete")
        self._transition("d-zombie-complete", "completed")
        # Lease NOT released — simulates a failed release path

        diag = self.reconciler.reconcile()
        zombies = [m for m in diag.mismatches if m.mismatch_type == ZOMBIE_LEASE]
        self.assertEqual(len(zombies), 1)
        z = zombies[0]
        self.assertEqual(z.terminal_id, "T2")
        self.assertEqual(z.dispatch_id, "d-zombie-complete")
        self.assertEqual(z.lease_state, "leased")
        self.assertEqual(z.dispatch_state, "completed")
        self.assertEqual(z.severity, "blocking")

    def test_detects_zombie_when_dispatch_failed_delivery(self):
        gen = self._acquire("T2", "d-zombie-fail")
        self._transition("d-zombie-fail", "failed_delivery")
        # Lease NOT released

        diag = self.reconciler.reconcile()
        zombies = [m for m in diag.mismatches if m.mismatch_type == ZOMBIE_LEASE]
        self.assertEqual(len(zombies), 1)
        self.assertEqual(zombies[0].dispatch_state, "failed_delivery")

    def test_detects_zombie_when_dispatch_expired(self):
        gen = self._acquire("T2", "d-zombie-expired")
        self._transition("d-zombie-expired", "expired")

        diag = self.reconciler.reconcile()
        zombies = [m for m in diag.mismatches if m.mismatch_type == ZOMBIE_LEASE]
        self.assertEqual(len(zombies), 1)
        self.assertEqual(zombies[0].dispatch_state, "expired")

    def test_detects_zombie_when_dispatch_timed_out(self):
        gen = self._acquire("T2", "d-zombie-timeout")
        self._transition("d-zombie-timeout", "timed_out")

        diag = self.reconciler.reconcile()
        zombies = [m for m in diag.mismatches if m.mismatch_type == ZOMBIE_LEASE]
        self.assertEqual(len(zombies), 1)
        self.assertEqual(zombies[0].dispatch_state, "timed_out")

    def test_no_zombie_when_lease_released(self):
        gen = self._acquire("T2", "d-clean")
        self._transition("d-clean", "completed")
        self.lease_mgr.release("T2", gen)

        diag = self.reconciler.reconcile()
        zombies = [m for m in diag.mismatches if m.mismatch_type == ZOMBIE_LEASE]
        self.assertEqual(len(zombies), 0)

    def test_no_zombie_when_dispatch_still_active(self):
        gen = self._acquire("T2", "d-active")
        self._transition("d-active", "delivering")
        # Lease still held — this is correct

        diag = self.reconciler.reconcile()
        zombies = [m for m in diag.mismatches if m.mismatch_type == ZOMBIE_LEASE]
        self.assertEqual(len(zombies), 0)

    def test_zombie_message_is_operator_readable(self):
        self._acquire("T2", "d-msg-test")
        self._transition("d-msg-test", "failed_delivery")

        diag = self.reconciler.reconcile()
        z = next(m for m in diag.mismatches if m.mismatch_type == ZOMBIE_LEASE)
        self.assertIn("T2", z.message)
        self.assertIn("d-msg-test", z.message)
        self.assertIn("failed_delivery", z.message)


# ---------------------------------------------------------------------------
# TestGhostDispatch
# ---------------------------------------------------------------------------

class TestGhostDispatch(_Base):
    """Dispatch in active state but terminal lease is idle."""

    def test_detects_ghost_when_delivering_without_lease(self):
        # Register and transition to delivering WITHOUT acquiring lease
        self._register("d-ghost-deliver", "T1")
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="d-ghost-deliver",
                                to_state="claimed", actor="test")
            transition_dispatch(conn, dispatch_id="d-ghost-deliver",
                                to_state="delivering", actor="test")
            conn.commit()

        diag = self.reconciler.reconcile()
        ghosts = [m for m in diag.mismatches if m.mismatch_type == GHOST_DISPATCH]
        self.assertEqual(len(ghosts), 1)
        g = ghosts[0]
        self.assertEqual(g.terminal_id, "T1")
        self.assertEqual(g.dispatch_id, "d-ghost-deliver")
        self.assertEqual(g.dispatch_state, "delivering")
        self.assertEqual(g.severity, "blocking")

    def test_detects_ghost_when_accepted_without_lease(self):
        self._register("d-ghost-accept", "T1")
        with get_connection(self.state_dir) as conn:
            for state in ("claimed", "delivering", "accepted"):
                transition_dispatch(conn, dispatch_id="d-ghost-accept",
                                    to_state=state, actor="test")
            conn.commit()

        diag = self.reconciler.reconcile()
        ghosts = [m for m in diag.mismatches if m.mismatch_type == GHOST_DISPATCH]
        self.assertEqual(len(ghosts), 1)
        self.assertEqual(ghosts[0].dispatch_state, "accepted")

    def test_no_ghost_when_lease_held_and_dispatch_active(self):
        self._acquire("T2", "d-legit")
        self._transition("d-legit", "delivering")

        diag = self.reconciler.reconcile()
        ghosts = [m for m in diag.mismatches if m.mismatch_type == GHOST_DISPATCH]
        self.assertEqual(len(ghosts), 0)

    def test_no_ghost_for_terminal_states(self):
        # Dispatch is completed with no lease — that's normal
        self._register("d-done", "T1")
        self._transition("d-done", "completed")

        diag = self.reconciler.reconcile()
        ghosts = [m for m in diag.mismatches if m.mismatch_type == GHOST_DISPATCH]
        self.assertEqual(len(ghosts), 0)


# ---------------------------------------------------------------------------
# TestQueueProjectionStale
# ---------------------------------------------------------------------------

class TestQueueProjectionStale(_Base):
    """Active dispatch exists in DB but queue projection shows nothing in progress."""

    def test_detects_stale_when_active_dispatch_not_in_projection(self):
        # Dispatch is delivering but projection shows it as queued
        self._register("d-pr2-001", "T2", pr_ref="PR-2")
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="d-pr2-001",
                                to_state="claimed", actor="test")
            transition_dispatch(conn, dispatch_id="d-pr2-001",
                                to_state="delivering", actor="test")
            conn.commit()

        proj_path = self._write_projection(active=[], completed=["PR-0", "PR-1"])
        reconciler = RuntimeStateReconciler(self.state_dir, projection_file=proj_path)

        diag = reconciler.reconcile()
        stale = [m for m in diag.mismatches if m.mismatch_type == QUEUE_PROJECTION_STALE]
        self.assertEqual(len(stale), 1)
        s = stale[0]
        self.assertEqual(s.dispatch_id, "d-pr2-001")
        self.assertEqual(s.dispatch_state, "delivering")
        self.assertIn("PR-2", s.metadata.get("pr_ref", ""))

    def test_no_stale_when_projection_lists_active_pr(self):
        self._register("d-pr2-active", "T2", pr_ref="PR-2")
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="d-pr2-active",
                                to_state="claimed", actor="test")
            conn.commit()

        proj_path = self._write_projection(active=["PR-2"])
        reconciler = RuntimeStateReconciler(self.state_dir, projection_file=proj_path)

        diag = reconciler.reconcile()
        stale = [m for m in diag.mismatches if m.mismatch_type == QUEUE_PROJECTION_STALE]
        self.assertEqual(len(stale), 0)

    def test_no_stale_check_when_no_projection_file(self):
        # Without projection file, queue-stale check is skipped
        self._register("d-no-proj", "T2", pr_ref="PR-2")
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="d-no-proj",
                                to_state="claimed", actor="test")
            conn.commit()

        # Reconciler without projection_file
        diag = self.reconciler.reconcile()
        stale = [m for m in diag.mismatches if m.mismatch_type == QUEUE_PROJECTION_STALE]
        self.assertEqual(len(stale), 0)

    def test_stale_message_mentions_in_progress_none(self):
        self._register("d-pr3-run", "T3", pr_ref="PR-3")
        with get_connection(self.state_dir) as conn:
            for state in ("claimed", "delivering", "accepted", "running"):
                transition_dispatch(conn, dispatch_id="d-pr3-run",
                                    to_state=state, actor="test")
            conn.commit()

        proj_path = self._write_projection(active=[])
        reconciler = RuntimeStateReconciler(self.state_dir, projection_file=proj_path)

        diag = reconciler.reconcile()
        stale = [m for m in diag.mismatches if m.mismatch_type == QUEUE_PROJECTION_STALE]
        self.assertEqual(len(stale), 1)
        self.assertIn("In Progress", stale[0].message)


# ---------------------------------------------------------------------------
# TestGenerationSnapshotDrift
# ---------------------------------------------------------------------------

class TestGenerationSnapshotDrift(_Base):
    """Stored generation snapshot disagrees with current DB generation."""

    def test_detects_drift_when_generation_advanced(self):
        gen = self._acquire("T2", "d-gen-drift")
        # Snapshot records generation N, but a new lease cycle occurred (gen N+1)
        self.lease_mgr.release("T2", gen)
        # Register the new dispatch before acquiring the lease (FK constraint)
        self._register("d-gen-drift-2", "T2")
        gen2 = self.lease_mgr.acquire("T2", "d-gen-drift-2").generation

        # Snapshot still has old generation
        diag = self.reconciler.reconcile(snapshot_generations={"T2": gen})
        drifts = [m for m in diag.mismatches if m.mismatch_type == GENERATION_SNAPSHOT_DRIFT]
        self.assertEqual(len(drifts), 1)
        d = drifts[0]
        self.assertEqual(d.terminal_id, "T2")
        self.assertEqual(d.snapshot_generation, gen)
        self.assertEqual(d.generation, gen2)

    def test_no_drift_when_generation_matches(self):
        gen = self._acquire("T2", "d-gen-match")

        diag = self.reconciler.reconcile(snapshot_generations={"T2": gen})
        drifts = [m for m in diag.mismatches if m.mismatch_type == GENERATION_SNAPSHOT_DRIFT]
        self.assertEqual(len(drifts), 0)

    def test_detects_drift_for_missing_terminal(self):
        # Snapshot references T99 which has no row in DB
        diag = self.reconciler.reconcile(snapshot_generations={"T99": 5})
        drifts = [m for m in diag.mismatches if m.mismatch_type == GENERATION_SNAPSHOT_DRIFT]
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0].terminal_id, "T99")
        self.assertEqual(drifts[0].snapshot_generation, 5)

    def test_drift_message_explains_stale_guard(self):
        gen = self._acquire("T2", "d-gen-msg")
        self.lease_mgr.release("T2", gen)
        self._register("d-gen-msg-2", "T2")
        self.lease_mgr.acquire("T2", "d-gen-msg-2")

        diag = self.reconciler.reconcile(snapshot_generations={"T2": gen})
        d = next(m for m in diag.mismatches if m.mismatch_type == GENERATION_SNAPSHOT_DRIFT)
        self.assertIn("generation guard", d.message)


# ---------------------------------------------------------------------------
# TestCleanState
# ---------------------------------------------------------------------------

class TestCleanState(_Base):
    """No mismatches when lease and dispatch state are consistent."""

    def test_clean_idle_terminal(self):
        # Terminal with no lease and no dispatches
        diag = self.reconciler.reconcile()
        self.assertTrue(diag.is_clean)

    def test_clean_active_lease_with_active_dispatch(self):
        self._acquire("T2", "d-clean-active")
        self._transition("d-clean-active", "delivering")

        diag = self.reconciler.reconcile()
        self.assertEqual(len(diag.mismatches), 0)

    def test_clean_after_release(self):
        gen = self._acquire("T2", "d-clean-release")
        self._transition("d-clean-release", "completed")
        self.lease_mgr.release("T2", gen)

        diag = self.reconciler.reconcile()
        self.assertEqual(len(diag.mismatches), 0)

    def test_is_clean_false_when_mismatch_exists(self):
        self._acquire("T2", "d-dirty")
        self._transition("d-dirty", "completed")
        # Lease not released

        diag = self.reconciler.reconcile()
        self.assertFalse(diag.is_clean)

    def test_has_blocking_true_for_zombie(self):
        self._acquire("T2", "d-block")
        self._transition("d-block", "expired")

        diag = self.reconciler.reconcile()
        self.assertTrue(diag.has_blocking)


# ---------------------------------------------------------------------------
# TestCheckTerminalWithReconciliation
# ---------------------------------------------------------------------------

class TestCheckTerminalWithReconciliation(_Base):
    """check_terminal uses reconciled runtime truth — PR-2 gate requirement."""

    def test_zombie_lease_reported_explicitly(self):
        """Terminal with zombie lease gets mismatch=zombie_lease, not opaque block."""
        gen = self._acquire("T2", "d-zombie-check")
        self._transition("d-zombie-check", "failed_delivery")
        # Lease still held (not released)

        result = self.core.check_terminal("T2", "d-new-dispatch")
        self.assertFalse(result["available"])
        self.assertEqual(result.get("mismatch"), ZOMBIE_LEASE)
        self.assertIn("zombie_lease", result["reason"])
        self.assertIn("failed_delivery", result["reason"])

    def test_zombie_lease_includes_dispatch_state(self):
        gen = self._acquire("T2", "d-zombie-complete")
        self._transition("d-zombie-complete", "completed")

        result = self.core.check_terminal("T2", "d-other")
        self.assertEqual(result.get("dispatch_state"), "completed")
        self.assertEqual(result.get("lease_state"), "leased")

    def test_zombie_lease_message_is_operator_readable(self):
        gen = self._acquire("T2", "d-zombie-msg")
        self._transition("d-zombie-msg", "expired")

        result = self.core.check_terminal("T2", "d-new")
        self.assertIn("mismatch_message", result)
        self.assertIsInstance(result["mismatch_message"], str)
        self.assertGreater(len(result["mismatch_message"]), 20)

    def test_normal_lease_still_blocks_without_mismatch(self):
        """Active lease with live dispatch returns leased reason, no mismatch."""
        gen = self._acquire("T2", "d-active-lease")
        self._transition("d-active-lease", "delivering")

        result = self.core.check_terminal("T2", "d-other")
        self.assertFalse(result["available"])
        self.assertIsNone(result.get("mismatch"))
        self.assertIn("leased:", result["reason"])

    def test_idle_terminal_always_available(self):
        result = self.core.check_terminal("T2", "d-any")
        self.assertTrue(result["available"])
        self.assertEqual(result["reason"], "idle")

    def test_same_dispatch_always_available(self):
        gen = self._acquire("T2", "d-same")
        self._transition("d-same", "delivering")

        result = self.core.check_terminal("T2", "d-same")
        self.assertTrue(result["available"])
        self.assertEqual(result["reason"], "same_dispatch")


# ---------------------------------------------------------------------------
# TestReconcileIdempotency
# ---------------------------------------------------------------------------

class TestReconcileIdempotency(_Base):
    """Repeated reconcile runs produce no state changes (read-only)."""

    def test_repeated_reconcile_does_not_change_lease_state(self):
        gen = self._acquire("T2", "d-idem")
        self._transition("d-idem", "failed_delivery")

        # Run reconciler twice
        diag1 = self.reconciler.reconcile()
        diag2 = self.reconciler.reconcile()

        # Both detect the same mismatch
        self.assertEqual(len(diag1.mismatches), len(diag2.mismatches))

        # Lease state is unchanged
        from runtime_coordination import get_lease
        with get_connection(self.state_dir) as conn:
            lease = get_lease(conn, "T2")
        self.assertEqual(lease["state"], "leased")

    def test_repeated_check_terminal_does_not_change_lease(self):
        gen = self._acquire("T2", "d-idem2")
        self._transition("d-idem2", "completed")

        result1 = self.core.check_terminal("T2", "d-new")
        result2 = self.core.check_terminal("T2", "d-new")

        self.assertEqual(result1["mismatch"], result2["mismatch"])

        with get_connection(self.state_dir) as conn:
            lease = get_lease(conn, "T2")
        self.assertEqual(lease["state"], "leased")


# ---------------------------------------------------------------------------
# TestDiagnosticOutput
# ---------------------------------------------------------------------------

class TestDiagnosticOutput(_Base):
    """Diagnostic result is serializable and has readable summary."""

    def test_to_dict_serializable(self):
        self._acquire("T2", "d-serial")
        self._transition("d-serial", "completed")

        diag = self.reconciler.reconcile()
        d = diag.to_dict()
        # Must be JSON-serializable
        json_str = json.dumps(d)
        self.assertIsInstance(json_str, str)
        self.assertIn("mismatches", d)
        self.assertIn("has_blocking", d)
        self.assertIn("is_clean", d)

    def test_summary_contains_mismatch_info(self):
        self._acquire("T2", "d-summary")
        self._transition("d-summary", "timed_out")

        diag = self.reconciler.reconcile()
        summary = diag.summary()
        self.assertIn("BLOCKING", summary)
        self.assertIn(ZOMBIE_LEASE, summary)

    def test_terminal_count_correct(self):
        # Schema initializes T1, T2, T3 — terminal_count reflects all DB rows
        self._acquire("T1", "d-t1")
        self._acquire("T2", "d-t2")

        diag = self.reconciler.reconcile()
        self.assertGreaterEqual(diag.terminal_count, 2)

    def test_load_reconciler_factory(self):
        r = load_reconciler(self.state_dir)
        self.assertIsInstance(r, RuntimeStateReconciler)
        diag = r.reconcile()
        self.assertIsInstance(diag, RuntimeStateDiagnostic)


# ---------------------------------------------------------------------------
# TestMultipleTerminals
# ---------------------------------------------------------------------------

class TestMultipleTerminals(_Base):
    """Divergent state on multiple terminals produces independent diagnostics."""

    def test_two_zombie_leases_detected_independently(self):
        self._acquire("T1", "d-z1")
        self._acquire("T2", "d-z2")
        self._transition("d-z1", "completed")
        self._transition("d-z2", "failed_delivery")

        diag = self.reconciler.reconcile()
        zombies = [m for m in diag.mismatches if m.mismatch_type == ZOMBIE_LEASE]
        terminals = {z.terminal_id for z in zombies}
        self.assertIn("T1", terminals)
        self.assertIn("T2", terminals)
        self.assertEqual(len(zombies), 2)

    def test_one_clean_one_zombie(self):
        gen1 = self._acquire("T1", "d-clean")
        self._acquire("T2", "d-zombie")
        self._transition("d-clean", "delivering")  # T1 still active
        self._transition("d-zombie", "completed")  # T2 zombie

        diag = self.reconciler.reconcile()
        zombies = [m for m in diag.mismatches if m.mismatch_type == ZOMBIE_LEASE]
        self.assertEqual(len(zombies), 1)
        self.assertEqual(zombies[0].terminal_id, "T2")


if __name__ == "__main__":
    unittest.main()
