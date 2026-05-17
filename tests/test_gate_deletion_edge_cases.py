#!/usr/bin/env python3
"""Edge-case coverage for net-deletion sanity checks in codex_final_gate.

Targets gaps not covered by test_codex_final_gate_mass_deletion.py:
- _get_deleted_files git fallback chain (origin/master, HEAD~1, full failure)
- _format_deleted_files_alert HOLD vs WARN label
- enforcement.warnings list contents (direct, not via clearance)
- evaluate_and_record with provided codex_verdict + deletion warnings prepended
- check_gate_clearance blockers: stale receipt, rerun_required, error findings,
  fail/pending/blocked/unknown verdicts
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from codex_final_gate import (
    DELETION_FILE_HOLD,
    DELETION_FILE_WARN,
    NET_LINE_DELETION_HOLD,
    NET_LINE_DELETION_WARN,
    CodexFinalGateReceipt,
    _format_deleted_files_alert,
    _get_deleted_files,
    check_gate_clearance,
    enforce_codex_gate,
    evaluate_and_record,
)
from review_contract import Deliverable, ReviewContract


def _make_contract(**overrides) -> ReviewContract:
    defaults = dict(
        pr_id="PR-edge",
        pr_title="Edge case test PR",
        feature_title="edge cases",
        branch="test/edge",
        track="A",
        risk_class="low",
        merge_policy="squash_merge",
        closure_stage="open",
        deliverables=[Deliverable(description="edge case work", category="infrastructure")],
        review_stack=[],
        changed_files=[],
        content_hash="edge1234",
    )
    defaults.update(overrides)
    return ReviewContract(**defaults)


def _mock_ok(stdout: str = ""):
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    return m


def _mock_fail():
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    return m


def _file_list_stdout(files: list) -> str:
    return "\n".join(files) + "\n" if files else ""


# ---------------------------------------------------------------------------
# _get_deleted_files: fallback chain
# ---------------------------------------------------------------------------

class TestGetDeletedFilesGitFallback:
    """_get_deleted_files must fall back: origin/main → origin/master → HEAD~1 → None."""

    def test_origin_main_success_no_fallback(self, tmp_path):
        expected = ["old/file_a.py", "old/file_b.py"]
        with patch("codex_final_gate.subprocess.run", return_value=_mock_ok(_file_list_stdout(expected))) as mock_run:
            result = _get_deleted_files(tmp_path)
        assert result == expected
        # Only called once: origin/main succeeded
        assert mock_run.call_count == 1

    def test_origin_main_fails_falls_back_to_origin_master(self, tmp_path):
        expected = ["legacy/module.py"]
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_fail(),                                     # origin/main fails
            _mock_ok(_file_list_stdout(expected)),            # origin/master succeeds
        ]):
            result = _get_deleted_files(tmp_path)
        assert result == expected

    def test_both_origins_fail_falls_back_to_head_minus_1(self, tmp_path):
        expected = ["src/old.py"]
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_fail(),                                     # origin/main fails
            _mock_fail(),                                     # origin/master fails
            _mock_ok(_file_list_stdout(expected)),            # HEAD~1 succeeds
        ]):
            result = _get_deleted_files(tmp_path)
        assert result == expected

    def test_all_refs_fail_returns_none(self, tmp_path):
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_fail(),  # origin/main
            _mock_fail(),  # origin/master
            _mock_fail(),  # HEAD~1
        ]):
            result = _get_deleted_files(tmp_path)
        assert result is None

    def test_timeout_on_first_ref_falls_back(self, tmp_path):
        expected = ["utils/old.py"]
        with patch("codex_final_gate.subprocess.run", side_effect=[
            subprocess.TimeoutExpired(cmd=[], timeout=10),    # origin/main times out
            _mock_ok(_file_list_stdout(expected)),            # origin/master succeeds
        ]):
            result = _get_deleted_files(tmp_path)
        assert result == expected

    def test_all_timeout_returns_none(self, tmp_path):
        timeout_exc = subprocess.TimeoutExpired(cmd=[], timeout=10)
        with patch("codex_final_gate.subprocess.run", side_effect=[
            timeout_exc,  # origin/main
            timeout_exc,  # origin/master
            timeout_exc,  # HEAD~1
        ]):
            result = _get_deleted_files(tmp_path)
        assert result is None

    def test_empty_stdout_origin_main_returns_empty_list(self, tmp_path):
        with patch("codex_final_gate.subprocess.run", return_value=_mock_ok("")):
            result = _get_deleted_files(tmp_path)
        assert result == []

    def test_whitespace_only_lines_filtered(self, tmp_path):
        stdout = "real/file.py\n  \n\t\nother/file.py\n"
        with patch("codex_final_gate.subprocess.run", return_value=_mock_ok(stdout)):
            result = _get_deleted_files(tmp_path)
        assert result == ["real/file.py", "other/file.py"]


# ---------------------------------------------------------------------------
# enforce_codex_gate: git failure on deleted files → graceful, no crash
# ---------------------------------------------------------------------------

class TestDeletedFilesGitFailureGraceful:
    """enforce_codex_gate must not crash when git fails to report deleted files."""

    def test_git_failure_on_deleted_files_no_crash(self, tmp_path):
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_fail(),  # _get_deleted_files: origin/main
            _mock_fail(),  # _get_deleted_files: origin/master
            _mock_fail(),  # _get_deleted_files: HEAD~1
            _mock_fail(),  # _get_net_line_deletion: origin/main
            _mock_fail(),  # _get_net_line_deletion: origin/master
            _mock_fail(),  # _get_net_line_deletion: HEAD~1
        ]):
            result = enforce_codex_gate(_make_contract(), project_root=tmp_path)

        assert result.mass_deletion_count == 0
        assert result.mass_deletion_warn is False
        assert result.net_line_deletion == 0
        assert result.net_line_deletion_warn is False
        assert "mass_file_deletion" not in result.reasons
        assert "net_line_deletion" not in result.reasons
        assert result.required is False

    def test_deleted_files_none_does_not_affect_reasons(self, tmp_path):
        """When _get_deleted_files returns None, count stays 0 and no deletion flags set."""
        with patch("codex_final_gate._get_deleted_files", return_value=None):
            with patch("codex_final_gate._get_net_line_deletion", return_value=None):
                result = enforce_codex_gate(_make_contract(), project_root=tmp_path)

        assert result.mass_deletion_count == 0
        assert result.deleted_files == []

    def test_deleted_files_none_with_existing_reasons_preserved(self, tmp_path):
        """When git fails, other enforcement reasons (risk_class) are not affected."""
        contract = _make_contract(risk_class="high")
        with patch("codex_final_gate._get_deleted_files", return_value=None):
            with patch("codex_final_gate._get_net_line_deletion", return_value=None):
                result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.required is True
        assert "risk_class_high" in result.reasons
        assert result.mass_deletion_count == 0


# ---------------------------------------------------------------------------
# _format_deleted_files_alert: HOLD vs WARN label
# ---------------------------------------------------------------------------

class TestFormatDeletedFilesAlertLabels:
    """_format_deleted_files_alert must show [HOLD] at HOLD threshold and [WARN] below."""

    def test_hold_level_shows_hold_label(self):
        files = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD)]
        output = "\n".join(_format_deleted_files_alert(files))
        assert "[HOLD]" in output
        assert "[WARN]" not in output

    def test_above_hold_level_shows_hold_label(self):
        files = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD + 10)]
        output = "\n".join(_format_deleted_files_alert(files))
        assert "[HOLD]" in output

    def test_warn_level_shows_warn_label(self):
        files = [f"old/file_{i}.py" for i in range(DELETION_FILE_WARN)]
        output = "\n".join(_format_deleted_files_alert(files))
        assert "[WARN]" in output
        assert "[HOLD]" not in output

    def test_below_warn_shows_warn_label(self):
        files = ["one_file.py"]
        output = "\n".join(_format_deleted_files_alert(files))
        assert "[WARN]" in output

    def test_file_count_in_header(self):
        files = ["a.py", "b.py", "c.py"]
        output = "\n".join(_format_deleted_files_alert(files))
        assert "3 file(s) deleted" in output

    def test_all_files_listed(self):
        files = ["src/alpha.py", "src/beta.py"]
        lines = _format_deleted_files_alert(files)
        full = "\n".join(lines)
        for f in files:
            assert f in full


# ---------------------------------------------------------------------------
# enforcement.warnings: direct field test
# ---------------------------------------------------------------------------

class TestEnforcementWarningsList:
    """enforce_codex_gate.warnings must contain the correct string tokens."""

    def test_file_warn_populates_warnings_list(self, tmp_path):
        deleted = [f"f_{i}.py" for i in range(DELETION_FILE_WARN)]
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_ok(_file_list_stdout(deleted)),
            _mock_ok("0\t0\tfile.py\n"),
        ]):
            result = enforce_codex_gate(_make_contract(), project_root=tmp_path)

        assert "mass_deletion_warning" in result.warnings
        assert "net_line_deletion_warning" not in result.warnings

    def test_net_line_warn_populates_warnings_list(self, tmp_path):
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_ok(""),                                              # no deleted files
            _mock_ok(f"0\t{NET_LINE_DELETION_WARN + 50}\tfile.py\n"),
        ]):
            result = enforce_codex_gate(_make_contract(), project_root=tmp_path)

        assert "net_line_deletion_warning" in result.warnings
        assert "mass_deletion_warning" not in result.warnings

    def test_both_warns_populate_warnings_list(self, tmp_path):
        deleted = [f"f_{i}.py" for i in range(DELETION_FILE_WARN + 1)]
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_ok(_file_list_stdout(deleted)),
            _mock_ok(f"0\t{NET_LINE_DELETION_WARN + 50}\tfile.py\n"),
        ]):
            result = enforce_codex_gate(_make_contract(), project_root=tmp_path)

        assert "mass_deletion_warning" in result.warnings
        assert "net_line_deletion_warning" in result.warnings

    def test_no_warn_empty_warnings_list(self, tmp_path):
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_ok(""),
            _mock_ok("0\t10\tfile.py\n"),
        ]):
            result = enforce_codex_gate(_make_contract(), project_root=tmp_path)

        assert result.warnings == []

    def test_hold_level_no_warning_in_warnings_list(self, tmp_path):
        """At HOLD threshold, mass_deletion_warning is NOT in warnings (HOLD adds to reasons)."""
        deleted = [f"f_{i}.py" for i in range(DELETION_FILE_HOLD)]
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_ok(_file_list_stdout(deleted)),
            _mock_ok("0\t0\tfile.py\n"),
        ]):
            result = enforce_codex_gate(_make_contract(), project_root=tmp_path)

        assert "mass_deletion_warning" not in result.warnings
        assert "mass_file_deletion" in result.reasons


# ---------------------------------------------------------------------------
# evaluate_and_record: provided codex_verdict + deletion warnings prepended
# ---------------------------------------------------------------------------

class TestEvaluateAndRecordWithVerdict:
    """evaluate_and_record must prepend deletion warnings to provided codex_verdict findings."""

    def _make_eval_contract(self, **overrides) -> ReviewContract:
        return _make_contract(
            pr_id="PR-eval",
            pr_title="Evaluate test",
            deliverables=[Deliverable(description="test eval", category="feature")],
            review_stack=["codex_gate"],
            content_hash="eval1234",
            **overrides,
        )

    def test_file_warn_prepended_to_existing_findings(self, tmp_path):
        contract = self._make_eval_contract()
        deleted = [f"f_{i}.py" for i in range(DELETION_FILE_WARN + 1)]
        codex_verdict = {
            "verdict": "pass",
            "findings": [{"severity": "info", "message": "looks good"}],
            "residual_risk": None,
            "rerun_required": False,
            "rerun_reason": None,
        }
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_ok(_file_list_stdout(deleted)),
            _mock_ok("0\t0\tfile.py\n"),
        ]):
            with patch("codex_final_gate.emit_governance_receipt"):
                receipt = evaluate_and_record(contract, codex_verdict=codex_verdict, project_root=tmp_path)

        deletion_warnings = [
            f for f in receipt.findings
            if f.get("severity") == "warning" and "Net deletion warning" in f.get("message", "")
        ]
        assert len(deletion_warnings) == 1
        assert receipt.findings[0] == deletion_warnings[0], "deletion warning must be first finding"
        assert receipt.findings[-1]["message"] == "looks good"

    def test_net_line_warn_prepended_to_verdict_findings(self, tmp_path):
        contract = self._make_eval_contract()
        codex_verdict = {
            "verdict": "pass",
            "findings": [{"severity": "warning", "message": "minor style issue"}],
            "residual_risk": None,
            "rerun_required": False,
            "rerun_reason": None,
        }
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_ok(""),
            _mock_ok(f"0\t{NET_LINE_DELETION_WARN + 50}\tfile.py\n"),
        ]):
            with patch("codex_final_gate.emit_governance_receipt"):
                receipt = evaluate_and_record(contract, codex_verdict=codex_verdict, project_root=tmp_path)

        net_warnings = [
            f for f in receipt.findings
            if "Net line deletion warning" in f.get("message", "")
        ]
        assert len(net_warnings) == 1

    def test_no_warn_verdict_findings_unchanged(self, tmp_path):
        contract = self._make_eval_contract()
        codex_verdict = {
            "verdict": "pass",
            "findings": [{"severity": "info", "message": "all good"}],
            "residual_risk": None,
            "rerun_required": False,
            "rerun_reason": None,
        }
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_ok(""),
            _mock_ok("50\t10\tfile.py\n"),  # net addition, not deletion
        ]):
            with patch("codex_final_gate.emit_governance_receipt"):
                receipt = evaluate_and_record(contract, codex_verdict=codex_verdict, project_root=tmp_path)

        assert len(receipt.findings) == 1
        assert receipt.findings[0]["message"] == "all good"

    def test_pending_verdict_when_no_codex_verdict_provided(self, tmp_path):
        contract = self._make_eval_contract()
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_ok(""),
            _mock_ok("0\t0\tfile.py\n"),
        ]):
            with patch("codex_final_gate.emit_governance_receipt"):
                receipt = evaluate_and_record(contract, project_root=tmp_path)

        assert receipt.verdict == "pending"

    def test_both_warns_prepended_before_verdict_findings(self, tmp_path):
        contract = self._make_eval_contract()
        deleted = [f"f_{i}.py" for i in range(DELETION_FILE_WARN + 1)]
        codex_verdict = {
            "verdict": "pass",
            "findings": [{"severity": "info", "message": "original finding"}],
            "residual_risk": None,
            "rerun_required": False,
            "rerun_reason": None,
        }
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_ok(_file_list_stdout(deleted)),
            _mock_ok(f"0\t{NET_LINE_DELETION_WARN + 50}\tfile.py\n"),
        ]):
            with patch("codex_final_gate.emit_governance_receipt"):
                receipt = evaluate_and_record(contract, codex_verdict=codex_verdict, project_root=tmp_path)

        # Both deletion warnings should be prepended; original finding should be last
        assert len(receipt.findings) == 3
        messages = [f["message"] for f in receipt.findings]
        assert messages[-1] == "original finding"
        assert any("Net deletion warning" in m for m in messages[:2])
        assert any("Net line deletion warning" in m for m in messages[:2])


# ---------------------------------------------------------------------------
# check_gate_clearance: blockers in combination with deletion
# ---------------------------------------------------------------------------

class TestClearanceBlockersWithDeletion:
    """Clearance blockers must be set correctly regardless of deletion state."""

    def _make_required_contract(self, **overrides) -> ReviewContract:
        return _make_contract(risk_class="high", content_hash="hash001", **overrides)

    def _make_pass_receipt(self, content_hash: str = "hash001") -> CodexFinalGateReceipt:
        return CodexFinalGateReceipt(
            pr_id="PR-edge",
            verdict="pass",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[],
            content_hash=content_hash,
            prompt_rendered=True,
            recorded_at="2026-05-18T00:00:00Z",
        )

    def _mock_clean_git(self):
        return [_mock_ok(""), _mock_ok("0\t0\tfile.py\n")]

    def test_stale_receipt_blocks_clearance(self, tmp_path):
        contract = self._make_required_contract()
        receipt = self._make_pass_receipt(content_hash="STALE_HASH")
        with patch("codex_final_gate.subprocess.run", side_effect=self._mock_clean_git()):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is False
        assert "codex_gate_stale_receipt" in result["blockers"]

    def test_rerun_required_blocks_clearance(self, tmp_path):
        contract = self._make_required_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-edge",
            verdict="pass",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[],
            content_hash="hash001",
            prompt_rendered=True,
            recorded_at="2026-05-18T00:00:00Z",
            rerun_required=True,
            rerun_reason="test_flake",
        )
        with patch("codex_final_gate.subprocess.run", side_effect=self._mock_clean_git()):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is False
        assert "codex_gate_rerun_required" in result["blockers"]

    def test_error_findings_block_clearance(self, tmp_path):
        contract = self._make_required_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-edge",
            verdict="pass",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[{"severity": "error", "message": "data loss risk"}],
            content_hash="hash001",
            prompt_rendered=True,
            recorded_at="2026-05-18T00:00:00Z",
        )
        with patch("codex_final_gate.subprocess.run", side_effect=self._mock_clean_git()):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is False
        assert any("codex_gate_unresolved_errors" in b for b in result["blockers"])

    def test_blocker_severity_finding_blocks_clearance(self, tmp_path):
        contract = self._make_required_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-edge",
            verdict="pass",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[{"severity": "blocker", "message": "security breach"}],
            content_hash="hash001",
            prompt_rendered=True,
            recorded_at="2026-05-18T00:00:00Z",
        )
        with patch("codex_final_gate.subprocess.run", side_effect=self._mock_clean_git()):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is False
        assert any("codex_gate_unresolved_errors" in b for b in result["blockers"])

    def test_pending_verdict_blocks_clearance(self, tmp_path):
        contract = self._make_required_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-edge",
            verdict="pending",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[],
            content_hash="hash001",
            prompt_rendered=True,
            recorded_at="2026-05-18T00:00:00Z",
        )
        with patch("codex_final_gate.subprocess.run", side_effect=self._mock_clean_git()):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is False
        assert "codex_gate_pending" in result["blockers"]

    def test_fail_verdict_blocks_clearance(self, tmp_path):
        contract = self._make_required_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-edge",
            verdict="fail",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[],
            content_hash="hash001",
            prompt_rendered=True,
            recorded_at="2026-05-18T00:00:00Z",
        )
        with patch("codex_final_gate.subprocess.run", side_effect=self._mock_clean_git()):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is False
        assert "codex_gate_failed" in result["blockers"]

    def test_blocked_verdict_blocks_clearance(self, tmp_path):
        contract = self._make_required_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-edge",
            verdict="blocked",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[],
            content_hash="hash001",
            prompt_rendered=True,
            recorded_at="2026-05-18T00:00:00Z",
        )
        with patch("codex_final_gate.subprocess.run", side_effect=self._mock_clean_git()):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is False
        assert "codex_gate_blocked" in result["blockers"]

    def test_unknown_verdict_blocks_clearance(self, tmp_path):
        contract = self._make_required_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-edge",
            verdict="xyzzy",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[],
            content_hash="hash001",
            prompt_rendered=True,
            recorded_at="2026-05-18T00:00:00Z",
        )
        with patch("codex_final_gate.subprocess.run", side_effect=self._mock_clean_git()):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is False
        assert any("codex_gate_unknown_verdict" in b for b in result["blockers"])

    def test_empty_content_hash_in_receipt_blocks(self, tmp_path):
        contract = self._make_required_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-edge",
            verdict="pass",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[],
            content_hash="",
            prompt_rendered=True,
            recorded_at="2026-05-18T00:00:00Z",
        )
        with patch("codex_final_gate.subprocess.run", side_effect=self._mock_clean_git()):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is False
        assert "codex_gate_stale_receipt" in result["blockers"]

    def test_mass_deletion_gate_with_stale_receipt(self, tmp_path):
        """Mass-deletion-triggered gate AND stale receipt: both result in blocked clearance."""
        contract = _make_contract(content_hash="current_hash")
        deleted = [f"f_{i}.py" for i in range(DELETION_FILE_HOLD + 5)]
        receipt = self._make_pass_receipt(content_hash="stale_hash")
        with patch("codex_final_gate.subprocess.run", side_effect=[
            _mock_ok(_file_list_stdout(deleted)),
            _mock_ok("0\t0\tfile.py\n"),
        ]):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)
        assert result["cleared"] is False
        assert "codex_gate_stale_receipt" in result["blockers"]
