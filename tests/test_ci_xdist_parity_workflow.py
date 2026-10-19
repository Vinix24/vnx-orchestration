"""Workflow rules of track ci-profile-a-xdist, D2 (.github/workflows/vnx-ci.yml): the parity job.

Pins the shape the plan requires of the job "xdist parity", so an edit that
breaks it fails a test instead of a merge:

* it needs Profile A and the shadow with if: !cancelled(), so a red serial
  Profile A or a failed shadow never skips it;
* it follows every rule of "Shadow evidence never fails a check": every step
  non-fatal, every step bounded with the sum under the job's timeout, the
  record and the upload with if: always(), no job-level continue-on-error;
* it downloads exactly the artifacts Profile A and the shadow upload, passes
  every step outcome to the parity script, and uploads xdist-parity-verdict;
* its selection step runs in the environment of the shadow's emission;
* its parity step and its record step, executed: a light run is
  not_applicable and names its skipped steps, failed downloads are exit 2, a
  parity step that failed after writing exit 0 leaves no clean verdict
  (OI-2145), a run without a verdict gets an exit-2 verdict with every field
  of a normal one (OI-2147), from the record script and, without a checkout,
  from the step's own bash, the two equal.
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
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "xdist_parity_verdicts"
PRELUDE_USES = "./.github/actions/profile-a-prelude"
PARITY_STEP_IDS = ("checkout", "prelude", "selection", "download_reference", "download_candidate")
RECORDED_STEP_IDS = PARITY_STEP_IDS + ("parity",)


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


@pytest.fixture(scope="module")
def parity(jobs) -> dict:
    return jobs["xdist-parity"]


def _step(job: dict, step_id: str) -> dict:
    [step] = [s for s in job["steps"] if s.get("id") == step_id]
    return step


def _uploads(job: dict) -> list:
    return [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")]


def test_the_parity_job_is_named_and_needs_both_jobs_without_being_skipped(parity):
    assert parity["name"] == "xdist parity"
    assert parity["needs"] == ["profile-a", "profile-a-xdist-shadow"]
    assert parity["if"] == "${{ !cancelled() }}"


def test_every_parity_step_is_non_fatal_and_bounded(parity):
    for step in parity["steps"]:
        assert step.get("continue-on-error") is True, step["name"]
        assert isinstance(step.get("timeout-minutes"), int), step["name"]
    total = sum(step["timeout-minutes"] for step in parity["steps"])
    assert total < parity["timeout-minutes"], "a step timeout must fire before the job timeout"
    assert "continue-on-error" not in parity, "job-level continue-on-error leaves a red CheckRun"


def test_the_parity_job_calls_the_same_prelude(parity):
    prelude = _step(parity, "prelude")
    assert prelude["uses"] == PRELUDE_USES
    assert "steps.checkout.outcome == 'success'" in prelude["if"]


def test_it_downloads_exactly_what_profile_a_and_the_shadow_upload(jobs, parity):
    [profile_a_upload] = _uploads(jobs["profile-a"])
    [shadow_upload] = _uploads(jobs["profile-a-xdist-shadow"])
    reference, candidate = _step(parity, "download_reference"), _step(parity, "download_candidate")
    assert reference["uses"].startswith("actions/download-artifact@")
    assert candidate["uses"].startswith("actions/download-artifact@")
    assert reference["with"]["name"] == profile_a_upload["with"]["name"] == "profile-a-evidence"
    assert candidate["with"]["name"] == shadow_upload["with"]["name"] == "profile-a-xdist-shadow-evidence"
    for step in (reference, candidate):
        assert "steps.prelude.outputs.profile != 'light'" in step["if"]


def test_it_emits_its_own_xdist_selection_for_the_fingerprint(parity):
    selection = _step(parity, "selection")
    assert selection["env"]["VNX_CI_XDIST"] == "1"
    assert "profile_a_pytest_args.sh" in selection["run"]
    assert "steps.prelude.outputs.profile != 'light'" in selection["if"]


def test_the_selection_runs_in_the_environment_of_the_shadows_emission(jobs, parity):
    """deepseek round 1 on #2087: without VNX_HOME and VNX_DATA_DIR, a serial entry's collection could fail here only.

    The args script collects the serial entries, importing the suite's
    conftest; a selection step that fails on every run is a step failure on
    every verdict, and the cutover rule could never hold.
    """
    selection, shadow_plan = _step(parity, "selection"), _step(jobs["profile-a-xdist-shadow"], "plan")
    for name in ("VNX_HOME", "VNX_DATA_DIR", "VNX_CI_XDIST"):
        assert selection["env"].get(name) == shadow_plan["env"][name], name


def test_the_parity_step_passes_every_input(parity):
    step = _step(parity, "parity")
    run = step["run"]
    assert "check_xdist_parity.py\" compare" in run
    for flag in ("--reference", "--candidate", "--constraints", "--args-dir", "--workspace-root \"$GITHUB_WORKSPACE\"",
                 "--output", "--summary", "--profile", "--head-sha", "--run-id", "--run-attempt", "--event", "--ref"):
        assert flag in run, flag
    for step_id in PARITY_STEP_IDS:
        assert f"--step \"{step_id}=$OUTCOME_{step_id.upper()}\"" in run, step_id
        assert step["env"][f"OUTCOME_{step_id.upper()}"] == f"${{{{ steps.{step_id}.outcome }}}}"
    assert step["env"]["RESULT_PROFILE_A"] == "${{ needs.profile-a.result }}"
    assert step["env"]["RESULT_SHADOW"] == "${{ needs.profile-a-xdist-shadow.result }}"


def test_the_record_step_passes_every_step_outcome_and_job_result(parity):
    step = _step(parity, "record")
    run = step["run"]
    assert "record_xdist_parity_verdict.py" in run
    for step_id in RECORDED_STEP_IDS:
        assert f"--step \"{step_id}=$OUTCOME_{step_id.upper()}\"" in run, step_id
        assert step["env"][f"OUTCOME_{step_id.upper()}"] == f"${{{{ steps.{step_id}.outcome }}}}"
    assert step["env"]["RESULT_PROFILE_A"] == "${{ needs.profile-a.result }}"
    assert step["env"]["RESULT_SHADOW"] == "${{ needs.profile-a-xdist-shadow.result }}"


def test_the_parity_job_records_and_uploads_always(parity):
    assert _step(parity, "record")["if"] == "${{ always() }}"
    [upload] = _uploads(parity)
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["name"] == "xdist-parity-verdict"
    assert upload["with"]["overwrite"] is True
    assert upload["with"]["path"].endswith("/xdist-parity/verdict/")


# --- the parity step and the record step, executed ---------------------------------------------------------


def _bash(step: dict, env: dict) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("OUTCOME_", "PARITY_", "RESULT_", "PYTEST_"))}
    return subprocess.run(["bash", "-c", step["run"]], env={**base, **env}, capture_output=True, text=True)


def _parity_env(tmp_path: Path, **overrides: str) -> dict:
    env = {
        "GITHUB_WORKSPACE": str(REPO_ROOT),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "PARITY_WORK": str(tmp_path / "work"),
        "PARITY_PROFILE": "heavy",
        "PARITY_HEAD_SHA": "a8b5100802ccb0a3d371806c271eaf2b47db3499",
        "PARITY_TESTED_SHA": "b39838cf709922fde46c1109c9909134bde9e1a4",
        "PARITY_RUN_ID": "38939122085",
        "PARITY_RUN_ATTEMPT": "2",
        "PARITY_EVENT": "pull_request",
        "PARITY_REF": "dispatch/x",
        "RESULT_PROFILE_A": "failure",
        "RESULT_SHADOW": "success",
        **{f"OUTCOME_{s.upper()}": "success" for s in PARITY_STEP_IDS},
    }
    env.update(overrides)
    return env


def _verdict(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "work" / "verdict" / "xdist-parity-verdict.json").read_text(encoding="utf-8"))




def _summary(tmp_path: Path) -> str:
    path = tmp_path / "summary.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_the_parity_step_on_a_light_run_writes_not_applicable_and_names_its_skipped_steps(parity, tmp_path):
    """deepseek round 5 on #2087 (OI-2147): the light early return set step_failures=[] beside three skipped steps."""
    proc = _bash(_step(parity, "parity"), _parity_env(
        tmp_path, PARITY_PROFILE="light", OUTCOME_SELECTION="skipped",
        OUTCOME_DOWNLOAD_REFERENCE="skipped", OUTCOME_DOWNLOAD_CANDIDATE="skipped"))
    assert proc.returncode == 0, proc.stderr
    verdict = _verdict(tmp_path)
    assert verdict["verdict"] == "not_applicable" and verdict["exit"] is None
    assert verdict["run_attempt"] == "2" and verdict["profile"] == "light"
    assert "not_applicable" in _summary(tmp_path)
    for name in ("selection", "download_reference", "download_candidate"):
        assert f"parity.{name}=skipped" in verdict["step_failures"], verdict["step_failures"]


