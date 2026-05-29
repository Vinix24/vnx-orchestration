#!/usr/bin/env python3
"""Net-deletion sanity check — focused integration tests for the codex gate.

Verifies the end-to-end stated behavior: PRs deleting >N files are flagged
via warning findings, WARN/HOLD thresholds route to the right outcome, and
the review instructions contain the net-deletion sanity check instruction.

Complements the component-level tests in test_codex_final_gate.py and the
threshold regression tests in test_codex_final_gate_mass_deletion.py.
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
    _apply_mass_deletion_warning,
    _format_deleted_files_alert,
    _format_review_instructions,
    check_gate_clearance,
    enforce_codex_gate,
    evaluate_and_record,
    render_codex_prompt,
)
from review_contract import Deliverable, ReviewContract


def _contract(**overrides) -> ReviewContract:
    defaults = dict(
        pr_id="PR-sanity",
        pr_title="Deletion sanity check PR",
        feature_title="sanity",
        branch="feat/sanity",
        track="A",
        risk_class="low",
        merge_policy="squash_merge",
        closure_stage="open",
        deliverables=[Deliverable(description="remove stale modules", category="infrastructure")],
        review_stack=["codex_gate"],
        changed_files=[],
        content_hash="sanity01",
    )
    defaults.update(overrides)
    return ReviewContract(**defaults)


def _mock_git(deleted: list[str], numstat_net: int = 0) -> list:
    """Return subprocess.run side_effect for (get_deleted_files, get_net_line_deletion)."""
    deleted_mock = MagicMock()
    deleted_mock.returncode = 0
    deleted_mock.stdout = "\n".join(deleted) + "\n" if deleted else ""

    numstat_mock = MagicMock()
    numstat_mock.returncode = 0
    numstat_mock.stdout = f"0\t{numstat_net}\tsome/file.py\n" if numstat_net > 0 else "100\t0\tsome/file.py\n"

    return [deleted_mock, numstat_mock]


class TestReviewInstructionsContainNetDeletionSanity:
    """The rendered prompt must include the net-deletion sanity check instruction."""

    def test_instruction_present_in_rendered_prompt(self):
        contract = _contract(
            pr_id="PR-check",
            pr_title="Check instructions",
            deliverables=[Deliverable(description="cleanup", category="infrastructure")],
            review_stack=["codex_gate"],
        )
        prompt = render_codex_prompt(contract)
        assert "Net deletion sanity" in prompt

    def test_instruction_mentions_five_file_threshold(self):
        contract = _contract(
            pr_id="PR-check",
            pr_title="Check threshold",
            deliverables=[Deliverable(description="cleanup", category="infrastructure")],
            review_stack=["codex_gate"],
        )
        prompt = render_codex_prompt(contract)
        assert "≥5" in prompt or ">= 5" in prompt or "5 files" in prompt

    def test_format_review_instructions_contains_net_deletion_sanity(self):
        lines = _format_review_instructions()
        text = "\n".join(lines)
        assert "Net deletion sanity" in text

    def test_instruction_warns_about_accidental_scope_reduction(self):
        lines = _format_review_instructions()
        text = "\n".join(lines)
        assert "accidental" in text.lower() or "intentional" in text.lower()


class TestNetDeletionFlaggingAboveWarnThreshold:
    """PRs deleting >= DELETION_FILE_WARN files must be flagged with a warning."""

    def test_warn_threshold_sets_mass_deletion_warn(self, tmp_path):
        contract = _contract()
        deleted = [f"src/old_{i}.py" for i in range(DELETION_FILE_WARN)]

        with patch("codex_final_gate.subprocess.run", side_effect=_mock_git(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.mass_deletion_warn is True
        assert result.mass_deletion_count == DELETION_FILE_WARN
        assert result.required is False, "warn level must NOT require gate by itself"

    def test_one_above_warn_threshold(self, tmp_path):
        contract = _contract()
        deleted = [f"src/old_{i}.py" for i in range(DELETION_FILE_WARN + 1)]

        with patch("codex_final_gate.subprocess.run", side_effect=_mock_git(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.mass_deletion_warn is True
        assert result.required is False

    def test_warn_injects_finding_in_receipt(self, tmp_path):
        contract = _contract(
            pr_id="PR-warn-receipt",
            pr_title="Warn level deletion receipt",
            deliverables=[Deliverable(description="remove helpers", category="infrastructure")],
            content_hash="warnreceipt01",
        )
        deleted = [f"helpers/h_{i}.py" for i in range(DELETION_FILE_WARN + 1)]

        with patch("codex_final_gate.subprocess.run", side_effect=_mock_git(deleted)):
            with patch("codex_final_gate.emit_governance_receipt"):
                receipt = evaluate_and_record(contract, project_root=tmp_path)

        warn_msgs = [
            f["message"] for f in receipt.findings
            if f.get("severity") == "warning" and "deletion" in f.get("message", "").lower()
        ]
        assert len(warn_msgs) >= 1
        assert any(str(DELETION_FILE_WARN) in m for m in warn_msgs)

    def test_warn_does_not_add_to_reasons(self, tmp_path):
        contract = _contract()
        deleted = [f"src/old_{i}.py" for i in range(DELETION_FILE_WARN)]

        with patch("codex_final_gate.subprocess.run", side_effect=_mock_git(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert "mass_file_deletion" not in result.reasons
        assert "mass_deletion_warning" in result.warnings


class TestNetDeletionBlockingAboveHoldThreshold:
    """PRs deleting >= DELETION_FILE_HOLD files must require the Codex gate."""

    def test_hold_threshold_requires_gate(self, tmp_path):
        contract = _contract()
        deleted = [f"src/old_{i}.py" for i in range(DELETION_FILE_HOLD)]

        with patch("codex_final_gate.subprocess.run", side_effect=_mock_git(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.required is True
        assert "mass_file_deletion" in result.reasons
        assert result.mass_deletion_warn is False

    def test_above_hold_threshold_blocks_clearance_without_receipt(self, tmp_path):
        contract = _contract()
        deleted = [f"src/old_{i}.py" for i in range(DELETION_FILE_HOLD + 5)]

        with patch("codex_final_gate.subprocess.run", side_effect=_mock_git(deleted)):
            result = check_gate_clearance(contract, receipt=None, project_root=tmp_path)

        assert result["cleared"] is False
        assert "missing_codex_gate_receipt" in result["blockers"]

    def test_hold_does_not_set_warn_flag(self, tmp_path):
        contract = _contract()
        deleted = [f"src/old_{i}.py" for i in range(DELETION_FILE_HOLD)]

        with patch("codex_final_gate.subprocess.run", side_effect=_mock_git(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.mass_deletion_warn is False


class TestBelowWarnThresholdNoDeletion:
    """PRs deleting < DELETION_FILE_WARN files must not trigger any deletion flags."""

    def test_four_deleted_files_no_flag(self, tmp_path):
        contract = _contract()
        deleted = [f"src/old_{i}.py" for i in range(DELETION_FILE_WARN - 1)]

        with patch("codex_final_gate.subprocess.run", side_effect=_mock_git(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.mass_deletion_warn is False
        assert result.required is False
        assert "mass_file_deletion" not in result.reasons

    def test_zero_deleted_files_no_flag(self, tmp_path):
        contract = _contract()

        with patch("codex_final_gate.subprocess.run", side_effect=_mock_git([])):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.mass_deletion_warn is False
        assert result.mass_deletion_count == 0
        assert result.required is False

    def test_no_deletion_warning_in_receipt(self, tmp_path):
        contract = _contract(
            pr_id="PR-clean-receipt",
            pr_title="Clean PR",
            deliverables=[Deliverable(description="add feature", category="feature")],
            content_hash="clean01",
        )
        with patch("codex_final_gate.subprocess.run", side_effect=_mock_git([])):
            with patch("codex_final_gate.emit_governance_receipt"):
                receipt = evaluate_and_record(contract, project_root=tmp_path)

        deletion_findings = [
            f for f in receipt.findings
            if "deletion" in f.get("message", "").lower()
        ]
        assert len(deletion_findings) == 0


class TestDeletedFilesAppearsInRenderedPrompt:
    """Deleted files must appear in the Net-Deletion Alert section of the rendered prompt."""

    def test_deleted_files_listed_in_prompt(self):
        contract = _contract(
            pr_id="PR-render-sanity",
            pr_title="Render sanity",
            deliverables=[Deliverable(description="remove old module", category="infrastructure")],
            review_stack=["codex_gate"],
        )
        deleted = ["scripts/old_helper.py", "tests/test_old_helper.py"]
        prompt = render_codex_prompt(contract, deleted_files=deleted)

        assert "Net-Deletion Alert" in prompt
        assert "`scripts/old_helper.py`" in prompt
        assert "`tests/test_old_helper.py`" in prompt

    def test_prompt_no_alert_when_no_deleted_files(self):
        contract = _contract(
            pr_id="PR-no-del",
            pr_title="No deletions",
            deliverables=[Deliverable(description="add feature", category="feature")],
            review_stack=["codex_gate"],
        )
        prompt = render_codex_prompt(contract, deleted_files=[])
        assert "Net-Deletion Alert" not in prompt

    def test_hold_level_shown_in_alert_header(self):
        contract = _contract(
            pr_id="PR-hold-render",
            pr_title="Hold level render",
            deliverables=[Deliverable(description="mass cleanup", category="infrastructure")],
            review_stack=["codex_gate"],
        )
        deleted = [f"src/old_{i}.py" for i in range(DELETION_FILE_HOLD)]
        prompt = render_codex_prompt(contract, deleted_files=deleted)
        assert "[HOLD]" in prompt

    def test_warn_level_shown_in_alert_header(self):
        contract = _contract(
            pr_id="PR-warn-render",
            pr_title="Warn level render",
            deliverables=[Deliverable(description="small cleanup", category="infrastructure")],
            review_stack=["codex_gate"],
        )
        deleted = [f"src/old_{i}.py" for i in range(DELETION_FILE_WARN)]
        prompt = render_codex_prompt(contract, deleted_files=deleted)
        assert "[WARN]" in prompt


class TestFormatDeletedFilesAlertSanity:
    """_format_deleted_files_alert output format invariants."""

    def test_returns_list_of_strings(self):
        result = _format_deleted_files_alert(["a.py", "b.py"])
        assert isinstance(result, list)
        assert all(isinstance(line, str) for line in result)

    def test_count_reflected_in_header(self):
        deleted = [f"file_{i}.py" for i in range(7)]
        lines = _format_deleted_files_alert(deleted)
        assert "7" in lines[0]

    def test_all_filenames_appear_as_code_spans(self):
        deleted = ["scripts/foo.py", "tests/bar.py", "docs/baz.md"]
        text = "\n".join(_format_deleted_files_alert(deleted))
        for f in deleted:
            assert f"`{f}`" in text


class TestClearanceWarningsKeyPresent:
    """check_gate_clearance must always include a 'warnings' key in its result."""

    def test_warnings_key_not_required_gate(self):
        contract = _contract(risk_class="low", review_stack=["gemini_review"], changed_files=["docs/x.md"])
        result = check_gate_clearance(contract, None)
        assert "warnings" in result

    def test_warnings_key_required_no_receipt(self):
        result = check_gate_clearance(_contract(), None)
        assert "warnings" in result

    def test_warnings_key_cleared(self):
        contract = _contract(content_hash="sanity01")
        receipt = CodexFinalGateReceipt(
            pr_id="PR-sanity",
            verdict="pass",
            required=True,
            content_hash="sanity01",
        )
        result = check_gate_clearance(contract, receipt)
        assert "warnings" in result
