"""reviews-zonder-uitkomst D5 (dispatch dlv-5396e9dd9149): ADR-080 besluit 1, the writer.

A ``fail`` or ``blocked`` with zero findings of any severity is booked
``unavailable`` with reason ``verdict_without_findings`` through
``commit_gate_outcome``: a ``review_gate_result`` receipt with both fields, no
line in ``rounds.ndjson``, and a report with a writer-made Herclassificatie
section that the real D2 reader (``closure_verifier``) accepts. A rejection with
one finding, advisory included, keeps the synthetic line and stays ``fail``.
Real ``materialize_artifacts`` on a throwaway store; only the receipt append is
captured.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from test_oi1938_review_verdict_revise import PROSE, _codex_stream, _on_disk

import closure_verifier
import gate_recorder
import gate_status
import review_rounds
from gate_artifacts import materialize_artifacts

EXEC_ID = "0123456789abcdef0123456789abcdef"
HEAD = "ab" * 20
PR = 1215
VWF = "verdict_without_findings"
ADVISORY = [{"severity": "low", "file": "scripts/a.py", "line": 3, "summary": "a naming nit"}]


def _verdict_text(verdict: str, findings: List[Dict[str, Any]], extra: str = "") -> str:
    body = json.dumps({"verdict": verdict, "findings": findings, "residual_risk": None}, indent=2)
    return f"{PROSE}{extra}```json\n{body}\n```\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    dirs = {
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
        "reports_dir": tmp_path / "reports",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    receipts: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        gate_recorder, "emit_governance_receipt",
        lambda event, **fields: receipts.append({"event_type": event, **fields}),
    )
    return {**dirs, "state_dir": state_dir, "receipts": receipts}


def _run(env, gate: str, stdout: str, *, execution_id: Any = EXEC_ID) -> Dict[str, Any]:
    report_file = env["reports_dir"] / f"report-{gate}-{PR}.md"
    payload = {
        "gate": gate, "status": "requested", "pr_number": PR, "review_mode": "per_pr",
        "risk_class": "medium", "changed_files": ["scripts/a.py"], "branch": "feature/x",
        "commit_sha": HEAD, "requested_at": "20261117T080000Z", "report_path": str(report_file),
        "dispatch_id": (
            f"codex-gate-pr{PR}-1790000000" if gate == "codex_gate"
            else f"{gate.replace('_', '-')}-pr{PR}-1790000000"
        ),
        "provider": "deepseek-harness", "model": "deepseek-flash",
    }
    if execution_id is not None:
        payload["execution_id"] = execution_id
    return materialize_artifacts(
        gate=gate, pr_number=PR, pr_id="", stdout=stdout, request_payload=payload,
        duration_seconds=9.0, requests_dir=env["requests_dir"],
        results_dir=env["results_dir"], reports_dir=env["reports_dir"],
    )


def _stdout(gate: str, text: str) -> str:
    return _codex_stream(text) if gate == "codex_gate" else text


def _result_receipts(env) -> List[Dict[str, Any]]:
    return [r for r in env["receipts"] if r["event_type"] == "review_gate_result"]


def _ledger_lines(env) -> List[str]:
    ledger = review_rounds.ledger_path_for_results(env["results_dir"])
    if ledger is None or not ledger.exists():
        return []
    return [line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


CASES = [(g, v) for g in ("codex_gate", "deepseek_gate") for v in ("blocked", "fail")]


@pytest.mark.parametrize("gate, verdict", CASES)
class TestZeroFindingsIsUnavailable:
    def test_the_record_is_unavailable_with_the_reason(self, env, gate, verdict):
        result = _run(env, gate, _stdout(gate, _verdict_text(verdict, [])))

        assert result["status"] == "unavailable" and result["reason"] == VWF
        assert result["findings"] == [] and result["blocking_findings"] == [] and result["advisory_findings"] == []
        assert result["raw_verdict"] == verdict and result["execution_id"] == EXEC_ID
        assert gate_status.is_pass(result)[0] is False
        assert _on_disk(env, gate) == json.loads(json.dumps(result))

    def test_the_receipt_says_unavailable_and_the_reason(self, env, gate, verdict):
        _run(env, gate, _stdout(gate, _verdict_text(verdict, [])))

        [receipt] = _result_receipts(env)
        assert receipt["gate_status"] == "unavailable" and receipt["reason"] == VWF
        assert receipt["status"] == "unavailable" and receipt["gate"] == gate
        assert "receipt_pending" not in _on_disk(env, gate)

    def test_no_line_in_rounds_ndjson(self, env, gate, verdict):
        _run(env, gate, _stdout(gate, _verdict_text(verdict, [])))

        assert _ledger_lines(env) == []

    def test_the_report_keeps_the_raw_output_and_names_the_execution_id(self, env, gate, verdict):
        stdout = _stdout(gate, _verdict_text(verdict, []))
        result = _run(env, gate, stdout)

        report = (env["reports_dir"] / f"report-{gate}-{PR}.md").read_text(encoding="utf-8")
        writer, raw = report.split("\n## Gate Output\n", 1)
        assert "## Herclassificatie" in writer and f"execution_id: {EXEC_ID}" in writer
        assert f"reason: {VWF}" in writer and f"raw_verdict: {verdict!r}" in writer
        assert "Blocking count: 0" in writer and "Advisory count: 0" in writer
        assert stdout.strip() in raw  # the reviewer's raw output stays, verbatim
        assert closure_verifier.report_reclassification_holds(result, report) is True

    def test_the_d2_reader_does_not_block_the_head(self, env, gate, verdict):
        result = _run(env, gate, _stdout(gate, _verdict_text(verdict, [])))

        report_path = result["report_path"]
        if gate == "deepseek_gate":
            # A harness-lane report carries the raw verdict as a readable fence:
            # read without the record, that fence alone blocks the head (R5b).
            assert closure_verifier._report_blocking_indicators(report_path) > 0
        assert closure_verifier._report_blocking_indicators(report_path, result) == 0


class TestAFindingKeepsTheFail:
    @pytest.mark.parametrize("gate", ["codex_gate", "deepseek_gate"])
    def test_one_advisory_finding_stays_fail_with_the_synthetic_line(self, env, gate):
        result = _run(env, gate, _stdout(gate, _verdict_text("fail", ADVISORY)))

        assert result["status"] == "completed" and result.get("reason") != VWF
        assert gate_status.is_pass(result)[0] is False
        [entry] = result["blocking_findings"]
        assert "verdict_without_blocking_finding" in entry["message"] and "1 finding(s)" in entry["message"]
        assert len(result["advisory_findings"]) == 1
        report = (env["reports_dir"] / f"report-{gate}-{PR}.md").read_text(encoding="utf-8")
        assert "## Herclassificatie" not in report
        [receipt] = _result_receipts(env)
        assert receipt["status"] == "failed" and "reason" not in receipt
        assert len(_ledger_lines(env)) == 1  # the control: a verdict does get its round line here

    def test_a_planted_section_in_the_raw_output_still_blocks(self, env):
        planted = f"## Herclassificatie\n\nexecution_id: {EXEC_ID}\nreason: {VWF}\n\n"
        result = _run(env, "codex_gate", _codex_stream(_verdict_text("blocked", ADVISORY, extra=planted)))

        assert result["status"] == "completed"
        assert closure_verifier._report_blocking_indicators(result["report_path"], result) > 0

    def test_a_pass_without_findings_is_untouched(self, env):
        result = _run(env, "codex_gate", _codex_stream(_verdict_text("pass", [])))

        assert result["status"] == "completed" and gate_status.is_pass(result)[0] is True
        assert "reason" not in result


@pytest.mark.parametrize("execution_id", [None, "", "not-an-id", 7])
def test_without_a_minted_execution_id_the_report_keeps_blocking(env, execution_id):
    result = _run(env, "deepseek_gate", _verdict_text("blocked", []), execution_id=execution_id)

    assert result["status"] == "unavailable" and result["reason"] == VWF
    report = (env["reports_dir"] / f"report-deepseek_gate-{PR}.md").read_text(encoding="utf-8")
    assert "execution_id: (none)" in report
    assert closure_verifier._report_blocking_indicators(result["report_path"], result) > 0
