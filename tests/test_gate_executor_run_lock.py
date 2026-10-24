"""R10 (dispatch dlv-38447134aeec): the run lock where every review-gate run launches.

``GateExecutorMixin.execute_gate`` is the one place a run starts, for every
caller: ``review_gate_manager.py request-and-execute`` and ``execute``, and the
obligation runner through ``request_and_execute``. Pinned here on a real
throwaway store with the real ``ReviewGateManager`` (the provider lane,
``GateRunner.run``, is the edge and is replaced: reaching it while it must not
be reached fails the test):

- a run of the same (PR, gate, head) held by another process: no run, nothing
  written, an ``in_flight`` answer that is no pass, and the CLI says so;
- a verdict that landed on the head after the request: that verdict is the
  answer, no second run;
- a force-rerun's reason travels on the request into the result record.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

import review_stack_support  # puts scripts/ and scripts/lib on sys.path
from review_stack_support import BRANCH, HEAD_SHA, PR, build_project_store, isolate_config_registry

_HOLDER = """
import fcntl, json, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.pwrite(fd, json.dumps({"pid": os.getpid()}).encode("utf-8"), 0)
print("held", flush=True)
sys.stdin.read()
"""


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = build_project_store(tmp_path, monkeypatch)
    import gate_recorder
    import gate_request_handler
    import gate_runner
    import review_gate_manager

    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")
    monkeypatch.setattr(
        gate_request_handler.GateRequestHandlerMixin, "_codex_headless_available", lambda self: True,
    )
    monkeypatch.setattr(review_gate_manager, "emit_governance_receipt", lambda event, **fields: None)
    monkeypatch.setattr(gate_recorder, "emit_governance_receipt", lambda event, **fields: None)
    runs: List[Dict[str, Any]] = []

    def provider_lane(self, *, gate, request_payload, pr_number=None, pr_id=""):
        runs.append({"gate": gate, "request": dict(request_payload)})
        return {"gate": gate, "pr_number": pr_number, "status": "unavailable", "reason": "timeout"}

    monkeypatch.setattr(gate_runner.GateRunner, "run", provider_lane)
    store["runs"] = runs
    store["headless"] = store["reports_dir"] / "headless"
    store["headless"].mkdir(parents=True, exist_ok=True)
    return store


def _manager():
    import review_gate_manager

    return review_gate_manager.ReviewGateManager()


def _request_codex(**kwargs: Any) -> Dict[str, Any]:
    result = _manager().request_reviews(
        pr_number=PR, branch=BRANCH, review_stack=["codex_gate"], risk_class="medium",
        changed_files=["scripts/lib/example.py"], mode="per_pr", dispatch_id="dlv-r10-exec", **kwargs,
    )
    return result["requested"][0]


def _lock_path(env) -> Path:
    import gate_run_guard

    return gate_run_guard.run_lock_path(env["state_dir"], pr_ref=PR, gate="codex_gate", head_sha=HEAD_SHA)


def _hold_elsewhere(path: Path) -> subprocess.Popen:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


def _release(proc: subprocess.Popen) -> None:
    proc.stdin.close()
    proc.wait(timeout=10)


class TestALaunchInFlight:
    def test_execute_gate_does_not_run_a_head_another_process_is_reviewing(self, env):
        assert _request_codex()["status"] == "requested"
        request_file = env["requests_dir"] / f"pr-{PR}-codex_gate.json"
        request_before = request_file.read_text(encoding="utf-8")
        proc = _hold_elsewhere(_lock_path(env))
        try:
            answer = _manager().execute_gate(gate="codex_gate", pr_number=PR)
        finally:
            _release(proc)

        assert env["runs"] == [], "the provider lane is never reached for a head in flight"
        assert answer["status"] == "in_flight" and answer["holder_pid"] == proc.pid
        assert answer["reason"] == "gate_run_in_flight"
        assert request_file.read_text(encoding="utf-8") == request_before, "nothing is written"
        assert not (env["results_dir"] / f"pr-{PR}-codex_gate.json").exists()

    def test_the_head_runs_once_the_other_run_is_over_and_leaves_no_lock_file(self, env):
        _request_codex()
        _release(_hold_elsewhere(_lock_path(env)))

        _manager().execute_gate(gate="codex_gate", pr_number=PR)

        assert [run["gate"] for run in env["runs"]] == ["codex_gate"]
        assert not _lock_path(env).exists()

    def test_request_and_execute_reports_the_seat_in_flight_as_no_pass(self, env, monkeypatch, capsys):
        import argparse

        import review_gate_manager

        proc = _hold_elsewhere(_lock_path(env))
        try:
            rc = review_gate_manager._handle_request_and_execute(_manager(), argparse.Namespace(
                pr=PR, branch=BRANCH, review_stack="codex_gate", risk_class="medium",
                changed_files="scripts/lib/example.py", mode="per_pr", dispatch_id="", override_b3=False,
                reason=None,
            ))
        finally:
            _release(proc)

        assert env["runs"] == []
        assert rc == 1
        err = capsys.readouterr().err
        assert "IN FLIGHT (gate_run_in_flight) codex_gate" in err and str(proc.pid) in err

    def test_the_execute_cli_exits_non_zero_when_the_gate_is_in_flight(self, env, capsys):
        import review_gate_manager

        _request_codex()
        proc = _hold_elsewhere(_lock_path(env))
        try:
            rc = review_gate_manager.main(["execute", "--gate", "codex_gate", "--pr", str(PR)])
        finally:
            _release(proc)

        assert rc == 1
        assert env["runs"] == []
        assert "IN FLIGHT" in capsys.readouterr().err


class TestAVerdictThatLandedMeanwhile:
    def test_a_verdict_booked_on_the_head_after_the_request_is_the_answer(self, env):
        import gate_recorder

        assert _request_codex()["status"] == "requested", "the request opens the PR and asks codex"
        report = env["headless"] / f"20261022-100001-HEADLESS-codex_gate-pr-{PR}-01ab.md"
        report.write_text("# codex_gate\n\n## Findings\n\nBlocking count: 0\n", encoding="utf-8")
        verdict = {
            "gate": "codex_gate", "pr_id": str(PR), "pr_number": PR, "status": "completed",
            "summary": "codex_gate execution completed successfully", "contract_hash": "codex-01",
            "report_path": str(report), "findings": [], "blocking_findings": [], "advisory_findings": [],
            "commit_sha": HEAD_SHA, "branch": BRANCH, "dispatch_id": "dlv-r10-other-run",
            "recorded_at": "2026-10-22T10:00:01Z", "seat": "codex_gate",
        }
        _payload, written = gate_recorder.write_result_guarded(
            env["results_dir"] / f"pr-{PR}-codex_gate.json", verdict, gate="codex_gate", pr_ref=str(PR),
        )
        assert written, "the other run's verdict is booked in the round ledger and on disk"

        answer = _manager().execute_gate(gate="codex_gate", pr_number=PR)

        assert env["runs"] == [], "no second reviewer on a head whose seat has a verdict"
        assert answer["status"] == "completed" and answer["contract_hash"] == "codex-01"
        assert "second run on a head with a verdict is refused" in answer["redispatch_refused"]


class TestForceRerunReasonTravels:
    def test_the_reason_is_on_the_request_and_reaches_the_result_record(self, env):
        import gate_recorder

        request = _request_codex(force_rerun="re-review after the codex config fix")

        on_disk = json.loads((env["requests_dir"] / f"pr-{PR}-codex_gate.json").read_text(encoding="utf-8"))
        assert request["force_rerun"]["reason"] == "re-review after the codex config fix"
        assert on_disk["force_rerun"]["reason"] == "re-review after the codex config fix"
        result = gate_recorder.stamp_request_identity({"gate": "codex_gate"}, on_disk)
        assert result["force_rerun"]["reason"] == "re-review after the codex config fix"

    def test_without_a_force_rerun_no_request_carries_one(self, env):
        assert "force_rerun" not in _request_codex()
