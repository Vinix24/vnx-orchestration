"""Workflow rules of track deterministische-testverificatie PR-3.

The ``red-on-base`` signal lives in ``.github/workflows/red-on-base.yml``, never in
``vnx-ci.yml``: its trigger includes the ``edited`` action, and a skipped job still
satisfies a required check. An ``edited`` event re-entering ``vnx-ci.yml`` would start a
run in which every pre-existing job is skipped — a run that concludes ``success`` — so a
body edit could turn a red head green for the required-check door and for the merge rule
that reads the latest ``VNX CI`` conclusion. ``vnx-ci.yml`` must therefore keep main's
``on:`` block and every pre-existing job's ``if:`` line.

The new file keeps the round-2 merge-base handling: one ``merge_base`` step persists the
computed sha, and both the selection and ``red_on_base.py --base`` read that one value.
The claims step persists the claims file as its own output, so ``red_on_base.py --claims``
can only read what that step wrote; an unreadable claim fails the job because the step is
neither ``continue-on-error`` nor missing ``set -euo pipefail``.
Every helper reports a violation instead of clearing when the evidence is absent,
``None``, malformed or unrecognised.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
VNX_CI = WORKFLOWS / "vnx-ci.yml"
RED_ON_BASE = WORKFLOWS / "red-on-base.yml"
RED_ON_BASE_JOB = "red-on-base"
EDITED_EXCLUSION = "github.event.action != 'edited'"
PRELUDE_ACTION = "./.github/actions/profile-a-prelude"
CHECKOUT_ACTION_PREFIX = "actions/checkout@"
RED_ON_BASE_RUNNER = "red_on_base.py"
MERGE_BASE_STEP_ID = "merge_base"
MERGE_BASE_OUTPUT_REF = "${{ steps.merge_base.outputs.sha }}"
CLAIMS_RUNNER = "red_run_claims.py"
CLAIMS_STEP_ID = "claims"
CLAIMS_OUTPUT_NAME = "path"
CLAIMS_OUTPUT_REF = "${{ steps.claims.outputs.path }}"
BASE_TIP_TOKENS = ("github.event.pull_request.base.sha", "github.base_ref")
BASE_ARG = re.compile(r"--base[= ]\"?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?\"?")
CLAIMS_ARG = re.compile(r"--claims[= ]\"?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?\"?")
SHA_OUTPUT_LINE = re.compile(r'echo\s+"?sha=\$merge_base"?\s*>>\s*"?\$GITHUB_OUTPUT"?')
CLAIMS_OUTPUT_LINE = re.compile(
    rf'echo\s+"?{CLAIMS_OUTPUT_NAME}=\$CLAIMS_PATH"?\s*>>\s*"?\$GITHUB_OUTPUT"?')
RED_RUN_EXPECTED_TYPES = ["opened", "synchronize", "reopened", "edited"]


# ---------------------------------------------------------------------------
# Loading — a missing, empty or non-mapping document is a failure, never an empty pass
# ---------------------------------------------------------------------------


def _load_workflow(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"workflow file not found: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not document:
        raise ValueError(f"{path} is not a non-empty mapping: {document!r}")
    return document


def _load_workflow_or_empty(path: Path) -> dict:
    """The workflow document of *path*, or ``{}`` when the file does not exist.

    The red run of this PR happens on ``origin/main``, which predates the new workflow.
    Reading the absent file as an empty mapping makes every new assertion fail on its own
    assertion instead of aborting the run with a ``FileNotFoundError``; an existing but
    empty or non-mapping file still raises, so a broken file never clears.
    """
    if not path.is_file():
        return {}
    return _load_workflow(path)


def _triggers(workflow: object) -> dict:
    """The ``on:`` block; PyYAML reads the bare key ``on`` as boolean ``True``."""
    if not isinstance(workflow, Mapping):
        raise ValueError(f"the workflow is not a mapping: {workflow!r}")
    for key in (True, "on"):
        if key in workflow:
            triggers = workflow[key]
            break
    else:
        raise ValueError("the workflow has no `on:` block")
    if not isinstance(triggers, Mapping) or not triggers:
        raise ValueError(f"the `on:` block is not a non-empty mapping: {triggers!r}")
    return triggers


# ---------------------------------------------------------------------------
# The edited trigger — must be absent from vnx-ci.yml, present only in red-on-base.yml
# ---------------------------------------------------------------------------


def _edited_trigger_violations(workflow: object) -> list[str]:
    """Every way the workflow's ``on:`` block can carry the ``edited`` action.

    Fail-closed: a missing or non-mapping ``on:`` block raises, and a ``types:`` value
    that is not a list of strings is a violation — an unreadable trigger never counts
    as "no edited". Only ``types`` absent (GitHub's default set, which excludes
    ``edited``) clears that event.
    """
    triggers = _triggers(workflow)
    violations: list[str] = []
    for event, config in triggers.items():
        if config is None:
            continue  # no `types:` means GitHub's default actions, never `edited`
        if not isinstance(config, Mapping):
            violations.append(f"trigger {event!r} is not a mapping: {config!r}")
            continue
        types = config.get("types")
        if types is None:
            continue
        if not isinstance(types, list) or not all(isinstance(entry, str) for entry in types):
            violations.append(f"trigger {event!r} has a malformed types list: {types!r}")
            continue
        if "edited" in types:
            violations.append(f"trigger {event!r} lists the edited action in its types")
    return violations


def _edited_exclusion_lines(text: object) -> list[int]:
    """Line numbers that carry the exclusion; a non-string (no text at all) raises."""
    if not isinstance(text, str):
        raise TypeError(f"workflow text is not a string: {type(text).__name__}")
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if EDITED_EXCLUSION in line
    ]


def test_the_new_workflow_is_pr_only_and_carries_the_edited_trigger(red_on_base):
    assert red_on_base, f"{RED_ON_BASE.name} is missing or empty"
    triggers = _triggers(red_on_base)
    assert list(triggers) == ["pull_request"]
    assert triggers["pull_request"]["types"] == RED_RUN_EXPECTED_TYPES
    assert red_on_base["name"] == "Red on base"


def test_vnx_ci_has_no_edited_trigger_and_no_exclusion():
    workflow = _load_workflow(VNX_CI)
    assert _edited_trigger_violations(workflow) == []
    assert _edited_exclusion_lines(VNX_CI.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("case", ["missing_on", "none_on", "scalar_on"])
def test_an_unreadable_on_block_raises_instead_of_clearing(case):
    if case == "missing_on":
        workflow = {"jobs": {"a": {}}}
    elif case == "none_on":
        workflow = {True: None}
    elif case == "scalar_on":
        workflow = {"on": "push"}
    else:  # pragma: no cover - guarded by the parametrization
        raise AssertionError(f"unknown case {case!r}")
    with pytest.raises(ValueError):
        _edited_trigger_violations(workflow)


@pytest.mark.parametrize(
    "case",
    ["malformed_types", "none_in_types", "unknown_event_config", "edited_in_types"],
)
def test_a_malformed_or_edited_trigger_stays_blocking(case):
    if case == "malformed_types":
        triggers = {"pull_request": {"types": "edited"}}
    elif case == "none_in_types":
        triggers = {"pull_request": {"types": [None]}}
    elif case == "unknown_event_config":
        triggers = {"pull_request": ["opened", "edited"]}
    elif case == "edited_in_types":
        triggers = {"pull_request": {"types": ["opened", "edited"]}}
    else:  # pragma: no cover - guarded by the parametrization
        raise AssertionError(f"unknown case {case!r}")
    assert _edited_trigger_violations({True: triggers}), f"case {case!r} cleared a malformed trigger"


def test_a_trigger_without_a_types_list_is_the_default_set_and_clears():
    triggers = {"pull_request": None, "push": {"branches": ["main"]}}
    assert _edited_trigger_violations({True: triggers}) == []


@pytest.mark.parametrize("case", ["in_job_if", "in_a_comment", "none_text", "bytes_text"])
def test_the_exclusion_is_found_wherever_it_is_and_needs_real_text(case):
    if case == "in_job_if":
        text = "jobs:\n  a:\n    if: github.event.action != 'edited'\n"
        assert _edited_exclusion_lines(text) == [3]
    elif case == "in_a_comment":
        text = "# github.event.action != 'edited' keeps the signal in step\non:\n  push:\n"
        assert _edited_exclusion_lines(text) == [1]
    elif case == "none_text":
        with pytest.raises(TypeError):
            _edited_exclusion_lines(None)
    elif case == "bytes_text":
        with pytest.raises(TypeError):
            _edited_exclusion_lines(b"github.event.action != 'edited'")
    else:  # pragma: no cover - guarded by the parametrization
        raise AssertionError(f"unknown case {case!r}")


# ---------------------------------------------------------------------------
# Job names — a check-run name of the new workflow must not exist in vnx-ci.yml
# ---------------------------------------------------------------------------


def _job_display_name(job_id: str, job: object) -> str:
    """The check-run name GitHub reports: the job's ``name``, or its id when it has none."""
    if not isinstance(job, Mapping):
        raise ValueError(f"job {job_id!r} is not a mapping: {job!r}")
    name = job.get("name")
    if name is None:
        return job_id
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"job {job_id!r} has a malformed name: {name!r}")
    return name


