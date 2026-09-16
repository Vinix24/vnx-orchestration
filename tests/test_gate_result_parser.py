#!/usr/bin/env python3
"""Tests for the finding types gate_result_parser.py produces (OI finding-ankers).

gate_result_parser.record_result / record_claude_github_result classify raw
verdict-JSON findings into GeminiReviewFinding / ClaudeGitHubReviewFinding
objects (gemini_prompt_renderer.py / claude_github_receipt.py). Both types
already carried `file_path`/`line` fields, but nothing upstream ever asked a
reviewer model to fill them in — measured 2026-09-15 over 705 result files
under ~/.vnx-data/vnx-dev/state/review_gates/results/: 300 raw findings, 634
classified (blocking+advisory) findings, 0 carrying a structured file_path or
line. This file pins the propagation (A), backward-compat loading of the old
2-key shape (B), and crash-safety for a garbage `line` value (C) now that
gate_lane_contract.VERDICT_CONTRACT and gate_runner._REVIEWER_VERDICT_TEMPLATE
ask reviewers to supply both fields.
"""

from __future__ import annotations

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gemini_prompt_renderer import GeminiReviewReceipt
from claude_github_receipt import ClaudeGitHubReviewReceipt


# ---------------------------------------------------------------------------
# Test A — a verdict finding with file_path/line filled round-trips exactly
# ---------------------------------------------------------------------------
#
# Measured on main (pre-fix) 2026-09-15: this assertion already PASSED —
# GeminiReviewFinding/ClaudeGitHubReviewFinding have carried file_path/line
# since PR-2 (fa275ec8) and PR-4 (98862a7f). Nothing was ever red here at the
# dataclass level; the gap was entirely upstream (the reviewer was never
# asked, per the 0-carrying-a-structured-field measurement above) and at the
# `line` normalization boundary (see test C, which IS red on main). Kept here
# as the permanent regression guard for the round-trip, now that the contract
# templates actually ask for these two fields.

def test_gemini_finding_round_trips_file_path_and_line():
    raw = [{
        "severity": "blocking",
        "category": "correctness",
        "message": "off-by-one in the loop bound",
        "file_path": "scripts/lib/gate_result_parser.py",
        "line": 137,
    }]
    receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-X", raw_findings=raw)
    finding = receipt.to_dict()["blocking_findings"][0]
    assert finding["file_path"] == "scripts/lib/gate_result_parser.py"
    assert finding["line"] == 137


def test_claude_github_finding_round_trips_file_path_and_line():
    raw = [{
        "severity": "blocking",
        "category": "correctness",
        "message": "off-by-one in the loop bound",
        "file_path": "scripts/lib/gate_result_parser.py",
        "line": 137,
    }]
    receipt = ClaudeGitHubReviewReceipt.from_result_payload({"findings": raw})
    finding = receipt.to_dict()["blocking_findings"][0]
    assert finding["file_path"] == "scripts/lib/gate_result_parser.py"
    assert finding["line"] == 137


# ---------------------------------------------------------------------------
# Test B — backward compat: a real, pre-existing 2-key finding still loads
# ---------------------------------------------------------------------------
#
# Taken verbatim from a real on-disk result,
# ~/.vnx-data/vnx-dev/state/review_gates/results/pr-1192-codex_gate.json,
# `blocking_findings[0]` — one of the 634 classified findings measured to
# carry no file_path/line key at all. This record must keep loading after
# this dispatch: a parser that breaks on the old 2-key shape would make the
# entire historical corpus (705 files) unreadable.

_REAL_OLD_STYLE_FINDING = {
    "severity": "error",
    "message": (
        "New line `+    except Exception:` in `_resolve_project_root` silently "
        "swallows all project-root resolution failures and falls back without "
        "logging or re-raising, which violates the gate's error-handling rule "
        "for silent exception swallowing."
    ),
}


def test_gemini_backward_compat_old_two_key_finding_defaults_file_path_and_line():
    receipt = GeminiReviewReceipt.from_raw_findings(
        pr_id="PR-1192", raw_findings=[_REAL_OLD_STYLE_FINDING]
    )
    finding = receipt.to_dict()["blocking_findings"][0]
    assert finding["file_path"] == ""
    assert finding["line"] == 0
    assert finding["message"] == _REAL_OLD_STYLE_FINDING["message"]


def test_claude_github_backward_compat_old_two_key_finding_defaults_file_path_and_line():
    receipt = ClaudeGitHubReviewReceipt.from_result_payload(
        {"findings": [_REAL_OLD_STYLE_FINDING]}
    )
    finding = receipt.to_dict()["blocking_findings"][0]
    assert finding["file_path"] == ""
    assert finding["line"] == 0
    assert finding["message"] == _REAL_OLD_STYLE_FINDING["message"]


# ---------------------------------------------------------------------------
# Test C — a nonsensical `line` value normalizes to 0, never crashes
# ---------------------------------------------------------------------------
#
# Measured on main (pre-fix) 2026-09-15: `line=int(raw.get("line", 0))` in
# both gemini_prompt_renderer.GeminiReviewReceipt.from_raw_findings and
# claude_github_receipt.ClaudeGitHubReviewReceipt.from_result_payload crashed
# on a non-numeric string and on None, and silently passed a negative int
# through unnormalized:
#   line="ergens" -> ValueError: invalid literal for int() with base 10: 'ergens'
#   line=None     -> TypeError: int() argument must be a string, a bytes-like
#                     object or a real number, not 'NoneType'
#   line=-3       -> passed through as -3 (not a crash, but not a valid line
#                     either — silently wrong instead of silently absent)

def _bad_line_finding(line_value):
    return {"severity": "blocking", "category": "x", "message": "y", "line": line_value}


def test_gemini_nonnumeric_line_normalizes_to_zero_without_crash():
    receipt = GeminiReviewReceipt.from_raw_findings(
        pr_id="PR-X", raw_findings=[_bad_line_finding("ergens")]
    )
    assert receipt.to_dict()["blocking_findings"][0]["line"] == 0


def test_gemini_negative_line_normalizes_to_zero_without_crash():
    receipt = GeminiReviewReceipt.from_raw_findings(
        pr_id="PR-X", raw_findings=[_bad_line_finding(-3)]
    )
    assert receipt.to_dict()["blocking_findings"][0]["line"] == 0


def test_gemini_null_line_normalizes_to_zero_without_crash():
    receipt = GeminiReviewReceipt.from_raw_findings(
        pr_id="PR-X", raw_findings=[_bad_line_finding(None)]
    )
    assert receipt.to_dict()["blocking_findings"][0]["line"] == 0


def test_claude_github_nonnumeric_line_normalizes_to_zero_without_crash():
    receipt = ClaudeGitHubReviewReceipt.from_result_payload(
        {"findings": [_bad_line_finding("ergens")]}
    )
    assert receipt.to_dict()["blocking_findings"][0]["line"] == 0


def test_claude_github_negative_line_normalizes_to_zero_without_crash():
    receipt = ClaudeGitHubReviewReceipt.from_result_payload(
        {"findings": [_bad_line_finding(-3)]}
    )
    assert receipt.to_dict()["blocking_findings"][0]["line"] == 0


def test_claude_github_null_line_normalizes_to_zero_without_crash():
    receipt = ClaudeGitHubReviewReceipt.from_result_payload(
        {"findings": [_bad_line_finding(None)]}
    )
    assert receipt.to_dict()["blocking_findings"][0]["line"] == 0
