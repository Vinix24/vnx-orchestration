#!/usr/bin/env python3
"""
Tests for lease_manager.py (PR-2)

Coverage:
  - LeaseManager.acquire / renew / release / expire / recover
  - Generation guard: stale renew and stale release rejected
  - Double-claim prevention (acquire on non-idle terminal)
  - expire_stale() batch TTL enforcement
  - project() / project_to_file() canonical projection
  - find_available() routing helper
  - is_expired_by_ttl() pure timestamp check
  - canonical_lease_active() env flag helper
  - _gc_expired_leases guarded by VNX_CANONICAL_LEASE_ACTIVE
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (
    InvalidTransitionError,
    get_connection,
    get_events,
    init_schema,
    register_dispatch,
)
from lease_manager import (
    LeaseManager,
    LeaseResult,
    canonical_lease_active,
    load_manager,
)


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------

class _LMTestCase(unittest.TestCase):
    """Creates a temp state dir, inits schema, provides a LeaseManager."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self._tmpdir.name
        init_schema(self.state_dir)
        self.mgr = LeaseManager(self.state_dir, auto_init=False)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _events(self, terminal_id: str, event_type: str | None = None) -> list:
        with get_connection(self.state_dir) as conn:
            return get_events(conn, entity_id=terminal_id, entity_type="lease",
                              event_type=event_type)

    def _reg(self, dispatch_id: str, terminal_id: str = "T1") -> None:
        """Register a dispatch row so terminal_leases FK is satisfied."""
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal_id)
            conn.commit()

    def acquire(self, terminal_id: str, dispatch_id: str, **kwargs):
        """Register dispatch then acquire lease in one call."""
        self._reg(dispatch_id, terminal_id)
        return self.mgr.acquire(terminal_id, dispatch_id=dispatch_id, **kwargs)


# ---------------------------------------------------------------------------
# Acquire
# ---------------------------------------------------------------------------

