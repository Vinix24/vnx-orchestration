#!/usr/bin/env python3
"""Tests for scripts/lib/cleanup_worker_exit.py (SUP-PR1).

Covers ten scenarios:
  A. success exit              → lease released, exited_clean, completed/
  B. failure exit              → lease released, exited_bad,  rejected/failure/
  C. killed exit               → lease released, exited_bad,  rejected/killed/
  D. timeout exit              → lease released, exited_bad,  rejected/timeout/
  E. stuck exit                → lease released, exited_bad,  rejected/stuck/
  F. idempotency               → second call is a no-op, no errors raised
  G. lease already released    → cleanup still succeeds, errors note it
  H. dispatch_file None        → file move skipped, other steps succeed
  I. LeaseManager raises       → caught, logged, surfaced via errors[]
  J. CLI invocation            → python3 cleanup_worker_exit.py exits 0
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (  # noqa: E402
    get_connection,
    init_schema,
    register_dispatch,
)
from lease_manager import LeaseManager  # noqa: E402
from worker_state_manager import WorkerStateManager  # noqa: E402

import cleanup_worker_exit as cwe  # noqa: E402


class _CWETestCase(unittest.TestCase):
    """Base — sets up a temp state_dir, dispatch_dir, and acquires a lease."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        init_schema(self.state_dir)

        self.dispatch_dir = self.tmp_path / "dispatches"
        (self.dispatch_dir / "active").mkdir(parents=True)

        self.lease_mgr = LeaseManager(self.state_dir, auto_init=False)
        self.worker_mgr = WorkerStateManager(self.state_dir, auto_init=False)

        # Register dispatch row + acquire lease + initialize worker state.
        self.dispatch_id = "d-cwe-001"
        self.terminal_id = "T1"
        with get_connection(self.state_dir) as conn:
            register_dispatch(
                conn,
                dispatch_id=self.dispatch_id,
                terminal_id=self.terminal_id,
            )
            conn.commit()
        lease = self.lease_mgr.acquire(
            self.terminal_id,
            dispatch_id=self.dispatch_id,
        )
        self.lease_generation = lease.generation
        self.worker_mgr.initialize(self.terminal_id, dispatch_id=self.dispatch_id)
        # Move worker into "working" so terminal transitions are valid.
        self.worker_mgr.transition(self.terminal_id, "working")

        # Drop a fake dispatch file in active/.
        self.dispatch_file = self.dispatch_dir / "active" / f"{self.dispatch_id}.md"
        self.dispatch_file.write_text("dummy dispatch payload\n")

    def tearDown(self):
        self._tmp.cleanup()

    _UNSET = object()

    def _call(
        self,
        exit_status: str,
        *,
        lease_generation=_UNSET,
        dispatch_file=_UNSET,
    ):
        """Invoke cleanup_worker_exit with fixture defaults (None to opt out)."""
        if lease_generation is self._UNSET:
            lease_generation = self.lease_generation
        if dispatch_file is self._UNSET:
            dispatch_file = self.dispatch_file
        return cwe.cleanup_worker_exit(
            terminal_id=self.terminal_id,
            dispatch_id=self.dispatch_id,
            exit_status=exit_status,
            lease_generation=lease_generation,
            dispatch_file=dispatch_file,
            state_dir=self.state_dir,
        )


class TestSuccessPath(_CWETestCase):
    """Case A — success exit goes to completed/, exited_clean, lease released."""

    def test_success_full_cleanup(self):
        result = self._call("success")

        self.assertTrue(result.lease_released)
        self.assertTrue(result.worker_transitioned)
        self.assertIsNotNone(result.dispatch_moved)
        self.assertTrue(result.dispatch_moved.exists())
        self.assertEqual(result.dispatch_moved.parent.name, "completed")
        self.assertFalse(self.dispatch_file.exists())

        lease = self.lease_mgr.get(self.terminal_id)
        self.assertEqual(lease.state, "idle")

        worker = self.worker_mgr.get(self.terminal_id)
        self.assertEqual(worker.state, "exited_clean")


