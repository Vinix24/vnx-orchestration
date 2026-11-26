"""OF-8: doctor reports fresh waivers and canonical CI conflicts, never guessed health."""
import argparse
import json
import sqlite3

import pytest

import config_runtime
import config_store_db
import gate_profile
import optional_features as of
from review_stack_support import isolate_config_registry
from test_optional_features import GATES, _yaml
from vnx_cli.commands import doctor


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


def test_of8_explicit_project_missing_store_fails(two_projects, capsys):
    target = two_projects[1]
    state = doctor._engine.resolve_data_root(target) / "state"
    (state / "runtime_coordination.db").rename(state / "saved.db")
    assert doctor.vnx_doctor(argparse.Namespace(project_dir=target, json=True, strict=False)) == 1
    output = json.loads(capsys.readouterr().out)
    check, = [c for c in output["checks"] if c["name"] == "optional_features"]
    assert check["status"] == doctor.FAIL
    assert "store_missing" in check["detail"] and str(state) in check["detail"]
    assert not (state / "runtime_coordination.db").exists()


@pytest.mark.parametrize("target_dials", [None, "checks: {}\n"])
def test_of8_explicit_project_ignores_ambient_dials(two_projects, target_dials):
    ambient, target = two_projects
    (ambient / ".vnx" / gate_profile.ENFORCEMENT_FILENAME).write_text(
        "checks:\n  ci_green_required:\n    level: 3\n")
    if target_dials is not None:
        (target / ".vnx" / gate_profile.ENFORCEMENT_FILENAME).write_text(target_dials)
    checks = doctor._check_optional_features(target)
    assert any("ci: VNX_CI_GATE_REQUIRED=0 (source: project_config)" in c.detail for c in checks)
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
    assert any("dial:ci_green_required" in c.detail for c in checks)


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


def test_of8_store_removed_after_wiring_still_fails(two_projects):
    target = two_projects[1]
    assert doctor._check_optional_features(target)[0].status == doctor.PASS
    state = doctor._engine.resolve_data_root(target) / "state"
    (state / "runtime_coordination.db").rename(state / "saved.db")
    check, = doctor._check_optional_features(target)
    assert check.status == doctor.FAIL and "store_missing" in check.detail


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