def _job_names(workflow: object) -> dict[str, str]:
    if not isinstance(workflow, Mapping):
        raise ValueError(f"the workflow is not a mapping: {workflow!r}")
    jobs = workflow.get("jobs")
    if not isinstance(jobs, Mapping) or not jobs:
        raise ValueError(f"the workflow has no jobs mapping: {jobs!r}")
    return {job_id: _job_display_name(job_id, job) for job_id, job in jobs.items()}


def _shared_job_names(candidate: object, reference: object) -> list[str]:
    """Every check-run name the candidate shares with the reference workflow."""
    reference_names = set(_job_names(reference).values())
    return sorted(name for name in _job_names(candidate).values() if name in reference_names)


def test_no_job_name_of_the_new_workflow_exists_in_vnx_ci(vnx_ci, red_on_base):
    assert isinstance(red_on_base.get("jobs"), Mapping) and red_on_base["jobs"], (
        f"{RED_ON_BASE.name} has no jobs mapping")
    assert _shared_job_names(red_on_base, vnx_ci) == []
    assert _job_names(red_on_base)[RED_ON_BASE_JOB] not in set(_job_names(vnx_ci).values())


@pytest.mark.parametrize(
    "case",
    ["name_missing", "name_none", "name_malformed", "job_not_a_mapping", "jobs_missing"],
)
def test_an_unreadable_job_name_stays_blocking(case):
    reference = {
        "jobs": {
            "profile-a": {"name": "Profile A (doctor + core tests)"},
            # A reference job without `name:` is reported to GitHub under its id.
            "xdist-parity": {"needs": ["profile-a"]},
        }
    }
    if case == "name_missing":  # no `name:` at all: GitHub reports the id, so it collides
        candidate = {"jobs": {"xdist-parity": {"runs-on": "ubuntu-latest"}}}
        assert _shared_job_names(candidate, reference) == ["xdist-parity"]
    elif case == "name_none":  # `name:` with no value reads as None: the id collides
        candidate = {"jobs": {"xdist-parity": {"name": None}}}
        assert _shared_job_names(candidate, reference) == ["xdist-parity"]
    elif case == "name_malformed":
        candidate = {"jobs": {"red-on-base": {"name": ["Profile A"]}}}
        with pytest.raises(ValueError):
            _shared_job_names(candidate, reference)
    elif case == "job_not_a_mapping":
        candidate = {"jobs": {"red-on-base": None}}
        with pytest.raises(ValueError):
            _shared_job_names(candidate, reference)
    elif case == "jobs_missing":
        candidate = {"name": "Red on base"}
        with pytest.raises(ValueError):
            _shared_job_names(candidate, reference)
    else:  # pragma: no cover - guarded by the parametrization
        raise AssertionError(f"unknown case {case!r}")


