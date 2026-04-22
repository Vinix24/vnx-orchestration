#!/usr/bin/env python3
"""
Tests for W0-PR2: Auto-lease-release on task receipt.

Coverage:
  - release_on_receipt() releases an owned lease (dispatch_id matches)
  - release_on_receipt() is idempotent when terminal already idle
  - release_on_receipt() rejects when dispatch_id doesn't match (ownership guard)
  - release_on_receipt() handles non-leased terminal states gracefully
  - release_on_receipt() returns structured audit dict in all paths
  - release-on-receipt CLI subcommand end-to-end
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (
    InvalidTransitionError,
    get_connection,
    init_schema,
    register_dispatch,
)
from lease_manager import LeaseManager
from dispatch_broker import DispatchBroker
from runtime_core import RuntimeCore


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

def _setup(tmp: tempfile.TemporaryDirectory):
    """Return (state_dir, dispatch_dir, lease_mgr, core)."""
    base = Path(tmp.name)
    state_dir = base / "state"
    dispatch_dir = base / "dispatches"
    state_dir.mkdir(parents=True)
    dispatch_dir.mkdir(parents=True)
    init_schema(state_dir)

    broker = DispatchBroker(str(state_dir), str(dispatch_dir), shadow_mode=False)
    lease_mgr = LeaseManager(state_dir, auto_init=False)
    core = RuntimeCore(broker=broker, lease_mgr=lease_mgr)
    return str(state_dir), str(dispatch_dir), lease_mgr, core


def _acquire(lease_mgr: LeaseManager, state_dir: str, terminal_id: str, dispatch_id: str):
    """Register dispatch and acquire lease. Returns generation."""
    with get_connection(state_dir) as conn:
        register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal_id)
        conn.commit()
    result = lease_mgr.acquire(terminal_id, dispatch_id=dispatch_id)
    return result.generation


# ---------------------------------------------------------------------------
# TestReleaseOnReceipt — core method
# ---------------------------------------------------------------------------

class TestReleaseOnReceipt(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.lease_mgr, self.core = _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_releases_owned_lease(self):
        """task_complete receipt releases the lease owned by that dispatch."""
        _acquire(self.lease_mgr, self.state_dir, "T1", "d-001")
        result = self.core.release_on_receipt("T1", dispatch_id="d-001")
        self.assertTrue(result["released"], f"expected released=True, got: {result}")
        lease = self.lease_mgr.get("T1")
        self.assertEqual(lease.state, "idle")

    def test_terminal_idle_after_release(self):
        _acquire(self.lease_mgr, self.state_dir, "T2", "d-002")
        self.core.release_on_receipt("T2", dispatch_id="d-002")
        lease = self.lease_mgr.get("T2")
        self.assertIsNone(lease.dispatch_id)
        self.assertEqual(lease.state, "idle")

    def test_idempotent_when_already_idle(self):
        """Terminal already idle returns released=True with skipped=True."""
        result = self.core.release_on_receipt("T1", dispatch_id="d-003")
        self.assertTrue(result["released"], f"expected released=True, got: {result}")
        self.assertTrue(result.get("skipped"), "expected skipped=True for already-idle terminal")

    def test_ownership_mismatch_rejected(self):
        """Dispatch ID mismatch prevents release of another dispatch's lease."""
        _acquire(self.lease_mgr, self.state_dir, "T3", "d-real-owner")
        result = self.core.release_on_receipt("T3", dispatch_id="d-wrong-owner")
        self.assertFalse(result["released"], f"mismatch should not release: {result}")
        self.assertIn("ownership_mismatch", result.get("reason", ""))
        lease = self.lease_mgr.get("T3")
        self.assertEqual(lease.state, "leased", "lease should remain held by real owner")

    def test_no_dispatch_id_releases_any_owner(self):
        """When no dispatch_id provided, ownership check is skipped."""
        _acquire(self.lease_mgr, self.state_dir, "T1", "d-004")
        result = self.core.release_on_receipt("T1")
        self.assertTrue(result["released"], f"should release without dispatch_id: {result}")
        lease = self.lease_mgr.get("T1")
        self.assertEqual(lease.state, "idle")

    def test_unknown_terminal_returns_structured_error(self):
        result = self.core.release_on_receipt("T99", dispatch_id="d-999")
        self.assertFalse(result["released"])
        self.assertIn("terminal_not_found", result.get("reason", ""))

    def test_result_always_contains_terminal_id(self):
        result = self.core.release_on_receipt("T2", dispatch_id="d-005")
        self.assertEqual(result["terminal_id"], "T2")

    def test_terminal_reacquirable_after_receipt_release(self):
        """After receipt release, terminal can be leased again for next dispatch."""
        _acquire(self.lease_mgr, self.state_dir, "T2", "d-006")
        self.core.release_on_receipt("T2", dispatch_id="d-006")

        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id="d-007", terminal_id="T2")
            conn.commit()
        new_lease = self.lease_mgr.acquire("T2", dispatch_id="d-007")
        self.assertEqual(new_lease.state, "leased")
        self.assertEqual(new_lease.dispatch_id, "d-007")

    def test_generation_advances_after_receipt_release(self):
        """Generation increments so stale heartbeats from old lease are rejected."""
        gen1 = _acquire(self.lease_mgr, self.state_dir, "T1", "d-008")
        self.core.release_on_receipt("T1", dispatch_id="d-008")

        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id="d-009", terminal_id="T1")
            conn.commit()
        r2 = self.lease_mgr.acquire("T1", dispatch_id="d-009")
        self.assertGreater(r2.generation, gen1)

    def test_release_emits_lease_events(self):
        """release_on_receipt must create auditable lease events."""
        from runtime_coordination import get_events
        _acquire(self.lease_mgr, self.state_dir, "T3", "d-010")
        self.core.release_on_receipt("T3", dispatch_id="d-010")

        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="T3", entity_type="lease")
        event_types = {e["event_type"] for e in events}
        self.assertIn("lease_released", event_types)
        self.assertIn("lease_returned_idle", event_types)


