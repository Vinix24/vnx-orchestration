"""Workflow rules of track deterministische-testverificatie PR-3 (.github/workflows/vnx-ci.yml).

Pins the shape of the ``red-on-base`` signal job: pull-request only, bounded, full
history, the PR body reaching the shell only through ``env:``, the verdict uploaded
with ``if: always()``. The ``edited`` trigger exists for the PR body alone, so every
job that existed before must carry an explicit exclusion and keep its own trigger.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "vnx-ci.yml"
JOB = "red-on-base"
EDITED_EXCLUSION = "github.event.action != 'edited'"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict:
    return workflow["jobs"]


def _by_name(job: dict) -> dict:
    return {step["name"]: step for step in job["steps"]}


def _triggers(workflow: dict) -> dict:
    """The ``on:`` block; PyYAML reads the bare key ``on`` as boolean ``True``."""
    return workflow["on"] if "on" in workflow else workflow[True]


def test_the_job_is_pr_only_bounded_and_checks_out_full_history(jobs):
    assert jobs[JOB]["if"] == "github.event_name == 'pull_request'"
    assert jobs[JOB]["timeout-minutes"] == 30
    checkout = _by_name(jobs[JOB])["Checkout"]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0


def test_the_pr_body_reaches_the_shell_only_through_env(jobs):
    consumers = [step for step in jobs[JOB]["steps"] if "pull_request.body" in json.dumps(step)]
    assert consumers, "no step reads github.event.pull_request.body"
    for step in consumers:
        assert "pull_request.body" not in str(step.get("run", ""))
        assert step["env"]["PR_BODY"] == "${{ github.event.pull_request.body }}"


def test_the_job_runs_the_claims_parser_and_the_verdict_tool(jobs):
    code = "\n".join(str(step.get("run", "")) for step in jobs[JOB]["steps"])
    assert "scripts/ci/red_run_claims.py" in code
    assert "classify_ci_profile.py --tests-only" in code
    assert "git merge-base" in code
    assert "red_on_base.py" in code and "--claims" in code and "--out red_on_base.json" in code


def test_the_verdict_is_uploaded_also_when_the_job_is_red(jobs):
    uploads = [step for step in jobs[JOB]["steps"]
               if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    assert uploads[0]["if"] == "always()"
    assert uploads[0]["with"]["path"] == "red_on_base.json"


def test_edited_reaches_only_the_red_on_base_trigger(workflow):
    trigger = _triggers(workflow)
    assert trigger["push"] == {"branches": ["main", "baseline/**"]}
    assert trigger["pull_request"]["types"] == ["opened", "synchronize", "reopened", "edited"]


def test_every_existing_job_excludes_the_edited_action(jobs):
    for name, job in jobs.items():
        if name == JOB:
            continue
        assert EDITED_EXCLUSION in job["if"], name


def test_the_xdist_parity_job_keeps_its_cancelled_guard(jobs):
    parity = jobs["xdist-parity"]["if"]
    assert "!cancelled()" in parity and EDITED_EXCLUSION in parity
