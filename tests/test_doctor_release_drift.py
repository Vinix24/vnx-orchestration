#!/usr/bin/env python3
"""Tests for ``release:main-drift`` and ``scripts/lib/release_drift.py``.

The check answers the fleet-release goal's first sentence: the release the
machine runs (``~/.vnx-system/current`` or the project's ``.vnx-version`` pin)
is N commits behind ``origin/main``. Drift is BLOCKING (FAIL, non-zero exit) —
a WARN would let ``vnx doctor`` exit 0 with the fleet on an old release
(tiebreaker 2026-10-25) — and it is skipped, with the reason on the line, when
there is no release or no local main ref (this check never fetches). Hermetic:
every test builds its own repo and release checkout under ``tmp_path`` and
redirects ``HOME``. ``release_drift`` is imported lazily: a module-level import
would make the red run a collection error instead of a failing assertion.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from vnx_cli.commands.doctor import FAIL, PASS, vnx_doctor

TAG = "v1.0.0"


def _drift():
    import release_drift

    return release_drift


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo(parent: Path, drift_commits: int = 0, path: str = "scripts/drift.py") -> Path:
    """A repo on ``main`` with tag ``v1.0.0`` and ``drift_commits`` after it."""
    repo = parent / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base release")
    _git(repo, "tag", TAG)
    for index in range(drift_commits):
        target = repo / (path if drift_commits == 1 else f"{path}.{index}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"VALUE = {index}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"feat: drift commit {index}")
    return repo


def _release(repo: Path, dest: Path, with_main_ref: bool = True) -> Path:
    """Clone ``repo``, detach on TAG: the shape of a materialized release dir."""
    subprocess.run(["git", "clone", "-q", str(repo), str(dest)], check=True, capture_output=True)
    _git(dest, "checkout", "-q", "--detach", TAG)
    if not with_main_ref:  # the fleet shape: tags only, no remote-tracking refs
        _git(dest, "update-ref", "-d", "refs/remotes/origin/main")
        _git(dest, "branch", "-D", "main")
    return dest


def _fake_git(drift_module, subcommand: str, *, returncode: int = 128,
              stdout: str = "", stderr: str = "fatal: bad object\n",
              raises: Exception | None = None):
    """A ``subprocess.run`` that fails one git subcommand and runs the rest.

    Patched onto ``release_drift``'s call site so the failure is a real failing
    subprocess answer, exactly what the finding confirmed against the checkout.
    """
    real_run = drift_module.subprocess.run

    def fake_run(command, *args, **kwargs):
        if subcommand in command:
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)
        return real_run(command, *args, **kwargs)

    return fake_run


class TestMeasure:
    def test_release_level_with_main_is_up_to_date(self, tmp_path):
        drift = _drift().measure(_release(_repo(tmp_path), tmp_path / "rel"))
        assert drift.status == _drift().UP_TO_DATE
        assert (drift.behind, drift.commits, drift.release) == (0, (), TAG)
        assert TAG in _drift().render_detail(drift)

    def test_one_commit_on_main_after_the_release_is_drift(self, tmp_path):
        repo = _repo(tmp_path, drift_commits=1)
        sha = _git(repo, "rev-parse", "--short=7", "HEAD")
        subject = _git(repo, "log", "-1", "--format=%s")
        drift = _drift().measure(_release(repo, tmp_path / "rel"))
        assert drift.status == _drift().BEHIND
        assert drift.behind == 1
        assert [c.sha[:7] for c in drift.commits] == [sha]
        assert drift.commits[0].subject == subject
        assert drift.commits[0].paths == ("scripts/drift.py",)
        detail = _drift().render_detail(drift)
        assert sha in detail and subject in detail and "scripts/drift.py" in detail

    def test_the_detail_shows_three_commits_and_counts_the_rest(self, tmp_path):
        drift = _drift().measure(_release(_repo(tmp_path, drift_commits=4), tmp_path / "rel"))
        detail = _drift().render_detail(drift)
        assert drift.behind == 4
        # Newest first, like git log; the tail counts what the line leaves out.
        assert drift.commits[0].paths == ("scripts/drift.py.3",)
        assert detail.count(" -> ") == 3
        assert "+1 more commit" in detail

    def test_missing_main_ref_is_skipped_never_reported_as_level(self, tmp_path):
        repo = _repo(tmp_path, drift_commits=1)
        release = _release(repo, tmp_path / "rel", with_main_ref=False)
        assert _drift().measure(release).status == _drift().NO_MAIN_REF
        assert _drift().measure(release, main_repo=tmp_path / "absent").status == _drift().NO_MAIN_REF
        detail = _drift().render_detail(_drift().measure(release))
        assert "skipped" in detail and "origin/main" in detail
        # The fleet shape: the main ref lives in the dev tree, which knows the tag.
        assert _drift().measure(release, main_repo=repo).status == _drift().BEHIND

    def test_a_release_ref_unknown_to_the_main_repo_is_not_compared(self, tmp_path):
        release = _release(_repo(tmp_path / "one"), tmp_path / "rel", with_main_ref=False)
        _git(release, "tag", "-d", TAG)
        _git(release, "update-ref", "refs/tags/v9.9.9", "HEAD")
        drift = _drift().measure(release, main_repo=_repo(tmp_path / "two"))

        assert drift.status == _drift().UNKNOWN_RELEASE
        assert "v9.9.9" in _drift().render_detail(drift)


class TestFailedMeasurement:
    """A git call that FAILS is a failed measurement — never a skip, never level.

    The finding (codex on 1ad57b8b): both refs resolved, ``rev-list`` broke, and
    the check passed because ``measure`` collapsed "the command failed" into the
    UNKNOWN_RELEASE it also uses for a legitimately absent ref. Only a documented
    absence may skip; a command that exits non-zero must block.
    """

    def test_a_failed_rev_list_is_a_failed_measurement_not_a_skip(self, tmp_path, monkeypatch):
        drift_module = _drift()
        release = _release(_repo(tmp_path, drift_commits=1), tmp_path / "rel")
        monkeypatch.setattr(drift_module.subprocess, "run", _fake_git(drift_module, "rev-list"))

        drift = drift_module.measure(release)

        assert drift.status == drift_module.MEASURE_FAILED
        detail = drift_module.render_detail(drift)
        assert "rev-list" in detail and "fatal: bad object" in detail
        assert "skipped" not in detail

    def test_a_silent_rev_list_answer_is_not_a_zero_count(self, tmp_path, monkeypatch):
        """Exit 0 with no count measured nothing; it is not "level" either."""
        drift_module = _drift()
        release = _release(_repo(tmp_path, drift_commits=1), tmp_path / "rel")
        monkeypatch.setattr(
            drift_module.subprocess, "run",
            _fake_git(drift_module, "rev-list", returncode=0, stdout="", stderr=""),
        )

        drift = drift_module.measure(release)

        assert drift.status == drift_module.MEASURE_FAILED
        assert "rev-list" in drift_module.render_detail(drift)

    def test_git_that_cannot_run_is_a_failed_measurement(self, tmp_path, monkeypatch):
        drift_module = _drift()
        release = _release(_repo(tmp_path, drift_commits=1), tmp_path / "rel")
        monkeypatch.setattr(
            drift_module.subprocess, "run",
            _fake_git(drift_module, "rev-list", raises=OSError("git vanished")),
        )

        drift = drift_module.measure(release)

        assert drift.status == drift_module.MEASURE_FAILED
        detail = drift_module.render_detail(drift)
        assert "rev-list" in detail and "git vanished" in detail


class TestDoctorCheck:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".vnx-system").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        for key in ("VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_DATA_HOME", "VNX_STATE_DIR", "VNX_PROJECT_ID"):
            monkeypatch.delenv(key, raising=False)
        project = tmp_path / "project"
        (project / ".vnx").mkdir(parents=True)
        (project / ".vnx-data" / "state").mkdir(parents=True)
        return home, project

    def _doctor(self, project: Path, capsys) -> tuple[int, dict]:
        rc = vnx_doctor(argparse.Namespace(project_dir=str(project), strict=False, json=True))
        return rc, {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}

    def test_a_clean_release_passes_and_doctor_exits_zero(self, env, tmp_path, capsys):
        home, project = env
        (home / ".vnx-system" / "current").symlink_to(_release(_repo(tmp_path), tmp_path / "rel"))

        rc, checks = self._doctor(project, capsys)

        assert checks["release:main-drift"]["status"] == PASS
        assert TAG in checks["release:main-drift"]["detail"]
        assert rc == 0

    def test_a_commit_on_main_after_the_release_blocks_doctor(self, env, tmp_path, capsys):
        home, project = env
        repo = _repo(tmp_path, drift_commits=1)
        (home / ".vnx-system" / "current").symlink_to(_release(repo, tmp_path / "rel"))
        sha = _git(repo, "rev-parse", "--short=7", "HEAD")
        subject = _git(repo, "log", "-1", "--format=%s")

        rc, checks = self._doctor(project, capsys)

        detail = checks["release:main-drift"]["detail"]
        assert checks["release:main-drift"]["status"] == FAIL
        assert sha in detail and subject in detail and "scripts/drift.py" in detail
        assert rc == 1

    def test_a_failed_measurement_blocks_doctor_naming_the_command(self, env, tmp_path, capsys, monkeypatch):
        home, project = env
        (home / ".vnx-system" / "current").symlink_to(
            _release(_repo(tmp_path, drift_commits=1), tmp_path / "rel")
        )
        drift_module = _drift()
        monkeypatch.setattr(drift_module.subprocess, "run", _fake_git(drift_module, "rev-list"))

        rc, checks = self._doctor(project, capsys)

        check = checks["release:main-drift"]
        assert check["status"] == FAIL
        assert "rev-list" in check["detail"] and "fatal: bad object" in check["detail"]
        assert not check["detail"].startswith("skipped:")
        assert rc == 1

    def test_no_main_ref_still_passes_with_a_skip_and_exits_zero(self, env, tmp_path, capsys, monkeypatch):
        home, project = env
        release = _release(_repo(tmp_path, drift_commits=1), tmp_path / "rel", with_main_ref=False)
        (home / ".vnx-system" / "current").symlink_to(release)
        monkeypatch.setattr("vnx_cli._engine.engine_root", lambda: tmp_path / "not-a-checkout")

        rc, checks = self._doctor(project, capsys)

        check = checks["release:main-drift"]
        assert check["status"] == PASS
        assert check["detail"].startswith("skipped:")
        assert "origin/main" in check["detail"]
        assert rc == 0

    @pytest.mark.parametrize("make_release, expected", [("plain", "not a git checkout"), ("none", "no release")])
    def test_a_skip_names_its_reason_and_exits_zero(self, env, tmp_path, capsys, make_release, expected):
        home, project = env
        if make_release == "plain":
            plain = tmp_path / "plain"
            plain.mkdir()
            (home / ".vnx-system" / "current").symlink_to(plain)

        rc, checks = self._doctor(project, capsys)

        assert checks["release:main-drift"]["status"] == PASS
        assert checks["release:main-drift"]["detail"].startswith("skipped:")
        assert expected in checks["release:main-drift"]["detail"]
        assert rc == 0
