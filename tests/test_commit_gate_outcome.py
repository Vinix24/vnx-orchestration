"""GE-4a: one writer for a gate outcome's record and its receipt (ADR-046 §2-§3).

Dispatch-ID: dlv-df37f8c3a3f8 (round 2: dlv-df37f8c3a3f8-r2)

The tests drive the public writers where the old code had them too, so the
red run on the old head shows which gap each one closes. ``gate_outcome`` is
imported inside the tests that need it: the old head has no such module.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_depth
import gate_recorder
import review_stack_support

GATE, PR, BRANCH = "glm_gate", 4207, "feat/ge-4a"
HEAD_A, HEAD_B = "a" * 40, "b" * 40
_OK_DEPTH = gate_depth.single_shot_depth(50000, False)

# What the OLD code (77ad8f78) wrote for the same inputs. GE-4a adds ``attempt_seq``.
OLD_TERMINAL_RECORD = {
    "blocking_findings": [], "branch": "feat/x", "commit_sha": "c" * 40, "contract_hash": "h1",
    "dispatch_id": "glm-gate-pr77-1", "execution_depth": _OK_DEPTH.to_dict(), "gate": "glm_gate",
    "model": "glm-5.2", "pr_id": "77", "pr_number": 77, "provider": "glm-harness",
    "report_path": "/tmp/r.md", "status": "pass",
}
OLD_NOT_EXECUTABLE_RECORD = {
    "advisory_findings": [], "blocking_findings": [], "branch": BRANCH, "commit_sha": "d" * 40,
    "contract_hash": "ch", "failure_reason": "codex binary not found in PATH", "gate": GATE,
    "pr_id": "78", "pr_number": 78, "reason": "provider_not_installed",
    "reason_detail": "codex binary not found in PATH", "report_path": "", "required_reruns": [],
    "residual_risk": "Gate evidence not available. Compensating evidence required.",
    "status": "not_executable", "summary": f"{GATE} not executable: codex binary not found in PATH",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    return review_stack_support.build_project_store(tmp_path, monkeypatch)


def _slot(env, pr: int = PR) -> Path:
    return env["results_dir"] / f"pr-{pr}-{GATE}.json"


def _verdict(status: str, head: str, n: int = 1) -> dict:
    return {
        "gate": GATE, "pr_id": str(PR), "pr_number": PR, "status": status, "commit_sha": head,
        "contract_hash": f"hash-{n}", "report_path": f"/tmp/glm-{n}.md", "dispatch_id": f"glm-gate-pr{PR}-{n}",
        "blocking_findings": [] if status == "pass" else [{"message": "a real defect"}],
        "provider": "glm-harness", "model": "glm-5.2", "branch": BRANCH,
    }


def _outage(head: str) -> dict:
    return {"gate": GATE, "pr_id": str(PR), "pr_number": PR, "status": "unavailable", "commit_sha": head,
            "reason": "dispatch_error", "reason_detail": "502", "dispatch_id": f"glm-gate-pr{PR}-9"}


def _terminal(env, payload: dict) -> Path:
    return gate_recorder.record_terminal_result(
        gate=GATE, pr_id=str(PR), result_path=_slot(env), payload=payload, execution_depth=_OK_DEPTH,
    )


def _not_executable(env, head: str, pr: int = PR) -> dict:
    request = {"gate": GATE, "pr_number": pr, "branch": BRANCH, "commit_sha": head, "contract_hash": "ch"}
    return gate_recorder.record_not_executable(
        gate=GATE, pr_number=pr, pr_id="", reason="provider_not_installed",
        reason_detail="codex binary not found in PATH", request_payload=request,
        requests_dir=env["requests_dir"], results_dir=env["results_dir"], state_dir=env["state_dir"],
    )


def _receipts(env) -> list:
    out = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):
        path = env["state_dir"] / name
        if path.exists():
            lines = (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip())
            out.extend(r for r in lines if r.get("event_type") == "review_gate_result")
    return out


def _on_disk(env) -> dict:
    return json.loads(_slot(env).read_text(encoding="utf-8"))


def _refusal_audit_lines(env) -> list:
    path = env["state_dir"] / "gate_execution_audit.ndjson"
    if not path.exists():
        return []
    lines = (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip())
    return [r for r in lines if r.get("reason") == "gate_result_write_refused"]


_REAL_EMIT = gate_recorder._emit_terminal_result_receipt


def _receipts_down_for(heads: set):
    """The receipt append fails for a record on one of ``heads``; the rest book for real."""
    calls = []

    def emit(payload, **kwargs):
        calls.append(payload.get("commit_sha"))
        if payload.get("commit_sha") in heads:
            raise OSError("append lock unavailable")
        return _REAL_EMIT(payload, **kwargs)

    return emit, calls


def test_record_and_receipt_carry_the_same_head_and_status(env):
    for n, (status, head) in enumerate((("pass", HEAD_A), ("fail", HEAD_B)), start=1):
        _terminal(env, _verdict(status, head, n))
        record, receipts = _on_disk(env), _receipts(env)
        assert len(receipts) == n, "exactly one receipt per call"
        assert (receipts[-1]["commit_sha"], receipts[-1]["gate_status"]) == (head, status)
        assert (record["commit_sha"], record["status"]) == (head, status)
        assert receipts[-1]["attempt_seq"] == record["attempt_seq"] == n
        assert "receipt_pending" not in record


def test_the_loser_leaves_the_winners_terminal_record_and_emits_no_receipt(env, monkeypatch):
    real_emit, winner_emitting, errors = gate_recorder._emit_terminal_result_receipt, threading.Event(), []

    def emit(payload, **kwargs):
        winner_emitting.set()
        return real_emit(payload, **kwargs)

    def loser():
        winner_emitting.wait(5)
        try:
            _terminal(env, _outage(HEAD_A))
        except gate_recorder.ResultOverwriteRefused as exc:
            errors.append(exc)

    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", emit)
    thread = threading.Thread(target=loser)
    thread.start()
    _terminal(env, _verdict("pass", HEAD_A))
    thread.join(10)

    assert len(errors) == 1
    assert (_on_disk(env)["status"], _on_disk(env)["commit_sha"]) == ("pass", HEAD_A)
    assert [(r["gate_status"], r["commit_sha"]) for r in _receipts(env)] == [("pass", HEAD_A)]


def test_receipts_land_in_the_order_their_records_do(env, monkeypatch):
    """The gap: the old writer emitted after it let go of the slot, so a second
    writer could land record AND receipt while the first was still emitting."""
    real_emit, first_emitting, second_done = gate_recorder._emit_terminal_result_receipt, threading.Event(), threading.Event()

    def emit(payload, **kwargs):
        if payload.get("commit_sha") == HEAD_A:
            first_emitting.set()
            second_done.wait(1.0)
        return real_emit(payload, **kwargs)

    def second():
        first_emitting.wait(5)
        _terminal(env, _verdict("pass", HEAD_B, 2))
        second_done.set()

    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", emit)
    thread = threading.Thread(target=second)
    thread.start()
    _terminal(env, _verdict("fail", HEAD_A, 1))
    thread.join(10)

    assert _on_disk(env)["commit_sha"] == HEAD_B
    assert [r["commit_sha"] for r in _receipts(env)] == [HEAD_A, HEAD_B]


def test_a_failed_receipt_stays_pending_and_the_next_call_books_it_once(env, monkeypatch):
    real_emit = gate_recorder._emit_terminal_result_receipt
    failing = mock.Mock(side_effect=OSError("append lock unavailable"))
    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", failing)
    _terminal(env, _verdict("pass", HEAD_A))
    failing.assert_called_once()
    assert _on_disk(env)["receipt_pending"] is True
    assert _receipts(env) == []

    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", real_emit)
    # Refused by the guard (a provider refusal never replaces a decided verdict
    # on its head), and still pays the debt of the record that stays.
    assert _not_executable(env, HEAD_A)["write_refused"] is True
    assert [(r["gate_status"], r["commit_sha"], r["attempt_seq"]) for r in _receipts(env)] == [("pass", HEAD_A, 1)]
    assert "receipt_pending" not in _on_disk(env)

    with pytest.raises(gate_recorder.ResultOverwriteRefused):
        _terminal(env, _outage(HEAD_A))
    assert len(_receipts(env)) == 1, "the third call books no duplicate"


def test_a_receipt_that_landed_before_the_failure_is_not_booked_twice(env, monkeypatch):
    real_emit = gate_recorder._emit_terminal_result_receipt

    def lands_then_fails(payload, **kwargs):
        real_emit(payload, **kwargs)
        raise OSError("the append failed after writing")

    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", lands_then_fails)
    _terminal(env, _verdict("pass", HEAD_A))
    assert _on_disk(env)["receipt_pending"] is True and len(_receipts(env)) == 1

    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", real_emit)
    with pytest.raises(gate_recorder.ResultOverwriteRefused):
        _terminal(env, _outage(HEAD_A))
    assert len(_receipts(env)) == 1, "the key returns the re-booking as a duplicate"
    assert "receipt_pending" not in _on_disk(env)


@pytest.mark.parametrize("flag", ["missing", None, "true", 1, False])
def test_a_flag_this_writer_did_not_set_is_neither_booked_nor_cleared(env, monkeypatch, flag):
    record = _verdict("pass", HEAD_A)
    if flag != "missing":
        record["receipt_pending"] = flag
    _slot(env).write_text(json.dumps(record), encoding="utf-8")
    before = _slot(env).read_bytes()
    spy = mock.Mock(wraps=gate_recorder._emit_terminal_result_receipt)
    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", spy)

    with pytest.raises(gate_recorder.ResultOverwriteRefused):
        _terminal(env, _outage(HEAD_A))
    spy.assert_not_called()
    assert _slot(env).read_bytes() == before


def test_a_slot_whose_receipt_is_still_unbooked_is_not_replaced(env, monkeypatch):
    """The gap (codex, round 1): a recovery that failed again let B replace A,
    and A's receipt then had no trace left for any later commit to book."""
    emit, calls = _receipts_down_for({HEAD_A})
    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", emit)
    _terminal(env, _verdict("pass", HEAD_A, 1))
    pending = _slot(env).read_bytes()
    assert json.loads(pending)["receipt_pending"] is True

    refused = None
    try:
        _terminal(env, _verdict("fail", HEAD_B, 2))
    except gate_recorder.ResultOverwriteRefused as exc:
        refused = exc
    record, booked = _on_disk(env), [r["commit_sha"] for r in _receipts(env)]
    assert (record["commit_sha"], record.get("receipt_pending"), booked) == (HEAD_A, True, []), (
        "A stays with its debt, and B books nothing"
    )
    assert _slot(env).read_bytes() == pending, "byte for byte"
    assert calls == [HEAD_A, HEAD_A], "the recovery was tried, B's receipt was not"
    assert refused is not None
    for part in (str(_slot(env)), f"commit_sha={HEAD_A!r}", "status='pass'", "attempt_seq=1"):
        assert part in str(refused)
    assert [line["reason_detail"] for line in _refusal_audit_lines(env)] == [str(refused)]

    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", _REAL_EMIT)
    _terminal(env, _verdict("fail", HEAD_B, 2))
    assert [(r["commit_sha"], r["gate_status"], r["attempt_seq"]) for r in _receipts(env)] == [
        (HEAD_A, "pass", 1), (HEAD_B, "fail", 2),
    ]
    record = _on_disk(env)
    assert (record["commit_sha"], record["status"], record["attempt_seq"]) == (HEAD_B, "fail", 2)
    assert "receipt_pending" not in record


