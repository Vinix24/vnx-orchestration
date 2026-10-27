"""RG-1 (track review-rondegrens-met-tiebreaker): the codex FAIL-round cap is part of B3.2.

Plan ``claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/review-rondegrens-met-tiebreaker.md``,
decisions 3, 4, 10 and 11; ADR-041 amendment R1e. Three codex FAIL rounds on three heads cap under
B3.2 with ``threshold == "rounds"`` and leave the codex seat unfilled at the merge door.
``VNX_REVIEW_ROUND_CAP`` is a positive integer and a malformed value raises instead of falling back
to 3; a 5 row moves the threshold and the override beats it; an ``opened`` line resets the count;
only ``verdict == "fail"`` rounds count; the blocker thresholds still report ``threshold ==
"blockers"``; every FAIL round in the acceptance fixtures carries a blocker (decision 4).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import BRANCH, PR, isolate_config_registry

PID = "proj-rg1"
HEADS = tuple(c * 40 for c in "abcde")


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


def _line(head: str, verdict: str, blockers: int, *, at: int, project_id: str = "") -> Any:
    import review_rounds

    return review_rounds.RoundLine(
        project_id=project_id, pr_number=PR, head_sha=head, seat=review_rounds.CODEX_SEAT,
        reviewer="codex_gate", takeover_from="", verdict=verdict, blocking_count=blockers,
        report_path=f"/r/20261027-{at:06d}-HEADLESS-codex-pr-{PR}-x.md", dispatch_id=f"d-{at}",
        recorded_at=f"2026-10-27T00:{at:02d}:00Z", branch=BRANCH, source="gate")


def _fail_rounds(*blockers: int, project_id: str = "") -> List[Any]:
    """One codex FAIL round per head, in order: ``blockers`` is the blocker count per round."""
    return [
        _line(HEADS[index], "fail", count, at=index + 1, project_id=project_id)
        for index, count in enumerate(blockers)
    ]


def _opened(*, basis: str = "new", project_id: str = "", at: str = "2026-10-27T00:00:00Z") -> Dict[str, Any]:
    line: Dict[str, Any] = {
        "kind": "opened", "project_id": project_id, "pr_number": PR, "basis": basis, "evidence": 0,
        "recorded_at": at, "source": "gate" if basis == "new" else "operator",
    }
    return {**line, "actor": "operator", "reason": "carried-over history"} if basis == "attested" else line


def _write_ledger(path: Path, lines: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (line.to_dict() if hasattr(line, "to_dict") else line for line in lines)
    path.write_text("".join(json.dumps(item) + "\n" for item in body), encoding="utf-8")


def _rewire_config() -> None:
    import config_registry
    import config_runtime

    config_runtime._wired_for.clear()
    config_registry.set_db_resolver(None)
    config_registry.set_default_project_id(None)


def _store_with_round_cap(tmp_path: Path, value: str) -> Path:
    import config_store_db

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    conn = sqlite3.connect(state_dir / "runtime_coordination.db")
    try:
        config_store_db.set_config(conn, PID, "VNX_REVIEW_ROUND_CAP", value, actor="test")
    finally:
        conn.close()
    return state_dir


def test_three_codex_fail_rounds_on_three_heads_cap_under_b3_2():
    import review_rounds

    cap = review_rounds.cap_state(_fail_rounds(1, 1, 1), PR)

    assert cap.capped is True
    assert (cap.rule, cap.cap_round, cap.threshold, cap.round_limit) == ("B3.2", 3, "rounds", 3)
    assert cap.head == HEADS[2]
    assert "threshold: rounds" in cap.summary()
    assert (cap.to_record()["threshold"], cap.to_record()["round_cap"]) == ("rounds", 3)
    assert "`rounds`" in review_rounds.cap_refusal_text(cap, where="test")


def test_the_merge_door_leaves_the_codex_seat_unfilled_at_the_round_cap(tmp_path):
    import review_rounds
    import review_seat_evaluation

    state_dir = tmp_path / "state"
    results_dir = state_dir / "review_gates" / "results"
    results_dir.mkdir(parents=True)
    project_id = review_rounds.store_project_id(state_dir / "review_gates")
    _write_ledger(
        review_rounds.ledger_path(state_dir / "review_gates"),
        [_opened(project_id=project_id), *_fail_rounds(1, 1, 1, project_id=project_id)],
    )
    scope = review_seat_evaluation.SeatScope(
        pr_id=str(PR), results_dir=results_dir, head_sha=HEADS[2], branch=BRANCH,
    )
    rounds = review_seat_evaluation.door_rounds(scope)
    fill = review_seat_evaluation.SeatFill(
        seat=review_rounds.CODEX_SEAT, filled=True, reviewer="codex_gate", route="direct",
        message="", spoke=True, record={"gate": "codex_gate", "commit_sha": HEADS[2]},
    )
    counted = review_seat_evaluation.counted_fill(fill, rounds, scope, {})

    assert rounds is not None and rounds.capped and rounds.cap.threshold == "rounds", rounds
    assert counted.filled is False, "the codex seat still counted at the round cap"
    assert "B3" in counted.message and "round cap" in counted.message


def test_the_cap_counts_fail_rounds_not_passes_and_not_blockers():
    """Decision 4: ``verdict == "fail"`` is the round, whatever its blocker count."""
    import review_rounds

    passes_and_a_fail = [
        _line(HEADS[0], "pass", 0, at=1),
        _line(HEADS[1], "pass", 0, at=2),
        _line(HEADS[2], "fail", 1, at=3),
    ]
    assert review_rounds.cap_state(passes_and_a_fail, PR).capped is False

    cap = review_rounds.cap_state(_fail_rounds(0, 0, 0), PR)
    assert (cap.capped, cap.threshold, cap.cap_round) == (True, "rounds", 3)


@pytest.mark.parametrize("bad", ["0", "-1", "x", ""])
def test_a_malformed_round_cap_raises_and_an_unset_one_reads_three(bad, monkeypatch):
    import review_rounds

    assert review_rounds.round_cap() == 3, "unset: the registry default"
    monkeypatch.setenv("VNX_REVIEW_ROUND_CAP", bad)

    with pytest.raises(review_rounds.RoundCapError):
        review_rounds.round_cap()
    with pytest.raises(review_rounds.RoundCapError):
        review_rounds.cap_state(_fail_rounds(1), PR)


def test_a_project_config_row_of_five_moves_the_threshold(tmp_path, monkeypatch):
    import review_rounds

    state_dir = _store_with_round_cap(tmp_path, "5")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    _rewire_config()

    assert review_rounds.round_cap() == 5
    assert review_rounds.cap_state(_fail_rounds(1, 1, 1), PR).capped is False
    cap = review_rounds.cap_state(_fail_rounds(1, 1, 1, 1, 1), PR)
    assert (cap.capped, cap.threshold, cap.cap_round, cap.round_limit) == (True, "rounds", 5, 5)


def test_the_override_beats_the_project_config_row(tmp_path, monkeypatch):
    import review_rounds

    state_dir = _store_with_round_cap(tmp_path, "5")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    _rewire_config()

    monkeypatch.setenv("VNX_OVERRIDE_REVIEW_ROUND_CAP", "6")
    assert review_rounds.round_cap() == 6
    assert review_rounds.cap_state(_fail_rounds(1, 1, 1, 1, 1), PR).capped is False

    monkeypatch.setenv("VNX_OVERRIDE_REVIEW_ROUND_CAP", "3")
    assert review_rounds.round_cap() == 3
    assert review_rounds.cap_state(_fail_rounds(1, 1, 1, 1, 1), PR).cap_round == 3


def test_an_attested_opened_line_resets_the_round_count(tmp_path):
    import review_rounds

    path = tmp_path / "review_gates" / "rounds.ndjson"
    _write_ledger(
        path,
        [*_fail_rounds(1, 1, 1), _opened(basis="attested", at="2026-10-27T01:00:00Z"),
         _line(HEADS[3], "fail", 1, at=9)],
    )
    counted = review_rounds.read_ledger(path).for_pr("", PR)

    assert len(counted) == 1, "the three FAIL rounds before the opened line no longer count"
    assert review_rounds.cap_state(counted, PR).capped is False


def test_every_fail_round_in_the_fixtures_carries_at_least_one_blocker():
    """Decision 4: a zero-blocker FAIL must be designed, not an accident (the probe above is outside)."""
    import review_rounds

    fixtures = {
        "three-fail-rounds": _fail_rounds(1, 1, 1),
        "five-fail-rounds": _fail_rounds(1, 1, 1, 1, 1),
        "b3.1": _fail_rounds(3),
        "b3.2-blockers": _fail_rounds(2, 2, 2),
        "opened-reset": [_line(HEADS[3], "fail", 1, at=9)],
    }

    for name, lines in fixtures.items():
        fails = [line for line in lines if line.verdict == review_rounds.VERDICT_FAIL]
        assert fails, name
        assert all(line.blocking_count >= 1 for line in fails), name


@pytest.mark.parametrize("blockers,rule", [((3,), "B3.1"), ((2, 2, 2), "B3.2")])
def test_the_blocker_thresholds_report_threshold_blockers(blockers, rule):
    import review_rounds

    cap = review_rounds.cap_state(_fail_rounds(*blockers), PR)

    assert (cap.capped, cap.rule, cap.threshold) == (True, rule, "blockers")
    assert "threshold: blockers" in cap.summary()
    assert cap.to_record()["threshold"] == "blockers"
