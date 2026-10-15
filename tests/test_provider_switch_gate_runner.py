"""gate_runner honors the provider switch (provider-schakelaar C).

Layer 1: ``GateRunner._run_with_stall_detection`` calls ``provider_switch.require`` with the gate
name before its ``subprocess.Popen``. ``GateRunner.run`` decides before anything is marked
executing, on every path. The harness lane maps the lane's ``EXIT_PROVIDER_SWITCHED_OFF`` (and a
report that names the refusal) to a ``provider_switched_off`` record instead of a dispatch error.

Every launch primitive is patched: ``gate_runner.subprocess.Popen``, the harness dispatcher
(``plan_gate_panel._make_default_dispatcher``), ``fetch_branch`` and the main-checkout snapshot.
The switch file is a tmp file (``VNX_PROVIDER_SWITCH_FILE``), the store a tmp dir.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gate_recorder  # noqa: E402
import gate_runner  # noqa: E402
import provider_reachability  # noqa: E402
import provider_switch  # noqa: E402
from gate_runner import GateRunner  # noqa: E402

PR = 7312
HEAD = "e" * 40
SWITCHED_OFF = "provider_switched_off"


def set_switches(**families: bool) -> None:
    path = Path(os.environ["VNX_PROVIDER_SWITCH_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "global": {
        family: {"enabled": enabled, "changed_at": "2026-10-15T08:00:00Z", "changed_by": "op",
                 "reason": "insufficient balance"}
        for family, enabled in families.items()
    }}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_ambient_override(monkeypatch):
    for key in [k for k in os.environ if k.startswith("VNX_OVERRIDE_PROVIDER_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    data_dir = tmp_path / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    (state_dir / "review_gates" / "requests").mkdir(parents=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True)
    reports_dir.mkdir(parents=True)
    # The runner decides for the project its own state dir names.
    (tmp_path / ".vnx-project-id").write_text("runner-test\n", encoding="utf-8")
    monkeypatch.setattr(gate_runner, "_capture_main_checkout_state", lambda project_root: {"head": "main"})
    receipts: List[Dict[str, Any]] = []
    monkeypatch.setattr(gate_recorder, "emit_governance_receipt",
                        lambda event, **fields: receipts.append({"event": event, **fields}))
    # A landed result is published as a GitHub check-run; never from a test.
    monkeypatch.setattr(gate_recorder, "publish_forge_check_run", lambda payload, **kw: None)
    return {"state_dir": state_dir, "reports_dir": reports_dir,
            "results_dir": state_dir / "review_gates" / "results",
            "requests_dir": state_dir / "review_gates" / "requests"}


@pytest.fixture
def popen(monkeypatch):
    calls: List[Any] = []

    def refuse_launch(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        raise AssertionError(f"a process was launched: {calls[-1]!r}")

    monkeypatch.setattr(gate_runner.subprocess, "Popen", refuse_launch)
    return calls


@pytest.fixture
def dispatcher(monkeypatch):
    """The harness lane's dispatcher, recording calls; ``behaviour`` decides what it does."""
    state: Dict[str, Any] = {"calls": [], "behaviour": None, "fetched": []}

    def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None):
        def dispatch(provider, model, instruction, dispatch_id):
            state["calls"].append({"provider": provider, "dispatch_id": dispatch_id})
            return state["behaviour"](dispatch_id)
        return dispatch

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
    monkeypatch.setattr(gate_runner, "fetch_branch",
                        lambda branch, **kw: state["fetched"].append(branch))
    return state


def runner(env) -> GateRunner:
    return GateRunner(state_dir=env["state_dir"], reports_dir=env["reports_dir"])


def payload(gate: str) -> Dict[str, Any]:
    return {"gate": gate, "status": "requested", "branch": "feat/switch", "pr_number": PR,
            "commit_sha": HEAD, "dispatch_id": "20261015-ps-c-test", "prompt": "Review this diff"}


def result_record(env, gate: str) -> Dict[str, Any]:
    return json.loads((env["results_dir"] / f"pr-{PR}-{gate}.json").read_text(encoding="utf-8"))


def no_reachability_fact(env) -> bool:
    return not (env["state_dir"] / "provider_reachability").exists()


# ---------------------------------------------------------------------------
# run(): decided before anything is marked executing
# ---------------------------------------------------------------------------

def test_run_refuses_a_switched_off_harness_gate_before_any_dispatch(env, dispatcher):
    set_switches(kimi=False)
    dispatcher["behaviour"] = lambda dispatch_id: pytest.fail("the harness lane was dispatched")

    result = runner(env).run(gate="kimi_gate", request_payload=payload("kimi_gate"), pr_number=PR)

    assert result["status"] == "not_executable" and result["reason"] == SWITCHED_OFF
    assert "family=kimi" in result["reason_detail"] and "consumer=gate_runner" in result["reason_detail"]
    assert dispatcher["calls"] == [] and dispatcher["fetched"] == []
    assert result_record(env, "kimi_gate")["reason"] == SWITCHED_OFF
    request = json.loads((env["requests_dir"] / f"pr-{PR}-kimi_gate.json").read_text(encoding="utf-8"))
    assert request["status"] == "not_executable" and "started_at" not in request
    assert no_reachability_fact(env)


