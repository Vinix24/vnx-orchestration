"""Tests for scripts/ci/record_xdist_parity_verdict.py (track ci-profile-a-xdist, D2b): the record step's decision.

The record step runs after the comparison in the job "xdist parity". It adds
the comparison step's own outcome to the verdict that step wrote (OI-2145),
or writes an exit-2 verdict with every field of a normal one when it wrote
none (OI-2147). The workflow-level counterpart, which runs the step itself and
holds its bash fallback equal to this script, is
tests/test_ci_xdist_parity_workflow.py.

The second half pins the verdict fixtures in tests/fixtures/xdist_parity_verdicts/
to what the pipeline writes today, so the history tests read the real schema:
each recorded fixture is regenerated from its source here and compared whole.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "record_xdist_parity_verdict.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "xdist_parity_verdicts"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "vnx-ci.yml"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rec = _load("record_xdist_parity_verdict", SCRIPT)
cxp = sys.modules["check_xdist_parity"]  # imported by the record script, which shares its step rule

META = {"head_sha": "a8b5100802ccb0a3d371806c271eaf2b47db3499", "tested_sha": "b39838cf709922fde46c1109c9909134bde9e1a4",
        "run_id": "38939122085", "run_attempt": "2", "event": "pull_request", "ref": "dispatch/x"}
ALL_SUCCESS = {name: "success" for name in cxp.RECORDED_STEPS}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _cli_args(verdict: Path, summary: Path | None = None, *, profile="heavy", steps=None, upstream=None) -> list:
    args = ["--verdict", str(verdict), "--profile", profile]
    if summary is not None:
        args += ["--summary", str(summary)]
    for name, value in META.items():
        args += [f"--{name.replace('_', '-')}", value]
    for name, outcome in (steps or ALL_SUCCESS).items():
        args += ["--step", f"{name}={outcome}"]
    for name, result in (upstream or {"profile_a": "success", "shadow": "success"}).items():
        args += ["--upstream", f"{name}={result}"]
    return args


# --- a written verdict gains the comparison step's outcome --------------------------------------------------


def test_a_successful_comparison_step_is_added_and_nothing_else_changes():
    written = fixture("written-heavy-parity")
    recorded = rec.record_compare_outcome(written, "success")
    assert recorded == {**written, "steps": {**written["steps"], "parity": "success"}}
    assert written["steps"].get("parity") is None, "the written verdict itself is not changed"


@pytest.mark.parametrize(("outcome", "failure"), [("failure", "parity.parity=failure"),
                                                  ("cancelled", "parity.parity=cancelled"),
                                                  ("", "parity.parity=unknown")],
                         ids=["failure", "cancelled", "no-outcome"])
def test_a_comparison_step_that_did_not_succeed_is_a_step_failure(outcome, failure):
    recorded = rec.record_compare_outcome(fixture("written-heavy-parity"), outcome)
    assert recorded["step_failures"] == [failure]
    assert recorded["steps"]["parity"] == outcome and recorded["exit"] == 0


def test_a_failure_already_listed_is_not_listed_twice():
    written = {**fixture("written-heavy-parity"), "step_failures": ["parity.parity=failure"]}
    assert rec.record_compare_outcome(written, "failure")["step_failures"] == ["parity.parity=failure"]


@pytest.mark.parametrize("content", ['{"exit": 0', "[1, 2]", '{"exit": 0, "step_failures": []}',
                                     '{"exit": 0, "steps": {}, "step_failures": "none"}'],
                         ids=["unparsable", "not-an-object", "no-steps", "step-failures-text"])
def test_a_written_file_that_is_no_verdict_reads_as_none(tmp_path, content):
    path = tmp_path / "v.json"
    path.write_text(content, encoding="utf-8")
    assert rec.read_parity_verdict(path) is None


# --- no verdict: the emergency verdict ----------------------------------------------------------------------


def test_the_emergency_verdict_has_the_field_set_of_a_comparison_verdict():
    emergency = rec.emergency_verdict(META, "heavy", {**ALL_SUCCESS, "parity": "failure"},
                                      {"profile_a": "success", "shadow": "success"})
    assert sorted(emergency) == sorted(fixture("written-heavy-parity"))
    assert {k: emergency[k] for k in META} == META


def test_the_emergency_verdict_names_every_step_and_job_that_did_not_succeed():
    steps = {**ALL_SUCCESS, "checkout": "failure", "prelude": "skipped", "parity": "skipped"}
    emergency = rec.emergency_verdict(META, "", steps, {"profile_a": "failure", "shadow": ""})
    assert emergency["profile"] == "unknown"
    assert emergency["shadow"] == {"record": "not_read", "job_result": None}
    assert emergency["reference"] == {"job_result": "failure", "result": "not_read", "exit": None, "duration_s": None}
    assert emergency["step_failures"] == [
        "parity.verdict=missing", "parity.checkout=failure", "parity.prelude=skipped", "parity.parity=skipped",
        "reference.job=failure", "reference.result=not_read", "shadow.record=not_read", "shadow.job=unknown"]
    assert emergency["invalid_evidence"] == [
        {"reason": "no_verdict", "detail": "the parity script wrote no verdict (checkout failure, parity step skipped)"}]


# --- the CLI ---------------------------------------------------------------------------------------------------


def test_the_cli_records_a_failed_comparison_step_into_an_exit_0_verdict_and_says_so(tmp_path):
    verdict, summary = tmp_path / "verdict.json", tmp_path / "summary.md"
    verdict.write_text(json.dumps(fixture("written-heavy-parity")), encoding="utf-8")
    code = rec.main(_cli_args(verdict, summary, steps={**ALL_SUCCESS, "parity": "failure"}))
    assert code == 0
    assert json.loads(verdict.read_text(encoding="utf-8")) == fixture("parity-step-failed-after-exit-0")
    assert "recorded as `parity.parity=failure`, so this run does not count as clean" in summary.read_text(encoding="utf-8")


def test_the_cli_writes_the_emergency_verdict_and_its_summary_when_there_is_none(tmp_path):
    verdict, summary = tmp_path / "verdict" / "verdict.json", tmp_path / "summary.md"
    assert rec.main(_cli_args(verdict, summary, steps={**ALL_SUCCESS, "parity": "failure"})) == 0
    assert json.loads(verdict.read_text(encoding="utf-8"))["exit"] == 2
    assert summary.read_text(encoding="utf-8").endswith("the parity script wrote no verdict.\n")


def test_the_cli_exits_2_with_a_reason_when_the_verdict_cannot_be_written(tmp_path, capsys):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    assert rec.main(_cli_args(blocker / "verdict.json")) == 2
    assert capsys.readouterr().err.startswith("record_xdist_parity_verdict: cannot write the verdict")


def test_a_summary_that_cannot_be_appended_exits_2_after_the_verdict_is_written(tmp_path, capsys):
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps(fixture("written-heavy-parity")), encoding="utf-8")
    code = rec.main(_cli_args(verdict, tmp_path / "no-such-dir" / "summary.md", steps={**ALL_SUCCESS, "parity": "failure"}))
    assert code == 2
    assert capsys.readouterr().err.startswith("record_xdist_parity_verdict: cannot append the summary")
    assert "parity.parity=failure" in json.loads(verdict.read_text(encoding="utf-8"))["step_failures"]


@pytest.mark.parametrize("value", ["no-equals-sign", "=success"], ids=["no-equals", "no-name"])
def test_a_malformed_step_is_a_usage_error_exit_2(tmp_path, capsys, value):
    with pytest.raises(SystemExit) as exc:
        rec.main(["--verdict", str(tmp_path / "v.json"), "--step", value])
    assert exc.value.code == 2
    assert f"expects NAME=VALUE, got {value!r}" in capsys.readouterr().err


# --- the fixtures are what the pipeline writes today ----------------------------------------------------------

# recorded fixture -> (the written verdict it comes from, the comparison step's outcome)
RECORDED_FROM = {
    "heavy-parity": ("written-heavy-parity", "success"),
    "heavy-differences": ("written-heavy-differences", "failure"),
    "parity-step-failed-after-exit-0": ("written-heavy-parity", "failure"),
}


@pytest.mark.parametrize("name", sorted(RECORDED_FROM))
def test_a_recorded_fixture_is_its_written_verdict_through_the_record_step(name):
    source, outcome = RECORDED_FROM[name]
    assert fixture(name) == rec.record_compare_outcome(fixture(source), outcome)


@pytest.mark.parametrize("name", ["written-heavy-parity", "written-heavy-differences"])
def test_a_written_fixture_is_a_real_ci_artifact_that_the_comparison_still_writes(name):
    """Run 39122740681 (exit 0) and run 39121916675 (exit 1 on main): their field set is today's."""
    written = fixture(name)
    assert written["schema"] == cxp.VERDICT_SCHEMA
    assert sorted(written["steps"]) == sorted(cxp.PARITY_STEPS)
    assert written["step_failures"] == cxp.step_failures(written["steps"], written["shadow"], written["reference"])


