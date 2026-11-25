"""M6: a stopped collector is visible to every beacon reader and the T0 index."""
from datetime import datetime, timedelta, timezone
import builtins
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_t0_state as bts
import health_beacon
import beacon_register
from daily_metrics import collect, days
from daily_metrics.store import DayStore


NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)


def measurement_store(tmp_path, monkeypatch, age=37, final=True):
    measured = NOW - timedelta(hours=age)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".vnx-project-id").write_text("vnx-dev\n")
    state = tmp_path / "state"
    store = DayStore(state / "daily_metrics" / "vnx-dev", "vnx-dev")
    for day in days.completed_days(measured)[-collect.BEACON_SETTLED_WINDOW:]:
        doc = {"day": day.isoformat(), "project_id": "vnx-dev", "collected_at": measured.isoformat()}
        (store.write_final if final else store.write_incomplete)(day.isoformat(), doc)
    monkeypatch.setattr(health_beacon.time, "time", lambda: measured.timestamp())
    collect.write_beacon(state, store, measured)
    monkeypatch.setattr(health_beacon.time, "time", lambda: NOW.timestamp())
    return state


def t0_health(state, **kwargs):
    return bts._build_system_health(
        state, True, daemon_liveness={"overall": "ok"},
        launchd_liveness={"overall": "ok"}, processor_liveness={"overall": "ok"},
        **kwargs,
    )


def test_nonparticipating_project_is_not_expected_or_degraded(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / ".vnx-project-id").write_text("vnx-dev\n")
    (state / "t0_receipts.ndjson").write_text("")
    expected = beacon_register.expected_component_names()
    for component in expected:
        if component != "daily_metrics":
            health_beacon.HealthBeacon(tmp_path, component).heartbeat_strict()
    health = t0_health(state)
    assert "daily_metrics" not in expected
    assert "daily_metrics" not in health["beacon_health"]["beacons"]
    assert health["beacon_health"]["overall"] == "ok"
    assert health["status"] == "healthy"
    assert "failing" not in bts._slim_health_for_index(health)["beacon_health"]


def test_measurement_37_hours_old_fails(tmp_path, monkeypatch):
    state = measurement_store(tmp_path, monkeypatch)
    beacon = health_beacon.all_beacons(state)["daily_metrics"]
    assert beacon["health"] == "fail"
    assert beacon["reason"] == "no_measurement_36h"
    assert beacon["age_hours"] == 37


@pytest.mark.parametrize("location", [".", "state"])
def test_beacon_alone_establishes_participation(tmp_path, monkeypatch, location):
    (tmp_path / ".vnx-project-id").write_text("vnx-dev\n")
    monkeypatch.setattr(health_beacon.time, "time", lambda: (NOW - timedelta(hours=37)).timestamp())
    health_beacon.HealthBeacon(tmp_path / location, "daily_metrics", None).heartbeat_strict()
    monkeypatch.setattr(health_beacon.time, "time", lambda: NOW.timestamp())
    beacon = t0_health(tmp_path / "state")["beacon_health"]["beacons"]["daily_metrics"]
    assert beacon["health"] == "fail"
    assert beacon["reason"] == "no_measurement_36h" and beacon["age_hours"] == 37


@pytest.mark.parametrize("age,expected", [(35, "ok"), (36, "ok"), (37, "fail")])
def test_shared_readers_and_t0_agree(tmp_path, monkeypatch, capsys, age, expected):
    import health_check
    from dashboard import api_health

    state = measurement_store(tmp_path, monkeypatch, age)
    summary = health_beacon.beacon_summary(tmp_path, expected=["daily_metrics"])
    assert summary["overall"] == expected
    assert summary["beacons"]["daily_metrics"]["health"] == expected
    assert t0_health(state)["beacon_health"]["beacons"]["daily_metrics"]["health"] == expected
    assert health_check.main(["--state-dir", str(tmp_path), "--components", "daily_metrics", "--json"]) == (expected == "fail")
    assert json.loads(capsys.readouterr().out)["beacons"]["daily_metrics"]["health"] == expected
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    assert api_health._operator_get_health()["beacons"]["daily_metrics"]["health"] == expected


@pytest.mark.parametrize("case,reason", [("absent", "never_measured"), ("old", "no_measurement_36h"),
                                         ("incomplete", "no_final_settled_day")])
def test_t0_index_names_project_even_with_many_failures(tmp_path, monkeypatch, case, reason):
    if case == "absent":
        (tmp_path / ".vnx-project-id").write_text("vnx-dev\n")
        state = tmp_path / "state"
    else:
        state = measurement_store(tmp_path, monkeypatch, 37 if case == "old" else 0, case != "incomplete")
    # Absence is a failure only for an explicitly expected project.
    health = t0_health(state, expected_beacon_components=["daily_metrics"]) if case == "absent" else t0_health(state)
    beacon = health["beacon_health"]["beacons"]["daily_metrics"]
    assert beacon["health"] == "fail"
    assert beacon["reason"] == reason
    line = bts._slim_health_for_index(health)["beacon_health"]["failing"][0]
    assert line.startswith("daily_metrics (vnx-dev): ") and reason in line
    if case == "old":
        assert "37 h" in line