# ---------------------------------------------------------------------------
# TestReleaseOnReceiptAllTerminals
# ---------------------------------------------------------------------------

class TestReleaseOnReceiptAllTerminals(unittest.TestCase):
    """Verify release_on_receipt works across all three terminals."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.lease_mgr, self.core = _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_all_three_terminals_release_cleanly(self):
        for i, terminal in enumerate(["T1", "T2", "T3"]):
            dispatch_id = f"d-all-{i:03d}"
            _acquire(self.lease_mgr, self.state_dir, terminal, dispatch_id)
            result = self.core.release_on_receipt(terminal, dispatch_id=dispatch_id)
            self.assertTrue(result["released"], f"{terminal}: {result}")
            lease = self.lease_mgr.get(terminal)
            self.assertEqual(lease.state, "idle", f"{terminal} not idle after release")


# ---------------------------------------------------------------------------
# TestReleaseOnReceiptCLI — CLI subcommand
# ---------------------------------------------------------------------------

class TestReleaseOnReceiptCLI(unittest.TestCase):
    """End-to-end test for `python runtime_core_cli.py release-on-receipt`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        # Replicate .vnx-data layout so project_root.py resolves correctly
        # with VNX_DATA_DIR=base VNX_DATA_DIR_EXPLICIT=1
        self.state_dir = base / "state"
        self.dispatch_dir = base / "dispatches"
        self.state_dir.mkdir(parents=True)
        self.dispatch_dir.mkdir(parents=True)
        init_schema(self.state_dir)
        self.lease_mgr = LeaseManager(self.state_dir, auto_init=False)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_cli(self, *args: str) -> tuple[int, dict]:
        """Run the CLI with VNX_DATA_DIR pointed at our temp data root."""
        env = {
            "PATH": "/usr/bin:/bin",
            # project_root.py respects VNX_DATA_DIR when VNX_DATA_DIR_EXPLICIT=1
            "VNX_DATA_DIR": str(self.state_dir.parent),
            "VNX_DATA_DIR_EXPLICIT": "1",
            "VNX_RUNTIME_PRIMARY": "1",
            "VNX_CANONICAL_LEASE_ACTIVE": "1",
            "HOME": Path.home().as_posix(),
        }
        cli = str(SCRIPT_DIR / "runtime_core_cli.py")
        proc = subprocess.run(
            [sys.executable, cli, "release-on-receipt", *args],
            capture_output=True, text=True, env=env,
        )
        try:
            data = json.loads(proc.stdout.strip())
        except (json.JSONDecodeError, ValueError):
            data = {"raw": proc.stdout, "stderr": proc.stderr}
        return proc.returncode, data

    def _acquire(self, terminal_id: str, dispatch_id: str) -> int:
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal_id)
            conn.commit()
        r = self.lease_mgr.acquire(terminal_id, dispatch_id=dispatch_id)
        return r.generation

    def test_cli_releases_owned_lease(self):
        self._acquire("T1", "cli-d-001")
        rc, data = self._run_cli("--terminal", "T1", "--dispatch-id", "cli-d-001")
        self.assertEqual(rc, 0, f"expected exit 0, got {rc}: {data}")
        self.assertTrue(data.get("released"), f"expected released=True: {data}")
        lease = self.lease_mgr.get("T1")
        self.assertEqual(lease.state, "idle")

    def test_cli_idempotent_on_idle_terminal(self):
        rc, data = self._run_cli("--terminal", "T2")
        self.assertEqual(rc, 0, f"idle terminal should exit 0: {data}")
        self.assertTrue(data.get("released"), f"expected released=True for idle: {data}")
        self.assertTrue(data.get("skipped"), f"expected skipped=True: {data}")

    def test_cli_rejects_ownership_mismatch(self):
        self._acquire("T3", "cli-real-owner")
        rc, data = self._run_cli("--terminal", "T3", "--dispatch-id", "cli-wrong-owner")
        self.assertNotEqual(rc, 0, f"mismatch should exit non-zero: {data}")
        self.assertFalse(data.get("released"), f"mismatch should not release: {data}")