def test_a_unique_job_name_clears():
    reference = {"jobs": {"profile-a": {"name": "Profile A (doctor + core tests)"}}}
    candidate = {"jobs": {"red-on-base": {"name": "Red on base (claimed tests prove a behaviour change)"}}}
    assert _shared_job_names(candidate, reference) == []


# ---------------------------------------------------------------------------
# Setup before the run — the prelude (or its pinned install) must come first
# ---------------------------------------------------------------------------


def _is_checkout_step(step: Mapping) -> bool:
    uses = step.get("uses")
    return isinstance(uses, str) and uses.strip().startswith(CHECKOUT_ACTION_PREFIX)


def _is_prelude_step(step: Mapping) -> bool:
    uses = step.get("uses")
    return isinstance(uses, str) and uses.strip() == PRELUDE_ACTION


def _setup_violations(steps: object) -> list[str]:
    """Every way *steps* can run ``red_on_base.py`` without the test environment installed.

    Fail-closed: a missing or non-list ``steps``, a step that is not a mapping, a
    non-string ``run``, no ``uses: ./.github/actions/profile-a-prelude`` step (a prelude
    whose ``uses`` is ``None``, a bare ``pip install`` and an unrecognised install do not
    count), a missing checkout, a checkout that is not ``fetch-depth: 0``, a prelude that
    comes before the checkout and a prelude that only comes after the runner each produce
    a violation. Only an empty list means the job is clean.
    """
    if not isinstance(steps, list) or not steps:
        return ["the job has no steps"]
    violations: list[str] = []
    runner_indexes: list[int] = []
    checkout_indexes: list[int] = []
    prelude_indexes: list[int] = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            violations.append(f"step {index} is not a mapping: {step!r}")
            continue
        run = step.get("run")
        if run is not None and not isinstance(run, str):
            violations.append(f"step {step.get('name')!r} has a non-string run")
            continue
        if isinstance(run, str) and RED_ON_BASE_RUNNER in run:
            runner_indexes.append(index)
        if _is_checkout_step(step):
            checkout_indexes.append(index)
        if _is_prelude_step(step):
            prelude_indexes.append(index)
    if len(runner_indexes) != 1:
        violations.append(f"expected exactly one step running red_on_base.py, found {len(runner_indexes)}")
    if len(checkout_indexes) != 1:
        violations.append(f"expected exactly one checkout step, found {len(checkout_indexes)}")
    for checkout_index in checkout_indexes:
        checkout = steps[checkout_index]
        with_options = checkout.get("with")
        if not isinstance(with_options, Mapping) or with_options.get("fetch-depth") != 0:
            violations.append("the checkout step does not use fetch-depth: 0")
    if len(prelude_indexes) != 1:
        violations.append(f"expected exactly one step using {PRELUDE_ACTION}, found {len(prelude_indexes)}")
    for prelude_index in prelude_indexes:
        if not any(checkout_index < prelude_index for checkout_index in checkout_indexes):
            violations.append(f"the prelude at index {prelude_index} does not come after the checkout")
    for runner_index in runner_indexes:
        if prelude_indexes and not any(prelude_index < runner_index for prelude_index in prelude_indexes):
            violations.append(f"the step running red_on_base.py at index {runner_index} has no setup before it")
    return violations


