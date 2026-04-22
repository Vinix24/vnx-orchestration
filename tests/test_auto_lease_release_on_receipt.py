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


if __name__ == "__main__":
    unittest.main()
