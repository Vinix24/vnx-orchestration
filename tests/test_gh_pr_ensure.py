"""Tests for gh_pr_ensure.py — shared find/create-PR helper.

All `gh` invocations are mocked at the subprocess.run boundary; nothing here
touches GitHub.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import gh_pr_ensure as ghe


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# find_open_pr
# ---------------------------------------------------------------------------

def test_find_open_pr_returns_number_when_open_pr_exists(tmp_path):
    payload = '[{"number": 42, "state": "OPEN"}]'
    with patch("gh_pr_ensure.subprocess.run", return_value=_completed(stdout=payload)) as mock_run:
        result = ghe.find_open_pr("dispatch/x", tmp_path)
    assert result == 42
    args = mock_run.call_args.args[0]
    assert args[:3] == ["gh", "pr", "list"]
    assert "--head" in args and "dispatch/x" in args


def test_find_open_pr_returns_none_when_no_pr(tmp_path):
    with patch("gh_pr_ensure.subprocess.run", return_value=_completed(stdout="[]")):
        assert ghe.find_open_pr("dispatch/x", tmp_path) is None


def test_find_open_pr_ignores_closed_prs(tmp_path):
    payload = '[{"number": 7, "state": "CLOSED"}, {"number": 8, "state": "MERGED"}]'
    with patch("gh_pr_ensure.subprocess.run", return_value=_completed(stdout=payload)):
        assert ghe.find_open_pr("dispatch/x", tmp_path) is None


def test_find_open_pr_none_on_gh_failure(tmp_path):
    with patch("gh_pr_ensure.subprocess.run", return_value=_completed(returncode=1, stderr="boom")):
        assert ghe.find_open_pr("dispatch/x", tmp_path) is None


def test_find_open_pr_none_on_empty_branch(tmp_path):
    with patch("gh_pr_ensure.subprocess.run") as mock_run:
        assert ghe.find_open_pr("", tmp_path) is None
    mock_run.assert_not_called()


def test_find_open_pr_none_on_exception(tmp_path):
    with patch("gh_pr_ensure.subprocess.run", side_effect=OSError("gh not found")):
        assert ghe.find_open_pr("dispatch/x", tmp_path) is None


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------

def test_create_pr_parses_number_from_url(tmp_path):
    url = "https://github.com/org/repo/pull/99\n"
    with patch("gh_pr_ensure.subprocess.run", return_value=_completed(stdout=url)) as mock_run:
        result = ghe.create_pr("dispatch/x", tmp_path, title="t", body="b")
    assert result == 99
    args = mock_run.call_args.args[0]
    assert args[:3] == ["gh", "pr", "create"]
    assert "--draft" not in args


def test_create_pr_passes_draft_flag(tmp_path):
    with patch("gh_pr_ensure.subprocess.run", return_value=_completed(stdout="https://x/pull/1")) as mock_run:
        ghe.create_pr("dispatch/x", tmp_path, title="t", body="b", draft=True)
    args = mock_run.call_args.args[0]
    assert "--draft" in args


def test_create_pr_none_on_gh_failure(tmp_path):
    with patch("gh_pr_ensure.subprocess.run", return_value=_completed(returncode=1, stderr="no access")):
        assert ghe.create_pr("dispatch/x", tmp_path, title="t", body="b") is None


def test_create_pr_none_on_unparseable_output(tmp_path):
    with patch("gh_pr_ensure.subprocess.run", return_value=_completed(stdout="not a url")):
        assert ghe.create_pr("dispatch/x", tmp_path, title="t", body="b") is None


def test_create_pr_none_on_exception(tmp_path):
    with patch("gh_pr_ensure.subprocess.run", side_effect=OSError("gh not found")):
        assert ghe.create_pr("dispatch/x", tmp_path, title="t", body="b") is None


# ---------------------------------------------------------------------------
# ensure_pr — scenarios (a) push+no-PR creates exactly one, (b) idempotent
# re-run, (c) existing-PR no-op
# ---------------------------------------------------------------------------

def test_ensure_pr_creates_when_none_exists(tmp_path):
    """(a) A branch with no open PR gets exactly one PR created."""
    with patch("gh_pr_ensure.find_open_pr", return_value=None) as mock_find:
        with patch("gh_pr_ensure.create_pr", return_value=55) as mock_create:
            result = ghe.ensure_pr("dispatch/x", tmp_path, title="t", body="b")
    assert result == {"pr_number": 55, "created": True, "reason": None}
    mock_find.assert_called_once()
    mock_create.assert_called_once()


def test_ensure_pr_is_idempotent_on_rerun(tmp_path):
    """(b) Re-running ensure_pr after a PR was created is a no-op — still one PR."""
    with patch("gh_pr_ensure.find_open_pr", return_value=None) as mock_find:
        with patch("gh_pr_ensure.create_pr", return_value=55) as mock_create:
            first = ghe.ensure_pr("dispatch/x", tmp_path, title="t", body="b")
    assert first["pr_number"] == 55
    assert first["created"] is True

    # Second call: the branch now HAS an open PR — find_open_pr returns it,
    # create_pr must never be invoked again.
    with patch("gh_pr_ensure.find_open_pr", return_value=55) as mock_find2:
        with patch("gh_pr_ensure.create_pr") as mock_create2:
            second = ghe.ensure_pr("dispatch/x", tmp_path, title="t", body="b")
    assert second == {"pr_number": 55, "created": False, "reason": None}
    mock_create2.assert_not_called()


def test_ensure_pr_noop_when_pr_already_exists(tmp_path):
    """(c) An existing-PR branch is a pure no-op — no create call at all."""
    with patch("gh_pr_ensure.find_open_pr", return_value=17) as mock_find:
        with patch("gh_pr_ensure.create_pr") as mock_create:
            result = ghe.ensure_pr("dispatch/x", tmp_path, title="t", body="b")
    assert result == {"pr_number": 17, "created": False, "reason": None}
    mock_find.assert_called_once()
    mock_create.assert_not_called()


def test_ensure_pr_reports_failure_reason_when_create_fails(tmp_path):
    """create_pr failing (and no PR appears on retry-check) surfaces a reason."""
    with patch("gh_pr_ensure.find_open_pr", return_value=None):
        with patch("gh_pr_ensure.create_pr", return_value=None):
            result = ghe.ensure_pr("dispatch/x", tmp_path, title="t", body="b")
    assert result["pr_number"] is None
    assert result["created"] is False
    assert "dispatch/x" in result["reason"]


def test_ensure_pr_recovers_from_create_race(tmp_path):
    """create_pr fails but a concurrent caller's PR is now visible — treated as success."""
    with patch("gh_pr_ensure.find_open_pr", side_effect=[None, 88]):
        with patch("gh_pr_ensure.create_pr", return_value=None):
            result = ghe.ensure_pr("dispatch/x", tmp_path, title="t", body="b")
    assert result == {"pr_number": 88, "created": False, "reason": None}
