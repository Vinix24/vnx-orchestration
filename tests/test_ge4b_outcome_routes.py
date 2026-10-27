"""GE-4b (dlv-009ac584e142): result writers and the request through commit_gate_outcome; the old head has no GateRequest."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import types
from pathlib import Path
from unittest import mock

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_artifacts
import gate_recorder
import governance_receipts
import review_stack_support
from atomic_io import slot_lock
from gate_executor import GateExecutorMixin
from gate_report_generator import GateReportGeneratorMixin

PR, HEAD_A, HEAD_B = 4310, "a" * 40, "b" * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    # The report generator imports ``_utc_now`` from the manager; the real one drags in everything.
    monkeypatch.setitem(sys.modules, "review_gate_manager", types.SimpleNamespace(_utc_now=governance_receipts.utc_now_iso))
    return store


class _Writers(GateExecutorMixin, GateReportGeneratorMixin):
    def __init__(self, env):
        self.requests_dir, self.results_dir, self.state_dir = env["requests_dir"], env["results_dir"], env["state_dir"]

    def _result_path(self, gate, pr_number):
        return self.results_dir / f"pr-{pr_number}-{gate}.json"


def _slot(env, gate, kind="results"):
    return env[f"{kind}_dir"] / f"pr-{PR}-{gate}.json"


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _receipts(env):
    lines = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):  # a receipt without dispatch_id is rerouted
        path = env["state_dir"] / name
        lines += path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [r for r in map(json.loads, filter(str.strip, lines)) if r.get("event_type") == "review_gate_result"]


def _request(env, gate, head, status="executing"):
    request = {"gate": gate, "status": status, "pr_number": PR, "branch": "feat/ge-4b", "commit_sha": head,
               "contract_hash": f"ch-{head[:4]}", "report_path": str(env["data_dir"] / f"{gate}-{head[:4]}.md")}
    gate_recorder.persist_request(env["requests_dir"], gate, request, pr_number=PR, pr_id="")
    return request


def _run_ci(env, head, bucket="pass", seen=None):
    request = _request(env, "ci_gate", head, status="requested")

    def gh(cmd, **kwargs):
        (seen if seen is not None else []).append(_json(_slot(env, "ci_gate", "requests"))["status"])
        return mock.Mock(returncode=0, stdout=json.dumps([{"name": "ci/t", "bucket": bucket, "state": "X"}]), stderr="")

    with mock.patch("gate_executor.shutil.which", return_value="/usr/bin/gh"), \
            mock.patch("gate_executor.subprocess") as sub:
        sub.TimeoutExpired, sub.run.side_effect = subprocess.TimeoutExpired, gh
        return _Writers(env)._execute_ci_gate(gate="ci_gate", pr_number=PR, pr_id="", request_payload=request)


def _verdict(head, status="completed", n=1):
    return {"gate": "codex_gate", "pr_id": "", "pr_number": PR, "status": status, "commit_sha": head,
            "contract_hash": f"hash-{n}", "report_path": f"/tmp/codex-{n}.md", "blocking_findings": [],
            "dispatch_id": f"codex-gate-pr{PR}-{n}"}


def _artifacts(env, payload, request, tmp_path):
    from gate_outcome import GateRequest

    report = tmp_path / "report.md"
    report.write_text("# report\n", encoding="utf-8")
    return gate_artifacts._write_result_record(
        env["results_dir"], "codex_gate", PR, "", payload, report,
        request=GateRequest(env["requests_dir"], request, {"completed_at": "2026-10-27T10:00:00Z"}),
    )


def _failure(env, gate):
    return _Writers(env)._write_failure_result(gate=gate, pr_number=PR, pr_id="", reason="timeout", reason_detail="900s",
                                               duration_seconds=900.0, partial_output_lines=0, runner_pid=1)


def test_ci_gate_books_one_of_each_a_repeat_none_and_a_new_head_the_next_attempt(env):
    seen = []
    returned = _run_ci(env, HEAD_A, seen=seen)
    record, request, stored = _json(_slot(env, "ci_gate")), _json(_slot(env, "ci_gate", "requests")), _slot(env, "ci_gate").read_bytes()
    assert seen == ["executing"], "the request says executing while the gate runs"
    assert (record["status"], record["attempt_seq"], returned["attempt_seq"]) == ("pass", 1, 1)
    assert request["status"] == "completed" and "completed_at" in request

    _run_ci(env, HEAD_A)
    assert _slot(env, "ci_gate").read_bytes() == stored, "a repeat rewrites nothing"
    assert _json(_slot(env, "ci_gate", "requests"))["status"] == "completed", "a repeat still answers its request"
    _run_ci(env, HEAD_B, bucket="fail")
    assert [(r["commit_sha"], r["gate_status"], r["attempt_seq"]) for r in _receipts(env)] == [(HEAD_A, "pass", 1), (HEAD_B, "fail", 2)]


def test_a_refused_ci_gate_outcome_ends_its_request_instead_of_leaving_it_executing(env):
    """GE-4c, kimi advisory (a): ``executing`` was the loop — the next call re-ran the gate into the same refusal."""
    _slot(env, "ci_gate").write_text('{"status": "pa', encoding="utf-8")
    assert _run_ci(env, HEAD_A)["write_refused"] is True
    request = _json(_slot(env, "ci_gate", "requests"))
    assert (request["status"], request["failure_reason"]) == ("failed", "write_refused")
    assert "completed_at" not in request
    assert _slot(env, "ci_gate").read_text(encoding="utf-8") == '{"status": "pa' and _receipts(env) == []


@pytest.mark.parametrize("status,receipts", [("completed", 1), ("partial_review", 0)])
def test_the_artifacts_writer_commits_record_receipt_and_request(env, tmp_path, status, receipts):
    assert _artifacts(env, _verdict(HEAD_A, status), _request(env, "codex_gate", HEAD_A), tmp_path) is None
    record, request = _json(_slot(env, "codex_gate")), _json(_slot(env, "codex_gate", "requests"))
    assert (record["status"], record["attempt_seq"]) == (status, 1)
    assert (request["status"], request["completed_at"]) == ("completed", "2026-10-27T10:00:00Z")
    assert len(_receipts(env)) == receipts


def test_a_refused_artifacts_write_books_nothing_and_ends_its_request(env, tmp_path):
    """GE-4c, kimi advisory (a): the record and the receipt stay refused; the attempt still ends."""
    _slot(env, "codex_gate").write_text('{"status": "comp', encoding="utf-8")
    request = _request(env, "codex_gate", HEAD_A)
    assert "refused by the overwrite guard" in _artifacts(env, _verdict(HEAD_A), request, tmp_path)
    ended = _json(_slot(env, "codex_gate", "requests"))
    assert (ended["status"], ended["failure_reason"]) == ("failed", "write_refused")
    assert "completed_at" not in ended
    assert (request["status"], request["failure_reason"]) == ("failed", "write_refused"), "memory mirrors disk"
    assert _slot(env, "codex_gate").read_text(encoding="utf-8") == '{"status": "comp' and _receipts(env) == []


def test_the_report_generator_writers_stamp_attempt_seq_and_a_refusal_books_nothing(env):
    _slot(env, "codex_gate").write_text(json.dumps(_verdict(HEAD_A)), encoding="utf-8")
    before = _slot(env, "codex_gate").read_bytes()
    assert _failure(env, "codex_gate")[1] is False and _slot(env, "codex_gate").read_bytes() == before
    assert _receipts(env) == [], "a refused write books nothing"

    failure, written = _failure(env, "gemini_review")
    assert written and failure["attempt_seq"] == _json(_slot(env, "gemini_review"))["attempt_seq"] == 1
    not_exec, written = _Writers(env)._write_not_executable_result(gate="wiring_gate", pr_number=PR, pr_id="",
                                                                   reason="provider_not_installed", reason_detail="no gh")
    assert written and not_exec["attempt_seq"] == _json(_slot(env, "wiring_gate"))["attempt_seq"] == 1
    assert [r["gate_status"] for r in _receipts(env)] == ["failed"], "a not_executable owes no receipt"


def test_the_loser_leaves_the_winners_record_and_request_status(env, tmp_path, monkeypatch):
    from gate_outcome import GateOutcome, GateRequest, commit_gate_outcome

    real_emit, winner_emitting, results = gate_recorder._emit_terminal_result_receipt, threading.Event(), []

    def loser():
        winner_emitting.wait(5)
        outage = {**_verdict(HEAD_A, "unavailable", 2), "contract_hash": "", "report_path": ""}
        results.append(commit_gate_outcome(
            GateOutcome.from_record("codex_gate", outage, pr_id=""), results_dir=env["results_dir"],
            request=GateRequest(env["requests_dir"], {"status": "executing", "marker": "loser"}),
        ))

    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt",
                        lambda payload, **kw: (winner_emitting.set(), real_emit(payload, **kw))[1])
    thread = threading.Thread(target=loser)
    thread.start()
    assert _artifacts(env, _verdict(HEAD_A), _request(env, "codex_gate", HEAD_A), tmp_path) is None
    thread.join(10)

    record, request = _json(_slot(env, "codex_gate")), _json(_slot(env, "codex_gate", "requests"))
    assert results[0][1] is False and (record["status"], record["attempt_seq"]) == ("completed", 1)
    # GE-4c round 2: the request on the slot is the WINNER's — already ended
    # ``completed`` — and the loser holds a payload that never reached this
    # slot. A refused write ends only the request it holds, so the winner's
    # request keeps its status and its completion metadata; the winner's record
    # and receipt are untouched too.
    assert request["status"] == "completed" and request.get("completed_at")
    assert "failure_reason" not in request
    assert [r["commit_sha"] for r in _receipts(env)] == [HEAD_A]


def test_persist_request_waits_for_the_request_slot(env):
    path, started = _slot(env, "ci_gate", "requests"), threading.Event()

    def write():
        started.set()
        gate_recorder.persist_request(env["requests_dir"], "ci_gate", {"status": "requested"}, pr_number=PR, pr_id="")

    with slot_lock(path):
        thread = threading.Thread(target=write)
        thread.start()
        started.wait(5)
        thread.join(0.3)
        assert thread.is_alive() and not path.exists(), "a request write goes through the slot lock"
    thread.join(5)
    assert _json(path) == {"status": "requested"}
    assert sorted(p.name for p in env["requests_dir"].iterdir()) == [f".{path.name}.lock", path.name]


@pytest.mark.parametrize("status", ["missing", None, 7, "mystery"])
def test_a_record_status_with_no_request_status_leaves_the_request(env, status):
    from gate_outcome import GateOutcome, GateRequest, commit_gate_outcome

    record = {k: v for k, v in _verdict(HEAD_A).items() if k != "status"}
    if status != "missing":
        record["status"] = status
    request = _request(env, "codex_gate", HEAD_A)
    before = _slot(env, "codex_gate", "requests").read_bytes()
    commit_gate_outcome(GateOutcome("codex_gate", "pass", "", PR, record), results_dir=env["results_dir"],
                        request=GateRequest(env["requests_dir"], request))
    assert _slot(env, "codex_gate", "requests").read_bytes() == before and request["status"] == "executing"
