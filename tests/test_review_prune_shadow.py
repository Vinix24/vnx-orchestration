"""Deltareview DR-8b (ADR-064 besluit 4, eisen 6, 8, 10; C4): pruning booked in shadow, only the next request."""
from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from review_stack_support import PR  # also puts scripts/ and scripts/lib on sys.path

import review_prune_shadow as rps
import review_rounds
import review_seat_evaluation as rse
from review_changekind import ChangeKindRecord

HEAD_P, HEAD_N = "a" * 40, "b" * 40
SETS = [("f1", "open"), ("f2", "fixed")]


def _round(head: str = HEAD_N, parent: str = HEAD_P, seat: str = "codex_gate", **extra: Any) -> Any:
    return review_rounds.RoundLine(
        "p", PR, head, seat, "codex_gate", "", "fail", 1, "/r/x.md", "d-2", "2026-11-12T00:00:00Z", branch="b",
        source="gate", parent_head_sha=parent, **extra)


def _record(kind: str = "NO_CODE_CHANGE", security: Any = None, reason: Any = None) -> Any:
    return ChangeKindRecord(kind, "p" * 40, "q" * 40, security or {}, "v1", reason)


def _args(**over: Any) -> tuple:
    args: dict = dict(rnd=_round(), parent=_round(HEAD_P, parent=""), record=_record(), findings=SETS,
                      parent_findings=SETS, request_seq=2)
    return tuple({**args, **over}.values())


def _decide(**over: Any) -> Any:
    return rps.decide_unchanged(*_args(**over))


def test_hash_is_order_independent_and_status_sensitive():
    assert rps.finding_set_hash(SETS) == rps.finding_set_hash(list(reversed(SETS)))
    assert rps.finding_set_hash(SETS) == rps.finding_set_hash(SETS + SETS)
    assert rps.finding_set_hash(SETS) != rps.finding_set_hash([("f1", "fixed"), ("f2", "fixed")])
    assert rps.finding_set_hash(SETS) != rps.finding_set_hash(SETS[:1])


def test_empty_set_has_its_own_defined_hash():
    assert rps.finding_set_hash([]) == rps.EMPTY_SET_HASH != rps.finding_set_hash(SETS)


def test_malformed_pairs_raise():
    for bad in ([("f1", "bogus")], [("", "open")], [("f1",)], ["f1"], [(None, "open")]):
        with pytest.raises(ValueError):
            rps.finding_set_hash(bad)


@pytest.mark.parametrize("kind", ["NO_CODE_CHANGE", "TRIVIAL_REBASE"])
def test_equal_sets_on_a_trivial_change_are_unchanged(kind):
    d = _decide(record=_record(kind))
    assert d.unchanged is True and d.finding_set_hash == d.parent_finding_set_hash and d.reason == "finding_set_equal"


@pytest.mark.parametrize("record", [_record("REWORK"), _record("REWORK", reason="cannot_prove"), _record("bogus"),
                                    _record(reason="anchor_unverifiable"), None, "NO_CODE_CHANGE",
                                    _record(security=["x"])])
def test_anything_but_a_trivial_change_is_not_unchanged(record):
    assert _decide(record=record).unchanged is False


@pytest.mark.parametrize("kind", ["NO_CODE_CHANGE", "TRIVIAL_REBASE"])
def test_a_security_path_is_never_unchanged(kind):
    assert _decide(record=_record(kind, security={"scripts/lib/x.py": "auth"})).reason == "security_path"


def test_changed_status_or_unreadable_sets_are_not_unchanged():
    assert _decide(parent_findings=[("f1", "fixed"), ("f2", "fixed")]).reason == "finding_set_changed"
    for field, bad in itertools.product(("findings", "parent_findings"), (None, [("f1", "bogus")])):
        assert _decide(**{field: bad}).reason == "finding_set_unreadable"


def test_missing_or_foreign_parent_is_not_unchanged():
    assert _decide(parent=None).reason == "no_parent_round"
    for parent in (_round("c" * 40, parent=""), _round(HEAD_P, parent="", seat="kimi_gate")):
        assert _decide(parent=parent).reason == "parent_not_of_this_round"
    assert _decide(rnd=_round(HEAD_N, changekind="REWORK")).reason == "changekind_disagrees_with_round"


