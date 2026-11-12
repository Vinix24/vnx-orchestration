"""Deltareview DR-4b: the shadow delta run next to the full codex round, booked in shadow/delta.ndjson (no model runs)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from review_stack_support import BRANCH, PR  # importing it puts scripts/ and scripts/lib on sys.path
import review_delta as rd
import review_delta_shadow as ds
import review_rounds as rr
from review_changekind import ChangeKindRecord, Interdiff
HEAD_A, HEAD_B = "a" * 40, "b" * 40
OPEN_ID = "f" * 16


def _ledger(tmp_path: Path, heads=(HEAD_A, HEAD_B), seat="codex_gate") -> Path:
    gates = tmp_path / "state" / "review_gates"
    gates.mkdir(parents=True)
    project = rr.store_project_id(gates)
    rows: List[Dict[str, Any]] = [{"kind": "opened", "project_id": project, "pr_number": PR, "basis": "new",
                                   "evidence": 0, "recorded_at": "2026-11-12T00:00:00Z", "source": "gate"}]
    for n, head in enumerate(heads):
        rows.append(rr.RoundLine(
            project_id=project, pr_number=PR, head_sha=head, seat=seat, reviewer="codex_gate", takeover_from="",
            verdict="fail" if n == 0 else "pass", blocking_count=1 if n == 0 else 0, report_path=f"/r/{n}.md",
            dispatch_id=f"d-{n}", recorded_at=f"2026-11-12T00:0{n}:00Z", branch=BRANCH, source="gate",
            finding_ids=(OPEN_ID,) if n == 0 else ()).to_dict())
    (gates / "rounds.ndjson").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp_path / "state"


def _record(kind: str = "NO_CODE_CHANGE", reason=None):
    return rd.DeltaInput(PR, "codex_gate", HEAD_A, "fail", "0.md", HEAD_B,
                         ChangeKindRecord(kind, "p", "q", {}, "1", reason), Interdiff(frozenset({"x.py"}), {}),
                         (rd.OpenFinding(OPEN_ID, "x.py", "f", "blocking", "broken"),), ())


def _answer(status: str) -> str:
    return '```delta_verdict/1\n{"verdict":"pass","findings":[{"id":"%s","status":"%s"}],"new_findings":[]}\n```\n' % (OPEN_ID, status)


@pytest.fixture
def shadow(tmp_path, monkeypatch):

    monkeypatch.setattr(ds, "create_gate_worktree", lambda **kw: tmp_path / "wt")
    monkeypatch.setattr(ds, "remove_gate_worktree", lambda *a, **kw: None)
    monkeypatch.setattr(ds.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=HEAD_B, returncode=0))
    calls: List[str] = []

    def go(state, record=None, answer=None, **payload):
        monkeypatch.setattr(ds, "assemble_delta_input", lambda *a: (record or _record(), (OPEN_ID,)))

        def run(prompt, cwd):
            calls.append(prompt)
            return {"status": "completed", "stdout": answer or _answer("fixed"), "duration_seconds": 1.5}

        request = {"pr_number": PR, "commit_sha": HEAD_B, "branch": BRANCH, "risk_class": "medium", **payload}
        return ds.run_shadow(request_payload=request, state_dir=state, project_root=tmp_path, run=run)

    go.calls = calls
    return go


def _shadow_lines(state: Path) -> List[Dict[str, Any]]:

    return ds.read_shadow(ds.shadow_path(state / "review_gates"))


def test_a_follow_up_head_runs_the_delta_and_books_it_next_to_the_full_round(tmp_path, shadow):
    state = _ledger(tmp_path)
    rounds = (state / "review_gates" / "rounds.ndjson")
    before = rounds.read_bytes()
    line = shadow(state)
    assert len(shadow.calls) == 1 and HEAD_A in shadow.calls[0] and OPEN_ID in shadow.calls[0]
    assert line["delta_verdict"] == "pass" and line["parent_head"] == HEAD_A and line["full_verdict"] == "pass"
    assert _shadow_lines(state) == [json.loads(json.dumps(line))]
    assert rounds.read_bytes() == before


@pytest.mark.parametrize("reason", ["security_path", "rebase_with_rework"])
def test_a_forced_full_review_runs_nothing_and_books_the_reason(tmp_path, shadow, reason):
    line = shadow(_ledger(tmp_path), record=_record("REWORK", reason))
    assert not shadow.calls and line["full_review_reason"] == reason and "delta_verdict" not in line


def test_uncomputable_input_forces_the_full_review():

    parent = rr.RoundLine("p", PR, HEAD_A, "codex_gate", "codex_gate", "", "fail", 1, "/nope.md", "d", "t", finding_ids=(OPEN_ID,))
    record, _ = ds.assemble_delta_input(Path("/nonexistent"), "origin/main", PR, HEAD_B, parent)
    assert record.changekind is None and rd.full_review_reason(record) == "cannot_prove"


@pytest.mark.parametrize("kw,heads", [
    ({"seat": "kimi_gate"}, (HEAD_A, HEAD_B)), ({"risk_class": "high"}, (HEAD_A, HEAD_B)), ({}, (HEAD_B,)),
])
def test_second_seat_high_pr_and_first_head_run_nothing(tmp_path, shadow, kw, heads):

    seat = kw.get("seat", "codex_gate")
    state = _ledger(tmp_path, heads=heads, seat=seat)
    rounds = rr.codex_rounds(rr.read_ledger(rr.ledger_path(state / "review_gates")).lines)
    verdict = ds.shadow_eligible(seat=seat, risk_class=kw.get("risk_class", "medium"), rounds=rounds, head=HEAD_B,
                                 shadow_lines=[], pr_number=PR)
    assert verdict.reason
    assert shadow(state, **{k: v for k, v in kw.items() if k == "risk_class"}) is None and not shadow.calls


def test_the_21st_pair_and_a_full_chain_run_nothing(tmp_path, shadow, monkeypatch):

    state = _ledger(tmp_path)
    for _ in range(20):
        ds.book(ds.shadow_path(state / "review_gates"), {"pr_number": 1, "delta_verdict": "pass"})
    assert shadow(state) is None and not shadow.calls
    monkeypatch.setenv(ds.MAX_CHAIN_KEY, "x")
    assert ds.max_chain() == 2


def test_a_blocker_answered_open_is_a_fail_line(tmp_path, shadow):
    line = shadow(_ledger(tmp_path), answer=_answer("open"))
    assert line["delta_verdict"] == "fail" and any("blocker ids not fixed" in r for r in line["reasons"])


def test_a_shadow_step_that_raises_changes_nothing_but_the_error_line(tmp_path, shadow, monkeypatch):

    state = _ledger(tmp_path)
    gates = state / "review_gates"
    before = (gates / "rounds.ndjson").read_bytes()
    monkeypatch.setattr(ds, "shadow_eligible", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert shadow(state) is None
    assert (gates / "rounds.ndjson").read_bytes() == before and not (gates / "results").exists()
    assert _shadow_lines(state)[0]["reasons"] == ["shadow_error"]


def test_a_corrupt_shadow_file_changes_nothing_the_ledger_returns(tmp_path, shadow):

    state = _ledger(tmp_path)
    path = rr.ledger_path(state / "review_gates")
    clean = rr.read_ledger(path)
    shadow_file = ds.shadow_path(state / "review_gates")
    shadow_file.parent.mkdir()
    shadow_file.write_bytes(b"{not json\n\xff\xfe\n")
    again = rr.read_ledger(path)
    assert again.lines == clean.lines and not again.bad and rr.codex_rounds(again.lines) == rr.codex_rounds(clean.lines)
    assert shadow(state)["delta_verdict"] == "pass"


def test_a_failed_error_booking_is_logged_and_run_shadow_stays_fail_open(tmp_path, shadow, monkeypatch, caplog):
    state = _ledger(tmp_path)
    monkeypatch.setattr(ds, "book", lambda path, record: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(ds, "shadow_eligible", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level("WARNING", logger=ds.logger.name):
        assert shadow(state) is None
    booking = [r.getMessage() for r in caplog.records if "OSError" in r.getMessage()]
    assert booking and "disk full" in booking[0] and "delta.ndjson" in booking[0]


def test_corrupt_shadow_lines_are_counted_and_a_clean_file_logs_nothing(tmp_path, caplog):
    path, clean = tmp_path / "delta.ndjson", tmp_path / "clean.ndjson"
    path.write_bytes(b'{"a":1}\n{not json\n[1]\n\n{"b":2}\n')
    clean.write_text('{"a":1}\n')
    with caplog.at_level("WARNING", logger=ds.logger.name):
        assert ds.read_shadow(clean) == [{"a": 1}] and ds.read_shadow(tmp_path / "none") == [] and not caplog.records
        assert ds.read_shadow(path) == [{"a": 1}, {"b": 2}]
    assert any("2 unreadable" in r.getMessage() for r in caplog.records)


def _stream(*messages: str, usage=None) -> str:
    events = [{"type": "thread.started", "thread_id": "t"}]
    events += [{"type": "item.completed", "item": {"id": f"i{n}", "type": "agent_message", "text": m}}
               for n, m in enumerate(messages)]
    events.append({"type": "turn.completed", "usage": usage} if usage is not None else {"type": "turn.completed"})
    return "".join(json.dumps(e) + "\n" for e in events)


def test_the_final_message_of_a_codex_stream_is_the_answer(tmp_path, shadow):
    line = shadow(_ledger(tmp_path), answer=_stream(_answer("fixed")))
    assert line["delta_verdict"] == "pass" and "tokens" not in line


def test_an_earlier_fenced_message_does_not_spoil_the_final_answer(tmp_path, shadow):
    stream = _stream("Looking.\n```json\n{}\n```\n", _answer("fixed"))
    assert shadow(_ledger(tmp_path), answer=stream)["delta_verdict"] == "pass"


def test_text_after_the_block_in_the_final_message_is_a_fail(tmp_path, shadow):
    line = shadow(_ledger(tmp_path), answer=_stream(_answer("fixed") + "Done."))
    assert line["delta_verdict"] == "fail" and rd.REASON_TRAILING in line["reasons"]


def test_plain_text_that_is_not_a_stream_is_parsed_as_it_is(tmp_path, shadow):
    assert shadow(_ledger(tmp_path), answer=_answer("fixed"))["delta_verdict"] == "pass"


def test_usage_is_booked_when_present_and_left_out_when_malformed(tmp_path, shadow):
    line = shadow(_ledger(tmp_path), answer=_stream(_answer("fixed"), usage={"input_tokens": 120, "output_tokens": 7}))
    assert line["tokens"] == {"input": 120, "output": 7}
    bad = shadow(_ledger(tmp_path / "b"), answer=_stream(_answer("fixed"), usage={"input_tokens": "x", "output_tokens": None}))
    assert "tokens" not in bad


def test_the_full_round_unwrap_still_joins_every_message():
    from codex_parser import _extract_codex_text
    assert _extract_codex_text(_stream("one", "two")) == "one\ntwo"