def test_the_prelude_comes_after_the_checkout_and_before_red_on_base_runs(red_on_base):
    steps = _red_job(red_on_base.get("jobs") or {})["steps"]
    assert _setup_violations(steps) == []


def _mutated_steps(clean_steps: list, case: str) -> list:
    """A copy of the real step list with one fail-open mutation applied."""
    steps = copy.deepcopy(clean_steps)
    prelude_index = next(index for index, step in enumerate(steps) if isinstance(step, Mapping) and _is_prelude_step(step))
    checkout_index = next(index for index, step in enumerate(steps) if isinstance(step, Mapping) and _is_checkout_step(step))
    runner_index = next(
        index
        for index, step in enumerate(steps)
        if isinstance(step, Mapping) and isinstance(step.get("run"), str) and RED_ON_BASE_RUNNER in step["run"]
    )
    if case == "missing_setup":
        del steps[prelude_index]
    elif case == "setup_after_runner":
        steps.insert(runner_index + 1, steps.pop(prelude_index))
    elif case == "none_uses":
        steps[prelude_index]["uses"] = None
    elif case == "unknown_install":
        steps.insert(runner_index, {"name": "Install pytest", "run": "python -m pip install pytest"})
        del steps[prelude_index]
    elif case == "malformed_step":
        steps[prelude_index] = "uses: ./.github/actions/profile-a-prelude"
    elif case == "non_string_run":
        steps[runner_index]["run"] = None
    elif case == "no_steps":
        steps = []
    elif case == "no_checkout":
        del steps[checkout_index]
    elif case == "shallow_checkout":
        steps[checkout_index]["with"]["fetch-depth"] = 1
    elif case == "no_fetch_depth":
        del steps[checkout_index]["with"]["fetch-depth"]
    elif case == "prelude_before_checkout":
        steps.insert(checkout_index, steps.pop(prelude_index))
    else:  # pragma: no cover - guarded by the parametrization
        raise AssertionError(f"unknown case {case!r}")
    return steps


