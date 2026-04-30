"""tests/test_gate_status_summarizer_usage.py — CFX-12 summarizer refactor verification.

Verifies that gate_status.is_pass() correctly drives the ci_gate and
claude_github_optional blocks in closure_verifier.py after the CFX-12
ad-hoc check refactor.  Each parameterized case covers a (status,
blocking_count) combination that was previously handled by hand-rolled
comparisons.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from gate_status import is_pass


# ---------------------------------------------------------------------------
# ci_gate result shapes: standard status + blocking_count
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("result,expected_pass", [
    ({"status": "pass", "blocking_count": 0}, True),
    ({"status": "pass", "blocking_count": 1}, False),
    ({"status": "pass", "blocking_count": 3}, False),
    ({"status": "fail", "blocking_count": 0}, False),
    ({"status": "fail", "blocking_count": 2}, False),
    ({"status": "failed", "blocking_count": 0}, False),
    ({"status": "running", "blocking_count": 0}, False),
    ({"status": "completed", "blocking_count": 0}, True),
    ({"status": "completed", "blocking_count": 1}, False),
])
def test_is_pass_ci_gate_shapes(result: Dict[str, Any], expected_pass: bool) -> None:
    passed, reason = is_pass(result)
    assert passed is expected_pass, f"is_pass({result!r}) returned passed={passed!r}, reason={reason!r}"
    if expected_pass:
        assert reason == "passed"
    else:
        assert reason


# ---------------------------------------------------------------------------
# claude_github_optional result shapes: state=completed + result_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("result,expected_pass", [
    ({"state": "completed", "result_status": "pass", "blocking_count": 0}, True),
    ({"state": "completed", "result_status": "pass", "blocking_count": 1}, False),
    ({"state": "completed", "result_status": "fail", "blocking_count": 0}, False),
    ({"state": "completed", "result_status": "fail", "blocking_count": 2}, False),
    ({"state": "completed"}, False),
    ({"state": "completed", "result_status": ""}, False),
    ({"state": "COMPLETED", "result_status": "pass", "blocking_count": 0}, True),
])
def test_is_pass_claude_github_optional_shapes(result: Dict[str, Any], expected_pass: bool) -> None:
    passed, reason = is_pass(result)
    assert passed is expected_pass, f"is_pass({result!r}) returned passed={passed!r}, reason={reason!r}"
    if expected_pass:
        assert reason == "passed"
    else:
        assert reason


# ---------------------------------------------------------------------------
# is_pass() does not regress on standard gate_status contract
# ---------------------------------------------------------------------------

def test_state_completed_result_status_does_not_override_status() -> None:
    """Explicit status field takes precedence over state/result_status."""
    result = {"status": "failed", "state": "completed", "result_status": "pass"}
    passed, reason = is_pass(result)
    assert passed is False
    assert "failed" in reason


def test_state_completed_result_status_pass_with_blocking_findings() -> None:
    """state=completed result_status=pass but blocking_findings present → fail."""
    result = {
        "state": "completed",
        "result_status": "pass",
        "blocking_findings": [{"id": "F1"}],
        "blocking_count": 1,
    }
    passed, reason = is_pass(result)
    assert passed is False
    assert "blocking" in reason
