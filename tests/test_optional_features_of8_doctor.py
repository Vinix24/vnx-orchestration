"""OF-8: doctor reports fresh waivers and canonical CI conflicts, never guessed health."""
import argparse
import json
import sqlite3
import subprocess

import pytest

import config_runtime
import config_store_db
import gate_profile
import optional_features as of
import pr_merge
from review_stack_support import isolate_config_registry
from test_optional_features import GATES, _yaml
from vnx_cli.commands import doctor

REAL_DOOR_FEATURES = pr_merge._door_features


@pytest.fixture
def two_projects(monkeypatch, tmp_path):
    isolation = isolate_config_registry(monkeypatch)
    next(isolation)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "global.json"))
    monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "stores"))
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "required")
    projects = [tmp_path / name for name in ("project-a", "project-b")]
    for root in projects:
        (root / ".vnx").mkdir(parents=True)
        (root / ".vnx-project-id").write_text(root.name + "\n")
        (root / ".vnx" / "branch_protection.yaml").write_text(_yaml(GATES))
        state = tmp_path / "stores" / root.name / "state"
        state.mkdir(parents=True)
        (state.parent / ".vnx-project-id").write_text(root.name + "\n")
        with sqlite3.connect(state / "runtime_coordination.db") as conn:
            config_store_db.ensure_config_tables(conn)
            if root == projects[1]:
                conn.execute("INSERT INTO project_config "
                             "(project_id, config_key, config_value, updated_by) VALUES (?, ?, ?, ?)",
                             (root.name, "VNX_CI_GATE_REQUIRED", "0", "test"))
    ambient = projects[0]
    monkeypatch.chdir(ambient)
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "stores" / ambient.name / "state"))
    monkeypatch.setenv("PROJECT_ROOT", str(ambient))
    monkeypatch.setenv("VNX_HOME", str(ambient))
    assert doctor._engine.resolve_data_root(projects[1]) == tmp_path / "stores" / "project-b"
    assert config_runtime.autowire() is True
    yield projects
    next(isolation, None)


def test_of8_explicit_project_reads_own_store(two_projects, capsys):
    target = two_projects[1]
    doctor.vnx_doctor(argparse.Namespace(project_dir=target, json=True, strict=False))
    output = json.loads(capsys.readouterr().out)
    checks = [c for c in output["checks"] if c["name"] == "optional_features"]
    assert len(checks) == 1 and checks[0]["status"] == doctor.PASS
    assert "ci: VNX_CI_GATE_REQUIRED=0 (source: project_config)" in checks[0]["detail"]
    assert config_runtime.wire_status().project_id == target.name


def test_of8_explicit_project_missing_store_warns(two_projects, monkeypatch, capsys):
    from test_vnx_doctor_strict import _inject_launchd

    target = two_projects[1]
    _inject_launchd(monkeypatch, target.name)
    state = doctor._engine.resolve_data_root(target) / "state"
    (state / "runtime_coordination.db").rename(state / "saved.db")
    assert doctor.vnx_doctor(argparse.Namespace(project_dir=target, json=True, strict=False)) == 0
    output = json.loads(capsys.readouterr().out)
    check, = [c for c in output["checks"] if c["name"] == "optional_features"]
    assert check["status"] == doctor.WARN
    assert "store_missing" in check["detail"] and str(state / "runtime_coordination.db") in check["detail"]
    assert not (state / "runtime_coordination.db").exists()


@pytest.mark.parametrize("target_dials", [None, "checks: {}\n"])
def test_of8_explicit_project_shares_engine_dials(two_projects, target_dials):
    ambient, target = two_projects
    (ambient / ".vnx" / gate_profile.ENFORCEMENT_FILENAME).write_text(
        "checks:\n  ci_green_required:\n    level: 3\n")
    if target_dials is not None:
        (target / ".vnx" / gate_profile.ENFORCEMENT_FILENAME).write_text(target_dials)
    checks = doctor._check_optional_features(target)
    assert not any("ci: VNX_CI_GATE_REQUIRED=0 (source: project_config)" in c.detail for c in checks)
    assert any("has no effect: dial:ci_green_required" in c.detail for c in checks)