def test_the_parity_step_records_failed_downloads_as_exit_2(parity, tmp_path):
    proc = _bash(_step(parity, "parity"), _parity_env(
        tmp_path, OUTCOME_DOWNLOAD_REFERENCE="failure", OUTCOME_DOWNLOAD_CANDIDATE="failure"))
    assert proc.returncode == 2, proc.stderr
    verdict = _verdict(tmp_path)
    assert verdict["exit"] == 2
    assert {"reason": "download_failed", "detail": "download_reference=failure"} in verdict["invalid_evidence"]
    assert verdict["head_sha"] == "a8b5100802ccb0a3d371806c271eaf2b47db3499"
    assert verdict["reference"]["job_result"] == "failure" and verdict["shadow"]["job_result"] == "success"
    assert verdict["steps"]["download_candidate"] == "failure"


def _record_env(tmp_path: Path, **overrides: str) -> dict:
    """The record step's inputs on a heavy run whose every step and needed job succeeded; *overrides* change them."""
    env = {**_parity_env(tmp_path), "RESULT_PROFILE_A": "success", "OUTCOME_PARITY": "success"}
    env.update(overrides)
    return env


# A run whose checkout failed: every later step that needs it was skipped.
NO_CHECKOUT = {"OUTCOME_CHECKOUT": "failure", "OUTCOME_PRELUDE": "skipped", "OUTCOME_SELECTION": "skipped",
               "OUTCOME_PARITY": "skipped"}


