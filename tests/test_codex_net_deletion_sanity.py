#!/usr/bin/env python3
"""Net-deletion sanity check coverage for codex_final_gate.

Fills coverage gaps not addressed by test_codex_final_gate_mass_deletion.py:
- render_codex_prompt(contract, deleted_files=...) direct API (not via CLI)
- _format_deleted_files_alert WARN vs HOLD label
- Net deletion sanity item in Review Instructions
- Dual HOLD trigger: both file-count and net-line-deletion in reasons simultaneously
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
    _format_deleted_files_alert,
    enforce_codex_gate,
    render_codex_prompt,
)
from review_contract import Deliverable, ReviewContract


def _make_contract(**overrides) -> ReviewContract:
    defaults = dict(
        pr_id="PR-nds",
        pr_title="Net deletion sanity test",
        feature_title="test",
        branch="feat/test",
        track="A",
        risk_class="low",
        merge_policy="squash_merge",
        closure_stage="open",
        deliverables=[Deliverable(description="remove stale files", category="infrastructure")],
        review_stack=["codex_gate"],
        changed_files=[],
        content_hash="nds123",
    )
    defaults.update(overrides)
    return ReviewContract(**defaults)


# ---------------------------------------------------------------------------
# _format_deleted_files_alert: WARN vs HOLD label
# ---------------------------------------------------------------------------

class TestFormatDeletedFilesAlert:
    """_format_deleted_files_alert must label correctly based on count vs DELETION_FILE_HOLD."""

    def test_warn_label_below_hold_threshold(self):
        """Count below DELETION_FILE_HOLD gets [WARN] label."""
        count = DELETION_FILE_HOLD - 1
        files = [f"old/file_{i}.py" for i in range(count)]
        lines = _format_deleted_files_alert(files)
        header = lines[0]
        assert "[WARN]" in header
        assert "[HOLD]" not in header
        assert str(count) in header

    def test_hold_label_at_threshold(self):
        """Count exactly DELETION_FILE_HOLD gets [HOLD] label."""
        files = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD)]
        lines = _format_deleted_files_alert(files)
        header = lines[0]
        assert "[HOLD]" in header
        assert "[WARN]" not in header

    def test_hold_label_above_threshold(self):
        """Count above DELETION_FILE_HOLD still gets [HOLD] label."""
        count = DELETION_FILE_HOLD + 10
        files = [f"old/file_{i}.py" for i in range(count)]
        lines = _format_deleted_files_alert(files)
        header = lines[0]
        assert "[HOLD]" in header

    def test_file_list_appears_in_output(self):
        """Each deleted file must appear as a list item in the alert."""
        files = ["scripts/old_helper.py", "scripts/legacy.py"]
        lines = _format_deleted_files_alert(files)
        full_text = "\n".join(lines)
        assert "`scripts/old_helper.py`" in full_text
        assert "`scripts/legacy.py`" in full_text

    def test_alert_header_includes_file_count(self):
        """Alert header must state the exact file count."""
        count = 7
        files = [f"file_{i}.py" for i in range(count)]
        lines = _format_deleted_files_alert(files)
        assert str(count) in lines[0]


# ---------------------------------------------------------------------------
# render_codex_prompt: direct deleted_files parameter
# ---------------------------------------------------------------------------

class TestRenderCodexPromptWithDeletedFiles:
    """render_codex_prompt must inject Net-Deletion Alert when deleted_files is provided."""

    def test_deleted_files_injects_alert_section(self):
        contract = _make_contract()
        deleted = ["old/module.py", "old/helper.py"]
        prompt = render_codex_prompt(contract, deleted_files=deleted)
        assert "## Net-Deletion Alert" in prompt
        assert "`old/module.py`" in prompt
        assert "`old/helper.py`" in prompt

    def test_no_deleted_files_none_omits_alert(self):
        """deleted_files=None must not inject the alert section."""
        contract = _make_contract()
        prompt = render_codex_prompt(contract, deleted_files=None)
        assert "## Net-Deletion Alert" not in prompt

    def test_no_deleted_files_empty_list_omits_alert(self):
        """deleted_files=[] (empty list) must not inject the alert section."""
        contract = _make_contract()
        prompt = render_codex_prompt(contract, deleted_files=[])
        assert "## Net-Deletion Alert" not in prompt

    def test_deleted_files_warn_label_when_below_hold(self):
        """Alert label is WARN when file count is below DELETION_FILE_HOLD."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD - 1)]
        prompt = render_codex_prompt(contract, deleted_files=deleted)
        assert "[WARN]" in prompt

    def test_deleted_files_hold_label_at_threshold(self):
        """Alert label is HOLD when file count equals DELETION_FILE_HOLD."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD)]
        prompt = render_codex_prompt(contract, deleted_files=deleted)
        assert "[HOLD]" in prompt


# ---------------------------------------------------------------------------
# render_codex_prompt: Review Instructions include net deletion sanity item
# ---------------------------------------------------------------------------

class TestRenderCodexPromptNetSanityInstruction:
    """Review Instructions section must include the net deletion sanity check item."""

    def test_net_deletion_sanity_item_present(self):
        """Instruction #6 about net deletion sanity must be in the rendered prompt."""
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "Net deletion sanity" in prompt

    def test_net_deletion_sanity_item_mentions_threshold(self):
        """The instruction must reference the ≥5-file warning threshold."""
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        # The instruction mentions "≥5 files" to match DELETION_FILE_WARN=5
        assert "≥5" in prompt or "5 files" in prompt or "WARN" in prompt or "warning finding" in prompt

    def test_review_instructions_section_present(self):
        """Review Instructions section must always appear in the prompt."""
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "## Review Instructions" in prompt


