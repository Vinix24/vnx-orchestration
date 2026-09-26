"""OI-1769: codex_parser._normalize_findings must preserve file_path/line.

Before this fix, ``_normalize_findings`` kept only ``severity``/``message`` —
``file_path``/``line`` were silently dropped even though both verdict-contract
templates (gate_lane_contract.VERDICT_CONTRACT, gate_runner
._REVIEWER_VERDICT_TEMPLATE) have asked every gate for them since PR #1859
(merged 2026-09-16). Neither template's addition touched codex_parser.py, so
this was a third normalizer the other two (review_contract._normalize_line's
two existing importers) never knew about.

Measured over the 60 most recent codex_gate records (2026-09-17): 36
findings, fields exclusively severity/message, zero with a non-empty
file_path — because every one of them passed through this function.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import codex_parser
from codex_parser import _normalize_findings
from review_contract import _normalize_line


class TestNormalizeFindingsPreservesAddress:

    def test_file_path_and_line_preserved(self):
        result = _normalize_findings([
            {"severity": "error", "message": "missing null check", "file_path": "scripts/lib/foo.py", "line": 42},
        ])
        assert result == [
            {"severity": "error", "message": "missing null check", "file_path": "scripts/lib/foo.py", "line": 42},
        ]

    def test_missing_address_defaults_to_empty_path_and_zero_line(self):
        """A finding about the PR as a whole (no single line) must get
        file_path="" and line=0 — never a guessed value.
        """
        result = _normalize_findings([{"severity": "info", "message": "PR-wide observation"}])
        assert result == [
            {"severity": "info", "message": "PR-wide observation", "file_path": "", "line": 0},
        ]

    def test_numeric_string_line_coerced_not_dropped(self):
        """A reviewing LLM regularly emits "line": "137" (string, not int) —
        review_contract._normalize_line's whole reason for existing.
        """
        result = _normalize_findings([
            {"severity": "warning", "message": "x", "file_path": "a.py", "line": "137"},
        ])
        assert result[0]["line"] == 137

    def test_negative_line_normalized_to_zero(self):
        result = _normalize_findings([
            {"severity": "warning", "message": "x", "file_path": "a.py", "line": -3},
        ])
        assert result[0]["line"] == 0

    def test_garbage_line_value_normalized_to_zero_not_raised(self):
        result = _normalize_findings([
            {"severity": "warning", "message": "x", "file_path": "a.py", "line": "ergens"},
        ])
        assert result[0]["line"] == 0

    def test_uses_shared_normalize_line_not_a_second_copy(self):
        """The canonical import: review_contract._normalize_line is the ONE
        line-coercion function, already reused by claude_github_receipt.py
        and review_receipt.py (see TestNormalizeLine in
        test_review_contract.py). codex_parser must import it too, not
        redefine its own.
        """
        assert codex_parser._normalize_line is _normalize_line

    def test_string_finding_gets_empty_address_not_a_crash(self):
        result = _normalize_findings(["bare string finding"])
        assert result == [
            {"severity": "warning", "message": "bare string finding", "file_path": "", "line": 0},
        ]

    def test_non_dict_non_string_finding_gets_empty_address(self):
        result = _normalize_findings([42])
        assert result == [
            {"severity": "warning", "message": "42", "file_path": "", "line": 0},
        ]

    def test_empty_findings_list_returns_empty(self):
        assert _normalize_findings([]) == []

    def test_none_input_returns_empty(self):
        assert _normalize_findings(None) == []

    @pytest.mark.parametrize(
        "raw_findings, expected_paths_and_lines",
        [
            (
                [
                    {"severity": "critical", "message": "a", "file_path": "x.py", "line": 10},
                    {"severity": "info", "message": "b"},
                ],
                [("x.py", 10), ("", 0)],
            ),
        ],
    )
    def test_mixed_addressed_and_unaddressed_findings(self, raw_findings, expected_paths_and_lines):
        result = _normalize_findings(raw_findings)
        actual = [(f["file_path"], f["line"]) for f in result]
        assert actual == expected_paths_and_lines
