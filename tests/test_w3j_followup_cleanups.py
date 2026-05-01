"""Tests for W3J follow-up cleanups.

Covers:
- OI-1307: request_reviews routes claude_github_optional through request_claude_github_with_contract,
           building a ReviewContract and writing a ClaudeGitHubReviewReceipt.
- OI-1308: SubprocessAdapter.event_store public property works; _get_event_store is a valid alias.
- OI-1309: deliver() clears stale session_id before launching a new subprocess.
- OI-1092: review_gate_manager and gate_executor use scoped paths (verify already resolved).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))


# ---------------------------------------------------------------------------
# Shared fixture — mirrors manager_env from test_gate_request_handler_w3f.py
# ---------------------------------------------------------------------------

@pytest.fixture
def manager_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    headless_dir = reports_dir / "headless"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
        headless_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
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
        "reports_dir": reports_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


def _make_manager():
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


# ---------------------------------------------------------------------------
# OI-1307 — request_reviews routes through request_claude_github_with_contract
# ---------------------------------------------------------------------------

class TestOI1307RequestReviewsContractBased:
    """OI-1307: claude_github_optional uses contract-based dispatch."""

    def test_request_reviews_builds_review_contract(self, manager_env, monkeypatch):
        """request_reviews with claude_github_optional must invoke request_claude_github_with_contract."""
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()

        captured_contracts = []
        original = manager.request_claude_github_with_contract

        def capture_contract(**kwargs):
            captured_contracts.append(kwargs.get("contract"))
            return original(**kwargs)

        with patch.object(manager, "request_claude_github_with_contract", side_effect=capture_contract):
            with patch("governance_receipts.emit_governance_receipt"):
                manager.request_reviews(
                    pr_number=10,
                    branch="fix/oi1307",
                    review_stack=["claude_github_optional"],
                    risk_class="medium",
                    changed_files=["scripts/lib/gate_request_handler.py"],
                    mode="per_pr",
                    dispatch_id="test-oi1307",
                )

        assert len(captured_contracts) == 1, "request_claude_github_with_contract must be called once"
        contract = captured_contracts[0]
        from review_contract import ReviewContract
        assert isinstance(contract, ReviewContract), "contract must be a ReviewContract instance"
        assert contract.branch == "fix/oi1307"
        assert contract.risk_class == "medium"
        assert "scripts/lib/gate_request_handler.py" in contract.changed_files

    def test_request_reviews_writes_claude_github_receipt_result(self, manager_env, monkeypatch):
        """A ClaudeGitHubReviewReceipt result record must be written to the results dir."""
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=11,
                branch="fix/oi1307",
                review_stack=["claude_github_optional"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-oi1307-receipt",
            )

        # The contract-based path writes to {pr_id}-claude_github_optional-contract.json
        # pr_id = str(pr_number) = "11", slug = "11"
        results_dir = manager_env["results_dir"]
        result_files = list(results_dir.glob("*claude_github_optional*"))
        assert result_files, f"Expected a result record in {results_dir}, found none"
        payload = json.loads(result_files[0].read_text())
        assert payload.get("gate") == "claude_github_optional"

    def test_request_reviews_returns_status_key(self, manager_env, monkeypatch):
        """Returned payload must have 'status' for backwards compat with emit_governance_receipt."""
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            result = manager.request_reviews(
                pr_number=12,
                branch="fix/oi1307",
                review_stack=["claude_github_optional"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-oi1307-status",
            )

        assert "requested" in result
        payloads = result["requested"]
        assert len(payloads) == 1
        assert "status" in payloads[0], "payload must have 'status' key for backwards compat"


# ---------------------------------------------------------------------------
# OI-1308 — SubprocessAdapter.event_store public property
# ---------------------------------------------------------------------------

class TestOI1308EventStorePublicProperty:
    """OI-1308: event_store is a public property; _get_event_store is a deprecated alias."""

    def test_event_store_property_accessible_without_leading_underscore(self):
        """adapter.event_store must be accessible (no AttributeError)."""
        from subprocess_adapter import SubprocessAdapter
        adapter = SubprocessAdapter()
        # EventStore may or may not be importable; the property must not raise
        result = adapter.event_store
        # result is None when EventStore is not available, which is acceptable
        assert result is None or hasattr(result, "clear")

    def test_get_event_store_deprecated_alias_returns_same_value(self):
        """_get_event_store() must return the same object as event_store."""
        from subprocess_adapter import SubprocessAdapter
        adapter = SubprocessAdapter()
        via_property = adapter.event_store
        via_method = adapter._get_event_store()
        assert via_property is via_method

    def test_event_store_lazy_loaded_only_once(self):
        """EventStore lazy-load runs at most once across multiple accesses."""
        from subprocess_adapter import SubprocessAdapter
        adapter = SubprocessAdapter()

        load_count = [0]
        original_flag = adapter._event_store_loaded

        def counting_import(name, *args, **kwargs):
            if name == "event_store":
                load_count[0] += 1
                raise ImportError("EventStore not available")
            raise ImportError(f"no module: {name}")

        with patch("builtins.__import__", side_effect=counting_import):
            adapter._event_store_loaded = False  # reset so lazy-load fires
            _ = adapter.event_store
            _ = adapter.event_store  # second access must not re-trigger import

        assert load_count[0] == 1, "EventStore import attempted more than once"


# ---------------------------------------------------------------------------
# OI-1309 — session_id cleared on deliver()
# ---------------------------------------------------------------------------

class TestOI1309SessionIdPopOnDeliver:
    """OI-1309: stale session_id is cleared before a new subprocess starts."""

    def _make_alive_process(self, pid: int = 99999) -> MagicMock:
        proc = MagicMock(spec=subprocess.Popen)
        proc.pid = pid
        proc.poll.return_value = None
        proc.returncode = None
        return proc

    def test_session_id_cleared_before_new_subprocess(self):
        """After dispatch A, session_id_A exists. On deliver() for dispatch B,
        get_session_id() must return None before the init event arrives."""
        from subprocess_adapter import SubprocessAdapter

        adapter = SubprocessAdapter()
        adapter._session_ids["T2"] = "session-dispatch-A"

        captured_session_ids: list = []
        mock_proc = self._make_alive_process()

        def popen_side_effect(cmd, **kwargs):
            # Capture what get_session_id returns at the moment Popen is called
            captured_session_ids.append(adapter.get_session_id("T2"))
            return mock_proc

        with patch("subprocess.Popen", side_effect=popen_side_effect):
            adapter.deliver("T2", "dispatch-B")

        assert captured_session_ids, "Popen side effect must have fired"
        assert captured_session_ids[0] is None, (
            f"get_session_id('T2') must be None at Popen time, got {captured_session_ids[0]!r}"
        )

    def test_session_id_not_carried_over_between_dispatches(self):
        """session_id from dispatch A must not be visible after deliver() for dispatch B."""
        from subprocess_adapter import SubprocessAdapter

        adapter = SubprocessAdapter()
        adapter._session_ids["T2"] = "stale-session"

        with patch("subprocess.Popen", return_value=self._make_alive_process()):
            adapter.deliver("T2", "dispatch-B")

        # Before any init event, session_id must be None
        assert adapter.get_session_id("T2") is None

    def test_session_id_still_cleared_when_no_prior_session(self):
        """deliver() on a fresh terminal_id (no prior session) must not raise."""
        from subprocess_adapter import SubprocessAdapter

        adapter = SubprocessAdapter()
        assert adapter.get_session_id("T3") is None

        with patch("subprocess.Popen", return_value=self._make_alive_process()):
            result = adapter.deliver("T3", "dispatch-fresh")

        assert result.success is True
        assert adapter.get_session_id("T3") is None


# ---------------------------------------------------------------------------
# OI-1092 — review_gate_manager uses scoped paths (verify already resolved)
# ---------------------------------------------------------------------------

class TestOI1092ScopedPaths:
    """OI-1092: verify review_gate_manager and gate_executor use scoped paths via env vars."""

    def test_review_gate_manager_uses_vnx_state_dir(self, manager_env, monkeypatch):
        """ReviewGateManager must resolve paths via VNX_STATE_DIR, not /tmp/."""
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()
        assert str(manager_env["state_dir"]) in str(manager.state_dir)

    def test_review_gate_manager_requests_dir_under_data_dir(self, manager_env, monkeypatch):
        """requests_dir and results_dir must be under VNX_DATA_DIR, not /tmp/."""
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()
        data_dir = manager_env["state_dir"].parent.parent
        assert str(data_dir) in str(manager.requests_dir)
        assert str(data_dir) in str(manager.results_dir)

    def test_no_hardcoded_tmp_in_review_gate_manager(self):
        """/tmp/ must not appear as a hardcoded path in review_gate_manager.py."""
        rgm_path = SCRIPTS_DIR / "review_gate_manager.py"
        content = rgm_path.read_text()
        assert "/tmp/" not in content, "review_gate_manager.py has hardcoded /tmp/ path"