class TestAcquire(_LMTestCase):
    def test_acquire_idle_terminal(self):
        result = self.acquire("T1", dispatch_id="d-001")
        self.assertEqual(result.state, "leased")
        self.assertEqual(result.dispatch_id, "d-001")
        self.assertGreater(result.generation, 1)

    def test_acquire_increments_generation(self):
        r1 = self.acquire("T1", dispatch_id="d-001")
        gen_after_acquire = r1.generation
        # Release then re-acquire; generation should keep incrementing
        self.mgr.release("T1", generation=gen_after_acquire)
        r2 = self.acquire("T1", dispatch_id="d-002")
        self.assertGreater(r2.generation, gen_after_acquire)

    def test_acquire_emits_lease_acquired_event(self):
        self.acquire("T2", dispatch_id="d-002")
        events = self._events("T2", "lease_acquired")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["to_state"], "leased")

    def test_acquire_sets_expires_at(self):
        result = self.acquire("T1", dispatch_id="d-001", lease_seconds=300)
        self.assertIsNotNone(result.expires_at)
        expires = datetime.fromisoformat(result.expires_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        # Should expire roughly 300s from now (allow ±5s slop)
        self.assertAlmostEqual(
            (expires - now).total_seconds(), 300, delta=5
        )

    def test_acquire_non_idle_raises(self):
        self.acquire("T1", dispatch_id="d-001")
        with self.assertRaises(InvalidTransitionError):
            self.acquire("T1", dispatch_id="d-002")

    def test_acquire_double_claim_prevention(self):
        """Two simultaneous acquire calls cannot both succeed for the same terminal."""
        self.acquire("T3", dispatch_id="d-x")
        with self.assertRaises(InvalidTransitionError):
            self.acquire("T3", dispatch_id="d-y")


# ---------------------------------------------------------------------------
# Renew
# ---------------------------------------------------------------------------

class TestRenew(_LMTestCase):
    def setUp(self):
        super().setUp()
        self.r = self.acquire("T1", dispatch_id="d-001")
        self.gen = self.r.generation

    def test_renew_updates_heartbeat(self):
        result = self.mgr.renew("T1", generation=self.gen)
        self.assertIsNotNone(result.last_heartbeat_at)

    def test_renew_extends_expiry(self):
        r1 = self.mgr.renew("T1", generation=self.gen, lease_seconds=300)
        r2 = self.mgr.renew("T1", generation=self.gen, lease_seconds=600)
        e1 = datetime.fromisoformat(r1.expires_at.replace("Z", "+00:00"))
        e2 = datetime.fromisoformat(r2.expires_at.replace("Z", "+00:00"))
        self.assertGreater(e2, e1)

    def test_renew_emits_lease_renewed_event(self):
        self.mgr.renew("T1", generation=self.gen)
        events = self._events("T1", "lease_renewed")
        self.assertEqual(len(events), 1)

    def test_renew_stale_generation_rejected(self):
        stale_gen = self.gen - 1
        with self.assertRaises(ValueError) as ctx:
            self.mgr.renew("T1", generation=stale_gen)
        self.assertIn("generation mismatch", str(ctx.exception).lower())

    def test_renew_wrong_generation_rejected(self):
        with self.assertRaises(ValueError):
            self.mgr.renew("T1", generation=self.gen + 999)

    def test_renew_not_leased_raises(self):
        self.mgr.release("T1", generation=self.gen)
        with self.assertRaises(InvalidTransitionError):
            self.mgr.renew("T1", generation=self.gen)


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------

class TestRelease(_LMTestCase):
    def setUp(self):
        super().setUp()
        self.r = self.acquire("T1", dispatch_id="d-001")
        self.gen = self.r.generation

    def test_release_returns_idle(self):
        result = self.mgr.release("T1", generation=self.gen)
        self.assertEqual(result.state, "idle")
        self.assertIsNone(result.dispatch_id)

    def test_release_emits_events(self):
        self.mgr.release("T1", generation=self.gen)
        # Should have lease_released + lease_returned_idle events
        all_events = self._events("T1")
        event_types = {e["event_type"] for e in all_events}
        self.assertIn("lease_released", event_types)
        self.assertIn("lease_returned_idle", event_types)

    def test_release_stale_generation_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.mgr.release("T1", generation=self.gen - 1)
        self.assertIn("generation mismatch", str(ctx.exception).lower())

    def test_release_wrong_generation_rejected(self):
        with self.assertRaises(ValueError):
            self.mgr.release("T1", generation=self.gen + 999)

    def test_release_then_reacquire(self):
        self.mgr.release("T1", generation=self.gen)
        result = self.acquire("T1", dispatch_id="d-002")
        self.assertEqual(result.state, "leased")
        self.assertEqual(result.dispatch_id, "d-002")


# ---------------------------------------------------------------------------
# Expire
# ---------------------------------------------------------------------------

class TestExpire(_LMTestCase):
    def test_expire_leased_terminal(self):
        r = self.acquire("T2", dispatch_id="d-001")
        result = self.mgr.expire("T2", reason="TTL test")
        self.assertEqual(result.state, "expired")

    def test_expire_emits_lease_expired_event(self):
        self.acquire("T2", dispatch_id="d-001")
        self.mgr.expire("T2", reason="test expiry")
        events = self._events("T2", "lease_expired")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "test expiry")

    def test_expire_idle_terminal_raises(self):
        with self.assertRaises(InvalidTransitionError):
            self.mgr.expire("T3")

    def test_expire_already_expired_raises(self):
        self.acquire("T2", dispatch_id="d-001")
        self.mgr.expire("T2")
        with self.assertRaises(InvalidTransitionError):
            self.mgr.expire("T2")


# ---------------------------------------------------------------------------
# Recover
# ---------------------------------------------------------------------------

class TestRecover(_LMTestCase):
    def _setup_expired(self, terminal_id="T1"):
        self.acquire(terminal_id, dispatch_id="d-exp")
        self.mgr.expire(terminal_id, reason="setup for recover test")

    def test_recover_expired_to_idle(self):
        self._setup_expired("T1")
        result = self.mgr.recover("T1")
        self.assertEqual(result.state, "idle")
        self.assertIsNone(result.dispatch_id)

    def test_recover_emits_events(self):
        self._setup_expired("T1")
        self.mgr.recover("T1")
        all_events = self._events("T1")
        event_types = {e["event_type"] for e in all_events}
        self.assertIn("lease_recovering", event_types)
        self.assertIn("lease_recovered", event_types)

    def test_recover_idle_terminal_raises(self):
        with self.assertRaises(InvalidTransitionError):
            self.mgr.recover("T2")

    def test_recover_leased_terminal_raises(self):
        self.acquire("T3", dispatch_id="d-001")
        with self.assertRaises(InvalidTransitionError):
            self.mgr.recover("T3")

    def test_recover_then_reacquire(self):
        self._setup_expired("T2")
        self.mgr.recover("T2")
        result = self.acquire("T2", dispatch_id="d-new")
        self.assertEqual(result.state, "leased")


# ---------------------------------------------------------------------------
# TTL helpers
# ---------------------------------------------------------------------------

