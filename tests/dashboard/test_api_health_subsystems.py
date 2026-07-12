#!/usr/bin/env python3
"""Tests for api_health's subsystem effectiveness summary (framework-status-audit-and-cockpit
PR-18).

Covers GET /api/operator/health's new "subsystems" field: every known cockpit subsystem
(config_registry + effectiveness-probe registry, via subsystem_health.known_subsystems()) joined
against health_beacon.all_beacons(), defaulting an unprobed subsystem to health="unknown" rather
than omitting it -- the health page needs every subsystem to appear so "unknown" can prompt the
operator to add/improve a probe (PR-18 acceptance criterion). Read-only: the summary never runs a
probe itself (that stays owned by `vnx subsystems --probe`, PR-3/PR-5).
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "dashboard"))

import api_health  # noqa: E402
from health_beacon import HealthBeacon  # noqa: E402


def test_subsystem_effectiveness_summary_at_least_10_subsystems(tmp_path):
    rows = api_health._subsystem_effectiveness_summary(tmp_path)
    assert len(rows) >= 10
    subsystems = {r["subsystem"] for r in rows}
    assert "governance-enforcement-stack" in subsystems
    assert "phantom_guard" in subsystems  # flag-less kernel subsystem


def test_subsystem_effectiveness_summary_no_duplicates(tmp_path):
    rows = api_health._subsystem_effectiveness_summary(tmp_path)
    names = [r["subsystem"] for r in rows]
    assert len(names) == len(set(names))


def test_subsystem_effectiveness_summary_unknown_when_no_beacon(tmp_path):
    rows = api_health._subsystem_effectiveness_summary(tmp_path)
    governance = next(r for r in rows if r["subsystem"] == "governance-enforcement-stack")
    assert governance["health"] == "unknown"
    assert governance["status"] == "unknown"
    assert governance["last_signal"] == ""
    assert governance["detail"] == {}


def test_subsystem_effectiveness_summary_reads_ok_beacon(tmp_path):
    beacon = HealthBeacon(tmp_path, "phantom_guard", expected_interval_seconds=None)
    beacon.heartbeat_strict(status="ok", details={"duplicates": 0})

    rows = api_health._subsystem_effectiveness_summary(tmp_path)

    phantom = next(r for r in rows if r["subsystem"] == "phantom_guard")
    assert phantom["health"] == "ok"
    assert phantom["status"] == "ok"
    assert phantom["last_signal"]
    assert phantom["detail"] == {"duplicates": 0}


def test_subsystem_effectiveness_summary_fail_beacon(tmp_path):
    beacon = HealthBeacon(tmp_path, "intelligence-self-learning-loop", expected_interval_seconds=None)
    beacon.heartbeat_strict(status="fail", details={"ignore_rate": 0.98})

    rows = api_health._subsystem_effectiveness_summary(tmp_path)

    loop = next(r for r in rows if r["subsystem"] == "intelligence-self-learning-loop")
    assert loop["health"] == "fail"


def test_operator_get_health_includes_subsystems(monkeypatch, tmp_path):
    beacon = HealthBeacon(tmp_path, "phantom_guard", expected_interval_seconds=None)
    beacon.heartbeat_strict(status="ok")
    monkeypatch.setattr(api_health, "_resolve_data_dir", lambda: tmp_path)

    body = api_health._operator_get_health()

    assert "subsystems" in body
    assert len(body["subsystems"]) >= 10
    phantom = next(r for r in body["subsystems"] if r["subsystem"] == "phantom_guard")
    assert phantom["health"] == "ok"
    # Existing beacon-summary fields stay intact -- no regression for PR-1..8/17 consumers.
    assert "overall" in body
    assert "counts" in body
    assert "beacons" in body


def test_operator_get_health_error_path_reports_empty_subsystems(monkeypatch, tmp_path):
    def _boom(_data_dir):
        raise RuntimeError("boom")

    monkeypatch.setattr(api_health, "_resolve_data_dir", lambda: tmp_path)
    monkeypatch.setattr(api_health, "beacon_summary", _boom)

    body = api_health._operator_get_health()

    assert body["overall"] == "fail"
    assert body["subsystems"] == []
    assert "error" in body


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
