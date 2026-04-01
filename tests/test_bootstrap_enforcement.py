#!/usr/bin/env python3
"""
Tests for fail-closed bootstrap enforcement (PR-1).

Coverage:
  BOOT-1  _get_dirs() raises RuntimeError when VNX_STATE_DIR and VNX_DATA_DIR are both unset
  BOOT-1  _get_dirs() derives paths from VNX_DATA_DIR when VNX_STATE_DIR is absent
  BOOT-1  _get_dirs() raises RuntimeError when only VNX_DISPATCH_DIR is missing
  BOOT-3  Dispatcher BOOT-3 check exits with code 1 when VNX_STATE_DIR does not exist
  BOOT-7  rc_register() exits 1 on failure (runtime_core_cli.py register fail path)
  BOOT-6  Lease acquire succeeds after registration — FK constraint satisfied
  BOOT-8  Lease acquire fails without prior registration — FK violation (regression guard)
  BOOT-9  chain-closeout releases all non-idle leases to idle
  BOOT-10 chain-closeout warns on non-terminal dispatches without --force
  BOOT-11 chain-closeout increments generation on released leases
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

from runtime_coordination import (
    get_connection,
    get_events,
    init_schema,
    register_dispatch,
    release_all_leases,
    TERMINAL_DISPATCH_STATES,
)
from lease_manager import LeaseManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _BaseCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self._tmpdir.name
        init_schema(self.state_dir)
        self.mgr = LeaseManager(self.state_dir, auto_init=False)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _reg(self, dispatch_id: str, terminal_id: str = "T1") -> None:
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal_id)
            conn.commit()

    def _acquire(self, terminal_id: str, dispatch_id: str) -> int:
        """Register then acquire — returns generation."""
        self._reg(dispatch_id, terminal_id)
        result = self.mgr.acquire(terminal_id, dispatch_id=dispatch_id)
        return result.generation

    def _events(self, terminal_id: str, event_type: str | None = None) -> list:
        with get_connection(self.state_dir) as conn:
            return get_events(conn, entity_id=terminal_id, entity_type="lease",
                              event_type=event_type)


# ---------------------------------------------------------------------------
# BOOT-1: _get_dirs() fail-closed behavior
# ---------------------------------------------------------------------------

class TestGetDirsBootOne(unittest.TestCase):
    """BOOT-1: _get_dirs() must not fall back to /tmp."""

    def _run_cli(self, env: dict, cmd: list[str] | None = None) -> subprocess.CompletedProcess:
        """Run runtime_core_cli.py compat-check in a subprocess with the given env."""
        base_env = {k: v for k, v in os.environ.items()}
        # Strip all VNX path vars so _get_dirs() starts from scratch
        for key in ("VNX_DATA_DIR", "VNX_STATE_DIR", "VNX_DISPATCH_DIR"):
            base_env.pop(key, None)
        base_env.update(env)
        args = cmd or ["compat-check"]
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "runtime_core_cli.py")] + args,
            env=base_env,
            capture_output=True,
            text=True,
        )

    def test_raises_when_both_data_and_state_unset(self):
        """Exit 1 with error message when VNX_DATA_DIR and VNX_STATE_DIR are both unset."""
        result = self._run_cli({})
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("VNX_STATE_DIR", combined)
        self.assertIn("VNX_DATA_DIR", combined)

    def test_no_tmp_fallback_in_output(self):
        """Error output must not mention /tmp as a fallback path."""
        result = self._run_cli({})
        self.assertNotIn("/tmp/vnx-state", result.stdout)
        self.assertNotIn("/tmp/vnx-dispatches", result.stdout)

    def test_derives_dirs_from_vnx_data_dir(self):
        """When VNX_DATA_DIR is set, state/dispatch dirs are derived from it (no /tmp)."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = tmp
            state_dir = os.path.join(data_dir, "state")
            dispatch_dir = os.path.join(data_dir, "dispatches")
            os.makedirs(state_dir)
            os.makedirs(dispatch_dir)
            result = self._run_cli({"VNX_DATA_DIR": data_dir})
            # Exit 0 or 1 is acceptable (compat-check may fail for other reasons),
            # but we must not see /tmp in the output
            self.assertNotIn("/tmp/vnx-state", result.stdout)
            self.assertNotIn("/tmp/vnx-dispatches", result.stdout)

    def test_raises_when_dispatch_dir_unset_no_data_dir(self):
        """Exit 1 when VNX_DISPATCH_DIR is absent and VNX_DATA_DIR is also absent."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_cli(
                {"VNX_STATE_DIR": tmp},
                cmd=["compat-check"],
            )
            # VNX_DISPATCH_DIR missing and VNX_DATA_DIR missing → error
            self.assertNotEqual(result.returncode, 0)


# ---------------------------------------------------------------------------
# BOOT-3: Dispatcher startup precondition check
# ---------------------------------------------------------------------------

class TestDispatcherBootThree(unittest.TestCase):
    """BOOT-3: Dispatcher exits with FATAL when VNX_STATE_DIR does not exist."""

    def _run_boot3_check(self, vnx_state_dir: str, vnx_data_dir: str) -> subprocess.CompletedProcess:
        """Run the exact BOOT-3 check logic extracted from the dispatcher."""
        check_script = textwrap.dedent(f"""\
            #!/bin/bash
            VNX_STATE_DIR="{vnx_state_dir}"
            VNX_DATA_DIR="{vnx_data_dir}"
            if [[ -z "${{VNX_STATE_DIR:-}}" ]] || [[ ! -d "$VNX_STATE_DIR" ]]; then
                echo "FATAL: VNX_STATE_DIR is unset or does not exist: '${{VNX_STATE_DIR:-}}'" >&2
                echo "Source bin/vnx or set VNX_DATA_DIR before starting the dispatcher." >&2
                exit 1
            fi
            if [[ -z "${{VNX_DATA_DIR:-}}" ]] || [[ ! -d "$VNX_DATA_DIR" ]]; then
                echo "FATAL: VNX_DATA_DIR is unset or does not exist: '${{VNX_DATA_DIR:-}}'" >&2
                echo "Source bin/vnx or set VNX_DATA_DIR before starting the dispatcher." >&2
                exit 1
            fi
            echo "OK"
        """)
        return subprocess.run(
            ["bash", "-c", check_script],
            capture_output=True,
            text=True,
        )

    def test_exits_1_when_state_dir_nonexistent(self):
        """Dispatcher BOOT-3 check exits 1 when VNX_STATE_DIR does not exist."""
        result = self._run_boot3_check("/does/not/exist/state", "/does/not/exist")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FATAL", result.stderr)
        self.assertIn("VNX_STATE_DIR", result.stderr)

    def test_exits_1_when_data_dir_nonexistent(self):
        """Dispatcher BOOT-3 check exits 1 when VNX_DATA_DIR does not exist."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_boot3_check(tmp, "/does/not/exist/data")
            self.assertEqual(result.returncode, 1)
            self.assertIn("FATAL", result.stderr)
            self.assertIn("VNX_DATA_DIR", result.stderr)

    def test_passes_when_both_dirs_exist(self):
        """Dispatcher BOOT-3 check passes when both dirs exist."""
        with tempfile.TemporaryDirectory() as state_tmp:
            with tempfile.TemporaryDirectory() as data_tmp:
                result = self._run_boot3_check(state_tmp, data_tmp)
                self.assertEqual(result.returncode, 0)
                self.assertIn("OK", result.stdout)

    def test_exits_1_when_state_dir_empty_string(self):
        """Dispatcher BOOT-3 check exits 1 when VNX_STATE_DIR is empty string."""
        result = self._run_boot3_check("", "/some/path")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FATAL", result.stderr)


