"""D6 fabric-state-herstel: system_health that tells the truth in the headless era.

Three behaviours:
  1. Only ``receipt_processor`` is still owed as a daemon; the tmux-era daemons
     are not expected, so a system where only it runs is ok (was degraded).
  2. ``beacon_summary`` no longer copies ``details`` verbatim: one beacon with
     5.000 rejected rows keeps ``system_health`` under a fixed byte bound.
  3. A locked/unreadable SQLite store reads degraded WITH its reason, via a
     read-only connection with a bounded busy timeout (ADR-007: this probe
     touches only the per-project ``quality_intelligence.db`` file, and the
     second project below proves a colliding beacon/component does not leak).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

import build_t0_state as bts  # noqa: E402
import daemon_register as dr  # noqa: E402
from health_beacon import beacon_summary  # noqa: E402

SYSTEM_HEALTH_MAX_BYTES = 8 * 1024

_SUPERVISOR = textwrap.dedent(
    """\
    RECEIPT_SERVICE_NAME="receipt_processor"
    RECEIPT_SCRIPT="receipt_processor.sh"

    start_all() {
        start_process "dispatcher" "dispatcher_minimal.sh"
        start_process "smart_tap" "smart_tap_json_translator.sh"
        start_process "$RECEIPT_SERVICE_NAME" "$RECEIPT_SCRIPT"
        start_process "heartbeat_ack_monitor" "heartbeat_ack_monitor.py"
        start_process "queue_watcher" "queue_popup_watcher.sh"
        start_process "dashboard" "generate_valid_dashboard.sh"
        start_process "state_manager" "unified_state_manager.py"
        start_process "intelligence_daemon" "intelligence_daemon.py"
        start_process "recommendations_engine" "recommendations_engine_daemon.sh"
    }
    """
)


def _fake_procs(*scripts: str) -> List[Any]:
    return [
        SimpleNamespace(info={"pid": 1000 + i, "cmdline": ["bash", f"/x/{name}"], "create_time": time.time()})
        for i, name in enumerate(scripts)
    ]


def _write_beacon(data_dir: Path, component: str, *, details: Dict[str, Any], status: str = "ok",
                  interval: int = 3600, age: int = 0) -> None:
    health_dir = data_dir / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    now = time.time() - age
    (health_dir / f"{component}.json").write_text(json.dumps({
        "component": component,
        "last_run_ts": int(now),
        "last_run_iso": "2026-09-29T00:00:00Z",
        "status": status,
        "details": details,
        "expected_interval_seconds": interval,
    }), encoding="utf-8")


class TestDaemonExpectation:
    def test_only_receipt_processor_running_is_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import psutil
        script = tmp_path / "vnx_supervisor_simple.sh"
        script.write_text(_SUPERVISOR, encoding="utf-8")
        monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: _fake_procs("receipt_processor.sh"))

        result = dr.measure_daemon_liveness(supervisor_script=script)

        assert result["overall"] == "ok"
        assert set(result["daemons"]) == {"receipt_processor"}

    def test_receipt_processor_absent_is_still_a_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import psutil
        script = tmp_path / "vnx_supervisor_simple.sh"
        script.write_text(_SUPERVISOR, encoding="utf-8")
        monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: _fake_procs("dispatcher_minimal.sh"))

        result = dr.measure_daemon_liveness(supervisor_script=script)

        assert result["overall"] == "fail"
        assert result["daemons"]["receipt_processor"]["state"] == "absent"

    def test_register_still_lists_every_supervised_daemon(self, tmp_path: Path) -> None:
        script = tmp_path / "vnx_supervisor_simple.sh"
        script.write_text(_SUPERVISOR, encoding="utf-8")
        assert len(dr.read_daemon_register(script)) == 9


def _health_with_giant_beacon(tmp_path: Path, name: str = "report_to_receipt_converter") -> Dict[str, Any]:
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "terminal_state.json").write_text("{}", encoding="utf-8")
    rejected = [
        {"report": f"20260929-{i:05d}-x.md", "reason": "missing ## Verification " * 6, "rejected_at": "2026-09-29T00:00:00Z"}
        for i in range(5000)
    ]
    _write_beacon(data_dir, name, status="fail", details={"rejected": rejected, "reason": "5000 reports rejected"})
    return bts._build_system_health(
        state_dir, db_initialized=True, expected_beacon_components=(),
        daemon_liveness={"overall": "ok", "daemons": {}},
        launchd_liveness={"overall": "ok", "jobs": {}},
    )


class TestBeaconBound:
    def test_five_thousand_rejected_rows_stay_under_bound(self, tmp_path: Path) -> None:
        health = _health_with_giant_beacon(tmp_path)
        size = len(json.dumps(health))
        assert size < SYSTEM_HEALTH_MAX_BYTES, f"system_health is {size} bytes"

    def test_beacon_carries_status_age_reason_and_count(self, tmp_path: Path) -> None:
        beacon = _health_with_giant_beacon(tmp_path)["beacon_health"]["beacons"]["report_to_receipt_converter"]
        assert beacon["health"] == "fail"
        assert beacon["status"] == "fail"
        assert beacon["age_seconds"] is not None
        assert beacon["reason"] == "5000 reports rejected"
        assert beacon["details_counts"] == {"rejected": 5000}
        assert "details" not in beacon

    def test_absent_and_corrupt_beacons_get_a_reason(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        (data_dir / "health").mkdir(parents=True)
        (data_dir / "health" / "broken.json").write_text("{nope", encoding="utf-8")
        summary = beacon_summary(data_dir, expected=["never_wrote"])
        assert summary["beacons"]["never_wrote"]["reason"] == "expected beacon was never written"
        assert summary["beacons"]["broken"]["health"] == "corrupt"
        assert summary["beacons"]["broken"]["reason"]

    def test_second_project_beacon_does_not_leak(self, tmp_path: Path) -> None:
        # Same component name in two projects' data dirs: each summary sees only its own rows.
        a = tmp_path / "proj_a"
        b = tmp_path / "proj_b"
        _write_beacon(a, "shared", status="fail", details={"rejected": [1, 2, 3]})
        _write_beacon(b, "shared", status="ok", details={"rejected": [1]})
        assert beacon_summary(a)["beacons"]["shared"]["details_counts"] == {"rejected": 3}
        assert beacon_summary(b)["beacons"]["shared"]["details_counts"] == {"rejected": 1}
        assert beacon_summary(b)["overall"] == "ok"

    def test_index_names_the_failing_beacon(self, tmp_path: Path) -> None:
        slim = bts._slim_health_for_index(_health_with_giant_beacon(tmp_path))
        assert slim["beacon_health"]["failing"] == ["report_to_receipt_converter"]
        assert len(json.dumps(slim)) < 1024


@pytest.fixture(scope="module")
def locked_health(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    """One full build against a locked quality_intelligence.db (other sections
    wait on the lock too, so it is built once and shared)."""
    tmp_path = tmp_path_factory.mktemp("locked")
    with pytest.MonkeyPatch.context() as monkeypatch:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        dispatch_dir = tmp_path / "dispatches"
        for sub in ("pending", "active", "conflicts"):
            (dispatch_dir / sub).mkdir(parents=True)
        (state_dir / "terminal_state.json").write_text("{}", encoding="utf-8")
        db = state_dir / "quality_intelligence.db"
        conn = sqlite3.connect(str(db), isolation_level=None)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE t (x)")
        conn.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(bts, "_init_and_check_db", lambda _sd: True)
        monkeypatch.setattr(bts, "_DB_PROBE_BUSY_TIMEOUT_SECONDS", 0.1, raising=False)
        monkeypatch.setattr(dr, "measure_daemon_liveness", lambda *_a, **_k: {"overall": "ok", "daemons": {}})
        monkeypatch.setattr(bts, "_measure_launchd_liveness", lambda *_a, **_k: {"overall": "ok", "jobs": {}})
        import beacon_register
        monkeypatch.setattr(beacon_register, "expected_component_names", lambda *_a, **_k: ())
        try:
            return bts.build_t0_state(state_dir, dispatch_dir)["system_health"]
        finally:
            conn.execute("ROLLBACK")
            conn.close()


class TestDbProbeReason:
    def test_locked_db_reads_degraded_with_reason(self, locked_health: Dict[str, Any]) -> None:
        health = locked_health
        assert health["status"] == "degraded"
        assert "locked" in health.get("db_reason", "")

    def test_index_carries_the_db_reason(self, locked_health: Dict[str, Any]) -> None:
        health = locked_health
        assert "locked" in bts._slim_health_for_index(health).get("db_reason", "")

    def test_probe_is_read_only(self, tmp_path: Path) -> None:
        db = tmp_path / "quality_intelligence.db"
        sqlite3.connect(str(db)).close()
        before = db.stat().st_mtime_ns
        assert bts._probe_db_health(db) == "healthy"
        assert db.stat().st_mtime_ns == before
