#!/usr/bin/env python3
"""Tests for scripts/lib/runtime_supervise.py — the CLI tick."""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

import runtime_supervise  # noqa: E402
from runtime_supervisor import AnomalyRecord  # noqa: E402


def _make_anomaly(anomaly_type: str = "progress_stall",
                  severity: str = "warning",
                  terminal_id: str = "T1",
                  dispatch_id: str | None = "d-001") -> AnomalyRecord:
    return AnomalyRecord(
        anomaly_type=anomaly_type,
        severity=severity,
        terminal_id=terminal_id,
        dispatch_id=dispatch_id,
        worker_state="working",
        lease_state="leased",
        evidence={"output_silence_seconds": 240},
    )


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    # Force append_event to write inside the test state dir.
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    return state


def _run_cli(state_dir: Path, anomalies: list[AnomalyRecord], extra_argv=None):
    extra_argv = extra_argv or []
    out = io.StringIO()
    err = io.StringIO()
    with patch.object(runtime_supervise, "RuntimeSupervisor") as MockSupervisor:
        instance = MockSupervisor.return_value
        instance.supervise_all.return_value = anomalies
        argv = ["--state-dir", str(state_dir), *extra_argv]
        with redirect_stdout(out), redirect_stderr(err):
            rc = runtime_supervise.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _read_register(state_dir: Path) -> list[dict]:
    path = state_dir / "dispatch_register.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _read_open_items(state_dir: Path) -> list[dict]:
    path = state_dir / "open_items.json"
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("items", [])


def test_case_a_no_anomalies(state_dir):
    rc, stdout, stderr = _run_cli(state_dir, [])
    assert rc == 0
    assert "0 anomalies" in stdout
    assert stderr == ""
    assert _read_register(state_dir) == []
    assert _read_open_items(state_dir) == []


def test_case_b_advisory_anomaly_emitted_no_oi(state_dir):
    anomaly = _make_anomaly(severity="warning")
    rc, stdout, stderr = _run_cli(state_dir, [anomaly])
    assert rc == 0
    assert "1 anomalies" in stdout
    # Stderr structured log emitted
    err_lines = [json.loads(l) for l in stderr.strip().splitlines()]
    assert len(err_lines) == 1
    assert err_lines[0]["anomaly"] == "progress_stall"
    assert err_lines[0]["severity"] == "warning"
    # Register has one entry
    events = _read_register(state_dir)
    assert len(events) == 1
    assert events[0]["event"] == "runtime_anomaly_detected"
    assert events[0]["dispatch_id"] == "d-001"
    assert events[0]["extra"]["severity"] == "warning"
    # No OI for non-blocking anomaly
    assert _read_open_items(state_dir) == []


def test_case_c_blocker_anomaly_creates_oi(state_dir):
    anomaly = _make_anomaly(anomaly_type="zombie_lease", severity="blocking")
    rc, _stdout, _stderr = _run_cli(state_dir, [anomaly])
    assert rc == 0
    events = _read_register(state_dir)
    assert len(events) == 1
    assert events[0]["extra"]["severity"] == "blocking"
    items = _read_open_items(state_dir)
    assert len(items) == 1
    assert items[0]["type"] == "runtime_anomaly"
    assert items[0]["anomaly"] == "zombie_lease"
    assert items[0]["severity"] == "blocking"


def test_case_d_no_oi_flag_skips_oi_write(state_dir):
    anomaly = _make_anomaly(anomaly_type="dead_worker", severity="blocking")
    rc, _stdout, _stderr = _run_cli(state_dir, [anomaly], extra_argv=["--no-oi"])
    assert rc == 0
    # Register still gets the entry
    events = _read_register(state_dir)
    assert len(events) == 1
    # But no OI written
    assert _read_open_items(state_dir) == []


def test_case_e_json_flag_emits_valid_json(state_dir):
    anomaly = _make_anomaly(severity="warning")
    rc, stdout, _stderr = _run_cli(state_dir, [anomaly], extra_argv=["--json"])
    assert rc == 0
    payload = json.loads(stdout.strip())
    assert payload["count"] == 1
    assert payload["blocking_count"] == 0
    assert payload["open_items_written"] == 0
    assert payload["anomalies"][0]["anomaly_type"] == "progress_stall"


def test_case_f_idempotent_no_duplicate_oi(state_dir):
    anomaly = _make_anomaly(anomaly_type="zombie_lease", severity="blocking")
    rc1, _o1, _e1 = _run_cli(state_dir, [anomaly])
    rc2, _o2, _e2 = _run_cli(state_dir, [anomaly])
    assert rc1 == 0 and rc2 == 0
    items = _read_open_items(state_dir)
    # OI dedup: same (terminal, dispatch, anomaly_type) → single open item
    assert len(items) == 1
    assert items[0]["anomaly"] == "zombie_lease"
    # Register, however, is append-only and grows.
    events = _read_register(state_dir)
    assert len(events) == 2


def test_anomaly_without_dispatch_id_still_registers(state_dir):
    anomaly = AnomalyRecord(
        anomaly_type="recovery_timeout",
        severity="blocking",
        terminal_id="T2",
        dispatch_id=None,
        worker_state=None,
        lease_state="recovering",
        evidence={"recovery_age_seconds": 700},
    )
    rc, _stdout, _stderr = _run_cli(state_dir, [anomaly])
    assert rc == 0
    events = _read_register(state_dir)
    assert len(events) == 1
    assert events[0]["dispatch_id"] == "anomaly:T2:recovery_timeout"
    assert events[0]["terminal"] == "T2"
