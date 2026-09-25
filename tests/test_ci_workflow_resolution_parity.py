#!/usr/bin/env python3
"""The CI workflow name is resolved the same way at gate time and at merge time (OI-1849).

``pre_merge_gate._resolve_ci_workflow_name`` and
``merge_preflight_ci_check._resolve_workflow_name`` point at each other in their
docstrings ("keep the two in sync"). Two functions that say so in a comment drift
the first time one of them is touched; this holds them to it.

Order, both: explicit argument > ``ci_workflow`` in the project's
``branch_protection.yaml`` on main > ``VNX_CI_WORKFLOW_NAME`` > ``VNX CI``.

And both read that declaration from the same place: main of the repo, over the
contents API. Never the checkout, which at gate time is on the PR's own branch.
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
from merge_target_helpers import (
    CONSUMER_REPO,
    consumer_yaml,
    contents_response,
    git_repo,
    install_gh_stub,
    not_found_rules,
)

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


PROJECT_YAML_PATH = ".vnx/branch_protection.yaml"
FABRIC_STYLE_YAML_PATH = "scripts/forge/branch_protection.yaml"


def _main_rules(text):
    """What ``consumer/y`` answers for its ``branch_protection.yaml`` on main. ``None``: no file."""
    if text is None:
        return not_found_rules(CONSUMER_REPO, PROJECT_YAML_PATH, FABRIC_STYLE_YAML_PATH)
    return [{
        "match": f"contents/{PROJECT_YAML_PATH}?ref=main", "repo": CONSUMER_REPO,
        "stdout": contents_response(text),
    }]


def _main_unreadable_rules():
    return [{
        "match": f"contents/{PROJECT_YAML_PATH}?ref=main", "repo": CONSUMER_REPO,
        "rc": 1, "stderr": "gh: Server Error (HTTP 500)\n",
    }]


def _checkout(tmp_path, local_text):
    """The checkout the gate runs in: on the PR's branch, so its own copy of the file is the PR's."""
    project = git_repo(tmp_path / "project", "https://github.com/consumer/y.git")
    if local_text is not None:
        path = project / PROJECT_YAML_PATH
        path.parent.mkdir(parents=True)
        path.write_text(local_text, encoding="utf-8")
    return project


class TestPreMergeGateReadsMainNotTheCheckout:
    """``vnx pre-merge-gate`` runs inside the checkout it judges, and on gate time that checkout
    is on the PR's branch. The PR must not pick the workflow the gate asks for (review finding
    I1 on #1915), so the declaration is read from main of the repo, over the contents API, the
    way the merge door reads it."""

    def _check(self, project, **kwargs):
        return pmg.check_ci_workflow(project, branch="feature/x", head_sha=SHA, **kwargs)

    def test_the_workflow_main_declares_beats_the_one_the_checkout_declares(self, tmp_path, monkeypatch):
        """The probe: the PR's branch says ``Always Green``, main says ``CI``."""
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, _main_rules(consumer_yaml(ci_workflow="CI")) + [_runs_rule()])
        project = _checkout(tmp_path, consumer_yaml(ci_workflow="Always Green"))

        result = self._check(project)

        assert _workflow_asked(stub) == "CI"
        assert result["status"] == "GO", result

    def test_the_file_is_read_from_main_of_the_repo_the_checkout_belongs_to(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, _main_rules(consumer_yaml(ci_workflow="CI")) + [_runs_rule()])

        self._check(_checkout(tmp_path, None))

        assert stub.repos_of("contents/") == {CONSUMER_REPO}
        assert stub.calls_with(f"contents/{PROJECT_YAML_PATH}?ref=main")

    def test_a_workflow_only_the_checkout_declares_is_not_asked_for(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, _main_rules(consumer_yaml()) + [_runs_rule()])

        self._check(_checkout(tmp_path, consumer_yaml(ci_workflow="Always Green")))

        assert _workflow_asked(stub) == "VNX CI"

    def test_a_project_without_the_field_on_main_keeps_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_CI_WORKFLOW_NAME", ENV)
        stub = install_gh_stub(tmp_path, monkeypatch, _main_rules(consumer_yaml()) + [_runs_rule()])

        self._check(_checkout(tmp_path, None))

        assert _workflow_asked(stub) == ENV

    def test_no_file_on_main_keeps_the_environment_then_the_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, _main_rules(None) + [_runs_rule()])

        self._check(_checkout(tmp_path, consumer_yaml(ci_workflow="Always Green")))

        assert _workflow_asked(stub) == "VNX CI"

    def test_a_broken_file_on_main_cannot_be_verified_rather_than_guessed_past(self, tmp_path, monkeypatch):
        rules = _main_rules("branch: main\nnot_a_field: 1\n") + [_runs_rule()]
        stub = install_gh_stub(tmp_path, monkeypatch, rules)

        result = self._check(_checkout(tmp_path, None))

        assert result["status"] == pmg.SKIPPED_UNVERIFIED
        assert "branch_protection.yaml" in result["detail"]
        assert stub.calls_with("run list") == []

    def test_a_main_that_cannot_be_read_cannot_be_verified(self, tmp_path, monkeypatch):
        stub = install_gh_stub(tmp_path, monkeypatch, _main_unreadable_rules() + [_runs_rule()])

        result = self._check(_checkout(tmp_path, consumer_yaml(ci_workflow="CI")))

        assert result["status"] == pmg.SKIPPED_UNVERIFIED
        assert "HTTP 500" in result["detail"]
        assert stub.calls_with("run list") == []

    def test_a_broken_file_in_the_checkout_is_never_read(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, _main_rules(consumer_yaml(ci_workflow="CI")) + [_runs_rule()])

        result = self._check(_checkout(tmp_path, "branch: main\nnot_a_field: 1\n"))

        assert result["status"] == "GO", result
        assert _workflow_asked(stub) == "CI"

    def test_an_explicit_workflow_needs_no_read_of_main(self, tmp_path, monkeypatch):
        stub = install_gh_stub(tmp_path, monkeypatch, _main_unreadable_rules() + [_runs_rule()])

        result = self._check(_checkout(tmp_path, None), workflow_name=EXPLICIT)

        assert result["status"] == "GO", result
        assert _workflow_asked(stub) == EXPLICIT
        assert stub.calls_with("contents/") == []


