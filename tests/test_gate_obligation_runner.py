"""tests/test_gate_obligation_runner.py — OI-1253 runner store-resolution guard.

The runner must never silently serve a store it cannot attribute to a project.
A central install whose only identity signal was a release-time git origin
resolves no project_id (the origin is refused by ``_project_id_from_git_remote``),
and ``_resolve_state_root`` would otherwise fall back to a project-local dir
under the immutable install. The runner must fail LOUD with an actionable
message instead of writing to a fabricated or unattributable store.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import vnx_paths  # noqa: E402
import gate_obligation_runner as runner  # noqa: E402


class TestDefaultStateDirGuard:
    """A runner without ``--state-dir`` must fail LOUD when project_id is None."""

    def test_loud_when_project_id_unresolvable(self, monkeypatch):
        monkeypatch.setattr(
            vnx_paths, "_resolve_state_project_id", lambda project_root: None,
        )
        with pytest.raises(runner.UnresolvableProjectError) as excinfo:
            runner._default_state_dir()
        message = str(excinfo.value)
        assert "--state-dir" in message
        assert "VNX_PROJECT_ID" in message

    def test_resolves_when_project_id_present(self, monkeypatch):
        monkeypatch.setattr(
            vnx_paths, "_resolve_state_project_id", lambda project_root: "vnx-dev",
        )
        state_dir = runner._default_state_dir()
        assert state_dir.name == "state"

    def test_main_returns_20_with_loud_error(self, monkeypatch, capsys):
        monkeypatch.setattr(
            vnx_paths, "_resolve_state_project_id", lambda project_root: None,
        )
        rc = runner.main([])
        assert rc == 20
        err = capsys.readouterr().err
        assert "project_id" in err
        assert "--state-dir" in err

    def test_main_state_dir_missing_still_returns_20(self, tmp_path, capsys):
        missing = tmp_path / "does-not-exist" / "state"
        rc = runner.main(["--state-dir", str(missing)])
        assert rc == 20
        assert "state dir not found" in capsys.readouterr().err


class TestOwnerRepoFromRemoteUrl:
    """``_owner_repo_from_remote_url`` accepts GitHub https/ssh forms and refuses
    anything else, including a local-filesystem origin (OI-1253)."""

    def test_https_url(self):
        assert (
            runner._owner_repo_from_remote_url(
                "https://github.com/Vinix24/vnx-orchestration.git"
            )
            == "Vinix24/vnx-orchestration"
        )

    def test_ssh_url(self):
        assert (
            runner._owner_repo_from_remote_url(
                "git@github.com:Vinix24/vnx-orchestration.git"
            )
            == "Vinix24/vnx-orchestration"
        )

    def test_no_trailing_git(self):
        assert (
            runner._owner_repo_from_remote_url(
                "https://github.com/Vinix24/vnx-orchestration"
            )
            == "Vinix24/vnx-orchestration"
        )

    def test_local_filesystem_origin_refused(self):
        assert (
            runner._owner_repo_from_remote_url(
                "/var/folders/ab/cd/T/vnx-checkout"
            )
            is None
        )

    def test_non_github_host_refused(self):
        assert runner._owner_repo_from_remote_url("https://gitlab.com/foo/bar.git") is None


class TestGhJsonRepoScoping:
    """``gh`` must be told the repo explicitly, never infer it from the cwd."""

    def test_injects_repo_flag_when_owner_repo_given(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name: "/opt/homebrew/bin/gh")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout='{"number": 1}')

        monkeypatch.setattr(runner.subprocess, "run", fake_run)
        result = runner._gh_json(["pr", "list"], owner_repo="Vinix24/vnx-orchestration")
        assert result == {"number": 1}
        assert captured["cmd"] == [
            "gh", "--repo", "Vinix24/vnx-orchestration", "pr", "list",
        ]

    def test_omits_repo_flag_when_none(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name: "/opt/homebrew/bin/gh")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout='{"number": 1}')

        monkeypatch.setattr(runner.subprocess, "run", fake_run)
        runner._gh_json(["pr", "list"], owner_repo=None)
        assert captured["cmd"] == ["gh", "pr", "list"]

    def test_pr_from_github_requests_the_right_repo(self, monkeypatch):
        captured = {}

        def fake_gh_json(args, *, owner_repo=None):
            captured["args"] = args
            captured["owner_repo"] = owner_repo
            return [{"number": 42}]

        monkeypatch.setattr(runner, "_gh_json", fake_gh_json)
        number = runner._pr_from_github("20260816-foo", "Vinix24/vnx-orchestration")
        assert number == 42
        assert captured["owner_repo"] == "Vinix24/vnx-orchestration"
        assert "dispatch/20260816-foo" in captured["args"]


class TestResolveGithubOwnerRepo:
    """Repo identity must come from the project registry / checkout, not cwd."""

    def test_resolves_from_registry_checkout_first(self, monkeypatch, tmp_path):
        checkout = tmp_path / "vnx-orchestration"
        checkout.mkdir()
        monkeypatch.setattr(
            vnx_paths, "project_id_from_state_dir", lambda state_dir: "vnx-dev",
        )
        monkeypatch.setattr(runner, "_project_checkout_path", lambda pid: checkout)

        def fake_origin(root):
            return "https://github.com/Vinix24/vnx-orchestration.git"

        monkeypatch.setattr(runner, "_git_remote_origin", fake_origin)
        assert (
            runner._resolve_github_owner_repo(tmp_path / "state")
            == "Vinix24/vnx-orchestration"
        )

    def test_cwd_fallback_returns_none_for_local_origin(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vnx_paths, "project_id_from_state_dir", lambda state_dir: "",
        )
        monkeypatch.setattr(runner, "_project_checkout_path", lambda pid: None)
        # A release-time temp checkout is a local-filesystem origin, never a
        # GitHub identity: the fallback must NOT fabricate an owner/repo from it.
        monkeypatch.setattr(
            runner, "_git_remote_origin", lambda root: "/var/folders/ab/cd/T/checkout",
        )
        assert runner._resolve_github_owner_repo(tmp_path / "state") is None

    def test_returns_none_when_no_remote_at_all(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            vnx_paths, "project_id_from_state_dir", lambda state_dir: "",
        )
        monkeypatch.setattr(runner, "_project_checkout_path", lambda pid: None)
        monkeypatch.setattr(runner, "_git_remote_origin", lambda root: None)
        assert runner._resolve_github_owner_repo(tmp_path / "state") is None


class TestPlistHasNoHardcodedProject:
    """The launchd template must pin NO project or store path (OI-1253)."""

    PLIST = ROOT / "scripts" / "launchd" / "com.vnx.gate-obligation-runner.plist"

    def test_template_has_no_hardcoded_project_or_store(self):
        content = self.PLIST.read_text(encoding="utf-8")
        assert "--state-dir" not in content
        assert "vnx-dev" not in content
        assert "~/.vnx-data" not in content
        assert "VNX_PROJECT_ID" in content
        assert "${VNX_PROJECT_ID}" in content