def test_the_non_raising_writer_gets_the_refused_record_back(env, monkeypatch):
    emit, _ = _receipts_down_for({HEAD_A})
    monkeypatch.setattr(gate_recorder, "_emit_terminal_result_receipt", emit)
    _terminal(env, _verdict("pass", HEAD_A, 1))
    pending = _slot(env).read_bytes()

    returned = _not_executable(env, HEAD_B)
    assert returned["write_refused"] is True and returned["commit_sha"] == HEAD_A
    assert _slot(env).read_bytes() == pending and _receipts(env) == []


@pytest.mark.parametrize("status", ["pass", "fail"])
def test_the_same_outcome_written_twice_is_one_attempt_and_one_receipt(env, status):
    _terminal(env, _verdict(status, HEAD_A, 1))
    stored = _slot(env).read_bytes()

    repeat = _verdict(status, HEAD_A, 1)
    _terminal(env, repeat)
    record = _on_disk(env)
    assert record["attempt_seq"] == 1, "a repeat is no new attempt"
    assert len(_receipts(env)) == 1
    assert not record.get("receipt_pending")
    assert _slot(env).read_bytes() == stored, "no rewrite"
    assert "attempt_seq" not in repeat, "a write that did not land stamps nothing"


def test_a_repeat_returns_the_stored_record_and_publishes_nothing(env, monkeypatch):
    from gate_outcome import GateOutcome, commit_gate_outcome

    _terminal(env, _verdict("pass", HEAD_A, 1))
    stored = _on_disk(env)
    publish = mock.Mock()
    monkeypatch.setattr(gate_recorder, "publish_forge_check_run", publish)

    on_disk, written = commit_gate_outcome(
        GateOutcome.from_record(GATE, _verdict("pass", HEAD_A, 1), pr_id=""), results_dir=env["results_dir"],
    )
    assert (on_disk, written) == (stored, True)
    publish.assert_not_called()


