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
* its parity step and its record step, executed: a light run is
  not_applicable, failed downloads are exit 2, a run without a verdict gets
  an exit-2 verdict from the record step.
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
PRELUDE_USES = "./.github/actions/profile-a-prelude"
PARITY_STEP_IDS = ("checkout", "prelude", "selection", "download_reference", "download_candidate")


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


def test_the_parity_step_on_a_light_run_writes_not_applicable(parity, tmp_path):
    proc = _bash(_step(parity, "parity"), _parity_env(
        tmp_path, PARITY_PROFILE="light", OUTCOME_SELECTION="skipped",
        OUTCOME_DOWNLOAD_REFERENCE="skipped", OUTCOME_DOWNLOAD_CANDIDATE="skipped"))
    assert proc.returncode == 0, proc.stderr
    verdict = _verdict(tmp_path)
    assert verdict["verdict"] == "not_applicable" and verdict["exit"] is None
    assert verdict["run_attempt"] == "2" and verdict["profile"] == "light"
    assert "not_applicable" in (tmp_path / "summary.md").read_text(encoding="utf-8")


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
    env = {k: v for k, v in _parity_env(tmp_path).items() if not k.startswith(("OUTCOME_", "RESULT_"))}
    env.update(OUTCOME_CHECKOUT="failure", OUTCOME_PARITY="skipped")
    env.update(overrides)
    return env


def test_the_record_step_writes_exit_2_when_no_verdict_exists(parity, tmp_path):
    proc = _bash(_step(parity, "record"), _record_env(tmp_path, PARITY_PROFILE=""))
    assert proc.returncode == 0, proc.stderr
    verdict = _verdict(tmp_path)
    assert verdict["exit"] == 2 and verdict["verdict"] == "invalid_evidence"
    assert verdict["invalid_evidence"][0]["reason"] == "no_verdict"
    assert "checkout failure, parity step skipped" in verdict["invalid_evidence"][0]["detail"]
    assert verdict["profile"] == "unknown" and verdict["selection_fingerprint"] is None
    assert verdict["run_id"] == "38939122085" and verdict["ref"] == "dispatch/x"
    assert "wrote no verdict" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_the_record_step_leaves_a_written_verdict_alone(parity, tmp_path):
    target = tmp_path / "work" / "verdict" / "xdist-parity-verdict.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"exit": 0, "verdict": "parity"}\n', encoding="utf-8")
    proc = _bash(_step(parity, "record"), _record_env(tmp_path, OUTCOME_CHECKOUT="success", OUTCOME_PARITY="success"))
    assert proc.returncode == 0, proc.stderr
    assert _verdict(tmp_path) == {"exit": 0, "verdict": "parity"}
    assert not (tmp_path / "summary.md").exists()


def test_the_record_step_keeps_the_json_valid_for_any_branch_name(parity, tmp_path):
    proc = _bash(_step(parity, "record"), _record_env(tmp_path, PARITY_REF='feat/"quoted" \\ name'))
    assert proc.returncode == 0, proc.stderr
    assert _verdict(tmp_path)["ref"] == "feat/quotedname"
