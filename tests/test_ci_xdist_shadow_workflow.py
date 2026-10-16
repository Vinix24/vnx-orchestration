"""Workflow rules of track ci-profile-a-xdist, D1 (.github/workflows/vnx-ci.yml).

Pins the shape the plan requires, so an edit that breaks it fails a test
instead of a merge:

* Profile A runs its selection from scripts/ci/profile_a_pytest_args.sh (the
  -k quarantine and the exclusion loop no longer live in the workflow), calls
  the prelude fatally, uploads its evidence with if: always(), and has a 55
  minute timeout (risk R9);
* the shadow job "Profile A (xdist shadow)" follows every rule of "Shadow
  evidence never fails a check": every step non-fatal, every step with its
  own timeout and their sum under the job's, the record and the upload with
  if: always(), the suite only on a heavy profile;
* the record step's verdict, executed for each outcome the runner can
  report: light is not_applicable, a failed checkout or prelude is
  prelude_failed, a refused selection is selection_failed, a red or timed-out
  phase is shadow_failed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "vnx-ci.yml"
PRELUDE = REPO_ROOT / ".github" / "actions" / "profile-a-prelude" / "action.yml"
PRELUDE_USES = "./.github/actions/profile-a-prelude"


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _steps_by_name(job: dict) -> dict:
    return {step["name"]: step for step in job["steps"]}


def _step_by_id(job: dict, step_id: str) -> dict:
    [step] = [s for s in job["steps"] if s.get("id") == step_id]
    return step


# --- Profile A ----------------------------------------------------------------


def test_profile_a_keeps_its_required_name_and_gets_55_minutes(jobs):
    assert jobs["profile-a"]["name"] == "Profile A (doctor + core tests)"
    assert jobs["profile-a"]["timeout-minutes"] == 55


def test_profile_a_calls_the_prelude_fatally(jobs):
    prelude = _step_by_id(jobs["profile-a"], "prelude")
    assert prelude["uses"] == PRELUDE_USES
    assert "continue-on-error" not in prelude


def test_profile_a_runs_the_one_selection_script(jobs):
    suite = _steps_by_name(jobs["profile-a"])["Run Profile A (scope-appropriate suite)"]
    run = suite["run"]
    assert 'profile_a_pytest_args.sh" "$PROFILE_A_EVIDENCE/args"' in run
    assert 'profile_a_run_phase.sh" full "$PROFILE_A_EVIDENCE"' in run
    assert "VNX_CI_XDIST" not in str(suite.get("env", {}))
    assert suite["env"]["CI_PROFILE"] == "${{ steps.prelude.outputs.profile }}"


def test_the_selection_no_longer_lives_in_the_workflow(jobs):
    code = [
        line
        for job in jobs.values()
        for step in job.get("steps", [])
        for line in str(step.get("run", "")).splitlines()
        if not line.lstrip().startswith("#")
    ]
    for needle in ("test_exclusions.txt", "TestFlagGateClaude", "IGNORE+=", "--timeout=180"):
        assert not [line for line in code if needle in line], needle


def test_profile_a_uploads_its_evidence_also_when_red(jobs):
    names = [s["name"] for s in jobs["profile-a"]["steps"]]
    upload_name = "Upload Profile A evidence (JUnit XML + collection manifest)"
    assert names.index(upload_name) == names.index("Run Profile A (scope-appropriate suite)") + 1
    upload = _steps_by_name(jobs["profile-a"])[upload_name]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert "always()" in upload["if"]
    assert upload["continue-on-error"] is True
    assert upload["with"]["name"] == "profile-a-evidence"


# --- the shadow job -------------------------------------------------------------


def test_the_shadow_job_is_named_and_runs_next_to_profile_a(jobs):
    shadow = jobs["profile-a-xdist-shadow"]
    assert shadow["name"] == "Profile A (xdist shadow)"
    assert "needs" not in shadow


def test_every_shadow_step_is_non_fatal_and_bounded(jobs):
    shadow = jobs["profile-a-xdist-shadow"]
    for step in shadow["steps"]:
        assert step.get("continue-on-error") is True, step["name"]
        assert isinstance(step.get("timeout-minutes"), int), step["name"]
    total = sum(step["timeout-minutes"] for step in shadow["steps"])
    assert total < shadow["timeout-minutes"], "a step timeout must fire before the job timeout"
    assert "continue-on-error" not in shadow, "job-level continue-on-error leaves a red CheckRun"


def test_the_shadow_calls_the_same_prelude(jobs):
    prelude = _step_by_id(jobs["profile-a-xdist-shadow"], "prelude")
    assert prelude["uses"] == PRELUDE_USES
    assert "steps.checkout.outcome == 'success'" in prelude["if"]


def test_the_shadow_suite_runs_on_heavy_only_in_the_two_phase_shape(jobs):
    shadow = jobs["profile-a-xdist-shadow"]
    plan = _step_by_id(shadow, "plan")
    assert "steps.prelude.outputs.profile == 'heavy'" in plan["if"]
    assert plan["env"]["VNX_CI_XDIST"] == "1"
    assert "profile_a_pytest_args.sh" in plan["run"] and "inactive" in plan["run"]
    serial = _step_by_id(shadow, "serial")
    parallel = _step_by_id(shadow, "parallel")
    assert "steps.plan.outputs.serial_active == 'true'" in serial["if"]
    assert "steps.plan.outcome == 'success'" in parallel["if"]
    assert "serial_active" not in parallel["if"], "phase 2 runs whatever phase 1 did"
    assert 'profile_a_run_phase.sh" serial' in serial["run"]
    assert 'profile_a_run_phase.sh" parallel' in parallel["run"]


def test_the_shadow_records_and_uploads_always(jobs):
    shadow = jobs["profile-a-xdist-shadow"]
    record = _step_by_id(shadow, "record")
    assert record["if"] == "${{ always() }}"
    upload = [s for s in shadow["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(upload) == 1 and upload[0]["if"] == "${{ always() }}"
    assert upload[0]["with"]["name"] == "profile-a-xdist-shadow-evidence"
    for step_id in ("checkout", "prelude", "plan", "serial", "parallel"):
        assert f"steps.{step_id}.outcome" in str(record["env"])


# --- the prelude ----------------------------------------------------------------


def test_the_prelude_installs_from_the_constraints_file():
    action = yaml.safe_load(PRELUDE.read_text(encoding="utf-8"))
    steps = {s["name"]: s for s in action["runs"]["steps"]}
    install = steps["Install Python deps"]["run"]
    assert '-c "$GITHUB_WORKSPACE/scripts/ci/profile_a_constraints.txt"' in install
    for tool in ("pytest", "pytest-xdist", "pytest-timeout"):
        assert f" {tool}" in install
    assert {"Setup Python", "Prepare expected repo layout", "Install ripgrep", "Doctor"} <= set(steps)
    assert "classify_ci_profile.py" in steps["Classify CI profile (docs-only vs full sweep)"]["run"]
    assert set(action["outputs"]) == {"profile", "vnx-home", "vnx-data-dir"}


# --- the record step's verdict ---------------------------------------------------

SUCCESS_RUN = {
    "OUTCOME_CHECKOUT": "success",
    "OUTCOME_PRELUDE": "success",
    "OUTCOME_PLAN": "success",
    "OUTCOME_SERIAL": "skipped",
    "OUTCOME_PARALLEL": "success",
    "SHADOW_PROFILE": "heavy",
    "SHADOW_SERIAL_ACTIVE": "false",
}


def _record(jobs, tmp_path: Path, overrides: dict, files: dict | None = None) -> tuple:
    record = _step_by_id(jobs["profile-a-xdist-shadow"], "record")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for name, body in (files or {}).items():
        (evidence / name).write_text(body, encoding="utf-8")
    summary = tmp_path / "summary.md"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OUTCOME_", "SHADOW_"))}
    env.update({
        "SHADOW_EVIDENCE": str(evidence),
        "SHADOW_HEAD_SHA": "a8b5100802ccb0a3d371806c271eaf2b47db3499",
        "SHADOW_TESTED_SHA": "b39838cf709922fde46c1109c9909134bde9e1a4",
        "SHADOW_RUN_ID": "38939122085",
        "SHADOW_RUN_ATTEMPT": "1",
        "SHADOW_EVENT": "pull_request",
        "GITHUB_STEP_SUMMARY": str(summary),
        **SUCCESS_RUN,
        **overrides,
    })
    proc = subprocess.run(["bash", "-c", record["run"]], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads((evidence / "xdist-shadow.json").read_text(encoding="utf-8")), summary.read_text()


@pytest.mark.parametrize(
    ("overrides", "verdict"),
    [
        ({}, "shadow_passed"),
        ({"SHADOW_PROFILE": "light", "OUTCOME_PLAN": "skipped", "OUTCOME_PARALLEL": "skipped"}, "not_applicable"),
        ({"OUTCOME_CHECKOUT": "failure", "OUTCOME_PRELUDE": "skipped", "SHADOW_PROFILE": ""}, "prelude_failed"),
        ({"OUTCOME_PRELUDE": "failure", "SHADOW_PROFILE": ""}, "prelude_failed"),
        ({"SHADOW_PROFILE": ""}, "prelude_failed"),
        ({"OUTCOME_PLAN": "failure", "OUTCOME_PARALLEL": "skipped"}, "selection_failed"),
        ({"OUTCOME_PARALLEL": "failure"}, "shadow_failed"),
        ({"OUTCOME_PARALLEL": "cancelled"}, "shadow_failed"),
        ({"SHADOW_SERIAL_ACTIVE": "true", "OUTCOME_SERIAL": "failure"}, "shadow_failed"),
        ({"SHADOW_SERIAL_ACTIVE": "true", "OUTCOME_SERIAL": "success"}, "shadow_passed"),
    ],
)
def test_the_record_step_maps_outcomes_to_a_verdict(jobs, tmp_path, overrides, verdict):
    record, summary = _record(jobs, tmp_path, overrides)
    assert record["verdict"] == verdict
    assert record["schema"] == "vnx-xdist-shadow/1"
    assert record["steps"]["prelude"] == {**SUCCESS_RUN, **overrides}["OUTCOME_PRELUDE"]
    assert f"Verdict: **{verdict}**" in summary


def test_the_record_step_reads_exit_codes_durations_and_memory(jobs, tmp_path):
    files = {
        "exit-parallel": "1\n",
        "duration-parallel-s": "612\n",
        "mem-total-kb": "16384000\n",
        "mem-baseline-used-kb-parallel": "1200000\n",
        "mem-peak-used-kb-parallel": "5400000\n",
    }
    record, _ = _record(jobs, tmp_path, {"OUTCOME_PARALLEL": "failure"}, files)
    assert record["verdict"] == "shadow_failed"
    assert record["exit_codes"] == {"serial": None, "parallel": 1}
    assert record["durations_s"] == {"serial": None, "parallel": 612}
    assert record["memory_kb"]["peak_used_parallel"] == 5400000
    assert record["head_sha"] == "a8b5100802ccb0a3d371806c271eaf2b47db3499"


def test_a_killed_phase_records_no_exit_code(jobs, tmp_path):
    record, _ = _record(jobs, tmp_path, {"OUTCOME_PARALLEL": "failure"})
    assert record["exit_codes"]["parallel"] is None
    assert record["verdict"] == "shadow_failed"
