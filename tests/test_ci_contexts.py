"""Regression guards for scripts/lib/ci_contexts.py.

Two measured cases anchor this file and they look identical from the outside:

  #1691 — five of fourteen required contexts were never created during an
          Actions outage. ``gh pr checks`` showed nine passes, zero fails.
  #1701 — twelve of fourteen green, Profile B and Profile C absent because
          both declare ``needs: profile-a`` and profile-a had not finished.

The first must block loudly. The second must say "wait". A guard that
collapses them is either useless or blocks every PR in its first minutes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import ci_contexts as cc  # noqa: E402


# ---------------------------------------------------------------------------
# Graph parsing
# ---------------------------------------------------------------------------


def _write_workflow(tmp_path: Path, filename: str, body: str) -> Path:
    workflows = tmp_path / "workflows"
    workflows.mkdir(exist_ok=True)
    (workflows / filename).write_text(body, encoding="utf-8")
    return workflows


def test_graph_uses_job_name_as_context_and_falls_back_to_job_key(tmp_path):
    workflows = _write_workflow(
        tmp_path,
        "ci.yml",
        """
name: Demo CI
on: [push]
jobs:
  named:
    name: Profile A (doctor + core tests)
    runs-on: ubuntu-latest
  unnamed:
    runs-on: ubuntu-latest
""",
    )
    graph = cc.parse_workflow_graph(workflows)
    assert "Profile A (doctor + core tests)" in graph.by_context
    assert graph.by_context["Profile A (doctor + core tests)"].job_key == "named"
    # No `name:` — GitHub uses the job key as the context, and so do we.
    assert "unnamed" in graph.by_context
    assert graph.parse_errors == ()


@pytest.mark.parametrize(
    "needs_literal,expected",
    [("profile-a", ("profile-a",)), ("[profile-a, profile-d]", ("profile-a", "profile-d"))],
)
def test_graph_normalises_needs_string_and_list(tmp_path, needs_literal, expected):
    workflows = _write_workflow(
        tmp_path,
        "ci.yml",
        f"""
name: Demo CI
on: [push]
jobs:
  profile-a:
    name: A
    runs-on: ubuntu-latest
  profile-d:
    name: D
    runs-on: ubuntu-latest
  profile-b:
    name: B
    needs: {needs_literal}
    runs-on: ubuntu-latest
""",
    )
    graph = cc.parse_workflow_graph(workflows)
    assert tuple(graph.by_context["B"].needs) == expected


def test_graph_records_unparseable_file_instead_of_skipping_it(tmp_path):
    workflows = _write_workflow(tmp_path, "broken.yml", "name: [unclosed\njobs:\n")
    graph = cc.parse_workflow_graph(workflows)
    assert graph.by_context == {}
    assert len(graph.parse_errors) == 1
    assert "broken.yml" in graph.parse_errors[0]


def test_graph_reports_duplicate_context_rather_than_overwriting(tmp_path):
    workflows = _write_workflow(
        tmp_path,
        "ci.yml",
        """
name: Demo CI
on: [push]
jobs:
  first:
    name: Shared
    runs-on: ubuntu-latest
  second:
    name: Shared
    runs-on: ubuntu-latest
""",
    )
    graph = cc.parse_workflow_graph(workflows)
    assert graph.by_context["Shared"].job_key == "first"
    assert any("duplicate context" in e for e in graph.parse_errors)


def test_graph_on_missing_directory_is_an_error_not_an_empty_pass(tmp_path):
    graph = cc.parse_workflow_graph(tmp_path / "does-not-exist")
    assert graph.by_context == {}
    assert graph.parse_errors and "not found" in graph.parse_errors[0]


def test_real_vnx_ci_still_gates_profile_b_and_c_behind_profile_a():
    """The #1701 fact this module is calibrated against, guarded in place.

    If vnx-ci.yml ever drops ``needs: profile-a`` from Profile B/C, the
    waiting_upstream classification stops describing this repo and the guard
    silently changes meaning.
    """
    graph = cc.parse_workflow_graph(REPO_ROOT / ".github" / "workflows")
    for context in ("Profile B (snapshot integration)", "Profile C (adoption smoke tests)"):
        node = graph.by_context[context]
        assert node.workflow_file == "vnx-ci.yml"
        assert "profile-a" in node.needs


# ---------------------------------------------------------------------------
# Classification — contexts that exist
# ---------------------------------------------------------------------------

_GRAPH_BODY = """
name: VNX CI
on: [push]
jobs:
  profile-a:
    name: Profile A
    runs-on: ubuntu-latest
  profile-b:
    name: Profile B
    needs: profile-a
    runs-on: ubuntu-latest
