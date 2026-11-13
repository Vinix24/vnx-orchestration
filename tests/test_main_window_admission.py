#!/usr/bin/env python3
"""The admission contract of the main window (ADR-073 besluit 3 and 6): the heavy-only
base, one outcome per run status and the gh adapter's
profile lookups. A fake runner stands in for gh: no real gh, no real git, no network."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import main_window as mw  # noqa: E402

BASE, M1, M2, M3 = "b" * 40, "1" * 40, "2" * 40, "3" * 40
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
LIMIT = 40


def _at(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(sha, run_id, *, status="completed", conclusion="success", age=5):
    return {"databaseId": run_id, "headSha": sha, "status": status, "conclusion": conclusion,
            "createdAt": _at(age), "attempt": 1}


def _profiles(**by_sha):
    return lambda sha: by_sha.get(sha, mw.PROFILE_HEAVY)


def _window(commits, runs, *, profile_of=None, commit_times=None):
    return mw.compute_window(commits, runs, [], profile_of=profile_of or _profiles(), now=NOW,
                             max_age_min=LIMIT, commit_times=commit_times)


def _one_entry(run, *, commit_time=None):
    return _window([M1, BASE], [_run(BASE, 1), *([run] if run else [])],
                   commit_times={M1: commit_time} if commit_time else None).entries[0]


# ── the heavy-only base ──────────────────────────────────────────────────────


def test_a_green_light_run_over_a_red_heavy_run_is_not_the_base():
    runs = [_run(BASE, 1), _run(M1, 2, conclusion="failure"), _run(M2, 3)]
    window = _window([M2, M1, BASE], runs, profile_of=_profiles(**{M2: mw.PROFILE_LIGHT}))
    assert window.base_sha == BASE
    assert window.verdict == mw.VERDICT_RED
    assert window.first_red.sha == M1
    assert [(e.sha, e.state) for e in window.entries] == [(M1, mw.RUN_RED), (M2, mw.RUN_GREEN)]


@pytest.mark.parametrize("profile", [mw.PROFILE_UNKNOWN, mw.PROFILE_LIGHT, "", None, "nonsense"])
def test_a_green_run_without_a_heavy_profile_is_never_a_base(profile):
    window = _window([M1], [_run(M1, 1)], profile_of=lambda _sha: profile)
    assert (window.verdict, window.reason) == (mw.VERDICT_UNMEASURABLE, "history_incomplete")


def test_profile_is_asked_only_for_green_candidates():
    asked = []
    runs = [_run(M2, 3, conclusion="failure"), _run(M1, 2, status="in_progress", conclusion=None), _run(BASE, 1)]
    _window([M2, M1, BASE], runs, profile_of=lambda sha: asked.append(sha) or mw.PROFILE_HEAVY)
    assert asked == [BASE]


@pytest.mark.parametrize("paths,expected", [
    (["scripts/a.py"], mw.PROFILE_HEAVY),
    (["docs/a.md", "scripts/a.py"], mw.PROFILE_HEAVY),
    (["docs/a.md", "README.md"], mw.PROFILE_LIGHT),
    ([], mw.PROFILE_UNKNOWN),
    ([f"docs/{i}.md" for i in range(mw.FILES_PAGE_CAP)], mw.PROFILE_UNKNOWN),
    ([f"docs/{i}.md" for i in range(mw.FILES_PAGE_CAP)] + ["src/x.py"], mw.PROFILE_HEAVY),
])
def test_profile_from_files(paths, expected):
    assert mw.profile_from_files(paths) == expected


# ── one bounded outcome per status ───────────────────────────────────────────


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending"])
def test_a_non_completed_run_is_running_inside_the_age_limit_and_stale_outside(status):
    young = _one_entry(_run(M1, 2, status=status, conclusion=None, age=LIMIT - 1))
    old = _one_entry(_run(M1, 2, status=status, conclusion=None, age=LIMIT + 1))
    assert (young.state, young.reason) == (mw.RUN_RUNNING, None)
    assert (old.state, old.reason) == (mw.RUN_NOGO, "stale_run")


def test_a_run_with_an_unreadable_creation_time_is_stale_not_running():
    run = _run(M1, 2, status="queued", conclusion=None)
    for value in (None, "", "yesterday", 5):
        assert _one_entry({**run, "createdAt": value}).reason == "stale_run"


@pytest.mark.parametrize("conclusion,state,reason", [
    ("failure", mw.RUN_RED, None),
    ("cancelled", mw.RUN_NOGO, "cancelled"),
    ("timed_out", mw.RUN_NOGO, "timed_out"),
    ("startup_failure", mw.RUN_NOGO, "startup_failure"),
    ("skipped", mw.RUN_NOGO, "skipped"),
    ("neutral", mw.RUN_NOGO, "neutral"),
    ("action_required", mw.RUN_NOGO, "action_required"),
    ("stale", mw.RUN_NOGO, "stale"),
    ("flaky-ish", mw.RUN_NOGO, "flaky-ish"),
    (None, mw.RUN_NOGO, "no_conclusion"),
])
def test_every_completed_conclusion_has_one_outcome(conclusion, state, reason):
    entry = _one_entry(_run(M1, 2, conclusion=conclusion))
    assert (entry.state, entry.reason) == (state, reason)


def test_a_green_run_newest_is_the_heavy_base_not_an_entry():
    window = _window([M1, BASE], [_run(BASE, 1), _run(M1, 2)])
    assert (window.base_sha, window.entries, window.verdict) == (M1, (), mw.VERDICT_GREEN)


def test_a_run_without_a_status_is_a_named_nogo():
    assert _one_entry({"databaseId": 2, "headSha": M1, "conclusion": "success"}).reason == "unknown_status"


def test_a_commit_without_a_run_is_running_while_young_and_no_run_when_old():
    assert _one_entry(None, commit_time=_at(LIMIT - 1)).state == mw.RUN_RUNNING
    old = _one_entry(None, commit_time=_at(LIMIT + 1))
    assert (old.state, old.reason) == (mw.RUN_NOGO, mw.NOGO_REASON_NO_RUN)
    for missing in (None, "garbage"):
        assert _one_entry(None, commit_time=missing).reason == "no_run"


def test_age_limit_boundary_is_exclusive():
    assert _one_entry(_run(M1, 2, status="queued", conclusion=None, age=LIMIT)).reason == "stale_run"


def test_a_named_nogo_gives_the_window_verdict_nogo_and_red_wins_over_it():
    runs = [_run(BASE, 1), _run(M1, 2, conclusion="cancelled"), _run(M2, 3, conclusion="failure")]
    assert _window([M2, M1, BASE], runs).verdict == mw.VERDICT_RED
    window = _window([M1, BASE], runs[:2])
    assert (window.verdict, window.first_nogo.sha) == (mw.VERDICT_NOGO, M1)


def test_ten_green_non_heavy_candidates_without_a_heavy_one_is_history_incomplete():
    runner = _Runner(commits=[f"{i:040x}" for i in range(12)], files="docs/a.md\n")
    shas = runner.commits
    runs = json.dumps([_run(sha, i + 1) for i, sha in enumerate(shas)])
    runner.runs = runs
    window = mw.read_window(Path("."), runner=runner, now=NOW)
    assert (window.verdict, window.reason) == (mw.VERDICT_UNMEASURABLE, "history_incomplete")
    assert len(runner.file_calls) == mw.MAX_PROFILE_LOOKUPS


# ── the adapter's profile lookups ────────────────────────────────────────────


class _Runner:
    def __init__(self, commits, files="scripts/a.py\n", runs="[]", files_reply=None):
        self.commits, self.files, self.runs, self.files_reply = commits, files, runs, files_reply
        self.file_calls = []

    def __call__(self, argv, *, timeout, cwd=None):
        if argv[1] == "api" and "/commits/" in argv[2]:
            self.file_calls.append(argv)
            if isinstance(self.files_reply, BaseException):
                raise self.files_reply
            return self.files_reply or SimpleNamespace(returncode=0, stdout=self.files, stderr="")
        out = "".join(f"{sha}\t{_at(1)}\n" for sha in self.commits) if argv[1] == "api" else self.runs
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


def test_the_adapter_asks_green_candidates_newest_first_memoizes_and_stops_at_a_heavy_one():
    runner = _Runner([M2, M1, BASE], files="src/x.py\n")
    runner.runs = json.dumps([_run(M2, 3), _run(M1, 2), _run(BASE, 1)])
    window = mw.read_window(Path("."), runner=runner, now=NOW)
    assert (window.verdict, window.base_sha) == (mw.VERDICT_GREEN, M2)
    assert [c[2] for c in runner.file_calls] == [f"repos/{{owner}}/{{repo}}/commits/{M2}"]
    assert runner.file_calls[0][-2:] == ["--jq", ".files[].filename"]


def test_a_failed_file_lookup_is_unknown_never_heavy_or_light():
    profile_of = mw.make_profile_reader(_Runner([M1], files_reply=SimpleNamespace(returncode=1, stdout="", stderr="")), ".", "gh")
    assert profile_of(M1) == mw.PROFILE_UNKNOWN
    profile_of = mw.make_profile_reader(_Runner([M1], files_reply=FileNotFoundError("gh")), ".", "gh")
    assert profile_of(M1) == mw.PROFILE_UNKNOWN


def test_a_repeated_sha_costs_one_lookup():
    runner = _Runner([M1])
    profile_of = mw.make_profile_reader(runner, ".", "gh")
    assert [profile_of(M1), profile_of(M1)] == [mw.PROFILE_HEAVY] * 2
    assert len(runner.file_calls) == 1