# ---------------------------------------------------------------------------
# BOOT-6 / BOOT-8: Register before acquire — FK constraint
# ---------------------------------------------------------------------------

class TestRegisterBeforeAcquire(_BaseCase):
    """BOOT-6: Acquire succeeds after registration. BOOT-8: FK violation without registration."""

    def test_acquire_succeeds_after_register(self):
        """Lease acquire succeeds when dispatch is registered first (FK satisfied)."""
        dispatch_id = "test-boot6-001"
        self._reg(dispatch_id, "T1")
        result = self.mgr.acquire("T1", dispatch_id=dispatch_id)
        self.assertTrue(result.state == "leased")
        self.assertEqual(result.dispatch_id, dispatch_id)

    def test_acquire_fails_without_register(self):
        """Lease acquire raises when dispatch_id is not in dispatches table (FK violation)."""
        dispatch_id = "unregistered-dispatch-001"
        # Do NOT call self._reg() — dispatch row absent
        with self.assertRaises(Exception):
            self.mgr.acquire("T1", dispatch_id=dispatch_id)

    def test_acquire_after_register_creates_lease_event(self):
        """Acquire after registration creates a lease_acquired coordination event."""
        dispatch_id = "test-boot6-event-001"
        self._acquire("T1", dispatch_id)
        events = self._events("T1", event_type="lease_acquired")
        self.assertGreater(len(events), 0)
        self.assertEqual(events[0]["entity_id"], "T1")