def _booked(tmp_path: Path, **over: Any) -> Any:
    return rps.book_decision(tmp_path, *_args(**over))


def test_booking_lands_in_shadow_only_and_leaves_rounds_byte_equal(tmp_path):
    rounds = tmp_path / "review_gates" / "rounds.ndjson"
    rounds.parent.mkdir(parents=True)
    rounds.write_text(json.dumps(_round(HEAD_P, parent="").to_dict()) + "\n")
    before = rounds.read_bytes()
    first, second = _booked(tmp_path), _booked(tmp_path, record=_record("REWORK"))
    assert rounds.read_bytes() == before
    path = rps.shadow_path(tmp_path)
    assert path == tmp_path / "review_gates" / "shadow" / "prune.ndjson"
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["unchanged"] for r in rows] == [True, False] == [first.unchanged, second.unchanged]
    row = rows[0]
    assert (row["pr_number"], row["seat"], row["head_sha"], row["parent_head_sha"], row["round_ref"]) == (
        PR, "codex_gate", HEAD_N, HEAD_P, "d-2") and row["finding_set_hash"] == row["parent_finding_set_hash"]
    assert "kind" not in row and rps.read_decisions(tmp_path) == rows and rps.read_decisions(tmp_path / "x") == []
    with open(path, "a") as fh:
        fh.write("not json\n[1]\n")
    assert rps.read_decisions(tmp_path) == rows


def _line(seq: int = 2, **over: Any) -> dict:
    return {"pr_number": PR, "seat": "codex_gate", "head_sha": HEAD_N, "request_seq": seq, "unchanged": True, **over}


def test_would_skip_only_the_next_request():
    lines = [_line(2)]
    assert rps.would_skip(lines, PR, "codex_gate", HEAD_N, 3) is True
    for pr, seat, head, seq in ((PR, "codex_gate", HEAD_N, 4), (PR, "codex_gate", HEAD_N, 2),  # the one after it
                                (PR, "kimi_gate", HEAD_N, 3), (PR + 1, "codex_gate", HEAD_N, 3),
                                (PR, "codex_gate", HEAD_P, 3)):  # another seat, PR, head
        assert rps.would_skip(lines, pr, seat, head, seq) is False


@pytest.mark.parametrize("line", [_line(unchanged=False), _line(unchanged="true"), _line(unchanged=None),
                                  _line(seq=True), _line(seq="2"), "junk", None])
def test_would_skip_needs_positive_evidence(line):
    assert rps.would_skip([line], PR, "codex_gate", HEAD_N, 3) is False


def test_booked_decisions_drive_would_skip(tmp_path):
    _booked(tmp_path, record=_record("REWORK"))
    assert rps.would_skip(rps.read_decisions(tmp_path), PR, "codex_gate", HEAD_N, 3) is False
    _booked(tmp_path, request_seq=5)
    assert rps.would_skip(rps.read_decisions(tmp_path), PR, "codex_gate", HEAD_N, 6) is True


def test_readers_and_the_door_never_open_the_shadow_file(tmp_path, monkeypatch):
    """C4: a corrupt shadow file beside a valid ledger changes nothing any reader returns, and is never opened."""
    path = review_rounds.ledger_path(tmp_path / "review_gates")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_round(HEAD_P, parent="").to_dict()) + "\n")
    clean = review_rounds.read_ledger(path)
    shadow = rps.shadow_path(tmp_path)
    shadow.parent.mkdir(parents=True)
    shadow.write_bytes(b"\xff\xfe{ corrupt\n{\"kind\": \"nope\"}\n")
    opened: list = []
    for name in ("open", "read_text", "read_bytes"):
        monkeypatch.setattr(Path, name, (lambda real: lambda self, *a, **k: (opened.append(self), real(self, *a, **k))[1])(
            getattr(Path, name)))
    ledger = review_rounds.read_ledger(path)
    assert (ledger.lines, review_rounds.codex_rounds(ledger.lines), ledger.bad) == (
        clean.lines, review_rounds.codex_rounds(clean.lines), clean.bad)
    assert shadow not in opened
    scope = rse.SeatScope(pr_id=str(PR), results_dir=tmp_path / "review_gates" / "results", project_id="p")
    assert rse.door_rounds(scope) is not None and shadow not in opened
