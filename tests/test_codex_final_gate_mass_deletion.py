#!/usr/bin/env python3
"""Regression tests for the mass-deletion and net-line-deletion tripwires.

``enforce_codex_gate`` must promote a PR to gate-required at the HOLD
thresholds, flag WARN-level deletion without requiring the gate, and never
crash on a git failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from codex_final_gate import (
    DELETION_FILE_HOLD,
    DELETION_FILE_WARN,
    NET_LINE_DELETION_HOLD,
    NET_LINE_DELETION_WARN,
    _parse_numstat_net,
    enforce_codex_gate,
    render_codex_prompt,
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
    """enforce_codex_gate must require the gate at the mass_file_deletion HOLD level."""

    def test_mass_deletion_triggers_enforce(self, tmp_path):
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_HOLD + 5)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.mass_deletion_count >= DELETION_FILE_HOLD
        assert "mass_file_deletion" in result.reasons
        assert result.required is True

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

    def test_warn_level_appears_in_warnings(self, tmp_path):
        """WARN-level file deletion surfaces in the result's warnings list."""
        contract = _make_contract()
        deleted = [f"old/file_{i}.py" for i in range(DELETION_FILE_WARN + 1)]

        with patch("codex_final_gate.subprocess.run", return_value=_mock_git_deleted(deleted)):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.warnings == ["mass_deletion_warning"]

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

    def test_warn_level_appears_in_warnings(self, tmp_path):
        """WARN-level net line deletion surfaces in the result's warnings list."""
        contract = _make_contract()
        with patch("codex_final_gate.subprocess.run", side_effect=[
            MagicMock(returncode=0, stdout=""),
            _mock_numstat(NET_LINE_DELETION_WARN + 50),
        ]):
            result = enforce_codex_gate(contract, project_root=tmp_path)

        assert result.warnings == ["net_line_deletion_warning"]

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


class TestRenderPromptIncludesDeletionAlert:
    """render_codex_prompt must inject the Net-Deletion Alert when files were deleted."""

    def _render_contract(self):
        return _make_contract(
            pr_id="PR-render",
            pr_title="Render test",
            deliverables=[Deliverable(description="cleanup old module", category="infrastructure")],
            review_stack=["codex_gate"],
            content_hash="render456",
        )

    def test_deleted_files_appear_in_rendered_prompt(self):
        contract = self._render_contract()
        deleted = ["old/module.py", "old/helper.py"]

        prompt = render_codex_prompt(contract, deleted_files=deleted)

        assert "Net-Deletion Alert [WARN] (2 file(s) deleted)" in prompt
        assert "`old/module.py`" in prompt
        assert "`old/helper.py`" in prompt

    def test_deleted_files_at_hold_level_render_hold(self):
        contract = self._render_contract()
        deleted = [f"old/module_{i}.py" for i in range(DELETION_FILE_HOLD)]

        prompt = render_codex_prompt(contract, deleted_files=deleted)

        assert f"Net-Deletion Alert [HOLD] ({DELETION_FILE_HOLD} file(s) deleted)" in prompt

    def test_no_deleted_files_no_alert_section(self):
        contract = self._render_contract()

        prompt = render_codex_prompt(contract)

        assert "Net-Deletion Alert" not in prompt