# ---------------------------------------------------------------------------
# enforce_codex_gate: dual HOLD (file count + net line deletion)
# ---------------------------------------------------------------------------

class TestEnforceDualHold:
    """When both file-count and net-line-deletion exceed HOLD thresholds, both reasons appear."""

    def _mock_deleted(self, files: list) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "\n".join(files) + "\n" if files else ""
        return m

    def _mock_numstat(self, net_removed: int) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stdout = f"0\t{net_removed}\tsome/file.py\n"
        return m

    def test_both_hold_triggers_both_reasons(self, tmp_path):
        """Both mass_file_deletion and net_line_deletion must appear in reasons when both exceed HOLD."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD + 5)]

        with patch("codex_final_gate.subprocess.run", side_effect=[
            self._mock_deleted(deleted),
            self._mock_numstat(NET_LINE_DELETION_HOLD + 100),
        ]):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert "mass_file_deletion" in result.reasons
        assert "net_line_deletion" in result.reasons
        assert result.required is True
        assert result.mass_deletion_warn is False  # HOLD supersedes WARN flag
        assert result.net_line_deletion_warn is False  # HOLD supersedes WARN flag

    def test_file_hold_only_no_line_reason(self, tmp_path):
        """File-count at HOLD but net-line below threshold: only mass_file_deletion reason."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD)]

        with patch("codex_final_gate.subprocess.run", side_effect=[
            self._mock_deleted(deleted),
            self._mock_numstat(NET_LINE_DELETION_WARN - 10),
        ]):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert "mass_file_deletion" in result.reasons
        assert "net_line_deletion" not in result.reasons
        assert result.required is True

    def test_line_hold_only_no_file_reason(self, tmp_path):
        """Net-line at HOLD but file count below threshold: only net_line_deletion reason."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_WARN - 1)]

        with patch("codex_final_gate.subprocess.run", side_effect=[
            self._mock_deleted(deleted),
            self._mock_numstat(NET_LINE_DELETION_HOLD),
        ]):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert "net_line_deletion" in result.reasons
        assert "mass_file_deletion" not in result.reasons
        assert result.required is True

    def test_both_warn_neither_required(self, tmp_path):
        """Both at WARN level: required remains False, both warn flags set."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_WARN)]

        with patch("codex_final_gate.subprocess.run", side_effect=[
            self._mock_deleted(deleted),
            self._mock_numstat(NET_LINE_DELETION_WARN),
        ]):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.required is False
        assert result.mass_deletion_warn is True
        assert result.net_line_deletion_warn is True
        assert "mass_file_deletion" not in result.reasons
        assert "net_line_deletion" not in result.reasons