class TestMergePreflightCliReadsMainNotTheCheckout:
    def _run(self, project, capsys, *extra):
        rc = mpc.main(["--project-root", str(project), "--head-sha", SHA, "--json", *extra])
        return rc, capsys.readouterr()

    @staticmethod
    def _rules(*main_rules):
        return [{"match": "auth status", "stdout": "ok\n"}, *main_rules, _runs_rule()]

    def test_the_workflow_main_declares_beats_the_one_the_checkout_declares(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
        monkeypatch.delenv("VNX_MERGE_OVERRIDE_REASON", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, self._rules(*_main_rules(consumer_yaml(ci_workflow="CI"))))
        project = _checkout(tmp_path, consumer_yaml(ci_workflow="Always Green"))

        rc, out = self._run(project, capsys)

        assert rc == 0, out
        assert _workflow_asked(stub) == "CI"
        assert json.loads(out.out)["workflow_name"] == "CI"
        assert stub.repos_of("contents/") == {CONSUMER_REPO}

    def test_a_project_without_a_declaration_on_main_keeps_the_environment(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("VNX_CI_WORKFLOW_NAME", ENV)
        monkeypatch.delenv("VNX_MERGE_OVERRIDE_REASON", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, self._rules(*_main_rules(None)))

        rc, out = self._run(_checkout(tmp_path, consumer_yaml(ci_workflow="Always Green")), capsys)

        assert rc == 0, out
        assert _workflow_asked(stub) == ENV

    def test_a_broken_file_on_main_stops_the_cli(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("VNX_MERGE_OVERRIDE_REASON", raising=False)
        stub = install_gh_stub(
            tmp_path, monkeypatch, self._rules(*_main_rules("branch: main\nnot_a_field: 1\n")),
        )

        rc, out = self._run(_checkout(tmp_path, None), capsys)

        assert rc == 1
        assert "CI-workflow van het project kon niet worden bepaald" in out.err
        assert stub.calls_with("run list") == []

    def test_a_main_that_cannot_be_read_stops_the_cli(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("VNX_MERGE_OVERRIDE_REASON", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, self._rules(*_main_unreadable_rules()))

        rc, out = self._run(_checkout(tmp_path, consumer_yaml(ci_workflow="CI")), capsys)

        assert rc == 1
        assert "CI-workflow van het project kon niet worden bepaald" in out.err
        assert "HTTP 500" in out.err
        assert stub.calls_with("run list") == []

    def test_a_broken_file_in_the_checkout_is_never_read(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("VNX_CI_WORKFLOW_NAME", raising=False)
        monkeypatch.delenv("VNX_MERGE_OVERRIDE_REASON", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, self._rules(*_main_rules(consumer_yaml(ci_workflow="CI"))))

        rc, out = self._run(_checkout(tmp_path, "branch: main\nnot_a_field: 1\n"), capsys)

        assert rc == 0, out
        assert _workflow_asked(stub) == "CI"

    def test_an_explicit_workflow_needs_no_read_of_main(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("VNX_MERGE_OVERRIDE_REASON", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, self._rules(*_main_unreadable_rules()))

        rc, out = self._run(_checkout(tmp_path, None), capsys, "--workflow", EXPLICIT)

        assert rc == 0, out
        assert _workflow_asked(stub) == EXPLICIT
        assert stub.calls_with("contents/") == []

    def test_an_override_reason_needs_no_read_of_main(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("VNX_MERGE_OVERRIDE_REASON", raising=False)
        stub = install_gh_stub(tmp_path, monkeypatch, self._rules(*_main_unreadable_rules()))

        rc, out = self._run(_checkout(tmp_path, None), capsys, "--override-reason", "CI is stuk")

        assert rc == 0, out
        assert json.loads(out.out)["overridden"] is True
        assert stub.calls_with("contents/") == []


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
