#!/usr/bin/env python3
"""Tests for OI-1629 deel c — the reviewed-worktree cleaner as a per-project
launchd job, wired through `vnx init`.

Measured 12-09: ``~/Library/LaunchAgents/com.vnx.cleanup-reviewed-worktrees.plist``
was hard-targeted at one repo (``REPO="$HOME/Development/mission-control"``) even
though ``scripts/cleanup_reviewed_worktrees.py`` has a ``--repo-root`` flag. Every
other repo's ``.vnx-data/worktrees/`` grew unchecked while the job "succeeded"
nightly on the wrong checkout.

This repo has had the per-project launchd machinery since #1830 (OI-1509/OI-1510):
a ``REQUIRED_PER_PROJECT_FAMILIES`` registration, a ``${VNX_PROJECT_ID}``-scoped
template, install via ``_install_launchd_agent`` (destination filename derived
from the resolved Label, so two projects never overwrite each other), and a
``vnx doctor`` FAIL on a missing instance. This module follows that same form for
the ``com.vnx.cleanup-reviewed-worktrees`` family and never invents a second one.

Mirrors ``tests/test_receipt_processor_launchd_init.py``'s pattern: fake engine
root + fake home + monkeypatched ``subprocess.run``, exercising the REAL install
path (``_install_launchd_agent`` / its per-family wrapper), never a
reimplementation.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
LAUNCHD_DIR = VNX_ROOT / "scripts" / "launchd"
sys.path.insert(0, str(VNX_ROOT))
sys.path.insert(0, str(LAUNCHD_DIR))

from vnx_cli.commands import init_cmd  # noqa: E402
import launchd_project_scope as lps  # noqa: E402
from launchd_test_support import make_central_engine, recording_launchctl, register_engine  # noqa: E402

CLEANUP_WORKTREES = "com.vnx.cleanup-reviewed-worktrees"


def _fake_engine_root(tmp_path: Path) -> Path:
    """A fake engine root under tmp_path (never under .vnx-data/worktrees/,
    which this suite's own real repo path is) so the OI-1117 worktree guard
    in _install_launchd_agent does not fire and silently skip the install."""
    real_engine = init_cmd._engine.engine_root()
    fake_engine_root = tmp_path / "fake-vnx-engine"
    make_central_engine(fake_engine_root)
    (fake_engine_root / "scripts").symlink_to(real_engine / "scripts", target_is_directory=True)
    return fake_engine_root


def _patch_launchctl(monkeypatch, loaded_labels: "set[str]") -> None:
    """Fake launchctl: 'load' registers the label (read from the just-written
    plist file path, launchd-style — the label IS the destination basename),
    'list' reports whatever is currently 'loaded'."""

    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        if cmd[:2] == ["launchctl", "load"]:
            dest = Path(cmd[-1])
            loaded_labels.add(dest.stem)
            m.stdout = ""
        elif cmd[:2] == ["launchctl", "unload"]:
            dest = Path(cmd[-1])
            loaded_labels.discard(dest.stem)
            m.stdout = ""
        elif cmd == ["launchctl", "list"]:
            m.stdout = "\n".join(loaded_labels)
        else:
            m.stdout = ""
        return m

    monkeypatch.setattr(init_cmd.subprocess, "run", fake_run)


class TestCleanupWorktreesTemplateAndPerProjectInstall:
    """The dispatch's required red test: the template must carry the
    ``${VNX_PROJECT_ID}`` placeholder AND two projects installing it must land
    on different destination files (OI-1510's collision must not regress)."""

    def test_template_is_project_scoped_and_two_projects_get_separate_files(
        self, tmp_path, monkeypatch
    ):
        # (1) Static contract: the real repo template's Label carries the
        # ${VNX_PROJECT_ID} placeholder, so the family is genuinely per-project.
        result = lps.check_template_contract(LAUNCHD_DIR)
        assert result["ok"] is True, result["violations"]
        labels = {c["family"]: c["label"] for c in result["checked"]}
        assert labels[CLEANUP_WORKTREES] == f"{CLEANUP_WORKTREES}.${{VNX_PROJECT_ID}}"

        # (2) Two projects installing the family resolve to two different
        # destination files, and neither carries the other project's id.
        fake_engine_root = _fake_engine_root(tmp_path)
        monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: fake_engine_root)
        # The root is the caller's, never ambient: an exported VNX_PROJECT_ROOT
        # must not leak into the plist (OI-1629 B).
        monkeypatch.setenv("VNX_PROJECT_ROOT", str(tmp_path / "ambient-root"))
        alpha_root = tmp_path / "alpha-checkout"
        beta_root = tmp_path / "beta-checkout"

        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        _patch_launchctl(monkeypatch, set())

        assert init_cmd._install_cleanup_reviewed_worktrees_runner(
            str(fake_engine_root), project_id="project-alpha", project_root=str(alpha_root)
        )
        assert init_cmd._install_cleanup_reviewed_worktrees_runner(
            str(fake_engine_root), project_id="project-beta", project_root=str(beta_root)
        )

        la_dir = fake_home / "Library" / "LaunchAgents"
        alpha_dest = la_dir / f"{CLEANUP_WORKTREES}.project-alpha.plist"
        beta_dest = la_dir / f"{CLEANUP_WORKTREES}.project-beta.plist"

        assert alpha_dest.is_file(), "project-alpha's plist is missing — beta's install overwrote it"
        assert beta_dest.is_file(), "project-beta's plist was never written"

        alpha_content = alpha_dest.read_text(encoding="utf-8")
        beta_content = beta_dest.read_text(encoding="utf-8")
        assert "project-alpha" in alpha_content
        assert "project-beta" not in alpha_content, (
            "project-alpha's installed plist carries project-beta's id — "
            "the second install overwrote the first project's file (OI-1510)"
        )
        assert "project-beta" in beta_content
        assert "project-alpha" not in beta_content


class TestCleanupWorktreesRunsAgainstTheProject:
    """The template must run the script with ``--repo-root`` pointing at the
    PROJECT's checkout, never at the engine root (a central install shares one
    engine across many projects, and the worktrees live under each project's
    own ``.vnx-data/worktrees/``)."""

    def test_installed_plist_passes_repo_root_for_the_project(
        self, tmp_path, monkeypatch
    ):
        fake_engine_root = _fake_engine_root(tmp_path)
        monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: fake_engine_root)
        project_checkout = tmp_path / "project-checkout"
        project_checkout.mkdir()

        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        _patch_launchctl(monkeypatch, set())

        init_cmd._install_cleanup_reviewed_worktrees_runner(
            str(fake_engine_root), project_id="project-alpha", project_root=str(project_checkout)
        )

        import plistlib

        dest = fake_home / "Library" / "LaunchAgents" / f"{CLEANUP_WORKTREES}.project-alpha.plist"
        data = plistlib.loads(dest.read_bytes())
        program_args = data.get("ProgramArguments")
        # /bin/bash -c "<command>" — the command is the last array element.
        command = program_args[-1] if isinstance(program_args, list) and program_args else ""
        assert f"--repo-root {project_checkout}" in command, (
            f"installed ProgramArguments {command!r} does not pass --repo-root "
            f"for the project checkout {project_checkout}"
        )
        env = data.get("EnvironmentVariables") or {}
        assert env.get("VNX_PROJECT_ROOT") == str(project_checkout)