def test_every_fixture_carries_one_field_set():
    names = sorted(p.stem for p in FIXTURES.glob("*.json"))
    assert names == ["emergency", "heavy-differences", "heavy-parity", "light", "parity-step-failed-after-exit-0",
                     "written-heavy-differences", "written-heavy-parity"]
    field_sets = {name: tuple(sorted(fixture(name))) for name in names}
    assert len(set(field_sets.values())) == 1, field_sets


def _workflow_step(step_id: str) -> str:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    [step] = [s for s in jobs["xdist-parity"]["steps"] if s.get("id") == step_id]
    return step["run"]


def _run_step(step_id: str, env: dict) -> None:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("OUTCOME_", "PARITY_", "RESULT_", "PYTEST_"))}
    proc = subprocess.run(["bash", "-c", _workflow_step(step_id)], env={**base, **env}, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def _step_env(tmp_path: Path, profile: str, outcomes: dict) -> dict:
    """The steps' inputs; the summary goes to *tmp_path*, never to the summary of the CI job running this test."""
    return {"GITHUB_WORKSPACE": str(REPO_ROOT), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            "PARITY_WORK": str(tmp_path / "work"), "PARITY_PROFILE": profile,
            **{f"PARITY_{k.upper()}": v for k, v in META.items()},
            **{f"OUTCOME_{k.upper()}": v for k, v in outcomes.items()},
            "RESULT_PROFILE_A": "success", "RESULT_SHADOW": "success"}


def _uploaded(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "work" / "verdict" / cxp.VERDICT_FILE).read_text(encoding="utf-8"))


def test_the_light_fixture_is_what_the_parity_and_record_steps_write_on_a_light_run(tmp_path):
    outcomes = {**ALL_SUCCESS, "selection": "skipped", "download_reference": "skipped", "download_candidate": "skipped"}
    env = _step_env(tmp_path, "light", outcomes)
    _run_step("parity", env)
    _run_step("record", env)
    assert _uploaded(tmp_path) == fixture("light")


def test_the_emergency_fixture_is_what_the_record_step_writes_after_a_crashed_comparison(tmp_path):
    _run_step("record", _step_env(tmp_path, "heavy", {**ALL_SUCCESS, "parity": "failure"}))
    assert _uploaded(tmp_path) == fixture("emergency")
