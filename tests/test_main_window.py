#!/usr/bin/env python3
"""Tests for scripts/lib/main_window.py: the pure core."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import main_window as mw  # noqa: E402

BASE, M1, M2, M3 = "b" * 40, "1" * 40, "2" * 40, "3" * 40


def _run(sha, *, run_id, status="completed", conclusion="success", created="2026-11-13T08:00:00Z"):
    return {"databaseId": run_id, "headSha": sha, "status": status, "conclusion": conclusion,
            "createdAt": created, "attempt": 1}


def _receipt(sha, pr, dispatch):
    return {"event_type": "pr_merged", "merge_sha": sha, "pr_number": pr, "dispatch_id": dispatch}


COMMITS = [M3, M2, M1, BASE]  # newest first
RECEIPTS = [_receipt(M1, 101, "d-one"), _receipt(M2, 102, "d-two"), _receipt(M3, 103, "d-three")]


def _by_sha(window):
    return {e.sha: e for e in window.entries}


# ── pure core ────────────────────────────────────────────────────────────────


def test_break_in_the_middle_of_three_merges_is_red_with_its_own_pr():
    runs = [
        _run(BASE, run_id=10),
        _run(M1, run_id=11, status="in_progress", conclusion=None),
        _run(M2, run_id=12, conclusion="failure"),
        _run(M3, run_id=13, status="in_progress", conclusion=None),
    ]
    window = mw.compute_window(COMMITS, runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_RED
    assert [e.sha for e in window.entries] == [M1, M2, M3]  # oldest first
    assert window.first_red.sha == M2
    assert (window.first_red.run_id, window.first_red.pr_number, window.first_red.dispatch_id) == (12, 102, "d-two")
    assert window.base_sha == BASE


def test_first_red_is_the_oldest_red_entry():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, conclusion="failure"),
            _run(M2, run_id=12, conclusion="failure"),
            _run(M3, run_id=13, status="queued", conclusion=None)]
    assert mw.compute_window(COMMITS, runs, RECEIPTS).first_red.sha == M1


def test_red_followed_by_newer_green_is_outside_the_window():
    runs = [_run(BASE, run_id=10, conclusion="failure"), _run(M1, run_id=11, conclusion="failure"),
            _run(M2, run_id=12)]
    window = mw.compute_window([M2, M1, BASE], runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_GREEN
    assert window.entries == ()
    assert window.base_sha == M2


def test_all_green_newest_commit_is_the_base_and_the_window_is_empty():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11), _run(M2, run_id=12)]
    window = mw.compute_window([M2, M1, BASE], runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_GREEN
    assert window.entries == () and window.base_sha == M2


def test_newest_merge_in_progress_on_green_base_is_running():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, status="in_progress", conclusion=None)]
    window = mw.compute_window([M1, BASE], runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_RUNNING
    assert window.entries[0].state == mw.RUN_RUNNING


def test_merge_commit_without_any_run_is_running():
    window = mw.compute_window([M1, BASE], [_run(BASE, run_id=10)], RECEIPTS)
    assert window.verdict == mw.VERDICT_RUNNING
    assert window.entries[0].run_id is None


def test_red_wins_over_a_running_entry():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, conclusion="cancelled"),
            _run(M2, run_id=12, status="queued", conclusion=None)]
    assert mw.compute_window([M2, M1, BASE], runs, RECEIPTS).verdict == mw.VERDICT_RED


def test_each_merge_maps_to_its_own_receipt():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, status="queued", conclusion=None),
            _run(M2, run_id=12, status="queued", conclusion=None), _run(M3, run_id=13, status="queued", conclusion=None)]
    entries = _by_sha(mw.compute_window(COMMITS, runs, RECEIPTS))
    assert [(entries[s].pr_number, entries[s].dispatch_id) for s in (M1, M2, M3)] == [
        (101, "d-one"), (102, "d-two"), (103, "d-three")]
    assert {e.mapping for e in entries.values()} == {mw.MAPPING_RECEIPT}


def test_merge_without_receipt_does_not_borrow_from_a_neighbour():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, status="queued", conclusion=None),
            _run(M2, run_id=12, status="queued", conclusion=None), _run(M3, run_id=13, status="queued", conclusion=None)]
    receipts = [_receipt(M1, 101, "d-one"), _receipt(M3, 103, "d-three")]
    entries = _by_sha(mw.compute_window(COMMITS, runs, receipts))
    assert entries[M2].pr_number is None
    assert entries[M2].dispatch_id is None
    assert entries[M2].mapping == mw.MAPPING_NO_RECEIPT
    assert entries[M1].pr_number == 101 and entries[M3].pr_number == 103


def test_no_green_run_among_the_commits_is_history_incomplete():
    runs = [_run(M1, run_id=11, conclusion="failure"), _run(M2, run_id=12, status="in_progress", conclusion=None)]
    window = mw.compute_window([M2, M1], runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_UNMEASURABLE
    assert window.reason == "history_incomplete"


def test_two_runs_for_one_sha_the_newest_counts():
    runs = [_run(BASE, run_id=10),
            _run(M1, run_id=11, conclusion="failure", created="2026-11-13T08:00:00Z"),
            _run(M1, run_id=14, conclusion="success", created="2026-11-13T09:00:00Z")]
    window = mw.compute_window([M1, BASE], runs, RECEIPTS)
    assert window.base_sha == M1
    assert window.verdict == mw.VERDICT_GREEN


def test_two_runs_for_one_sha_newest_red_beats_older_green():
    runs = [_run(BASE, run_id=10),
            _run(M1, run_id=11, conclusion="success", created="2026-11-13T08:00:00Z"),
            _run(M1, run_id=14, conclusion="failure", created="2026-11-13T09:00:00Z")]
    window = mw.compute_window([M1, BASE], runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_RED
    assert window.first_red.run_id == 14


@pytest.mark.parametrize("run", [
    {"databaseId": 5, "headSha": M1, "status": "completed", "conclusion": None},
    {"databaseId": 5, "headSha": M1, "status": "completed", "conclusion": ""},
    {"databaseId": 5, "headSha": M1, "status": None, "conclusion": "success"},
    {"databaseId": 5, "headSha": M1},
])
def test_unusable_run_in_the_window_is_unmeasurable_never_green(run):
    window = mw.compute_window([M1, BASE], [_run(BASE, run_id=10), run], RECEIPTS)
    assert window.verdict == mw.VERDICT_UNMEASURABLE
    assert window.reason == "unusable_run"


@pytest.mark.parametrize("run", [
    {"databaseId": 5, "headSha": BASE, "status": "completed", "conclusion": None},
    {"databaseId": 5, "headSha": BASE, "status": None, "conclusion": "success"},
    {"databaseId": 5, "headSha": BASE, "status": "in_progress", "conclusion": "success"},
])
def test_unusable_or_unfinished_run_is_never_a_base(run):
    window = mw.compute_window([BASE], [run], RECEIPTS)
    assert window.verdict == mw.VERDICT_UNMEASURABLE
    assert window.reason == "history_incomplete"


@pytest.mark.parametrize("commits,runs,merges", [
    ([], [], []),
    (None, [], []),
    ([M1, ""], [], []),
    ([M1, 7], [], []),
    ([M1], None, []),
    ([M1], ["x"], []),
    ([M1], [], None),
    ([M1], [], ["x"]),
])
def test_unusable_input_is_unmeasurable(commits, runs, merges):
    window = mw.compute_window(commits, runs, merges)
    assert window.verdict == mw.VERDICT_UNMEASURABLE
    assert window.reason == "invalid_input"


def test_to_dict_is_deterministic_and_sorted():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, conclusion="failure")]
    window = mw.compute_window([M1, BASE], runs, RECEIPTS)
    first = json.dumps(window.to_dict(), sort_keys=False)
    assert first == json.dumps(mw.compute_window([M1, BASE], runs, RECEIPTS).to_dict(), sort_keys=False)
    assert list(window.to_dict()) == sorted(window.to_dict())
    assert window.to_dict()["first_red"]["sha"] == M1
    assert "vermoedelijk" in window.to_dict()["first_red_note"]
