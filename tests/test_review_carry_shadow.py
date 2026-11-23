"""Deltareview DR-8a (ADR-064 eis 2-6, failure modes F1-F5): a carried PASS in shadow."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
import review_carry_shadow as rcs
import review_rounds
from review_changekind import ChangeKindRecord, NO_CODE_CHANGE, REWORK, TRIVIAL_REBASE

PR = 77
PARENT, HEAD = "a" * 40, "b" * 40
SEAT = review_rounds.CODEX_SEAT
FIELDS = ("verdict_origin", "carried_from", "carry_rule", "carry_rule_version", "source_patch_id", "head_patch_id",
          "security_paths_touched", "plan_scope_rule_version")


def _round(head=PARENT, verdict="pass", seat=SEAT, pr=PR, takeover="") -> review_rounds.RoundLine:
    return review_rounds.RoundLine(
        project_id="p", pr_number=pr, head_sha=head, seat=seat, reviewer="codex_gate", takeover_from=takeover,
        verdict=verdict, blocking_count=0 if verdict == "pass" else 1, report_path="/r/x.md", dispatch_id="d-1",
        recorded_at="2026-11-12T00:00:00Z", branch="b", source="gate")


def _kind(kind=TRIVIAL_REBASE, security=None, reason=None, parent="p" * 40, head="q" * 40) -> ChangeKindRecord:
    return ChangeKindRecord(kind, parent, head, security or {}, "rule-1", reason)


def _carry(**kw) -> rcs.CarryLine:
    decision = rcs.decide_carry(_round(), _kind(**kw), pr=PR, seat=SEAT, head=HEAD, recorded_at="2026-11-12T01:00:00Z")
    assert decision.line, decision.reason
    return decision.line


def _write_rounds(gates: Path, lines) -> Path:
    path = review_rounds.ledger_path(gates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x.to_dict()) + "\n" for x in lines))
    return path


@pytest.mark.parametrize("kind", [NO_CODE_CHANGE, TRIVIAL_REBASE])
def test_carry_keeps_seat_and_reviewer_of_the_source_and_names_all_provenance(kind):
    line = _carry(kind=kind)
    assert (line.seat, line.reviewer, line.source_head_sha, line.head_sha) == (SEAT, "codex_gate", PARENT, HEAD)
    assert line.verdict_origin == "carried" and line.carried_from == f"{PR}:{SEAT}:{PARENT}"
    assert line.security_paths_touched == () and line.plan_scope_rule_version == "rule-1"
    assert rcs.validate_carry(line.to_dict()) == line


@pytest.mark.parametrize("field", FIELDS)
def test_f1_a_carry_without_each_provenance_field_is_refused(field):
    record = _carry().to_dict()
    del record[field]
    with pytest.raises(rcs.CarryInvalid):
        rcs.validate_carry(record)


@pytest.mark.parametrize("field", [f for f in FIELDS if f != "security_paths_touched"])
@pytest.mark.parametrize("bad", ["", "  ", None, 3])
def test_f1_an_empty_or_malformed_provenance_field_is_refused(field, bad):
    record = {**_carry().to_dict(), field: bad}
    with pytest.raises(rcs.CarryInvalid):
        rcs.validate_carry(record)


@pytest.mark.parametrize("paths", [["scripts/lib/x.py"], "x", None, ["not", "an", "object"]])
def test_validation_refuses_a_security_path_and_a_malformed_path_field(paths):
    record = {**_carry().to_dict(), "security_paths_touched": paths}
    with pytest.raises(rcs.CarryInvalid):
        rcs.validate_carry(record)
    with pytest.raises(rcs.CarryInvalid):
        rcs.validate_carry([record])


def test_f2_f4_a_booking_is_no_round_and_writes_only_the_shadow_file(tmp_path):
    gates = tmp_path / "review_gates"
    rounds = _write_rounds(gates, [_round()])
    before_bytes = rounds.read_bytes()
    before = review_rounds.codex_rounds(review_rounds.read_ledger(rounds).lines)
    def files() -> set:
        return {p.relative_to(gates).as_posix() for p in gates.rglob("*") if p.is_file()}

    files_before = files()
    written = rcs.book_carry(gates, _carry())
    assert written == gates / "shadow" / "carry.ndjson"
    assert files() - files_before == {"shadow/carry.ndjson"} and files_before <= files()
    assert review_rounds.codex_rounds(review_rounds.read_ledger(rounds).lines) == before
    assert rounds.read_bytes() == before_bytes
    assert '"kind"' not in written.read_text()


def test_booking_appends_one_line_per_carry_and_survives_a_torn_tail(tmp_path):
    gates = tmp_path / "g"
    rcs.book_carry(gates, _carry())
    path = rcs.carry_path(gates)
    path.write_bytes(path.read_bytes() + b'{"torn": ')
    rcs.book_carry(gates, _carry(kind=NO_CODE_CHANGE))
    lines, skipped = rcs.read_carry(gates)
    assert (len(lines), skipped) == (2, 1)


def test_reader_skips_and_counts_an_invalid_line_and_never_makes_it_a_verdict(tmp_path):
    gates = tmp_path / "g"
    bad = _carry().to_dict()
    del bad["carry_rule"]
    rcs.carry_path(gates).parent.mkdir(parents=True)
    rcs.carry_path(gates).write_text(json.dumps(bad) + "\nnot json\n\n")
    lines, skipped = rcs.read_carry(gates)
    assert (lines, skipped) == ((), 2)
    assert rcs.effective_shadow_verdict([], lines, PR, SEAT, HEAD) is None
    assert rcs.read_carry(tmp_path / "absent") == ((), 0)


def test_f3_a_fresh_verdict_on_the_same_head_wins_in_both_orders(tmp_path):
    carry = _carry()
    fresh = _round(head=HEAD, verdict="fail")
    assert rcs.effective_shadow_verdict([_round()], [carry], PR, SEAT, HEAD) == carry
    assert rcs.effective_shadow_verdict([_round(), fresh], [carry], PR, SEAT, HEAD) == fresh  # carry booked first
    gates = tmp_path / "g"
    rcs.book_carry(gates, carry)  # a fresh line arriving after the booking
    later = _round(head=HEAD, verdict="pass")
    assert rcs.effective_shadow_verdict([fresh, later], rcs.read_carry(gates)[0], PR, SEAT, HEAD) == later
    assert rcs.effective_shadow_verdict([fresh], [carry], PR, SEAT, "c" * 40) is None


@pytest.mark.parametrize("record,reason", [
    (_kind(kind=REWORK, reason="rebase_with_rework"), "changekind_rework"),  # F5: a rebase with a content change
    (_kind(kind="WHATEVER"), "changekind_whatever"),
    (_kind(security={"scripts/lib/a.py": "auth"}), "security_path"),
    (_kind(kind=NO_CODE_CHANGE, security={"scripts/lib/p.py": "permission"}), "security_path"),
    (_kind(reason="cannot_prove"), "cannot_prove_cannot_prove"),
    (_kind(parent=None), "cannot_prove_patch_id"),
    (_kind(head=""), "cannot_prove_patch_id"),
])
def test_unprovable_or_unsafe_changes_give_no_carry_and_a_reason(record, reason):
    decision = rcs.decide_carry(_round(), record, pr=PR, seat=SEAT, head=HEAD)
    assert decision.line is None and decision.reason == reason


@pytest.mark.parametrize("source,seat,pr,reason", [
    (_round(), "kimi_gate", PR, "other_seat"),
    (_round(seat="kimi_gate"), SEAT, PR, "other_seat"),
    (_round(), SEAT, PR + 1, "other_pr"),
    (_round(verdict="fail"), SEAT, PR, "source_not_pass"),
    (_round(takeover="kimi_gate"), SEAT, PR, "source_is_takeover"),
])
def test_another_seat_pr_failed_or_takeover_source_gives_no_carry(source, seat, pr, reason):
    assert rcs.decide_carry(source, _kind(), pr=pr, seat=seat, head=HEAD).reason == reason


def test_f5_equal_head_sha_is_never_proof_and_a_missing_head_gives_no_carry():
    assert rcs.decide_carry(_round(head=HEAD), _kind(), pr=PR, seat=SEAT, head=HEAD).reason == "no_new_head"
    assert rcs.decide_carry(_round(), _kind(), pr=PR, seat=SEAT, head="").line is None


def test_c4_the_ledger_readers_never_open_the_carry_file(tmp_path, monkeypatch):
    gates = tmp_path / "state" / "review_gates"
    rounds = _write_rounds(gates, [_round(), _round(head=HEAD, verdict="fail")])
    clean = review_rounds.read_ledger(rounds)
    expected = (clean.lines, clean.bad, review_rounds.codex_rounds(clean.lines))
    carry = rcs.carry_path(gates)
    carry.parent.mkdir(parents=True)
    carry.write_text('{"corrupt": \n\x00\x00')
    carry.chmod(0)
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1])
    try:
        again = review_rounds.read_ledger(rounds)
        assert (again.lines, again.bad, review_rounds.codex_rounds(again.lines)) == expected
    finally:
        carry.chmod(0o644)
    assert not [p for p in opened if p.endswith("carry.ndjson")]
