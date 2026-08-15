#!/usr/bin/env python3
"""Tests for the fail-closed VNX CI-run check (OI-1216).

Covers the four required states from the dispatch, each with a stubbed ``gh``
outcome (no network):
  (a) successful run for the exact head SHA            -> GO
  (b) zero runs                                        -> NO-GO
  (c) run with conclusion=failure for the exact head   -> NO-GO
  (d) success but for a DIFFERENT sha on the branch    -> NO-GO

plus the fail-closed edges the dispatch names: gh missing, gh not
authenticated, and a still-running (in_progress) run.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import merge_preflight_ci_check as mpci
from merge_preflight_ci_check import (
    check_ci_run_for_head,
    DEFAULT_CI_WORKFLOW_NAME,
    CI_WORKFLOW_NAME_ENV_VAR,
)


def _git_head_mock(sha: str) -> MagicMock:
    return MagicMock(returncode=0, stdout=sha + "\n", stderr="")


def _git_branch_mock(branch: str = "feature-branch") -> MagicMock:
    return MagicMock(returncode=0, stdout=branch + "\n", stderr="")


def _gh_auth_ok() -> MagicMock:
    return MagicMock(returncode=0, stdout="", stderr="")


def _gh_auth_fail(stderr: str = "You are not logged into any GitHub hosts.") -> MagicMock:
    return MagicMock(returncode=1, stdout="", stderr=stderr)


def _gh_run_output(conclusion, head_sha, status="completed", database_id=12345):
    runs = [{
        "conclusion": conclusion,
        "headSha": head_sha,
        "status": status,
        "databaseId": database_id,
    }]
    return MagicMock(returncode=0, stdout=json.dumps(runs), stderr="")


def _gh_empty_output():
    return MagicMock(returncode=0, stdout=json.dumps([]), stderr="")


def _subprocess_mocks(head_sha, branch="feature-branch"):
    """git head -> git branch -> gh auth (ok) -> gh run list."""
    return [
        _git_head_mock(head_sha),
        _git_branch_mock(branch),
        _gh_auth_ok(),
    ]


GH_PRESENT = "/usr/local/bin/gh"


class TestCheckCIRunForHead:
    def test_ci_succeeded_for_exact_head(self, tmp_path):
        """(a) successful run for the exact head SHA -> GO."""
        sha = "a" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "GO"
        assert result["ci_conclusion"] == "success"
        assert result["ran_on_sha"] is True
        assert result["head_sha"] == sha
        assert "geslaagd" in result["message"]

    def test_zero_runs_is_no_go(self, tmp_path):
        """(b) zero runs -> NO-GO with a message that names the consequence."""
        sha = "b" * 40
        mocks = _subprocess_mocks(sha) + [_gh_empty_output()]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert result["ran_on_sha"] is False
        assert "Geen VNX CI-run gevonden" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_failure_conclusion_is_no_go(self, tmp_path):
        """(c) run with conclusion=failure for the exact head -> NO-GO."""
        sha = "c" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("failure", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert result["ci_conclusion"] == "failure"
        assert result["ran_on_sha"] is True
        assert "conclusion is 'failure'" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_success_on_different_sha_is_no_go(self, tmp_path):
        """(d) success but for a DIFFERENT sha on the branch -> NO-GO."""
        sha = "d" * 40
        other = "e" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", other, database_id=99999)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert result["ran_on_sha"] is False
        assert result["head_sha"] == sha
        assert "heeft niet gedraaid op HEAD" in result["message"]
        assert "oudere commit telt niet" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_in_progress_run_is_no_go(self, tmp_path):
        """A still-running (in_progress) run for the exact head is NO-GO, not GO."""
        sha = "f" * 40
        mocks = _subprocess_mocks(sha) + [
            _gh_run_output(None, sha, status="in_progress")
        ]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert result["ran_on_sha"] is True
        assert "draait nog" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_gh_not_available_is_no_go(self, tmp_path):
        """gh missing -> NO-GO with a distinct message, never a silent GO."""
        with patch("merge_preflight_ci_check.shutil.which", return_value=None):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "gh CLI niet beschikbaar" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_gh_not_authenticated_is_no_go(self, tmp_path):
        """gh present but auth status fails -> NO-GO with a distinct message."""
        sha = "g" * 40
        mocks = [
            _git_head_mock(sha),
            _git_branch_mock(),
            _gh_auth_fail(),
        ]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "niet geauthenticeerd" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_gh_run_list_failure_is_no_go(self, tmp_path):
        """gh run list non-zero exit -> NO-GO, never GO."""
        sha = "h" * 40
        run_fail = MagicMock(returncode=1, stdout="", stderr="HTTP 403")
        mocks = _subprocess_mocks(sha) + [run_fail]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "gh run list faalde" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_gh_output_unparseable_is_no_go(self, tmp_path):
        """gh emits non-JSON -> NO-GO, never a silent pass."""
        sha = "i" * 40
        bad_json = MagicMock(returncode=0, stdout="not json", stderr="")
        mocks = _subprocess_mocks(sha) + [bad_json]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "niet te parsen" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_git_head_unresolvable_is_no_go(self, tmp_path):
        """git rev-parse HEAD fails -> NO-GO, never a silent pass."""
        git_fail = MagicMock(returncode=128, stdout="", stderr="fatal: not a git repository")
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=[git_fail]), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "HEAD-SHA kon niet worden bepaald" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_uses_default_workflow_name(self, tmp_path):
        """The gh run list call uses the default workflow name 'VNX CI'."""
        sha = "j" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks) as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "GO"
        gh_call_args = mock_run.call_args_list[3].args[0]
        workflow_idx = gh_call_args.index("--workflow")
        assert gh_call_args[workflow_idx + 1] == DEFAULT_CI_WORKFLOW_NAME

    def test_explicit_workflow_name_wins(self, tmp_path):
        """An explicit workflow_name argument is used verbatim."""
        sha = "k" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks) as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path, workflow_name="CI/CD Pipeline")
        assert result["verdict"] == "GO"
        gh_call_args = mock_run.call_args_list[3].args[0]
        workflow_idx = gh_call_args.index("--workflow")
        assert gh_call_args[workflow_idx + 1] == "CI/CD Pipeline"

    def test_env_workflow_name_override(self, tmp_path, monkeypatch):
        """VNX_CI_WORKFLOW_NAME is used when no explicit argument is given."""
        monkeypatch.setenv(CI_WORKFLOW_NAME_ENV_VAR, "CI/CD Pipeline")
        sha = "l" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks) as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "GO"
        gh_call_args = mock_run.call_args_list[3].args[0]
        workflow_idx = gh_call_args.index("--workflow")
        assert gh_call_args[workflow_idx + 1] == "CI/CD Pipeline"


class TestCLI:
    def test_cli_json_go_exits_zero(self, tmp_path, capsys):
        sha = "m" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            rc = mpci.main(["--project-root", str(tmp_path), "--json"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "GO"

    def test_cli_json_no_go_exits_nonzero(self, tmp_path, capsys):
        sha = "n" * 40
        mocks = _subprocess_mocks(sha) + [_gh_empty_output()]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            rc = mpci.main(["--project-root", str(tmp_path), "--json"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "NO-GO"
        assert "niet toetsbaar" in out["message"]
