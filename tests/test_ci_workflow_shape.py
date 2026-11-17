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


def test_the_real_workflow_is_the_single_shape():
    a = shape.profile_a(REAL)
    assert a.shape == shape.SINGLE and [j.role for j in a.jobs] == [shape.ROLE_SINGLE]
    assert a.required_job.name == shape.REQUIRED_CHECK_NAME
    assert a.job_running("Side-door exhaustiveness audit") is a.required_job
    assert a.needs(a.required_job) == []
    assert a.timeouts() == {a.required_job.job_id: a.required_job.timeout} and a.required_job.timeout


def test_the_aggregator_fixture_is_the_aggregator_shape():
    a = shape.profile_a(AGGREGATOR)
    assert a.shape == shape.AGGREGATOR
    assert sorted(j.name for j in a.jobs_with_role(shape.ROLE_SHARD)) == [f"Profile A shard ({k}/4)" for k in range(1, 5)]
    assert [j.name for j in a.jobs_with_role(shape.ROLE_GUARDS)] == ["Profile A guards"]
    assert a.required_job.role == shape.ROLE_AGGREGATOR and a.required_job.job_id == "aggregate"
    assert sorted(a.needs(a.required_job)) == ["guards", "shard-1", "shard-2", "shard-3", "shard-4"]
    assert a.timeouts() == {"shard-1": 38, "shard-2": 38, "shard-3": 38, "shard-4": 38, "guards": 15, "aggregate": 80}


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
    target = tmp_path / "vnx-ci.yml"
    target.write_bytes(b"jobs:\n  a: \xff\xfe\n")
    with pytest.raises(shape.WorkflowShapeError, match="unreadable"):
        shape.profile_a(target)


def _without_required_job(doc: dict) -> None:
    del doc["jobs"]["aggregate"]


def _renamed(doc: dict) -> None:
    doc["jobs"]["aggregate"]["name"] = "Profile A"


def _name_twice(doc: dict) -> None:
    doc["jobs"]["extra"] = {"name": shape.REQUIRED_CHECK_NAME, "steps": []}


def _drop_shard(doc: dict) -> None:
    del doc["jobs"]["shard-3"]


def _duplicate_shard(doc: dict) -> None:
    doc["jobs"]["shard-3"]["name"] = "Profile A shard (2/4)"


def _mixed_totals(doc: dict) -> None:
    doc["jobs"]["shard-4"]["name"] = "Profile A shard (4/5)"


def _no_guards(doc: dict) -> None:
    del doc["jobs"]["guards"]


def _guards_without_shards(doc: dict) -> None:
    for k in range(1, 5):
        del doc["jobs"][f"shard-{k}"]


def _aggregator_skips_guards(doc: dict) -> None:
    doc["jobs"]["aggregate"]["needs"] = ["shard-1", "shard-2", "shard-3", "shard-4"]


def _aggregator_skips_a_shard(doc: dict) -> None:
    doc["jobs"]["aggregate"]["needs"].remove("shard-2")


@pytest.mark.parametrize("mutate, expected", [
    (_without_required_job, "exactly one job named"),
    (_renamed, "exactly one job named"),
    (_name_twice, "exactly one job named"),
    (_drop_shard, "do not cover 1..N"),
    (_duplicate_shard, "do not cover 1..N"),
    (_mixed_totals, "do not cover 1..N"),
    (_no_guards, "exactly one 'Profile A guards'"),
    (_guards_without_shards, "do not cover 1..N"),
    (_aggregator_skips_guards, "does not need"),
    (_aggregator_skips_a_shard, "does not need"),
    (lambda d: d["jobs"]["aggregate"].pop("if"), "always()"),
    (lambda d: d["jobs"]["aggregate"].__setitem__("if", "success()"), "always()"),
    (lambda d: d["jobs"]["aggregate"].__setitem__("needs", {"a": 1}), "needs value"),
    (lambda d: d["jobs"]["guards"].__setitem__("steps", None), "no steps list"),
], ids=lambda v: v.__name__ if callable(v) and hasattr(v, "__name__") and v.__name__ != "<lambda>" else None)
def test_a_shape_that_is_neither_raises(mutate, expected):
    doc = copy.deepcopy(_doc())
    mutate(doc)
    with pytest.raises(shape.WorkflowShapeError, match=expected):
        shape.profile_a(doc).steps


def test_a_single_job_without_a_steps_list_raises():
    doc = copy.deepcopy(_doc(REAL))
    doc["jobs"]["profile-a"]["steps"] = None
    with pytest.raises(shape.WorkflowShapeError, match="no steps list"):
        shape.profile_a(doc)


def test_a_job_that_is_not_a_mapping_raises():
    doc = copy.deepcopy(_doc(REAL))
    doc["jobs"]["profile-a"]["needs"] = 7
    with pytest.raises(shape.WorkflowShapeError, match="needs value"):
        shape.profile_a(doc).needs(shape.profile_a(doc).required_job)
    doc["jobs"]["other"] = None
    with pytest.raises(shape.WorkflowShapeError, match="not mappings"):
        shape.profile_a(doc)


def test_needs_accepts_a_string_and_a_job_without_a_name_uses_its_id():
    doc = {"jobs": {shape.REQUIRED_CHECK_NAME: {"needs": "x", "steps": []}}}
    # a job whose id equals the check name is found by its (defaulted) display name
    assert shape.profile_a(doc).required_job.needs == ["x"]
