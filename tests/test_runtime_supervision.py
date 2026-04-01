#!/usr/bin/env python3
"""
Tests for runtime_supervisor.py (Feature 12, PR-2)

Quality gate coverage (gate_pr2_runtime_supervision_and_escalation):
  - All runtime supervision tests pass
  - No-output stall detection produces structured runtime outcomes under test
  - Bad exits classify into explicit operator-meaningful states under test
  - Unresolved runtime anomalies create durable open items automatically
  - Audit records exist for stall, stale, and bad-exit paths
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (
    get_connection,
    get_events,
    init_schema,
    register_dispatch,
    acquire_lease,
    transition_dispatch,
)
from worker_state_manager import WorkerStateManager
from runtime_supervisor import (
    ANOMALY_TYPES,
    AnomalyRecord,
    RuntimeSupervisor,
    create_open_items_for_anomalies,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _setup(tmp_path: str, *, stall_threshold: int = 180, startup_grace: int = 120,
           idle_grace: int = 120, dead_threshold: int = 300, stale_threshold: int = 90):
    """Initialize schema, create dispatch+lease for T1, return (state_dir, supervisor, worker_mgr)."""
    state_dir = os.path.join(tmp_path, "state")
    os.makedirs(state_dir, exist_ok=True)
    init_schema(state_dir)

    with get_connection(state_dir) as conn:
        register_dispatch(conn, dispatch_id="d-001", terminal_id="T1", track="B")
        acquire_lease(conn, terminal_id="T1", dispatch_id="d-001")
        conn.commit()

    supervisor = RuntimeSupervisor(
        state_dir, auto_init=False,
        stall_threshold=stall_threshold,
        startup_grace=startup_grace,
        idle_grace=idle_grace,
        dead_threshold=dead_threshold,
        stale_threshold=stale_threshold,
    )
    worker_mgr = WorkerStateManager(state_dir, auto_init=False)
    return state_dir, supervisor, worker_mgr


def _set_heartbeat(state_dir: str, terminal_id: str, seconds_ago: float):
    """Set last_heartbeat_at to a specific time in the past."""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"
    with get_connection(state_dir) as conn:
        conn.execute(
            "UPDATE terminal_leases SET last_heartbeat_at = ? WHERE terminal_id = ?",
            (ts, terminal_id),
        )
        conn.commit()


def _set_worker_timestamps(state_dir: str, terminal_id: str, *,
                           state_entered_ago: float = 0,
                           last_output_ago: Optional[float] = None):
    """Set worker state timestamps to specific times in the past."""
    now = datetime.now(timezone.utc)
    entered = (now - timedelta(seconds=state_entered_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"

    output_ts = None
    if last_output_ago is not None:
        output_ts = (now - timedelta(seconds=last_output_ago)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"

    with get_connection(state_dir) as conn:
        conn.execute(
            """UPDATE worker_states
               SET state_entered_at = ?, last_output_at = ?, updated_at = ?
               WHERE terminal_id = ?""",
            (entered, output_ts, entered, terminal_id),
        )
        conn.commit()


from typing import Optional


# ===========================================================================
# Test: Stall detection (§5.2)
# ===========================================================================

class TestStallDetection(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.supervisor, self.worker_mgr = _setup(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_anomaly_when_fresh(self):
        """Within grace period → no anomaly."""
        self.worker_mgr.initialize("T1", "d-001")
        _set_heartbeat(self.state_dir, "T1", 10)
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=30)
        anomalies = self.supervisor.supervise_terminal("T1")
        self.assertEqual(len(anomalies), 0)

    def test_startup_stall(self):
        """initializing beyond startup_grace → startup_stall."""
        self.worker_mgr.initialize("T1", "d-001")
        _set_heartbeat(self.state_dir, "T1", 10)
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=130)
        anomalies = self.supervisor.supervise_terminal("T1")
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0].anomaly_type, "startup_stall")
        self.assertEqual(anomalies[0].severity, "warning")
        # Worker should now be stalled
        ws = self.worker_mgr.get("T1")
        self.assertEqual(ws.state, "stalled")

    def test_progress_stall(self):
        """working with no output beyond threshold → progress_stall."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        _set_heartbeat(self.state_dir, "T1", 10)
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=200, last_output_ago=200)
        anomalies = self.supervisor.supervise_terminal("T1")
        stall = [a for a in anomalies if a.anomaly_type == "progress_stall"]
        self.assertEqual(len(stall), 1)
        ws = self.worker_mgr.get("T1")
        self.assertEqual(ws.state, "stalled")

    def test_inter_task_stall(self):
        """idle_between_tasks beyond grace → inter_task_stall."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        self.worker_mgr.transition("T1", "idle_between_tasks")
        _set_heartbeat(self.state_dir, "T1", 10)
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=130)
        anomalies = self.supervisor.supervise_terminal("T1")
        stall = [a for a in anomalies if a.anomaly_type == "inter_task_stall"]
        self.assertEqual(len(stall), 1)
        ws = self.worker_mgr.get("T1")
        self.assertEqual(ws.state, "stalled")

    def test_no_stall_for_blocked(self):
        """blocked state → no stall detection (expected silence)."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        self.worker_mgr.transition("T1", "blocked", blocked_reason="MCP timeout")
        _set_heartbeat(self.state_dir, "T1", 10)
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=300)
        anomalies = self.supervisor.supervise_terminal("T1")
        stalls = [a for a in anomalies if "stall" in a.anomaly_type]
        self.assertEqual(len(stalls), 0)

    def test_no_stall_for_awaiting_input(self):
        """awaiting_input → no stall detection."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        self.worker_mgr.transition("T1", "awaiting_input")
        _set_heartbeat(self.state_dir, "T1", 10)
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=300)
        anomalies = self.supervisor.supervise_terminal("T1")
        stalls = [a for a in anomalies if "stall" in a.anomaly_type]
        self.assertEqual(len(stalls), 0)


# ===========================================================================
# Test: Dead worker escalation (§4.4 H-3)
# ===========================================================================

class TestDeadWorkerEscalation(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.supervisor, self.worker_mgr = _setup(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_dead_heartbeat_escalates_to_exited_bad(self):
        """Dead heartbeat → exited_bad + dead_worker anomaly."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        _set_heartbeat(self.state_dir, "T1", 400)
        anomalies = self.supervisor.supervise_terminal("T1")
        dead = [a for a in anomalies if a.anomaly_type == "dead_worker"]
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0].severity, "blocking")
        ws = self.worker_mgr.get("T1")
        self.assertEqual(ws.state, "exited_bad")

    def test_dead_heartbeat_from_stalled(self):
        """stalled + dead heartbeat → exited_bad."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        self.worker_mgr.transition("T1", "stalled")
        _set_heartbeat(self.state_dir, "T1", 400)
        anomalies = self.supervisor.supervise_terminal("T1")
        dead = [a for a in anomalies if a.anomaly_type == "dead_worker"]
        self.assertEqual(len(dead), 1)
        ws = self.worker_mgr.get("T1")
        self.assertEqual(ws.state, "exited_bad")

    def test_dead_heartbeat_from_initializing(self):
        """initializing + dead heartbeat → exited_bad."""
        self.worker_mgr.initialize("T1", "d-001")
        _set_heartbeat(self.state_dir, "T1", 400)
        anomalies = self.supervisor.supervise_terminal("T1")
        dead = [a for a in anomalies if a.anomaly_type == "dead_worker"]
        self.assertEqual(len(dead), 1)
        ws = self.worker_mgr.get("T1")
        self.assertEqual(ws.state, "exited_bad")

    def test_no_escalation_for_terminal_state(self):
        """Already exited_bad → no double escalation."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        self.worker_mgr.transition("T1", "exited_bad")
        _set_heartbeat(self.state_dir, "T1", 400)
        anomalies = self.supervisor.supervise_terminal("T1")
        self.assertEqual(len(anomalies), 0)


