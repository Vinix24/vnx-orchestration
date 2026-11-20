"""The Profile A shape helper: both shapes, and every way it must refuse instead of answering empty."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

import ci_workflow_shape as shape

REPO = Path(__file__).resolve().parents[1]
REAL = REPO / ".github" / "workflows" / "vnx-ci.yml"
AGGREGATOR = REPO / "tests" / "fixtures" / "profile_a_aggregator_shape.yml"


def _doc(path: Path = AGGREGATOR) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _single() -> dict:
    """The real workflow without shards and guards: the single shape the required job had before C3."""
    doc = copy.deepcopy(_doc(REAL))
    for job_id in [*(f"profile-a-shard-{k}" for k in range(1, 5)), "profile-a-guards"]:
        doc["jobs"].pop(job_id)
    return doc


def test_the_real_workflow_is_the_aggregator_shape():
    a = shape.profile_a(REAL)
    assert a.shape == shape.AGGREGATOR
    assert [j.role for j in a.jobs] == [shape.ROLE_SHARD] * 4 + [shape.ROLE_GUARDS, shape.ROLE_AGGREGATOR]
    assert a.required_job.name == shape.REQUIRED_CHECK_NAME and a.required_job.job_id == "profile-a"
    assert a.job_running("Side-door exhaustiveness audit").name == "Profile A guards"
    assert a.job_running("Legacy path gate").name == "Profile A guards"
    timeouts = a.timeouts()
    assert [timeouts.pop(f"profile-a-shard-{k}") for k in range(1, 5)] == [26] * 4
    assert timeouts.pop("profile-a-guards") == 15 and "80" in timeouts.pop("profile-a") and timeouts == {}
    assert shape.profile_a(_single()).shape == shape.SINGLE


def test_the_aggregator_fixture_is_the_aggregator_shape():
    a = shape.profile_a(AGGREGATOR)
    assert a.shape == shape.AGGREGATOR
    assert sorted(j.name for j in a.jobs_with_role(shape.ROLE_SHARD)) == [f"Profile A shard ({k}/4)" for k in range(1, 5)]
    assert [j.name for j in a.jobs_with_role(shape.ROLE_GUARDS)] == ["Profile A guards"]
    assert a.required_job.role == shape.ROLE_AGGREGATOR and a.required_job.job_id == "aggregate"
    assert sorted(a.required_job.needs) == ["guards", "shard-1", "shard-2", "shard-3", "shard-4"]
    assert a.timeouts() == {"shard-1": 26, "shard-2": 26, "shard-3": 26, "shard-4": 26, "guards": 15, "aggregate": 80}


def test_steps_are_one_list_tagged_with_their_job_and_found_by_name():
    a = shape.profile_a(AGGREGATOR)
    assert {s.job.role for s in a.steps} == {shape.ROLE_SHARD, shape.ROLE_GUARDS, shape.ROLE_AGGREGATOR}
    assert a.job_running("Side-door exhaustiveness audit").name == "Profile A guards"
    assert a.job_running("Check the xdist completeness (required)").role == shape.ROLE_AGGREGATOR
    assert len(a.steps_named("Profile A shard run")) == 4


def test_a_step_name_found_twice_where_one_is_expected_raises():
    with pytest.raises(shape.WorkflowShapeError, match="found 4 times"):
        shape.profile_a(AGGREGATOR).step("Profile A shard run")
    assert len(shape.profile_a(AGGREGATOR).steps_named("Profile A shard run", shape.ROLE_SHARD)) == 4


def test_a_missing_step_raises_instead_of_returning_nothing():
    for lookup in (lambda a: a.step("nope"), lambda a: a.job_running("nope")):
        with pytest.raises(shape.WorkflowShapeError, match="found 0 times"):
            lookup(shape.profile_a(REAL))


@pytest.mark.parametrize("content", [None, "", "jobs: [unclosed", "- a\n- list\n", "jobs: {}\n", "jobs: [1]\n"],
                         ids=["missing", "empty", "malformed", "list", "no-jobs", "jobs-list"])
def test_an_unusable_workflow_raises(tmp_path, content):
    target = tmp_path / "vnx-ci.yml"
    if content is not None:
        target.write_text(content, encoding="utf-8")
    with pytest.raises(shape.WorkflowShapeError):
        shape.profile_a(target)


def test_an_undecodable_workflow_raises(tmp_path):
    (tmp_path / "w.yml").write_bytes(b"jobs:\n  a: \xff\xfe\n")
    with pytest.raises(shape.WorkflowShapeError, match="unreadable"):
        shape.profile_a(tmp_path / "w.yml")


def _drop(*ids):
    return lambda d: [d["jobs"].pop(i) for i in ids]


def _set(job, key, value):
    return lambda d: d["jobs"][job].__setitem__(key, value)


NOT_A_SHAPE = {
    "required-job-gone": (_drop("aggregate"), "exactly one job named"),
    "required-job-renamed": (_set("aggregate", "name", "Profile A"), "exactly one job named"),
    "required-name-twice": (lambda d: d["jobs"].__setitem__("x", {"name": shape.REQUIRED_CHECK_NAME}), "exactly one job named"),
    "shard-gone": (_drop("shard-3"), "do not cover 1..N"),
    "shard-twice": (_set("shard-3", "name", "Profile A shard (2/4)"), "do not cover 1..N"),
    "mixed-totals": (_set("shard-4", "name", "Profile A shard (4/5)"), "do not cover 1..N"),
    "no-guards": (_drop("guards"), "exactly one 'Profile A guards'"),
    "guards-without-shards": (_drop("shard-1", "shard-2", "shard-3", "shard-4"), "do not cover 1..N"),
    "skips-guards": (_set("aggregate", "needs", ["shard-1", "shard-2", "shard-3", "shard-4"]), "does not need"),
    "skips-a-shard": (_set("aggregate", "needs", ["shard-1", "shard-2", "shard-3", "guards"]), "does not need"),
    "no-if": (lambda d: d["jobs"]["aggregate"].pop("if"), "always()"),
    "if-success": (_set("aggregate", "if", "success()"), "always()"),
    "needs-mapping": (_set("aggregate", "needs", {"a": 1}), "needs value"),
    "guards-no-steps": (_set("guards", "steps", None), "no steps list"),
}


@pytest.mark.parametrize("mutate, expected", NOT_A_SHAPE.values(), ids=list(NOT_A_SHAPE))
def test_a_shape_that_is_neither_raises(mutate, expected):
    doc = copy.deepcopy(_doc())
    mutate(doc)
    with pytest.raises(shape.WorkflowShapeError, match=expected):
        shape.profile_a(doc).steps


def test_a_single_job_without_a_steps_list_raises():
    doc = _single()
    doc["jobs"]["profile-a"]["steps"] = None
    with pytest.raises(shape.WorkflowShapeError, match="no steps list"):
        shape.profile_a(doc)


def test_a_needs_value_or_a_job_that_is_not_usable_raises():
    doc = _single()
    doc["jobs"]["profile-a"]["needs"] = 7
    with pytest.raises(shape.WorkflowShapeError, match="needs value"):
        shape.profile_a(doc).required_job.needs
    doc["jobs"]["profile-a"]["needs"] = "x"
    assert shape.profile_a(doc).required_job.needs == ["x"]
    doc["jobs"]["other"] = None
    with pytest.raises(shape.WorkflowShapeError, match="not mappings"):
        shape.profile_a(doc)
