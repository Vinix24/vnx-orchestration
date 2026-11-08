#!/usr/bin/env python3
"""`vnx gate-profile` and `gate_profile.required_stack` (track gate-profiel-per-project, P7).

The CLI, `gate.sh` and the merge door read one function. These tests run the real CLI (in
process for the drift check, in a subprocess for exit codes) against a temp state dir and a
temp enforcement config, never the real central store.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "scripts" / "gate_profile_cli.py"
for _p in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "lib"):
    sys.path.insert(0, str(_p))

import gate_profile
import gate_profile_cli
import vnx_mode
from dispatch_spec import ReviewGateConfigError

VNX_DEV_DIALS = """\
version: 1
checks:
  codex_gate_required:
    level: 2
  kimi_gate_required:
    level: 2
  ci_green_required:
    level: 3
"""

AMBIENT = (
    "VNX_GOVERNANCE_PROFILE", "VNX_DEFAULT_REVIEW_STACK", "VNX_CI_GATE_REQUIRED", "VNX_PROJECT_ID",
    "VNX_OVERRIDE_GOVERNANCE_PROFILE", "VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "VNX_OVERRIDE_CI_GATE_REQUIRED",
    "VNX_HOME", "PROJECT_ROOT",
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A clean environment: no ambient profile or stack key, state and VNX_HOME in tmp."""
    for key in AMBIENT:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "home" / ".vnx").mkdir(parents=True)
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VNX_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _dials(home: Path, body: str) -> Path:
    path = home / ".vnx" / "governance_enforcement.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _run_cli(*args: str, extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()}
    env.update(extra or {})
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=60, env=env)


# --- required_stack -------------------------------------------------------------------------

def test_no_config_standard_profile_is_the_default_review_stack(home):
    names, provenance = gate_profile.required_stack()
    assert names == ["codex_gate", "kimi_gate"]
    assert provenance == {"codex_gate": "default", "kimi_gate": "default"}


def test_c6_lock_measured_dials_give_the_stack_the_door_asks_today(home):
    _dials(home, VNX_DEV_DIALS)
    names, provenance = gate_profile.required_stack()
    assert names == ["codex_gate", "kimi_gate", "ci_gate"]
    assert provenance["ci_gate"] == "enforcement"


def test_heavy_profile_names_three_seats_with_profile_provenance(home, monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
    names, provenance = gate_profile.required_stack()
    assert names == ["codex_gate", "kimi_gate", "claude_gate"]
    assert set(provenance.values()) == {"profile"}


def test_enforcement_outranks_the_light_profile_and_adds_the_mandatory_seat(home, monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")
    _dials(home, VNX_DEV_DIALS)
    names, provenance = gate_profile.required_stack()
    assert names == ["codex_gate", "kimi_gate", "ci_gate"]
    assert set(provenance.values()) == {"enforcement"}  # the config defines the stack


def test_level_1_is_advisory_and_keeps_the_review_stack_level_0_removes_the_seat(home):
    _dials(home, "checks:\n  kimi_gate_required:\n    level: 1\n")
    assert gate_profile.required_stack()[0] == ["codex_gate", "kimi_gate"]
    _dials(home, "checks:\n  kimi_gate_required:\n    level: 0\n")
    assert gate_profile.required_stack()[0] == ["codex_gate"]


def test_ci_gate_joins_on_level_2_and_on_an_explicit_env_flag_but_not_on_the_registry_default(home, monkeypatch):
    assert "ci_gate" not in gate_profile.required_stack()[0]
    _dials(home, "checks:\n  ci_green_required:\n    level: 2\n")
    assert gate_profile.required_stack()[0][-1] == "ci_gate"
    _dials(home, "checks:\n  ci_green_required:\n    level: 1\n")
    assert "ci_gate" not in gate_profile.required_stack()[0]
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "yes")
    names, provenance = gate_profile.required_stack()
    assert names[-1] == "ci_gate" and provenance["ci_gate"] == "env"


def test_the_ci_env_flag_set_false_does_not_add_ci_gate(home, monkeypatch):
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    assert "ci_gate" not in gate_profile.required_stack()[0]


def test_config_search_order_is_vnx_home_dot_vnx_then_project_dot_vnx_then_vnx_home(home, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".vnx").mkdir(parents=True)
    monkeypatch.setenv("PROJECT_ROOT", str(project))
    (home / "governance_enforcement.yaml").write_text("checks:\n  codex_gate_required:\n    level: 0\n")
    assert gate_profile.enforcement_config_path() == home / "governance_enforcement.yaml"
    (project / ".vnx" / "governance_enforcement.yaml").write_text("checks:\n  codex_gate_required:\n    level: 1\n")
    assert gate_profile.enforcement_config_path() == project / ".vnx" / "governance_enforcement.yaml"
    _dials(home, "checks:\n  codex_gate_required:\n    level: 2\n")
    assert gate_profile.enforcement_config_path() == home / ".vnx" / "governance_enforcement.yaml"


@pytest.mark.parametrize("body, reason", [
    ("checks:\n  codex_gate_required:\n    level: hoog\n", "not an integer"),
    ("checks:\n  codex_gate_required:\n    level:\n", "not an integer"),
    ("checks:\n  codex_gate_required:\n    description: x\n", "no level"),
    ("checks:\n  kimi_gate_required:\n    description: x\n  ci_green_required:\n    level: 3\n", "no level"),
])
def test_an_unreadable_dial_refuses_instead_of_guessing(home, body, reason):
    _dials(home, body)
    with pytest.raises(ReviewGateConfigError, match=reason):
        gate_profile.required_stack()


def test_an_unreadable_config_file_refuses(home):
    path = _dials(home, "checks:\n")
    path.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ReviewGateConfigError):
        gate_profile.required_stack()