def test_run_refuses_a_switched_off_path_binary_gate_without_popen(env, popen, monkeypatch):
    set_switches(gemini=False)
    monkeypatch.setattr(gate_runner.shutil, "which", lambda name: f"/usr/bin/{name}")

    result = runner(env).run(gate="gemini_review", request_payload=payload("gemini_review"), pr_number=PR)

    assert result["status"] == "not_executable" and result["reason"] == SWITCHED_OFF
    assert popen == []


def test_run_lets_an_allowed_harness_gate_through(env, dispatcher):
    set_switches(kimi=True)
    dispatcher["behaviour"] = lambda dispatch_id: (
        "Reviewed.\n```json\n" + json.dumps({"verdict": "pass", "findings": [], "residual_risk": None}) + "\n```\n"
    )

    result = runner(env).run(gate="kimi_gate", request_payload=payload("kimi_gate"), pr_number=PR)

    assert len(dispatcher["calls"]) == 1
    assert result["status"] != "not_executable"


# ---------------------------------------------------------------------------
# Layer 1: _run_with_stall_detection calls require before its Popen
# ---------------------------------------------------------------------------

def test_layer_one_refuses_before_popen(env, popen):
    set_switches(gemini=False)
    with pytest.raises(provider_switch.ProviderDisabledError) as excinfo:
        runner(env)._run_with_stall_detection(
            gate="gemini_review", binary="gemini", prompt="x", timeout=5, stall_threshold=5,
            request_payload=payload("gemini_review"),
        )
    assert excinfo.value.decision.family == "gemini"
    assert excinfo.value.decision.consumer == "gate_runner"
    assert excinfo.value.decision.scope == "runner-test"
    assert popen == []


def test_a_layer_one_refusal_is_booked_as_the_switch_and_the_worktree_removed(env, popen, monkeypatch):
    """The family switched off between run()'s decision and the launch."""
    removed: List[Path] = []
    worktree = env["state_dir"].parent / "gate-worktree"
    monkeypatch.setattr(gate_runner, "create_gate_worktree", lambda **kw: worktree)
    monkeypatch.setattr(gate_runner, "remove_gate_worktree", lambda path, **kw: removed.append(path))
    set_switches(gemini=False)

    result = runner(env)._run_subprocess_path(
        gate="gemini_review", binary="gemini", prompt="x", pr_number=PR, pr_id="",
        request_payload=payload("gemini_review"),
    )

    assert result["status"] == "not_executable" and result["reason"] == SWITCHED_OFF
    assert removed == [worktree]
    assert popen == []