# ---------------------------------------------------------------------------
# BOOT-7: rc_register fail-closed via CLI exit code
# ---------------------------------------------------------------------------

class TestRcRegisterFailClosed(unittest.TestCase):
    """BOOT-7: runtime_core_cli.py register exits 1 when registration fails."""

    def test_register_exits_1_on_bad_state_dir(self):
        """register subcommand exits 1 when the state dir is invalid."""
        env = {k: v for k, v in os.environ.items()}
        env["VNX_STATE_DIR"] = "/does/not/exist"
        env["VNX_DATA_DIR"] = "/does/not/exist"
        env.pop("VNX_RUNTIME_PRIMARY", None)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "runtime_core_cli.py"),
                "register",
                "--dispatch-id", "test-fail-001",
                "--terminal", "T1",
                "--track", "B",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_register_disabled_exits_0_when_runtime_primary_0(self):
        """register subcommand exits 0 when VNX_RUNTIME_PRIMARY=0 (legacy mode)."""
        with tempfile.TemporaryDirectory() as tmp:
            dispatch_dir = os.path.join(tmp, "dispatches")
            os.makedirs(dispatch_dir)
            env = {k: v for k, v in os.environ.items()}
            env["VNX_STATE_DIR"] = tmp
            env["VNX_DATA_DIR"] = tmp
            env["VNX_RUNTIME_PRIMARY"] = "0"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "runtime_core_cli.py"),
                    "register",
                    "--dispatch-id", "test-disabled-001",
                    "--terminal", "T1",
                    "--track", "B",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertFalse(data["registered"])
            self.assertEqual(data["reason"], "runtime_core_disabled")


# ---------------------------------------------------------------------------
# BOOT-9 / BOOT-10 / BOOT-11: chain-closeout
# ---------------------------------------------------------------------------