def test_an_empty_result_refuses(home, monkeypatch):
    monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "ci_gate")
    names, _ = gate_profile.required_stack()
    assert names == ["ci_gate"]  # an explicit stack naming only ci_gate is the operator's own choice
    _dials(home, "checks:\n  ci_green_required:\n    level: 0\n")
    with pytest.raises(ReviewGateConfigError, match="empty"):
        gate_profile.required_stack()
    monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", " , ")
    with pytest.raises(ReviewGateConfigError, match="empty"):
        gate_profile.required_stack()


def test_an_invalid_profile_raises_the_profile_error(home, monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "zwaar")
    with pytest.raises(gate_profile.GateProfileConfigError):
        gate_profile.required_stack()


def test_an_operator_stack_override_wins_over_the_profile_in_required_stack(home, monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
    monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "glm_gate")
    names, provenance = gate_profile.required_stack()
    assert names == ["glm_gate"] and provenance == {"glm_gate": "override"}


# --- the CLI --------------------------------------------------------------------------------

@pytest.mark.parametrize("profile", [None, "light", "standard", "heavy"])
def test_show_json_equals_what_the_module_returns_field_by_field(home, monkeypatch, capsys, profile):
    if profile:
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", profile)
    _dials(home, VNX_DEV_DIALS)
    assert gate_profile_cli.main(["show", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    resolution = gate_profile.resolve()
    names, provenance = gate_profile.required_stack()
    assert set(shown) == {"profile", "source", "raw", "error", "request_stack", "required_stack", "values"}
    assert (shown["profile"], shown["source"], shown["raw"], shown["error"]) == (
        resolution.profile, resolution.source, resolution.raw, None,
    )
    assert shown["required_stack"] == {"names": names, "provenance": provenance}
    assert shown["request_stack"]["names"] == gate_profile.review_stack(None, resolution)[0]
    for field in gate_profile.FIELDS:
        assert shown["values"][field]["value"] == gate_profile.value(field)
        assert shown["values"][field]["source"]


def test_show_plain_prints_both_stacks_and_the_two_sources_warning(home, capsys):
    _dials(home, VNX_DEV_DIALS)
    assert gate_profile_cli.main(["show"]) == 0
    out = capsys.readouterr().out
    assert "request_stack" in out and "required_stack" in out
    assert "codex_gate,kimi_gate,ci_gate" in out
    assert "WARNING" in out and "added: ci_gate" in out


def test_show_prints_no_warning_when_the_stacks_agree(home, capsys):
    assert gate_profile_cli.main(["show"]) == 0
    assert "WARNING" not in capsys.readouterr().out


def test_value_required_stack_csv_is_one_line(home, capsys):
    _dials(home, VNX_DEV_DIALS)
    assert gate_profile_cli.main(["--value", "required_stack", "--csv"]) == 0
    assert capsys.readouterr().out == "codex_gate,kimi_gate,ci_gate\n"


def test_value_of_a_standard_profile_field_is_empty_because_standard_contributes_nothing(home, capsys):
    assert gate_profile_cli.main(["--value", "round_cap"]) == 0
    assert capsys.readouterr().out == "\n"


def test_zwaar_exits_2_and_names_the_raw_value_the_source_and_the_reason(home):
    for args in (["show"], ["--value", "required_stack", "--csv"]):
        result = _run_cli(*args, extra={"VNX_GOVERNANCE_PROFILE": "zwaar", "VNX_STATE_DIR": os.environ["VNX_STATE_DIR"]})
        assert result.returncode == 2, result.stdout
        assert "'zwaar'" in result.stderr and "source: env" in result.stderr and "not one of" in result.stderr
        assert result.stdout == ""


def test_zwaar_in_json_mode_still_exits_2_with_the_error_record(home):
    result = _run_cli("show", "--json", extra={"VNX_GOVERNANCE_PROFILE": "zwaar", "VNX_STATE_DIR": os.environ["VNX_STATE_DIR"]})
    assert result.returncode == 2
    record = json.loads(result.stdout)
    assert record["profile"] is None and record["raw"] == "zwaar" and record["required_stack"] is None


def test_a_malformed_dial_exits_2_from_the_cli(home):
    _dials(home, "checks:\n  codex_gate_required:\n    level: hoog\n")
    result = _run_cli("--value", "required_stack", "--csv", extra={
        "VNX_HOME": str(home), "VNX_STATE_DIR": os.environ["VNX_STATE_DIR"]})
    assert result.returncode == 2 and result.stdout == "" and "hoog" in result.stderr


def test_usage_needs_exactly_one_of_show_or_value(home):
    assert _run_cli().returncode == 2
    assert _run_cli("show", "--value", "round_cap").returncode == 2
    assert _run_cli("--value", "nonsense").returncode == 2


# --- wiring ---------------------------------------------------------------------------------

def test_gate_profile_is_a_starter_operator_command():
    assert "gate-profile" in vnx_mode.TIER_STARTER_OPERATOR


def test_bin_vnx_routes_gate_profile_to_the_cli():
    text = (REPO_ROOT / "bin" / "vnx").read_text(encoding="utf-8")
    assert 'gate_profile_cli.py" "$@"' in text
    assert "    gate-profile)" in text
