#!/usr/bin/env python3
"""Tests for the flood-safe crash-recovery sweep.

Covers the contract from the dispatch:
  * orphan with a DEAD orchestrator PID  -> dead_letter + failed receipt
  * orphan with a LIVE orchestrator PID  -> skipped, untouched
  * cap behaviour                        -> at most --max-orphans recovered/run
  * idempotency                          -> a second run over the same orphan no-ops
  * dry-run                              -> classifies but writes nothing

PID liveness is injected via the ``pid_alive`` predicate so the tests are
deterministic and never depend on real process IDs. ``is_pid_alive`` itself is
exercised separately against the current process (alive) and a never-issued PID
(dead).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import crash_recovery_sweep as crs  # noqa: E402  (the lib module)

# scripts/ and scripts/lib both define a crash_recovery_sweep module; the lib
# one wins for `import crash_recovery_sweep` because scripts/lib is inserted
# last (front of sys.path). Import the CLI explicitly by file to test main().
import importlib.util as _ilu  # noqa: E402

_cli_spec = _ilu.spec_from_file_location(
    "crash_recovery_sweep_cli", str(SCRIPT_DIR / "crash_recovery_sweep.py")
)
crs_cli = _ilu.module_from_spec(_cli_spec)
_cli_spec.loader.exec_module(crs_cli)


def _dead_pid_predicate(_pid):
    return False


def _alive_pid_predicate(_pid):
    return True


class _SweepBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / ".vnx-data"
        self.active = self.data_dir / "dispatches" / "active"
        self.active.mkdir(parents=True)
        self.state_dir = self.data_dir / "state"
        self.state_dir.mkdir(parents=True)
        self.reports_dir = self.data_dir / "unified_reports"
        self.reports_dir.mkdir(parents=True)
        # Receipt writer / report stub read these from the environment.
        self._orig_env = {
            k: os.environ.get(k)
            for k in (
                "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_STATE_DIR",
                "VNX_REPORTS_DIR", "VNX_PROJECT_ID",
            )
        }
        os.environ["VNX_DATA_DIR"] = str(self.data_dir)
        # issue #225: opt in to honoring VNX_DATA_DIR over VNX_HOME resolution.
        os.environ["VNX_DATA_DIR_EXPLICIT"] = "1"
        os.environ["VNX_STATE_DIR"] = str(self.state_dir)
        os.environ["VNX_REPORTS_DIR"] = str(self.reports_dir)
        os.environ["VNX_PROJECT_ID"] = "vnx-dev"

    def tearDown(self):
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _make_orphan(self, dispatch_id, *, terminal="T1", worker_pid=None):
        d = self.active / dispatch_id
        d.mkdir(parents=True)
        manifest = {
            "dispatch_id": dispatch_id,
            "terminal": terminal,
            "model": "sonnet",
            "timestamp": "2026-05-26T09:00:00+00:00",
        }
        if worker_pid is not None:
            manifest["worker_pid"] = worker_pid
        (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return d

    def _receipts(self):
        f = self.state_dir / "t0_receipts.ndjson"
        if not f.exists():
            return []
        return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]

    def _dead_letter_exists(self, dispatch_id):
        return (
            self.data_dir / "dispatches" / "dead_letter" / dispatch_id / "manifest.json"
        ).exists()

    def _active_exists(self, dispatch_id):
        return (self.active / dispatch_id).exists()


class TestPidLiveness(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(crs.is_pid_alive(os.getpid()))

    def test_none_pid_is_not_alive(self):
        self.assertFalse(crs.is_pid_alive(None))

    def test_nonpositive_pid_is_not_alive(self):
        self.assertFalse(crs.is_pid_alive(0))
        self.assertFalse(crs.is_pid_alive(-1))

    def test_unused_high_pid_is_dead(self):
        # PID 2**31-1 is effectively never allocated; ProcessLookupError -> dead.
        self.assertFalse(crs.is_pid_alive(2**31 - 1))


class TestDeadPidRecovery(_SweepBase):
    def test_dead_orphan_promoted_and_receipt_written(self):
        self._make_orphan("d-dead-001", worker_pid=999999)

        result = crs.sweep(
            self.data_dir,
            state_dir=self.state_dir,
            pid_alive=_dead_pid_predicate,
        )

        self.assertEqual(result.recovered, ["d-dead-001"])
        self.assertEqual(result.skipped_alive, [])
        self.assertFalse(self._active_exists("d-dead-001"))
        self.assertTrue(self._dead_letter_exists("d-dead-001"))

        receipts = self._receipts()
        self.assertEqual(len(receipts), 1)
        r = receipts[0]
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["failure_reason"], crs.ORCHESTRATOR_DEATH_REASON)
        self.assertEqual(r["dispatch_id"], "d-dead-001")
        self.assertEqual(r["terminal"], "T1")

    def test_dead_orphan_writes_report_stub(self):
        self._make_orphan("d-dead-002", worker_pid=999999)
        crs.sweep(
            self.data_dir, state_dir=self.state_dir, pid_alive=_dead_pid_predicate,
        )
        self.assertTrue((self.reports_dir / "d-dead-002_report.md").exists())


class TestLivePidSkipped(_SweepBase):
    def test_live_orphan_left_untouched(self):
        self._make_orphan("d-live-001", worker_pid=os.getpid())

        result = crs.sweep(
            self.data_dir,
            state_dir=self.state_dir,
            pid_alive=_alive_pid_predicate,
        )

        self.assertEqual(result.recovered, [])
        self.assertEqual(result.skipped_alive, ["d-live-001"])
        # Untouched: still active, no dead_letter, no receipt.
        self.assertTrue(self._active_exists("d-live-001"))
        self.assertFalse(self._dead_letter_exists("d-live-001"))
        self.assertEqual(self._receipts(), [])

    def test_mixed_live_and_dead(self):
        self._make_orphan("d-dead", worker_pid=111)
        self._make_orphan("d-live", worker_pid=222)

        # Predicate: only pid 222 is alive.
        def liveness(pid):
            return pid == 222

        result = crs.sweep(self.data_dir, state_dir=self.state_dir, pid_alive=liveness)

        self.assertEqual(result.recovered, ["d-dead"])
        self.assertEqual(result.skipped_alive, ["d-live"])
        self.assertTrue(self._active_exists("d-live"))
        self.assertFalse(self._active_exists("d-dead"))


class TestCapBehaviour(_SweepBase):
    def test_cap_limits_recoveries_and_flags_capped(self):
        for i in range(5):
            self._make_orphan(f"d-cap-{i:02d}", worker_pid=900000 + i)

        result = crs.sweep(
            self.data_dir,
            state_dir=self.state_dir,
            max_orphans=2,
            pid_alive=_dead_pid_predicate,
        )

        self.assertEqual(len(result.recovered), 2)
        self.assertTrue(result.capped)
        # Exactly 2 receipts; 3 orphans still in active/.
        self.assertEqual(len(self._receipts()), 2)
        remaining = [p.name for p in self.active.iterdir() if (p / "manifest.json").exists()]
        self.assertEqual(len(remaining), 3)

    def test_cap_not_set_when_under_limit(self):
        self._make_orphan("d-one", worker_pid=900001)
        result = crs.sweep(
            self.data_dir, state_dir=self.state_dir,
            max_orphans=10, pid_alive=_dead_pid_predicate,
        )
        self.assertFalse(result.capped)
        self.assertEqual(len(result.recovered), 1)

    def test_live_orphans_do_not_consume_cap(self):
        # Live orphans are skipped and must not count against the cap.
        self._make_orphan("d-live-a", worker_pid=1)
        self._make_orphan("d-live-b", worker_pid=2)
        self._make_orphan("d-dead-a", worker_pid=3)

        def liveness(pid):
            return pid in (1, 2)

        result = crs.sweep(
            self.data_dir, state_dir=self.state_dir,
            max_orphans=1, pid_alive=liveness,
        )
        # The single dead orphan is recovered; cap is not tripped by the skips.
        self.assertEqual(result.recovered, ["d-dead-a"])
        self.assertFalse(result.capped)


class TestIdempotency(_SweepBase):
    def test_second_run_is_noop(self):
        self._make_orphan("d-idem-001", worker_pid=999999)

        first = crs.sweep(
            self.data_dir, state_dir=self.state_dir, pid_alive=_dead_pid_predicate,
        )
        second = crs.sweep(
            self.data_dir, state_dir=self.state_dir, pid_alive=_dead_pid_predicate,
        )

        self.assertEqual(first.recovered, ["d-idem-001"])
        # Orphan is gone from active/, so the second run scans 0 and recovers 0.
        self.assertEqual(second.scanned, 0)
        self.assertEqual(second.recovered, [])
        # Still exactly one receipt total.
        self.assertEqual(len(self._receipts()), 1)


class TestDryRun(_SweepBase):
    def test_dry_run_writes_nothing(self):
        self._make_orphan("d-dry-001", worker_pid=999999)

        result = crs.sweep(
            self.data_dir,
            state_dir=self.state_dir,
            dry_run=True,
            pid_alive=_dead_pid_predicate,
        )

        # Reported as "would recover" but no side effects.
        self.assertTrue(result.dry_run)
        self.assertEqual(result.recovered, ["d-dry-001"])
        self.assertTrue(self._active_exists("d-dry-001"))
        self.assertFalse(self._dead_letter_exists("d-dry-001"))
        self.assertEqual(self._receipts(), [])
        self.assertFalse((self.reports_dir / "d-dry-001_report.md").exists())

    def test_dry_run_skips_live_orphans_too(self):
        self._make_orphan("d-dry-live", worker_pid=os.getpid())
        result = crs.sweep(
            self.data_dir, state_dir=self.state_dir,
            dry_run=True, pid_alive=_alive_pid_predicate,
        )
        self.assertEqual(result.recovered, [])
        self.assertEqual(result.skipped_alive, ["d-dry-live"])


class TestNoResolvablePid(_SweepBase):
    def test_orphan_without_pid_is_recovered_and_flagged(self):
        # No worker_pid in manifest, no lease row -> PID unresolvable.
        self._make_orphan("d-nopid-001", worker_pid=None)

        result = crs.sweep(
            self.data_dir, state_dir=self.state_dir, pid_alive=_dead_pid_predicate,
        )
        self.assertEqual(result.recovered, ["d-nopid-001"])
        self.assertEqual(result.skipped_no_pid, ["d-nopid-001"])
        self.assertTrue(self._dead_letter_exists("d-nopid-001"))


class TestDiscovery(_SweepBase):
    def test_empty_active_dir(self):
        result = crs.sweep(self.data_dir, state_dir=self.state_dir)
        self.assertEqual(result.scanned, 0)
        self.assertEqual(result.recovered, [])

    def test_entry_without_manifest_ignored(self):
        # A bare dir with no manifest.json is not an orphan we can recover.
        (self.active / "d-bare").mkdir()
        result = crs.sweep(
            self.data_dir, state_dir=self.state_dir, pid_alive=_dead_pid_predicate,
        )
        self.assertEqual(result.scanned, 0)


class TestCli(_SweepBase):
    def test_dry_run_json_output_writes_nothing(self):
        self._make_orphan("d-cli-001", worker_pid=999999)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = crs_cli.main([
                "--data-dir", str(self.data_dir),
                "--state-dir", str(self.state_dir),
                "--dry-run", "--json",
            ])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["recovered"], ["d-cli-001"])
        # Dry-run wrote nothing.
        self.assertTrue(self._active_exists("d-cli-001"))
        self.assertEqual(self._receipts(), [])

    def test_max_orphans_below_one_rejected(self):
        with self.assertRaises(SystemExit):
            crs_cli.main([
                "--data-dir", str(self.data_dir),
                "--max-orphans", "0",
            ])

    def test_text_output_default(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = crs_cli.main([
                "--data-dir", str(self.data_dir),
                "--state-dir", str(self.state_dir),
            ])
        self.assertEqual(rc, 0)
        self.assertIn("scanned 0", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
