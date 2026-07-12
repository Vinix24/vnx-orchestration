#!/usr/bin/env python3
"""Tests for api_subsystems — the subsystem cockpit HTTP handler (framework-status-audit-and-cockpit
PR-4).

Covers GET /api/operator/subsystems: rowset shape (union of CONFIG_REGISTRY_SUBSYSTEMS + the
canonical-flag-per-subsystem view of CONFIG_REGISTRY), health attachment from health_beacon
(VNX_DATA_DIR root, not VNX_STATE_DIR), and the 503 fail-open path when the registry is unavailable.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "dashboard"))

import config_registry as cr  # noqa: E402
import api_subsystems as api_sub  # noqa: E402
from health_beacon import HealthBeacon  # noqa: E402

PID = "vnx-dev"


def test_build_rows_returns_at_least_10_subsystems():
    rows = api_sub.build_rows(cr, PID)
    assert len(rows) >= 10
    subsystems = {r["subsystem"] for r in rows}
    assert "governance-enforcement-stack" in subsystems
    assert "phantom_guard" in subsystems  # flag-less kernel subsystem


def test_build_rows_no_duplicate_subsystems():
    rows = api_sub.build_rows(cr, PID)
    subsystems = [r["subsystem"] for r in rows]
    assert len(subsystems) == len(set(subsystems))


def test_build_rows_flag_backed_row_has_effective_value():
    rows = api_sub.build_rows(cr, PID)
    governance = next(r for r in rows if r["subsystem"] == "governance-enforcement-stack")
    assert governance["flag"] == "VNX_GOVERNANCE_ENFORCED"
    assert governance["status"] == "PARK"
    assert governance["effective_value"] == "0"  # default off


def test_build_rows_flag_less_row_has_no_flag():
    rows = api_sub.build_rows(cr, PID)
    phantom = next(r for r in rows if r["subsystem"] == "phantom_guard")
    assert phantom["flag"] is None
    assert phantom["status"] == "LIVE"
    assert phantom["effective_value"] is None


def test_attach_health_unknown_when_no_beacon(tmp_path):
    rows = [{"subsystem": "phantom_guard"}]
    api_sub._attach_health(rows, tmp_path)
    assert rows[0]["health"] == "unknown"
    assert rows[0]["last_signal"] == ""


def test_attach_health_reads_beacon(tmp_path):
    beacon = HealthBeacon(tmp_path, "phantom_guard", expected_interval_seconds=None)
    beacon.heartbeat_strict(status="ok", details={"signal": "zero duplicates"})
    rows = [{"subsystem": "phantom_guard"}]
    api_sub._attach_health(rows, tmp_path)
    assert rows[0]["health"] == "ok"
    assert rows[0]["last_signal"]


def test_attach_health_fail_beacon(tmp_path):
    beacon = HealthBeacon(tmp_path, "intelligence-self-learning-loop", expected_interval_seconds=None)
    beacon.heartbeat_strict(status="fail", details={"signal": "98% ignore rate"})
    rows = [{"subsystem": "intelligence-self-learning-loop"}]
    api_sub._attach_health(rows, tmp_path)
    assert rows[0]["health"] == "fail"


def test_operator_get_subsystems_returns_200_with_health(monkeypatch, tmp_path):
    beacon = HealthBeacon(tmp_path, "phantom_guard", expected_interval_seconds=None)
    beacon.heartbeat_strict(status="ok")
    monkeypatch.setattr(api_sub, "_resolve_data_dir", lambda: tmp_path)

    body, status = api_sub.operator_get_subsystems({}, project_id=PID)

    assert status == 200
    assert body["project_id"] == PID
    assert len(body["subsystems"]) >= 10
    phantom = next(r for r in body["subsystems"] if r["subsystem"] == "phantom_guard")
    assert phantom["health"] == "ok"
    governance = next(r for r in body["subsystems"] if r["subsystem"] == "governance-enforcement-stack")
    assert governance["health"] == "unknown"  # no beacon written for it


def test_operator_get_subsystems_unavailable_returns_503(monkeypatch):
    monkeypatch.setattr(api_sub, "_REGISTRY_AVAILABLE", False)
    body, status = api_sub.operator_get_subsystems({}, project_id=PID)
    assert status == 503
    assert body["subsystems"] == []
    assert "error" in body