class TestTTLHelpers(_LMTestCase):
    def test_is_expired_by_ttl_false_for_fresh_lease(self):
        self.acquire("T1", dispatch_id="d-001", lease_seconds=600)
        self.assertFalse(self.mgr.is_expired_by_ttl("T1"))

    def test_is_expired_by_ttl_false_for_idle(self):
        self.assertFalse(self.mgr.is_expired_by_ttl("T1"))

    def test_is_expired_by_ttl_true_for_past_expiry(self):
        # Acquire with 1-second TTL, then force-set expires_at to past via DB
        r = self.acquire("T1", dispatch_id="d-001", lease_seconds=600)
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE terminal_leases SET expires_at = ? WHERE terminal_id = 'T1'",
                (past,),
            )
            conn.commit()
        self.assertTrue(self.mgr.is_expired_by_ttl("T1"))

    def test_expire_stale_batch(self):
        # Acquire T1 and T2; expire T1's TTL manually, leave T2 fresh
        self.acquire("T1", dispatch_id="d-001", lease_seconds=600)
        self.acquire("T2", dispatch_id="d-002", lease_seconds=600)
        past = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE terminal_leases SET expires_at = ? WHERE terminal_id = 'T1'",
                (past,),
            )
            conn.commit()

        expired = self.mgr.expire_stale(reason="batch TTL test")
        self.assertIn("T1", expired)
        self.assertNotIn("T2", expired)

        t1 = self.mgr.get("T1")
        t2 = self.mgr.get("T2")
        self.assertEqual(t1.state, "expired")
        self.assertEqual(t2.state, "leased")

    def test_expire_stale_idempotent(self):
        """expire_stale() called twice does not raise or duplicate events."""
        self.acquire("T3", dispatch_id="d-003", lease_seconds=600)
        past = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE terminal_leases SET expires_at = ? WHERE terminal_id = 'T3'",
                (past,),
            )
            conn.commit()

        first = self.mgr.expire_stale()
        second = self.mgr.expire_stale()  # terminal now in 'expired', not 'leased'
        self.assertIn("T3", first)
        self.assertEqual(second, [])  # already expired, skipped

    def test_expire_stale_no_events_for_idle(self):
        expired = self.mgr.expire_stale()
        self.assertEqual(expired, [])


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

class TestProjection(_LMTestCase):
    def test_project_returns_schema_v1(self):
        snapshot = self.mgr.project()
        self.assertEqual(snapshot["schema_version"], 1)

    def test_project_includes_all_terminals(self):
        snapshot = self.mgr.project()
        terminals = snapshot["terminals"]
        self.assertIn("T1", terminals)
        self.assertIn("T2", terminals)
        self.assertIn("T3", terminals)

    def test_project_idle_status(self):
        snapshot = self.mgr.project()
        self.assertEqual(snapshot["terminals"]["T1"]["status"], "idle")

    def test_project_leased_becomes_working(self):
        self.acquire("T2", dispatch_id="d-proj")
        snapshot = self.mgr.project()
        self.assertEqual(snapshot["terminals"]["T2"]["status"], "working")
        self.assertEqual(snapshot["terminals"]["T2"]["claimed_by"], "d-proj")

    def test_project_expired_becomes_recovering(self):
        self.acquire("T3", dispatch_id="d-exp")
        self.mgr.expire("T3")
        snapshot = self.mgr.project()
        self.assertEqual(snapshot["terminals"]["T3"]["status"], "recovering")

    def test_project_to_file_creates_file(self):
        out = self.mgr.project_to_file()
        self.assertTrue(out.exists())
        data = json.loads(out.read_text())
        self.assertEqual(data["schema_version"], 1)
        self.assertIn("terminals", data)

    def test_project_to_file_is_valid_json(self):
        self.acquire("T1", dispatch_id="d-file")
        out = self.mgr.project_to_file()
        data = json.loads(out.read_text())
        t1 = data["terminals"]["T1"]
        self.assertEqual(t1["status"], "working")
        self.assertEqual(t1["claimed_by"], "d-file")

    def test_project_to_file_idempotent(self):
        out1 = self.mgr.project_to_file()
        out2 = self.mgr.project_to_file()
        self.assertEqual(out1, out2)
        data = json.loads(out2.read_text())
        self.assertIn("terminals", data)

    def test_project_reflects_release(self):
        r = self.acquire("T2", dispatch_id="d-rel")
        self.mgr.release("T2", generation=r.generation)
        snapshot = self.mgr.project()
        self.assertEqual(snapshot["terminals"]["T2"]["status"], "idle")
        self.assertNotIn("claimed_by", snapshot["terminals"]["T2"])


# ---------------------------------------------------------------------------
# Routing helper
# ---------------------------------------------------------------------------

