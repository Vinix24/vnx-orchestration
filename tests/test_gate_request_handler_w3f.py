"""Tests for W3F receipt metadata fixes.

Covers:
- OI-1076: gate request payloads embed commit_sha
- OI-1128: _mark_gate_unavailable forwards dispatch_id into result records
- OI-1129: contract flows always include dispatch_id at top-level payload
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))


@pytest.fixture
def manager_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
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
    return {
        "project_root": project_root,
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


def _make_manager():
    import importlib
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


# ---------------------------------------------------------------------------
# OI-1076: commit_sha embedded in request payloads
# ---------------------------------------------------------------------------

class TestCommitShaInPayloads:
    """OI-1076: all gate request payloads must include commit_sha."""

    def _get_real_sha(self) -> str:
        """Get the real HEAD sha for assertion."""
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
            cwd=str(VNX_ROOT),
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def test_codex_request_payload_includes_commit_sha(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=2,
                branch="fix/test",
                review_stack=["codex_gate"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-sha-codex",
            )

        req_file = manager_env["requests_dir"] / "pr-2-codex_gate.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert "commit_sha" in payload, "codex_gate payload must contain commit_sha"

    def test_ci_gate_request_payload_includes_commit_sha(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=3,
                branch="fix/test",
                review_stack=["ci_gate"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-sha-ci",
            )

        req_file = manager_env["requests_dir"] / "pr-3-ci_gate.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert "commit_sha" in payload, "ci_gate payload must contain commit_sha"

    def test_claude_github_request_payload_includes_commit_sha(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=4,
                branch="fix/test",
                review_stack=["claude_github_optional"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-sha-claude-gh",
            )

        # OI-1307: claude_github_optional now routes through contract-based path
        # _contract_slug("4") = "4", so file is "4-claude_github_optional-contract.json"
        req_file = manager_env["requests_dir"] / "4-claude_github_optional-contract.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert "commit_sha" in payload, "claude_github_optional payload must contain commit_sha"

    def test_commit_sha_comes_from_github_pr_head_not_local_head(self, manager_env, monkeypatch):
        """commit_sha must come from the PR head on GitHub (``gh pr view headRefOid``),
        never from the local checkout HEAD (``git rev-parse HEAD``)."""
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()

        fake_head_oid = "e" * 40

        # OI-1307/B6: the request handler resolves the PR head through
        # get_pr_head_sha (gh pr view headRefOid). Patch the consumer binding so
        # the payload must carry THAT value — not the local HEAD. If the handler
        # were reverted to ``git rev-parse HEAD`` this test goes RED on both
        # asserts below.
        import gate_request_handler
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: fake_head_oid)

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=5,
                branch="fix/sha-verify",
                review_stack=["codex_gate"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-sha-match",
            )

        req_file = manager_env["requests_dir"] / "pr-5-codex_gate.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert payload["commit_sha"] == fake_head_oid, (
            f"commit_sha must come from the GitHub PR head, got {payload['commit_sha']!r}"
        )

        local_head = self._get_real_sha()
        if local_head:
            assert payload["commit_sha"] != local_head, (
                "commit_sha must not resolve to the local checkout HEAD"
            )

    def test_gemini_contract_payload_includes_commit_sha(self, manager_env, monkeypatch):
        """_build_gemini_contract_payload must embed commit_sha."""
        monkeypatch.chdir(manager_env["project_root"])

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            with patch("gate_request_handler.render_gemini_prompt", return_value="mock prompt"):
                payload = manager.request_gemini_with_contract(
                    contract=ReviewContract(
                        pr_id="PR-10",
                        branch="fix/test",
                        risk_class="low",
                        changed_files=["scripts/foo.py"],
                        content_hash="abc123",
                    ),
                    mode="per_pr",
                    dispatch_id="test-sha-contract",
                )

        assert "commit_sha" in payload, "gemini contract payload must contain commit_sha"

    def test_ci_gate_contract_payload_includes_commit_sha(self, manager_env, monkeypatch):
        """_build_ci_gate_contract_payload must embed commit_sha."""
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            payload = manager.request_ci_gate_with_contract(
                contract=ReviewContract(
                    pr_id="PR-11",
                    branch="fix/test",
                    risk_class="low",
                    changed_files=["scripts/foo.py"],
                    content_hash="def456",
                ),
                pr_number=11,
                mode="per_pr",
                dispatch_id="test-sha-ci-contract",
            )

        assert "commit_sha" in payload, "ci_gate contract payload must contain commit_sha"


# ---------------------------------------------------------------------------
# OI-1128: _mark_gate_unavailable forwards dispatch_id
# ---------------------------------------------------------------------------

class TestMarkGateUnavailableDispatchId:
    """OI-1128: _mark_gate_unavailable must forward dispatch_id to the result record."""

    def test_not_executable_result_includes_dispatch_id_for_codex(self, manager_env, monkeypatch):
        """codex_gate unavailable: result record must include dispatch_id."""
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setattr("review_gate_manager.shutil.which", lambda tool: None)
        manager = _make_manager()
        dispatch_id = "20260501-oi1128-test-B"

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=21,
                branch="fix/unavail-codex",
                review_stack=["codex_gate"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id=dispatch_id,
            )

        result_file = manager_env["results_dir"] / "pr-21-codex_gate.json"
        assert result_file.exists()
        result = json.loads(result_file.read_text())
        assert result.get("dispatch_id") == dispatch_id

    def test_not_executable_result_omits_dispatch_id_when_not_provided(self, manager_env, monkeypatch):
        """When dispatch_id is not provided, not_executable result omits it."""
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setattr("review_gate_manager.shutil.which", lambda tool: None)
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=22,
                branch="fix/no-dispatch-id",
                review_stack=["codex_gate"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
            )

        result_file = manager_env["results_dir"] / "pr-22-codex_gate.json"
        assert result_file.exists()
        result = json.loads(result_file.read_text())
        assert result["status"] == "not_executable"
        assert "dispatch_id" not in result, (
            "result record must not have dispatch_id when none was provided"
        )

    def test_mark_gate_unavailable_direct_call_forwards_dispatch_id(self, manager_env, monkeypatch):
        """Direct invocation of _mark_gate_unavailable writes dispatch_id to result file."""
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()
        dispatch_id = "20260501-oi1128-direct"

        payload: Dict[str, Any] = {
            "gate": "codex_gate",
            "status": "not_executable",
            "requested_at": "2026-05-01T00:00:00Z",
        }

        manager._mark_gate_unavailable(
            payload,
            gate="codex_gate",
            pr_number=30,
            pr_id="",
            dispatch_id=dispatch_id,
        )

        result_file = manager_env["results_dir"] / "pr-30-codex_gate.json"
        assert result_file.exists()
        result = json.loads(result_file.read_text())
        assert result.get("dispatch_id") == dispatch_id


# ---------------------------------------------------------------------------
# OI-1129: contract flows always include dispatch_id at top-level
# ---------------------------------------------------------------------------

class TestContractFlowDispatchIdTopLevel:
    """OI-1129: contract flows must always emit dispatch_id at the top-level of the payload."""

    def test_gemini_contract_always_has_dispatch_id_when_provided(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            with patch("gate_request_handler.render_gemini_prompt", return_value="mock"):
                payload = manager.request_gemini_with_contract(
                    contract=ReviewContract(
                        pr_id="PR-50",
                        branch="fix/test",
                        risk_class="low",
                        changed_files=["f.py"],
                        content_hash="aaa",
                    ),
                    mode="per_pr",
                    dispatch_id="contract-dispatch-50",
                )

        assert payload.get("dispatch_id") == "contract-dispatch-50", (
            "dispatch_id must be at top-level of gemini contract payload"
        )

    def test_gemini_contract_has_dispatch_id_even_when_empty(self, manager_env, monkeypatch):
        """When dispatch_id='', it must still appear at top-level (not absent)."""
        monkeypatch.chdir(manager_env["project_root"])

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            with patch("gate_request_handler.render_gemini_prompt", return_value="mock"):
                payload = manager.request_gemini_with_contract(
                    contract=ReviewContract(
                        pr_id="PR-51",
                        branch="fix/test",
                        risk_class="low",
                        changed_files=["f.py"],
                        content_hash="bbb",
                    ),
                    mode="per_pr",
                )

        assert "dispatch_id" in payload, (
            "dispatch_id must always be present at top-level in gemini contract payload"
        )
        assert payload["dispatch_id"] == "", (
            "dispatch_id must be empty string when not provided, not absent"
        )

    def test_claude_github_contract_has_dispatch_id_at_top_level(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            receipt = manager.request_claude_github_with_contract(
                contract=ReviewContract(
                    pr_id="PR-52",
                    branch="fix/test",
                    risk_class="low",
                    changed_files=["f.py"],
                    content_hash="ccc",
                ),
                mode="per_pr",
                dispatch_id="contract-dispatch-52",
            )

        # The persisted payload file is what matters for top-level dispatch_id
        # Contract slug: PR-52 → pr52 (lower, hyphens removed)
        requests_dir = manager_env["requests_dir"]
        req_file = requests_dir / "pr52-claude_github_optional-contract.json"
        assert req_file.exists(), f"Expected contract request file at {req_file}"
        payload = json.loads(req_file.read_text())
        assert payload.get("dispatch_id") == "contract-dispatch-52", (
            "dispatch_id must be at top-level of claude_github contract payload"
        )

    def test_claude_github_contract_has_dispatch_id_even_when_empty(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_claude_github_with_contract(
                contract=ReviewContract(
                    pr_id="PR-53",
                    branch="fix/test",
                    risk_class="low",
                    changed_files=["f.py"],
                    content_hash="ddd",
                ),
                mode="per_pr",
            )

        # Contract slug: PR-53 → pr53 (lower, hyphens removed)
        requests_dir = manager_env["requests_dir"]
        req_file = requests_dir / "pr53-claude_github_optional-contract.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert "dispatch_id" in payload, "dispatch_id must always be present in claude_github contract payload"
        assert payload["dispatch_id"] == ""

    def test_ci_gate_contract_has_dispatch_id_at_top_level(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            payload = manager.request_ci_gate_with_contract(
                contract=ReviewContract(
                    pr_id="PR-54",
                    branch="fix/test",
                    risk_class="low",
                    changed_files=["f.py"],
                    content_hash="eee",
                ),
                pr_number=54,
                mode="per_pr",
                dispatch_id="contract-dispatch-54",
            )

        assert payload.get("dispatch_id") == "contract-dispatch-54", (
            "dispatch_id must be at top-level of ci_gate contract payload"
        )

    def test_ci_gate_contract_has_dispatch_id_even_when_empty(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            payload = manager.request_ci_gate_with_contract(
                contract=ReviewContract(
                    pr_id="PR-55",
                    branch="fix/test",
                    risk_class="low",
                    changed_files=["f.py"],
                    content_hash="fff",
                ),
                pr_number=55,
                mode="per_pr",
            )

        assert "dispatch_id" in payload, "dispatch_id must always be present in ci_gate contract payload"
        assert payload["dispatch_id"] == ""


# ---------------------------------------------------------------------------
# reviews-zonder-uitkomst D4, step 0: does a run started outside the obligation
# runner count on the obligation record (head_runs)?
# ---------------------------------------------------------------------------

HEAD_D4 = "ab" * 20
DEGENERATE_D4 = {"gate": "codex_gate", "pr_number": 7, "status": "unavailable", "reason": "gate_execution_degenerate"}


def _d4_request(env, dispatch_id, head=HEAD_D4):
    (env["requests_dir"] / "pr-7-codex_gate.json").write_text(json.dumps({
        "gate": "codex_gate", "pr_number": 7, "status": "requested", "commit_sha": head, "dispatch_id": dispatch_id,
    }))


def _d4_obligation(env, dispatch_id="d4-step0"):
    import gate_obligations

    return gate_obligations.register_obligation(
        env["state_dir"], dispatch_id=dispatch_id, gate="codex_gate", pr_number=7, branch="fix/step0",
    )


def _d4_run(env, result=DEGENERATE_D4):
    """One execute_gate by hand, the reviewer patched to ``result``; returns the GateRunner.run mock."""
    with patch("gate_runner.GateRunner.run", return_value=dict(result)) as run:
        _make_manager().execute_gate(gate="codex_gate", pr_number=7)
    return run


class TestRequestAndExecuteCountsOnTheObligation:
    """D4a: a run counts on its obligation where it launches, execute_gate, whoever started it."""

    @pytest.fixture(autouse=True)
    def _cwd(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])

    def test_a_reviewer_run_outside_the_runner_updates_head_runs(self, manager_env):
        obligation = _d4_obligation(manager_env)
        _d4_request(manager_env, "d4-step0")
        _d4_run(manager_env)

        head_runs = json.loads(obligation.read_text()).get("head_runs") or {}
        assert head_runs.get("head_sha") == HEAD_D4
        assert head_runs.get("reviewer_runs") == 1

    def test_two_degenerate_runs_by_hand_cap_the_obligation_and_a_third_still_runs(self, manager_env):
        obligation = _d4_obligation(manager_env)
        _d4_request(manager_env, "d4-step0")
        _d4_run(manager_env)
        _d4_run(manager_env)
        record = json.loads(obligation.read_text())
        assert record["reason"] == "degenerate_retry_cap" and record["status"] == "pending"
        assert HEAD_D4[:8] in record["reason_detail"]

        assert _d4_run(manager_env).call_count == 1, "execute_gate counts, it refuses nothing at the cap"
        assert json.loads(obligation.read_text())["head_runs"]["reviewer_runs"] == 3

    def test_a_quota_refusal_does_not_count_and_a_new_head_starts_at_one(self, manager_env):
        obligation = _d4_obligation(manager_env)
        _d4_request(manager_env, "d4-step0")
        _d4_run(manager_env, {**DEGENERATE_D4, "reason": "lane_exhausted"})
        assert "head_runs" not in json.loads(obligation.read_text())
        _d4_run(manager_env)
        _d4_request(manager_env, "d4-step0", head="cd" * 20)
        _d4_run(manager_env)
        assert json.loads(obligation.read_text())["head_runs"]["reviewer_runs"] == 1

    def test_a_lane_that_rewrites_the_dispatch_id_still_counts_on_the_original(self, manager_env):
        obligation = _d4_obligation(manager_env)

        def harness_run(*, request_payload, **_ignored):
            request_payload["dispatch_id"] = "gate-glm_gate-pr7-generated"  # as _run_harness_lane_path does
            return dict(DEGENERATE_D4)

        for expected in (1, 2):
            _d4_request(manager_env, "d4-step0")
            with patch("gate_runner.GateRunner.run", side_effect=harness_run):
                _make_manager().execute_gate(gate="codex_gate", pr_number=7)
            assert json.loads(obligation.read_text()).get("head_runs", {}).get("reviewer_runs") == expected
        assert json.loads(obligation.read_text())["reason"] == "degenerate_retry_cap"

    @pytest.mark.parametrize("dispatch_id", ["", "d4-other", "../d4-step0", "/tmp/d4-step0"])
    def test_no_dispatch_id_no_obligation_or_an_unsafe_id_writes_nothing(self, manager_env, dispatch_id):
        obligation = _d4_obligation(manager_env)
        before = obligation.read_text()
        _d4_request(manager_env, dispatch_id)
        _d4_run(manager_env)
        assert obligation.read_text() == before
        assert sorted(p.name for p in obligation.parent.iterdir()) == ["d4-step0.json"]

    def test_an_unreadable_obligation_counts_nothing_and_is_logged(self, manager_env, caplog):
        obligation = _d4_obligation(manager_env)
        obligation.write_text("{not json")
        _d4_request(manager_env, "d4-step0")
        with caplog.at_level("WARNING", logger="gate_run_guard"):
            _d4_run(manager_env)
        assert obligation.read_text() == "{not json"
        assert "unreadable" in caplog.text and "not counted" in caplog.text

    def test_a_run_through_the_obligation_runner_is_counted_once(self, manager_env, monkeypatch):
        import gate_obligation_runner as runner
        import review_gate_manager as rgm

        for name in ("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "VNX_OVERRIDE_VNX_REVIEW_GATE_TAKEOVER_CHAIN"):
            monkeypatch.delenv(name, raising=False)
        obligation = _d4_obligation(manager_env, "d4-runner")
        manager = _make_manager()

        def request_and_execute(*, pr_number, dispatch_id="", **_ignored):
            _d4_request(manager_env, dispatch_id)
            manager.execute_gate(gate="codex_gate", pr_number=pr_number)
            return {"gates": [], "has_required_failure": False}

        runs = []

        def degenerate_run(self, *, gate, pr_number, **_ignored):
            runs.append(gate)
            record = {**DEGENERATE_D4, "commit_sha": HEAD_D4, "dispatch_id": "d4-runner"}
            manager._result_path(gate, pr_number).write_text(json.dumps(record))
            return record

        monkeypatch.setattr(manager, "request_and_execute", request_and_execute)
        monkeypatch.setattr(rgm, "_compute_changed_files", lambda branch: [])
        monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
        monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo, snapshot=None: None)
        monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda state_dir: "Vinix24/vnx-orchestration")
        monkeypatch.setattr(runner, "_new_github_snapshot", lambda owner_repo: None)
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: HEAD_D4)
        monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr_number, owner_repo, snapshot=None: "OPEN")
        monkeypatch.setattr(runner, "_list_open_prs", lambda owner_repo: {"prs": [], "suspect": False})
        with patch("gate_runner.GateRunner.run", degenerate_run):
            first = runner.run(manager_env["state_dir"])["outcomes"][0]
            assert json.loads(obligation.read_text())["head_runs"]["reviewer_runs"] == 1, "counted once"
            second = runner.run(manager_env["state_dir"])["outcomes"][0]

        assert runs == ["codex_gate", "codex_gate"], "both runs went through execute_gate"
        assert (first["action"], second["action"]) == ("pending", "retry_capped")
        record = json.loads(obligation.read_text())
        assert record["head_runs"]["reviewer_runs"] == 2 and record["reason"] == "degenerate_retry_cap"