@pytest.mark.parametrize(
    "case",
    ["missing_setup", "setup_after_runner", "none_uses", "unknown_install", "malformed_step",
     "non_string_run", "no_steps", "no_checkout", "shallow_checkout", "no_fetch_depth",
     "prelude_before_checkout"],
)
def test_a_job_without_setup_before_the_runner_stays_blocking(red_on_base, case):
    clean = _red_job(red_on_base.get("jobs") or {})["steps"]
    assert _setup_violations(_mutated_steps(clean, case)), f"mutation {case!r} was not reported"


@pytest.mark.parametrize("case", ["none_steps", "not_a_list", "no_job"])
def test_an_unreadable_step_list_stays_blocking(case):
    if case == "none_steps":
        steps = None
    elif case == "not_a_list":
        steps = {"Checkout": {"uses": "actions/checkout@v4"}}
    elif case == "no_job":
        steps = []
    else:  # pragma: no cover - guarded by the parametrization
        raise AssertionError(f"unknown case {case!r}")
    assert _setup_violations(steps) == ["the job has no steps"]


# ---------------------------------------------------------------------------
# The job shape the round-2 work established, now in the new file
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vnx_ci() -> dict:
    return _load_workflow(VNX_CI)


@pytest.fixture(scope="module")
def red_on_base() -> dict:
    return _load_workflow_or_empty(RED_ON_BASE)


@pytest.fixture(scope="module")
def jobs(red_on_base: dict) -> dict:
    jobs = red_on_base.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def _red_job(jobs: object) -> dict:
    """The new job, or an assertion failure when the workflow (or the job) is absent."""
    assert isinstance(jobs, Mapping) and RED_ON_BASE_JOB in jobs, (
        f"{RED_ON_BASE.name} has no job {RED_ON_BASE_JOB!r}")
    return jobs[RED_ON_BASE_JOB]