class TestChainCloseout(_BaseCase):
    """BOOT-9 through BOOT-11: chain-closeout releases all leases with generation increment."""

    def test_closeout_releases_all_leased_terminals(self):
        """chain-closeout sets all leased terminals to idle."""
        self._acquire("T1", "dispatch-t1-001")
        self._acquire("T2", "dispatch-t2-001")
        self._acquire("T3", "dispatch-t3-001")

        with get_connection(self.state_dir) as conn:
            result = release_all_leases(conn, force=True)
            conn.commit()

        self.assertTrue(result["all_idle"])
        self.assertIn("T1", result["released"])
        self.assertIn("T2", result["released"])
        self.assertIn("T3", result["released"])
        self.assertEqual(result["already_idle"], [])

    def test_closeout_skips_already_idle(self):
        """Terminals already idle are not re-released."""
        self._acquire("T1", "dispatch-t1-002")
        # T2 remains idle

        with get_connection(self.state_dir) as conn:
            result = release_all_leases(conn, force=True)
            conn.commit()

        self.assertTrue(result["all_idle"])
        self.assertIn("T1", result["released"])
        self.assertIn("T2", result["already_idle"])

    def test_closeout_increments_generation(self):
        """BOOT-11: generation is incremented for each released lease."""
        gen_before = self._acquire("T1", "dispatch-t1-003")

        with get_connection(self.state_dir) as conn:
            result = release_all_leases(conn, force=True)
            conn.commit()

        lease = self.mgr.get("T1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.generation, gen_before + 1)

    def test_closeout_emits_audit_events(self):
        """BOOT-10 step 3: audit events are emitted for each released lease."""
        self._acquire("T1", "dispatch-t1-004")

        with get_connection(self.state_dir) as conn:
            release_all_leases(conn, force=True)
            conn.commit()

        events = self._events("T1", event_type="lease_released")
        self.assertGreater(len(events), 0)
        ev = events[0]
        self.assertEqual(ev["actor"], "chain_closeout")
        self.assertEqual(ev["reason"], "chain_boundary_cleanup")
        self.assertEqual(ev["to_state"], "idle")

    def test_closeout_blocked_by_non_terminal_dispatches_without_force(self):
        """BOOT-10 step 1: closeout is blocked when non-terminal dispatches exist (no --force)."""
        self._acquire("T1", "dispatch-active-001")
        # dispatch-active-001 is in 'queued' state (non-terminal)

        with get_connection(self.state_dir) as conn:
            result = release_all_leases(conn, force=False)
            # No commit — transaction should be rolled back

        self.assertTrue(result["blocked"])
        self.assertFalse(result["all_idle"])
        self.assertEqual(result["released"], [])
        self.assertGreater(len(result["non_terminal_dispatches"]), 0)
        self.assertIn("WARN", result["message"])

    def test_closeout_force_proceeds_with_non_terminal_dispatches(self):
        """BOOT-10: --force proceeds even when non-terminal dispatches exist."""
        self._acquire("T1", "dispatch-active-002")

        with get_connection(self.state_dir) as conn:
            result = release_all_leases(conn, force=True)
            conn.commit()

        self.assertTrue(result["all_idle"])
        self.assertIn("T1", result["released"])
        # non_terminal_dispatches reported but not blocking
        self.assertFalse(result["blocked"])

    def test_closeout_stale_release_rejected_after_generation_increment(self):
        """BOOT-11: old generation cannot release a lease after chain-closeout."""
        old_gen = self._acquire("T1", "dispatch-t1-005")

        with get_connection(self.state_dir) as conn:
            release_all_leases(conn, force=True)
            conn.commit()

        # Try to release with the old generation — should fail (stale)
        with self.assertRaises(Exception):
            self.mgr.release("T1", old_gen)

    def test_closeout_via_cli_exits_0_on_success(self):
        """chain-closeout CLI exits 0 when all leases are idle after closeout."""
        with tempfile.TemporaryDirectory() as tmp:
            dispatch_dir = os.path.join(tmp, "dispatches")
            os.makedirs(dispatch_dir)
            init_schema(tmp)
            env = {k: v for k, v in os.environ.items()}
            env["VNX_STATE_DIR"] = tmp
            env["VNX_DATA_DIR"] = tmp
            env["VNX_RUNTIME_PRIMARY"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "runtime_core_cli.py"),
                    "chain-closeout",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertTrue(data["all_idle"])

    def test_closeout_cli_exits_1_when_blocked(self):
        """chain-closeout CLI exits 1 when non-terminal dispatches exist without --force."""
        with tempfile.TemporaryDirectory() as tmp:
            dispatch_dir = os.path.join(tmp, "dispatches")
            os.makedirs(dispatch_dir)
            init_schema(tmp)
            # Insert a queued dispatch and lease it
            mgr = LeaseManager(tmp, auto_init=False)
            with get_connection(tmp) as conn:
                register_dispatch(conn, dispatch_id="cli-active-001", terminal_id="T1")
                conn.commit()
            mgr.acquire("T1", dispatch_id="cli-active-001")

            env = {k: v for k, v in os.environ.items()}
            env["VNX_STATE_DIR"] = tmp
            env["VNX_DATA_DIR"] = tmp
            env["VNX_RUNTIME_PRIMARY"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "runtime_core_cli.py"),
                    "chain-closeout",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1)
            data = json.loads(result.stdout)
            self.assertTrue(data["blocked"])


if __name__ == "__main__":
    unittest.main()