class TestCleanupWorktreesInstalled:
    """vnx init must install com.vnx.cleanup-reviewed-worktrees — not just
    ship the template and the registration."""

    def test_vnx_init_calls_cleanup_reviewed_worktrees_installer(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        monkeypatch.setattr(init_cmd, "_bootstrap_runtime_dbs", MagicMock(return_value=None))
        spy = MagicMock(return_value=True)
        monkeypatch.setattr(init_cmd, "_install_cleanup_reviewed_worktrees_runner", spy)

        from vnx_cli.commands.init_cmd import vnx_init

        ns = argparse.Namespace(
            project_path=None,
            project_dir=str(tmp_path),
            project_id=None,
            template="minimal",
            force=False,
            non_interactive=False,
            set_version=None,
        )
        rc = vnx_init(ns)
        assert rc == 0
        assert spy.called, "vnx init did not call _install_cleanup_reviewed_worktrees_runner"
        _, kwargs = spy.call_args
        assert kwargs.get("project_root") == str(tmp_path.resolve())
        assert kwargs.get("project_id"), (
            f"_install_cleanup_reviewed_worktrees_runner called without a project_id: "
            f"{spy.call_args}"
        )


def _read_install(home: Path, project_id: str) -> dict:
    import plistlib

    dest = home / "Library" / "LaunchAgents" / f"{CLEANUP_WORKTREES}.{project_id}.plist"
    assert dest.is_file(), f"{dest} was not written"
    return plistlib.loads(dest.read_bytes())


class TestVnxInitSweepsTheProject:
    """W1: ``vnx init`` in project P installs a sweeper aimed at P, never at the engine."""

    def test_installed_plist_targets_the_project_not_the_engine(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        engine = register_engine(home, tmp_path / "engine", "engine")
        for name in ("scripts", "templates"):
            (engine / name).symlink_to(
                init_cmd._engine.engine_root() / name, target_is_directory=True
            )
        monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: engine)
        monkeypatch.delenv("VNX_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        monkeypatch.setattr(init_cmd, "_bootstrap_runtime_dbs", MagicMock(return_value=None))
        recording_launchctl(tmp_path / "bin", monkeypatch)

        project = tmp_path / "project"
        project.mkdir()
        ns = argparse.Namespace(
            project_path=None, project_dir=str(project), project_id="alpha", template="minimal",
            force=False, non_interactive=False, set_version=None,
        )
        assert init_cmd.vnx_init(ns) == 0

        data = _read_install(home, "alpha")
        command = data["ProgramArguments"][-1]
        assert f"--repo-root {project.resolve()} " in command + " "
        assert str(engine) not in command.split("--repo-root", 1)[1]
        assert data["EnvironmentVariables"]["VNX_PROJECT_ROOT"] == str(project.resolve())

    def test_installer_without_project_root_refuses(self, tmp_path):
        import pytest

        with pytest.raises(RuntimeError, match="project checkout"):
            init_cmd._install_cleanup_reviewed_worktrees_runner(
                str(tmp_path), project_id="alpha", project_root=""
            )


RELOAD = LAUNCHD_DIR / "reload_plist.sh"


class TestReloadPlistResolvesTheRootFromTheRegistry:
    """W2: ``reload_plist.sh`` takes the checkout from the registry for the project id."""

    def _setup(self, tmp_path: Path, monkeypatch):
        home = tmp_path / "home"
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        engine = register_engine(home, tmp_path / "engine", "engine")
        (engine / "scripts").symlink_to(
            init_cmd._engine.engine_root() / "scripts", target_is_directory=True
        )
        alpha = register_engine(home, tmp_path / "alpha-checkout", "alpha")
        beta = tmp_path / "beta-checkout"
        beta.mkdir()
        (beta / ".vnx-project-id").write_text("beta\n", encoding="utf-8")
        log = recording_launchctl(tmp_path / "bin", monkeypatch)
        return home, engine, alpha, beta, log

    def _run(self, home: Path, engine: Path, project: str, extra: "dict | None" = None):
        import os
        import subprocess

        env = {
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "VNX_HOME": str(engine),
            "VNX_TEST_LAUNCHCTL_SHIM": os.environ["VNX_TEST_LAUNCHCTL_SHIM"],
            **(extra or {}),
        }
        return subprocess.run(
            ["bash", str(RELOAD), CLEANUP_WORKTREES, project],
            capture_output=True, text=True, env=env, timeout=60,
        )

    def test_root_comes_from_the_registry_not_vnx_home(self, tmp_path, monkeypatch):
        home, engine, alpha, _beta, _log = self._setup(tmp_path, monkeypatch)
        result = self._run(home, engine, "alpha")
        assert result.returncode == 0, result.stdout + result.stderr
        data = _read_install(home, "alpha")
        assert f"--repo-root {alpha.resolve()} " in data["ProgramArguments"][-1] + " "
        assert data["EnvironmentVariables"]["VNX_PROJECT_ROOT"] == str(alpha.resolve())

    def test_explicit_root_of_another_project_is_refused_and_nothing_is_written(
        self, tmp_path, monkeypatch
    ):
        home, engine, _alpha, beta, log = self._setup(tmp_path, monkeypatch)
        result = self._run(home, engine, "alpha", {"VNX_PROJECT_ROOT": str(beta)})
        assert result.returncode != 0
        assert "beta" in result.stderr and "alpha" in result.stderr
        assert list((home / "Library" / "LaunchAgents").iterdir()) == []
        assert log.read_text(encoding="utf-8") == ""

    def test_explicit_root_with_the_same_project_id_is_accepted(self, tmp_path, monkeypatch):
        home, engine, alpha, _beta, _log = self._setup(tmp_path, monkeypatch)
        result = self._run(home, engine, "alpha", {"VNX_PROJECT_ROOT": str(alpha)})
        assert result.returncode == 0, result.stdout + result.stderr

    def test_unregistered_project_names_project_and_registry_and_writes_nothing(
        self, tmp_path, monkeypatch
    ):
        home, engine, _alpha, _beta, log = self._setup(tmp_path, monkeypatch)
        result = self._run(home, engine, "gamma")
        assert result.returncode != 0
        assert "gamma" in result.stderr
        assert str(home / ".vnx" / "projects.json") in result.stderr
        assert list((home / "Library" / "LaunchAgents").iterdir()) == []
        assert log.read_text(encoding="utf-8") == ""
