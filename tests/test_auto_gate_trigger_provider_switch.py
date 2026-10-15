"""auto_gate_trigger names a provider-switch refusal instead of booking a failed gate
(provider-schakelaar C).

``_trigger_gate`` runs ``review_gate_manager request-and-execute`` for one gate. When that call
fails only because the operator's switch refused every seat that did not pass, the trigger books
``provider_switched_off``; any other failure stays ``failed``. ``subprocess.run`` is patched with
the JSON the real command prints, so no gate and no provider is started.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import auto_gate_trigger  # after the sys.path insert above

SWITCHED_OFF = "provider_switched_off"


def _switched_off_seat(gate: str = "kimi_gate") -> Dict[str, Any]:
    return {"gate": gate, "request_status": "not_executable", "execution_status": "not_executable",
            "passed": False, "reason": SWITCHED_OFF, "detail": {"gate": gate, "reason": SWITCHED_OFF}}


def _chain_exhausted_seat(*hop_reasons: str) -> Dict[str, Any]:
    gates = ["kimi_gate", "deepseek_gate", "glm_gate"]
    return {"gate": "kimi_gate", "request_status": "chain_exhausted", "execution_status": "chain_exhausted",
            "passed": False, "reason": "takeover_chain_exhausted",
            "detail": {"gate": "kimi_gate", "status": "chain_exhausted",
                       "takeover_path": [{"gate": g, "reason": r} for g, r in zip(gates, hop_reasons)]}}


def _passed_seat(gate: str = "codex_gate") -> Dict[str, Any]:
    return {"gate": gate, "request_status": "requested", "execution_status": "completed", "passed": True,
            "detail": {"gate": gate, "status": "completed"}}


def _failed_seat(gate: str = "deepseek_gate") -> Dict[str, Any]:
    return {"gate": gate, "request_status": "requested", "execution_status": "completed", "passed": False,
            "detail": {"gate": gate, "status": "completed", "blocking_findings": [{"title": "defect"}]}}


@pytest.fixture
def manager_output(monkeypatch):
    """Patch the request-and-execute subprocess: exit code and JSON stdout per test."""
    state: Dict[str, Any] = {"calls": []}

    def fake_run(cmd, **kwargs):
        state["calls"].append(cmd)
        return subprocess.CompletedProcess(cmd, state["rc"], stdout=state["stdout"], stderr="ERROR: not PASS")

    monkeypatch.setattr(auto_gate_trigger.subprocess, "run", fake_run)
    return state


@pytest.mark.parametrize("gates", [
    [_passed_seat(), _switched_off_seat()],
    [_switched_off_seat()],
    [_passed_seat(), _chain_exhausted_seat(SWITCHED_OFF, SWITCHED_OFF, SWITCHED_OFF)],
    [_passed_seat(), {**_failed_seat("kimi_gate"),
                      "detail": {"gate": "kimi_gate", "status": "not_executable", "reason": SWITCHED_OFF}}],
])
def test_a_call_refused_only_by_the_switch_is_booked_switched_off(manager_output, gates):
    manager_output.update(rc=1, stdout=json.dumps({"has_required_failure": True, "gates": gates}))
    assert auto_gate_trigger._trigger_gate(42, "feat/x", "kimi_gate") == auto_gate_trigger.SWITCHED_OFF
    assert manager_output["calls"][0][manager_output["calls"][0].index("--review-stack") + 1] == "kimi_gate"


@pytest.mark.parametrize("stdout", [
    json.dumps({"has_required_failure": True, "gates": [_switched_off_seat(), _failed_seat()]}),
    json.dumps({"has_required_failure": True,
                "gates": [_chain_exhausted_seat(SWITCHED_OFF, "provider_quota_exhausted", SWITCHED_OFF)]}),
    json.dumps({"has_required_failure": True, "gates": [_failed_seat()]}),
    json.dumps({"has_required_failure": True, "gates": []}),
    "Traceback (most recent call last): boom",
])
def test_any_other_failure_stays_failed(manager_output, stdout):
    manager_output.update(rc=1, stdout=stdout)
    assert auto_gate_trigger._trigger_gate(42, "feat/x", "kimi_gate") == auto_gate_trigger.FAILED


def test_exit_zero_is_triggered(manager_output):
    manager_output.update(rc=0, stdout=json.dumps({"has_required_failure": False, "gates": [_passed_seat()]}))
    assert auto_gate_trigger._trigger_gate(42, "feat/x", "codex_gate") == auto_gate_trigger.TRIGGERED


def test_feature_trigger_books_a_switched_off_gate_apart_from_failed_ones(tmp_path, monkeypatch):
    import governance_audit
    import gh_pr_ensure

    outcomes = {"codex_gate": auto_gate_trigger.TRIGGERED, "kimi_gate": auto_gate_trigger.SWITCHED_OFF,
                "ci_gate": auto_gate_trigger.FAILED}
    logged: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []

    class _State:
        status, completed_prs, total_prs, completion_pct = "completed", 2, 2, 100

    class _FeatureStateMachine:
        @staticmethod
        def parse_feature_plan(path):
            return _State()

    monkeypatch.setitem(sys.modules, "feature_state_machine", _FeatureStateMachine)
    monkeypatch.setattr(auto_gate_trigger, "_find_feature_plan", lambda state_dir: tmp_path / "FEATURE_PLAN.md")
    monkeypatch.setattr(auto_gate_trigger, "_extract_feature_id_from_plan", lambda plan: "F99")
    monkeypatch.setattr(auto_gate_trigger, "_get_current_branch", lambda: "feat/f99")
    monkeypatch.setattr(auto_gate_trigger, "_load_required_gates", lambda: list(outcomes))
    monkeypatch.setattr(auto_gate_trigger, "_trigger_gate", lambda pr, branch, gate: outcomes[gate])
    monkeypatch.setattr(auto_gate_trigger, "_log_auto_gate_event", lambda data_dir, record: events.append(record))
    monkeypatch.setattr(gh_pr_ensure, "ensure_pr", lambda *a, **k: {"pr_number": 77, "created": False})
    monkeypatch.setattr(governance_audit, "log_gate_result", lambda **fields: logged.append(fields))

    result = auto_gate_trigger.trigger_gates_if_feature_complete("F99", tmp_path / "state", dispatch_id="d-1")

    assert result["gates"] == ["codex_gate"]
    assert result["gates_switched_off"] == ["kimi_gate"]
    assert result["gates_failed"] == ["ci_gate"]
    assert events[0]["gates_switched_off"] == ["kimi_gate"]
    assert {entry["gate"]: entry["status"] for entry in logged} == outcomes
