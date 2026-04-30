#!/usr/bin/env python3
"""Tests for scripts/lib/lease_sweep.py (SUP-PR2)."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (  # noqa: E402
    get_connection,
    init_schema,
    register_dispatch,
)
from lease_manager import LeaseManager  # noqa: E402

import lease_sweep  # noqa: E402


def _past_iso(seconds: int = 60) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class _SweepTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmpdir.name)
        init_schema(self.state_dir)
        self.mgr = LeaseManager(self.state_dir, auto_init=False)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _acquire(self, terminal_id: str, dispatch_id: str, *, lease_seconds: int = 600):
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal_id)
            conn.commit()
        return self.mgr.acquire(terminal_id, dispatch_id=dispatch_id, lease_seconds=lease_seconds)

    def _force_expires_at(self, terminal_id: str, when_iso: str) -> None:
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE terminal_leases SET expires_at = ? WHERE terminal_id = ?",
                (when_iso, terminal_id),
            )
            conn.commit()


class TestLeaseSweepRun(_SweepTestCase):
    def test_empty_table_returns_no_expirations(self):
        expired = lease_sweep.run(self.state_dir)
        self.assertEqual(expired, [])

    def test_single_expired_lease(self):
        self._acquire("T1", "d-001")
        self._force_expires_at("T1", _past_iso(60))

        expired = lease_sweep.run(self.state_dir)
        self.assertEqual(expired, ["T1"])

        lease = self.mgr.get("T1")
        self.assertEqual(lease.state, "expired")

    def test_multiple_expired_some_valid(self):
        self._acquire("T1", "d-001")
        self._acquire("T2", "d-002")
        self._acquire("T3", "d-003")
        self._force_expires_at("T1", _past_iso(120))
        self._force_expires_at("T3", _past_iso(30))
        # T2 keeps fresh expires_at from acquire()

        expired = sorted(lease_sweep.run(self.state_dir))
        self.assertEqual(expired, ["T1", "T3"])
        self.assertEqual(self.mgr.get("T1").state, "expired")
        self.assertEqual(self.mgr.get("T2").state, "leased")
        self.assertEqual(self.mgr.get("T3").state, "expired")

    def test_lease_without_expires_at_ignored(self):
        self._acquire("T1", "d-001")
        # NULL out expires_at — expire_stale must skip it.
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE terminal_leases SET expires_at = NULL WHERE terminal_id = 'T1'"
            )
            conn.commit()

        expired = lease_sweep.run(self.state_dir)
        self.assertEqual(expired, [])
        self.assertEqual(self.mgr.get("T1").state, "leased")

    def test_idempotent(self):
        self._acquire("T1", "d-001")
        self._force_expires_at("T1", _past_iso(60))

        first = lease_sweep.run(self.state_dir)
        second = lease_sweep.run(self.state_dir)
        self.assertEqual(first, ["T1"])
        self.assertEqual(second, [])


class TestLeaseSweepCli(_SweepTestCase):
    def test_json_flag_emits_valid_json(self):
        self._acquire("T1", "d-001")
        self._force_expires_at("T1", _past_iso(60))

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = lease_sweep.main(["--state-dir", str(self.state_dir), "--json"])
        self.assertEqual(rc, 0)

        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["expired"], ["T1"])

    def test_text_output_default(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = lease_sweep.main(["--state-dir", str(self.state_dir)])
        self.assertEqual(rc, 0)
        self.assertIn("expired 0 stale leases", buf.getvalue())

    def test_explicit_state_dir_arg(self):
        self._acquire("T1", "d-001")
        self._force_expires_at("T1", _past_iso(60))

        rc = lease_sweep.main(["--state-dir", str(self.state_dir), "--json"])
        self.assertEqual(rc, 0)
        # Verify side effect was applied via the same state_dir
        self.assertEqual(self.mgr.get("T1").state, "expired")


if __name__ == "__main__":
    unittest.main()