def _written(tmp_path: Path, name: str) -> dict:
    """Put fixture *name* where the parity step writes its verdict, as the parity step wrote it."""
    document = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    target = tmp_path / "work" / "verdict" / "xdist-parity-verdict.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(document), encoding="utf-8")
    return document


def _without(document: dict, *names: str) -> dict:
    return {k: v for k, v in document.items() if k not in names}


def test_a_parity_step_that_failed_after_writing_exit_0_leaves_no_clean_verdict(parity, tmp_path):
    """codex round 4 on #2087 (OI-2145): the record step kept a written exit-0 verdict when the parity step failed.

    The parity step writes its verdict, then appends the summary; when that
    append fails it exits 2 with exit 0 and no step failure in the verdict
    (tests/test_ci_check_xdist_parity.py pins that order). The verdict here is
    the real artifact of run 39122740681, as the parity step wrote it.
    """
    written = _written(tmp_path, "written-heavy-parity")
    assert written["exit"] == 0 and written["step_failures"] == []
    proc = _bash(_step(parity, "record"), _record_env(tmp_path, OUTCOME_PARITY="failure"))
    assert proc.returncode == 0, proc.stderr
    recorded = _verdict(tmp_path)
    assert "parity.parity=failure" in recorded["step_failures"], recorded["step_failures"]
    assert recorded["steps"].get("parity") == "failure"
    assert _without(recorded, "steps", "step_failures") == _without(written, "steps", "step_failures")
    assert "ended `failure` after it wrote exit 0" in _summary(tmp_path)