def test_an_undecided_outcome_on_the_same_head_still_lands(env):
    """An outage owes no receipt, so the repeat rule leaves it alone: a retry's
    newer cause replaces the older one and counts as the next attempt."""
    _terminal(env, _outage(HEAD_A))
    retry = {**_outage(HEAD_A), "reason_detail": "429 weekly quota"}
    _terminal(env, retry)
    record = _on_disk(env)
    assert (record["reason_detail"], record["attempt_seq"]) == ("429 weekly quota", 2)
    assert _receipts(env) == []


@pytest.mark.parametrize("field,value", [
    ("commit_sha", "missing"), ("commit_sha", None), ("commit_sha", ""), ("commit_sha", "   "),
    ("status", "missing"), ("status", None), ("status", "mystery"),
    ("blocking_findings", [{"message": "a real defect"}]),
])
def test_a_repeat_needs_the_same_head_status_and_decision_on_the_slot(env, field, value):
    stored = _verdict("pass", HEAD_A)
    if value == "missing":
        del stored[field]
    else:
        stored[field] = value
    _slot(env).write_text(json.dumps(stored), encoding="utf-8")

    _terminal(env, _verdict("pass", HEAD_A))
    record = _on_disk(env)
    assert (record["commit_sha"], record["status"], record["attempt_seq"]) == (HEAD_A, "pass", 1)
    assert record.get("blocking_findings") == [] and len(_receipts(env)) == 1