@pytest.mark.parametrize("engine_location, engine_level, project_level, required", [
    (".vnx", 3, None, True),
    (".vnx", 3, 0, True),
    (".vnx", 0, 3, False),
    (".", 3, None, True),
    (".", 3, 0, False),
])
def test_of8_doctor_dispatch_merge_share_dials(
        two_projects, tmp_path, monkeypatch, engine_location, engine_level, project_level, required):
    target = two_projects[1]
    engine = tmp_path / "engine"
    (engine / ".vnx").mkdir(parents=True)
    (engine / engine_location / gate_profile.ENFORCEMENT_FILENAME).write_text(
        f"checks:\n  ci_green_required:\n    level: {engine_level}\n")
    if project_level is not None:
        (target / ".vnx" / gate_profile.ENFORCEMENT_FILENAME).write_text(
            f"checks:\n  ci_green_required:\n    level: {project_level}\n")
    monkeypatch.setenv("VNX_HOME", str(engine))
    monkeypatch.setenv("PROJECT_ROOT", str(target))
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    state = doctor._engine.resolve_data_root(target) / "state"
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_PROJECT_ID", target.name)
    with sqlite3.connect(state / "runtime_coordination.db") as conn:
        conn.execute("DELETE FROM project_config WHERE project_id = ? AND config_key = ?",
                     (target.name, "VNX_CI_GATE_REQUIRED"))
    protection = target / ".vnx" / "branch_protection.yaml"
    monkeypatch.setattr(pr_merge, "_door_features", REAL_DOOR_FEATURES)
    monkeypatch.setattr(pr_merge, "active_store", lambda: (state, target.name))
    monkeypatch.setattr(pr_merge, "_gh", lambda *a, **k: subprocess.CompletedProcess([], 0, "a" * 40, ""))
    monkeypatch.setattr(pr_merge, "_fetch_protection_yaml", lambda *a: (
        str(protection), pr_merge.YamlFetchResult(text=protection.read_text(), not_found=False, error=None)))
    checks = doctor._check_optional_features(target)
    dispatch_ci = of.snapshot(target.name, project_root=target)["ci"]
    merge_ci = pr_merge._door_features()["ci"]
    assert all(c.status == doctor.PASS for c in checks)
    doctor_required = any(c.name == "optional_features:ci" for c in checks)
    assert (doctor_required, dispatch_ci.on, merge_ci.on) == (required,) * 3
    assert dispatch_ci == merge_ci
    assert dispatch_ci.on is required
    if required:
        assert dispatch_ci.source == "dial:ci_green_required"
        assert any(c.detail == dispatch_ci.conflict for c in checks)
        assert not any("ci: VNX_CI_GATE_REQUIRED=0" in c.detail for c in checks)
    else:
        assert any("ci: VNX_CI_GATE_REQUIRED=0 (source: env)" in c.detail for c in checks)
        assert not any("has no effect" in c.detail for c in checks)


@pytest.mark.parametrize("key, value, status, detail", [
    ("VNX_PLAN_GATE_ENFORCE", "off", doctor.PASS, "plan_gate: VNX_PLAN_GATE_ENFORCE=off (source: project_config)"),
    ("VNX_DEFAULT_REVIEW_STACK", "codex_gate,ci_gate", doctor.PASS, "review_stack:project_config"),
    ("VNX_GOVERNANCE_PROFILE", "unknown", doctor.FAIL, "VNX_GOVERNANCE_PROFILE"),
])
def test_of8_project_profile_stack_and_plan_reads(two_projects, key, value, status, detail):
    target = two_projects[1]
    db = doctor._engine.resolve_data_root(target) / "state" / "runtime_coordination.db"
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO project_config "
                     "(project_id, config_key, config_value, updated_by) VALUES (?, ?, ?, ?)",
                     (target.name, key, value, "test"))
    checks = doctor._check_optional_features(target)
    assert any(c.status == status and detail in c.detail for c in checks)


