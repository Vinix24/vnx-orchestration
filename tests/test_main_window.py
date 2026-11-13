#!/usr/bin/env python3
"""Tests for scripts/lib/main_window.py: the pure core and the gh adapter.

The adapter runs with a fake runner: no real gh, no real git, no network.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import main_window as mw  # noqa: E402

BASE, M1, M2, M3 = "b" * 40, "1" * 40, "2" * 40, "3" * 40


def _run(sha, *, run_id, status="completed", conclusion="success", created="2026-11-13T08:00:00Z"):
    return {"databaseId": run_id, "headSha": sha, "status": status, "conclusion": conclusion,
            "createdAt": created, "attempt": 1}


def _receipt(sha, pr, dispatch):
    return {"event_type": "pr_merged", "merge_sha": sha, "pr_number": pr, "dispatch_id": dispatch}


NOW = datetime(2026, 11, 13, 8, 10, tzinfo=timezone.utc)
FRESH = "2026-11-13T08:05:00Z"  # younger than the 40 minute age limit at NOW


def _heavy(_sha):
    return mw.PROFILE_HEAVY


def _cw(commits, runs, merges, *, profile_of=_heavy, now=NOW, **kwargs):
    return mw.compute_window(commits, runs, merges, profile_of=profile_of, now=now, **kwargs)


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
    window = _cw(COMMITS, runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_RED
    assert [e.sha for e in window.entries] == [M1, M2, M3]  # oldest first
    assert window.first_red.sha == M2
    assert (window.first_red.run_id, window.first_red.pr_number, window.first_red.dispatch_id) == (12, 102, "d-two")
    assert window.base_sha == BASE


def test_first_red_is_the_oldest_red_entry():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, conclusion="failure"),
            _run(M2, run_id=12, conclusion="failure"),
            _run(M3, run_id=13, status="queued", conclusion=None)]
    assert _cw(COMMITS, runs, RECEIPTS).first_red.sha == M1


def test_red_followed_by_newer_green_is_outside_the_window():
    runs = [_run(BASE, run_id=10, conclusion="failure"), _run(M1, run_id=11, conclusion="failure"),
            _run(M2, run_id=12)]
    window = _cw([M2, M1, BASE], runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_GREEN
    assert window.entries == ()
    assert window.base_sha == M2


def test_all_green_newest_commit_is_the_base_and_the_window_is_empty():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11), _run(M2, run_id=12)]
    window = _cw([M2, M1, BASE], runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_GREEN
    assert window.entries == () and window.base_sha == M2


def test_newest_merge_in_progress_on_green_base_is_running():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, status="in_progress", conclusion=None)]
    window = _cw([M1, BASE], runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_RUNNING
    assert window.entries[0].state == mw.RUN_RUNNING


def test_merge_commit_without_any_run_is_running_while_young():
    window = _cw([M1, BASE], [_run(BASE, run_id=10)], RECEIPTS, commit_times={M1: FRESH})
    assert window.verdict == mw.VERDICT_RUNNING
    assert window.entries[0].run_id is None


def test_red_wins_over_a_running_entry():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, conclusion="failure"),
            _run(M2, run_id=12, status="queued", conclusion=None)]
    assert _cw([M2, M1, BASE], runs, RECEIPTS).verdict == mw.VERDICT_RED


def test_each_merge_maps_to_its_own_receipt():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, status="queued", conclusion=None),
            _run(M2, run_id=12, status="queued", conclusion=None), _run(M3, run_id=13, status="queued", conclusion=None)]
    entries = _by_sha(_cw(COMMITS, runs, RECEIPTS))
    assert [(entries[s].pr_number, entries[s].dispatch_id) for s in (M1, M2, M3)] == [
        (101, "d-one"), (102, "d-two"), (103, "d-three")]
    assert {e.mapping for e in entries.values()} == {mw.MAPPING_RECEIPT}


def test_merge_without_receipt_does_not_borrow_from_a_neighbour():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, status="queued", conclusion=None),
            _run(M2, run_id=12, status="queued", conclusion=None), _run(M3, run_id=13, status="queued", conclusion=None)]
    receipts = [_receipt(M1, 101, "d-one"), _receipt(M3, 103, "d-three")]
    entries = _by_sha(_cw(COMMITS, runs, receipts))
    assert entries[M2].pr_number is None
    assert entries[M2].dispatch_id is None
    assert entries[M2].mapping == mw.MAPPING_NO_RECEIPT
    assert entries[M1].pr_number == 101 and entries[M3].pr_number == 103


def test_no_green_run_among_the_commits_is_history_incomplete():
    runs = [_run(M1, run_id=11, conclusion="failure"), _run(M2, run_id=12, status="in_progress", conclusion=None)]
    window = _cw([M2, M1], runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_UNMEASURABLE
    assert window.reason == "history_incomplete"


def test_two_runs_for_one_sha_the_newest_counts():
    runs = [_run(BASE, run_id=10),
            _run(M1, run_id=11, conclusion="failure", created="2026-11-13T08:00:00Z"),
            _run(M1, run_id=14, conclusion="success", created="2026-11-13T09:00:00Z")]
    window = _cw([M1, BASE], runs, RECEIPTS)
    assert window.base_sha == M1
    assert window.verdict == mw.VERDICT_GREEN


def test_two_runs_for_one_sha_newest_red_beats_older_green():
    runs = [_run(BASE, run_id=10),
            _run(M1, run_id=11, conclusion="success", created="2026-11-13T08:00:00Z"),
            _run(M1, run_id=14, conclusion="failure", created="2026-11-13T09:00:00Z")]
    window = _cw([M1, BASE], runs, RECEIPTS)
    assert window.verdict == mw.VERDICT_RED
    assert window.first_red.run_id == 14


@pytest.mark.parametrize("run", [
    {"databaseId": 5, "headSha": M1, "status": "completed", "conclusion": None},
    {"databaseId": 5, "headSha": M1, "status": "completed", "conclusion": ""},
    {"databaseId": 5, "headSha": M1, "status": None, "conclusion": "success"},
    {"databaseId": 5, "headSha": M1},
])
def test_unusable_run_in_the_window_is_a_named_nogo_never_green(run):
    window = _cw([M1, BASE], [_run(BASE, run_id=10), run], RECEIPTS)
    assert window.verdict == mw.VERDICT_NOGO
    assert window.entries[0].state == mw.RUN_NOGO
    assert window.entries[0].reason in {"no_conclusion", "unknown_status"}


@pytest.mark.parametrize("run", [
    {"databaseId": 5, "headSha": BASE, "status": "completed", "conclusion": None},
    {"databaseId": 5, "headSha": BASE, "status": None, "conclusion": "success"},
    {"databaseId": 5, "headSha": BASE, "status": "in_progress", "conclusion": "success"},
])
def test_unusable_or_unfinished_run_is_never_a_base(run):
    window = _cw([BASE], [run], RECEIPTS)
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
    window = _cw(commits, runs, merges)
    assert window.verdict == mw.VERDICT_UNMEASURABLE
    assert window.reason == "invalid_input"


def test_to_dict_is_deterministic_and_sorted():
    runs = [_run(BASE, run_id=10), _run(M1, run_id=11, conclusion="failure")]
    window = _cw([M1, BASE], runs, RECEIPTS)
    first = json.dumps(window.to_dict(), sort_keys=False)
    assert first == json.dumps(_cw([M1, BASE], runs, RECEIPTS).to_dict(), sort_keys=False)
    assert list(window.to_dict()) == sorted(window.to_dict())
    assert window.to_dict()["first_red"]["sha"] == M1
    assert "vermoedelijk" in window.to_dict()["first_red_note"]


# ── gh adapter, fake runner ──────────────────────────────────────────────────


def _proc(stdout="", returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class FakeRunner:
    """Answers the commits call and the runs call; records every argv."""

    def __init__(self, commits=None, runs=None):
        self.commits = commits if commits is not None else _proc(
            "".join(f"{sha}\t{FRESH}\n" for sha in COMMITS))
        self.files = _proc("scripts/x.py\n")
        self.runs = runs if runs is not None else _proc(json.dumps([
            _run(BASE, run_id=10), _run(M1, run_id=11, status="queued", conclusion=None),
            _run(M2, run_id=12, conclusion="failure"),
            _run(M3, run_id=13, status="in_progress", conclusion=None)]))
        self.calls = []

    def __call__(self, argv, *, timeout, cwd=None):
        self.calls.append((list(argv), timeout, cwd))
        if argv[1] == "api" and "/commits/" in argv[2]:
            return self.files
        reply = self.commits if argv[1] == "api" else self.runs
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _write_receipts(state_dir, records, junk=()):
    state_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records] + list(junk)
    (state_dir / "t0_receipts.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_adapter_reads_commits_runs_and_receipts(tmp_path):
    _write_receipts(tmp_path / "state", RECEIPTS + [{"event_type": "task_complete", "merge_sha": M3}],
                    junk=["not json"])
    runner = FakeRunner()
    window = mw.read_window(tmp_path, state_dir=tmp_path / "state", runner=runner, limit=30, now=NOW)
    assert window.verdict == mw.VERDICT_RED
    assert (window.first_red.pr_number, window.first_red.dispatch_id) == (102, "d-two")
    commits_argv, commits_timeout, commits_cwd = runner.calls[0]
    assert commits_argv[:3] == ["gh", "api", "repos/{owner}/{repo}/commits?sha=main&per_page=30"]
    assert commits_argv[-1] == ".[] | [.sha, .commit.committer.date] | @tsv"
    assert commits_cwd == str(tmp_path) and commits_timeout == 15
    runs_argv = next(c[0] for c in runner.calls if c[0][1] == "run")
    assert runs_argv[runs_argv.index("--workflow") + 1] == "VNX CI"
    assert runs_argv[runs_argv.index("--event") + 1] == "push"
    assert runs_argv[runs_argv.index("--branch") + 1] == "main"
    assert runs_argv[runs_argv.index("--limit") + 1] == "50"


def test_adapter_workflow_name_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(mw.CI_WORKFLOW_NAME_ENV_VAR, "Other CI")
    runner = FakeRunner()
    mw.read_window(tmp_path, runner=runner, now=NOW)
    runs_argv = next(c[0] for c in runner.calls if c[0][1] == "run")
    assert runs_argv[runs_argv.index("--workflow") + 1] == "Other CI"


def test_missing_or_unreadable_receipt_file_gives_no_receipt_not_unmeasurable(tmp_path):
    for state_dir in (None, tmp_path / "absent"):
        window = mw.read_window(tmp_path, state_dir=state_dir, runner=FakeRunner(), now=NOW)
        assert window.verdict == mw.VERDICT_RED
        assert window.first_red.mapping == mw.MAPPING_NO_RECEIPT
        assert window.first_red.pr_number is None


def test_only_the_last_5000_receipt_lines_are_read(tmp_path):
    filler = [{"event_type": "task_complete"}] * mw.RECEIPT_TAIL_LINES
    _write_receipts(tmp_path / "state", [_receipt(M2, 102, "d-two")] + filler)
    window = mw.read_window(tmp_path, state_dir=tmp_path / "state", runner=FakeRunner(), now=NOW)
    assert window.first_red.mapping == mw.MAPPING_NO_RECEIPT


_FAILURES = {
    "missing": (FileNotFoundError("gh"), "gh_missing"),
    "timeout": (subprocess.TimeoutExpired(cmd="gh", timeout=15), "gh_timeout"),
    "nonzero": (_proc("", returncode=1), "gh_failed"),
}


@pytest.mark.parametrize("name", sorted(_FAILURES))
def test_commits_call_failure_is_unmeasurable_with_reason(tmp_path, name):
    reply, reason = _FAILURES[name]
    window = mw.read_window(tmp_path, runner=FakeRunner(commits=reply), now=NOW)
    assert window.verdict == mw.VERDICT_UNMEASURABLE
    assert window.reason == f"commits_{reason}"


@pytest.mark.parametrize("name", sorted(_FAILURES))
def test_runs_call_failure_is_unmeasurable_with_reason(tmp_path, name):
    reply, reason = _FAILURES[name]
    window = mw.read_window(tmp_path, runner=FakeRunner(runs=reply), now=NOW)
    assert window.verdict == mw.VERDICT_UNMEASURABLE
    assert window.reason == f"runs_{reason}"


def test_bad_json_on_the_runs_call_is_unmeasurable(tmp_path):
    for body in ("not json", "{}", "null"):
        window = mw.read_window(tmp_path, runner=FakeRunner(runs=_proc(body)), now=NOW)
        assert (window.verdict, window.reason) == (mw.VERDICT_UNMEASURABLE, "runs_bad_json")


def test_empty_commits_output_is_unmeasurable(tmp_path):
    window = mw.read_window(tmp_path, runner=FakeRunner(commits=_proc("\n")), now=NOW)
    assert (window.verdict, window.reason) == (mw.VERDICT_UNMEASURABLE, "commits_empty")


def test_empty_run_list_is_history_incomplete_never_green(tmp_path):
    window = mw.read_window(tmp_path, runner=FakeRunner(runs=_proc("[]")), now=NOW)
    assert (window.verdict, window.reason) == (mw.VERDICT_UNMEASURABLE, "history_incomplete")


# ── receipt-only merges (the GitHub read-after-write gap, ci-cost 3b part d) ──

M9 = "9" * 40
BASE_TIME = "2026-11-13T08:00:00Z"
_RUNS_TWO = [_run(BASE, run_id=10), _run(M1, run_id=11)]
_TIMES = {BASE: BASE_TIME, M1: "2026-11-13T08:02:00Z"}


def _m1_light(sha):
    """M1 is a green light-profile entry, not the base: BASE stays the heavy base."""
    return mw.PROFILE_LIGHT if sha == M1 else mw.PROFILE_HEAVY


def _cwo(merges, **kwargs):
    return _cw([M1, BASE], _RUNS_TWO, merges, profile_of=_m1_light, **kwargs)


def _late_receipt(sha=M9, **extra):
    rec = _receipt(sha, 109, "d-nine")
    rec.update({"timestamp": FRESH, **extra})
    return rec


def test_a_young_receipt_only_merge_is_running_and_blocks_the_next_admission():
    window = _cwo([_late_receipt()], commit_times=_TIMES)
    assert window.verdict == mw.VERDICT_RUNNING
    entry = _by_sha(window)[M9]
    assert (entry.state, entry.run_id, entry.status, entry.conclusion) == (mw.RUN_RUNNING, None, None, None)
    assert (entry.pr_number, entry.dispatch_id, entry.mapping) == (109, "d-nine", mw.MAPPING_RECEIPT)
    assert [e.sha for e in window.entries] == [M1, M9]  # receipt-only merges come last
    admission = mw.admit(window, max_unverified=1)
    assert admission.verdict == mw.NO_GO and admission.reason.startswith("unverified_limit")


def test_a_receipt_older_than_the_base_commit_is_ignored():
    rec = _late_receipt(timestamp="2026-11-13T07:59:59Z")
    assert _cwo([rec], commit_times=_TIMES).verdict == mw.VERDICT_GREEN
    equal = _late_receipt(timestamp=BASE_TIME)
    assert _cwo([equal], commit_times=_TIMES).verdict == mw.VERDICT_GREEN


def test_a_receipt_for_a_sha_in_the_commit_list_is_one_entry():
    window = _cwo([_late_receipt(sha=M1)], commit_times=_TIMES)
    assert [e.sha for e in window.entries] == [M1]
    assert window.verdict == mw.VERDICT_GREEN
    below_base = _cwo([_late_receipt(sha=BASE)], commit_times=_TIMES)
    assert [e.sha for e in below_base.entries] == [M1]


@pytest.mark.parametrize("stamp", [None, "", "yesterday", 42])
def test_a_receipt_only_merge_with_an_unreadable_time_counts(stamp):
    rec = _late_receipt()
    rec["timestamp"] = stamp
    window = _cwo([rec], commit_times=_TIMES)
    assert window.verdict == mw.VERDICT_RUNNING
    entry = _by_sha(window)[M9]
    assert (entry.state, entry.reason) == (mw.RUN_RUNNING, mw.RUNNING_REASON_RECEIPT_TIME_UNREADABLE)
    missing = _late_receipt()
    del missing["timestamp"]
    assert _by_sha(_cwo([missing], commit_times=_TIMES))[M9].reason == \
        mw.RUNNING_REASON_RECEIPT_TIME_UNREADABLE


@pytest.mark.parametrize("times", [None, {}, {M1: "x"}, {BASE: "not a date"}, {BASE: None}])
def test_an_unreadable_base_time_with_a_receipt_only_merge_is_unmeasurable(times):
    window = _cwo([_late_receipt()], commit_times=times)
    assert window.verdict == mw.VERDICT_UNMEASURABLE
    assert window.reason == mw.UNMEASURABLE_REASON_BASE_TIME_UNKNOWN
    assert mw.admit(window, max_unverified=1).reason == "base_time_unknown"


def test_an_unreadable_base_time_without_a_receipt_only_merge_changes_nothing():
    assert _cwo([_late_receipt(sha=M1)], commit_times=None).verdict == mw.VERDICT_GREEN
    assert _cwo([], commit_times={}).verdict == mw.VERDICT_GREEN


def test_a_receipt_only_merge_past_the_age_limit_is_a_named_nogo():
    rec = _late_receipt(timestamp="2026-11-13T07:20:00Z")  # 50 min before NOW, after the base
    times = {BASE: "2026-11-13T07:00:00Z", M1: "2026-11-13T07:01:00Z"}
    window = _cwo([rec], commit_times=times)
    assert window.verdict == mw.VERDICT_NOGO
    assert (_by_sha(window)[M9].state, _by_sha(window)[M9].reason) == (mw.RUN_NOGO, mw.NOGO_REASON_RECEIPT_ONLY_STALE)
    assert mw.admit(window, max_unverified=3).reason == f"receipt_only_stale: {M9}"


def test_receipt_only_merges_are_ordered_by_receipt_time_oldest_first():
    newer = _late_receipt(sha="8" * 40, timestamp="2026-11-13T08:07:00Z")
    older = _late_receipt(sha=M9, timestamp="2026-11-13T08:04:00Z")
    window = _cwo([newer, older], commit_times=_TIMES)
    assert [e.sha for e in window.entries] == [M1, M9, "8" * 40]


def test_adapter_counts_a_merge_github_does_not_list_yet(tmp_path):
    _write_receipts(tmp_path / "state", [_late_receipt()])
    commits = _proc(f"{M1}\t{_TIMES[M1]}\n{BASE}\t{BASE_TIME}\n")
    runs = _proc(json.dumps(_RUNS_TWO))
    window = mw.read_window(tmp_path, state_dir=tmp_path / "state",
                            runner=FakeRunner(commits=commits, runs=runs), now=NOW)
    assert window.verdict == mw.VERDICT_RUNNING
    assert M9 in _by_sha(window)