class TestFindAvailable(_LMTestCase):
    def test_find_available_returns_idle_terminal(self):
        tid = self.mgr.find_available()
        self.assertIn(tid, {"T1", "T2", "T3"})

    def test_find_available_excludes_leased(self):
        self.acquire("T1", dispatch_id="d-001")
        self.acquire("T2", dispatch_id="d-002")
        tid = self.mgr.find_available()
        self.assertEqual(tid, "T3")

    def test_find_available_returns_none_when_all_busy(self):
        self.acquire("T1", dispatch_id="d-001")
        self.acquire("T2", dispatch_id="d-002")
        self.acquire("T3", dispatch_id="d-003")
        self.assertIsNone(self.mgr.find_available())

    def test_find_available_prefer_track(self):
        tid = self.mgr.find_available(prefer_track="B")
        self.assertEqual(tid, "T2")

    def test_find_available_prefer_track_fallback(self):
        self.acquire("T2", dispatch_id="d-002")
        tid = self.mgr.find_available(prefer_track="B")
        # T2 is busy; fallback to any idle terminal
        self.assertIn(tid, {"T1", "T3"})


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

class TestQueryHelpers(_LMTestCase):
    def test_get_existing_terminal(self):
        result = self.mgr.get("T1")
        self.assertIsNotNone(result)
        self.assertEqual(result.terminal_id, "T1")

    def test_get_nonexistent_terminal(self):
        result = self.mgr.get("T99")
        self.assertIsNone(result)

    def test_list_all_returns_three_terminals(self):
        results = self.mgr.list_all()
        tids = {r.terminal_id for r in results}
        self.assertEqual(tids, {"T1", "T2", "T3"})

    def test_list_all_returns_lease_results(self):
        results = self.mgr.list_all()
        for r in results:
            self.assertIsInstance(r, LeaseResult)


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

class TestFactory(_LMTestCase):
    def test_load_manager_returns_instance(self):
        mgr = load_manager(self.state_dir)
        self.assertIsInstance(mgr, LeaseManager)

    def test_canonical_lease_active_default_false(self):
        env_bak = os.environ.pop("VNX_CANONICAL_LEASE_ACTIVE", None)
        try:
            self.assertFalse(canonical_lease_active())
        finally:
            if env_bak is not None:
                os.environ["VNX_CANONICAL_LEASE_ACTIVE"] = env_bak

    def test_canonical_lease_active_set_true(self):
        os.environ["VNX_CANONICAL_LEASE_ACTIVE"] = "1"
        try:
            self.assertTrue(canonical_lease_active())
        finally:
            del os.environ["VNX_CANONICAL_LEASE_ACTIVE"]

    def test_canonical_lease_active_set_zero(self):
        os.environ["VNX_CANONICAL_LEASE_ACTIVE"] = "0"
        try:
            self.assertFalse(canonical_lease_active())
        finally:
            del os.environ["VNX_CANONICAL_LEASE_ACTIVE"]


# ---------------------------------------------------------------------------
# Shadow writer GC guard (integration with terminal_state_shadow.py)
# ---------------------------------------------------------------------------

class TestShadowGCGuard(unittest.TestCase):
    """Verify that _gc_expired_leases is skipped when VNX_CANONICAL_LEASE_ACTIVE=1."""

    def setUp(self):
        from terminal_state_shadow import _gc_expired_leases
        self._gc = _gc_expired_leases

    def test_gc_skipped_when_canonical_active(self):
        os.environ["VNX_CANONICAL_LEASE_ACTIVE"] = "1"
        try:
            from datetime import datetime, timedelta, timezone
            past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            terminals = {
                "T1": {"claimed_by": "d-001", "lease_expires_at": past, "status": "working"}
            }
            count = self._gc(terminals)
            self.assertEqual(count, 0)
            # Record must remain unchanged — canonical path owns expiry
            self.assertEqual(terminals["T1"]["claimed_by"], "d-001")
            self.assertEqual(terminals["T1"]["status"], "working")
        finally:
            del os.environ["VNX_CANONICAL_LEASE_ACTIVE"]

    def test_gc_runs_when_canonical_inactive(self):
        os.environ.pop("VNX_CANONICAL_LEASE_ACTIVE", None)
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        terminals = {
            "T1": {"claimed_by": "d-001", "lease_expires_at": past, "status": "working"}
        }
        count = self._gc(terminals)
        self.assertEqual(count, 1)
        self.assertIsNone(terminals["T1"]["claimed_by"])
        self.assertEqual(terminals["T1"]["status"], "idle")


if __name__ == "__main__":
    unittest.main()
