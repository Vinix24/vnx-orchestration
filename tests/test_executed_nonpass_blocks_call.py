"""Regression: an executed gate that does not decide PASS must fail the call.

Measured live: a per-PR request for an ordinary change carries
``required: false`` on its codex_gate record -- ``auto_merge_policy`` only
forces the seat for ``mode == "final"`` or a governance-path change. The gate
executed, reported one blocking finding and ``passed: false``, yet
``request-and-execute`` exited 0: ``_execute_requested_gates`` consulted
``required`` before flipping ``has_required_failure``, and
``_handle_request_and_execute`` derives its exit code from that flag alone.

The exit code is what ``vnx gate``, ``scripts/t0_gate_enforcement.sh`` and the
orchestrator loops act on, so every loop that trusted it counted a blocker as
a PASS.

The fix scopes the exemption correctly: an EXECUTED gate blocks on its own
verdict whatever ``required`` says, while ``required`` keeps its merge-policy
meaning on the request records. The exemptions stay where they already live --
``claude_github_optional`` by name, and advisory gates on the pre-booked
branch (wiring_gate shadow mode books status ``advisory``, never
``requested``).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_recorder  # noqa: E402
import review_gate_manager as rgm  # noqa: E402
from gate_executor import GateExecutorMixin  # noqa: E402

PR = 42
BRANCH = "feat/ordinary-change"
HEAD_SHA = "abc1234def5678abc1234def5678abc1234def5"


class _FakeManager(GateExecutorMixin):
    """A manager whose review stack resolves to canned request/exec results."""

    def __init__(self, requested, exec_results):
        self._requested = requested
        self._exec_results = exec_results

    def request_reviews(self, **kwargs):  # noqa: ANN003
        return {"requested": self._requested}

    def execute_gate(self, *, gate, pr_number, pr_id=""):
        return self._exec_results[gate]


def _args(review_stack: str) -> argparse.Namespace:
    return argparse.Namespace(
        pr=PR,
        branch=BRANCH,
        review_stack=review_stack,
        risk_class="medium",
        # Non-empty changed-files keeps the handler off the git-diff fallback.
        changed_files="scripts/lib/example.py",
        mode="per_pr",
        dispatch_id="",
    )


def _requested_entry(gate="codex_gate", *, required, status="requested"):
    return {"gate": gate, "status": status, "required": required}


def _blocker(commit_sha=HEAD_SHA):
    """An executed codex_gate that ran and found one blocking finding."""
    return {
        "gate": "codex_gate",
        "status": "completed",
        "commit_sha": commit_sha,
        "contract_hash": "088a30754169bb91",
        "report_path": "/tmp/codex-gate-pr42.md",
        "blocking_findings": [{"severity": "blocking", "title": "return value changed"}],
        "blocking_count": 1,
        "advisory_findings": [],
    }


def _evidenced_pass(commit_sha=HEAD_SHA):
    return {
        "gate": "codex_gate",
        "status": "completed",
        "commit_sha": commit_sha,
        "contract_hash": "088a30754169bb91",
        "report_path": "/tmp/codex-gate-pr42.md",
        "blocking_findings": [],
        "blocking_count": 0,
        "advisory_findings": [],
    }


@pytest.fixture(autouse=True)
def _pin_head(monkeypatch):
    monkeypatch.setattr(gate_recorder, "get_pr_head_sha", lambda _n: HEAD_SHA)


# ---------------------------------------------------------------------------
# Criterion 1: an executed blocker with required=false fails the call
# ---------------------------------------------------------------------------


def test_executed_blocker_with_required_false_fails_the_call(capsys):
    """The measured defect: required=false must not silence an executed blocker."""
    manager = _FakeManager(
        [_requested_entry(required=False)], {"codex_gate": _blocker()},
    )

    rc = rgm._handle_request_and_execute(manager, _args("codex_gate"))

    assert rc == 1, (
        "an executed codex_gate that returned a blocking finding must fail "
        "request-and-execute even though the merge policy stamped required=false"
    )
    captured = capsys.readouterr()
    assert "codex_gate" in captured.err, "the failing gate must be named on stderr"


def test_executed_blocker_with_required_false_sets_required_failure():
    """The rollup flag itself, at the unit level."""
    manager = _FakeManager(
        [_requested_entry(required=False)], {"codex_gate": _blocker()},
    )

    gates, has_required_failure = manager._execute_requested_gates(
        {"requested": [_requested_entry(required=False)]}, PR,
    )

    assert gates[0]["passed"] is False
    assert has_required_failure is True


# ---------------------------------------------------------------------------
# Criterion 3: a decided PASS on the PR head still exits 0
# ---------------------------------------------------------------------------


def test_executed_evidenced_pass_with_required_false_exits_zero(capsys):
    manager = _FakeManager(
        [_requested_entry(required=False)], {"codex_gate": _evidenced_pass()},
    )

    rc = rgm._handle_request_and_execute(manager, _args("codex_gate"))

    assert rc == 0
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Criterion 2: the exemptions stay
# ---------------------------------------------------------------------------


def test_claude_github_optional_nonpass_still_exits_zero(capsys):
    """claude_github_optional is never a seat this call's success depends on."""
    manager = _FakeManager(
        [_requested_entry("claude_github_optional", required=False)],
        {"claude_github_optional": {
            "gate": "claude_github_optional",
            "status": "unavailable",
            "contract_hash": "",
            "report_path": "",
            "blocking_findings": [],
        }},
    )

    rc = rgm._handle_request_and_execute(manager, _args("claude_github_optional"))

    assert rc == 0
    assert capsys.readouterr().err == ""


def test_advisory_wiring_gate_prebooked_with_required_false_exits_zero(capsys):
    """wiring_gate shadow mode books status=advisory, never "requested".

    The advisory exemption lives on the pre-booked branch, so it is untouched
    by the executed-gate fix: required=false still means "does not gate".
    """
    manager = _FakeManager(
        [_requested_entry("wiring_gate", required=False, status="advisory")],
        {},
    )

    rc = rgm._handle_request_and_execute(manager, _args("wiring_gate"))

    assert rc == 0
    assert capsys.readouterr().err == ""
