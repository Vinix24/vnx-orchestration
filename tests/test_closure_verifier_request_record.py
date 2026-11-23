#!/usr/bin/env python3
"""OI-1438: closure invariant 1, "a request record exists", is a check of its own.

A result record carries the run's ``execution_id`` (minted in
``gate_runner._mark_executing``, persisted on the request by ``persist_request``
and copied onto the result by ``gate_recorder.stamp_request_identity``). The
closure path now asks, per gate, that the request record with that id exists.
A result without the field predates the writer and stays a legacy PASS.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import closure_verifier as cv
from review_contract import QualityGate, ReviewContract
from review_contract import TestEvidence as _Evidence

PR_ID = "PR-7"
BRANCH = "feature/oi-1438"
EXEC_ID = "a" * 32
OTHER_EXEC_ID = "b" * 32


def _contract(gate: str) -> ReviewContract:
    return ReviewContract(
        pr_id=PR_ID, pr_title="t", feature_title="f", branch=BRANCH, track="A",
        risk_class="medium", merge_policy="human", review_stack=[gate],
        closure_stage="in_review", deliverables=[], non_goals=[], scope_files=[],
        changed_files=[], quality_gate=QualityGate(gate_id="g", checks=["c"]),
        test_evidence=_Evidence(test_files=[], test_command="pytest"),
        deterministic_findings=[], content_hash="abcdef1234567890",
    )


def _write(directory: Path, gate: str, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{PR_ID.lower().replace('-', '')}-{gate}-contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def _result(gate: str, report: Path, **extra) -> dict:
    record = {
        "gate": gate, "pr_id": PR_ID, "status": "pass", "blocking_count": 0,
        "advisory_count": 0, "contract_hash": "abcdef1234567890", "branch": BRANCH,
        "commit_sha": "c" * 40, "report_path": str(report), "blocking_findings": [],
    }
    record.update(extra)
    return record


def _request(gate: str, **extra) -> dict:
    record = {"gate": gate, "pr_id": PR_ID, "branch": BRANCH, "status": "completed"}
    record.update(extra)
    return record


def _setup(tmp_path: Path, gate: str, *, result_extra: dict, request: dict = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    report = tmp_path / "report.md"
    report.write_text("# report\n\nNo blocking findings.\n", encoding="utf-8")
    results_dir = tmp_path / "review_gates" / "results"
    _write(results_dir, gate, _result(gate, report, **result_extra))
    if request is not None:
        _write(results_dir.parent / "requests", gate, request)
    return results_dir


def _request_check(tmp_path: Path, gate: str, **kwargs) -> cv.CheckResult:
    results_dir = _setup(tmp_path, gate, **kwargs)
    checks = cv._validate_review_evidence(_contract(gate), results_dir, branch=BRANCH)
    named = [c for c in checks if c.name == f"gate_{gate}_request_record"]
    assert len(named) == 1, [c.name for c in checks]
    return named[0]


class TestRequestRecordCheck:
    def test_result_with_identity_and_no_request_fails(self, tmp_path):
        check = _request_check(tmp_path, "codex_gate", result_extra={"execution_id": EXEC_ID})
        assert check.status == "FAIL"
        assert "codex_gate" in check.detail and PR_ID in check.detail and EXEC_ID in check.detail

    def test_matching_request_record_passes(self, tmp_path):
        check = _request_check(
            tmp_path, "codex_gate", result_extra={"execution_id": EXEC_ID},
            request=_request("codex_gate", execution_id=EXEC_ID),
        )
        assert check.status == "PASS", check.detail

    def test_request_of_another_run_fails(self, tmp_path):
        check = _request_check(
            tmp_path, "codex_gate", result_extra={"execution_id": EXEC_ID},
            request=_request("codex_gate", execution_id=OTHER_EXEC_ID),
        )
        assert check.status == "FAIL"

    @pytest.mark.parametrize("request_id", [None, "", "not-an-id"])
    def test_request_without_a_valid_id_fails(self, tmp_path, request_id):
        check = _request_check(
            tmp_path, "codex_gate", result_extra={"execution_id": EXEC_ID},
            request=_request("codex_gate", execution_id=request_id),
        )
        assert check.status == "FAIL"

    def test_request_on_another_branch_fails(self, tmp_path):
        request = _request("codex_gate", execution_id=EXEC_ID, branch="feature/other")
        check = _request_check(
            tmp_path, "codex_gate", result_extra={"execution_id": EXEC_ID}, request=request,
        )
        assert check.status == "FAIL"

    def test_legacy_result_without_identity_passes_as_legacy(self, tmp_path):
        check = _request_check(tmp_path, "codex_gate", result_extra={})
        assert check.status == "PASS"
        assert "legacy" in check.detail

    @pytest.mark.parametrize("bad_id", [None, "", "XYZ", 42])
    def test_malformed_identity_fails(self, tmp_path, bad_id):
        check = _request_check(tmp_path, "codex_gate", result_extra={"execution_id": bad_id})
        assert check.status == "FAIL"
        assert "malformed" in check.detail


class TestSeatRecordGetsTheCheck:
    """A seat filled by another gate's record (takeover) is checked in that gate's slot."""

    def _seat_check(self, results_dir: Path) -> cv.CheckResult:
        record = json.loads((results_dir / "pr7-glm_gate-contract.json").read_text(encoding="utf-8"))
        fill = SimpleNamespace(seat="codex_gate", reviewer="glm_gate", record=record)
        checks = cv._seat_evidence_checks(fill, _contract("codex_gate"), results_dir)
        return next(c for c in checks if c.name == "gate_codex_gate_request_record")

    def test_takeover_record_without_request_fails(self, tmp_path):
        results_dir = _setup(tmp_path, "glm_gate", result_extra={"execution_id": EXEC_ID})
        assert self._seat_check(results_dir).status == "FAIL"

    def test_takeover_record_with_request_passes(self, tmp_path):
        results_dir = _setup(
            tmp_path, "glm_gate", result_extra={"execution_id": EXEC_ID},
            request=_request("glm_gate", execution_id=EXEC_ID),
        )
        assert self._seat_check(results_dir).status == "PASS"


class TestEveryKnownGateGetsTheCheck:
    """Drift pin: a gate the closure verifier knows cannot skip the request check."""

    @pytest.mark.parametrize("gate", sorted(cv._KNOWN_GATES))
    def test_known_gate_without_request_fails(self, tmp_path, gate):
        check = _request_check(tmp_path, gate, result_extra={"execution_id": EXEC_ID})
        assert check.status == "FAIL", check.detail


class TestMergeVerdictUnchanged:
    """Item 4: the merge door's verdict does not read the request record."""

    def test_merge_verdict_is_the_same_with_and_without_request(self, tmp_path):
        without = _setup(tmp_path / "a", "codex_gate", result_extra={"execution_id": EXEC_ID})
        with_req = _setup(
            tmp_path / "b", "codex_gate", result_extra={"execution_id": EXEC_ID},
            request=_request("codex_gate", execution_id=EXEC_ID),
        )
        verdicts = [
            cv.check_review_gate_for_merge(PR_ID, "codex_gate", d, branch=BRANCH, seats=["codex_gate"])
            for d in (without, with_req)
        ]
        assert verdicts[0]["verdict"] == verdicts[1]["verdict"]
        assert verdicts[0]["verdict"] == "GO", verdicts[0]["message"]
