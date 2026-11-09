"""Proposal-ledger tests for the daily-metrics track (doorloop PR-11b).

The ledger is the retry-safe memory of decision 26: at most three proposals per
project and metric day, one open proposal per key, and fourteen days of silence
after an ``undetermined`` verdict. Every test builds its own ledger under
``tmp_path`` and its own ``Candidate`` objects; nothing reads a clock.
"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
from datetime import date, timedelta
from pathlib import Path

import pytest

from daily_metrics.proposal_rules import Candidate

DAY = date(2026, 11, 8)
RECORDED_AT = "2026-11-09T04:12:00Z"
RULE_VERSION = "0123456789ab"


def _ledger():
    """The module under test, imported lazily so the red run fails on its first assert."""
    from daily_metrics import proposal_ledger

    return proposal_ledger


def _candidate(key, exceedance=1.0):
    return Candidate(key=key, metric_id="merges", seat=None, value=10.0,
                     baseline=5.0, baseline_n=120, day_n=9, exceedance=exceedance)


CANDIDATES = [_candidate(f"merges:{name}") for name in ("a", "b", "c", "d", "e")]


def _append(path, candidates, *, day=DAY, project_id="p"):
    return _ledger().append_proposals(
        path, project_id=project_id, metric_day=day, candidates=candidates,
        rule_version=RULE_VERSION, recorded_at=RECORDED_AT)


def _append_event(path, event):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _append_verdict(path, *, proposal_id, key, day, verdict):
    _append_event(path, {
        "event": "verdict", "proposal_id": proposal_id, "project_id": "p",
        "key": key, "metric_day": day.isoformat(), "verdict": verdict,
        "recorded_at": RECORDED_AT,
    })


def test_the_proposal_ledger_module_exists():
    assert importlib.util.find_spec("daily_metrics.proposal_ledger") is not None
    ledger = _ledger()
    assert ledger.LEDGER_NAME == "proposals.ndjson"
    assert ledger.ledger_path(Path("/tmp/store")) == Path("/tmp/store/proposals.ndjson")


def test_one_day_writes_at_most_three_ids_in_candidate_order(tmp_path):
    ledger = _ledger()
    path = ledger.ledger_path(tmp_path)
    assert ledger.read_events(path) == []

    written = _append(path, CANDIDATES)

    expected = [ledger.proposal_id("p", candidate.key, DAY, RULE_VERSION)
                for candidate in CANDIDATES[:3]]
    assert written == expected
    events = ledger.read_events(path)
    assert [event["event"] for event in events] == ["proposed", "proposed", "proposed"]
    assert [event["key"] for event in events] == [candidate.key for candidate in CANDIDATES[:3]]
    assert events[0]["metric_day"] == DAY.isoformat()
    assert events[0]["recorded_at"] == RECORDED_AT


def test_a_second_run_on_the_same_day_writes_nothing(tmp_path):
    ledger = _ledger()
    path = ledger.ledger_path(tmp_path)
    assert len(_append(path, CANDIDATES)) == 3
    before = path.read_bytes()

    assert _append(path, CANDIDATES) == []

    assert path.read_bytes() == before
    proposed = [event for event in ledger.read_events(path) if event["event"] == "proposed"]
    assert len(proposed) == 3


def test_a_finished_partial_run_keeps_the_first_lines_byte_identical(tmp_path):
    ledger = _ledger()
    path = ledger.ledger_path(tmp_path)
    first = _append(path, CANDIDATES[:2])
    assert len(first) == 2
    before_lines = path.read_text(encoding="utf-8").splitlines()

    written = _append(path, CANDIDATES)

    assert written == [ledger.proposal_id("p", CANDIDATES[2].key, DAY, RULE_VERSION)]
    after_lines = path.read_text(encoding="utf-8").splitlines()
    assert after_lines[:2] == before_lines
    assert len(ledger.read_events(path)) == 3


def test_an_open_proposal_from_an_earlier_day_blocks_the_key_until_a_verdict(tmp_path):
    ledger = _ledger()
    path = ledger.ledger_path(tmp_path)
    earlier = DAY - timedelta(days=1)
    target = CANDIDATES[0]

    early_ids = _append(path, [target], day=earlier)
    assert len(early_ids) == 1
    assert target.key in ledger.open_keys(ledger.read_events(path))
    assert _append(path, [target]) == []

    _append_verdict(path, proposal_id=early_ids[0], key=target.key, day=earlier,
                    verdict="helped")
    assert _append(path, [target]) == [ledger.proposal_id("p", target.key, DAY, RULE_VERSION)]


def test_an_undetermined_verdict_blocks_fourteen_days(tmp_path):
    ledger = _ledger()
    target = CANDIDATES[0]

    recent = ledger.ledger_path(tmp_path / "recent")
    _append_verdict(recent, proposal_id="0" * 12, key=target.key,
                    day=DAY - timedelta(days=10), verdict="undetermined")
    assert target.key in ledger.blocked_keys(ledger.read_events(recent), DAY)
    assert _append(recent, [target]) == []

    stale = ledger.ledger_path(tmp_path / "stale")
    _append_verdict(stale, proposal_id="1" * 12, key=target.key,
                    day=DAY - timedelta(days=15), verdict="undetermined")
    assert target.key not in ledger.blocked_keys(ledger.read_events(stale), DAY)
    assert len(_append(stale, [target])) == 1


def test_proposal_id_is_deterministic_and_covers_all_four_inputs():
    ledger = _ledger()
    base = ledger.proposal_id("p", "merges:a", DAY, RULE_VERSION)
    assert base == ledger.proposal_id("p", "merges:a", DAY, RULE_VERSION)
    assert len(base) == 12
    assert all(character in "0123456789abcdef" for character in base)

    variants = [
        ledger.proposal_id("other", "merges:a", DAY, RULE_VERSION),
        ledger.proposal_id("p", "merges:b", DAY, RULE_VERSION),
        ledger.proposal_id("p", "merges:a", DAY + timedelta(days=1), RULE_VERSION),
        ledger.proposal_id("p", "merges:a", DAY, "ffffffffffff"),
    ]
    assert len(set(variants + [base])) == 5


def test_a_damaged_ledger_blocks_proposing_and_stays_unchanged(tmp_path):
    ledger = _ledger()
    path = ledger.ledger_path(tmp_path)
    assert len(_append(path, CANDIDATES[:1])) == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    before = path.read_bytes()

    with pytest.raises(ledger.LedgerUnreadable) as excinfo:
        _append(path, CANDIDATES)

    assert "line 2" in str(excinfo.value)
    assert path.read_bytes() == before


def _append_in_child(path, barrier, candidates):
    from daily_metrics.proposal_ledger import append_proposals

    barrier.wait(timeout=30)
    append_proposals(Path(path), project_id="p", metric_day=DAY, candidates=candidates,
                     rule_version=RULE_VERSION, recorded_at=RECORDED_AT)


def test_two_processes_never_exceed_three_events(tmp_path):
    ledger = _ledger()
    path = ledger.ledger_path(tmp_path)
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    processes = [context.Process(target=_append_in_child, args=(str(path), barrier, CANDIDATES))
                 for _ in range(2)]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)

    assert [process.exitcode for process in processes] == [0, 0]
    events = ledger.read_events(path)
    assert len(events) == 3
    assert sorted(event["key"] for event in events) == [candidate.key for candidate in CANDIDATES[:3]]