@pytest.mark.parametrize("legacy_interval", [None, 999999999])
def test_existing_beacons_and_canonical_path(tmp_path, monkeypatch, legacy_interval):
    state = measurement_store(tmp_path, monkeypatch)
    path = state / "health" / "daily_metrics.json"
    payload = json.loads(path.read_text())
    payload["expected_interval_seconds"] = legacy_interval
    path.write_text(json.dumps(payload))
    health_beacon.HealthBeacon(tmp_path, "daily_metrics").heartbeat()
    assert health_beacon.all_beacons(tmp_path)["daily_metrics"]["reason"] == "no_measurement_36h"


@pytest.mark.parametrize("field", ["last_run_ts", "status"])
@pytest.mark.parametrize("value", ["missing", None, {}, "unknown", float("nan"), float("inf"), True, 10**400])
def test_invalid_evidence_never_passes(tmp_path, monkeypatch, field, value):
    state = measurement_store(tmp_path, monkeypatch, age=0)
    path = state / "health" / "daily_metrics.json"
    payload = json.loads(path.read_text())
    payload.pop(field) if value == "missing" else payload.update({field: value})
    payload["age_seconds"] = "untrusted derived field"
    path.write_text(json.dumps(payload))
    beacon = health_beacon.beacon_summary(tmp_path)["beacons"]["daily_metrics"]
    assert beacon["health"] in ("fail", "corrupt") and beacon["reason"]


@pytest.mark.parametrize("broken", ["json", "read", "future"])
def test_unreadable_or_future_beacon_is_visible(tmp_path, monkeypatch, broken):
    state = measurement_store(tmp_path, monkeypatch, age=0)
    path = state / "health" / "daily_metrics.json"
    if broken == "json":
        path.write_text("{")
    elif broken == "future":
        payload = json.loads(path.read_text())
        payload["last_run_ts"] = NOW.timestamp() + 1
        path.write_text(json.dumps(payload))
    else:
        health_beacon.HealthBeacon(tmp_path, "daily_metrics").heartbeat()
        original = builtins.open

        def denied(target, *args, **kwargs):
            if target == path:
                raise PermissionError("measurement unreadable")
            return original(target, *args, **kwargs)

        monkeypatch.setattr("builtins.open", denied)
    beacon = t0_health(state)["beacon_health"]["beacons"]["daily_metrics"]
    assert beacon["health"] == "corrupt" and beacon["reason"]


def test_explicit_project_root_wins_over_environment_and_other_project(tmp_path, monkeypatch):
    first = measurement_store(tmp_path / "first", monkeypatch)
    second = measurement_store(tmp_path / "second", monkeypatch, age=0)
    (second.parent / ".vnx-project-id").write_text("vnx-dev-other\n")
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev-other")
    monkeypatch.setenv("VNX_STATE_DIR", str(second))
    line = bts._slim_health_for_index(t0_health(first))["beacon_health"]["failing"][0]
    assert line.startswith("daily_metrics (vnx-dev): ") and "37 h" in line


@pytest.mark.parametrize("location", ["health", "state/health"])
@pytest.mark.parametrize("broken", ["read", "json", "symlink", "stat"])
def test_unreadable_opt_in_signal_is_named_without_glob(tmp_path, monkeypatch, location, broken):
    (tmp_path / ".vnx-project-id").write_text("vnx-dev\n")
    path = tmp_path / location / "daily_metrics.json"
    path.parent.mkdir(parents=True)
    if broken == "symlink":
        path.symlink_to(tmp_path / "missing-beacon")
    elif broken == "json":
        path.write_text("{")
    elif broken == "stat":
        original_stat = Path.lstat

        def denied_stat(target, *args, **kwargs):
            if target == path:
                raise PermissionError("opt-in metadata unreadable")
            return original_stat(target, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", denied_stat)
    else:
        original = builtins.open

        def denied(target, *args, **kwargs):
            if target == path:
                raise PermissionError("opt-in signal unreadable")
            return original(target, *args, **kwargs)

        monkeypatch.setattr("builtins.open", denied)
    monkeypatch.setattr(Path, "glob", lambda *args: iter(()))
    health = t0_health(tmp_path / "state")
    beacon = health["beacon_health"]["beacons"]["daily_metrics"]
    assert beacon["health"] == "corrupt" and beacon["reason"]
    if broken in ("read", "stat"):
        assert "unreadable" in beacon["reason"]
    assert health["status"] == "degraded"
    assert bts._slim_health_for_index(health)["beacon_health"]["failing"][0].startswith("daily_metrics (vnx-dev): ")


def test_other_projects_beacon_does_not_opt_in_empty_project(tmp_path, monkeypatch):
    other = measurement_store(tmp_path / "vnx-dev-other", monkeypatch)
    local = tmp_path / "vnx-dev"
    local.mkdir()
    (local / ".vnx-project-id").write_text("vnx-dev\n")
    monkeypatch.setenv("VNX_STATE_DIR", str(other))
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev-other")
    assert "daily_metrics" not in t0_health(local / "state")["beacon_health"]["beacons"]
