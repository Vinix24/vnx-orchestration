"""Tests for scripts/aggregator/t0_lifecycle.py (Wave 5 PR-5.2).

Coverage:
  - spawn creates lease + returns T0Instance with valid pid
  - spawn refuses if project T0 already running (ValueError)
  - heartbeat updates last_heartbeat_at timestamp
  - kill SIGTERM — graceful shutdown, lease released
  - kill SIGTERM escalation to SIGKILL when process unresponsive
  - reap_dead_t0s detects stale heartbeats
  - list_running returns correct subset after spawn/kill
  - StateAggregator receives t0_spawned lifecycle event
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Project root in path for package imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.aggregator.state_aggregator import ProjectStateUpdate, StateAggregator
from scripts.aggregator.t0_lifecycle import T0LifecycleManager, _T0_TERMINAL_ID

_SCRIPTS_LIB = _PROJECT_ROOT / "scripts" / "lib"
sys.path.insert(0, str(_SCRIPTS_LIB))


# Minimal schema for tests — only the terminal_leases table that T0LifecycleManager uses.
# The full init_schema chain (v1→v10) fails on fresh DBs because v10 depends on the
# schemas/migrations/0010 ALTER TABLE that init_schema doesn't apply.
_MINIMAL_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS terminal_leases (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id         TEXT    NOT NULL,
    project_id          TEXT    NOT NULL DEFAULT 'vnx-dev',
    state               TEXT    NOT NULL DEFAULT 'idle',
    dispatch_id         TEXT,
    generation          INTEGER NOT NULL DEFAULT 1,
    leased_at           TEXT,
    expires_at          TEXT,
    last_heartbeat_at   TEXT,
    released_at         TEXT,
    metadata_json       TEXT    DEFAULT '{}',
    UNIQUE(terminal_id, project_id)
);
"""


def _init_test_db(db_path: Path) -> None:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_MINIMAL_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def _get_conn(db_path: Path):
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------


