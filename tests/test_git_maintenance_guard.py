"""Tests for the shared git auto-maintenance guard used by throwaway test repos.

D3-9 (track `ci-profile-a-xdist`). ``git commit`` ends with a call that spawns
a detached ``git maintenance run --auto --quiet --detach`` child unless
``maintenance.auto`` is false or ``gc.auto`` is 0; that child owns
``.git/objects/maintenance.lock`` for a few milliseconds, which made
``TemporaryDirectory.__exit__`` fail with ``OSError: [Errno 39] Directory not
empty: 'objects'`` in VNX CI run 39971696706 (reenacted by
``test_attest_d2.py::TestThrowawayRepoGitMaintenance``).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from git_maintenance_guard import (
    AUTO_MAINTENANCE_SETTINGS,
    auto_maintenance_disabled,
    disable_auto_maintenance,
    install_auto_maintenance_guard,
)


def _fresh_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=str(path), check=True, capture_output=True,
    )
    return path


def _local_get(repo: Path, key: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "config", "--local", "--get", key],
        cwd=str(repo), capture_output=True, text=True,
    )


def _set_local(repo: Path, key: str, value: str) -> None:
    subprocess.run(["git", "config", key, value], cwd=str(repo), check=True, capture_output=True)


class TestDisableAutoMaintenance:
    def test_writes_both_settings_into_the_repo_config(self, tmp_path):
        repo = _fresh_repo(tmp_path / "repo")
        assert auto_maintenance_disabled(repo) is False  # precondition: nothing set yet

        disable_auto_maintenance(repo)

        for key, expected in AUTO_MAINTENANCE_SETTINGS:
            result = _local_get(repo, key)
            assert result.returncode == 0, f"{key} was not written to the repo config"
            assert result.stdout.strip() == expected
        assert auto_maintenance_disabled(repo) is True

    def test_is_idempotent(self, tmp_path):
        repo = _fresh_repo(tmp_path / "repo")
        disable_auto_maintenance(repo)
        disable_auto_maintenance(repo)
        assert auto_maintenance_disabled(repo) is True


class TestAutoMaintenanceDisabledFailsClosed:
    """``auto_maintenance_disabled`` is only ever allowed to say True on
    positive evidence: the exact expected value for BOTH keys, read from the
    repo's own config. Missing, empty, malformed and unknown values stay
    blocking; so does a value supplied only by the process environment.
    """

    def test_fresh_repo_is_not_disabled(self, tmp_path):
        assert auto_maintenance_disabled(_fresh_repo(tmp_path / "repo")) is False

    def test_one_key_missing_stays_blocking(self, tmp_path):
        repo = _fresh_repo(tmp_path / "repo")
        _set_local(repo, "maintenance.auto", "false")
        assert auto_maintenance_disabled(repo) is False

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("gc.auto", ""),                # empty read
            ("gc.auto", "not-a-number"),    # malformed
            ("maintenance.auto", "true"),   # the opposite of the expected value
            ("gc.auto", "1"),               # another non-zero gc.auto
        ],
    )
    def test_any_other_value_stays_blocking(self, tmp_path, key, value):
        repo = _fresh_repo(tmp_path / "repo")
        disable_auto_maintenance(repo)
        _set_local(repo, key, value)
        assert auto_maintenance_disabled(repo) is False

    def test_environment_scope_is_not_repo_evidence(self, tmp_path):
        """conftest's session guard supplies both settings through the
        environment; that says nothing about this repo's own config."""
        repo = _fresh_repo(tmp_path / "repo")
        inherited = subprocess.run(
            ["git", "config", "--get", "maintenance.auto"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert inherited.returncode == 0
        assert inherited.stdout.strip() == "false"
        assert _local_get(repo, "maintenance.auto").returncode != 0
        assert auto_maintenance_disabled(repo) is False


class TestInstallAutoMaintenanceGuard:
    def test_adds_both_pairs_as_command_scope_config(self):
        env: dict[str, str] = {}
        install_auto_maintenance_guard(env)
        assert env["GIT_CONFIG_COUNT"] == "2"
        for index, (key, value) in enumerate(AUTO_MAINTENANCE_SETTINGS):
            assert env[f"GIT_CONFIG_KEY_{index}"] == key
            assert env[f"GIT_CONFIG_VALUE_{index}"] == value

    def test_appends_to_existing_command_scope_entries(self):
        env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "kept",
        }
        install_auto_maintenance_guard(env)
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("user.name", "kept")
        assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == AUTO_MAINTENANCE_SETTINGS[0]
        assert (env["GIT_CONFIG_KEY_2"], env["GIT_CONFIG_VALUE_2"]) == AUTO_MAINTENANCE_SETTINGS[1]

    def test_is_idempotent(self):
        env: dict[str, str] = {}
        install_auto_maintenance_guard(env)
        install_auto_maintenance_guard(env)
        assert env["GIT_CONFIG_COUNT"] == "2"

    def test_a_malformed_existing_count_is_refused(self):
        with pytest.raises(ValueError, match="not an integer"):
            install_auto_maintenance_guard({"GIT_CONFIG_COUNT": "two"})

    def test_a_negative_existing_count_is_refused(self):
        with pytest.raises(ValueError, match="negative"):
            install_auto_maintenance_guard({"GIT_CONFIG_COUNT": "-1"})

    def test_a_commit_under_the_guard_spawns_no_maintenance_child(self, tmp_path):
        """Behavioural proof: git traces the detached child it spawns through
        a ``child_start`` event, so its absence is positive evidence."""
        repo = _fresh_repo(tmp_path / "repo")
        trace = tmp_path / "trace.json"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_TRACE2_EVENT": str(trace),
        }
        install_auto_maintenance_guard(env)

        (repo / "f.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True, capture_output=True, env=env)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "base"],
            check=True, capture_output=True, env=env,
        )

        assert '"commit"' in trace.read_text(), "git trace was not written; the proof is void"
        assert "maintenance" not in trace.read_text()