class TestFailureExitPaths(_CWETestCase):
    """Cases B–E — every non-success exit_status routes to rejected/<reason>/."""

    def _assert_rejected_with_reason(self, exit_status: str, expected_reason: str):
        result = self._call(exit_status)
        self.assertTrue(result.lease_released)
        self.assertTrue(result.worker_transitioned)
        self.assertIsNotNone(result.dispatch_moved)
        self.assertEqual(result.dispatch_moved.parent.name, expected_reason)
        self.assertEqual(result.dispatch_moved.parent.parent.name, "rejected")

        worker = self.worker_mgr.get(self.terminal_id)
        self.assertEqual(worker.state, "exited_bad")

    def test_failure_routes_to_rejected_failure(self):
        self._assert_rejected_with_reason("failure", "failure")

    def test_killed_routes_to_rejected_killed(self):
        self._assert_rejected_with_reason("killed", "killed")

    def test_timeout_routes_to_rejected_timeout(self):
        self._assert_rejected_with_reason("timeout", "timeout")

    def test_stuck_routes_to_rejected_stuck(self):
        self._assert_rejected_with_reason("stuck", "stuck")


class TestIdempotency(_CWETestCase):
    """Case F — calling cleanup twice is safe; second call is a no-op."""

    def test_double_cleanup_is_safe(self):
        first = self._call("success")
        self.assertTrue(first.lease_released)
        self.assertTrue(first.worker_transitioned)
        moved_path = first.dispatch_moved

        # Second call: dispatch_file is now gone, lease is idle, worker terminal.
        second = self._call("success", dispatch_file=moved_path)

        # Lease already released → still True via the pre-check.
        self.assertTrue(second.lease_released)
        # Worker already terminal → still True (records a note).
        self.assertTrue(second.worker_transitioned)
        # Notes recorded in errors[] but no exceptions surfaced.
        self.assertTrue(
            any("already" in err for err in second.errors),
            f"expected 'already' note in errors={second.errors}",
        )


class TestLeaseAlreadyReleased(_CWETestCase):
    """Case G — lease was released before cleanup runs."""

    def test_lease_already_released_is_tolerated(self):
        # Release lease out-of-band.
        self.lease_mgr.release(self.terminal_id, generation=self.lease_generation)

        result = self._call("success")
        self.assertTrue(result.lease_released)
        self.assertTrue(
            any("lease_already_released" in err for err in result.errors),
            f"expected lease_already_released note, got errors={result.errors}",
        )


class TestNoDispatchFile(_CWETestCase):
    """Case H — dispatch_file=None skips the move step."""

    def test_no_dispatch_file_skips_move(self):
        result = self._call("success", dispatch_file=None)
        self.assertTrue(result.lease_released)
        self.assertTrue(result.worker_transitioned)
        self.assertIsNone(result.dispatch_moved)
        # Dispatch file remained where it was.
        self.assertTrue(self.dispatch_file.exists())


class TestLeaseRaises(_CWETestCase):
    """Case I — LeaseManager.release raises; cleanup continues for other steps."""

    def test_lease_failure_does_not_block_other_steps(self):
        original_release = LeaseManager.release

        def boom(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("simulated DB failure")

        try:
            LeaseManager.release = boom
            result = self._call("success")
        finally:
            LeaseManager.release = original_release

        self.assertFalse(result.lease_released)
        self.assertTrue(
            any("lease_release_failed" in err for err in result.errors),
            f"expected lease_release_failed note, got errors={result.errors}",
        )
        # Other steps still ran.
        self.assertTrue(result.worker_transitioned)
        self.assertIsNotNone(result.dispatch_moved)


class TestCLI(_CWETestCase):
    """Case J — `python3 cleanup_worker_exit.py ...` exits 0 with side effects."""

    def test_cli_invocation(self):
        cli_path = SCRIPT_DIR / "lib" / "cleanup_worker_exit.py"
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "VNX_STATE_DIR": str(self.state_dir),
            "VNX_DATA_DIR": str(self.tmp_path),
            "VNX_DATA_DIR_EXPLICIT": "1",
        }
        proc = subprocess.run(
            [
                sys.executable,
                str(cli_path),
                "--terminal-id",
                self.terminal_id,
                "--dispatch-id",
                self.dispatch_id,
                "--exit-status",
                "success",
                "--lease-generation",
                str(self.lease_generation),
                "--dispatch-file",
                str(self.dispatch_file),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}")
        payload = json.loads(proc.stdout.strip())
        self.assertTrue(payload["lease_released"])
        self.assertTrue(payload["worker_transitioned"])
        self.assertIsNotNone(payload["dispatch_moved"])

        # Side effects: dispatch file moved.
        self.assertFalse(self.dispatch_file.exists())
        moved = Path(payload["dispatch_moved"])
        self.assertTrue(moved.exists())
        self.assertEqual(moved.parent.name, "completed")


if __name__ == "__main__":
    unittest.main()
