#!/usr/bin/env python3
"""Net-deletion sanity check in _request_codex (gate_request_handler).

Regression coverage: _request_codex must set required=True and surface
mass_deletion_flagged when the PR deletes >= _CODEX_MASS_DELETION_HOLD files,
so the request payload agrees with what enforce_codex_gate will decide during
execution — preventing a false-negative required=False at request time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from gate_request_handler import _CODEX_MASS_DELETION_HOLD, _count_deleted_files_in_pr


def _mock_git_deleted(deleted_files: list):
    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = "\n".join(deleted_files) + "\n" if deleted_files else ""
    return mock


def _mock_git_fail():
    mock = MagicMock()
    mock.returncode = 1
    mock.stdout = ""
    return mock


# ---------------------------------------------------------------------------
# Unit tests for _count_deleted_files_in_pr helper
# ---------------------------------------------------------------------------

class TestCountDeletedFilesInPr:

    def test_returns_count_on_success(self):
        deleted = [f"old/file_{i}.py" for i in range(10)]
        with patch("gate_request_handler.subprocess.run", return_value=_mock_git_deleted(deleted)):
            assert _count_deleted_files_in_pr() == 10

    def test_returns_zero_on_git_failure(self):
        with patch("gate_request_handler.subprocess.run", return_value=_mock_git_fail()):
            assert _count_deleted_files_in_pr() == 0

    def test_returns_zero_when_no_deletions(self):
        with patch("gate_request_handler.subprocess.run", return_value=_mock_git_deleted([])):
            assert _count_deleted_files_in_pr() == 0

    def test_fallback_to_head_minus_one(self):
        deleted = [f"file_{i}.py" for i in range(5)]
        fail = _mock_git_fail()
        success = _mock_git_deleted(deleted)
        with patch("gate_request_handler.subprocess.run", side_effect=[fail, fail, success]):
            assert _count_deleted_files_in_pr() == 5

    def test_timeout_returns_zero(self):
        with patch(
            "gate_request_handler.subprocess.run",
            side_effect=subprocess_timeout(),
        ):
            assert _count_deleted_files_in_pr() == 0


def subprocess_timeout():
    import subprocess as _sp
    return _sp.TimeoutExpired(cmd=["git"], timeout=10)


# ---------------------------------------------------------------------------
# Integration tests: _request_codex payload via ReviewGateManager fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def manager_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    headless_reports_dir = data_dir / "headless_reports"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
        headless_reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_HEADLESS_REPORTS_DIR", str(headless_reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")
    return {
        "project_root": project_root,
        "state_dir": state_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
    }


def _make_manager(manager_env):
    from review_gate_manager import ReviewGateManager
    return ReviewGateManager()


class TestRequestCodexMassDeletion:

    def test_mass_deletion_sets_required_true(self, manager_env):
        deleted = [f"old/file_{i}.py" for i in range(_CODEX_MASS_DELETION_HOLD + 5)]
        manager = _make_manager(manager_env)

        with patch("gate_request_handler._count_deleted_files_in_pr", return_value=len(deleted)):
            with patch("gate_request_handler._get_head_commit_sha", return_value="abc123"):
                payload = manager._request_codex(
                    pr_number=42,
                    branch="feat/big-cleanup",
                    risk_class="low",
                    changed_files=[],
                    mode="per_pr",
                )

        assert payload["required"] is True
        assert payload["mass_deletion_flagged"] is True
        assert payload["mass_deletion_count"] == len(deleted)

    def test_below_hold_required_stays_false(self, manager_env):
        manager = _make_manager(manager_env)

        with patch("gate_request_handler._count_deleted_files_in_pr", return_value=3):
            with patch("gate_request_handler._get_head_commit_sha", return_value="abc123"):
                payload = manager._request_codex(
                    pr_number=43,
                    branch="feat/small-cleanup",
                    risk_class="low",
                    changed_files=[],
                    mode="per_pr",
                )

        assert payload["required"] is False
        assert payload["mass_deletion_flagged"] is False
        assert payload["mass_deletion_count"] == 3

    def test_exact_threshold_sets_required_true(self, manager_env):
        manager = _make_manager(manager_env)

        with patch("gate_request_handler._count_deleted_files_in_pr", return_value=_CODEX_MASS_DELETION_HOLD):
            with patch("gate_request_handler._get_head_commit_sha", return_value="abc123"):
                payload = manager._request_codex(
                    pr_number=44,
                    branch="feat/exactly-hold",
                    risk_class="low",
                    changed_files=[],
                    mode="per_pr",
                )

        assert payload["required"] is True
        assert payload["mass_deletion_flagged"] is True

    def test_git_failure_does_not_set_required(self, manager_env):
        manager = _make_manager(manager_env)

        with patch("gate_request_handler._count_deleted_files_in_pr", return_value=0):
            with patch("gate_request_handler._get_head_commit_sha", return_value="abc123"):
                payload = manager._request_codex(
                    pr_number=45,
                    branch="feat/git-failure",
                    risk_class="low",
                    changed_files=[],
                    mode="per_pr",
                )

        assert payload["required"] is False
        assert payload["mass_deletion_flagged"] is False
        assert payload["mass_deletion_count"] == 0

    def test_governance_path_still_sets_required(self, manager_env):
        """Governance-path logic must still work independently of deletion check."""
        manager = _make_manager(manager_env)

        with patch("gate_request_handler._count_deleted_files_in_pr", return_value=0):
            with patch("gate_request_handler._get_head_commit_sha", return_value="abc123"):
                payload = manager._request_codex(
                    pr_number=46,
                    branch="feat/governance",
                    risk_class="low",
                    changed_files=["scripts/codex_final_gate.py"],
                    mode="per_pr",
                )

        assert payload["required"] is True
        assert payload["mass_deletion_flagged"] is False

    def test_final_mode_always_required(self, manager_env):
        manager = _make_manager(manager_env)

        with patch("gate_request_handler._count_deleted_files_in_pr", return_value=0):
            with patch("gate_request_handler._get_head_commit_sha", return_value="abc123"):
                payload = manager._request_codex(
                    pr_number=47,
                    branch="feat/final-mode",
                    risk_class="low",
                    changed_files=[],
                    mode="final",
                )

        assert payload["required"] is True

    def test_payload_persisted_with_deletion_fields(self, manager_env):
        """Request file written to disk must include mass_deletion_count and mass_deletion_flagged."""
        manager = _make_manager(manager_env)
        deleted_count = _CODEX_MASS_DELETION_HOLD + 3

        with patch("gate_request_handler._count_deleted_files_in_pr", return_value=deleted_count):
            with patch("gate_request_handler._get_head_commit_sha", return_value="abc123"):
                manager._request_codex(
                    pr_number=99,
                    branch="feat/persist-test",
                    risk_class="low",
                    changed_files=[],
                    mode="per_pr",
                )

        request_file = manager_env["requests_dir"] / "pr-99-codex_gate.json"
        assert request_file.exists()
        data = json.loads(request_file.read_text())
        assert data["mass_deletion_count"] == deleted_count
        assert data["mass_deletion_flagged"] is True
        assert data["required"] is True
