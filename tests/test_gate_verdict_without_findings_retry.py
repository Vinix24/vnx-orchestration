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
from typing import Any, Dict, List

import pytest

import review_stack_support  # puts scripts/ and scripts/lib on sys.path
from review_stack_support import BRANCH, HEAD_SHA, PR, build_project_store, isolate_config_registry

import gate_obligation_runner as runner
import gate_request_handler
import gate_run_guard
import review_rounds
from gate_obligations import (
    REASON_DEGENERATE_RETRY_CAP,
    REASON_VERDICT_WITHOUT_FINDINGS_RETRY_CAP,
    STATUS_PENDING,
    obligation_path,
    register_obligation,
    update_obligation,
)
from gate_request_handler import _PrHead
from test_gate_obligation_runner_one_run_per_head import (
    HEAD_A, _held_by_another_process, _patch, _ScriptedManager, _store,
)

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
    return _manager()._verdict_without_findings_hop("codex_gate", PR, _PrHead(PR, head))


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

    def test_oi2984_an_empty_chain_at_the_cap_ends_in_the_named_state_not_a_third_run(self, env, monkeypatch):
        _run_once(env)
        _run_once(env)

        reader, path, _ = _manager()._resolve_seat_reader("codex_gate", PR, {}, {}, _PrHead(PR, HEAD_SHA))
        assert reader is None and [(h["gate"], h["reason"]) for h in path] == [("codex_gate", CAP)]

        monkeypatch.setattr(gate_request_handler, "_build_review_gate_takeover_chain", lambda: {})
        answer = _manager().request_and_execute(
            pr_number=PR, branch=BRANCH, review_stack=["codex_gate"], risk_class="medium",
            changed_files=["scripts/lib/example.py"], mode="per_pr", dispatch_id=DID,
        )

        assert [run["gate"] for run in env["runs"]] == ["codex_gate", "codex_gate"]
        exhausted = json.loads((env["results_dir"] / f"pr-{PR}-codex_gate-chain-exhausted.json").read_text())
        assert exhausted["status"] == "chain_exhausted" and exhausted["takeover_path"][0]["reason"] == CAP
        assert [(e["gate"], e["request_status"]) for e in answer["gates"]] == [("codex_gate", "chain_exhausted")]

    def test_oi2984_an_empty_chain_below_the_cap_still_reads_its_own_seat(self, env):
        _run_once(env)

        reader, path, _ = _manager()._resolve_seat_reader("codex_gate", PR, {}, {}, _PrHead(PR, HEAD_SHA))
        assert reader == "codex_gate" and path == []

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


def _finalize(env, result: Dict[str, Any]) -> Dict[str, Any]:
    """The runner's end of a run: a temporary refusal booked its own reason, then the cap report."""
    path = obligation_path(env["state_dir"], DID)
    update_obligation(path, reason="gate_parked", reason_detail="temporary refusal")
    outcome: Dict[str, Any] = {"action": "pending"}
    runner._report_cap_reached(path, HEAD_SHA, result, outcome, gate="codex_gate", pr_number=PR, dispatch_id=DID)
    return outcome


class TestTheRunnerFinalization:
    def test_the_park_keeps_the_verdict_without_findings_reason_and_the_hop(self, env):
        _run_once(env)
        _run_once(env)

        outcome = _finalize(env, VWF)

        assert outcome["action"] == "retry_capped" and outcome["reason"] == CAP
        record = _record(env)
        assert record["reason"] == CAP and record["reason_detail"].startswith(CAP)
        assert _hop(env)["reason"] == CAP

    def test_the_degenerate_path_still_parks_with_its_own_reason(self, env):
        degenerate = {"status": "unavailable", "reason": "gate_execution_degenerate"}
        env["answers"][0] = degenerate
        _run_once(env)
        _run_once(env)

        outcome = _finalize(env, degenerate)

        assert outcome["reason"] == REASON_DEGENERATE_RETRY_CAP
        assert _record(env)["reason"] == REASON_DEGENERATE_RETRY_CAP
        assert _hop(env) is None


