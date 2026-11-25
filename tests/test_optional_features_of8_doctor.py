"""OF-8: doctor reports fresh waivers and canonical CI conflicts, never guessed health."""
import argparse
import json

import pytest

import config_runtime
import gate_profile
import optional_features as of
from review_stack_support import isolate_config_registry
from test_optional_features import GATES, _yaml
from vnx_cli.commands import doctor


@pytest.fixture
def project(monkeypatch, tmp_path):
    isolation = isolate_config_registry(monkeypatch)
    next(isolation)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "global.json"))
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "required")
    monkeypatch.setattr(config_runtime, "autowire", lambda: True)
    monkeypatch.setattr(config_runtime, "strict_resolver", lambda: None)
    monkeypatch.setattr(doctor._engine, "derive_project_id", lambda root: "acme")
    monkeypatch.setattr(gate_profile, "enforcement_config_path", lambda: tmp_path / "dials")
    (tmp_path / "dials").write_text("checks: {}\n")
    (tmp_path / ".vnx").mkdir()
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
    ("unreadable", "ci_requirement_unreadable"),
])
def test_of8_refusal_is_doctor_failure(project, monkeypatch, bad, code):
    if bad == "unreadable":
        def broken(*args):
            raise OSError("store unreadable")
        monkeypatch.setattr(config_runtime, "strict_resolver", lambda: (broken, "acme"))
    else:
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", bad)
    checks = doctor._check_optional_features(project)
    assert len(checks) == 1 and checks[0].status == doctor.FAIL
    assert f"OptionalFeatureRefused: {code}" in checks[0].detail
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
        (project / "dials").write_text("checks:\n  ci_green_required:\n    level: 3\n")
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
