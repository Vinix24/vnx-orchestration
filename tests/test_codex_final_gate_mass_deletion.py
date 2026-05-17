#!/usr/bin/env python3
"""Regression tests: enforce_codex_gate and check_gate_clearance must agree on mass_file_deletion.

Codex round-1 finding: check_gate_clearance() called enforce_codex_gate() without project_root,
so a PR whose only trigger was deleting >=20 files appeared gate-required during evaluate/enforce
but gate-not-required during closure checks — false-positive clearance path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    _parse_numstat_net,
    _persist_result,
    check_gate_clearance,
    enforce_codex_gate,
    evaluate_and_record,
)
from review_contract import Deliverable, ReviewContract


def _make_contract(**overrides) -> ReviewContract:
    defaults = dict(
        pr_id="PR-999",
        pr_title="Mass deletion test PR",
        feature_title="test feature",
        branch="feat/test",
        track="A",
        risk_class="low",
        merge_policy="squash_merge",
        closure_stage="open",
        deliverables=[Deliverable(description="cleanup old files", category="infrastructure")],
        review_stack=[],
        changed_files=[],
        content_hash="abc123",
    )
    defaults.update(overrides)
    return ReviewContract(**defaults)


def _mock_git_deleted(deleted_files: list):
    """Return a mock subprocess.run result that reports these files as deleted."""
    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = "\n".join(deleted_files) + "\n" if deleted_files else ""
    return mock


class TestMassDeletionTrigger:
    """enforce_codex_gate and check_gate_clearance must agree on mass_file_deletion."""

    def test_mass_deletion_triggers_enforce(self, tmp_path):
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD + 5)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.mass_deletion_count >= DELETION_FILE_HOLD
        assert "mass_file_deletion" in result.reasons
        assert result.required is True

    def test_mass_deletion_blocks_clearance(self, tmp_path):
        """check_gate_clearance must NOT clear a PR that triggered mass_deletion."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD + 5)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            result = check_gate_clearance(contract, receipt=None, project_root=tmp_path)

        assert result["cleared"] is False
        assert result["reason"] != "codex_gate_not_required"
        assert "missing_codex_gate_receipt" in result["blockers"]

    def test_enforce_and_clearance_agree_on_mass_deletion(self, tmp_path):
        """Enforce and check_gate_clearance must agree: mass-deletion PR requires gate."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD + 5)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            enforcement = enforce_codex_gate(contract, project_root=tmp_path)
            clearance = check_gate_clearance(contract, receipt=None, project_root=tmp_path)

        assert enforcement.required is True, "enforce_codex_gate must flag mass deletion"
        assert clearance["cleared"] is False, "check_gate_clearance must not clear mass-deletion PR"

    def test_small_deletion_does_not_trigger(self, tmp_path):
        """PR deleting < DELETION_FILE_HOLD files must not trigger mass_file_deletion."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(3)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert "mass_file_deletion" not in result.reasons
        assert result.required is False

    def test_exact_threshold_triggers(self, tmp_path):
        """PR deleting exactly DELETION_FILE_HOLD files must trigger gate."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert "mass_file_deletion" in result.reasons
        assert result.required is True

    def test_clearance_with_passing_receipt_and_mass_deletion(self, tmp_path):
        """A passing receipt still blocks clearance when mass_deletion gate is triggered."""
        contract = _make_contract(content_hash="deadbeef01234567")
        receipt = CodexFinalGateReceipt(
            pr_id="PR-999",
            verdict="pass",
            required=True,
            enforcement_reasons=["mass_file_deletion"],
            findings=[],
            content_hash="deadbeef01234567",
            prompt_rendered=True,
            recorded_at="2026-05-13T00:00:00Z",
        )
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD + 5)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            result = check_gate_clearance(contract, receipt=receipt, project_root=tmp_path)

        assert result["cleared"] is True
        assert result["reason"] == "codex_gate_passed"


class TestDeletionWarnLevel:
    """WARN-level deletion (>= DELETION_FILE_WARN, < DELETION_FILE_HOLD) sets deletion_warn=True."""

    def test_warn_level_sets_flag(self, tmp_path):
        """WARN threshold sets deletion_warn without triggering gate requirement."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_WARN + 1)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.mass_deletion_warn is True
        assert "mass_file_deletion" not in result.reasons
        assert result.required is False

    def test_warn_exact_threshold(self, tmp_path):
        """Exactly DELETION_FILE_WARN deleted files sets deletion_warn."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_WARN)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.mass_deletion_warn is True
        assert result.mass_deletion_count == DELETION_FILE_WARN

    def test_below_warn_no_flag(self, tmp_path):
        """Below DELETION_FILE_WARN, deletion_warn stays False."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_WARN - 1)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.mass_deletion_warn is False
        assert result.required is False

    def test_hold_level_no_warn_flag(self, tmp_path):
        """At HOLD threshold, deletion_warn is False (mass_file_deletion takes over)."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.mass_deletion_warn is False
        assert "mass_file_deletion" in result.reasons
        assert result.required is True

    def test_warn_injects_finding_in_receipt(self, tmp_path):
        """evaluate_and_record injects a warning finding when deletion_warn is active."""
        contract = _make_contract(
            pr_id="PR-warn",
            pr_title="Warn level deletion",
            deliverables=[Deliverable(description="remove stale helpers", category="infrastructure")],
            review_stack=["codex_gate"],
            content_hash="warn1234",
        )
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_WARN + 2)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            with patch("codex_final_gate.emit_governance_receipt"):
                receipt = evaluate_and_record(contract, project_root=tmp_path)

        warning_findings = [
            f for f in receipt.findings
            if f.get("severity") == "warning" and "Net deletion warning" in f.get("message", "")
        ]
        assert len(warning_findings) == 1
        assert str(DELETION_FILE_WARN) in warning_findings[0]["message"]

    def test_no_warn_finding_below_threshold(self, tmp_path):
        """evaluate_and_record does not inject deletion warning when below WARN threshold."""
        contract = _make_contract(
            pr_id="PR-clean",
            pr_title="Clean PR",
            deliverables=[Deliverable(description="add feature", category="feature")],
            review_stack=["codex_gate"],
            content_hash="clean5678",
        )
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_WARN - 1)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            with patch("codex_final_gate.emit_governance_receipt"):
                receipt = evaluate_and_record(contract, project_root=tmp_path)

        deletion_warnings = [
            f for f in receipt.findings
            if "Net deletion" in f.get("message", "")
        ]
        assert len(deletion_warnings) == 0


class TestPersistResultAtomicWrite:
    """_persist_result must write via atomic tmp → fsync → os.replace, no partial-state."""

    def _make_receipt(self) -> CodexFinalGateReceipt:
        return CodexFinalGateReceipt(
            pr_id="PR-atomic",
            verdict="pass",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[],
            content_hash="deadbeef",
            prompt_rendered=True,
            recorded_at="2026-05-16T00:00:00Z",
        )

    def test_no_tmp_artifact_after_success(self, tmp_path):
        """After successful write, no .tmp file remains next to the receipt."""
        contract = _make_contract(pr_id="PR-atomic", content_hash="deadbeef")
        receipt = self._make_receipt()
        output_path = tmp_path / "gate_results" / "receipt.json"

        with patch("codex_final_gate.emit_governance_receipt"):
            _persist_result(receipt, output_path, contract)

        assert output_path.exists(), "receipt file must be written"
        tmp_artifact = output_path.with_suffix(output_path.suffix + ".tmp")
        assert not tmp_artifact.exists(), "no .tmp artifact should remain after success"

    def test_receipt_content_is_valid_json(self, tmp_path):
        """Written receipt must be parseable JSON matching the receipt object."""
        import json as _json
        contract = _make_contract(pr_id="PR-atomic", content_hash="deadbeef")
        receipt = self._make_receipt()
        output_path = tmp_path / "receipt.json"

        with patch("codex_final_gate.emit_governance_receipt"):
            _persist_result(receipt, output_path, contract)

        parsed = _json.loads(output_path.read_text())
        assert parsed["pr_id"] == "PR-atomic"
        assert parsed["verdict"] == "pass"

    def test_canonical_file_not_written_when_replace_fails(self, tmp_path):
        """If os.replace fails, the canonical file must not contain partial data."""
        import codex_final_gate as cgf
        contract = _make_contract(pr_id="PR-atomic", content_hash="deadbeef")
        receipt = self._make_receipt()
        output_path = tmp_path / "receipt.json"

        sentinel = OSError("simulated disk-full on replace")
        with patch("codex_final_gate.os.replace", side_effect=sentinel):
            with patch("codex_final_gate.emit_governance_receipt"):
                with pytest.raises(OSError):
                    _persist_result(receipt, output_path, contract)

        assert not output_path.exists(), "canonical file must not exist when os.replace fails"


class TestParseNumstatNet:
    """Unit tests for _parse_numstat_net helper."""

    def test_basic_net_deletion(self):
        output = "10\t50\tsome/file.py\n5\t20\tother/file.py\n"
        assert _parse_numstat_net(output) == (50 + 20) - (10 + 5)

    def test_net_addition(self):
        output = "100\t10\tsome/file.py\n"
        assert _parse_numstat_net(output) == 10 - 100

    def test_binary_files_skipped(self):
        output = "-\t-\tbinary.png\n10\t30\ttext.py\n"
        assert _parse_numstat_net(output) == 30 - 10

    def test_empty_output_returns_zero(self):
        assert _parse_numstat_net("") == 0

    def test_no_tabs_skipped(self):
        output = "just_a_filename\n10\t30\tfile.py\n"
        assert _parse_numstat_net(output) == 30 - 10


def _mock_numstat(net_removed: int):
    """Return a mock subprocess.run result reporting net_removed as a single deletion."""
    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = f"0\t{net_removed}\tsome/file.py\n"
    return mock


class TestNetLineDeletion:
    """enforce_codex_gate must detect net line deletion and flag appropriately."""

    def test_hold_level_triggers_gate(self, tmp_path):
        contract = _make_contract()
        with patch("codex_final_gate.subprocess.run", side_effect=[
            MagicMock(returncode=0, stdout=""),   # _get_deleted_files → no deleted files
            _mock_numstat(NET_LINE_DELETION_HOLD + 100),  # _get_net_line_deletion → HOLD
        ]):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.net_line_deletion >= NET_LINE_DELETION_HOLD
        assert "net_line_deletion" in result.reasons
        assert result.required is True

    def test_warn_level_sets_flag_not_required(self, tmp_path):
        contract = _make_contract()
        with patch("codex_final_gate.subprocess.run", side_effect=[
            MagicMock(returncode=0, stdout=""),
            _mock_numstat(NET_LINE_DELETION_WARN + 50),
        ]):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.net_line_deletion_warn is True
        assert "net_line_deletion" not in result.reasons
        assert result.required is False

    def test_below_warn_no_flag(self, tmp_path):
        contract = _make_contract()
        with patch("codex_final_gate.subprocess.run", side_effect=[
            MagicMock(returncode=0, stdout=""),
            _mock_numstat(NET_LINE_DELETION_WARN - 10),
        ]):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.net_line_deletion_warn is False
        assert "net_line_deletion" not in result.reasons

    def test_net_addition_ignored(self, tmp_path):
        """PR that adds more lines than it removes must not trigger net_line_deletion."""
        contract = _make_contract()
        mock_net_add = MagicMock()
        mock_net_add.returncode = 0
        mock_net_add.stdout = f"500\t10\tsome/file.py\n"  # +490 net lines
        with patch("codex_final_gate.subprocess.run", side_effect=[
            MagicMock(returncode=0, stdout=""),
            mock_net_add,
        ]):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.net_line_deletion_warn is False
        assert "net_line_deletion" not in result.reasons

    def test_warn_injects_finding_in_receipt(self, tmp_path):
        """evaluate_and_record injects a warning finding when net_line_deletion_warn is active."""
        contract = _make_contract(
            pr_id="PR-netline",
            pr_title="Big line reduction",
            deliverables=[Deliverable(description="refactor module", category="refactor")],
            review_stack=["codex_gate"],
            content_hash="netline123",
        )
        with patch("codex_final_gate.subprocess.run", side_effect=[
            MagicMock(returncode=0, stdout=""),
            _mock_numstat(NET_LINE_DELETION_WARN + 50),
        ]):
            with patch("codex_final_gate.emit_governance_receipt"):
                receipt = evaluate_and_record(contract, project_root=tmp_path)

        line_warn_findings = [
            f for f in receipt.findings
            if f.get("severity") == "warning" and "Net line deletion warning" in f.get("message", "")
        ]
        assert len(line_warn_findings) == 1
        assert str(NET_LINE_DELETION_WARN) in line_warn_findings[0]["message"]

    def test_git_failure_returns_zero_net(self, tmp_path):
        """Git failure on numstat must not crash — net_line_deletion stays 0."""
        contract = _make_contract()
        with patch("codex_final_gate.subprocess.run", side_effect=[
            MagicMock(returncode=0, stdout=""),  # _get_deleted_files succeeds
            MagicMock(returncode=1, stdout=""),  # _get_net_line_deletion origin/main fails
            MagicMock(returncode=1, stdout=""),  # _get_net_line_deletion origin/master fails
            MagicMock(returncode=1, stdout=""),  # _get_net_line_deletion HEAD~1 fails
        ]):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.net_line_deletion == 0
        assert result.net_line_deletion_warn is False


class TestNoProjectRootSkipsDeletionCheck:
    """enforce_codex_gate without project_root must skip deletion checks entirely."""

    def test_no_project_root_skips_deletion(self):
        """project_root=None → deletion count stays 0, no deletion reasons added."""
        from review_contract import Deliverable, ReviewContract
        contract = ReviewContract(
            pr_id="PR-noroot",
            pr_title="No root test",
            feature_title="test",
            branch="feat/test",
            track="A",
            risk_class="low",
            merge_policy="squash_merge",
            closure_stage="open",
            deliverables=[Deliverable(description="thing", category="feature")],
            review_stack=[],
            changed_files=[],
            content_hash="noroot01",
        )
        result = enforce_codex_gate(contract, project_root=None)
        assert result.mass_deletion_count == 0
        assert result.mass_deletion_warn is False
        assert result.net_line_deletion == 0
        assert result.net_line_deletion_warn is False
        assert "mass_file_deletion" not in result.reasons
        assert "net_line_deletion" not in result.reasons


class TestBothDeletionTriggersSimultaneously:
    """Both file-count and net-line HOLD triggers must both appear in receipt."""

    def _make_contract(self, **overrides):
        from review_contract import Deliverable, ReviewContract
        defaults = dict(
            pr_id="PR-both",
            pr_title="Both triggers",
            feature_title="test",
            branch="feat/test",
            track="A",
            risk_class="low",
            merge_policy="squash_merge",
            closure_stage="open",
            deliverables=[Deliverable(description="cleanup", category="infrastructure")],
            review_stack=[],
            changed_files=[],
            content_hash="both1234",
        )
        defaults.update(overrides)
        return ReviewContract(**defaults)

    def test_both_hold_triggers_both_reasons(self, tmp_path):
        """File HOLD + net-line HOLD both add their reason to enforcement."""
        contract = self._make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD)]
        mock_deleted = MagicMock(returncode=0, stdout="\n".join(deleted) + "\n")
        mock_numstat = MagicMock(returncode=0, stdout=f"0\t{NET_LINE_DELETION_HOLD + 100}\tsome/file.py\n")
        with patch("codex_final_gate.subprocess.run", side_effect=[mock_deleted, mock_numstat]):
            result = enforce_codex_gate(contract, project_root=tmp_path)
        assert "mass_file_deletion" in result.reasons
        assert "net_line_deletion" in result.reasons
        assert result.required is True

    def test_both_warn_findings_in_receipt(self, tmp_path):
        """evaluate_and_record injects two warning findings when both WARN levels are hit."""
        from review_contract import Deliverable
        contract = self._make_contract(
            pr_id="PR-both-warn",
            pr_title="Both WARN level",
            deliverables=[Deliverable(description="reduce module", category="refactor")],
            review_stack=["codex_gate"],
            content_hash="bothwarn1",
        )
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_WARN + 1)]
        mock_deleted = MagicMock(returncode=0, stdout="\n".join(deleted) + "\n")
        mock_numstat = MagicMock(returncode=0, stdout=f"0\t{NET_LINE_DELETION_WARN + 50}\tsome/file.py\n")
        with patch("codex_final_gate.subprocess.run", side_effect=[mock_deleted, mock_numstat]):
            with patch("codex_final_gate.emit_governance_receipt"):
                receipt = evaluate_and_record(contract, project_root=tmp_path)
        file_warn = [
            f for f in receipt.findings
            if f.get("severity") == "warning" and "Net deletion warning" in f.get("message", "")
        ]
        line_warn = [
            f for f in receipt.findings
            if f.get("severity") == "warning" and "Net line deletion warning" in f.get("message", "")
        ]
        assert len(file_warn) == 1, "expected exactly one file-deletion warning finding"
        assert len(line_warn) == 1, "expected exactly one net-line-deletion warning finding"


class TestRenderPromptIncludesDeletionAlert:
    """_cmd_render_prompt must inject Net-Deletion Alert when deleted files exist."""

    def _write_contract(self, tmp_path):
        contract = _make_contract(
            pr_id="PR-render",
            pr_title="Render test",
            deliverables=[Deliverable(description="cleanup old module", category="infrastructure")],
            review_stack=["codex_gate"],
            content_hash="render456",
        )
        path = tmp_path / "contract.json"
        path.write_text(contract.to_json(), encoding="utf-8")
        return path

    def test_deleted_files_appear_in_rendered_prompt(self, tmp_path, capsys):
        from codex_final_gate import main as cgf_main

        contract_path = self._write_contract(tmp_path)
        deleted = ["old/module.py", "old/helper.py"]
        mock_git = MagicMock()
        mock_git.returncode = 0
        mock_git.stdout = "\n".join(deleted) + "\n"

        with patch("codex_final_gate.subprocess.run", return_value=mock_git):
            exit_code = cgf_main(["render-prompt", "--contract", str(contract_path)])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Net-Deletion Alert" in captured.out
        assert "old/module.py" in captured.out

    def test_no_deleted_files_no_alert_section(self, tmp_path, capsys):
        from codex_final_gate import main as cgf_main

        contract_path = self._write_contract(tmp_path)
        mock_git = MagicMock()
        mock_git.returncode = 0
        mock_git.stdout = ""

        with patch("codex_final_gate.subprocess.run", return_value=mock_git):
            exit_code = cgf_main(["render-prompt", "--contract", str(contract_path)])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Net-Deletion Alert" not in captured.out