"""


@pytest.fixture()
def graph(tmp_path):
    return cc.parse_workflow_graph(_write_workflow(tmp_path, "vnx-ci.yml", _GRAPH_BODY))


def _run(status="completed", conclusion="success", name="VNX CI", run_id=1):
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion}


def _check(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_present_and_successful_is_the_only_non_blocking_state(graph):
    [state] = cc.classify_contexts(["Profile A"], [_check("Profile A")], [_run()], graph)
    assert state.state == cc.STATE_PASSED
    assert state.blocking is False


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_skipped_and_neutral_conclusions_count_as_passing(graph, conclusion):
    [state] = cc.classify_contexts(
        ["Profile A"], [_check("Profile A", conclusion=conclusion)], [_run()], graph
    )
    assert state.state == cc.STATE_PASSED


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required", ""])
def test_terminal_non_passing_conclusion_is_failed(graph, conclusion):
    [state] = cc.classify_contexts(
        ["Profile A"], [_check("Profile A", conclusion=conclusion)], [_run()], graph
    )
    assert state.state == cc.STATE_FAILED
    assert state.blocking is True


def test_unfinished_check_run_is_running_not_failed(graph):
    [state] = cc.classify_contexts(
        ["Profile A"],
        [_check("Profile A", status="in_progress", conclusion=None)],
        [_run(status="in_progress", conclusion=None)],
        graph,
    )
    assert state.state == cc.STATE_RUNNING
    assert state.blocking is True and state.transient is True


def test_duplicate_check_runs_resolve_to_the_failing_one(graph):
    """One context, two runs (push + pull_request). The passing copy must not
    win: resolving a disagreement in favour of green is the green-lie itself.
    """
    [state] = cc.classify_contexts(
        ["Profile A"],
        [_check("Profile A"), _check("Profile A", conclusion="failure")],
        [_run()],
        graph,
    )
    assert state.state == cc.STATE_FAILED


# ---------------------------------------------------------------------------
# Classification — contexts that do NOT exist (the whole point)
# ---------------------------------------------------------------------------


def test_1701_shape_absent_context_behind_a_running_dependency_says_wait(graph):
    """Profile B absent, VNX CI alive, profile-a not finished — WAIT."""
    jobs = {1: [{"name": "Profile A", "status": "in_progress", "conclusion": None}]}
    [state] = cc.classify_contexts(
        ["Profile B"],
        [_check("Profile A", status="in_progress", conclusion=None)],
        [_run(status="in_progress", conclusion=None)],
        graph,
        jobs,
    )
    assert state.state == cc.STATE_WAITING_UPSTREAM
    assert state.transient is True
    assert "Profile A" in state.detail


def test_1691_shape_absent_context_on_a_finished_run_is_never_created(graph):
    """VNX CI concluded, Profile B never appeared — the outage shape. Blocks."""
    jobs = {1: [{"name": "Profile A", "status": "completed", "conclusion": "success"}]}
    [state] = cc.classify_contexts(
        ["Profile B"], [_check("Profile A")], [_run()], graph, jobs
    )
    assert state.state == cc.STATE_NEVER_CREATED
    assert state.blocking is True and state.transient is False
    assert "#1691" in state.detail


def test_absent_context_whose_dependency_failed_can_never_be_created(graph):
    """Run still alive, but profile-a already failed: waiting cannot help."""
    jobs = {1: [{"name": "Profile A", "status": "completed", "conclusion": "failure"}]}
    [state] = cc.classify_contexts(
        ["Profile B"],
        [_check("Profile A", conclusion="failure")],
        [_run(status="in_progress", conclusion=None)],
        graph,
        jobs,
    )
    assert state.state == cc.STATE_NEVER_CREATED
    assert "profile-a" in state.detail


def test_absent_context_with_no_workflow_run_at_all_is_no_run(graph):
    [state] = cc.classify_contexts(["Profile A"], [], [], graph)
    assert state.state == cc.STATE_NO_RUN
    assert state.blocking is True


def test_absent_context_no_job_produces_it_is_unverified_never_green(graph):
    [state] = cc.classify_contexts(["Some App Check"], [], [_run()], graph)
    assert state.state == cc.STATE_UNVERIFIED
    assert state.blocking is True
    assert "cannot tell" in state.detail


def test_waiting_names_the_dependency_even_without_job_data(graph):
    """Job-level detail may fail to load. The verdict must survive that.

    With no job list, an unobserved ancestor counts as pending — "not created
    yet" is exactly the state that makes the child wait — so the message still
    names Profile A instead of degrading to a bare "still running".
    """
    [state] = cc.classify_contexts(
        ["Profile B"], [], [_run(status="queued", conclusion=None)], graph
    )
    assert state.state == cc.STATE_WAITING_UPSTREAM
    assert "waiting on Profile A" in state.detail


def test_waiting_falls_back_to_declared_needs_when_the_dependency_is_unresolvable(tmp_path):
    """``needs:`` naming a job that does not exist in the file.

    The ancestor cannot be resolved to a context name, so there is nothing to
    report as pending — the message falls back to the raw declaration rather
    than claiming the run simply has not got round to the job.
    """
    broken = cc.parse_workflow_graph(
        _write_workflow(
            tmp_path,
            "vnx-ci.yml",
            """