def _by_name(job: dict) -> dict:
    return {step["name"]: step for step in job["steps"]}


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
    if not isinstance(job, Mapping):
        return [f"the job is not a mapping: {job!r}"]
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["the job has no steps"]
    producer_ids = [
        index for index, step in enumerate(steps)
        if isinstance(step, Mapping) and step.get("id") == MERGE_BASE_STEP_ID]
    if len(producer_ids) != 1:
        return [f"expected exactly one step with id {MERGE_BASE_STEP_ID!r}, found {len(producer_ids)}"]
    producer_index = producer_ids[0]
    violations: list[str] = []
    if not _writes_sha_output(str(steps[producer_index].get("run") or "")):
        violations.append(
            f"step id {MERGE_BASE_STEP_ID!r} does not persist its merge-base as sha to $GITHUB_OUTPUT"
        )
    consumers = [
        step for step in steps
        if isinstance(step, Mapping) and "--base" in str(step.get("run") or "")]
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
        name = step.get("name") if isinstance(step, Mapping) else step
        for token in BASE_TIP_TOKENS:
            if token in json.dumps(step):
                violations.append(
                    f"step {name!r} reads the base-branch tip {token!r} after the merge-base"
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
    job = copy.deepcopy(_red_job(jobs))
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
    job = _red_job(jobs)
    assert job["timeout-minutes"] == 30
    checkout = _by_name(job)["Checkout"]
    assert checkout["uses"].startswith(CHECKOUT_ACTION_PREFIX)
    assert checkout["with"]["fetch-depth"] == 0


def test_the_pr_body_reaches_the_shell_only_through_env(jobs):
    consumers = [step for step in _red_job(jobs)["steps"] if "pull_request.body" in json.dumps(step)]
    assert consumers, "no step reads github.event.pull_request.body"
    for step in consumers:
        assert "pull_request.body" not in str(step.get("run", ""))
        assert step["env"]["PR_BODY"] == "${{ github.event.pull_request.body }}"


def test_the_job_runs_the_claims_parser_and_the_verdict_tool(jobs):
    code = "\n".join(str(step.get("run", "")) for step in _red_job(jobs)["steps"])
    assert "scripts/ci/red_run_claims.py" in code
    assert "classify_ci_profile.py --tests-only" in code
    assert "git merge-base" in code
    assert "red_on_base.py" in code and "--claims" in code and "--out red_on_base.json" in code


def test_the_verdict_is_uploaded_also_when_the_job_is_red(jobs):
    uploads = [step for step in _red_job(jobs)["steps"]
               if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    assert uploads[0]["if"] == "always()"
    assert uploads[0]["with"]["path"] == "red_on_base.json"


def test_the_merge_base_is_computed_once_and_persisted(jobs):
    producer = _producer(_red_job(jobs))
    assert producer["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert producer["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert 'git merge-base "$BASE_SHA" "$HEAD_SHA"' in producer["run"]
    assert _writes_sha_output(producer["run"])


def test_base_is_the_persisted_merge_base_output(jobs):
    runner = _runner(_red_job(jobs))
    assert runner["env"]["MERGE_BASE_SHA"] == MERGE_BASE_OUTPUT_REF
    assert '--base "$MERGE_BASE_SHA"' in runner["run"]


def test_no_step_passes_the_base_branch_tip_to_base(jobs):
    for step in _red_job(jobs)["steps"]:
        run = str(step.get("run") or "")
        if "--base" not in run:
            continue
        for token in BASE_TIP_TOKENS:
            assert token not in run, f"{step.get('name')!r} passes {token!r} to --base"


def test_the_selection_diffs_the_same_merge_base(jobs):
    job = _red_job(jobs)
    selection = _by_name(job)["Select the PR's test files"]
    assert selection["env"]["MERGE_BASE_SHA"] == MERGE_BASE_OUTPUT_REF
    assert '"$MERGE_BASE_SHA" "$HEAD_SHA"' in selection["run"]
    assert _merge_base_violations(job) == []


@pytest.mark.parametrize(
    "case",
    ["missing_step", "missing_output", "unknown_output", "none_env", "malformed_base",
     "base_tip_env", "no_base_argument"],
)
def test_a_job_that_does_not_pass_the_merge_base_stays_blocking(jobs, case):
    assert _merge_base_violations(_mutated(jobs, case)), f"mutation {case!r} was not reported"


@pytest.mark.parametrize("job", [None, {}, {"steps": None}, {"steps": []}],
                         ids=["none", "no_steps_key", "none_steps", "empty_steps"])
def test_an_unreadable_job_never_clears_the_merge_base_check(job):
    assert _merge_base_violations(job)


# ---------------------------------------------------------------------------
# Claims — an unreadable claim fails the job, and --claims reads only its output
# ---------------------------------------------------------------------------


def _writes_claims_output(run: str) -> bool:
    """True only when *run* persists the claims file as ``path`` to ``$GITHUB_OUTPUT``."""
    return any(CLAIMS_OUTPUT_LINE.match(line.strip()) for line in run.splitlines())


def _claims_violations(job: object) -> list[str]:
    """Every way *job* can let an unreadable claim pass or lose it before ``red_on_base.py``.

    Fail-closed: a job that is not a mapping, no steps, a step that is not a mapping, no
    single step running ``red_run_claims.py``, a claims step with another id, with
    ``continue-on-error``, without ``set -euo pipefail`` or without a persisted ``path``
    output, and a ``red_on_base.py`` step that runs before the claims step or does not
    take ``--claims`` from that output each produce a violation. Only an empty list
    means the job is clean.
    """
    if not isinstance(job, Mapping):
        return [f"the job is not a mapping: {job!r}"]
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["the job has no steps"]
    violations: list[str] = []
    claims_indexes: list[int] = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            violations.append(f"step {index} is not a mapping: {step!r}")
            continue
        run = step.get("run")
        if run is not None and not isinstance(run, str):
            violations.append(f"step {step.get('name')!r} has a non-string run")
            continue
        if isinstance(run, str) and CLAIMS_RUNNER in run:
            claims_indexes.append(index)
    if len(claims_indexes) != 1:
        violations.append(
            f"expected exactly one step running {CLAIMS_RUNNER}, found {len(claims_indexes)}")
        return violations
    claims_index = claims_indexes[0]
    claims_step = steps[claims_index]
    if claims_step.get("id") != CLAIMS_STEP_ID:
        violations.append(
            f"the claims step has id {claims_step.get('id')!r}, not {CLAIMS_STEP_ID!r}")
    if claims_step.get("continue-on-error") not in (None, False):
        violations.append("the claims step sets continue-on-error, so an unreadable claim cannot fail the job")
    claims_run = claims_step.get("run")
    if not isinstance(claims_run, str):
        violations.append("the claims step has no readable run")
    else:
        if "set -euo pipefail" not in claims_run:
            violations.append("the claims step does not run under set -euo pipefail")
        if not _writes_claims_output(claims_run):
            violations.append(
                f"the claims step does not persist its file as {CLAIMS_OUTPUT_NAME!r} to $GITHUB_OUTPUT")
    runner_indexes = [
        index for index, step in enumerate(steps)
        if isinstance(step, Mapping) and RED_ON_BASE_RUNNER in str(step.get("run") or "")]
    if len(runner_indexes) != 1:
        violations.append(
            f"expected exactly one step running {RED_ON_BASE_RUNNER}, found {len(runner_indexes)}")
    for runner_index in runner_indexes:
        runner = steps[runner_index]
        name = runner.get("name")
        run = str(runner.get("run") or "")
        if runner_index < claims_index:
            violations.append(f"step {name!r} runs {RED_ON_BASE_RUNNER} before the claims step")
        if "--claims" not in run:
            violations.append(f"step {name!r} runs {RED_ON_BASE_RUNNER} without --claims")
            continue
        variables = CLAIMS_ARG.findall(run)
        if not variables:
            violations.append(f"step {name!r}: --claims does not resolve through an env variable")
        env = runner.get("env")
        env = env if isinstance(env, Mapping) else {}
        for variable in variables:
            if env.get(variable) != CLAIMS_OUTPUT_REF:
                violations.append(
                    f"step {name!r}: --claims resolves through {variable!r} to "
                    f"{env.get(variable)!r}, not {CLAIMS_OUTPUT_REF!r}")
    return violations


def _claims_step(job: dict) -> dict:
    steps = [
        step for step in job["steps"]
        if isinstance(step, Mapping) and CLAIMS_RUNNER in str(step.get("run") or "")]
    assert len(steps) == 1, f"expected one step running {CLAIMS_RUNNER}, found {len(steps)}"
    return steps[0]


def _mutated_claims(jobs: dict, case: str) -> object:
    """A copy of the real job with one fail-open claims mutation applied."""
    job = copy.deepcopy(_red_job(jobs))
    claims_step = _claims_step(job)
    runner = _runner(job)
    if case == "missing_claims_step":
        job["steps"] = [step for step in job["steps"] if step is not claims_step]
    elif case == "continue_on_error":
        claims_step["continue-on-error"] = True
    elif case == "no_set_euo":
        claims_step["run"] = claims_step["run"].replace("set -euo pipefail\n", "")
    elif case == "no_output":
        claims_step["run"] = claims_step["run"].replace(
            'echo "path=$CLAIMS_PATH" >> "$GITHUB_OUTPUT"', "")
    elif case == "wrong_id":
        claims_step["id"] = "claims-file"
    elif case == "none_run":
        claims_step["run"] = None
    elif case == "malformed_step":
        job["steps"] = ["not a mapping" if step is claims_step else step for step in job["steps"]]
    elif case == "runner_without_claims":
        runner["run"] = runner["run"].replace('--claims "$CLAIMS_PATH"', "")
    elif case == "claims_static_path":
        runner["env"]["CLAIMS_PATH"] = "$RUNNER_TEMP/claims.txt"
    elif case == "claims_none_env":
        runner["env"]["CLAIMS_PATH"] = None
    elif case == "claims_unknown_ref":
        runner["env"]["CLAIMS_PATH"] = "${{ steps.claims.outputs.unknown }}"
    elif case == "claims_after_runner":
        job["steps"].remove(claims_step)
        job["steps"].append(claims_step)
    elif case == "no_steps":
        job["steps"] = []
    else:
        raise AssertionError(f"unknown mutation case {case!r}")
    return job


def test_the_claims_are_persisted_and_read_back_from_the_same_step(jobs):
    job = _red_job(jobs)
    assert _claims_violations(job) == []
    claims_step = _claims_step(job)
    assert claims_step["id"] == CLAIMS_STEP_ID
    assert "continue-on-error" not in claims_step
    assert "set -euo pipefail" in claims_step["run"]
    runner = _runner(job)
    assert runner["env"]["CLAIMS_PATH"] == CLAIMS_OUTPUT_REF
    assert '--claims "$CLAIMS_PATH"' in runner["run"]


@pytest.mark.parametrize(
    "case",
    ["missing_claims_step", "continue_on_error", "no_set_euo", "no_output", "wrong_id",
     "none_run", "malformed_step", "runner_without_claims", "claims_static_path",
     "claims_none_env", "claims_unknown_ref", "claims_after_runner", "no_steps"],
)
def test_a_job_that_lets_an_unreadable_claim_through_stays_blocking(jobs, case):
    assert _claims_violations(_mutated_claims(jobs, case)), f"mutation {case!r} was not reported"


@pytest.mark.parametrize("job", [None, {}, {"steps": None}, {"steps": []}],
                         ids=["none", "no_steps_key", "none_steps", "empty_steps"])
def test_an_unreadable_job_never_clears_the_claims_check(job):
    assert _claims_violations(job)
