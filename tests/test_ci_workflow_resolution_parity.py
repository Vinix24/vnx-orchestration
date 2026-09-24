#!/usr/bin/env python3
"""The CI workflow name is resolved the same way at gate time and at merge time (OI-1849).

``pre_merge_gate._resolve_ci_workflow_name`` and
``merge_preflight_ci_check._resolve_workflow_name`` point at each other in their
docstrings ("keep the two in sync"). Two functions that say so in a comment drift
the first time one of them is touched; this holds them to it.

Order, both: explicit argument > ``ci_workflow`` in the project's
``branch_protection.yaml`` > ``VNX_CI_WORKFLOW_NAME`` > ``VNX CI``.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import merge_preflight_ci_check as mpc
import pre_merge_gate as pmg
from merge_target_helpers import consumer_yaml, git_repo, install_gh_stub

SHA = "e" * 40
EXPLICIT, PROJECT, ENV = "Explicit Flow", "Project Flow", "Env Flow"


def _expected(explicit, project, env):
    return explicit or project or env or "VNX CI"


@pytest.mark.parametrize("explicit,project,env", list(itertools.product(
    [None, EXPLICIT], [None, PROJECT], [None, ENV],
)))
def test_both_resolvers_agree_on_every_combination(monkeypatch, explicit, project, env):
    if env is None:
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
    else:
        monkeypatch.setenv("VNX_CI_WORKFLOW_NAME", env)

    merge_side = mpc._resolve_workflow_name(explicit, project)
    gate_side = pmg._resolve_ci_workflow_name(explicit, project)

    assert merge_side == gate_side == _expected(explicit, project, env)


def test_the_two_defaults_are_the_same_string():
    assert mpc.DEFAULT_CI_WORKFLOW_NAME == pmg.DEFAULT_CI_WORKFLOW_NAME == "VNX CI"
    assert mpc.CI_WORKFLOW_NAME_ENV_VAR == pmg.CI_WORKFLOW_NAME_ENV_VAR == "VNX_CI_WORKFLOW_NAME"


def _runs_rule():
    """A ``gh run list`` answer that green-lights SHA, for any workflow."""
    return {
        "match": "run list",
        "stdout": json.dumps([{
            "conclusion": "success", "headSha": SHA, "status": "completed",
            "databaseId": 3, "createdAt": "2026-09-24T09:00:00Z",
        }]),
    }


def _workflow_asked(stub):
    argv = stub.calls_with("run list")[0]["argv"]
    return argv[argv.index("--workflow") + 1]


def _project_with(tmp_path, text):
    project = git_repo(tmp_path / "project", "https://github.com/consumer/y.git")
    if text is not None:
        path = project / ".vnx" / "branch_protection.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
    return project


class TestPreMergeGateReadsTheProjectFile:
    """``vnx pre-merge-gate`` runs inside the checkout it judges, so it reads the local copy."""

    def _check(self, project):
        return pmg.check_ci_workflow(project, branch="feature/x", head_sha=SHA)

    def test_the_declared_workflow_is_the_one_asked_for(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, [_runs_rule()])
        project = _project_with(tmp_path, consumer_yaml(ci_workflow="CI"))

        result = self._check(project)

        assert _workflow_asked(stub) == "CI"
        assert result["status"] == "GO", result

    def test_a_project_without_the_field_keeps_the_environment_and_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_CI_WORKFLOW_NAME", ENV)
        stub = install_gh_stub(tmp_path, monkeypatch, [_runs_rule()])

        self._check(_project_with(tmp_path, consumer_yaml()))

        assert _workflow_asked(stub) == ENV

    def test_a_broken_file_cannot_be_verified_rather_than_guessed_past(self, tmp_path, monkeypatch):
        stub = install_gh_stub(tmp_path, monkeypatch, [_runs_rule()])
        project = _project_with(tmp_path, "branch: main\nnot_a_field: 1\n")

        result = self._check(project)

        assert result["status"] == pmg.SKIPPED_UNVERIFIED
        assert "branch_protection.yaml" in result["detail"]
        assert stub.calls_with("run list") == []


class TestMergePreflightCliReadsTheProjectFile:
    def _run(self, project, capsys):
        rc = mpc.main(["--project-root", str(project), "--head-sha", SHA, "--json"])
        return rc, capsys.readouterr()

    def test_the_declared_workflow_is_the_one_asked_for(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
        monkeypatch.delenv("VNX_MERGE_OVERRIDE_REASON", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, [
            {"match": "auth status", "stdout": "ok\n"}, _runs_rule(),
        ])
        project = _project_with(tmp_path, consumer_yaml(ci_workflow="CI"))

        rc, out = self._run(project, capsys)

        assert rc == 0, out
        assert _workflow_asked(stub) == "CI"
        assert json.loads(out.out)["workflow_name"] == "CI"

    def test_a_broken_file_stops_the_cli(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("VNX_MERGE_OVERRIDE_REASON", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, [
            {"match": "auth status", "stdout": "ok\n"}, _runs_rule(),
        ])
        project = _project_with(tmp_path, "branch: main\nnot_a_field: 1\n")

        rc, out = self._run(project, capsys)

        assert rc == 1
        assert "CI-workflow van het project kon niet worden bepaald" in out.err
        assert stub.calls_with("run list") == []


def test_check_ci_run_for_head_ranks_the_project_below_the_argument_and_above_the_environment(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("VNX_CI_WORKFLOW_NAME", ENV)
    monkeypatch.delenv("VNX_MERGE_OVERRIDE_REASON", raising=False)
    project = git_repo(tmp_path / "p", "https://github.com/consumer/y.git")
    stub = install_gh_stub(tmp_path, monkeypatch, [
        {"match": "auth status", "stdout": "ok\n"}, _runs_rule(),
    ])

    mpc.check_ci_run_for_head(project, head_sha=SHA, project_workflow=PROJECT)
    mpc.check_ci_run_for_head(project, head_sha=SHA, project_workflow=PROJECT, workflow_name=EXPLICIT)

    asked = [c["argv"][c["argv"].index("--workflow") + 1] for c in stub.calls_with("run list")]
    assert asked == [PROJECT, EXPLICIT]
