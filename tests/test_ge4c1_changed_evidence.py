"""GE-4c-1 round 3 (dlv-cda65b5aa104-r3): a write with changed evidence is no repeat.

On 83ae25b8 a second ``record_result`` on the same head, status and decision
returned the stored record as a repeat, so new required reruns and a new
residual risk never reached the disk while the call reported success.

``gemini_review`` sits in no review seat, so the round ledger books it no line;
a seat gate's second verdict on one head is the round ledger's own refusal (R1),
before this round and after it. ``gate_outcome`` names are imported inside the
tests that need them: 83ae25b8 has no ``DuplicateReceiptRefused``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_depth
import gate_recorder
import review_stack_support

PR, BRANCH, HEAD = review_stack_support.PR, review_stack_support.BRANCH, review_stack_support.HEAD_SHA
GATE = "gemini_review"
CLAUDE_SLOT = "pr4-claude_github_optional-contract.json"
_OK_DEPTH = gate_depth.single_shot_depth(50000, False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("VNX_PARENT_DISPATCH", "VNX_TASK_CLASS", "VNX_TIER_FROM", "VNX_TIER_TO"):
        monkeypatch.delenv(name, raising=False)
    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    for name in ("r1.md", "r2.md"):
        (store["reports_dir"] / name).write_text("# r\n", encoding="utf-8")
    return store


def _manager():
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _slot(env, gate: str = GATE) -> Path:
    return env["results_dir"] / f"pr-{PR}-{gate}.json"


def _ndjson(env, name: str) -> list:
    path = env["state_dir"] / name
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def _receipts(env) -> list:
    lines = _ndjson(env, "t0_receipts.ndjson") + _ndjson(env, "gate_events.ndjson")
    return [r for r in lines if r.get("event_type") == "review_gate_result"]


def _refusal_audit_lines(env) -> list:
    return [r for r in _ndjson(env, "gate_execution_audit.ndjson") if r.get("reason") == "gate_result_write_refused"]


def _request(env, name: str, gate: str) -> Path:
    path = env["requests_dir"] / name
    path.write_text(json.dumps({"gate": gate, "status": "executing", "pr_number": PR, "branch": BRANCH,
                                "commit_sha": HEAD, "contract_hash": "contract-hash"}), encoding="utf-8")
    return path


def _record(env, report: str, gate: str = GATE, **fields):
    return _manager().record_result(
        gate=gate, pr_number=PR, branch=BRANCH, status="pass", summary="clean", contract_hash="contract-hash",
        pr_id=str(PR), report_path=str(env["reports_dir"] / report), **fields,
    )


def _claude(env, report: str, required_reruns: list):
    return _manager().record_claude_github_result(
        pr_id="PR-4", branch=BRANCH, status="pass", summary="clean", contract_hash="contract-hash",
        findings=[], pr_number=PR, report_path=str(env["reports_dir"] / report), required_reruns=required_reruns,
    )


def _verdict(env, dispatch: str, report: str, **fields) -> dict:
    return {"gate": GATE, "pr_id": str(PR), "pr_number": PR, "status": "pass", "commit_sha": HEAD,
            "contract_hash": "h", "report_path": str(env["reports_dir"] / report), "dispatch_id": dispatch,
            "blocking_findings": [], "branch": BRANCH, **fields}


def _terminal(env, payload: dict) -> Path:
    return gate_recorder.record_terminal_result(
        gate=GATE, pr_id=str(PR), result_path=_slot(env), payload=payload, execution_depth=_OK_DEPTH,
    )


# --- codex's reproduction (record_result, R9) ------------------------------------------------


def test_changed_evidence_under_its_own_report_lands_as_the_next_attempt(env):
    _record(env, "r1.md")
    _record(env, "r2.md", required_reruns=["codex_gate"], residual_risk="a new risk")

    record = _json(_slot(env))
    assert (record["required_reruns"], record["residual_risk"], record["attempt_seq"]) == (["codex_gate"], "a new risk", 2)
    assert [(r["attempt_seq"], r["required_reruns"], r["residual_risk"]) for r in _receipts(env)] == [
        (1, [], ""), (2, ["codex_gate"], "a new risk"),
    ]


@pytest.mark.parametrize("gate", [GATE, "kimi_gate"])
def test_changed_evidence_under_the_same_report_is_refused_and_changes_nothing(env, gate):
    request = _request(env, f"pr-{PR}-{gate}.json", gate)
    _record(env, "r1.md", gate=gate)
    stored, request_before = _slot(env, gate).read_bytes(), request.read_bytes()

    with pytest.raises(gate_recorder.ResultOverwriteRefused) as refused:
        _record(env, "r1.md", gate=gate, required_reruns=["codex_gate"], residual_risk="a new risk")
    from gate_outcome import DuplicateReceiptRefused

    assert isinstance(refused.value, DuplicateReceiptRefused)
    message = str(refused.value)
    assert "changes required_reruns, residual_risk of" in message and "idempotency key" in message
    assert _slot(env, gate).read_bytes() == stored and request.read_bytes() == request_before
    assert [line["reason_detail"] for line in _refusal_audit_lines(env)] == [message]
    assert len(_receipts(env)) == 1


@pytest.mark.parametrize("second_report", ["r1.md", "r2.md"])
def test_the_same_outcome_with_the_same_evidence_stays_a_repeat(env, second_report):
    """report_path joins no comparison: a runner retry under a new report is still the outcome on the slot."""
    _record(env, "r1.md", required_reruns=["codex_gate"], residual_risk="known")
    stored = _slot(env).read_bytes()

    _record(env, second_report, required_reruns=["codex_gate"], residual_risk="known")
    assert _slot(env).read_bytes() == stored, "no rewrite"
    assert _json(_slot(env))["attempt_seq"] == 1 and len(_receipts(env)) == 1
    assert _refusal_audit_lines(env) == []


# --- record_claude_github_result (R8): the same commit -----------------------------------------


def test_claude_github_changed_evidence_goes_through_the_same_commit(env):
    _request(env, CLAUDE_SLOT, "claude_github_optional")
    slot = env["results_dir"] / CLAUDE_SLOT
    _claude(env, "r1.md", ["codex_gate"])
    _claude(env, "r2.md", ["codex_gate", "kimi_gate"])
    record = _json(slot)
    assert (record["required_reruns"], record["attempt_seq"]) == (["codex_gate", "kimi_gate"], 2)
    assert [(r["attempt_seq"], r["commit_sha"]) for r in _receipts(env)] == [(1, HEAD), (2, HEAD)]
    stored = slot.read_bytes()

    with pytest.raises(gate_recorder.ResultOverwriteRefused) as refused:
        _claude(env, "r2.md", ["kimi_gate"])
    assert "changes required_reruns of" in str(refused.value)
    assert slot.read_bytes() == stored and len(_receipts(env)) == 2
    assert len(_refusal_audit_lines(env)) == 1


# --- the runner's records: the durable outcome id is an identity too ---------------------------


def test_changed_evidence_of_the_same_dispatch_is_refused_whatever_its_report(env):
    """A receipt that names a dispatch is deduped on its outcome id (ADR-038), which reads no report_path."""
    _terminal(env, _verdict(env, "gemini-gate-pr-1", "r1.md"))
    stored = _slot(env).read_bytes()

    with pytest.raises(gate_recorder.ResultOverwriteRefused) as refused:
        _terminal(env, _verdict(env, "gemini-gate-pr-1", "r2.md", residual_risk="a new risk"))
    assert "changes residual_risk of" in str(refused.value) and "outcome id" in str(refused.value)
    assert _slot(env).read_bytes() == stored and len(_receipts(env)) == 1

    _terminal(env, _verdict(env, "gemini-gate-pr-2", "r2.md", residual_risk="a new risk"))
    assert (_json(_slot(env))["residual_risk"], _json(_slot(env))["attempt_seq"]) == ("a new risk", 2)
    assert [r["dispatch_id"] for r in _receipts(env)] == ["gemini-gate-pr-1", "gemini-gate-pr-2"]


# Fail closed: an evidence field that is missing, None, malformed or another
# value on the slot is no repeat. The write lands as the next attempt.
NEW_EVIDENCE = {"blocking_findings": [], "advisory_findings": [{"message": "nit"}],
                "required_reruns": ["codex_gate"], "residual_risk": "known"}


@pytest.mark.parametrize("field", sorted(NEW_EVIDENCE))
@pytest.mark.parametrize("stored_value", ["missing", None, 7, "another value"])
def test_evidence_that_differs_on_the_slot_is_no_repeat(env, field, stored_value):
    _terminal(env, _verdict(env, "gemini-gate-pr-1", "r1.md", **NEW_EVIDENCE))
    stored = _json(_slot(env))
    if stored_value == "missing":
        del stored[field]
    else:
        stored[field] = [{"message": "another"}] if stored_value == "another value" and field != "residual_risk" else stored_value
    _slot(env).write_text(json.dumps(stored), encoding="utf-8")

    _terminal(env, _verdict(env, "gemini-gate-pr-2", "r2.md", **NEW_EVIDENCE))
    record = _json(_slot(env))
    assert (record[field], record["attempt_seq"]) == (NEW_EVIDENCE[field], 2)
    assert len(_receipts(env)) == 2


# --- the identity is the append's own -----------------------------------------------------------


@pytest.mark.parametrize("writer", ["runner", "parser"])
def test_the_identity_is_the_one_the_receipt_append_booked(env, writer):
    """The runner's record names its dispatch, the parser's names none: only the first has a durable outcome id."""
    if writer == "runner":
        _terminal(env, _verdict(env, "gemini-gate-pr-1", "r1.md"))
    else:
        _record(env, "r1.md")
    (line,) = _receipts(env)
    key, outcome_id = gate_recorder.outcome_receipt_identity(_json(_slot(env)), gate=GATE, pr_ref=str(PR))
    assert key in {entry["key"] for entry in _ndjson(env, "receipt_idempotency_recent.ndjson")}
    assert outcome_id == (line["outcome_id"] if writer == "runner" else "")


# --- the advisory: record_result(unavailable) against the runner's unavailable ------------------


def _ndjson_lines(env) -> dict:
    found = {}
    for path in sorted(env["project_root"].rglob("*.ndjson")):
        lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        if lines:
            found[str(path.relative_to(env["project_root"]))] = len(lines)
    return found


def test_an_unavailable_record_result_leaves_what_the_runners_unavailable_leaves(env):
    """Measured on main 404c611b: the runner's record_failure for an outage leaves no NDJSON line
    (no receipt, no round-ledger line, no transition event); its reachability entry, only for a
    quota text, is JSON state. record_result has neither a reason nor its text, so it leaves none either."""
    gate_recorder.record_failure(
        gate="kimi_gate", pr_number=PR, pr_id="",
        result={"reason": "timeout", "reason_detail": "timed out after 600s", "duration_seconds": 600.0,
                "partial_output_lines": 0, "runner_pid": 1},
        request_payload={"gate": "kimi_gate", "status": "executing", "pr_number": PR, "branch": BRANCH,
                         "commit_sha": HEAD, "contract_hash": "contract-hash"},
        requests_dir=env["requests_dir"], results_dir=env["results_dir"],
    )
    assert _json(_slot(env, "kimi_gate"))["status"] == "unavailable"
    runner = _ndjson_lines(env)

    _manager().record_result(gate=GATE, pr_number=PR, branch=BRANCH, status="unavailable",
                             summary="provider down", pr_id=str(PR))
    assert _json(_slot(env))["status"] == "unavailable"
    assert runner == _ndjson_lines(env) == {}
    assert not (env["state_dir"] / "provider_reachability").exists()
