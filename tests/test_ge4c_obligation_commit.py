"""GE-4c (dlv-a4644be8f2ee): the obligation booked in the record's slot lock (ADR-046 step 8).

Every test drives the public entry points — ``commit_gate_outcome`` and its
obligation-only sibling ``commit_obligation_outcome`` — so the red run on the
base head shows the gap each one closes: there, an obligation outcome was a
separate ``gate_obligations.update_obligation`` call outside the record's
critical section (or, for the runner, after it), and none of the four
artefacts shared a failure contract.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_obligations
import gate_recorder
import review_stack_support

GATE, PR, BRANCH = "codex_gate", 4407, "feat/ge-4c"
HEAD_A, HEAD_B = "a" * 40, "b" * 40
DISPATCH = "codex-gate-pr4407-1"
OTHER_DISPATCH = "codex-gate-pr4407-2"
BOOKED_AT = "2026-10-27T10:00:00Z"

# What the runner books an existing evidence file as (gate_obligation_runner._book_existing_evidence).
FULFILLED_FIELDS = {
    "status": gate_obligations.STATUS_FULFILLED,
    "reason": "fulfilled_by_existing_evidence",
    "reason_detail": "codex_gate already passed on this head",
    "resolved_at": BOOKED_AT,
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    return review_stack_support.build_project_store(tmp_path, monkeypatch)


def _slot(env) -> Path:
    return env["results_dir"] / f"pr-{PR}-{GATE}.json"


def _request_slot(env) -> Path:
    return env["requests_dir"] / f"pr-{PR}-{GATE}.json"


def _register(env, dispatch_id: str = DISPATCH) -> Path:
    path = gate_obligations.register_obligation(
        env["state_dir"], dispatch_id=dispatch_id, gate=GATE, pr_number=PR, pr_id=str(PR), branch=BRANCH,
    )
    assert path is not None
    return path


def _obligation(path: Path, **fields):
    from gate_outcome import GateObligation

    return GateObligation(path=path, fields={**FULFILLED_FIELDS, **fields})


def _record(head: str, status: str = "pass", n: int = 1) -> dict:
    return {
        "gate": GATE, "pr_id": "", "pr_number": PR, "status": status, "commit_sha": head,
        "contract_hash": f"hash-{n}", "report_path": f"/tmp/codex-{n}.md", "dispatch_id": DISPATCH,
        "blocking_findings": [] if status == "pass" else [{"message": "a real defect"}],
        "provider": "codex-harness", "model": "gpt-5-codex", "branch": BRANCH,
    }


def _commit(env, head: str = HEAD_A, status: str = "pass", *, obligation=None, request=None,
            raise_on_refusal: bool = False, n: int = 1):
    from gate_outcome import GateOutcome, commit_gate_outcome

    outcome = GateOutcome(gate=GATE, status="pass" if status == "pass" else "unavailable",
                          pr_id="", pr_number=PR, record=_record(head, status, n))
    return commit_gate_outcome(
        outcome, results_dir=env["results_dir"], obligation=obligation, request=request,
        raise_on_refusal=raise_on_refusal,
    )


def _payload(status: str = "executing", **extra) -> dict:
    return {"gate": GATE, "status": status, "pr_number": PR, "pr_id": str(PR), "branch": BRANCH,
            "commit_sha": HEAD_A, "contract_hash": "hash-1", **extra}


def _request(env, status: str = "executing", **extra) -> dict:
    request = _payload(status, **extra)
    gate_recorder.persist_request(env["requests_dir"], GATE, request, pr_number=PR, pr_id="")
    return request


def _gate_request(env, request: dict):
    from gate_outcome import GateRequest

    return GateRequest(env["requests_dir"], request, {"completed_at": BOOKED_AT})


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _receipts(env) -> list:
    out = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):
        path = env["state_dir"] / name
        if path.exists():
            lines = (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip())
            out.extend(r for r in lines if r.get("event_type") == "review_gate_result")
    return out


def _obligation_files(env) -> list:
    root = env["state_dir"] / "review_gates" / "obligations"
    return sorted(root.glob("*.json")) if root.is_dir() else []


# ---------------------------------------------------------------------------
# One call, four artefacts
# ---------------------------------------------------------------------------


def test_one_commit_books_one_record_one_receipt_one_request_end_and_one_obligation(env):
    obligation_path = _register(env)
    request = _request(env)

    record, written = _commit(env, obligation=_obligation(obligation_path), request=_gate_request(env, request))

    assert written is True and record["attempt_seq"] == 1
    assert [p.name for p in env["results_dir"].glob(f"pr-{PR}-*.json")] == [_slot(env).name], "exactly one record"
    on_disk = _json(_slot(env))
    assert (on_disk["status"], on_disk["attempt_seq"]) == ("pass", 1)
    assert "obligation_pending" not in on_disk and "obligation_booking" not in on_disk, "the debt is cleared"
    assert len(_receipts(env)) == 1
    assert _json(_request_slot(env))["status"] == "completed"
    assert [p.name for p in _obligation_files(env)] == [obligation_path.name], "exactly one obligation"
    booked = _json(obligation_path)
    assert booked["status"] == gate_obligations.STATUS_FULFILLED
    assert booked["booked_for"] == {"gate": GATE, "pr_number": PR, "commit_sha": HEAD_A, "attempt_seq": 1}
    assert booked["reason"] == "fulfilled_by_existing_evidence", "the outcome's own fields land"


def test_a_refused_commit_books_no_record_no_receipt_no_obligation_and_ends_its_request(env):
    obligation_path = _register(env)
    request = _request(env)
    _slot(env).write_text('{"status": "pa', encoding="utf-8")
    before_obligation = obligation_path.read_bytes()

    record, written = _commit(env, obligation=_obligation(obligation_path), request=_gate_request(env, request))

    assert written is False
    assert _slot(env).read_text(encoding="utf-8") == '{"status": "pa', "the refused record is untouched"
    assert _receipts(env) == [], "a refusal books no receipt"
    assert obligation_path.read_bytes() == before_obligation, "a refusal books no obligation"
    ended = _json(_request_slot(env))
    assert (ended["status"], ended["failure_reason"]) == ("failed", "write_refused"), "step 9, not the run's end"
    assert "completed_at" not in ended
    assert record.get("status") == "pass" and "attempt_seq" not in record, "the caller's dict stays unstamped"


def test_two_threads_on_one_slot_one_wins_and_the_loser_books_no_obligation(env, monkeypatch):
    winner_obligation, loser_obligation = _register(env), _register(env, OTHER_DISPATCH)
    real_emit, winner_emitting, results = gate_recorder._emit_terminal_result_receipt, threading.Event(), []

    def loser():
        assert winner_emitting.wait(5)
        results.append(_commit(
            env, status="unavailable", obligation=_obligation(loser_obligation, status="failed"),
            raise_on_refusal=False, n=2,
        ))

    monkeypatch.setattr(
        gate_recorder, "_emit_terminal_result_receipt",
        lambda payload, **kw: (winner_emitting.set(), real_emit(payload, **kw))[1],
    )
    thread = threading.Thread(target=loser)
    thread.start()
    assert _commit(env, obligation=_obligation(winner_obligation))[1] is True
    thread.join(10)
    assert not thread.is_alive()

    assert results and results[0][1] is False, "the loser was refused"
    on_disk = _json(_slot(env))
    assert (on_disk["status"], on_disk["attempt_seq"]) == ("pass", 1)
    assert len(_receipts(env)) == 1
    assert _json(winner_obligation)["status"] == gate_obligations.STATUS_FULFILLED
    assert _json(winner_obligation)["booked_for"]["attempt_seq"] == 1
    assert _json(loser_obligation)["status"] == gate_obligations.STATUS_PENDING, "the loser's obligation stays"
    assert "booked_for" not in _json(loser_obligation)


# ---------------------------------------------------------------------------
# Crash between the record and the obligation: the next commit pays the debt once
# ---------------------------------------------------------------------------


def test_a_crash_between_the_record_and_the_obligation_books_it_once_on_the_next_commit(env, monkeypatch):
    obligation_path = _register(env)
    real_book, calls = gate_obligations.book_obligation_outcome, []

    def crashes_once(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("the obligation store is on fire")
        return real_book(*args, **kwargs)

    monkeypatch.setattr(gate_obligations, "book_obligation_outcome", crashes_once)

    record, written = _commit(env, obligation=_obligation(obligation_path))

    assert written is True, "the record and the receipt landed"
    assert len(_receipts(env)) == 1
    assert _json(obligation_path)["status"] == gate_obligations.STATUS_PENDING, "the booking did not land"
    owed = _json(_slot(env))
    assert owed["obligation_pending"] is True, "the record is the durable work order"
    assert owed["obligation_booking"]["path"] == str(obligation_path)
    assert owed["obligation_booking"]["fields"]["status"] == gate_obligations.STATUS_FULFILLED

    # The next commit on the slot pays the debt before it looks at its own write.
    repeated, written = _commit(env, obligation=_obligation(obligation_path))

    assert written is True
    booked = _json(obligation_path)
    assert booked["status"] == gate_obligations.STATUS_FULFILLED
    assert booked["booked_for"] == {"gate": GATE, "pr_number": PR, "commit_sha": HEAD_A, "attempt_seq": 1}, (
        "the booking names the attempt that owed it, not the one that paid it"
    )
    assert "obligation_pending" not in _json(_slot(env)), "the debt is cleared once"
    settled, receipts = obligation_path.read_bytes(), len(_receipts(env))

    # A third commit finds no debt: the obligation is not booked a second time.
    _commit(env, obligation=_obligation(obligation_path))
    assert obligation_path.read_bytes() == settled
    assert len(_receipts(env)) == receipts == 1


def test_a_pending_booking_that_cannot_be_read_refuses_the_next_commit(env):
    _commit(env)
    record = {**_json(_slot(env)), "obligation_pending": True, "obligation_booking": "not a booking"}
    _slot(env).write_text(json.dumps(record), encoding="utf-8")
    before = _slot(env).read_bytes()

    from gate_outcome import ObligationPendingRefused

    with pytest.raises(ObligationPendingRefused):
        _commit(env, HEAD_B, obligation=_obligation(_register(env)), raise_on_refusal=True)

    assert _slot(env).read_bytes() == before, "replacing the record would drop the only trace of the debt"
    assert len(_receipts(env)) == 1


def test_a_pending_booking_that_fails_again_refuses_the_next_commit(env, monkeypatch):
    obligation_path = _register(env)
    real_book = gate_obligations.book_obligation_outcome
    monkeypatch.setattr(gate_obligations, "book_obligation_outcome",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("still on fire")))
    _commit(env, obligation=_obligation(obligation_path))
    monkeypatch.setattr(gate_obligations, "book_obligation_outcome", real_book)
    before, obligation_before = _slot(env).read_bytes(), obligation_path.read_bytes()

    from gate_outcome import ObligationPendingRefused

    monkeypatch.setattr(gate_obligations, "book_obligation_outcome",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("still on fire")))
    with pytest.raises(ObligationPendingRefused):
        _commit(env, HEAD_B, raise_on_refusal=True)

    assert _slot(env).read_bytes() == before and obligation_path.read_bytes() == obligation_before
    assert len(_receipts(env)) == 1


# ---------------------------------------------------------------------------
# The obligation-only entry point the runner uses
# ---------------------------------------------------------------------------


def test_the_obligation_only_commit_rewrites_no_record_and_books_once(env):
    from gate_outcome import commit_obligation_outcome

    _commit(env)
    before = _slot(env).read_bytes()
    obligation_path = _register(env)
    obligation = _obligation(obligation_path)

    booked = commit_obligation_outcome(obligation, result_path=_slot(env))

    assert _slot(env).read_bytes() == before, "a decided record is not rewritten for a booking"
    assert booked["status"] == gate_obligations.STATUS_FULFILLED
    assert booked["booked_for"]["attempt_seq"] == 1 and booked["booked_for"]["commit_sha"] == HEAD_A
    settled = obligation_path.read_bytes()

    assert commit_obligation_outcome(obligation, result_path=_slot(env))["status"] == gate_obligations.STATUS_FULFILLED
    assert obligation_path.read_bytes() == settled, "idempotent by value"

    # A different booking for the same attempt still lands: idempotency is by value, not by tuple.
    changed = commit_obligation_outcome(
        _obligation(obligation_path, reason_detail="the runner's later, more specific finding"),
        result_path=_slot(env),
    )
    assert changed["reason_detail"] == "the runner's later, more specific finding"


def test_an_obligation_only_commit_pays_a_pending_debt_before_its_own(env, monkeypatch):
    from gate_outcome import commit_obligation_outcome

    first, second = _register(env), _register(env, OTHER_DISPATCH)
    real_book = gate_obligations.book_obligation_outcome
    monkeypatch.setattr(gate_obligations, "book_obligation_outcome",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("store down")))
    _commit(env, obligation=_obligation(first))
    monkeypatch.setattr(gate_obligations, "book_obligation_outcome", real_book)
    assert _json(first)["status"] == gate_obligations.STATUS_PENDING

    commit_obligation_outcome(_obligation(second), result_path=_slot(env))

    assert _json(first)["status"] == gate_obligations.STATUS_FULFILLED, "the older debt is paid first"
    assert _json(second)["status"] == gate_obligations.STATUS_FULFILLED
    assert _json(first)["booked_for"]["attempt_seq"] == 1
    assert "obligation_pending" not in _json(_slot(env))


@pytest.mark.parametrize("content", ["{not json", '[1, 2]'])
def test_a_booking_into_an_unreadable_obligation_is_loud(env, content):
    from gate_outcome import commit_obligation_outcome

    _commit(env)
    obligation_path = _register(env)
    obligation_path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        commit_obligation_outcome(_obligation(obligation_path), result_path=_slot(env))


def test_a_booking_into_an_obligation_nobody_registered_is_loud(env):
    from gate_outcome import commit_obligation_outcome

    _commit(env)
    missing = env["state_dir"] / "review_gates" / "obligations" / "never-registered.json"

    with pytest.raises(ValueError):
        commit_obligation_outcome(_obligation(missing), result_path=_slot(env))


# ---------------------------------------------------------------------------
# GE-4c round 2, finding 1: a repeat that books an obligation writes its work
# order first, so a booking that raises is recoverable instead of lost
# ---------------------------------------------------------------------------


def test_a_repeat_that_cannot_book_leaves_a_work_order_the_next_commit_pays_once(env, monkeypatch):
    obligation_path = _register(env)
    _commit(env)  # the decided outcome lands first, without an obligation
    real_book, calls = gate_obligations.book_obligation_outcome, []

    def crashes_once(path, fields, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("the obligation store is on fire")
        return real_book(path, fields, **kwargs)

    monkeypatch.setattr(gate_obligations, "book_obligation_outcome", crashes_once)

    repeated, written = _commit(env, obligation=_obligation(obligation_path))

    assert written is True and repeated["attempt_seq"] == 1, "a repeat advances no attempt"
    owed = _json(_slot(env))
    assert owed["obligation_pending"] is True, "the repeat path persists its work order before booking"
    assert owed["obligation_booking"]["path"] == str(obligation_path)
    assert owed["obligation_booking"]["fields"]["status"] == gate_obligations.STATUS_FULFILLED
    assert _json(obligation_path)["status"] == gate_obligations.STATUS_PENDING, "the booking did not land"

    # The next commit on the slot pays that debt, exactly once.
    _commit(env, obligation=_obligation(obligation_path))

    booked = _json(obligation_path)
    assert booked["status"] == gate_obligations.STATUS_FULFILLED
    assert booked["booked_for"] == {"gate": GATE, "pr_number": PR, "commit_sha": HEAD_A, "attempt_seq": 1}
    assert "obligation_pending" not in _json(_slot(env)), "the marker is cleared"
    settled = obligation_path.read_bytes()

    _commit(env, obligation=_obligation(obligation_path))
    assert obligation_path.read_bytes() == settled, "booked once, not once per commit"


def test_a_repeat_with_an_obligation_books_once_and_leaves_no_marker(env):
    obligation_path = _register(env)
    _commit(env)

    repeated, written = _commit(env, obligation=_obligation(obligation_path))

    assert written is True and repeated["attempt_seq"] == 1
    booked = _json(obligation_path)
    assert booked["status"] == gate_obligations.STATUS_FULFILLED
    assert booked["booked_for"]["attempt_seq"] == 1, "booked for the attempt the outcome names"
    assert "obligation_pending" not in _json(_slot(env)), "the work order is cleared by the booking"
    assert "obligation_pending" not in repeated
    settled = obligation_path.read_bytes()

    _commit(env, obligation=_obligation(obligation_path))
    assert obligation_path.read_bytes() == settled, "idempotent by value"


# ---------------------------------------------------------------------------
# GE-4c round 2, finding 2: a refused writer ends the request it HOLDS, never
# the one on the slot that belongs to someone else
# ---------------------------------------------------------------------------


def _refused(env, held: dict):
    """A refusal on the slot (the pass already there vs an ``unavailable`` on the same head)."""
    record, written = _commit(env, status="unavailable", request=_gate_request(env, held))
    assert written is False, "the decided pass on the slot refuses the outage"
    return record


def test_a_refused_write_leaves_a_completed_request_and_its_metadata_intact(env, caplog):
    _commit(env)  # a decided pass holds the result slot
    completed = _payload("completed", commit_sha=HEAD_A, completed_at=BOOKED_AT)
    gate_recorder.persist_request(env["requests_dir"], GATE, completed, pr_number=PR, pr_id="")
    before = _request_slot(env).read_bytes()

    with caplog.at_level(logging.WARNING, logger="gate_outcome"):
        _refused(env, _payload())  # the caller's own request never reached this slot

    assert _request_slot(env).read_bytes() == before, "a completed request keeps its status and completed_at"
    assert "request_already_terminal" in caplog.text, "the refusal is named"


def test_a_refused_write_leaves_a_newer_retry_in_the_same_slot_untouched(env, caplog):
    _commit(env)
    retry = _payload("executing", commit_sha=HEAD_B, contract_hash="hash-2",
                     requested_at="2026-10-27T11:00:00Z")
    gate_recorder.persist_request(env["requests_dir"], GATE, retry, pr_number=PR, pr_id="")
    before = _request_slot(env).read_bytes()

    held = _payload(requested_at="2026-10-27T10:00:00Z")  # the attempt that ran, one round earlier
    with caplog.at_level(logging.WARNING, logger="gate_outcome"):
        _refused(env, held)

    assert _request_slot(env).read_bytes() == before, "the newer retry in the same gate/PR slot stays"
    assert "request_belongs_to_another_attempt" in caplog.text, "the refusal is named"


def test_a_refused_write_still_ends_its_own_executing_request(env):
    _commit(env)
    held = _request(env)  # the caller's request IS the record on the slot

    _refused(env, held)

    ended = _json(_request_slot(env))
    assert (ended["status"], ended["failure_reason"]) == ("failed", "write_refused")