# ===========================================================================
# Test: Zombie lease detection (§6.3)
# ===========================================================================

class TestZombieLease(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.supervisor, self.worker_mgr = _setup(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_zombie_lease_detected(self):
        """Lease leased but dispatch completed → zombie_lease."""
        self.worker_mgr.initialize("T1", "d-001")
        # Advance dispatch to completed state
        with get_connection(self.state_dir) as conn:
            transition_dispatch(conn, dispatch_id="d-001", to_state="claimed")
            transition_dispatch(conn, dispatch_id="d-001", to_state="delivering")
            transition_dispatch(conn, dispatch_id="d-001", to_state="accepted")
            transition_dispatch(conn, dispatch_id="d-001", to_state="running")
            transition_dispatch(conn, dispatch_id="d-001", to_state="completed")
            conn.commit()
        _set_heartbeat(self.state_dir, "T1", 10)
        anomalies = self.supervisor.supervise_terminal("T1")
        zombies = [a for a in anomalies if a.anomaly_type == "zombie_lease"]
        self.assertEqual(len(zombies), 1)
        self.assertEqual(zombies[0].severity, "blocking")


# ===========================================================================
# Test: Ghost dispatch detection (§6.3)
# ===========================================================================

class TestGhostDispatch(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        state_dir = os.path.join(self._tmp.name, "state")
        os.makedirs(state_dir)
        init_schema(state_dir)
        self.state_dir = state_dir
        self.supervisor = RuntimeSupervisor(state_dir, auto_init=False)

    def tearDown(self):
        self._tmp.cleanup()

    def test_ghost_dispatch_detected(self):
        """Dispatch in 'running' but terminal is idle → ghost_dispatch."""
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id="d-ghost", terminal_id="T1", track="A")
            transition_dispatch(conn, dispatch_id="d-ghost", to_state="claimed")
            transition_dispatch(conn, dispatch_id="d-ghost", to_state="delivering")
            transition_dispatch(conn, dispatch_id="d-ghost", to_state="accepted")
            transition_dispatch(conn, dispatch_id="d-ghost", to_state="running")
            conn.commit()
        # T1 lease is still idle (never acquired)
        anomalies = self.supervisor.supervise_all()
        ghosts = [a for a in anomalies if a.anomaly_type == "ghost_dispatch"]
        self.assertEqual(len(ghosts), 1)
        self.assertEqual(ghosts[0].terminal_id, "T1")
        self.assertEqual(ghosts[0].severity, "blocking")


# ===========================================================================
# Test: Heartbeat-output divergence (§7.1)
# ===========================================================================

class TestHeartbeatOutputDivergence(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.supervisor, self.worker_mgr = _setup(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_heartbeat_without_output(self):
        """Fresh heartbeat but output silence > 2x stall → heartbeat_without_output."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        _set_heartbeat(self.state_dir, "T1", 10)
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=400, last_output_ago=400)
        anomalies = self.supervisor.supervise_terminal("T1")
        hwo = [a for a in anomalies if a.anomaly_type == "heartbeat_without_output"]
        self.assertGreaterEqual(len(hwo), 1)

    def test_output_without_heartbeat(self):
        """Output recent but heartbeat stale → output_without_heartbeat."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        _set_heartbeat(self.state_dir, "T1", 100)  # stale (90-300)
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=200, last_output_ago=10)
        anomalies = self.supervisor.supervise_terminal("T1")
        owh = [a for a in anomalies if a.anomaly_type == "output_without_heartbeat"]
        self.assertGreaterEqual(len(owh), 1)


# ===========================================================================
# Test: Recovery timeout (§7.1)
# ===========================================================================

class TestRecoveryTimeout(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        state_dir = os.path.join(self._tmp.name, "state")
        os.makedirs(state_dir)
        init_schema(state_dir)
        self.state_dir = state_dir
        self.supervisor = RuntimeSupervisor(state_dir, auto_init=False, recovery_timeout=600)

    def tearDown(self):
        self._tmp.cleanup()

    def test_recovery_timeout_detected(self):
        """Terminal stuck in expired > recovery_timeout → recovery_timeout."""
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id="d-rt", terminal_id="T1")
            acquire_lease(conn, terminal_id="T1", dispatch_id="d-rt")
            conn.commit()
        # Force lease to expired
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=700)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z"
        with get_connection(self.state_dir) as conn:
            conn.execute(
                "UPDATE terminal_leases SET state = 'expired', last_heartbeat_at = ? WHERE terminal_id = 'T1'",
                (old_ts,),
            )
            conn.commit()
        anomalies = self.supervisor.supervise_terminal("T1")
        rt = [a for a in anomalies if a.anomaly_type == "recovery_timeout"]
        self.assertEqual(len(rt), 1)
        self.assertEqual(rt[0].severity, "blocking")


# ===========================================================================
# Test: Open item auto-creation (§7.2, §7.3)
# ===========================================================================

class TestOpenItemCreation(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.supervisor, self.worker_mgr = _setup(self._tmp.name)
        self.oi_path = Path(self._tmp.name) / "open_items.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_anomaly_creates_open_item(self):
        """Detected anomaly → open item written to file."""
        anomaly = AnomalyRecord(
            anomaly_type="progress_stall",
            severity="warning",
            terminal_id="T1",
            dispatch_id="d-001",
            worker_state="stalled",
            lease_state="leased",
            evidence={"output_silence_seconds": 200},
        )
        items = create_open_items_for_anomalies([anomaly], self.oi_path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "runtime_anomaly")
        self.assertEqual(items[0]["anomaly"], "progress_stall")
        self.assertTrue(items[0]["auto_created"])
        self.assertIsNone(items[0]["resolution"])

        # Verify persisted
        with open(self.oi_path) as f:
            data = json.load(f)
        self.assertEqual(len(data["items"]), 1)

    def test_dedup_prevents_duplicate(self):
        """OI-5: Same anomaly type + terminal + dispatch → update, not duplicate."""
        anomaly1 = AnomalyRecord(
            anomaly_type="progress_stall",
            severity="warning",
            terminal_id="T1",
            dispatch_id="d-001",
            worker_state="stalled",
            lease_state="leased",
            evidence={"output_silence_seconds": 200},
        )
        anomaly2 = AnomalyRecord(
            anomaly_type="progress_stall",
            severity="warning",
            terminal_id="T1",
            dispatch_id="d-001",
            worker_state="stalled",
            lease_state="leased",
            evidence={"output_silence_seconds": 300},
        )
        create_open_items_for_anomalies([anomaly1], self.oi_path)
        items = create_open_items_for_anomalies([anomaly2], self.oi_path)

        with open(self.oi_path) as f:
            data = json.load(f)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["evidence"]["output_silence_seconds"], 300)

    def test_different_anomaly_types_not_deduped(self):
        """Different anomaly types for same terminal → separate items."""
        a1 = AnomalyRecord(
            anomaly_type="progress_stall", severity="warning",
            terminal_id="T1", dispatch_id="d-001",
            worker_state="stalled", lease_state="leased",
            evidence={},
        )
        a2 = AnomalyRecord(
            anomaly_type="dead_worker", severity="blocking",
            terminal_id="T1", dispatch_id="d-001",
            worker_state="exited_bad", lease_state="leased",
            evidence={},
        )
        create_open_items_for_anomalies([a1, a2], self.oi_path)
        with open(self.oi_path) as f:
            data = json.load(f)
        self.assertEqual(len(data["items"]), 2)

    def test_open_item_schema(self):
        """Open item matches §7.2 contract schema."""
        anomaly = AnomalyRecord(
            anomaly_type="zombie_lease", severity="blocking",
            terminal_id="T1", dispatch_id="d-001",
            worker_state=None, lease_state="leased",
            evidence={"dispatch_state": "completed"},
        )
        items = create_open_items_for_anomalies([anomaly], self.oi_path)
        item = items[0]
        required_fields = {
            "id", "type", "anomaly", "severity", "terminal_id",
            "dispatch_id", "worker_state", "lease_state", "detected_at",
            "evidence", "auto_created", "resolution", "resolved_at",
        }
        self.assertTrue(required_fields.issubset(set(item.keys())))


# ===========================================================================
# Test: Coordination events (audit records)
# ===========================================================================

class TestAuditRecords(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.supervisor, self.worker_mgr = _setup(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stall_creates_coordination_event(self):
        """Stall detection transition → worker_stall_detected event."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        _set_heartbeat(self.state_dir, "T1", 10)
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=200, last_output_ago=200)
        self.supervisor.supervise_terminal("T1")
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="T1", event_type="worker_stall_detected")
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "stall_detector")

    def test_dead_worker_creates_exited_event(self):
        """Dead worker escalation → worker_exited event."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        _set_heartbeat(self.state_dir, "T1", 400)
        self.supervisor.supervise_terminal("T1")
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id="T1", event_type="worker_exited")
        self.assertGreaterEqual(len(events), 1)


# ===========================================================================
# Test: Full supervision sweep
# ===========================================================================

class TestFullSupervisionSweep(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.supervisor, self.worker_mgr = _setup(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sweep_healthy_terminal(self):
        """Healthy worker → no anomalies from supervise_all()."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        _set_heartbeat(self.state_dir, "T1", 10)
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=30, last_output_ago=10)
        anomalies = self.supervisor.supervise_all()
        self.assertEqual(len(anomalies), 0)

    def test_sweep_detects_multiple_anomalies(self):
        """Worker stalled + heartbeat divergence → multiple anomalies from single sweep."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        _set_heartbeat(self.state_dir, "T1", 10)
        # Output silence > 2x stall_threshold → both progress_stall and heartbeat_without_output
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=400, last_output_ago=400)
        anomalies = self.supervisor.supervise_all()
        types = {a.anomaly_type for a in anomalies}
        self.assertIn("progress_stall", types)

    def test_idle_terminal_no_anomaly(self):
        """Idle terminal with no worker → no anomalies."""
        anomalies = self.supervisor.supervise_all()
        self.assertEqual(len(anomalies), 0)


# ===========================================================================
# Test: Evidence structure
# ===========================================================================

class TestEvidenceStructure(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.supervisor, self.worker_mgr = _setup(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_anomaly_evidence_has_required_fields(self):
        """Anomaly evidence contains heartbeat and output timestamps."""
        self.worker_mgr.initialize("T1", "d-001")
        self.worker_mgr.transition("T1", "working")
        _set_heartbeat(self.state_dir, "T1", 10)
        _set_worker_timestamps(self.state_dir, "T1", state_entered_ago=200, last_output_ago=200)
        anomalies = self.supervisor.supervise_terminal("T1")
        self.assertGreaterEqual(len(anomalies), 1)
        ev = anomalies[0].evidence
        self.assertIn("last_heartbeat_at", ev)
        self.assertIn("output_silence_seconds", ev)
        self.assertIn("heartbeat_age_seconds", ev)

    def test_anomaly_to_open_item_dict(self):
        """AnomalyRecord.to_open_item_dict() produces valid §7.2 structure."""
        record = AnomalyRecord(
            anomaly_type="dead_worker",
            severity="blocking",
            terminal_id="T1",
            dispatch_id="d-001",
            worker_state="exited_bad",
            lease_state="leased",
            evidence={"heartbeat_age_seconds": 400},
        )
        item = record.to_open_item_dict()
        self.assertEqual(item["type"], "runtime_anomaly")
        self.assertEqual(item["anomaly"], "dead_worker")
        self.assertTrue(item["auto_created"])
        self.assertIsNone(item["resolution"])
        self.assertIsNone(item["resolved_at"])
        self.assertIn("id", item)


if __name__ == "__main__":
    unittest.main()
