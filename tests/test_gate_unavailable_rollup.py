"""Regression tests for OI-1178: a non-executed gate must never read as PASS.

The measured bug (12-08, PR #1481): a codex_gate result with
``status="failed"``, ``summary="Gate execution exit_nonzero: Subprocess exited
with code 1"``, empty ``contract_hash`` and empty ``report_path`` was a
non-execution booked as a review verdict, and the CLI rollup printed
"All gates PASS" over it while the per-gate line showed "codex_gate FAIL".

These tests pin three invariants:

- ``record_failure`` books execution-level reasons (``exit_nonzero``,
  ``timeout``, …) as ``unavailable`` — absence of evidence — never ``failed``.
- a real verdict failure (reason not in ``EXECUTION_FAILURE_REASONS``) still
  books ``failed``.
- ``_execute_requested_gates`` sets ``has_required_failure`` for any required
  gate that did not pass with complete evidence, so the rollup can never sum
  a non-run gate up as PASS.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gate_executor import GateExecutorMixin
from gate_recorder import EXECUTION_FAILURE_REASONS, record_failure
from gate_status import has_complete_evidence, is_pass


# ---------------------------------------------------------------------------
# has_complete_evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result,expected",
    [
        # OI-1435: has_complete_evidence now also requires is_terminal, so
        # every case here carries a terminal status="pass" — the intent of
        # this table is to pin the contract_hash/report_path requirement in
        # isolation from terminality (covered separately below).
        ({"status": "pass", "contract_hash": "abc", "report_path": "/tmp/r.md"}, True),
        ({"status": "pass", "contract_hash": "", "report_path": "/tmp/r.md"}, False),
        ({"status": "pass", "contract_hash": "abc", "report_path": ""}, False),
        ({"status": "pass", "contract_hash": "abc", "report_path": None}, False),
        ({"status": "pass"}, False),
        ({}, False),
    ],
)
def test_has_complete_evidence_requires_hash_and_path(result, expected):
    assert has_complete_evidence(result) is expected


# ---------------------------------------------------------------------------
# has_complete_evidence: terminality is enforced BY THE FUNCTION (OI-1435)
# ---------------------------------------------------------------------------


def test_has_complete_evidence_false_on_nonterminal_record_even_with_full_evidence():
    """The invariant hangs off the function, not off caller call-order.

    A caller that invokes has_complete_evidence WITHOUT ever calling
    is_terminal first must still get the correct answer: an unavailable
    (non-terminal) record with both evidence fields fully populated must
    never read as complete evidence.
    """
    record = {"status": "unavailable", "contract_hash": "abc123", "report_path": "/tmp/r.md"}
    assert has_complete_evidence(record) is False


def test_has_complete_evidence_false_on_unavailable_with_populated_report_path_empty_hash():
    """OI-1477: glm_gate.py/kimi_gate.py now populate ``report_path`` on the
    failure path too (see their OI-1178/OI-1435/OI-1477 evidence block),
    while ``contract_hash`` stays empty -- that is the exact shape those
    writers now produce. This must never be readable as complete evidence:
    is_terminal() is checked before contract_hash/report_path are ever
    consulted, and "unavailable" is never terminal, so a populated
    report_path alone must not tip this to True."""
    record = {"status": "unavailable", "contract_hash": "", "report_path": "/tmp/glm-gate-pr1696-1787774904.md"}
    assert has_complete_evidence(record) is False


@pytest.mark.parametrize("status", ["pending", "running", "queued", "requested"])
def test_has_complete_evidence_false_on_inflight_status_with_full_evidence(status):
    record = {"status": status, "contract_hash": "abc123", "report_path": "/tmp/r.md"}
    assert has_complete_evidence(record) is False


@pytest.mark.parametrize("status", ["pass", "completed", "failed", "not_executable"])
def test_has_complete_evidence_true_on_terminal_status_with_full_evidence(status):
    record = {"status": status, "contract_hash": "abc123", "report_path": "/tmp/r.md"}
    assert has_complete_evidence(record) is True


# ---------------------------------------------------------------------------
# has_complete_evidence: three buckets — absent, empty, sentinel (OI-1435)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        # bucket 1: field ABSENT entirely
        {"status": "pass", "report_path": "/tmp/r.md"},
        # bucket 2: field present but EMPTY/whitespace
        {"status": "pass", "contract_hash": "   ", "report_path": "/tmp/r.md"},
        # bucket 3: field carries a SENTINEL placeholder, not a real value
        {"status": "pass", "contract_hash": "unknown", "report_path": "/tmp/r.md"},
        {"status": "pass", "contract_hash": "None", "report_path": "/tmp/r.md"},
        {"status": "pass", "contract_hash": "null", "report_path": "/tmp/r.md"},
    ],
)
def test_has_complete_evidence_false_on_contract_hash_absent_empty_or_sentinel(result):
    assert has_complete_evidence(result) is False


@pytest.mark.parametrize(
    "result",
    [
        # bucket 1: field ABSENT entirely
        {"status": "pass", "contract_hash": "abc123"},
        # bucket 2: field present but EMPTY/whitespace
        {"status": "pass", "contract_hash": "abc123", "report_path": " "},
        # bucket 3: field carries a SENTINEL placeholder, not a real value
        {"status": "pass", "contract_hash": "abc123", "report_path": "unknown"},
        {"status": "pass", "contract_hash": "abc123", "report_path": "NONE"},
        {"status": "pass", "contract_hash": "abc123", "report_path": "NULL"},
    ],
)
def test_has_complete_evidence_false_on_report_path_absent_empty_or_sentinel(result):
    assert has_complete_evidence(result) is False


# ---------------------------------------------------------------------------
# record_failure: execution-level reason -> unavailable, never failed
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder_env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    requests_dir = state_dir / "review_gates" / "requests"
    results_dir = state_dir / "review_gates" / "results"
    for d in (requests_dir, results_dir):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    return {"requests_dir": requests_dir, "results_dir": results_dir}


def _failure_result(reason="exit_nonzero", reason_detail="Subprocess exited with code 1"):
    return {
        "reason": reason,
        "reason_detail": reason_detail,
        "duration_seconds": 1.0,
        "partial_output_lines": 0,
        "runner_pid": os.getpid(),
    }


@pytest.mark.parametrize("reason", sorted(EXECUTION_FAILURE_REASONS))
def test_record_failure_execution_reason_books_unavailable(recorder_env, reason):
    """Every execution-level reason books unavailable with an unmistakable summary."""
    out = record_failure(
        gate="codex_gate",
        pr_number=42,
        pr_id="",
        result=_failure_result(reason=reason, reason_detail=f"boom: {reason}"),
        request_payload={"gate": "codex_gate", "status": "requested"},
        requests_dir=recorder_env["requests_dir"],
        results_dir=recorder_env["results_dir"],
    )
    assert out["status"] == "unavailable", reason
    assert "UNAVAILABLE" in out["summary"]
    assert "NOT a review fail" in out["summary"]
    assert out["report_path"] == ""
    assert out["blocking_findings"] == []
    # The non-execution is not a pass and carries no complete evidence.
    assert is_pass(out)[0] is False
    assert has_complete_evidence(out) is False


def test_record_failure_non_execution_reason_still_books_failed(recorder_env):
    """A real verdict failure (not an infra reason) must still book failed."""
    out = record_failure(
        gate="gemini_review",
        pr_number=42,
        pr_id="",
        result=_failure_result(reason="review_verdict_blocked", reason_detail="blocked"),
        request_payload={"gate": "gemini_review", "status": "requested"},
        requests_dir=recorder_env["requests_dir"],
        results_dir=recorder_env["results_dir"],
    )
    assert out["status"] == "failed"
    assert "UNAVAILABLE" not in out["summary"]


# ---------------------------------------------------------------------------
# _execute_requested_gates: required non-pass must set has_required_failure
# ---------------------------------------------------------------------------


def _make_executor(exec_results):
    mixin = GateExecutorMixin()

    def fake_execute_gate(gate, pr_number, pr_id=""):
        return exec_results[gate]

    mixin.execute_gate = fake_execute_gate
    return mixin


def test_executed_unavailable_sets_required_failure():
    """An executed gate that booked unavailable must block the rollup (OI-1178)."""
    unavailable = {
        "status": "unavailable",
        "reason": "exit_nonzero",
        "contract_hash": "",
        "report_path": "",
        "blocking_findings": [],
    }
    mixin = _make_executor({"codex_gate": unavailable})
    gates, has_required_failure = mixin._execute_requested_gates(
        {"requested": [{"gate": "codex_gate", "status": "requested", "required": True}]},
        pr_number=1481,
    )
    assert has_required_failure is True
    assert gates[0]["passed"] is False
    assert "unavailable" in gates[0]["pass_reason"]


def test_executed_real_fail_sets_required_failure():
    """A gate that ran and found a blockade (failed) must block the rollup."""
    failed = {
        "status": "failed",
        "reason": "verdict",
        "contract_hash": "abc",
        "report_path": "/tmp/report.md",
        "blocking_findings": [{"severity": "error", "title": "x"}],
    }
    mixin = _make_executor({"codex_gate": failed})
    gates, has_required_failure = mixin._execute_requested_gates(
        {"requested": [{"gate": "codex_gate", "status": "requested", "required": True}]},
        pr_number=1481,
    )
    assert has_required_failure is True
    assert gates[0]["passed"] is False


def test_executed_completed_pass_with_evidence_does_not_fail():
    """A real completed pass with evidence must NOT set has_required_failure."""
    passed = {
        "status": "completed",
        "contract_hash": "abc",
        "report_path": "/tmp/report.md",
        "blocking_findings": [],
    }
    mixin = _make_executor({"codex_gate": passed})
    gates, has_required_failure = mixin._execute_requested_gates(
        {"requested": [{"gate": "codex_gate", "status": "requested", "required": True}]},
        pr_number=1481,
    )
    assert has_required_failure is False
    assert gates[0]["passed"] is True


def test_executed_pass_without_evidence_is_not_a_pass():
    """A completed status with empty evidence must not sum up as PASS."""
    hollow_pass = {
        "status": "completed",
        "contract_hash": "",
        "report_path": "",
        "blocking_findings": [],
    }
    mixin = _make_executor({"codex_gate": hollow_pass})
    gates, has_required_failure = mixin._execute_requested_gates(
        {"requested": [{"gate": "codex_gate", "status": "requested", "required": True}]},
        pr_number=1481,
    )
    assert has_required_failure is True
    assert gates[0]["passed"] is False


def test_optional_claude_github_failure_does_not_fail():
    """claude_github_optional stays optional: its non-pass never blocks."""
    unavailable = {"status": "unavailable", "contract_hash": "", "report_path": ""}
    mixin = _make_executor({"claude_github_optional": unavailable})
    gates, has_required_failure = mixin._execute_requested_gates(
        {"requested": [{"gate": "claude_github_optional", "status": "requested", "required": True}]},
        pr_number=1481,
    )
    assert has_required_failure is False
    assert gates[0]["passed"] is False


def test_not_executable_branch_still_blocks():
    """The pre-existing else-branch behavior for not_executable is preserved."""
    mixin = GateExecutorMixin()
    gates, has_required_failure = mixin._execute_requested_gates(
        {"requested": [{"gate": "codex_gate", "status": "not_executable", "required": True}]},
        pr_number=1481,
    )
    assert has_required_failure is True


@pytest.mark.parametrize(
    "status",
    [
        "blocked",          # OI-1265: unknown gate fallback books this status
        "weird_status",     # a status not in any closed list must still count
        "fail",             # a pre-booked verdict failure is a non-pass
    ],
)
def test_required_non_pass_status_blocks_else_branch(status):
    """A required gate pre-booked with any non-pass status must set
    has_required_failure (OI-1265). The count is inverted: everything that is
    NOT a pass blocks, instead of a closed list of known failing statuses."""
    mixin = GateExecutorMixin()
    gates, has_required_failure = mixin._execute_requested_gates(
        {"requested": [{"gate": "ci_gate", "status": status, "required": True}]},
        pr_number=1265,
    )
    assert has_required_failure is True
    assert gates[0]["passed"] is False


def test_required_pass_status_does_not_block_else_branch():
    """A required gate pre-booked as a pass must NOT set has_required_failure."""
    mixin = GateExecutorMixin()
    gates, has_required_failure = mixin._execute_requested_gates(
        {"requested": [{"gate": "wiring_gate", "status": "pass", "required": True}]},
        pr_number=1265,
    )
    assert has_required_failure is False
    assert gates[0]["passed"] is True


def test_optional_unknown_status_does_not_block_else_branch():
    """claude_github_optional stays optional even with a non-pass status."""
    mixin = GateExecutorMixin()
    gates, has_required_failure = mixin._execute_requested_gates(
        {"requested": [{"gate": "claude_github_optional", "status": "blocked", "required": True}]},
        pr_number=1265,
    )
    assert has_required_failure is False
    assert gates[0]["passed"] is False