def test_of8_failed_autowire_is_named(two_projects, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("cannot construct resolver")
    monkeypatch.setattr(config_store_db, "make_db_resolver", broken)
    check, = doctor._check_optional_features(two_projects[1])
    assert check.status == doctor.FAIL and "resolver_failed" in check.detail


@pytest.mark.parametrize("overrides", [False, True])
def test_of8_corrupt_store_cannot_be_hidden(two_projects, monkeypatch, overrides):
    target = two_projects[1]
    db = doctor._engine.resolve_data_root(target) / "state" / "runtime_coordination.db"
    db.write_bytes(b"not a sqlite database")
    if overrides:
        monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "1")
        monkeypatch.setenv("VNX_OVERRIDE_PLAN_GATE_ENFORCE", "required")
    check, = doctor._check_optional_features(target)
    assert check.status == doctor.FAIL and "config_store_unreadable" in check.detail


@pytest.mark.parametrize("location", [".vnx", "."])
def test_of8_project_dials_are_read(two_projects, location):
    target = two_projects[1]
    (target / location / gate_profile.ENFORCEMENT_FILENAME).write_text(
        "checks:\n  ci_green_required:\n    level: 3\n")
    checks = doctor._check_optional_features(target)
    assert all(c.status == doctor.PASS for c in checks)
    assert any("dial:ci_green_required" in c.detail for c in checks) is (location == ".vnx")
    assert any("ci: VNX_CI_GATE_REQUIRED=0" in c.detail for c in checks) is (location == ".")


@pytest.mark.parametrize("bad", ["directory", "dangling", "malformed"])
def test_of8_unreadable_project_dials_fail(two_projects, bad):
    target = two_projects[1]
    path = target / ".vnx" / gate_profile.ENFORCEMENT_FILENAME
    if bad == "directory":
        path.mkdir()
    elif bad == "dangling":
        path.symlink_to(target / "absent.yaml")
    else:
        path.write_text("checks:\n  ci_green_required:\n    level: unknown\n")
    check, = doctor._check_optional_features(target)
    assert check.status == doctor.FAIL and "ci_requirement_unreadable" in check.detail
    assert str(path) in check.detail


def test_of8_store_removed_after_wiring_warns(two_projects):
    target = two_projects[1]
    assert doctor._check_optional_features(target)[0].status == doctor.PASS
    state = doctor._engine.resolve_data_root(target) / "state"
    (state / "runtime_coordination.db").rename(state / "saved.db")
    check, = doctor._check_optional_features(target)
    assert check.status == doctor.WARN and "store_missing" in check.detail


@pytest.mark.parametrize("bad", ["directory", "dangling"])
def test_of8_invalid_store_path_stays_failure(two_projects, bad):
    target = two_projects[1]
    db = doctor._engine.resolve_data_root(target) / "state" / "runtime_coordination.db"
    db.rename(db.with_name("saved.db"))
    if bad == "directory":
        db.mkdir()
    else:
        db.symlink_to(db.with_name("absent.db"))
    check, = doctor._check_optional_features(target)
    assert check.status == doctor.FAIL


