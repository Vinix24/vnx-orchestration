"""OI-1943 / OI-1947: a review rejection is decided evidence.

Review gates book a rejection as status ``completed`` plus a non-empty
``blocking_findings`` list (``gate_artifacts.materialize_artifacts``). Every
fixture here has that measured shape, never ``status: failed``.

Covers the shared predicate (``gate_status.decided_verdict``), the runner's
pre- and post-execution handling, the merge-door peer scan, the seat line, and
the four verdict readers that did not know ``revise`` (OI-1947).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import closure_verifier as cv  # noqa: E402
import gate_obligation_runner as runner  # noqa: E402
import gate_report_recovery  # noqa: E402
import gate_status  # noqa: E402
import glm_gate  # noqa: E402
import kimi_gate  # noqa: E402
from gate_obligations import (  # noqa: E402
    REASON_FAILED_BY_GATE_VERDICT,
    REASON_FAILED_BY_TAKEOVER,
    REASON_PR_MERGED,
    STATUS_FAILED,
    STATUS_FULFILLED,
    STATUS_PENDING,
    STATUS_RETIRED,
    obligation_path,
    register_obligation,
)
from gate_seat_line import format_seat_line  # noqa: E402

HEAD = "deadbeef00" * 4
OLD = "0ldc0de000" * 4
CUT_DEPTH = {
    "mode": "single_shot", "parsed": False, "diff_truncated": True, "diff_chars": 90000,
    "diff_limit": 60000, "truncated_files": ["a.py"], "files_read": 0,
}


def _state_dir(tmp_path: Path, name: str = "vnx-data") -> Path:
    state_dir = tmp_path / name / "state"
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    (state_dir / "unified_reports").mkdir(parents=True, exist_ok=True)
    return state_dir


def _report(state_dir: Path, name: str) -> Path:
    path = state_dir / "unified_reports" / f"{name}.md"
    path.write_text("gate report body", encoding="utf-8")
    return path


def _rejection(state_dir, gate, pr, *, commit_sha=HEAD, cut=False, dispatch_id="d-rej"):
    """The measured shape: completed + one blocking finding, no blocking_count."""
    finding = {"severity": "error", "message": "real defect", "file_path": "a.py", "line": 3}
    record = {
        "gate": gate, "pr_number": pr, "dispatch_id": dispatch_id, "status": "completed",
        "findings": [finding], "blocking_findings": [finding], "advisory_findings": [],
        "contract_hash": "sha256:deadbeef", "report_path": str(_report(state_dir, f"{gate}-{pr}")),
        "commit_sha": commit_sha,
    }
    if cut:
        record["execution_depth"] = dict(CUT_DEPTH)
    return record


def _passing(state_dir, gate, pr, *, commit_sha=HEAD):
    return {
        "gate": gate, "pr_number": pr, "dispatch_id": "d-pass", "status": "completed",
        "findings": [], "blocking_findings": [], "advisory_findings": [],
        "contract_hash": "sha256:deadbeef", "report_path": str(_report(state_dir, f"{gate}-{pr}-p")),
        "commit_sha": commit_sha,
    }


def _result_path(state_dir: Path, gate: str, pr: int) -> Path:
    return state_dir / "review_gates" / "results" / f"pr-{pr}-{gate}.json"


def _write(state_dir: Path, gate: str, pr: int, record: dict) -> Path:
    path = _result_path(state_dir, gate, pr)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class _Manager:
    """Writes the given result records when the gate 'runs'. No provider starts."""

    def __init__(self, state_dir: Path, records: dict, *, required_failure: bool = False):
        self.state_dir = state_dir
        self.records = records
        self.required_failure = required_failure
        self.calls = []

    def _request_path(self, gate, pr):
        return self.state_dir / "review_gates" / "requests" / f"pr-{pr}-{gate}.json"

    def _result_path(self, gate, pr):
        return _result_path(self.state_dir, gate, pr)

    def request_and_execute(self, *, pr_number, branch, review_stack, risk_class,
                            changed_files, mode, dispatch_id=""):
        self.calls.append(list(review_stack))
        for gate in review_stack:
            self._request_path(gate, pr_number).write_text(
                json.dumps({"gate": gate, "pr_number": pr_number, "status": "completed"}),
                encoding="utf-8",
            )
        for gate, record in self.records.items():
            _write(self.state_dir, gate, pr_number, record)
        return {"pr_number": pr_number, "branch": "b", "gates": [],
                "has_required_failure": self.required_failure}


def _patch(monkeypatch, manager, *, head=HEAD, pr_state="OPEN"):
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda state_dir: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: head)
    monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr_number, owner_repo: pr_state)
    fake = types.ModuleType("review_gate_manager")
    fake._compute_changed_files = lambda branch: ["scripts/lib/foo.py"]
    monkeypatch.setitem(sys.modules, "review_gate_manager", fake)
    monkeypatch.delenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)


def _register(state_dir, dispatch_id, gate, pr, project_id="vnx-dev"):
    register_obligation(
        state_dir, dispatch_id=dispatch_id, gate=gate, project_id=project_id, pr_number=pr,
    )


def _obligation(state_dir, dispatch_id):
    return json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))


def _decision(state_dir, dispatch_id, gate, pr, monkeypatch, *, head=HEAD, pr_state="OPEN"):
    monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda n: head)
    monkeypatch.setattr(runner, "_pr_state_from_github", lambda n, o: pr_state)
    resolution = runner.PrResolution(
        runner.RESOLUTION_RESOLVED, pr_number=pr, owner_repo="Vinix24/vnx-orchestration",
    )
    index = runner._index_gate_results(state_dir)
    return runner._pre_execution_decision(state_dir, dispatch_id, gate, resolution, 1, index)


# ---------------------------------------------------------------------------
# The shared predicate
# ---------------------------------------------------------------------------


class TestDecidedVerdict:
    def test_completed_with_blocking_is_fail(self, tmp_path):
        assert gate_status.decided_verdict(_rejection(_state_dir(tmp_path), "codex_gate", 1)) == "fail"

    def test_rejection_on_a_cut_diff_is_still_fail(self, tmp_path):
        record = _rejection(_state_dir(tmp_path), "codex_gate", 1, cut=True)
        assert gate_status.decided_verdict(record) == "fail"
        assert gate_status.has_complete_evidence(record) is False

    def test_blocking_count_alone_is_fail(self):
        assert gate_status.decided_verdict({"status": "completed", "blocking_count": 2}) == "fail"

    def test_fail_status_is_fail(self):
        assert gate_status.decided_verdict({"status": "failed"}) == "fail"

    def test_clean_completed_is_pass(self, tmp_path):
        assert gate_status.decided_verdict(_passing(_state_dir(tmp_path), "codex_gate", 1)) == "pass"

    @pytest.mark.parametrize("record", [
        {"status": "partial_review"},
        {"status": "not_executable"},
        {"status": "unavailable"},
        {"status": "running"},
        {"status": "weird"},
        {},
        {"status": "completed", "execution_depth": CUT_DEPTH},
    ])
    def test_everything_else_is_undecided(self, record):
        assert gate_status.decided_verdict(record) == ""

    def test_has_evidence_fields(self):
        assert gate_status.has_evidence_fields({"contract_hash": "h", "report_path": "p"}) is True
        assert gate_status.has_evidence_fields({"contract_hash": "none", "report_path": "p"}) is False
        assert gate_status.has_evidence_fields({"contract_hash": "h"}) is False


# ---------------------------------------------------------------------------
# Runner: pre-execution (A1, A2, A6, A7, C5, C8)
# ---------------------------------------------------------------------------


class TestPreExecution:
    def test_a1_open_pr_rejection_on_head_fulfills_by_failed_evidence(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-a1", "codex_gate", 7001)
        result = _write(state_dir, "codex_gate", 7001, _rejection(state_dir, "codex_gate", 7001))
        manager = _Manager(state_dir, {})
        _patch(monkeypatch, manager)

        assert _decision(state_dir, "d-a1", "codex_gate", 7001, monkeypatch)["kind"] == "fulfill_by_failed_evidence"

        summary = runner.run(state_dir)

        assert manager.calls == []
        record = _obligation(state_dir, "d-a1")
        assert record["status"] == STATUS_FAILED
        assert record["reason"] == "failed_by_existing_evidence"
        assert record["fulfilled_by"] == "codex_gate"
        assert record["evidence_result_path"] == str(result)
        assert summary["outcomes"][0]["action"] == STATUS_FAILED

    @pytest.mark.parametrize("pr_state", ["MERGED", "CLOSED"])
    def test_a2_merged_or_closed_pr_still_books_the_rejection(self, tmp_path, monkeypatch, pr_state):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-a2", "codex_gate", 7002)
        _write(state_dir, "codex_gate", 7002, _rejection(state_dir, "codex_gate", 7002))
        _patch(monkeypatch, _Manager(state_dir, {}), pr_state=pr_state)

        dry = runner.run(state_dir, write=False)
        assert dry["outcomes"][0]["action"] == "would_stamp_failed"

        runner.run(state_dir)
        record = _obligation(state_dir, "d-a2")
        assert record["status"] == STATUS_FAILED
        assert record["reason"] == "failed_by_existing_evidence"

    def test_a6_old_head_rejection_is_a_mismatch(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        record = _rejection(state_dir, "codex_gate", 7003, commit_sha=OLD)

        kind, detail = runner._has_decided_evidence(record, HEAD)

        assert kind == runner._EVIDENCE_MISMATCH
        assert OLD[:8] in detail and HEAD[:8] in detail

    def test_a6_old_head_rejection_on_merged_pr_retires_with_a_note(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-a6m", "codex_gate", 7004)
        _write(state_dir, "codex_gate", 7004, _rejection(state_dir, "codex_gate", 7004, commit_sha=OLD))
        _patch(monkeypatch, _Manager(state_dir, {}), pr_state="MERGED")

        runner.run(state_dir)

        record = _obligation(state_dir, "d-a6m")
        assert record["status"] == STATUS_RETIRED
        assert record["reason"] == REASON_PR_MERGED
        assert "DIFFERENT commit" in (record.get("reason_detail") or "")

    def test_a6_old_head_rejection_left_on_disk_stays_pending_post_exec(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-a6p", "codex_gate", 7005)
        _write(state_dir, "codex_gate", 7005, _rejection(state_dir, "codex_gate", 7005, commit_sha=OLD))
        _patch(monkeypatch, _Manager(state_dir, {}))

        runner.run(state_dir)

        record = _obligation(state_dir, "d-a6p")
        assert record["status"] == STATUS_PENDING
        assert record["reason"] == "stale_evidence_sha_mismatch"

    def test_a7_cut_diff_rejection_is_usable_evidence(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        record = _rejection(state_dir, "deepseek_gate", 7006, cut=True)

        kind, _detail = runner._has_decided_evidence(record, HEAD)

        assert kind == runner._EVIDENCE_USABLE
        assert gate_status.has_complete_evidence(record) is False

    def test_a7_rejection_without_a_report_on_disk_is_not_decided(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        record = _rejection(state_dir, "codex_gate", 7007)
        Path(record["report_path"]).unlink()

        kind, _detail = runner._has_decided_evidence(record, HEAD)

        assert kind == runner._EVIDENCE_NOT_DECIDED

    def test_a7_rejection_without_contract_hash_is_not_decided(self, tmp_path):
        record = _rejection(_state_dir(tmp_path), "codex_gate", 7008)
        record["contract_hash"] = ""

        assert runner._has_decided_evidence(record, HEAD)[0] == runner._EVIDENCE_NOT_DECIDED

    def test_c5_partial_review_and_pass_with_gap_stay_not_decided(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        partial = _passing(state_dir, "deepseek_gate", 7009)
        partial["status"] = "partial_review"
        partial["execution_depth"] = dict(CUT_DEPTH)
        gap = _passing(state_dir, "deepseek_gate", 7009)
        gap["execution_depth"] = dict(CUT_DEPTH)

        assert runner._has_decided_evidence(partial, HEAD)[0] == runner._EVIDENCE_NOT_DECIDED
        assert runner._has_decided_evidence(gap, HEAD)[0] == runner._EVIDENCE_NOT_DECIDED

    def test_c1_a_pass_on_head_still_fulfills_by_evidence(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-c1", "codex_gate", 7010)
        _write(state_dir, "codex_gate", 7010, _passing(state_dir, "codex_gate", 7010))
        assert _decision(state_dir, "d-c1", "codex_gate", 7010, monkeypatch)["kind"] == "fulfill_by_evidence"

    def test_c8_fix_forward_rejection_on_old_head_gates_again(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-c8-orig", "codex_gate", 7011)
        _register(state_dir, "d-c8-fix", "codex_gate", 7011)
        _write(state_dir, "codex_gate", 7011,
               _rejection(state_dir, "codex_gate", 7011, commit_sha="h1" * 20, dispatch_id="d-c8-orig"))

        decision = _decision(state_dir, "d-c8-fix", "codex_gate", 7011, monkeypatch, head="h2" * 20)

        assert decision["kind"] == "attempt_gate"

    def test_second_project_with_colliding_pr_does_not_leak(self, tmp_path, monkeypatch):
        mine = _state_dir(tmp_path, "mine")
        other = _state_dir(tmp_path, "other")
        _register(mine, "d-adr7", "codex_gate", 7012, project_id="vnx-dev")
        _register(other, "d-adr7", "codex_gate", 7012, project_id="other-project")
        _write(other, "codex_gate", 7012, _rejection(other, "codex_gate", 7012))

        assert _decision(mine, "d-adr7", "codex_gate", 7012, monkeypatch)["kind"] == "attempt_gate"


# ---------------------------------------------------------------------------
# Runner: post-execution booking (A3, A4, A5, A7)
# ---------------------------------------------------------------------------


class TestPostExecution:
    def test_a3_a_rejection_the_gate_wrote_books_failed(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-a3", "codex_gate", 7101)
        manager = _Manager(state_dir, {"codex_gate": _rejection(state_dir, "codex_gate", 7101)})
        _patch(monkeypatch, manager)

        summary = runner.run(state_dir)

        record = _obligation(state_dir, "d-a3")
        assert record["status"] == STATUS_FAILED
        assert record["reason"] == REASON_FAILED_BY_GATE_VERDICT
        assert "1 blocking finding(s)" in record["reason_detail"]
        assert record["fulfilled_by"] == "codex_gate"
        assert record["evidence_result_path"] == str(_result_path(state_dir, "codex_gate", 7101))
        assert summary["outcomes"][0]["action"] == "failed"

    def test_a3_required_failure_keeps_its_reason(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-a3r", "codex_gate", 7102)
        manager = _Manager(
            state_dir, {"codex_gate": _rejection(state_dir, "codex_gate", 7102)}, required_failure=True,
        )
        _patch(monkeypatch, manager)

        runner.run(state_dir)

        record = _obligation(state_dir, "d-a3r")
        assert record["status"] == STATUS_FAILED
        assert record["reason"] == "required_failure"

    def test_c1_a_pass_the_gate_wrote_books_fulfilled(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-c1p", "codex_gate", 7103)
        _patch(monkeypatch, _Manager(state_dir, {"codex_gate": _passing(state_dir, "codex_gate", 7103)}))

        runner.run(state_dir)

        record = _obligation(state_dir, "d-c1p")
        assert record["status"] == STATUS_FULFILLED
        assert record.get("reason") is None

    def test_a4_declared_rejection_beside_a_peer_pass_books_failed(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-a4", "codex_gate", 7104)
        manager = _Manager(state_dir, {
            "codex_gate": _rejection(state_dir, "codex_gate", 7104),
            "kimi_gate": _passing(state_dir, "kimi_gate", 7104),
        })
        _patch(monkeypatch, manager)

        runner.run(state_dir)

        record = _obligation(state_dir, "d-a4")
        assert record["status"] == STATUS_FAILED
        assert "resolved_by_gate" not in record

    def test_a5_a_successor_rejection_stops_the_walk(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-a5", "codex_gate", 7105)
        _write(state_dir, "codex_gate", 7105, {
            "gate": "codex_gate", "pr_number": 7105, "status": "unavailable",
            "reason": "dispatch_error", "contract_hash": "", "report_path": "",
        })
        manager = _Manager(state_dir, {
            "kimi_gate": _rejection(state_dir, "kimi_gate", 7105),
            "glm_gate": _passing(state_dir, "glm_gate", 7105),
        })
        _patch(monkeypatch, manager)

        runner.run(state_dir)

        record = _obligation(state_dir, "d-a5")
        assert record["status"] == STATUS_FAILED
        assert record["reason"] == REASON_FAILED_BY_TAKEOVER
        assert record["resolved_by_gate"] == "kimi_gate"
        assert record["takeover_hops"] == ["codex_gate", "kimi_gate"]

    def test_a7_cut_diff_rejection_books_failed(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-a7", "deepseek_gate", 7106)
        manager = _Manager(state_dir, {"deepseek_gate": _rejection(state_dir, "deepseek_gate", 7106, cut=True)})
        _patch(monkeypatch, manager)

        runner.run(state_dir)

        assert _obligation(state_dir, "d-a7")["status"] == STATUS_FAILED

    def test_c3_a_failed_status_record_keeps_its_booking(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-c3", "codex_gate", 7107)
        record = _rejection(state_dir, "codex_gate", 7107)
        record["status"] = "failed"
        _patch(monkeypatch, _Manager(state_dir, {"codex_gate": record}))

        runner.run(state_dir)

        booked = _obligation(state_dir, "d-c3")
        assert booked["status"] == STATUS_FAILED
        assert booked.get("reason") != REASON_FAILED_BY_GATE_VERDICT

    def test_stale_rejection_stays_pending_and_current_rejection_books_failed(
        self, tmp_path, monkeypatch,
    ):
        """OI-1914 first, OI-1943 second: a rejection about a superseded head is
        never booked; the same rejection about the current head books failed."""
        state_dir = _state_dir(tmp_path)
        _register(state_dir, "d-stale", "codex_gate", 7108)
        stale = _rejection(state_dir, "codex_gate", 7108, commit_sha=OLD)
        _patch(monkeypatch, _Manager(state_dir, {"codex_gate": stale}), head=HEAD)

        runner.run(state_dir)

        pending = _obligation(state_dir, "d-stale")
        assert pending["status"] == STATUS_PENDING
        assert pending["reason"] == "stale_evidence_sha_mismatch"

        other = _state_dir(tmp_path, "vnx-data-other")
        _register(other, "d-stale", "codex_gate", 7108, project_id="other")
        current = _rejection(other, "codex_gate", 7108, commit_sha=HEAD)
        _patch(monkeypatch, _Manager(other, {"codex_gate": current}), head=HEAD)

        runner.run(other)

        booked = _obligation(other, "d-stale")
        assert booked["status"] == STATUS_FAILED
        assert booked["reason"] == REASON_FAILED_BY_GATE_VERDICT
        assert _obligation(state_dir, "d-stale")["status"] == STATUS_PENDING


# ---------------------------------------------------------------------------
# Merge door peers (A8, C6, C7)
# ---------------------------------------------------------------------------


import test_oi1624_gate_absence_vs_rejection as t1624  # noqa: E402
import test_oi1719_obligation_takeover_booking as t1719  # noqa: E402


class TestMergeDoorPeers:
    def _blocking(self, **extra):
        finding = {"severity": "error", "message": "real defect"}
        data = {"status": "completed", "blocking_findings": [finding], "findings": [finding]}
        data.update(extra)
        return data

    def test_a8_absence_route_peer_rejection_blocks(self, tmp_path):
        results_dir = tmp_path / "results"
        report = t1624._report_file(tmp_path)
        t1624._write_result(results_dir, "codex_gate", t1624._codex_not_executable())
        t1624._write_result(results_dir, "kimi_gate", t1624._glm_pass(
            report, gate="kimi_gate", **self._blocking(),
        ))
        t1624._write_result(results_dir, "glm_gate", t1624._glm_pass(report))

        verdict = t1624._check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "kimi_gate" in verdict["message"]

    def test_a8_booking_route_peer_rejection_blocks(self, tmp_path):
        results_dir = t1719._booked_setup(tmp_path)
        report = t1719._report_file(tmp_path)
        t1719._write_result(results_dir, "kimi_gate", t1719._glm_pass(
            report, gate="kimi_gate", **self._blocking(),
        ))

        verdict = t1719._check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "afkeuring" in verdict["message"]

    def test_c6_declared_gates_completed_rejection_stays_no_go_beside_a_pass(self, tmp_path):
        results_dir = t1719._booked_setup(tmp_path)
        report = t1719._report_file(tmp_path)
        t1719._write_result(results_dir, "codex_gate", t1719._glm_pass(
            report, gate="codex_gate", **self._blocking(),
        ))

        verdict = t1719._check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "codex_gate" in verdict["message"]

    def test_c7_a_fail_status_peer_still_blocks(self, tmp_path):
        results_dir = t1719._booked_setup(tmp_path)
        report = t1719._report_file(tmp_path)
        t1719._write_result(results_dir, "kimi_gate", t1719._glm_pass(
            report, gate="kimi_gate", status="fail", blocking_count=1,
        ))

        assert t1719._check(tmp_path)["verdict"] == "NO-GO"


# ---------------------------------------------------------------------------
# Seat line (A9)
# ---------------------------------------------------------------------------


class TestSeatLine:
    def test_a9_cut_diff_rejection_prints_fail(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        record = _rejection(state_dir, "deepseek_gate", 7201, cut=True)
        entry = {
            "gate": "deepseek_gate", "request_status": "completed", "execution_status": "completed",
            "passed": False, "pass_reason": "1 blocking finding(s)", "detail": record,
        }

        line, passed = format_seat_line(
            "deepseek_gate", pr_number=7201, state_dir=state_dir,
            seat_entries={"deepseek_gate": entry},
        )

        assert passed is False
        assert line.startswith("Gate 'deepseek_gate': FAIL (")


# ---------------------------------------------------------------------------
# OI-1947: every verdict reader reads revise as a rejection
# ---------------------------------------------------------------------------


def _fence(verdict: str, findings=None) -> str:
    body = json.dumps({"verdict": verdict, "findings": findings or [], "residual_risk": None})
    return f"review prose\n```json\n{body}\n```\n"


class TestReviseIsARejectionInEveryReader:
    @pytest.mark.parametrize("gate_module", [glm_gate, kimi_gate])
    def test_glm_and_kimi_extract_and_map_revise_to_fail(self, gate_module):
        verdict = gate_module._extract_verdict(_fence("REVISE"))

        assert verdict.get("verdict", "").lower() == "revise"
        status, blocking, _residual = gate_module._verdict_to_status(verdict, _fence("REVISE"))
        assert status == "fail"
        assert blocking == []

    @pytest.mark.parametrize("gate_module", [glm_gate, kimi_gate])
    def test_glm_and_kimi_keep_refusing_approve(self, gate_module):
        assert gate_module._extract_verdict(_fence("approve")) == {}

    def test_report_recovery_reads_revise_as_a_candidate_verdict(self):
        assert gate_report_recovery._extract_verdict_block(_fence("revise")).get("verdict") == "revise"
        assert gate_report_recovery._extract_verdict_block(_fence("approve")) == {}

    def test_report_recovery_conflict_check_sees_a_revise_in_the_primary_text(self):
        primary = '{"verdict": "revise", "findings": []'
        assert gate_report_recovery.recovered_verdict_conflicts(primary, {"verdict": "pass"}) is not None
        assert gate_report_recovery.recovered_verdict_conflicts(primary, {"verdict": "fail"}) is None

    def test_closure_fence_reads_revise_and_counts_it_as_blocking(self):
        fence = cv._extract_report_verdict_fence(_fence("revise"))

        assert fence is not None
        assert cv._count_fence_blocking_indicators(fence) == 1
        assert cv._extract_report_verdict_fence(_fence("approve")) is None
        assert cv._extract_report_verdict_fence(_fence("block")) is not None
