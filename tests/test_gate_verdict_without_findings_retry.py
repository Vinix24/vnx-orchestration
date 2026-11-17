"""reviews-zonder-uitkomst D4b (dispatch dlv-e9c53156823a-b): ADR-080 besluit 3.

A run that said blocked or fail with no finding (``verdict_without_findings``)
gets one retry on the same head; the second ends in
``verdict_without_findings_retry_cap`` on the obligation, and the takeover walk
then passes the gate over with a hop of that name. Real obligation files and a
real ``ReviewGateManager`` in a throwaway store; the provider lane
(``GateRunner.run``) is the only edge and is replaced.
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

import gate_run_guard
import review_rounds
from gate_obligations import (
    REASON_DEGENERATE_RETRY_CAP,
    REASON_VERDICT_WITHOUT_FINDINGS_RETRY_CAP,
    STATUS_PENDING,
    obligation_path,
    register_obligation,
)
from gate_request_handler import _PrHead

CAP = REASON_VERDICT_WITHOUT_FINDINGS_RETRY_CAP
DID = "dlv-d4b-obligation"
OTHER_HEAD = "d4" * 20
VWF = {"status": "unavailable", "reason": "verdict_without_findings"}
CHAIN = {"codex_gate": "kimi_gate", "kimi_gate": "claude_gate"}


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
    store["runs"] = runs
    store["answers"] = [dict(VWF)]

    def provider_lane(self, *, gate, request_payload, pr_number=None, pr_id=""):
        runs.append({"gate": gate})
        # The harness lane overwrites dispatch_id with a gate-run id.
        request_payload["dispatch_id"] = "gate-run-rewritten"
        answer = store["answers"][0] if len(store["answers"]) == 1 else store["answers"].pop(0)
        return {"gate": gate, "pr_number": pr_number, **answer}

    monkeypatch.setattr(gate_runner.GateRunner, "run", provider_lane)
    store["obligation"] = register_obligation(
        store["state_dir"], dispatch_id=DID, gate="codex_gate", project_id="vnx-dev", pr_number=PR,
    )
    return store


def _manager():
    import review_gate_manager

    return review_gate_manager.ReviewGateManager()


def _request() -> None:
    _manager().request_reviews(
        pr_number=PR, branch=BRANCH, review_stack=["codex_gate"], risk_class="medium",
        changed_files=["scripts/lib/example.py"], mode="per_pr", dispatch_id=DID,
    )


def _run_once(env) -> Dict[str, Any]:
    _request()
    return _manager().execute_gate(gate="codex_gate", pr_number=PR)


def _record(env) -> Dict[str, Any]:
    return json.loads(obligation_path(env["state_dir"], DID).read_text(encoding="utf-8"))


def _hop(env, head: str = HEAD_SHA):
    return _manager()._verdict_without_findings_hop("codex_gate", PR, head)


class TestTheCount:
    def test_the_reason_counts_as_a_reviewer_run(self):
        assert gate_run_guard.reviewer_ran(VWF) is True

    @pytest.mark.parametrize("result", [
        {"status": "unavailable", "reason": "lane_exhausted"},
        {"status": "unavailable", "reason": "verdict_without_finding"},
        {"status": "unavailable", "reason": None},
        {"status": "unavailable"},
    ])
    def test_a_run_without_reviewer_ran_does_not_count(self, result):
        assert gate_run_guard.reviewer_ran(result) is False


class TestRetryThenCap:
    def test_one_run_leaves_the_obligation_open_for_a_second_run_on_the_head(self, env):
        _run_once(env)

        record = _record(env)
        assert record["head_runs"]["reviewer_runs"] == 1
        assert record["head_runs"]["last_reason"] == "verdict_without_findings"
        assert record.get("reason") != CAP and record["status"] == STATUS_PENDING
        assert _hop(env) is None

        _run_once(env)
        assert [run["gate"] for run in env["runs"]] == ["codex_gate", "codex_gate"]

    def test_the_second_run_books_the_cap_reason_on_the_pre_run_dispatch_id(self, env):
        _run_once(env)
        _run_once(env)

        record = _record(env)
        assert record["reason"] == CAP and record["head_runs"]["reviewer_runs"] == 2
        assert record["reason_detail"].startswith(CAP)
        assert record["status"] == STATUS_PENDING
        assert not obligation_path(env["state_dir"], "gate-run-rewritten").exists()

    def test_a_degenerate_cap_keeps_its_own_reason_and_no_hop(self, env):
        env["answers"][0] = {"status": "unavailable", "reason": "gate_execution_degenerate"}
        _run_once(env)
        _run_once(env)

        assert _record(env)["reason"] == REASON_DEGENERATE_RETRY_CAP
        assert _hop(env) is None

    def test_a_refusal_that_ran_no_reviewer_counts_nothing(self, env):
        env["answers"][0] = {"status": "unavailable", "reason": "lane_exhausted"}
        _run_once(env)

        assert "head_runs" not in _record(env)


class TestTheHop:
    def test_the_next_request_passes_the_gate_over_with_a_hop_of_the_same_name(self, env):
        _run_once(env)
        _run_once(env)

        hop = _hop(env)
        assert hop["gate"] == "codex_gate" and hop["reason"] == CAP and hop["status"] == "unavailable"
        reader, path, refusal = _manager()._walk_takeover_chain(
            "codex_gate", PR, dict(CHAIN), None, head=_PrHead(PR, HEAD_SHA),
        )
        assert reader != "codex_gate" and refusal is None
        assert [h["reason"] for h in path][0] == CAP
        assert not list(env["results_dir"].glob("*not_executable*"))

    def test_a_new_head_starts_again_at_one_and_is_not_passed_over(self, env):
        _run_once(env)
        _run_once(env)
        assert _hop(env, OTHER_HEAD) is None
        assert gate_run_guard.runs_on_head(_record(env)["head_runs"], OTHER_HEAD) == 0
        again = gate_run_guard.next_head_runs(_record(env)["head_runs"], OTHER_HEAD, VWF)
        assert again["reviewer_runs"] == 1

    def test_the_end_of_the_chain_is_the_named_exhausted_state_not_codex(self, env):
        _run_once(env)
        _run_once(env)

        reader, path, _ = _manager()._walk_takeover_chain(
            "codex_gate", PR, {}, None, head=_PrHead(PR, HEAD_SHA),
        )
        assert reader is None and [h["gate"] for h in path] == ["codex_gate"]

    @pytest.mark.parametrize("mutate", [
        lambda r: r.pop("head_runs"),
        lambda r: r.update(head_runs="x"),
        lambda r: r.update(gate="kimi_gate"),
        lambda r: r.update(pr_number=PR + 1, pr_id="PR-%d" % (PR + 1)),
        lambda r: r.update(reason="unknown_reason"),
    ])
    def test_a_record_that_does_not_prove_the_cap_on_this_head_skips_nothing(self, env, mutate):
        _run_once(env)
        _run_once(env)
        record = _record(env)
        mutate(record)
        env["obligation"].write_text(json.dumps(record), encoding="utf-8")

        assert _hop(env) is None

    def test_an_unreadable_obligation_is_a_failure_not_a_pass(self, env):
        env["obligation"].write_text("{not json", encoding="utf-8")

        with pytest.raises(ValueError):
            _hop(env)

    def test_an_unknown_head_skips_nothing(self, env):
        _run_once(env)
        _run_once(env)
        assert _hop(env, "") is None


class TestTheLockAndTheLedger:
    def test_a_second_start_while_the_lock_is_held_does_not_run_and_counts_nothing(self, env):
        _request()
        lock_path = gate_run_guard.run_lock_path(env["state_dir"], pr_ref=PR, gate="codex_gate", head_sha=HEAD_SHA)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(lock_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        assert holder.stdout.readline().strip() == "held"
        try:
            answer = _manager().execute_gate(gate="codex_gate", pr_number=PR)
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)

        assert answer["status"] == "in_flight" and env["runs"] == []
        assert "head_runs" not in _record(env)

    def test_a_reclassified_run_writes_no_line_in_rounds_ndjson(self, env):
        results_dir = env["results_dir"]
        payload = {"gate": "codex_gate", "pr_number": PR, "commit_sha": HEAD_SHA, **VWF}

        booking = review_rounds.book_result_verdict(
            results_dir / f"pr-{PR}-codex_gate.json", payload, gate="codex_gate", on_disk=None,
        )

        assert booking.outcome == "skipped" and "no review-seat verdict" in booking.detail
        ledger = review_rounds.ledger_path_for_results(results_dir)
        assert ledger is None or not ledger.exists() or ledger.read_text(encoding="utf-8") == ""


_HOLDER = """
import fcntl, json, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.pwrite(fd, json.dumps({"pid": os.getpid()}).encode("utf-8"), 0)
print("held", flush=True)
sys.stdin.read()
"""