def test_of8_store_read_oserror_stays_failure(two_projects, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("config read denied")
    monkeypatch.setattr(config_store_db, "read_config", denied)
    check, = doctor._check_optional_features(two_projects[1])
    assert check.status == doctor.FAIL and "config read denied" in check.detail


@pytest.fixture
def project(monkeypatch, tmp_path):
    isolation = isolate_config_registry(monkeypatch)
    next(isolation)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "global.json"))
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "required")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "data" / "state"))
    monkeypatch.setenv("VNX_PROJECT_ID", "acme")
    monkeypatch.setenv("VNX_HOME", str(tmp_path))
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    (tmp_path / "data" / "state").mkdir(parents=True)
    with sqlite3.connect(tmp_path / "data" / "state" / "runtime_coordination.db") as conn:
        config_store_db.ensure_config_tables(conn)
    (tmp_path / ".vnx-project-id").write_text("acme\n")
    (tmp_path / ".vnx").mkdir()
    (tmp_path / ".vnx" / gate_profile.ENFORCEMENT_FILENAME).write_text("checks: {}\n")
    (tmp_path / ".vnx" / "branch_protection.yaml").write_text(_yaml(GATES))
    yield tmp_path
    next(isolation, None)


def test_of8_doctor_current_state_ci_row(project, capsys):
    doctor.vnx_doctor(argparse.Namespace(project_dir=project, json=False, strict=False))
    output = capsys.readouterr().out
    assert "current state" in output
    assert "ci: VNX_CI_GATE_REQUIRED=0 (source: env)" in output
    assert of.FEATURE_BY_NAME["ci"].off["0"] in output


def test_of8_nothing_waived_is_one_line_and_fresh(project, monkeypatch):
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "1")
    checks = doctor._check_optional_features(project)
    assert checks == [doctor.Check("optional_features", doctor.PASS, "nothing waived")]
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    assert "ci: VNX_CI_GATE_REQUIRED=0" in doctor._check_optional_features(project)[0].detail


@pytest.mark.parametrize("bad, code", [
    ("bogus", "plan_gate_mode_unknown"), ("", "plan_gate_mode_unknown"),
    ("unreadable", "config_store_unreadable"),
])
def test_of8_refusal_is_doctor_failure(project, monkeypatch, bad, code):
    if bad == "unreadable":
        (project / "data" / "state" / "runtime_coordination.db").write_bytes(b"not a sqlite database")
    else:
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", bad)
    checks = doctor._check_optional_features(project)
    assert len(checks) == 1 and checks[0].status == doctor.FAIL
    assert code in checks[0].detail
    assert "nothing waived" not in checks[0].detail


def test_of8_refusal_reaches_json_and_exit_status(project, monkeypatch, capsys):
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "bogus")
    assert doctor.vnx_doctor(argparse.Namespace(project_dir=project, json=True, strict=False)) == 1
    output = json.loads(capsys.readouterr().out)
    check = next(c for c in output["checks"] if c["name"] == "optional_features")
    assert check["status"] == doctor.FAIL and "plan_gate_mode_unknown" in check["detail"]


@pytest.mark.parametrize("raw, source", [(None, "env"), ("", "env"), ("bogus", "env"),
                                       ("0", "project_config_error")])
def test_of8_unreadable_request_after_ci_decision_stays_failure(project, monkeypatch, raw, source):
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "1")
    monkeypatch.setattr(of, "_resolve", lambda *a: (raw, source))
    check, = doctor._check_optional_features(project)
    assert check.status == doctor.FAIL and "ci_requirement_unreadable" in check.detail


@pytest.mark.parametrize("source", ["dial", "protection", "stack"])
def test_of8_conflict_uses_canonical_ci_sources(project, monkeypatch, source):
    if source == "dial":
        (project / ".vnx" / gate_profile.ENFORCEMENT_FILENAME).write_text(
            "checks:\n  ci_green_required:\n    level: 3\n")
    elif source == "protection":
        (project / ".vnx" / "branch_protection.yaml").write_text(_yaml([*GATES, "CI"]))
    else:
        monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "codex_gate,ci_gate")
    taken = of.snapshot("acme", project_root=project)
    checks = doctor._check_optional_features(project)
    conflict = next(c for c in checks if "has no effect" in c.detail)
    assert "VNX_CI_GATE_REQUIRED=0" in conflict.detail
    assert taken["ci"].source in conflict.detail
    assert not of.waived_guarantees("acme", snapshot=taken)
