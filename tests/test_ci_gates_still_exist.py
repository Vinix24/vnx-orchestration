#!/usr/bin/env python3
"""Blocking-CI contract for the whole-tree scans that left the Profile A parallel phase.

Four whole-tree scans ran twice per PR: in their own blocking ``profile-a`` step and
again in the parallel pytest phase (~40 s). tsk-5 removed the parallel copies, so the
step is now the only thing that runs the check; this file guards it — parse the
workflow as YAML, find the step in the ``profile-a`` job, and fail, naming the removed
test, when the step, its command, its ``set -euo pipefail`` or its blocking nature (no
``continue-on-error``) is gone. Without it, dropping step and test together is silent.

Fail-closed on evidence: an unreadable or malformed workflow, a missing job, a missing
or duplicated step, a missing, empty or non-string ``run``, an absent command token, a
missing ``set -euo pipefail`` and any ``continue-on-error`` that is not plainly false
are all violations — never an empty pass. See ADR-053.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "vnx-ci.yml"
PROFILE_A_JOB = "profile-a"
PIPE_FAIL = "set -euo pipefail"
# GitHub accepts a boolean or an expression string here; only these spell "still blocking".
_BLOCKING_VALUES = frozenset({"false", "0", "no"})


@dataclass(frozen=True)
class CiGate:
    """One blocking step and the parallel-phase test(s) that left because of it."""

    step: str
    command_token: str
    removed_tests: tuple[str, ...]


GATES = (
    CiGate(
        "Side-door exhaustiveness audit",
        "scripts/lib/dispatch_sidedoor_audit.py",
        (
            "tests/test_dispatch_sidedoor_audit.py::test_no_unaudited_side_door_callers",
            "tests/test_dispatch_sidedoor_audit.py::test_no_unaudited_raw_claude_spawns",
        ),
    ),
    CiGate(
        "Central-mode path gate",
        "scripts/check_no_file_derived_data_paths.py",
        ("tests/test_central_mode_path_gate.py::test_current_tree_passes",),
    ),
    CiGate(
        "Run architecture doc drift guard unit tests",
        "tests/test_architecture_doc_drift.py",
        ("tests/test_architecture_doc_drift.py (whole file, scripts/ci/test_exclusions.txt)",),
    ),
    CiGate(
        "Run status vocabulary drift guard unit tests",
        "tests/test_status_vocab_drift.py",
        ("tests/test_status_vocab_drift.py (whole file, scripts/ci/test_exclusions.txt)",),
    ),
)


def _disables_blocking(value: object) -> bool:
    """True when a ``continue-on-error`` value would stop the step from blocking.

    Absent (``None``) and ``False`` keep the step blocking. A string is blocking only
    when it spells one of ``_BLOCKING_VALUES``; an expression, an unknown word and any
    non-string non-false value are fail-closed as "non-blocking".
    """
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _BLOCKING_VALUES
    return True


def _load_workflow(path: Path) -> tuple[dict | None, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{path}: unreadable workflow ({exc})"
    except UnicodeDecodeError as exc:
        return None, f"{path}: undecodable workflow ({exc})"
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"{path}: malformed YAML ({exc})"
    if not isinstance(document, dict):
        return None, f"{path}: workflow document is not a mapping (got {type(document).__name__})"
    return document, ""


def _gate_names() -> str:
    return "; ".join(f"{gate.step!r} guards {', '.join(gate.removed_tests)}" for gate in GATES)


def _gate_violations(steps: list, gate: CiGate, path: Path) -> list[str]:
    label = f"{path}: step {gate.step!r} (guards {', '.join(gate.removed_tests)})"
    matches = [step for step in steps if isinstance(step, dict) and step.get("name") == gate.step]
    if not matches:
        return [f"{label}: step is gone — the parallel-phase test it replaced is no longer guarded"]
    violations = []
    if len(matches) > 1:
        violations.append(f"{label}: step name appears {len(matches)} times; exactly one is checkable")
    step = matches[0]
    if _disables_blocking(step.get("continue-on-error")):
        violations.append(
            f"{label}: continue-on-error {step.get('continue-on-error')!r} makes the step non-blocking"
        )
    run = step.get("run")
    if not isinstance(run, str) or not run.strip():
        return violations + [f"{label}: run is {run!r} — the step must still run its command"]
    if gate.command_token not in run:
        violations.append(f"{label}: command no longer contains {gate.command_token!r}")
    if PIPE_FAIL not in run:
        violations.append(f"{label}: run block lost {PIPE_FAIL!r}, so a failure cannot stop the step")
    return violations


def gate_violations(path: Path = WORKFLOW) -> list[str]:
    """Every way the four blocking steps fall short of the ADR-053 contract."""
    document, error = _load_workflow(path)
    if error:
        return [f"{error}; unmeasured: {_gate_names()}"]
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return [f"{path}: no jobs mapping; unmeasured: {_gate_names()}"]
    job = jobs.get(PROFILE_A_JOB)
    if not isinstance(job, dict):
        return [f"{path}: no {PROFILE_A_JOB!r} job step; unmeasured: {_gate_names()}"]
    violations = []
    if _disables_blocking(job.get("continue-on-error")):
        violations.append(
            f"{path}: the {PROFILE_A_JOB!r} job sets continue-on-error "
            f"{job.get('continue-on-error')!r}; every step below it is non-blocking"
        )
    steps = job.get("steps")
    if not isinstance(steps, list):
        return violations + [f"{path}: {PROFILE_A_JOB!r} has no steps list; unmeasured: {_gate_names()}"]
    for gate in GATES:
        violations.extend(_gate_violations(steps, gate, path))
    return violations


def assert_gates_present(workflow_path: Path = WORKFLOW) -> None:
    violations = gate_violations(workflow_path)
    assert not violations, (
        "the blocking CI contract of ADR-053 is broken:\n"
        + "\n".join(f"  - {violation}" for violation in violations)
    )


# ---------------------------------------------------------------------------
# The real workflow
# ---------------------------------------------------------------------------


def test_the_four_blocking_steps_are_still_there() -> None:
    assert_gates_present(WORKFLOW)


# ---------------------------------------------------------------------------
# Broken copies: the same assertion must go red on each
# ---------------------------------------------------------------------------

_MISSING = object()


def _document() -> dict:
    document, error = _load_workflow(WORKFLOW)
    assert not error, error
    return document


def _steps(document: dict) -> list:
    return document["jobs"][PROFILE_A_JOB]["steps"]


def _step(document: dict, name: str) -> dict:
    return next(step for step in _steps(document) if step.get("name") == name)


def _violations(tmp_path: Path, mutate) -> list[str]:
    document = _document()
    mutate(document)
    target = tmp_path / "vnx-ci.yml"
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return gate_violations(target)


def _names_removed_test(violations: list[str], gate: CiGate) -> bool:
    return any(test in violation for violation in violations for test in gate.removed_tests)


GATE_IDS = [gate.step for gate in GATES]


@pytest.mark.parametrize("gate", GATES, ids=GATE_IDS)
def test_a_removed_step_turns_the_gate_red(tmp_path: Path, gate: CiGate) -> None:
    violations = _violations(tmp_path, lambda doc: _steps(doc).remove(_step(doc, gate.step)))
    assert any(gate.step in violation for violation in violations), violations
    assert _names_removed_test(violations, gate), violations


@pytest.mark.parametrize("gate", GATES, ids=GATE_IDS)
@pytest.mark.parametrize("value", [True, "${{ github.event_name == 'push' }}"], ids=["true", "expression"])
def test_a_non_blocking_step_turns_the_gate_red(tmp_path: Path, gate: CiGate, value: object) -> None:
    violations = _violations(
        tmp_path, lambda doc: _step(doc, gate.step).__setitem__("continue-on-error", value)
    )
    assert any("non-blocking" in violation for violation in violations), violations
    assert _names_removed_test(violations, gate), violations


def test_continue_on_error_false_stays_green(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path, lambda doc: _step(doc, GATES[0].step).__setitem__("continue-on-error", False)
    )
    assert violations == []


def _strip_run_pipefail(document: dict) -> None:
    run = _step(document, GATES[0].step)["run"]
    _step(document, GATES[0].step)["run"] = run.replace(PIPE_FAIL, "")


def _replace_run_command(document: dict) -> None:
    run = _step(document, GATES[0].step)["run"]
    _step(document, GATES[0].step)["run"] = run.replace(GATES[0].command_token, "scripts/lib/other.py")


@pytest.mark.parametrize(
    "mutate, expected",
    [(_strip_run_pipefail, PIPE_FAIL), (_replace_run_command, "command no longer contains")],
    ids=["without-pipefail", "dead-command"],
)
def test_a_weakened_run_block_turns_the_gate_red(tmp_path: Path, mutate, expected: str) -> None:
    assert any(expected in violation for violation in _violations(tmp_path, mutate))


@pytest.mark.parametrize(
    "run_value",
    [_MISSING, None, 42, ["python3", "scripts/lib/dispatch_sidedoor_audit.py"], "", "   "],
    ids=["key-absent", "none", "int", "list", "empty", "blank"],
)
def test_a_missing_blank_or_non_string_run_turns_the_gate_red(tmp_path: Path, run_value: object) -> None:
    def mutate(document: dict) -> None:
        step = _step(document, GATES[0].step)
        if run_value is _MISSING:
            step.pop("run")
        else:
            step["run"] = run_value

    violations = _violations(tmp_path, mutate)
    assert any(GATES[0].step in violation and "run is" in violation for violation in violations), violations


STRUCTURAL_BREAKS = {
    "no-jobs-mapping": (lambda doc: doc.pop("jobs"), "no jobs mapping"),
    "no-profile-a-job": (lambda doc: doc["jobs"].pop(PROFILE_A_JOB), PROFILE_A_JOB),
    "job-continue-on-error": (
        lambda doc: doc["jobs"][PROFILE_A_JOB].__setitem__("continue-on-error", True),
        "job sets continue-on-error",
    ),
    "steps-not-a-list": (lambda doc: doc["jobs"][PROFILE_A_JOB].__setitem__("steps", None), "no steps list"),
    "duplicate-step": (
        lambda doc: _steps(doc).append(dict(_step(doc, GATES[1].step))),
        "appears 2 times",
    ),
}


@pytest.mark.parametrize(
    "mutate, expected",
    STRUCTURAL_BREAKS.values(),
    ids=list(STRUCTURAL_BREAKS),
)
def test_a_broken_workflow_shape_turns_the_gate_red(tmp_path: Path, mutate, expected: str) -> None:
    assert any(expected in violation for violation in _violations(tmp_path, mutate))


@pytest.mark.parametrize(
    "content",
    [None, "", "jobs: [unclosed", "- just\n- a\n- list\n"],
    ids=["missing", "empty", "malformed", "list"],
)
def test_an_unusable_workflow_turns_the_gate_red(tmp_path: Path, content: str | None) -> None:
    target = tmp_path / "vnx-ci.yml"
    if content is not None:
        target.write_text(content, encoding="utf-8")
    violations = gate_violations(target)
    assert violations, f"a workflow with content {content!r} passed as evidence"
    joined = "\n".join(violations)
    assert all(gate.step in joined for gate in GATES), violations
