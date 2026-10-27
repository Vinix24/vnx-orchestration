"""GE-4c-1 (dlv-cda65b5aa104): the failure record, the not-executable request and both gate_result_parser
writers through commit_gate_outcome. On 0977a7fc they write no attempt_seq and a refusal still ends the request."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_recorder
import review_stack_support

PR, BRANCH, HEAD = review_stack_support.PR, review_stack_support.BRANCH, review_stack_support.HEAD_SHA
GOLDEN = json.loads((VNX_ROOT / "tests/fixtures/ge4c1_parser_receipts_golden.json").read_text(encoding="utf-8"))
# Random by design: the append time, its ingest time and the line's hash over both.
RANDOM_FIELDS = {"timestamp", "ingested_at", "outcome_id"}
CLAUDE_INPUTS = dict(pr_id="PR-4", branch=BRANCH, status="pass", summary="clean", contract_hash="contract-hash",
                     findings=[{"severity": "advisory", "category": "style", "message": "nit"}],
                     pr_number=PR, required_reruns=["codex_gate"])
RECORD_INPUTS = dict(gate="kimi_gate", pr_number=PR, branch=BRANCH, status="fail", summary="blocked",
                     findings=[{"severity": "blocking", "title": "bug"}, {"severity": "advisory", "title": "nit"}],
                     residual_risk="medium", contract_hash="contract-hash", pr_id=str(PR), required_reruns=["kimi_gate"])


@pytest.fixture
def env(tmp_path, monkeypatch):
    # The append layer stamps these from the process; the golden was taken without them.
    for name in ("VNX_PARENT_DISPATCH", "VNX_TASK_CLASS", "VNX_TIER_FROM", "VNX_TIER_TO"):
        monkeypatch.delenv(name, raising=False)
    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    store["report"] = store["reports_dir"] / "r.md"
    store["report"].write_text("# r\n", encoding="utf-8")
    return store


def _manager():
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _receipts(env):
    lines = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):  # a receipt without dispatch_id is rerouted
        path = env["state_dir"] / name
        lines += path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [r for r in map(json.loads, filter(str.strip, lines)) if r.get("event_type") == "review_gate_result"]


def _request(env, name, **fields):
    path = env["requests_dir"] / name
    gate = "claude_github_optional" if name.startswith("pr4-") else name[len(f"pr-{PR}-"):-len(".json")]
    payload = {"gate": gate, "status": "executing", "pr_number": PR, "branch": BRANCH, "commit_sha": HEAD,
               "contract_hash": "contract-hash", **fields}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, payload


def _not_executable(env, request):
    return gate_recorder.record_not_executable(
        gate="wiring_gate", pr_number=PR, pr_id="", reason="provider_not_installed", reason_detail="no gh",
        request_payload=request, requests_dir=env["requests_dir"], results_dir=env["results_dir"],
        state_dir=env["state_dir"])


def _failure(env, request, reason):
    return gate_recorder.record_failure_simple(
        gate="gemini_review", pr_number=PR, pr_id="", reason=reason, reason_detail="detail",
        request_payload=request, requests_dir=env["requests_dir"], results_dir=env["results_dir"])


def _claude(env):
    return _manager().record_claude_github_result(report_path=str(env["report"]), **CLAUDE_INPUTS)


def _record(env):
    return _manager().record_result(report_path=str(env["report"]), **RECORD_INPUTS)


# (writer, result slot, request slot, receipts, request status) per row; R3 once per request status.
ROWS = {
    "R2": (lambda env, req: _not_executable(env, req), "pr-{}-wiring_gate.json", "not_executable", 0),
    "R3-failed": (lambda env, req: _failure(env, req, "review_blocked"), "pr-{}-gemini_review.json", "failed", 1),
    "R3-unavailable": (lambda env, req: _failure(env, req, "timeout"), "pr-{}-gemini_review.json", "unavailable", 0),
    "R8": (lambda env, req: _claude(env), "pr4-claude_github_optional-contract.json", "completed", 1),
    "R9": (lambda env, req: _record(env), "pr-{}-kimi_gate.json", "completed", 1),
}


@pytest.mark.parametrize("row", sorted(ROWS))
def test_one_call_commits_one_record_its_receipt_and_its_request(env, row):
    write, slot, request_status, receipts = ROWS[row]
    request_path, request = _request(env, slot.format(PR))
    write(env, request)
    record = _json(env["results_dir"] / slot.format(PR))
    assert (record["attempt_seq"], "receipt_pending" in record, "receipt_fields" in record) == (1, False, False)
    assert [r["attempt_seq"] for r in _receipts(env)] == [1] * receipts, "only a decided verdict owes a receipt"
    on_disk = _json(request_path)
    assert on_disk["status"] == request_status
    assert {"failed_at", "completed_at", "resolved_at"} & set(on_disk), "the end lands with its time"


@pytest.mark.parametrize("row", sorted(ROWS))
def test_a_refused_write_books_nothing_and_changes_no_request(env, row):
    write, slot, _status, _receipts_owed = ROWS[row]
    (env["results_dir"] / slot.format(PR)).write_text('{"status": "pa', encoding="utf-8")
    request_path, request = _request(env, slot.format(PR))
    before = request_path.read_bytes()
    if row in ("R8", "R9"):
        with pytest.raises(gate_recorder.ResultOverwriteRefused):
            write(env, request)
    else:
        assert write(env, request)["write_refused"] is True
    assert request_path.read_bytes() == before and request["status"] == "executing"
    assert (env["results_dir"] / slot.format(PR)).read_text(encoding="utf-8") == '{"status": "pa'
    assert _receipts(env) == []


@pytest.mark.parametrize("row", ["R8", "R9"])
@pytest.mark.parametrize("status", ["", "skipped", "partial_review", "PASSED-ish"])
def test_a_status_that_is_no_outcome_is_refused_before_anything_is_written(env, row, status):
    """Fail closed: only the four outcomes reach the commit; ``record_result`` also keeps its in-flight records."""
    _write, slot, _status, _receipts_owed = ROWS[row]
    request_path, _request_payload = _request(env, slot.format(PR))
    before = request_path.read_bytes()
    manager = _manager()
    with pytest.raises(ValueError, match="an outcome is one of"):
        if row == "R8":
            manager.record_claude_github_result(report_path=str(env["report"]), **{**CLAUDE_INPUTS, "status": status})
        else:
            manager.record_result(report_path=str(env["report"]), **{**RECORD_INPUTS, "status": status})
    assert not (env["results_dir"] / slot.format(PR)).exists()
    assert request_path.read_bytes() == before and _receipts(env) == []


@pytest.mark.parametrize("gate,write,request_slot", [
    ("claude_github_optional", _claude, "pr4-claude_github_optional-contract.json"),
    ("kimi_gate", _record, f"pr-{PR}-kimi_gate.json"),
])
def test_the_parser_receipt_keeps_every_field_of_todays_receipt(env, gate, write, request_slot):
    if gate == "claude_github_optional":
        _request(env, request_slot, commit_sha="a" * 40)
    write(env)
    (line,) = _receipts(env)
    golden = json.loads(json.dumps(GOLDEN["receipts"][gate]).replace("<store>", str(env["project_root"].parent.resolve())))
    assert set(golden) <= set(line), f"dropped: {sorted(set(golden) - set(line))}"
    # One vocabulary (ADR-046): the raw word moves to gate_status, and the
    # verdict that read "investigate" off it now reads the outcome.
    expected = {"pass": ("completed", "accept"), "fail": ("failed", "reject")}[golden["status"]]
    assert (line["status"], line["verdict"]["decision"], line["gate_status"]) == (*expected, golden["status"])
    changed = {k for k in golden if k not in RANDOM_FIELDS | {"status", "verdict"} and line[k] != golden[k]}
    assert changed == set(), {k: (golden[k], line[k]) for k in changed}
    assert line["attempt_seq"] == 1


def test_a_receipt_booked_later_still_carries_the_parser_fields(env, monkeypatch):
    real_emit = gate_recorder._emit_terminal_result_receipt

    def outage(payload, **kw):
        raise OSError("append lock taken")

    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", outage)
    _record(env)
    record = _json(env["results_dir"] / f"pr-{PR}-kimi_gate.json")
    assert record["receipt_pending"] is True and record["receipt_fields"]["residual_risk"] == "medium"

    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", real_emit)
    monkeypatch.setattr("gate_result_parser.get_pr_head_sha", lambda _pr: "b" * 40)
    _manager().record_result(report_path=str(env["report"]), **{**RECORD_INPUTS, "status": "pass", "findings": []})
    first, second = _receipts(env)
    assert (first["attempt_seq"], first["residual_risk"], first["summary"], first["commit_sha"]) == (1, "medium", "blocked", HEAD)
    assert (second["attempt_seq"], second["commit_sha"]) == (2, "b" * 40)


@pytest.mark.parametrize("stored", [None, "fields", 7, ["branch"]])
def test_malformed_receipt_fields_on_disk_book_the_generic_receipt(env, stored):
    from gate_outcome import _book_receipt

    path = env["results_dir"] / f"pr-{PR}-codex_gate.json"
    record = {"gate": "codex_gate", "pr_number": PR, "status": "fail", "commit_sha": HEAD, "attempt_seq": 3,
              "contract_hash": "h", "report_path": str(env["report"]), "receipt_pending": True, "receipt_fields": stored}
    path.write_text(json.dumps(record), encoding="utf-8")
    assert _book_receipt(path, record, gate="codex_gate", pr_ref=str(PR)) is True
    (line,) = _receipts(env)
    assert (line["status"], line["attempt_seq"], "branch" in line) == ("failed", 3, False)
    assert {"receipt_pending", "receipt_fields"} & set(_json(path)) == set()


def test_an_extra_field_never_replaces_the_emitters_own(env):
    record = {"gate": "codex_gate", "pr_number": PR, "status": "pass", "commit_sha": HEAD, "attempt_seq": 2,
              "contract_hash": "h", "report_path": str(env["report"])}
    gate_recorder._emit_terminal_result_receipt(record, gate="codex_gate", pr_ref=str(PR), extra_fields={
        "status": "fail", "timestamp": "1999", "gate_status": "fail", "attempt_seq": 9, "commit_sha": "c" * 40,
        "summary": "kept"})
    (line,) = _receipts(env)
    assert (line["status"], line["gate_status"], line["attempt_seq"], line["commit_sha"]) == ("completed", "pass", 2, HEAD)
    assert line["summary"] == "kept" and line["timestamp"] != "1999"