class TestTheCapBelongsToItsHead:
    def test_a_counted_run_on_a_new_head_clears_the_old_cap_and_does_not_hop(self, env):
        _run_once(env)
        _run_once(env)
        assert _record(env)["reason"] == CAP

        record = gate_run_guard.book_reviewer_run(
            env["state_dir"], dispatch_id=DID, gate="codex_gate", pr_ref=PR, head_sha=OTHER_HEAD, result=VWF,
        )

        assert record["head_runs"]["reviewer_runs"] == 1
        assert gate_run_guard.cap_reached(record["head_runs"], OTHER_HEAD) is False
        assert _record(env).get("reason") is None
        assert _hop(env, OTHER_HEAD) is None

    def test_a_cap_reason_without_the_cap_on_this_head_is_no_hop(self, env):
        _run_once(env)
        _run_once(env)
        record = _record(env)
        record["head_runs"] = dict(record["head_runs"], head_sha=OTHER_HEAD, reviewer_runs=1)
        env["obligation"].write_text(json.dumps(record), encoding="utf-8")

        assert _hop(env, OTHER_HEAD) is None


class TestTheLockAndTheLedger:
    def test_a_second_start_while_the_lock_is_held_does_not_run_and_counts_nothing(self, env):
        _request()
        lock_path = gate_run_guard.run_lock_path(env["state_dir"], pr_ref=PR, gate="codex_gate", head_sha=HEAD_SHA)
        with _held_by_another_process(lock_path):
            answer = _manager().execute_gate(gate="codex_gate", pr_number=PR)

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




class _Handover(_ScriptedManager):
    def request_and_execute(self, *, review_stack, **kw):  # the hop: codex's seat goes to kimi
        self.asked = list(review_stack)
        return super().request_and_execute(review_stack=["kimi_gate"], **kw)


@pytest.mark.parametrize("last_reason, successor, status, reason", [
    ("verdict_without_findings", {"status": "pass"}, "fulfilled", "fulfilled_by_takeover_evidence"),
    ("verdict_without_findings", VWF, STATUS_PENDING, CAP),
    ("gate_execution_degenerate", VWF, STATUS_PENDING, REASON_DEGENERATE_RETRY_CAP),
])
def test_a_runner_tick_at_the_cap_runs_the_successor_never_the_declared_gate(
    tmp_path, monkeypatch, last_reason, successor, status, reason,
):
    state_dir, head = _store(tmp_path), {"sha": HEAD_A}
    (report := tmp_path / "kimi.md").write_text("kimi report", encoding="utf-8")
    runs = {"head_sha": HEAD_A, "reviewer_runs": 2, "last_status": "unavailable", "last_reason": last_reason}
    path = register_obligation(state_dir, dispatch_id=DID, gate="codex_gate", project_id="vnx-dev", pr_number=PR)
    update_obligation(path, head_runs=runs, reason=gate_run_guard.cap_reason(runs))
    codex = state_dir / "review_gates" / "results" / f"pr-{PR}-codex_gate.json"
    codex.write_text(json.dumps(dict(VWF, gate="codex_gate", commit_sha=HEAD_A)), encoding="utf-8")
    manager = _Handover(state_dir, [dict(successor, report_path=str(report), contract_hash="sha256:k")], head)
    _patch(monkeypatch, manager, head)
    monkeypatch.setattr(gate_request_handler, "_build_review_gate_takeover_chain", lambda: dict(CHAIN))

    outcome = runner.run(state_dir)["outcomes"][0]

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["status"] == status and record["reason"] == reason and record["head_runs"] == runs
    assert json.loads(codex.read_text()) == dict(VWF, gate="codex_gate", commit_sha=HEAD_A)
    capped = last_reason == "gate_execution_degenerate"  # only the degenerate cap parks and calls nothing
    assert (outcome["action"] == "retry_capped") is capped and getattr(manager, "asked", None) == (None if capped else ["codex_gate"])
    assert [call["review_stack"] for call in manager.calls] == ([] if capped else [["kimi_gate"]])
