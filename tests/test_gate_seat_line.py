#!/usr/bin/env python3
"""OI-1888: the `vnx gate` slot-line must name whoever actually read the seat.

`scripts/commands/gate.sh` used to print `Gate '<seat>': PASS` from the raw
requested seat name and request-and-execute's exit code alone. When a seat's
own reader was unavailable and the takeover chain handed it to a different
gate (kimi_gate unavailable -> glm_gate reads instead), the exit code
correctly reflected the successor's verdict but the line still named the
original seat as if it had read. PR #1952 merged on 2026-09-27 on exactly
that misreading.

`gate_seat_line.py` resolves, per requested seat, which report entry (from
review_gate_manager's request-and-execute JSON) accounts for it -- reusing
`gate_enforcement_verify.resolve_seat_entries`, the same takeover
interpretation `scripts/t0_gate_enforcement.sh` already relies on, rather
than re-deriving the takeover walk a second time -- and builds the display
line from THAT entry's own verdict, never the seat name alone.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# Reuse the report/entry builders and the state_dir fixture from
# test_t0_gate_enforcement.py instead of a second copy (the dispatch's own
# instruction: reuse the existing takeover interpretation and its fixtures).
from test_t0_gate_enforcement import _entry, _report, state_dir  # noqa: E402,F401

from gate_enforcement_verify import resolve_seat_entries  # noqa: E402
from gate_seat_line import format_seat_line, format_seat_lines  # noqa: E402

GATE_SEAT_LINE = REPO_ROOT / "scripts" / "lib" / "gate_seat_line.py"


def _line(report: Dict[str, Any], seat: str, *, pr_number: int = 7, state_dir: Path):
    seat_entries = resolve_seat_entries(report, pr_number=pr_number, state_dir=state_dir)
    return format_seat_line(seat, pr_number=pr_number, state_dir=state_dir, seat_entries=seat_entries)


# ---------------------------------------------------------------------------
# (a) takeover: glm reads for kimi with PASS
# ---------------------------------------------------------------------------

def test_takeover_line_names_the_successor_and_its_verdict(state_dir: Path) -> None:
    entry = _entry("glm_gate", takeover_path=["kimi_gate"])
    entry["detail"]["takeover_path"] = [
        {"gate": "kimi_gate", "reason": "lane_exhausted", "detail": "quota spent", "status": "unavailable"},
    ]
    line, passed = _line(_report(entry), "kimi_gate", state_dir=state_dir)

    assert passed is True
    assert line == "Gate 'kimi_gate' -> glm_gate (takeover, lane_exhausted): PASS"


def test_a_multi_hop_takeover_uses_the_seats_own_hop_reason(state_dir: Path) -> None:
    """codex_gate exhausted -> kimi_gate exhausted -> glm_gate reads. The line
    for the codex_gate seat must cite CODEX's own hop reason, not kimi's."""
    entry = _entry("glm_gate")
    entry["detail"]["takeover_path"] = [
        {"gate": "codex_gate", "reason": "quota_spent", "detail": "codex out of quota", "status": "unavailable"},
        {"gate": "kimi_gate", "reason": "lane_exhausted", "detail": "kimi 403", "status": "unavailable"},
    ]
    line, passed = _line(_report(entry), "codex_gate", state_dir=state_dir)

    assert passed is True
    assert line == "Gate 'codex_gate' -> glm_gate (takeover, quota_spent): PASS"


# ---------------------------------------------------------------------------
# (b) no takeover: the line is unchanged
# ---------------------------------------------------------------------------

def test_no_takeover_line_is_unchanged(state_dir: Path) -> None:
    line, passed = _line(_report(_entry("kimi_gate")), "kimi_gate", state_dir=state_dir)
    assert passed is True
    assert line == "Gate 'kimi_gate': PASS"


# ---------------------------------------------------------------------------
# (c) unavailable, no successor: never PASS
# ---------------------------------------------------------------------------

def test_unavailable_without_a_successor_is_never_pass(state_dir: Path) -> None:
    entry = {
        "gate": "kimi_gate",
        "request_status": "not_executable",
        "execution_status": "not_executable",
        "passed": False,
        "reason": "provider_not_installed",
        "reason_detail": "kimi binary not found on PATH",
        "detail": {"gate": "kimi_gate"},
    }
    line, passed = _line(_report(entry), "kimi_gate", state_dir=state_dir)

    assert passed is False
    assert line == "Gate 'kimi_gate': UNAVAILABLE (kimi binary not found on PATH)"
    assert "PASS" not in line


def test_chain_exhausted_without_a_successor_is_never_pass(state_dir: Path) -> None:
    entry = {
        "gate": "kimi_gate",
        "request_status": "chain_exhausted",
        "execution_status": "chain_exhausted",
        "passed": False,
        "reason_detail": "kimi_gate unavailable (lane_exhausted) -- takeover chain exhausted, no live reader remains",
        "detail": {"gate": "kimi_gate", "takeover_path": []},
    }
    line, passed = _line(_report(entry), "kimi_gate", state_dir=state_dir)

    assert passed is False
    assert line.startswith("Gate 'kimi_gate': UNAVAILABLE")
    assert "PASS" not in line


def test_a_seat_with_no_entry_at_all_is_unavailable_not_pass(state_dir: Path) -> None:
    line, passed = _line(_report(_entry("codex_gate")), "kimi_gate", state_dir=state_dir)
    assert passed is False
    assert line.startswith("Gate 'kimi_gate': UNAVAILABLE")


def test_a_decided_fail_is_labelled_fail_not_unavailable(state_dir: Path) -> None:
    entry = {
        "gate": "kimi_gate",
        "request_status": "requested",
        "execution_status": "failed",
        "passed": False,
        "reason_detail": "2 blocking finding(s)",
        "detail": {"gate": "kimi_gate"},
    }
    line, passed = _line(_report(entry), "kimi_gate", state_dir=state_dir)

    assert passed is False
    assert line == "Gate 'kimi_gate': FAIL (2 blocking finding(s))"


# ---------------------------------------------------------------------------
# (d) diff_truncated
# ---------------------------------------------------------------------------

def test_diff_truncated_is_shown_on_a_pass(state_dir: Path) -> None:
    entry = _entry("kimi_gate")
    entry["detail"]["diff_truncated"] = True
    line, passed = _line(_report(entry), "kimi_gate", state_dir=state_dir)
    assert passed is True
    assert line == "Gate 'kimi_gate': PASS (diff truncated)"


def test_diff_truncated_is_shown_on_a_takeover(state_dir: Path) -> None:
    entry = _entry("glm_gate", takeover_path=["kimi_gate"])
    entry["detail"]["diff_truncated"] = True
    line, passed = _line(_report(entry), "kimi_gate", state_dir=state_dir)
    assert passed is True
    assert line == "Gate 'kimi_gate' -> glm_gate (takeover, quota): PASS (diff truncated)"


# ---------------------------------------------------------------------------
# format_seat_lines: order + per-seat outcome
# ---------------------------------------------------------------------------

def test_format_seat_lines_preserves_requested_order(state_dir: Path) -> None:
    report = _report(_entry("codex_gate"), _entry("kimi_gate"))
    results = format_seat_lines(["kimi_gate", "codex_gate"], report, pr_number=7, state_dir=state_dir)
    assert [line for line, _ in results] == [
        "Gate 'kimi_gate': PASS",
        "Gate 'codex_gate': PASS",
    ]
    assert [passed for _, passed in results] == [True, True]


def test_format_seat_lines_reports_a_failing_seat_independently(state_dir: Path) -> None:
    failing = _entry("kimi_gate")
    failing["passed"] = False
    failing["execution_status"] = "unavailable"
    report = _report(_entry("codex_gate"), failing)
    results = format_seat_lines(["codex_gate", "kimi_gate"], report, pr_number=7, state_dir=state_dir)
    assert [passed for _, passed in results] == [True, False]


# ---------------------------------------------------------------------------
# CLI: the actual subcommand gate.sh invokes
# ---------------------------------------------------------------------------

def _run_cli(report: Dict[str, Any], *, pr: int, state_dir: Path, seats: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE_SEAT_LINE), "--pr", str(pr), "--state-dir", str(state_dir), "--seats", seats],
        input=json.dumps(report, indent=2), capture_output=True, text=True, timeout=30,
    )


def test_cli_prints_tag_prefixed_lines_and_exits_zero_on_full_pass(state_dir: Path) -> None:
    report = _report(_entry("codex_gate"), _entry("kimi_gate"))
    proc = _run_cli(report, pr=7, state_dir=state_dir, seats="codex_gate,kimi_gate")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "PASS\tGate 'codex_gate': PASS",
        "PASS\tGate 'kimi_gate': PASS",
    ]


def test_cli_exits_non_zero_when_a_seat_is_unavailable(state_dir: Path) -> None:
    entry = {
        "gate": "kimi_gate", "request_status": "not_executable", "execution_status": "not_executable",
        "passed": False, "reason_detail": "kimi binary not found", "detail": {"gate": "kimi_gate"},
    }
    proc = _run_cli(_report(entry), pr=7, state_dir=state_dir, seats="kimi_gate")
    assert proc.returncode == 1
    assert proc.stdout.strip() == "FAIL\tGate 'kimi_gate': UNAVAILABLE (kimi binary not found)"


def test_cli_extracts_the_report_from_merged_stdout_and_stderr(state_dir: Path) -> None:
    """gate.sh feeds this script `2>&1`-merged output, the same shape
    gate_enforcement_verify.extract_report already expects — noise around the
    JSON block must not break extraction."""
    report = _report(_entry("kimi_gate"))
    merged = (
        "gate_request_handler: review-gate takeover chain actief\n"
        + json.dumps(report, indent=2)
        + "\nERROR: required gates did not PASS: kimi_gate\n"
    )
    proc = subprocess.run(
        [sys.executable, str(GATE_SEAT_LINE), "--pr", "7", "--state-dir", str(state_dir), "--seats", "kimi_gate"],
        input=merged, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "PASS\tGate 'kimi_gate': PASS"
