#!/usr/bin/env python3
"""Tests for extended check_net_deletion in pre_merge_gate (net line deletion sanity check)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))

from pre_merge_gate import (
    DELETION_FILE_HOLD,
    DELETION_FILE_WARN,
    NET_LINE_DELETION_HOLD,
    NET_LINE_DELETION_WARN,
    _parse_numstat_net,
    check_net_deletion,
)


def _mock_deleted(deleted_files: list):
    m = MagicMock()
    m.returncode = 0
    m.stdout = "\n".join(deleted_files) + "\n" if deleted_files else ""
    return m


def _mock_numstat(net_removed: int):
    m = MagicMock()
    m.returncode = 0
    m.stdout = f"0\t{net_removed}\tsome/file.py\n"
    return m


def _mock_both(deleted_files: list, net_removed: int):
    """side_effect list: first call = git diff-filter (deleted files), second = numstat."""
    return [_mock_deleted(deleted_files), _mock_numstat(net_removed)]


class TestParseNumstatNet:
    def test_basic_net_deletion(self):
        assert _parse_numstat_net("10\t50\tfile.py\n5\t20\tother.py\n") == (50 + 20) - (10 + 5)

    def test_net_addition_returns_negative(self):
        assert _parse_numstat_net("100\t10\tfile.py\n") == 10 - 100

    def test_binary_files_skipped(self):
        assert _parse_numstat_net("-\t-\tbinary.png\n10\t30\ttext.py\n") == 30 - 10

    def test_empty_output_returns_zero(self):
        assert _parse_numstat_net("") == 0


class TestCheckNetDeletion:
    """check_net_deletion must flag both file count and net line deletion independently."""

    def test_file_hold_triggers(self, tmp_path):
        deleted = [f"file_{i}.py" for i in range(DELETION_FILE_HOLD)]
        with patch("pre_merge_gate.subprocess.run", side_effect=_mock_both(deleted, 0)):
            result = check_net_deletion(tmp_path)
        assert result["status"] == "HOLD"
        assert result["deleted_count"] == DELETION_FILE_HOLD

    def test_net_line_hold_triggers(self, tmp_path):
        with patch("pre_merge_gate.subprocess.run", side_effect=_mock_both([], NET_LINE_DELETION_HOLD)):
            result = check_net_deletion(tmp_path)
        assert result["status"] == "HOLD"
        assert result["net_line_deletion"] >= NET_LINE_DELETION_HOLD

    def test_both_warn_still_go(self, tmp_path):
        deleted = [f"file_{i}.py" for i in range(DELETION_FILE_WARN)]
        with patch("pre_merge_gate.subprocess.run", side_effect=_mock_both(deleted, NET_LINE_DELETION_WARN)):
            result = check_net_deletion(tmp_path)
        assert result["status"] == "GO"
        assert result["file_deletion_warn"] is True
        assert result["net_line_deletion_warn"] is True

    def test_file_warn_only(self, tmp_path):
        deleted = [f"file_{i}.py" for i in range(DELETION_FILE_WARN)]
        with patch("pre_merge_gate.subprocess.run", side_effect=_mock_both(deleted, 10)):
            result = check_net_deletion(tmp_path)
        assert result["status"] == "GO"
        assert result["file_deletion_warn"] is True
        assert result["net_line_deletion_warn"] is False

    def test_net_line_warn_only(self, tmp_path):
        with patch("pre_merge_gate.subprocess.run", side_effect=_mock_both([], NET_LINE_DELETION_WARN + 50)):
            result = check_net_deletion(tmp_path)
        assert result["status"] == "GO"
        assert result["net_line_deletion_warn"] is True
        assert result["file_deletion_warn"] is False

    def test_clean_pr_no_flags(self, tmp_path):
        with patch("pre_merge_gate.subprocess.run", side_effect=_mock_both([], 10)):
            result = check_net_deletion(tmp_path)
        assert result["status"] == "GO"
        assert result["file_deletion_warn"] is False
        assert result["net_line_deletion_warn"] is False

    def test_net_addition_ignored(self, tmp_path):
        """PR that adds more lines than it removes must not trigger net_line_deletion."""
        m_net_add = MagicMock()
        m_net_add.returncode = 0
        m_net_add.stdout = "1000\t10\tfile.py\n"
        with patch("pre_merge_gate.subprocess.run", side_effect=[_mock_deleted([]), m_net_add]):
            result = check_net_deletion(tmp_path)
        assert result["status"] == "GO"
        assert result["net_line_deletion_warn"] is False

    def test_git_failure_on_numstat_falls_back_to_none(self, tmp_path):
        fail = MagicMock()
        fail.returncode = 1
        fail.stdout = ""
        with patch("pre_merge_gate.subprocess.run", side_effect=[
            _mock_deleted([]),
            fail, fail, fail,  # all numstat attempts fail
        ]):
            result = check_net_deletion(tmp_path)
        assert result["net_line_deletion"] is None
        assert result["status"] == "GO"

    def test_file_hold_plus_net_line_hold(self, tmp_path):
        """Both triggers: status must still be HOLD."""
        deleted = [f"file_{i}.py" for i in range(DELETION_FILE_HOLD + 5)]
        with patch("pre_merge_gate.subprocess.run", side_effect=_mock_both(deleted, NET_LINE_DELETION_HOLD + 100)):
            result = check_net_deletion(tmp_path)
        assert result["status"] == "HOLD"

    def test_result_has_required_keys(self, tmp_path):
        with patch("pre_merge_gate.subprocess.run", side_effect=_mock_both([], 0)):
            result = check_net_deletion(tmp_path)
        for key in ("check", "status", "detail", "deleted_count", "deleted_files", "net_line_deletion",
                    "net_line_deletion_warn", "file_deletion_warn"):
            assert key in result, f"missing key: {key}"
