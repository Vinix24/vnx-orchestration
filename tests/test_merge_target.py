#!/usr/bin/env python3
"""Tests for scripts/lib/merge_target.py (OI-1849): which project a merge goes
into, and what the running door is compared against.

Real git repos in tmp, a stub ``gh`` on PATH (tests/merge_target_helpers.py),
the real ``vnx_paths`` resolver. Nothing touches a real repo or ``~/.vnx-system``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import merge_target as mt
from merge_target_helpers import (
    CONSUMER_REPO,
    FABRIC_REPO,
    git_repo,
    install_gh_stub,
    isolate_project_env,
    make_consumer,
    make_install,
)

REPO_VIEW_RULES = [
    {"match": "repo view", "repo": CONSUMER_REPO, "stdout": CONSUMER_REPO + "\n"},
    {"match": "repo view", "repo": FABRIC_REPO, "stdout": FABRIC_REPO + "\n"},
]


class TestIsCentralInstall:
    def test_the_marker_says_central(self, tmp_path):
        assert mt.is_central_install(make_install(tmp_path)) is True

    def test_a_checkout_without_the_marker_is_not_central(self, tmp_path):
        assert mt.is_central_install(make_install(tmp_path, central=False)) is False

    def test_a_marker_that_says_something_else_is_not_central(self, tmp_path):
        install = make_install(tmp_path, central=False)
        (install / ".vnx-install-mode").write_text("embedded\n", encoding="utf-8")
        assert mt.is_central_install(install) is False


class TestDoorReference:
    """What the running door is compared against: main for a checkout, its own tag for an install."""

    def test_a_checkout_is_compared_to_main(self, tmp_path):
        assert mt.door_reference(make_install(tmp_path, central=False)) == ("main", "main")

    def test_an_install_is_compared_to_its_own_release_tag(self, tmp_path):
        ref, label = mt.door_reference(make_install(tmp_path, version="1.6.3"))
        assert ref == "v1.6.3"
        assert "v1.6.3" in label

    def test_an_install_without_a_version_cannot_be_judged(self, tmp_path):
        install = make_install(tmp_path)
        (install / "VERSION").unlink()
        with pytest.raises(mt.MergeTargetError, match="VERSION"):
            mt.door_reference(install)

    def test_an_install_with_an_empty_version_cannot_be_judged(self, tmp_path):
        install = make_install(tmp_path)
        (install / "VERSION").write_text("\n", encoding="utf-8")
        with pytest.raises(mt.MergeTargetError, match="leeg"):
            mt.door_reference(install)


class TestResolveTargetRepo:
    def test_the_repo_is_the_one_gh_resolves_in_that_directory(self, tmp_path, monkeypatch):
        install_gh_stub(tmp_path, monkeypatch, REPO_VIEW_RULES)
        assert mt.resolve_target_repo(make_consumer(tmp_path)) == CONSUMER_REPO
        assert mt.resolve_target_repo(make_install(tmp_path)) == FABRIC_REPO

    def test_a_gh_failure_is_an_error_with_its_cause(self, tmp_path, monkeypatch):
        install_gh_stub(tmp_path, monkeypatch, [
            {"match": "repo view", "rc": 1, "stderr": "no default remote repository\n"},
        ])
        with pytest.raises(mt.MergeTargetError, match="no default remote repository"):
            mt.resolve_target_repo(make_consumer(tmp_path))

    def test_a_directory_that_is_not_a_repo_is_an_error(self, tmp_path, monkeypatch):
        install_gh_stub(tmp_path, monkeypatch, REPO_VIEW_RULES)
        bare = tmp_path / "not-a-repo"
        bare.mkdir()
        with pytest.raises(mt.MergeTargetError):
            mt.resolve_target_repo(bare)

    def test_an_answer_that_is_not_owner_slash_name_is_an_error(self, tmp_path, monkeypatch):
        install_gh_stub(tmp_path, monkeypatch, [{"match": "repo view", "stdout": "no-slash-here\n"}])
        with pytest.raises(mt.MergeTargetError, match="owner/name"):
            mt.resolve_target_repo(make_consumer(tmp_path))

    def test_a_missing_gh_is_an_error(self, tmp_path, monkeypatch):
        consumer = make_consumer(tmp_path)
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        with pytest.raises(mt.MergeTargetError, match="gh CLI"):
            mt.resolve_target_repo(consumer)


class TestResolveMergeTarget:
    def test_run_from_a_project_the_target_is_the_project_not_the_install(self, tmp_path, monkeypatch):
        install = make_install(tmp_path)
        consumer = make_consumer(tmp_path)
        install_gh_stub(tmp_path, monkeypatch, REPO_VIEW_RULES)
        isolate_project_env(monkeypatch, install)
        monkeypatch.chdir(consumer)

        target = mt.resolve_merge_target(install)

        assert target.project_root == consumer
        assert target.repo == CONSUMER_REPO

    def test_the_project_root_variable_wins_over_the_cwd(self, tmp_path, monkeypatch):
        install = make_install(tmp_path)
        consumer = make_consumer(tmp_path)
        elsewhere = git_repo(tmp_path / "elsewhere", "https://github.com/other/z.git")
        install_gh_stub(tmp_path, monkeypatch, REPO_VIEW_RULES)
        isolate_project_env(monkeypatch, install)
        monkeypatch.setenv("VNX_PROJECT_ROOT", str(consumer))
        monkeypatch.chdir(elsewhere)

        target = mt.resolve_merge_target(install)

        assert target.project_root == consumer
        assert target.repo == CONSUMER_REPO

    def test_a_central_install_is_never_its_own_target(self, tmp_path, monkeypatch):
        """Run from inside the install, the resolver lands on the install. Merging "the PR
        of the install" would judge the fabric's repo for a merge meant for a project."""
        install = make_install(tmp_path)
        install_gh_stub(tmp_path, monkeypatch, REPO_VIEW_RULES)
        isolate_project_env(monkeypatch, install)
        monkeypatch.chdir(install)

        with pytest.raises(mt.MergeTargetError, match="installatie zelf"):
            mt.resolve_merge_target(install)

    def test_a_fabric_checkout_is_its_own_target(self, tmp_path, monkeypatch):
        """No marker, so not an install: the checkout the door runs from is the project."""
        checkout = make_install(tmp_path, central=False)
        install_gh_stub(tmp_path, monkeypatch, REPO_VIEW_RULES)
        isolate_project_env(monkeypatch, checkout)
        monkeypatch.chdir(checkout)

        target = mt.resolve_merge_target(checkout)

        assert target.project_root == checkout
        assert target.repo == FABRIC_REPO

    def test_an_unnameable_repo_stops_the_resolution(self, tmp_path, monkeypatch):
        install = make_install(tmp_path)
        consumer = make_consumer(tmp_path)
        install_gh_stub(tmp_path, monkeypatch, [{"match": "repo view", "rc": 1, "stderr": "HTTP 401\n"}])
        isolate_project_env(monkeypatch, install)
        monkeypatch.chdir(consumer)

        with pytest.raises(mt.MergeTargetError, match="HTTP 401"):
            mt.resolve_merge_target(install)
