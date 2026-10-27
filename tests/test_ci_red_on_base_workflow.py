"""Workflow rules of track deterministische-testverificatie PR-3 (.github/workflows/vnx-ci.yml).

Pins the shape of the ``red-on-base`` signal job: pull-request only, bounded, full
history, the PR body reaching the shell only through ``env:``, the verdict uploaded
with ``if: always()``. The ``edited`` trigger exists for the PR body alone, so every
job that existed before must carry an explicit exclusion and keep its own trigger.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "vnx-ci.yml"
JOB = "red-on-base"
EDITED_EXCLUSION = "github.event.action != 'edited'"
MERGE_BASE_STEP_ID = "merge_base"
MERGE_BASE_OUTPUT_REF = "${{ steps.merge_base.outputs.sha }}"
BASE_TIP_TOKENS = ("github.event.pull_request.base.sha", "github.base_ref")
BASE_ARG = re.compile(r"--base[= ]\"?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?\"?")
SHA_OUTPUT_LINE = re.compile(r'echo\s+"?sha=\$merge_base"?\s*>>\s*"?\$GITHUB_OUTPUT"?')


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


def _writes_sha_output(run: str) -> bool:
    """True only when *run* persists the computed merge-base as ``sha`` to ``$GITHUB_OUTPUT``."""
    return any(SHA_OUTPUT_LINE.match(line.strip()) for line in run.splitlines())


def _merge_base_violations(job: dict) -> list[str]:
    """Every way *job* can hand a base-branch tip to ``--base`` instead of the merge-base.

    Fail-closed: an absent or duplicated merge-base step, a step that persists no ``sha``
    output, a ``--base`` that does not resolve to ``steps.merge_base.outputs.sha`` (missing,
    ``None``, malformed or an unknown name), and any base-tip reference left after that
    computation each produce a violation. Only an empty list means the job is clean.
    """
    steps = job.get("steps") or []
    producer_ids = [index for index, step in enumerate(steps) if step.get("id") == MERGE_BASE_STEP_ID]
    if len(producer_ids) != 1:
        return [f"expected exactly one step with id {MERGE_BASE_STEP_ID!r}, found {len(producer_ids)}"]
    producer_index = producer_ids[0]
    violations: list[str] = []
    if not _writes_sha_output(str(steps[producer_index].get("run") or "")):
        violations.append(
            f"step id {MERGE_BASE_STEP_ID!r} does not persist its merge-base as sha to $GITHUB_OUTPUT"
        )
    consumers = [step for step in steps if "--base" in str(step.get("run") or "")]
    if not consumers:
        violations.append("no step passes --base at all")
    for step in consumers:
        name = step.get("name")
        run = str(step.get("run") or "")
        env = step.get("env") or {}
        variables = BASE_ARG.findall(run)
        if not variables:
            violations.append(f"step {name!r}: --base does not resolve through an env variable")
        for variable in variables:
            if env.get(variable) != MERGE_BASE_OUTPUT_REF:
                violations.append(
                    f"step {name!r}: --base resolves through {variable!r} to {env.get(variable)!r}, "
                    f"not {MERGE_BASE_OUTPUT_REF!r}"
                )
        for token in BASE_TIP_TOKENS:
            if token in run:
                violations.append(f"step {name!r}: --base reads the base-branch tip {token!r}")
    for step in steps[producer_index + 1:]:
        for token in BASE_TIP_TOKENS:
            if token in json.dumps(step):
                violations.append(
                    f"step {step.get('name')!r} reads the base-branch tip {token!r} after the merge-base"
                )
    return violations


def _producer(job: dict) -> dict:
    producers = [step for step in job["steps"] if step.get("id") == MERGE_BASE_STEP_ID]
    assert len(producers) == 1, f"expected one step with id {MERGE_BASE_STEP_ID!r}, found {len(producers)}"
    return producers[0]


def _runner(job: dict) -> dict:
    return _by_name(job)["Run the claimed tests on base and on head"]


def _mutated(jobs: dict, case: str) -> dict:
    """A copy of the real job with one fail-open mutation applied."""
    job = copy.deepcopy(jobs[JOB])
    if case == "missing_step":
        job["steps"] = [step for step in job["steps"] if step.get("id") != MERGE_BASE_STEP_ID]
    elif case == "missing_output":
        _producer(job)["run"] = 'set -euo pipefail\ngit merge-base "$BASE_SHA" "$HEAD_SHA"\n'
    elif case == "unknown_output":
        _producer(job)["run"] = _producer(job)["run"].replace('"sha=$merge_base"', '"base=$merge_base"')
    elif case == "none_env":
        _runner(job)["env"]["MERGE_BASE_SHA"] = None
    elif case == "malformed_base":
        _runner(job)["run"] = _runner(job)["run"].replace(
            '--base "$MERGE_BASE_SHA"', '--base "${{ github.event.pull_request.base.sha }}"'
        )
    elif case == "base_tip_env":
        _runner(job)["env"]["BASE_SHA"] = "${{ github.event.pull_request.base.sha }}"
    elif case == "no_base_argument":
        _runner(job)["run"] = _runner(job)["run"].replace('--base "$MERGE_BASE_SHA"', "")
    else:
        raise AssertionError(f"unknown mutation case {case!r}")
    return job


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


def test_the_merge_base_is_computed_once_and_persisted(jobs):
    producer = _producer(jobs[JOB])
    assert producer["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert producer["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert 'git merge-base "$BASE_SHA" "$HEAD_SHA"' in producer["run"]
    assert _writes_sha_output(producer["run"])


def test_base_is_the_persisted_merge_base_output(jobs):
    runner = _runner(jobs[JOB])
    assert runner["env"]["MERGE_BASE_SHA"] == MERGE_BASE_OUTPUT_REF
    assert '--base "$MERGE_BASE_SHA"' in runner["run"]


def test_no_step_passes_the_base_branch_tip_to_base(jobs):
    for step in jobs[JOB]["steps"]:
        run = str(step.get("run") or "")
        if "--base" not in run:
            continue
        for token in BASE_TIP_TOKENS:
            assert token not in run, f"{step.get('name')!r} passes {token!r} to --base"


def test_the_selection_diffs_the_same_merge_base(jobs):
    selection = _by_name(jobs[JOB])["Select the PR's test files"]
    assert selection["env"]["MERGE_BASE_SHA"] == MERGE_BASE_OUTPUT_REF
    assert '"$MERGE_BASE_SHA" "$HEAD_SHA"' in selection["run"]
    assert _merge_base_violations(jobs[JOB]) == []


@pytest.mark.parametrize(
    "case",
    ["missing_step", "missing_output", "unknown_output", "none_env", "malformed_base",
     "base_tip_env", "no_base_argument"],
)
def test_a_job_that_does_not_pass_the_merge_base_stays_blocking(jobs, case):
    assert _merge_base_violations(_mutated(jobs, case)), f"mutation {case!r} was not reported"
