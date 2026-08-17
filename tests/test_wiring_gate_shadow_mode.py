#!/usr/bin/env python3
"""OI-1265 follow-up: wiring_gate shadow-mode advisory must not gate the merge.

The OI-1265 fix in gate_executor inverted the required-failure count: any
required gate that did not pass now blocks, instead of a closed list of known
failing statuses (``not_executable`` / ``not_configured``). That correctly
catches unknown statuses, but it also caught ``advisory`` — the status
wiring_gate books when VNX_WIRING_GATE_REQUIRED=0 (shadow mode). A gate
explicitly in shadow mode must never gate the merge: "advisory" means "I
looked, I report, I do not block".

The fix makes the wiring_gate request payload carry ``required`` derived from
VNX_WIRING_GATE_REQUIRED — the same toggle that decides the gate's status
(check_pr_wiring: ``status="fail" if required else "advisory"``). The
executor's existing ``req.get("required", True)`` then lets the gate declare
its own blocking posture. A future shadow-mode gate does the same: set
``required: False`` on its payload, no status list to update.

Three invariants pinned here:
- shadow mode (VNX_WIRING_GATE_REQUIRED=0): advisory + findings -> required=False
  -> NOT a required failure.
- blocking mode (VNX_WIRING_GATE_REQUIRED=1): fail -> required=True -> IS a
  required failure.
- OI-1265 survives: a required gate (required=True) with an unexpected/unknown
  status still counts as a required failure. This is what stops the fix from
  degrading into a blanket "advisory never blocks" exclusion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gate_executor import GateExecutorMixin


@pytest.fixture
def manager_env(tmp_path, monkeypatch):
    """A fake VNX_HOME/state tree, enough to instantiate ReviewGateManager."""
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    return {
        "project_root": project_root,
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


def _make_manager():
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


def _unwired_result(status: str):
    """A WiringGateResult with one unwired symbol, mirroring check_pr_wiring."""
    from wiring_gate import UnwiredSymbol, WiringGateResult
    return WiringGateResult(
        status=status,
        unwired=[UnwiredSymbol(name="orphan_fn", file="scripts/foo.py", line=12, kind="function")],
        total_checked=1,
        summary="1 unwired symbol(s): orphan_fn",
    )


# ---------------------------------------------------------------------------
# Shadow mode: advisory must not gate
# ---------------------------------------------------------------------------


def test_shadow_mode_advisory_with_findings_does_not_block(manager_env, monkeypatch):
    """VNX_WIRING_GATE_REQUIRED=0: advisory + findings -> required=False, no gate.

    Red without the fix: the payload carried no ``required`` field, so the
    executor's ``req.get("required", True)`` defaulted to True and the advisory
    result blocked the merge.
    """
    monkeypatch.chdir(manager_env["project_root"])
    monkeypatch.setenv("VNX_WIRING_GATE_REQUIRED", "0")
    manager = _make_manager()

    with patch("wiring_gate.check_pr_wiring", return_value=_unwired_result("advisory")):
        payload = manager._request_wiring_gate(pr_number=1590, branch="fix/shadow")

    assert payload["status"] == "advisory"
    assert payload["required"] is False, "shadow-mode wiring_gate must declare required=False"
    assert payload["blocking_count"] == 0

    gates, has_required_failure = manager._execute_requested_gates(
        {"requested": [payload]}, pr_number=1590
    )
    assert has_required_failure is False, "advisory wiring_gate must not be a required failure"
    assert gates[0]["gate"] == "wiring_gate"
    # Advisory is not a pass (there ARE findings), but it must not gate.
    assert gates[0]["passed"] is False


def test_error_branch_in_shadow_mode_does_not_block(manager_env, monkeypatch):
    """A wiring subprocess failure still honors shadow mode: required=False.

    The gate reports the error loudly (status=fail, blocking_findings) but a
    shadow-mode gate never gates the merge.
    """
    monkeypatch.chdir(manager_env["project_root"])
    monkeypatch.setenv("VNX_WIRING_GATE_REQUIRED", "0")
    manager = _make_manager()

    from wiring_gate import WiringGateError
    with patch("wiring_gate.check_pr_wiring", side_effect=WiringGateError("gh pr diff failed")):
        payload = manager._request_wiring_gate(pr_number=1590, branch="fix/error")

    assert payload["status"] == "fail"
    assert payload["required"] is False

    _gates, has_required_failure = manager._execute_requested_gates(
        {"requested": [payload]}, pr_number=1590
    )
    assert has_required_failure is False


def test_not_executable_in_shadow_mode_does_not_block(manager_env, monkeypatch):
    """gh missing in shadow mode (not_executable) must not gate either."""
    monkeypatch.chdir(manager_env["project_root"])
    monkeypatch.setenv("VNX_WIRING_GATE_REQUIRED", "0")
    manager = _make_manager()
    monkeypatch.setattr(manager, "_wiring_gate_available", lambda: False)

    payload = manager._request_wiring_gate(pr_number=1590, branch="fix/gh-missing")

    assert payload["status"] == "not_executable"
    assert payload["required"] is False

    _gates, has_required_failure = manager._execute_requested_gates(
        {"requested": [payload]}, pr_number=1590
    )
    assert has_required_failure is False


# ---------------------------------------------------------------------------
# Required mode: the same gate must gate
# ---------------------------------------------------------------------------


def test_required_mode_fail_blocks(manager_env, monkeypatch):
    """VNX_WIRING_GATE_REQUIRED=1: fail + findings -> required=True, gates.

    The payload-contract assert (``required is True``) is red without the fix:
    the field was absent entirely.
    """
    monkeypatch.chdir(manager_env["project_root"])
    monkeypatch.setenv("VNX_WIRING_GATE_REQUIRED", "1")
    manager = _make_manager()

    with patch("wiring_gate.check_pr_wiring", return_value=_unwired_result("fail")):
        payload = manager._request_wiring_gate(pr_number=1590, branch="fix/blocking")

    assert payload["status"] == "fail"
    assert payload["required"] is True, "required-mode wiring_gate must declare required=True"
    assert payload["blocking_count"] == 1

    gates, has_required_failure = manager._execute_requested_gates(
        {"requested": [payload]}, pr_number=1590
    )
    assert has_required_failure is True
    assert gates[0]["passed"] is False


# ---------------------------------------------------------------------------
# OI-1265 preserved: required gate + unexpected/unknown status still blocks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["advisory", "weird_status"])
def test_required_gate_unexpected_status_still_blocks(status):
    """A required gate with an unexpected/unknown status must still gate.

    ``advisory`` here is the trap the fix must avoid: excluding advisory by name
    would make a gate that is explicitly required (required=True) silently
    non-blocking. The OI-1265 inversion — count what did NOT pass — stays.
    """
    mixin = GateExecutorMixin()
    gates, has_required_failure = mixin._execute_requested_gates(
        {"requested": [{"gate": "wiring_gate", "status": status, "required": True}]},
        pr_number=1590,
    )
    assert has_required_failure is True
    assert gates[0]["passed"] is False