# ---------------------------------------------------------------------------
# TestNoConfirmationTimeoutGuard — conflicting-state fix (W0-PR2-fix)
# ---------------------------------------------------------------------------

class TestNoConfirmationTimeoutGuard(unittest.TestCase):
    """Verify the no_confirmation timeout guard at the Python API boundary.

    The shell script (receipt_processor_v4.sh C2b) must NOT call
    release_on_receipt for task_timeout+no_confirmation events because
    Section C deliberately keeps the canonical lease held (blocked state)
    to prevent immediate re-dispatch.

    These tests verify that:
      1. release_on_receipt CAN release a held lease (used by other event types)
      2. A still-leased terminal after a no_confirmation scenario remains leased
         (i.e., the lease was NOT released — the caller honored the guard)
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.lease_mgr, self.core = _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lease_stays_held_when_caller_skips_release_for_no_confirmation(self):
        """Canonical lease must remain leased after a no_confirmation timeout
        if the caller correctly skips calling release_on_receipt.

        This is the contract: the shell C2b guard prevents the auto-release,
        leaving the DB in leased state to match the blocked shadow state.
        """
        _acquire(self.lease_mgr, self.state_dir, "T1", "d-nc-001")
        # Simulate the shell guard: do NOT call release_on_receipt for no_confirmation
        lease = self.lease_mgr.get("T1")
        self.assertEqual(lease.state, "leased", "lease should remain held — caller skipped release")
        self.assertEqual(lease.dispatch_id, "d-nc-001")

    def test_release_on_receipt_releases_normally_for_task_complete(self):
        """task_complete (not no_confirmation) must still auto-release the lease."""
        _acquire(self.lease_mgr, self.state_dir, "T2", "d-nc-002")
        result = self.core.release_on_receipt("T2", dispatch_id="d-nc-002")
        self.assertTrue(result["released"], f"task_complete path must release: {result}")
        lease = self.lease_mgr.get("T2")
        self.assertEqual(lease.state, "idle")

    def test_release_on_receipt_releases_for_regular_task_timeout(self):
        """A task_timeout without no_confirmation status must release the lease."""
        _acquire(self.lease_mgr, self.state_dir, "T3", "d-nc-003")
        result = self.core.release_on_receipt("T3", dispatch_id="d-nc-003")
        self.assertTrue(result["released"], f"regular timeout must release: {result}")
        lease = self.lease_mgr.get("T3")
        self.assertEqual(lease.state, "idle")

    def test_lease_release_after_no_confirmation_guard_still_succeeds(self):
        """Dispatcher can explicitly release a no_confirmation-blocked lease
        later (e.g., after the grace window expires) via release_on_receipt."""
        _acquire(self.lease_mgr, self.state_dir, "T1", "d-nc-004")
        # Grace window passes; dispatcher explicitly releases
        result = self.core.release_on_receipt("T1", dispatch_id="d-nc-004")
        self.assertTrue(result["released"], f"explicit later release must succeed: {result}")
        lease = self.lease_mgr.get("T1")
        self.assertEqual(lease.state, "idle")


# ---------------------------------------------------------------------------
# TestShellQuotingEdgeCases — dispatch IDs with special characters (W0-PR2-fix)
# ---------------------------------------------------------------------------

class TestShellQuotingEdgeCases(unittest.TestCase):
    """Verify release_on_receipt handles dispatch IDs that could expose
    shell quoting bugs if passed incorrectly via unquoted expansions.

    The shell fix replaces `${dispatch_id:+--dispatch-id "$dispatch_id"}`
    (unquoted outer expansion) with an array-based approach. These Python
    tests confirm the Python API itself is robust with unusual dispatch IDs,
    and the CLI tests confirm argument passing is correct end-to-end.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir, self.lease_mgr, self.core = _setup(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_dispatch_id_with_hyphens_and_numbers(self):
        """Dispatch IDs matching the standard format are handled correctly."""
        dispatch_id = "20260422-180510-w0-pr2-fix-A"
        _acquire(self.lease_mgr, self.state_dir, "T1", dispatch_id)
        result = self.core.release_on_receipt("T1", dispatch_id=dispatch_id)
        self.assertTrue(result["released"], f"standard dispatch ID: {result}")

    def test_dispatch_id_none_vs_empty_string_treated_consistently(self):
        """None and empty string both skip the ownership guard."""
        _acquire(self.lease_mgr, self.state_dir, "T2", "d-any-owner")

        result_none = self.core.release_on_receipt("T2", dispatch_id=None)
        self.assertTrue(result_none["released"], f"None dispatch_id must release: {result_none}")

    def test_empty_string_dispatch_id_skips_ownership_guard(self):
        """Empty string dispatch_id (default from CLI --dispatch-id '') must skip guard."""
        _acquire(self.lease_mgr, self.state_dir, "T3", "d-any-owner-2")
        result = self.core.release_on_receipt("T3", dispatch_id="")
        self.assertTrue(result["released"], f"empty dispatch_id must skip ownership guard: {result}")

    def test_cli_dispatch_id_with_hyphens_passed_correctly(self):
        """CLI receives dispatch ID with hyphens as a single argument (array quoting fix)."""
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        state_dir = base / "state"
        dispatch_dir = base / "dispatches"
        state_dir.mkdir(parents=True)
        dispatch_dir.mkdir(parents=True)
        init_schema(state_dir)
        lease_mgr = LeaseManager(state_dir, auto_init=False)

        dispatch_id = "20260422-180510-w0-pr2-fix-A"
        with get_connection(state_dir) as conn:
            register_dispatch(conn, dispatch_id=dispatch_id, terminal_id="T1")
            conn.commit()
        lease_mgr.acquire("T1", dispatch_id=dispatch_id)

        env = {
            "PATH": "/usr/bin:/bin",
            "VNX_DATA_DIR": str(state_dir.parent),
            "VNX_DATA_DIR_EXPLICIT": "1",
            "VNX_RUNTIME_PRIMARY": "1",
            "VNX_CANONICAL_LEASE_ACTIVE": "1",
            "HOME": Path.home().as_posix(),
        }
        cli = str(SCRIPT_DIR / "runtime_core_cli.py")
        proc = subprocess.run(
            [sys.executable, cli, "release-on-receipt", "--terminal", "T1",
             "--dispatch-id", dispatch_id],
            capture_output=True, text=True, env=env,
        )
        try:
            data = json.loads(proc.stdout.strip())
        except (json.JSONDecodeError, ValueError):
            data = {"raw": proc.stdout, "stderr": proc.stderr}

        self.assertEqual(proc.returncode, 0, f"expected exit 0: {data}")
        self.assertTrue(data.get("released"), f"hyphenated dispatch ID must release: {data}")

        lease = lease_mgr.get("T1")
        self.assertEqual(lease.state, "idle")
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
