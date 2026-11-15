"""Characterisation of ``gate_artifacts.materialize_artifacts`` (golf schoon R35b, OI-2906).

Files and content for a PASS and a FAIL run, the exact ``record_failure``
arguments of both refusals, every early exit, the partial review, the prompt
pin and the side-effect order. Green on the old code and after extract-method.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_artifacts
import gate_recorder
import gate_register_emit
from gate_artifacts import materialize_artifacts

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _stream(verdict_obj, actions: int = 16) -> str:
    """A codex ``exec --json`` stream with *actions* reads and one verdict message."""
    lines = [json.dumps({"type": "thread.started", "thread_id": "01a0-r35b"}),
             json.dumps({"type": "turn.started"})]
    lines += [json.dumps({"type": "item.completed", "item": {
        "id": f"item_{i}", "type": "command_execution", "exit_code": 0, "status": "completed",
        "command": f"/bin/zsh -lc \"sed -n '1,200p' scripts/lib/mod_{i}.py\"",
    }}) for i in range(actions)]
    lines.append(json.dumps({"type": "item.completed", "item": {
        "id": "item_99", "type": "agent_message", "text": json.dumps(verdict_obj),
    }}))
    lines.append(json.dumps({"type": "turn.completed",
                             "usage": {"input_tokens": 18219, "output_tokens": 602}}))
    return "\n".join(lines) + "\n"


PASS_VERDICT = {"verdict": "pass", "residual_risk": "diff only", "findings": [
    {"severity": "warning", "message": "loose heredoc", "file_path": "scripts/lib/mod_3.py", "line": 41}]}
FAIL_VERDICT = {"verdict": "fail", "residual_risk": "", "findings": [
    {"severity": "error", "message": "race on rename", "file_path": "scripts/lib/mod_1.py", "line": 7}]}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    requests = data / "state" / "review_gates" / "requests"
    results = data / "state" / "review_gates" / "results"
    reports = data / "reports"
    for d in (requests, results, reports):
        d.mkdir(parents=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(data / "state"))
    monkeypatch.delenv("VNX_PROJECT_ROOT", raising=False)
    return {"data": data, "requests": requests, "results": results, "reports": reports}


@pytest.fixture
def calls(monkeypatch):
    """Record record_failure, the register emit and the side-effect order."""
    log = {"record_failure": [], "register": [], "order": [], "prompt_sha": []}
    real_record = gate_recorder.record_failure

    def spy_record(**kw):
        log["record_failure"].append(kw)
        log["order"].append("record_failure")
        return real_record(**kw)

    def spy_register(event, **kw):
        log["register"].append((event, kw))
        log["order"].append("register")
        return True

    real_write = gate_artifacts._write_result_record
    real_sidecar = gate_artifacts._write_pipeline_sidecar

    def spy_write(*a, **kw):
        log["order"].append("result_record")
        return real_write(*a, **kw)

    def spy_sidecar(*a, **kw):
        log["order"].append("sidecar")
        return real_sidecar(*a, **kw)

    def spy_prompt_sha(dispatch_id, data_dir):
        log["prompt_sha"].append((dispatch_id, data_dir))
        return "f" * 64

    monkeypatch.setattr(gate_recorder, "record_failure", spy_record)
    monkeypatch.setattr(gate_register_emit, "emit_codex_gate_to_register", spy_register)
    monkeypatch.setattr(gate_artifacts, "_write_result_record", spy_write)
    monkeypatch.setattr(gate_artifacts, "_write_pipeline_sidecar", spy_sidecar)
    monkeypatch.setattr(gate_artifacts, "final_prompt_sha_for_dispatch", spy_prompt_sha)
    return log


def _payload(dirs, gate="codex_gate", dispatch_id="20261115-r35b-builder", **extra):
    payload = {"gate": gate, "pr_id": "4242", "pr_number": 4242, "branch": "refactor/r35b",
               "report_path": str(dirs["reports"] / f"{gate}-report.md"),
               "contract_hash": "0123456789abcdef", "dispatch_id": dispatch_id,
               "commit_sha": "a" * 40}
    payload.update(extra)
    return payload


def _run(dirs, stdout, payload, gate="codex_gate", final_answer_dir=None):
    return materialize_artifacts(
        gate=gate, pr_number=4242, pr_id="4242", stdout=stdout,
        request_payload=payload, duration_seconds=12.5,
        requests_dir=dirs["requests"], results_dir=dirs["results"],
        reports_dir=dirs["reports"], final_answer_dir=final_answer_dir,
    )


def _tree(root: Path) -> list:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _expected_success(dirs, payload, stdout, *, blocking, advisory, findings, residual):
    depth = gate_artifacts.gate_depth.measure_execution_depth(stdout).to_dict()
    return {
        "gate": "codex_gate", "pr_id": "4242", "pr_number": 4242, "status": "completed",
        "summary": "codex_gate execution completed successfully",
        "contract_hash": "0123456789abcdef", "report_path": payload["report_path"],
        "findings": findings, "blocking_findings": blocking, "advisory_findings": advisory,
        "required_reruns": [], "residual_risk": residual, "duration_seconds": 12.5,
        "execution_depth": depth, "final_prompt_sha256": "f" * 64,
        "dispatch_id": "20261115-r35b-builder", "verdict_source": "report",
        "branch": "refactor/r35b", "commit_sha": "a" * 40, "attempt_seq": 1,
    }


def _check_success_files(dirs, payload, result):
    assert _tree(dirs["data"]) == [
        "reports/codex_gate-report.md", "state/append_receipt.lock",
        "state/provider_reachability/.codex.json.lock", "state/provider_reachability/codex.json",
        "state/receipt_idempotency_recent.ndjson", "state/receipt_outcome_index.json",
        "state/report_pipeline/gate-codex_gate-pr-4242.json",
        "state/review_gates/.rounds.ndjson.lock",
        "state/review_gates/requests/.4242-codex_gate-contract.json.lock",
        "state/review_gates/requests/4242-codex_gate-contract.json",
        "state/review_gates/results/.4242-codex_gate-contract.json.lock",
        "state/review_gates/results/4242-codex_gate-contract.json",
        "state/review_gates/rounds.ndjson", "state/t0_receipts.ndjson",
    ]
    on_disk = json.loads((dirs["results"] / "4242-codex_gate-contract.json").read_text())
    for key, value in result.items():
        assert on_disk[key] == value, key
    request = json.loads((dirs["requests"] / "4242-codex_gate-contract.json").read_text())
    assert request["completed_at"] == result["recorded_at"]
    sidecar_path = dirs["data"] / "state" / "report_pipeline" / "gate-codex_gate-pr-4242.json"
    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar == {
        "dispatch_id": "20261115-r35b-builder", "gate": "codex_gate",
        "pr_id": "4242", "pr_number": 4242, "status": result["status"],
        "findings": result["findings"], "blocking_findings": result["blocking_findings"],
        "advisory_findings": result["advisory_findings"],
        "contract_hash": result["contract_hash"], "report_path": result["report_path"],
        "duration_seconds": 12.5, "source": "gate_runner",
        "recorded_at": result["recorded_at"],
    }
    report = Path(payload["report_path"]).read_text(encoding="utf-8")
    assert report.startswith("# codex_gate — Headless Gate Report\n\n**PR**: 4242\n"
                             "**Branch**: refactor/r35b\n**Gate**: codex_gate\n**Generated**: ")
    assert "## Findings" in report and "## Gate Output" in report
    return report


def test_pass_run_writes_report_result_and_sidecar(dirs, calls):
    stdout = _stream(PASS_VERDICT)
    payload = _payload(dirs)
    result = _run(dirs, stdout, payload)

    recorded_at = result.pop("recorded_at")
    assert _ISO.match(recorded_at)
    findings = gate_artifacts._normalize_findings(PASS_VERDICT["findings"])
    assert result == _expected_success(
        dirs, payload, stdout, blocking=[], advisory=findings,
        findings=findings, residual="diff only",
    )
    result["recorded_at"] = recorded_at
    report = _check_success_files(dirs, payload, result)
    assert "Blocking count: 0\nAdvisory count: 1" in report
    assert calls["record_failure"] == []
    assert calls["register"] == [("gate_passed", {
        "dispatch_id": "20261115-r35b-builder", "pr_number": 4242,
        "pr_id": "4242", "gate": "codex_gate",
    })]
    assert calls["prompt_sha"] == [("20261115-r35b-builder", dirs["data"])]
    assert calls["order"] == ["result_record", "sidecar", "register"]


def test_fail_run_books_blocking_and_registers_failed(dirs, calls):
    stdout = _stream(FAIL_VERDICT)
    payload = _payload(dirs)
    result = _run(dirs, stdout, payload)

    recorded_at = result.pop("recorded_at")
    findings = gate_artifacts._normalize_findings(FAIL_VERDICT["findings"])
    expected = _expected_success(
        dirs, payload, stdout, blocking=findings, advisory=[],
        findings=findings, residual="",
    )
    expected["failure_reason"] = "1 blocking finding(s): race on rename"
    assert result == expected
    result["recorded_at"] = recorded_at
    report = _check_success_files(dirs, payload, result)
    assert "Blocking count: 1\nAdvisory count: 0" in report
    assert "- [BLOCKING] race on rename" in report
    assert [e for e, _ in calls["register"]] == ["gate_failed"]
    assert calls["order"] == ["result_record", "sidecar", "register"]


def _refusal_kw(dirs, gate, payload, stdout, reason, detail):
    return {
        "gate": gate, "pr_number": 4242, "pr_id": "4242",
        "result": {"reason": reason, "reason_detail": detail, "duration_seconds": 12.5,
                   "partial_output_lines": len(stdout.splitlines()),
                   "runner_pid": os.getpid()},
        "request_payload": payload,
        "requests_dir": dirs["requests"], "results_dir": dirs["results"],
    }


def test_identity_guard_refusal_passes_exact_record_failure_arguments(dirs, calls):
    stdout = _stream(PASS_VERDICT)
    payload = _payload(dirs, gate="kimi_gate")
    result = _run(dirs, stdout, payload, gate="kimi_gate")
    detail = gate_recorder.gate_dispatch_identity_error("kimi_gate", "20261115-r35b-builder")
    assert calls["record_failure"] == [_refusal_kw(
        dirs, "kimi_gate", payload, stdout, "gate_dispatch_identity_invalid", detail,
    )], "the identity refusal passes no extra="
    assert result["reason"] == "gate_dispatch_identity_invalid"
    assert not Path(payload["report_path"]).exists()
    assert calls["order"] == ["record_failure"]


def test_degenerate_refusal_passes_exact_record_failure_arguments(dirs, calls):
    stdout = _stream({"verdict": "pass", "findings": [], "residual_risk": ""}, actions=0)
    payload = _payload(dirs)
    result = _run(dirs, stdout, payload)
    depth = gate_artifacts.gate_depth.measure_execution_depth(stdout)
    expected = _refusal_kw(dirs, "codex_gate", payload, stdout, "gate_execution_degenerate",
                           gate_artifacts.gate_depth.degenerate_detail(depth))
    expected["extra"] = {"execution_depth": depth.to_dict(),
                         "degenerate_report_path": payload["report_path"]}
    assert calls["record_failure"] == [expected]
    assert result["reason"] == "gate_execution_degenerate"
    assert Path(payload["report_path"]).exists(), "the refused run's report is evidence"
    assert calls["register"] == []


def _unwritable(dirs, payload, monkeypatch):
    blocker = dirs["reports"] / "not-a-dir"
    blocker.write_text("x")
    payload["report_path"] = str(blocker / "codex_gate-report.md")


_EARLY_EXITS = {
    "missing_report_path": (
        lambda d, p, m: p.update(report_path=""), PASS_VERDICT,
        "artifact_materialization_failed", "No report_path in request payload", None),
    "unwritable_report": (
        _unwritable, PASS_VERDICT, "artifact_materialization_failed",
        "Failed to write report: ", None),
    "sparse_output": (
        None, "one line only", "empty_review_content",
        "Gate output has only 1 substantive line(s); expected review content", False),
    "empty_report_file": (
        lambda d, p, m: m.setattr(gate_artifacts, "_validate_report_file",
                                  lambda _p: "Report file is empty or missing after write"),
        PASS_VERDICT, "artifact_materialization_failed",
        "Report file is empty or missing after write", False),
    "no_verdict": (
        None, {"note": "no verdict here"}, "validation_failed", "no_verdict_block: ", True),
    "result_write_error": (
        lambda d, p, m: m.setattr(gate_artifacts, "_write_result_record",
                                  lambda *a, **kw: "Failed to write result record: boom"),
        PASS_VERDICT, "artifact_materialization_failed",
        "Failed to write result record: boom", True),
}


@pytest.mark.parametrize("case", sorted(_EARLY_EXITS))
def test_early_exit_books_one_failure(dirs, calls, monkeypatch, case):
    setup, out, reason, detail, report_kept = _EARLY_EXITS[case]
    payload = _payload(dirs)
    if setup is not None:
        setup(dirs, payload, monkeypatch)
    result = _run(dirs, out if isinstance(out, str) else _stream(out), payload)
    assert len(calls["record_failure"]) == 1
    res = calls["record_failure"][0]["result"]
    assert res["reason"] == reason
    assert res["reason_detail"].startswith(detail)
    if detail.endswith(": "):
        assert len(res["reason_detail"]) > len(detail)
    else:
        assert res["reason_detail"] == detail
    assert result["status"] == "unavailable"
    if report_kept is not None:
        assert Path(payload["report_path"]).exists() is report_kept
    assert calls["register"] == []


def test_partial_review_payload(dirs, calls):
    stdout = _stream(PASS_VERDICT, actions=1)
    payload = _payload(dirs, diff_coverage={
        "diff_chars": 900, "diff_truncated": True, "diff_limit": 500,
        "truncated_files": ["a.py", "b.py", "c.py"],
    })
    result = _run(dirs, stdout, payload)
    depth = gate_artifacts.gate_depth.with_diff_coverage(
        gate_artifacts.gate_depth.measure_execution_depth(stdout), payload["diff_coverage"],
    )
    gap = gate_artifacts.gate_depth.coverage_gap(depth)
    assert gap
    assert result["status"] == "partial_review"
    assert result["summary"] == f"codex_gate PARTIAL_REVIEW ({gap}) — NOT a pass"
    assert result["reason"] == "diff_truncated"
    assert result["reason_detail"] == gap
    assert result["execution_depth"] == depth.to_dict()
    assert calls["record_failure"] == []


def test_no_dispatch_id_skips_prompt_pin_and_dispatch_id(dirs, calls):
    payload = _payload(dirs, dispatch_id="")
    result = _run(dirs, _stream(PASS_VERDICT), payload)
    assert result["status"] == "completed"
    assert "final_prompt_sha256" not in result
    assert "dispatch_id" not in result
    assert calls["prompt_sha"] == []
    sidecar = json.loads(
        (dirs["data"] / "state" / "report_pipeline" / "gate-codex_gate-pr-4242.json").read_text()
    )
    assert sidecar["dispatch_id"] == "gate-codex_gate-pr-4242"