def test_the_record_step_adds_a_successful_parity_step_and_changes_nothing_else(parity, tmp_path):
    written = _written(tmp_path, "written-heavy-parity")
    proc = _bash(_step(parity, "record"), _record_env(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert _verdict(tmp_path) == {**written, "steps": {**written["steps"], "parity": "success"}}
    assert _summary(tmp_path) == ""


def test_a_verdict_with_differences_carries_the_failed_parity_step_next_to_its_exit(parity, tmp_path):
    written = _written(tmp_path, "written-heavy-differences")
    proc = _bash(_step(parity, "record"), _record_env(tmp_path, OUTCOME_PARITY="failure"))
    assert proc.returncode == 0, proc.stderr
    recorded = _verdict(tmp_path)
    assert recorded["exit"] == 1 and recorded["differences"] == written["differences"]
    assert recorded["step_failures"] == written["step_failures"] + ["parity.parity=failure"]
    assert _summary(tmp_path) == "", "exit 1 already says the comparison did not hold"


def test_the_emergency_verdict_has_every_field_of_a_normal_verdict(parity, tmp_path):
    """deepseek round 5 on #2087 (OI-2147): the exit-2 verdict lacked steps, shadow, reference, counts, tool_versions.

    The normal verdict is the real artifact of run 39122740681.
    """
    normal = json.loads((FIXTURES / "written-heavy-parity.json").read_text(encoding="utf-8"))
    proc = _bash(_step(parity, "record"), _record_env(tmp_path, OUTCOME_PARITY="failure"))
    assert proc.returncode == 0, proc.stderr
    emergency = _verdict(tmp_path)
    assert sorted(emergency) == sorted(normal), sorted(set(normal) ^ set(emergency))
    assert sorted(emergency["steps"]) == sorted(RECORDED_STEP_IDS)
    assert sorted(emergency["reference"]) == sorted(normal["reference"])
    assert emergency["shadow"] == {"record": "not_read", "job_result": "success"}
    assert emergency["exit"] == 2 and emergency["verdict"] == "invalid_evidence"
    assert emergency["selection_fingerprint"] is None and emergency["counts"] is None
    assert emergency["step_failures"] == ["parity.verdict=missing", "parity.parity=failure", "reference.result=not_read",
                                          "shadow.record=not_read"]


def test_the_record_step_writes_exit_2_when_no_verdict_exists(parity, tmp_path):
    proc = _bash(_step(parity, "record"), _record_env(tmp_path, PARITY_PROFILE="", **NO_CHECKOUT))
    assert proc.returncode == 0, proc.stderr
    verdict = _verdict(tmp_path)
    assert verdict["exit"] == 2 and verdict["verdict"] == "invalid_evidence"
    assert verdict["invalid_evidence"][0]["reason"] == "no_verdict"
    assert "checkout failure, parity step skipped" in verdict["invalid_evidence"][0]["detail"]
    assert verdict["profile"] == "unknown" and verdict["selection_fingerprint"] is None
    assert verdict["run_id"] == "38939122085" and verdict["ref"] == "dispatch/x"
    assert "wrote no verdict" in _summary(tmp_path)


def test_a_written_verdict_that_does_not_read_is_replaced_by_the_emergency_verdict(parity, tmp_path):
    target = tmp_path / "work" / "verdict" / "xdist-parity-verdict.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"exit": 0, "verdict": "parity"', encoding="utf-8")
    proc = _bash(_step(parity, "record"), _record_env(tmp_path, OUTCOME_PARITY="failure"))
    assert proc.returncode == 0, proc.stderr
    verdict = _verdict(tmp_path)
    assert verdict["exit"] == 2 and verdict["invalid_evidence"][0]["reason"] == "no_verdict"


@pytest.mark.parametrize("change", [
    NO_CHECKOUT,
    {"OUTCOME_PARITY": "failure"},
    {"OUTCOME_PARITY": "cancelled", "RESULT_PROFILE_A": "failure", "RESULT_SHADOW": "", "OUTCOME_SELECTION": ""},
    {"PARITY_PROFILE": "", "OUTCOME_PRELUDE": "failure", "OUTCOME_PARITY": "failure"},
], ids=["no-checkout", "parity-crashed", "red-profile-a-and-empty-results", "prelude-failed"])
def test_without_a_checkout_the_steps_bash_writes_the_scripts_emergency_verdict(parity, tmp_path, change):
    """The fallback for a run without the record script (no checkout) must not drift from the script."""
    bare = tmp_path / "bare-workspace"
    bare.mkdir()
    runs = {"script": _record_env(tmp_path / "script", **change),
            "bash": _record_env(tmp_path / "bash", GITHUB_WORKSPACE=str(bare), **change)}
    for name, env in runs.items():
        proc = _bash(_step(parity, "record"), env)
        assert proc.returncode == 0, (name, proc.stderr)
    assert _verdict(tmp_path / "bash") == _verdict(tmp_path / "script")
    assert _summary(tmp_path / "bash") == _summary(tmp_path / "script") != ""


@pytest.mark.parametrize(("workspace", "ref"), [("script", 'feat/"quoted" \\ name'), ("bash", "feat/quotedname")])
def test_the_record_step_keeps_the_json_valid_for_any_branch_name(parity, tmp_path, workspace, ref):
    """The script writes the ref as given, JSON-escaped; the bash fallback keeps only token characters."""
    bare = tmp_path / "bare-workspace"
    bare.mkdir()
    env = _record_env(tmp_path, PARITY_REF='feat/"quoted" \\ name', OUTCOME_PARITY="failure",
                      GITHUB_WORKSPACE=str(REPO_ROOT if workspace == "script" else bare))
    proc = _bash(_step(parity, "record"), env)
    assert proc.returncode == 0, proc.stderr
    assert _verdict(tmp_path)["ref"] == ref
