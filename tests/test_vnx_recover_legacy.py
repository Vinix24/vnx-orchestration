#!/usr/bin/env python3
"""Tests for VNX Recover Legacy — Python-led legacy file-based cleanup (PR-3).

Tests stale lock detection, PID cleanup, dispatch file recovery, terminal
state reset, and payload cleanup. Validates dry-run mode and idempotency.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_recover_legacy import (
    CleanupAction,
    LegacyRecoveryReport,
    cleanup_stale_locks,
    cleanup_stale_pids,
    cleanup_incomplete_dispatches,
    reset_stale_terminal_claims,
    cleanup_unclean_marker,
    cleanup_stale_payloads,
    run_legacy_recovery,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def recovery_dirs(tmp_path):
    """Create a minimal recovery directory structure."""
    locks = tmp_path / "locks"
    pids = tmp_path / "pids"
    dispatches = tmp_path / "dispatches"
    state = tmp_path / "state"
    data = tmp_path / "data"
    for d in [locks, pids, dispatches / "active", dispatches / "failed", state, data]:
        d.mkdir(parents=True)
    return {
        "locks_dir": str(locks),
        "pids_dir": str(pids),
        "dispatch_dir": str(dispatches),
        "state_dir": str(state),
        "data_dir": str(data),
    }


# ---------------------------------------------------------------------------
# Stale lock cleanup
# ---------------------------------------------------------------------------

class TestCleanupStaleLocks:
    def test_dead_process_lock(self, recovery_dirs):
        """Lock with PID that is not running should be cleared."""
        lock_dir = Path(recovery_dirs["locks_dir"]) / "test.lock"
        lock_dir.mkdir()
        (lock_dir / "pid").write_text("999999999")  # unlikely to be running

        actions = cleanup_stale_locks(
            recovery_dirs["locks_dir"], recovery_dirs["pids_dir"],
        )
        assert len(actions) == 1
        assert actions[0].outcome == "applied"
        assert "process_dead" in actions[0].detail
        assert not lock_dir.exists()

    def test_orphan_lock(self, recovery_dirs):
        """Lock with no PID file should be cleared."""
        lock_dir = Path(recovery_dirs["locks_dir"]) / "orphan.lock"
        lock_dir.mkdir()

        actions = cleanup_stale_locks(
            recovery_dirs["locks_dir"], recovery_dirs["pids_dir"],
        )
        assert len(actions) == 1
        assert "orphan_lock" in actions[0].detail

    def test_expired_lock(self, recovery_dirs):
        """Lock older than max_age should be cleared (dead PID to avoid killing self)."""
        lock_dir = Path(recovery_dirs["locks_dir"]) / "old.lock"
        lock_dir.mkdir()
        # Use a PID that is not running, but set old timestamp to trigger expiry
        (lock_dir / "pid").write_text("999999997")
        (lock_dir / "created_at").write_text(str(int(time.time()) - 7200))  # 2h ago

        actions = cleanup_stale_locks(
            recovery_dirs["locks_dir"], recovery_dirs["pids_dir"],
            max_age=3600,
        )
        assert len(actions) == 1
        assert "process_dead" in actions[0].detail or "expired" in actions[0].detail

    def test_dry_run(self, recovery_dirs):
        """Dry run should not delete anything."""
        lock_dir = Path(recovery_dirs["locks_dir"]) / "test.lock"
        lock_dir.mkdir()
        (lock_dir / "pid").write_text("999999999")

        actions = cleanup_stale_locks(
            recovery_dirs["locks_dir"], recovery_dirs["pids_dir"],
            dry_run=True,
        )
        assert len(actions) == 1
        assert actions[0].outcome == "would_apply"
        assert lock_dir.exists()  # not deleted

    def test_no_locks_dir(self, tmp_path):
        """Missing locks dir should return empty."""
        actions = cleanup_stale_locks(str(tmp_path / "nonexistent"), str(tmp_path))
        assert actions == []

    def test_clears_associated_pid_files(self, recovery_dirs):
        """Should also remove .pid and .pid.fingerprint for the lock."""
        lock_dir = Path(recovery_dirs["locks_dir"]) / "supervisor.lock"
        lock_dir.mkdir()
        (lock_dir / "pid").write_text("999999999")
        pid_file = Path(recovery_dirs["pids_dir"]) / "supervisor.pid"
        pid_file.write_text("999999999")
        fp_file = Path(recovery_dirs["pids_dir"]) / "supervisor.pid.fingerprint"
        fp_file.write_text("abc")

        cleanup_stale_locks(recovery_dirs["locks_dir"], recovery_dirs["pids_dir"])
        assert not pid_file.exists()
        assert not fp_file.exists()


# ---------------------------------------------------------------------------
# Stale PID cleanup
# ---------------------------------------------------------------------------

class TestCleanupStalePids:
    def test_dead_process_pid(self, recovery_dirs):
        pid_file = Path(recovery_dirs["pids_dir"]) / "supervisor.pid"
        pid_file.write_text("999999999")

        actions = cleanup_stale_pids(recovery_dirs["pids_dir"])
        assert len(actions) == 1
        assert actions[0].outcome == "applied"
        assert not pid_file.exists()

    def test_alive_process_not_cleaned(self, recovery_dirs):
        pid_file = Path(recovery_dirs["pids_dir"]) / "self.pid"
        pid_file.write_text(str(os.getpid()))

        actions = cleanup_stale_pids(recovery_dirs["pids_dir"])
        assert len(actions) == 0
        assert pid_file.exists()

    def test_dry_run(self, recovery_dirs):
        pid_file = Path(recovery_dirs["pids_dir"]) / "supervisor.pid"
        pid_file.write_text("999999999")

        actions = cleanup_stale_pids(recovery_dirs["pids_dir"], dry_run=True)
        assert len(actions) == 1
        assert actions[0].outcome == "would_apply"
        assert pid_file.exists()


# ---------------------------------------------------------------------------
# Incomplete dispatch cleanup
# ---------------------------------------------------------------------------

class TestCleanupIncompleteDispatches:
    def test_moves_active_to_failed(self, recovery_dirs):
        active = Path(recovery_dirs["dispatch_dir"]) / "active"
        (active / "dispatch-001.md").write_text("# Dispatch")

        actions = cleanup_incomplete_dispatches(recovery_dirs["dispatch_dir"])
        assert len(actions) == 1
        assert actions[0].outcome == "applied"
        assert not (active / "dispatch-001.md").exists()
        assert (Path(recovery_dirs["dispatch_dir"]) / "failed" / "dispatch-001.recovered.md").exists()

    def test_dry_run(self, recovery_dirs):
        active = Path(recovery_dirs["dispatch_dir"]) / "active"
        (active / "dispatch-001.md").write_text("# Dispatch")

        actions = cleanup_incomplete_dispatches(recovery_dirs["dispatch_dir"], dry_run=True)
        assert len(actions) == 1
        assert actions[0].outcome == "would_apply"
        assert (active / "dispatch-001.md").exists()

    def test_no_active_dir(self, tmp_path):
        actions = cleanup_incomplete_dispatches(str(tmp_path / "dispatches"))
        assert actions == []


# ---------------------------------------------------------------------------
# Terminal state reset
# ---------------------------------------------------------------------------

class TestResetStaleTerminalClaims:
    def test_resets_working_to_idle(self, recovery_dirs):
        ts_data = {
            "schema_version": 1,
            "terminals": {
                "T1": {"terminal_id": "T1", "status": "working", "claimed_by": "dispatch-1"},
                "T2": {"terminal_id": "T2", "status": "idle"},
            },
        }
        ts_file = Path(recovery_dirs["state_dir"]) / "terminal_state.json"
        ts_file.write_text(json.dumps(ts_data))

        actions = reset_stale_terminal_claims(recovery_dirs["state_dir"])
        assert len(actions) == 1
        assert actions[0].target == "T1"

        updated = json.loads(ts_file.read_text())
        assert updated["terminals"]["T1"]["status"] == "idle"
        assert updated["terminals"]["T1"]["claimed_by"] is None

    def test_no_stale_terminals(self, recovery_dirs):
        ts_data = {
            "schema_version": 1,
            "terminals": {
                "T1": {"terminal_id": "T1", "status": "idle"},
            },
        }
        ts_file = Path(recovery_dirs["state_dir"]) / "terminal_state.json"
        ts_file.write_text(json.dumps(ts_data))

        actions = reset_stale_terminal_claims(recovery_dirs["state_dir"])
        assert actions == []

    def test_dry_run(self, recovery_dirs):
        ts_data = {
            "schema_version": 1,
            "terminals": {
                "T1": {"terminal_id": "T1", "status": "working"},
            },
        }
        ts_file = Path(recovery_dirs["state_dir"]) / "terminal_state.json"
        ts_file.write_text(json.dumps(ts_data))

        actions = reset_stale_terminal_claims(recovery_dirs["state_dir"], dry_run=True)
        assert len(actions) == 1
        assert actions[0].outcome == "would_apply"
        # File not modified
        updated = json.loads(ts_file.read_text())
        assert updated["terminals"]["T1"]["status"] == "working"


# ---------------------------------------------------------------------------
# Unclean-shutdown marker
# ---------------------------------------------------------------------------

class TestCleanupUncleanMarker:
    def test_clears_marker(self, recovery_dirs):
        marker = Path(recovery_dirs["locks_dir"]) / ".unclean_shutdown"
        marker.write_text("1")

        actions = cleanup_unclean_marker(recovery_dirs["locks_dir"])
        assert len(actions) == 1
        assert not marker.exists()

    def test_no_marker(self, recovery_dirs):
        actions = cleanup_unclean_marker(recovery_dirs["locks_dir"])
        assert actions == []

    def test_dry_run(self, recovery_dirs):
        marker = Path(recovery_dirs["locks_dir"]) / ".unclean_shutdown"
        marker.write_text("1")

        actions = cleanup_unclean_marker(recovery_dirs["locks_dir"], dry_run=True)
        assert len(actions) == 1
        assert marker.exists()


# ---------------------------------------------------------------------------
# Stale payload cleanup
# ---------------------------------------------------------------------------

class TestCleanupStalePayloads:
    def test_cleans_old_payloads(self, recovery_dirs):
        payload_dir = Path(recovery_dirs["data_dir"]) / "dispatch_payloads"
        payload_dir.mkdir()
        old_file = payload_dir / "payload_001.txt"
        old_file.write_text("data")
        # Make it old by setting mtime to 2 hours ago
        old_mtime = time.time() - 7200
        os.utime(old_file, (old_mtime, old_mtime))

        actions = cleanup_stale_payloads(recovery_dirs["data_dir"], max_age_minutes=60)
        assert len(actions) == 1
        assert not old_file.exists()

    def test_keeps_fresh_payloads(self, recovery_dirs):
        payload_dir = Path(recovery_dirs["data_dir"]) / "dispatch_payloads"
        payload_dir.mkdir()
        fresh_file = payload_dir / "payload_002.txt"
        fresh_file.write_text("data")

        actions = cleanup_stale_payloads(recovery_dirs["data_dir"], max_age_minutes=60)
        assert actions == []
        assert fresh_file.exists()

    def test_no_payload_dir(self, recovery_dirs):
        actions = cleanup_stale_payloads(recovery_dirs["data_dir"])
        assert actions == []


# ---------------------------------------------------------------------------
# Full legacy recovery
# ---------------------------------------------------------------------------

class TestRunLegacyRecovery:
    def test_clean_state(self, recovery_dirs):
        report = run_legacy_recovery(**recovery_dirs)
        assert report.issues_found == 0
        assert report.issues_resolved == 0

    def test_with_issues(self, recovery_dirs):
        # Create a stale lock
        lock_dir = Path(recovery_dirs["locks_dir"]) / "test.lock"
        lock_dir.mkdir()
        (lock_dir / "pid").write_text("999999999")

        # Create a stale PID
        pid_file = Path(recovery_dirs["pids_dir"]) / "old.pid"
        pid_file.write_text("999999998")

        report = run_legacy_recovery(**recovery_dirs)
        assert report.issues_found >= 2
        assert report.issues_resolved >= 2

    def test_dry_run(self, recovery_dirs):
        lock_dir = Path(recovery_dirs["locks_dir"]) / "test.lock"
        lock_dir.mkdir()
        (lock_dir / "pid").write_text("999999999")

        report = run_legacy_recovery(**recovery_dirs, dry_run=True)
        assert report.issues_found >= 1
        assert report.issues_resolved == 0
        assert lock_dir.exists()

    def test_terminal_reset_only_when_runtime_off(self, recovery_dirs):
        ts_data = {
            "schema_version": 1,
            "terminals": {"T1": {"terminal_id": "T1", "status": "working"}},
        }
        ts_file = Path(recovery_dirs["state_dir"]) / "terminal_state.json"
        ts_file.write_text(json.dumps(ts_data))

        # With runtime_primary=True, terminal state should NOT be reset
        report = run_legacy_recovery(**recovery_dirs, runtime_primary=True, legacy_only=False)
        terminal_actions = [a for a in report.actions if a.step == "terminal_state"]
        assert len(terminal_actions) == 0

        # With legacy_only=True, terminal state SHOULD be reset
        ts_file.write_text(json.dumps(ts_data))
        report = run_legacy_recovery(**recovery_dirs, runtime_primary=True, legacy_only=True)
        terminal_actions = [a for a in report.actions if a.step == "terminal_state"]
        assert len(terminal_actions) == 1

    def test_idempotent(self, recovery_dirs):
        """A-R8: Repeated runs produce no compound effects."""
        lock_dir = Path(recovery_dirs["locks_dir"]) / "test.lock"
        lock_dir.mkdir()
        (lock_dir / "pid").write_text("999999999")

        report1 = run_legacy_recovery(**recovery_dirs)
        assert report1.issues_resolved >= 1

        report2 = run_legacy_recovery(**recovery_dirs)
        assert report2.issues_found == 0

    def test_summary_text(self, recovery_dirs):
        report = run_legacy_recovery(**recovery_dirs)
        assert "clean" in report.summary_text()

        report.issues_resolved = 3
        assert "3 issue(s) resolved" in report.summary_text()
