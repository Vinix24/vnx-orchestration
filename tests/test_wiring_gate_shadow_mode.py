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

GE-7 (dlv-20e9ed688893) changed two things this file pins, both deliberately:
**the status word** — ADR-046 §1 has four statuses and no fifth "advisory", so
the record books ``fail`` and carries the non-blocking decision on
``required: false`` — and **the extra booking**: one result record, one
``review_gate_result`` receipt with ``commit_sha`` and the request's end, through
``commit_gate_outcome``. The published check is ``failure`` on
``vnx-gate/wiring_gate``, a context branch protection does not require.
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

PR = 1590
HEAD_SHA = "c" * 40


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
    # GE-7: the gate is decided at request time and books its outcome, so the head
    # is the writer's own SSOT lookup (patched where it is looked up) and nothing
    # may leave the machine — the two forge hooks, as review_stack_support does.
    import forge_gate_publisher
    import gate_request_handler
    monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda _pr: HEAD_SHA)
    monkeypatch.setattr(forge_gate_publisher, "publish_for_record", lambda *a, **kw: None)
    monkeypatch.setattr(forge_gate_publisher, "publish_review_summary", lambda *a, **kw: None)
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


def _receipts(state_dir) -> list:
    """Every ``review_gate_result`` receipt in the store, from both ledgers."""
    rows = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):
        path = state_dir / name
        if path.exists():
            rows += [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]
    return [row for row in rows if row.get("event_type") == "review_gate_result"]


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


def test_shadow_mode_finding_with_findings_does_not_block(manager_env, monkeypatch):
    """VNX_WIRING_GATE_REQUIRED=0: a finding + required=False -> no gate, no required failure.

    Red without the OI-1265 fix: no ``required`` field, so the executor's
    ``req.get("required", True)`` defaulted to True and the result blocked. GE-7
    changed the status word (``fail`` + ``required: false``) and booked the run;
    the executor's answer is unchanged: not a pass, not a required failure.
    """
    monkeypatch.chdir(manager_env["project_root"])
    monkeypatch.setenv("VNX_WIRING_GATE_REQUIRED", "0")
    manager = _make_manager()

    with patch("wiring_gate.check_pr_wiring", return_value=_unwired_result("advisory")):
        payload = manager._request_wiring_gate(pr_number=PR, branch="fix/shadow")

    assert payload["status"] == "fail", "a finding is a fail; required=False is what keeps it advisory"
    assert payload["required"] is False, "shadow-mode wiring_gate must declare required=False"
    assert payload["blocking_count"] == 0, "the findings keep the shadow posture: advisory only"

    record = json.loads((manager_env["results_dir"] / f"pr-{PR}-wiring_gate.json").read_text())
    assert (record["status"], record["required"], record["attempt_seq"]) == ("fail", False, 1)
    assert record["commit_sha"] == HEAD_SHA, "the record names the head it judged"
    request = json.loads((manager_env["requests_dir"] / f"pr-{PR}-wiring_gate.json").read_text())
    assert request["status"] == "completed", "the request's own end, not the record's status word"
    receipts = _receipts(manager_env["state_dir"])
    assert [(row["gate"], row["gate_status"], row["commit_sha"]) for row in receipts] == [
        ("wiring_gate", "fail", HEAD_SHA)
    ], "a non-blocking gate now books exactly one review_gate_result for the head it judged"

    gates, has_required_failure = manager._execute_requested_gates(
        {"requested": [payload]}, pr_number=PR
    )
    assert has_required_failure is False, "a shadow-mode wiring_gate finding must not be a required failure"
    assert gates[0]["gate"] == "wiring_gate"
    # Not a pass (there ARE findings), and it must not gate.
    assert gates[0]["passed"] is False


def test_the_published_check_for_a_shadow_mode_finding_cannot_fail_the_merge(manager_env, monkeypatch):
    """``required=false`` keeps it non-blocking on the publisher's side too.

    The publisher never reads ``required``: ``fail`` maps to ``failure``, but on
    ``vnx-gate/wiring_gate``, which branch protection does not require, and
    ``wiring_gate`` is not a review peer, so it can neither fill nor block the
    required ``vnx-gate/review`` summary.
    """
    import forge_gate_publisher
    from forge_protection_drift import PROTECTION_YAML_RELATIVE_PATH, load_protection_config

    monkeypatch.chdir(manager_env["project_root"])
    monkeypatch.setenv("VNX_WIRING_GATE_REQUIRED", "0")
    manager = _make_manager()
    with patch("wiring_gate.check_pr_wiring", return_value=_unwired_result("advisory")):
        manager._request_wiring_gate(pr_number=PR, branch="fix/shadow")

    record = json.loads((manager_env["results_dir"] / f"pr-{PR}-wiring_gate.json").read_text())
    verdict = forge_gate_publisher.classify_record(record, HEAD_SHA)
    assert verdict.conclusion == forge_gate_publisher.CONCLUSION_FAILURE

    required = {check.context for check in load_protection_config(VNX_ROOT / PROTECTION_YAML_RELATIVE_PATH).checks}
    assert forge_gate_publisher.check_run_name("wiring_gate") not in required
    assert "vnx-gate/review" in required, "the merge-blocking check is the review summary"

    from closure_verifier import _REVIEW_PEER_GATES
    assert "wiring_gate" not in _REVIEW_PEER_GATES, "a non-peer can neither fill nor block the summary"


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