name: VNX CI
on: [push]
jobs:
  profile-b:
    name: Profile B
    needs: ghost-job
    runs-on: ubuntu-latest
""",
        )
    )
    [state] = cc.classify_contexts(
        ["Profile B"], [], [_run(status="queued", conclusion=None)], broken
    )
    assert state.state == cc.STATE_WAITING_UPSTREAM
    assert "declares needs: ghost-job" in state.detail


def test_missing_context_without_dependencies_on_a_live_run_still_waits(graph):
    [state] = cc.classify_contexts(
        ["Profile A"], [], [_run(status="queued", conclusion=None)], graph
    )
    assert state.state == cc.STATE_WAITING_UPSTREAM
    assert "has not created this job yet" in state.detail


# ---------------------------------------------------------------------------
# Contract guards
# ---------------------------------------------------------------------------


def test_only_passed_is_non_blocking():
    """An allowlist, so a state added later blocks until someone decides."""
    assert cc.NON_BLOCKING_STATES == frozenset({cc.STATE_PASSED})


def test_summarise_counts_each_state_separately(graph):
    states = cc.classify_contexts(
        ["Profile A", "Profile B", "Some App Check"],
        [_check("Profile A")],
        [_run()],
        graph,
        {1: [{"name": "Profile A", "status": "completed", "conclusion": "success"}]},
    )
    summary = cc.summarise(states)
    assert summary["total"] == 3
    assert summary["passed"] == 1
    assert summary["never_created"] == 1
    assert summary["unverified"] == 1
    assert summary["blocking"] == 2
    assert summary["by_state"]["Profile B"] == cc.STATE_NEVER_CREATED


def test_required_contexts_unions_both_github_shapes(monkeypatch, tmp_path):
    payload = {
        "required_status_checks": {
            "contexts": ["legacy only", "shared"],
            "checks": [{"context": "shared"}, {"context": "checks only"}, {"no_context": 1}],
        }
    }
    monkeypatch.setattr(cc, "_gh_json", lambda *a, **k: payload)
    assert cc.fetch_required_contexts(tmp_path) == ["checks only", "legacy only", "shared"]


def test_required_contexts_refuses_a_non_mapping_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_gh_json", lambda *a, **k: ["not", "a", "mapping"])
    with pytest.raises(cc.CIContextsError):
        cc.fetch_required_contexts(tmp_path)


def test_gh_failure_raises_rather_than_returning_an_empty_list(tmp_path):
    with pytest.raises(cc.CIContextsError) as excinfo:
        cc._gh_json(["api", "repos/{owner}/{repo}/nope"], tmp_path, timeout=10)
    assert "failed" in str(excinfo.value) or "not available" in str(excinfo.value)