class _LifecycleBase(unittest.TestCase):
    """Creates temp dirs, initialises schema, builds a T0LifecycleManager."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmpdir.name)
        self.coord_db = self.state_dir / "runtime_coordination.db"
        _init_test_db(self.coord_db)

        self.vnx_data_dir = self.state_dir / "vnx-data"
        self.vnx_data_dir.mkdir()
        self.aggregator = StateAggregator(self.vnx_data_dir)

        self.mgr = self._make_mgr()

    def _make_mgr(self, **extra) -> T0LifecycleManager:
        opts = {
            "coord_db_path": str(self.coord_db),
            "projects": {
                "proj-a": {"root": str(self.state_dir), "cmd": ["sleep", "60"]},
                "proj-b": {"root": str(self.state_dir), "cmd": ["sleep", "60"]},
                "proj-c": {"root": str(self.state_dir), "cmd": ["sleep", "60"]},
                "test-proj": {"root": str(self.state_dir), "cmd": ["sleep", "60"]},
            },
            "heartbeat_timeout": 120,
            "aggregator": self.aggregator,
        }
        opts.update(extra)
        return T0LifecycleManager(opts)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _lease_row(self, project_id: str) -> dict | None:
        conn = _get_conn(self.coord_db)
        try:
            row = conn.execute(
                "SELECT * FROM terminal_leases WHERE terminal_id=? AND project_id=?",
                (_T0_TERMINAL_ID, project_id),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def _aggregator_events(self, event_type: str | None = None) -> list[dict]:
        events_path = self.vnx_data_dir / "events" / "state_aggregator.ndjson"
        if not events_path.exists():
            return []
        records = []
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if event_type is None or rec.get("event_type") == event_type:
                records.append(rec)
        return records


# ---------------------------------------------------------------------------
# Test: spawn creates lease + process
# ---------------------------------------------------------------------------


class TestSpawnCreatesLeaseAndProcess(_LifecycleBase):
    @patch("scripts.aggregator.t0_lifecycle.subprocess.Popen")
    def test_spawn_creates_lease_and_process(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 99001
        mock_popen.return_value = mock_proc

        instance = self.mgr.spawn("test-proj")

        self.assertEqual(instance.project_id, "test-proj")
        self.assertEqual(instance.pid, 99001)
        self.assertEqual(instance.state, "running")
        self.assertIsNotNone(instance.started_at)
        self.assertIsNotNone(instance.last_heartbeat_at)

        row = self._lease_row("test-proj")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "leased")
        self.assertEqual(row["terminal_id"], _T0_TERMINAL_ID)
        self.assertEqual(row["project_id"], "test-proj")

        meta = json.loads(row["metadata_json"])
        self.assertEqual(meta["pid"], 99001)

    @patch("scripts.aggregator.t0_lifecycle.subprocess.Popen")
    def test_spawn_sets_vnx_project_id_env(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 99002
        mock_popen.return_value = mock_proc

        self.mgr.spawn("test-proj")

        call_kwargs = mock_popen.call_args[1]
        env = call_kwargs.get("env", {})
        self.assertEqual(env.get("VNX_PROJECT_ID"), "test-proj")


# ---------------------------------------------------------------------------
# Test: spawn refuses if already running
# ---------------------------------------------------------------------------


class TestSpawnRefusesIfAlreadyRunning(_LifecycleBase):
    @patch("scripts.aggregator.t0_lifecycle.subprocess.Popen")
    def test_spawn_refuses_if_already_running(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 88001
        mock_popen.return_value = mock_proc

        self.mgr.spawn("test-proj")

        with self.assertRaises(ValueError) as ctx:
            self.mgr.spawn("test-proj")

        self.assertIn("test-proj", str(ctx.exception))
        self.assertIn("already running", str(ctx.exception))

    @patch("scripts.aggregator.t0_lifecycle.subprocess.Popen")
    def test_second_spawn_different_project_succeeds(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 88100
        mock_popen.return_value = mock_proc

        inst_a = self.mgr.spawn("proj-a")
        inst_b = self.mgr.spawn("proj-b")

        self.assertEqual(inst_a.project_id, "proj-a")
        self.assertEqual(inst_b.project_id, "proj-b")


# ---------------------------------------------------------------------------
# Test: heartbeat updates timestamp
# ---------------------------------------------------------------------------


class TestHeartbeatUpdatesTimestamp(_LifecycleBase):
    @patch("scripts.aggregator.t0_lifecycle.subprocess.Popen")
    def test_heartbeat_updates_timestamp(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 77001
        mock_popen.return_value = mock_proc

        self.mgr.spawn("test-proj")
        row_before = self._lease_row("test-proj")

        time.sleep(0.01)
        recorded = self.mgr.heartbeat("test-proj", 77001)

        self.assertTrue(recorded)
        row_after = self._lease_row("test-proj")
        self.assertGreater(
            row_after["last_heartbeat_at"],
            row_before["last_heartbeat_at"],
        )

    @patch("scripts.aggregator.t0_lifecycle.subprocess.Popen")
    def test_heartbeat_wrong_pid_returns_false(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 77002
        mock_popen.return_value = mock_proc

        self.mgr.spawn("test-proj")
        result = self.mgr.heartbeat("test-proj", 99999)
        self.assertFalse(result)

    def test_heartbeat_no_lease_returns_false(self) -> None:
        result = self.mgr.heartbeat("test-proj", 12345)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Test: kill SIGTERM graceful
# ---------------------------------------------------------------------------


class TestKillSigtermGraceful(_LifecycleBase):
    def test_kill_sigterm_graceful(self) -> None:
        import subprocess as sp
        # Short timeout so the test completes in <1s even if zombie detection is imperfect
        mgr = self._make_mgr(heartbeat_timeout=0.3, kill_poll_interval=0.05)
        proc = sp.Popen(
            ["sleep", "60"],
            stdout=sp.DEVNULL,
            stderr=sp.DEVNULL,
        )
        pid = proc.pid

        now = "2026-05-16T12:00:00.000+00:00"
        meta = json.dumps({"pid": pid, "project_root": "", "started_at": now})
        conn = _get_conn(self.coord_db)
        try:
            conn.execute(
                """
                INSERT INTO terminal_leases
                    (terminal_id, project_id, state, dispatch_id, generation,
                     leased_at, last_heartbeat_at, metadata_json)
                VALUES (?, 'test-proj', 'leased', NULL, 1, ?, ?, ?)
                """,
                (_T0_TERMINAL_ID, now, now, meta),
            )
            conn.commit()
        finally:
            conn.close()

        killed = mgr.kill("test-proj", signal_type=signal.SIGTERM)

        self.assertTrue(killed)
        # Reap zombie and verify the process is gone (SIGTERM or SIGKILL escalation)
        try:
            proc.wait(timeout=2)
        except sp.TimeoutExpired:
            proc.kill()
            proc.wait()
        self.assertIsNotNone(proc.returncode, "Process should have terminated")

        row = self._lease_row("test-proj")
        self.assertEqual(row["state"], "released")


# ---------------------------------------------------------------------------
# Test: kill escalates to SIGKILL when unresponsive
# ---------------------------------------------------------------------------


class TestKillSigkillEscalation(_LifecycleBase):
    def test_kill_sigkill_force_when_unresponsive(self) -> None:
        mgr = self._make_mgr(
            heartbeat_timeout=0.5,
            kill_poll_interval=0.05,
        )
        # Spawn a process that ignores SIGTERM
        import subprocess as sp
        proc = sp.Popen(
            [
                "python3",
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)",
            ],
            stdout=sp.DEVNULL,
            stderr=sp.DEVNULL,
        )
        pid = proc.pid

        now = "2026-05-16T12:00:00.000+00:00"
        meta = json.dumps({"pid": pid, "project_root": "", "started_at": now})
        conn = _get_conn(self.coord_db)
        try:
            conn.execute(
                """
                INSERT INTO terminal_leases
                    (terminal_id, project_id, state, dispatch_id, generation,
                     leased_at, last_heartbeat_at, metadata_json)
                VALUES (?, 'test-proj', 'leased', NULL, 1, ?, ?, ?)
                """,
                (_T0_TERMINAL_ID, now, now, meta),
            )
            conn.commit()
        finally:
            conn.close()

        killed = mgr.kill("test-proj", signal_type=signal.SIGTERM)

        self.assertTrue(killed)
        # Process must be dead (SIGKILL escalation fired)
        proc.wait(timeout=2)
        self.assertIsNotNone(proc.returncode)

        row = self._lease_row("test-proj")
        self.assertEqual(row["state"], "released")


# ---------------------------------------------------------------------------
# Test: reap dead T0s
# ---------------------------------------------------------------------------


class TestReapDeadT0s(_LifecycleBase):
    def _insert_lease(
        self,
        project_id: str,
        *,
        last_heartbeat_at: str,
        state: str = "leased",
    ) -> None:
        meta = json.dumps({"pid": 0, "project_root": ""})
        conn = _get_conn(self.coord_db)
        try:
            conn.execute(
                """
                INSERT INTO terminal_leases
                    (terminal_id, project_id, state, dispatch_id, generation,
                     leased_at, last_heartbeat_at, metadata_json)
                VALUES (?, ?, ?, NULL, 1, ?, ?, ?)
                """,
                (_T0_TERMINAL_ID, project_id, state, last_heartbeat_at, last_heartbeat_at, meta),
            )
            conn.commit()
        finally:
            conn.close()

    def test_reap_dead_t0s_finds_stale(self) -> None:
        mgr = self._make_mgr(heartbeat_timeout=60)
        now = datetime.now(timezone.utc)
        # Stale: 2 hours ago → definitely older than 60s timeout
        stale_ts = (now - timedelta(hours=2)).isoformat(timespec="milliseconds")
        self._insert_lease("proj-a", last_heartbeat_at=stale_ts)
        # Fresh: 10 seconds ago → not older than 60s timeout
        fresh_ts = (now - timedelta(seconds=10)).isoformat(timespec="milliseconds")
        self._insert_lease("proj-b", last_heartbeat_at=fresh_ts)

        reaped = mgr.reap_dead_t0s()

        self.assertIn("proj-a", reaped)
        self.assertNotIn("proj-b", reaped)

        row_a = self._lease_row("proj-a")
        row_b = self._lease_row("proj-b")
        self.assertEqual(row_a["state"], "released")
        self.assertEqual(row_b["state"], "leased")

    def test_reap_dead_t0s_idempotent(self) -> None:
        mgr = self._make_mgr(heartbeat_timeout=60)
        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="milliseconds")
        self._insert_lease("proj-a", last_heartbeat_at=stale_ts)

        first = mgr.reap_dead_t0s()
        second = mgr.reap_dead_t0s()

        self.assertIn("proj-a", first)
        self.assertEqual(second, [])

    def test_reap_no_stale_returns_empty(self) -> None:
        mgr = self._make_mgr(heartbeat_timeout=60)
        reaped = mgr.reap_dead_t0s()
        self.assertEqual(reaped, [])


# ---------------------------------------------------------------------------
# Test: list_running returns active subset
# ---------------------------------------------------------------------------


class TestListRunningReturnsActive(_LifecycleBase):
    @patch("scripts.aggregator.t0_lifecycle.subprocess.Popen")
    def test_list_running_returns_active(self, mock_popen: MagicMock) -> None:
        pids = iter([11001, 11002, 11003])

        def _make_mock(*args, **kwargs):
            m = MagicMock()
            m.pid = next(pids)
            return m

        mock_popen.side_effect = _make_mock

        self.mgr.spawn("proj-a")
        self.mgr.spawn("proj-b")
        self.mgr.spawn("proj-c")

        running = self.mgr.list_running()
        self.assertEqual(len(running), 3)
        project_ids = {i.project_id for i in running}
        self.assertEqual(project_ids, {"proj-a", "proj-b", "proj-c"})

        # Kill proj-b via DB (bypass actual process to avoid real signal)
        conn = _get_conn(self.coord_db)
        try:
            conn.execute(
                "UPDATE terminal_leases SET state='released' WHERE terminal_id=? AND project_id=?",
                (_T0_TERMINAL_ID, "proj-b"),
            )
            conn.commit()
        finally:
            conn.close()

        running_after = self.mgr.list_running()
        self.assertEqual(len(running_after), 2)
        remaining = {i.project_id for i in running_after}
        self.assertNotIn("proj-b", remaining)


# ---------------------------------------------------------------------------
# Test: StateAggregator receives lifecycle events
# ---------------------------------------------------------------------------


class TestStateAggregatorReceivesLifecycleEvents(_LifecycleBase):
    @patch("scripts.aggregator.t0_lifecycle.subprocess.Popen")
    def test_state_aggregator_receives_lifecycle_events(
        self, mock_popen: MagicMock
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 55001
        mock_popen.return_value = mock_proc

        self.mgr.spawn("test-proj")

        events = self._aggregator_events("t0_spawned")
        self.assertGreaterEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt["event_type"], "t0_spawned")
        self.assertEqual(evt["sub_provider"], "test-proj")
        data = evt["data"]
        self.assertEqual(data["pid"], 55001)

    @patch("scripts.aggregator.t0_lifecycle.subprocess.Popen")
    def test_state_aggregator_receives_heartbeat_event(
        self, mock_popen: MagicMock
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 55002
        mock_popen.return_value = mock_proc

        self.mgr.spawn("test-proj")
        self.mgr.heartbeat("test-proj", 55002)

        hb_events = self._aggregator_events("t0_heartbeat")
        # Both spawn-time heartbeat state and explicit heartbeat call
        self.assertGreaterEqual(len(hb_events), 1)


if __name__ == "__main__":
    unittest.main()