def test_codex_is_never_refused_by_layer_one(env):
    """Decision 2b: require allows codex whatever the switch file says."""
    Path(os.environ["VNX_PROVIDER_SWITCH_FILE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(os.environ["VNX_PROVIDER_SWITCH_FILE"]).write_text("{broken", encoding="utf-8")
    decision = provider_switch.require("codex_gate", consumer="gate_runner")
    assert decision.allowed and decision.family == "codex"


# ---------------------------------------------------------------------------
# Harness lane: the lane's refusal exit code becomes the switch record
# ---------------------------------------------------------------------------

def _lane_refused(dispatch_id: str) -> str:
    raise RuntimeError(
        f"no report for {dispatch_id} (rc={provider_switch.EXIT_PROVIDER_SWITCHED_OFF}): stdout:  | "
        "stderr: provider_switched_off: kimi family=kimi source=global scope=runner-test "
        "consumer=provider_dispatch reason='insufficient balance'"
    )


def test_lane_exit_provider_switched_off_is_booked_as_the_switch_not_a_dispatch_error(env, dispatcher):
    """kimi was on when run() decided, the lane refused it (exit 4). The refusal is not a
    harness_lane_dispatch_error, so the operator's reason text ("insufficient balance") is never
    read as a quota refusal and never becomes a reachability fact."""
    set_switches(kimi=True)
    dispatcher["behaviour"] = _lane_refused

    result = runner(env).run(gate="kimi_gate", request_payload=payload("kimi_gate"), pr_number=PR)

    assert len(dispatcher["calls"]) == 1
    assert result["status"] == "not_executable" and result["reason"] == SWITCHED_OFF
    assert f"rc={provider_switch.EXIT_PROVIDER_SWITCHED_OFF}" in result["reason_detail"]
    record = result_record(env, "kimi_gate")
    assert record["reason"] == SWITCHED_OFF and "reason_before_classification" not in record
    assert provider_reachability.get("kimi", state_dir=env["state_dir"]).state.value == "unmeasured"
    assert no_reachability_fact(env)


def test_any_other_lane_exit_stays_a_dispatch_error(env, dispatcher):
    set_switches(kimi=True)

    def crashed(dispatch_id):
        raise RuntimeError(f"no report for {dispatch_id} (rc=1): stdout: boom | stderr: ")

    dispatcher["behaviour"] = crashed

    result = runner(env).run(gate="kimi_gate", request_payload=payload("kimi_gate"), pr_number=PR)

    assert result["status"] == "unavailable" and result["reason"] == "harness_lane_dispatch_error"


def test_a_lane_report_that_names_the_refusal_is_booked_as_the_switch(env, dispatcher):
    set_switches(kimi=True)
    dispatcher["behaviour"] = lambda dispatch_id: (
        "---\n"
        f"dispatch_id: {dispatch_id}\n"
        "failure_reason: \"provider_switched_off: kimi family=kimi source=global\"\n"
        "exit_code: 4\n"
        "---\n\nThe provider switch refused the call.\n"
    )

    result = runner(env).run(gate="kimi_gate", request_payload=payload("kimi_gate"), pr_number=PR)

    assert result["status"] == "not_executable" and result["reason"] == SWITCHED_OFF
    assert no_reachability_fact(env)


@pytest.mark.parametrize("message,expected", [
    ("no report for kimi-gate-pr1-2 (rc=4): stdout: x", 4),
    ("no report for kimi-gate-pr1-2 (rc=-9): stdout: x", -9),
    ("no report for kimi-gate-pr1-2 (rc=1 deadline exceeded)", None),
    ("claude_headless_failed (rc=4): x", None),
    ("connection refused", None),
])
def test_dispatch_exit_code_is_read_only_from_the_no_report_message(message, expected):
    assert gate_runner._dispatch_exit_code(RuntimeError(message)) == expected


# ---------------------------------------------------------------------------
# The record: replaceable by the gate's own result, never displacing evidence
# ---------------------------------------------------------------------------

def _switch_note(head: str = HEAD) -> Dict[str, Any]:
    return {"gate": "kimi_gate", "pr_number": PR, "status": "not_executable", "reason": SWITCHED_OFF,
            "reason_detail": "provider_switched_off: kimi_gate family=kimi", "commit_sha": head,
            "contract_hash": "", "report_path": "", "recorded_at": "2026-10-15T08:00:00Z"}


def test_reason_is_a_non_verdict_and_a_runner_refusal():
    assert SWITCHED_OFF in gate_recorder.NON_VERDICT_NOT_EXECUTABLE_REASONS
    assert SWITCHED_OFF in gate_recorder.RUNNER_REFUSAL_NOT_EXECUTABLE_REASONS
    assert gate_recorder.is_routing_announcement(_switch_note()) is True


def test_a_later_live_outage_replaces_the_switch_note_on_the_same_head(env):
    path = env["results_dir"] / f"pr-{PR}-kimi_gate.json"
    path.write_text(json.dumps(_switch_note()), encoding="utf-8")
    outage = {"gate": "kimi_gate", "pr_number": PR, "status": "unavailable", "reason": "timeout",
              "reason_detail": "no answer", "commit_sha": HEAD, "recorded_at": "2026-10-15T09:00:00Z"}
    _on_disk, written = gate_recorder.write_result_guarded(path, outage, gate="kimi_gate", pr_ref=str(PR))
    assert written is True
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "unavailable"


@pytest.mark.parametrize("existing", [
    {"status": "unavailable", "reason": "timeout", "reason_detail": "no answer"},
    {"status": "pass", "contract_hash": "0123456789abcdef", "report_path": "r.md", "blocking_findings": []},
])
def test_a_switch_note_never_displaces_what_the_provider_did_on_this_head(env, existing):
    path = env["results_dir"] / f"pr-{PR}-kimi_gate.json"
    record = {"gate": "kimi_gate", "pr_number": PR, "commit_sha": HEAD, **existing}
    path.write_text(json.dumps(record), encoding="utf-8")
    _on_disk, written = gate_recorder.write_result_guarded(path, _switch_note(), gate="kimi_gate", pr_ref=str(PR))
    assert written is False
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == existing["status"]


def test_a_switch_note_replaces_a_record_about_another_head(env):
    path = env["results_dir"] / f"pr-{PR}-kimi_gate.json"
    path.write_text(json.dumps({"gate": "kimi_gate", "pr_number": PR, "status": "unavailable",
                                "reason": "timeout", "commit_sha": "f" * 40}), encoding="utf-8")
    _on_disk, written = gate_recorder.write_result_guarded(path, _switch_note(), gate="kimi_gate", pr_ref=str(PR))
    assert written is True