def test_a_write_that_names_no_head_is_no_repeat(env):
    _terminal(env, _verdict("pass", HEAD_A))
    headless = _verdict("pass", HEAD_A)
    del headless["commit_sha"]

    _terminal(env, headless)
    record = _on_disk(env)
    assert "commit_sha" not in record and record["attempt_seq"] == 2


def test_a_refused_commit_writes_nothing_and_attempt_seq_counts_landed_records(env):
    from gate_outcome import GateOutcome, commit_gate_outcome

    _terminal(env, _verdict("pass", HEAD_A, 1))
    _terminal(env, _verdict("fail", HEAD_B, 2))
    before = _slot(env).read_bytes()
    outage = GateOutcome.from_record(GATE, _outage(HEAD_B), pr_id="")

    on_disk, written = commit_gate_outcome(outage, results_dir=env["results_dir"])
    assert (written, on_disk) == (False, json.loads(before))
    assert _slot(env).read_bytes() == before and len(_receipts(env)) == 2
    assert "attempt_seq" not in outage.record and "receipt_pending" not in outage.record
    with pytest.raises(gate_recorder.ResultOverwriteRefused):
        commit_gate_outcome(outage, results_dir=env["results_dir"], raise_on_refusal=True)

    _not_executable(env, "c" * 40)
    assert [_on_disk(env)[k] for k in ("status", "attempt_seq")] == ["not_executable", 3]
    with pytest.raises(ValueError, match="names no PR"):
        commit_gate_outcome(GateOutcome(GATE, "pass", "", None, {}), results_dir=env["results_dir"])


@pytest.mark.parametrize("seq", ["missing", None, "2", True, 0, -3])
def test_a_count_this_writer_did_not_write_restarts_at_one(env, seq):
    record = _verdict("pass", HEAD_A)
    if seq != "missing":
        record["attempt_seq"] = seq
    _slot(env).write_text(json.dumps(record), encoding="utf-8")

    _terminal(env, _verdict("fail", HEAD_B, 2))
    assert _on_disk(env)["attempt_seq"] == 1


def test_the_routed_writers_keep_their_record_shape(env):
    payload = {k: v for k, v in OLD_TERMINAL_RECORD.items() if k != "execution_depth"}
    out = env["results_dir"] / "pr-77-glm_gate.json"
    assert gate_recorder.record_terminal_result(
        gate="glm_gate", pr_id="77", result_path=out, payload=payload, execution_depth=_OK_DEPTH,
    ) == out
    assert json.loads(out.read_text(encoding="utf-8")) == payload == {**OLD_TERMINAL_RECORD, "attempt_seq": 1}

    returned = _not_executable(env, "d" * 40, pr=78)
    on_disk = json.loads(_slot(env, 78).read_text(encoding="utf-8"))
    for record in (returned, on_disk):
        assert isinstance(record.pop("recorded_at"), str)
        assert record == {**OLD_NOT_EXECUTABLE_RECORD, "attempt_seq": 1}


@pytest.mark.parametrize("literal,outcome", [
    ("completed", "pass"), ("approve", "pass"), ("failed", "fail"), ("blocked", "fail"),
    ("unavailable", "unavailable"), ("not_executable", "not_executable"),
])
def test_every_outcome_literal_maps_to_one_of_four(literal, outcome):
    from gate_outcome import GateOutcome

    assert GateOutcome.from_record(GATE, {"status": literal}, pr_id="1").status == outcome


@pytest.mark.parametrize("status", ["pending", "running", "partial_review", "", None, "mystery", "missing"])
def test_a_status_that_is_no_outcome_is_refused_before_anything_is_written(env, status):
    payload = _verdict("pass", HEAD_A)
    if status == "missing":
        del payload["status"]
    else:
        payload["status"] = status

    with pytest.raises(ValueError, match="one of"):
        _terminal(env, payload)
    assert not _slot(env).exists() and _receipts(env) == []
