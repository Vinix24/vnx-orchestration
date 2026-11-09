"""A read proves its coverage by returning fewer rows than its limit (PR-4, decision 21)."""

from __future__ import annotations

import importlib
import importlib.util
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

DAY, NOW = date(2026, 11, 9), datetime(2026, 11, 10, 4, 10, tzinfo=timezone.utc)
ROOT = Path("/repo")

def test_the_github_fetch_module_exists():
    assert importlib.util.find_spec("daily_metrics.github_fetch") is not None

@pytest.fixture()
def gh():
    module = importlib.import_module("daily_metrics.github_fetch")
    module.clear_cache()
    yield module
    module.clear_cache()

def _runner(respond):
    calls = []

    def run(args, cwd):
        calls.append((list(args), cwd))
        return respond(list(args))

    return run, calls

def _row(number, merged_at):
    return {"number": number, "createdAt": "2026-11-09T00:00:00Z", "mergedAt": merged_at}

def _ok(rows):
    return 0, json.dumps(rows), ""

def _span(args):
    start, end = args[args.index("--search") + 1].split(":", 1)[1].split("..")
    to_dt = lambda stamp: datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return to_dt(start), to_dt(end)

WHOLE = [_row(1, "2026-11-09T01:00:00Z"), _row(2, "2026-11-09T05:00:00Z"),
         _row(3, "2026-11-09T17:00:00Z"), _row(4, "2026-11-09T21:00:00Z")]

def test_a_full_page_is_halved_until_each_half_proves_the_coverage(gh, monkeypatch):
    monkeypatch.setenv("VNX_DAILY_METRICS_GH_LIMIT", "4")

    def respond(args):
        start, end = _span(args)
        if start.hour == 0 and end.hour == 23:
            return _ok(WHOLE)
        inside = [row for row in WHOLE if start <= _span_of(row) <= end]
        return _ok(inside)

    runner, calls = _runner(respond)
    population = gh.fetch_merged(DAY, DAY, project_root=ROOT, now=NOW, runner=runner)
    assert population.truncated is False
    assert len(population.rows) == 4
    assert len(calls) > 1

def _span_of(row):
    return datetime.fromisoformat(row["mergedAt"].replace("Z", "+00:00"))

def test_a_page_that_stays_full_at_the_last_level_is_truncated(gh, monkeypatch):
    monkeypatch.setenv("VNX_DAILY_METRICS_GH_LIMIT", "2")
    monkeypatch.setenv("VNX_DAILY_METRICS_GH_MAX_DEPTH", "2")

    def respond(args):
        start, _ = _span(args)
        stamp = f"{start:%Y-%m-%dT%H:%M:%S}Z"
        return _ok([_row(int(start.timestamp()) * 10 + offset, stamp) for offset in range(2)])

    population = gh.fetch_merged(DAY, DAY, project_root=ROOT, now=NOW,
                                 runner=_runner(respond)[0])
    assert population.truncated is True and population.error is None

def test_a_row_outside_the_range_or_an_unparseable_stamp_is_unknown(gh):
    for rows in ([_row(1, "2026-11-10T01:00:00Z")], [_row(1, "not-a-stamp")]):
        population = gh.fetch_merged(DAY, DAY, project_root=ROOT, now=NOW,
                                     runner=_runner(lambda args: _ok(rows))[0])
        assert population.error == "row_outside_range"
        gh.clear_cache()

def test_a_failed_or_unparseable_read_is_gh_failed_with_the_detail(gh):
    for response, detail in (((1, "", "HTTP 502"), "rc=1: HTTP 502"),
                             ((0, "not json", ""), "unparseable gh output")):
        population = gh.fetch_merged(DAY, DAY, project_root=ROOT, now=NOW,
                                     runner=_runner(lambda args: response)[0])
        assert population.error == "gh_failed"
        assert population.error_detail.startswith(detail)
        gh.clear_cache()

def test_one_population_per_run_and_clear_cache_starts_a_new_one(gh):
    runner, calls = _runner(lambda args: _ok([]))
    first = gh.fetch_merged(DAY, DAY, project_root=ROOT, now=NOW, runner=runner)
    assert gh.fetch_merged(DAY, DAY, project_root=ROOT, now=NOW, runner=runner) is first
    assert len(calls) == 1
    gh.clear_cache()
    assert gh.fetch_merged(DAY, DAY, project_root=ROOT, now=NOW, runner=runner) is not first
    assert len(calls) == 2

def test_a_failed_read_is_never_memoised_and_the_next_call_retries(gh):
    responses = [(1, "", "HTTP 502"), _ok([_row(1, "2026-11-09T01:00:00Z")])]
    runner, calls = _runner(lambda args: responses.pop(0))
    first = gh.fetch_merged(DAY, DAY, project_root=ROOT, now=NOW, runner=runner)
    assert first.error == "gh_failed"
    second = gh.fetch_merged(DAY, DAY, project_root=ROOT, now=NOW, runner=runner)
    assert second.error is None and len(second.rows) == 1
    assert len(calls) == 2
