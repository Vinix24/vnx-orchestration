"""The CI source: median CI duration per merged PR from the runs on its head (PR-6)."""

from __future__ import annotations

import importlib
import importlib.util
from datetime import date, datetime, timezone

import pytest

DAY, NOW = date(2026, 11, 9), datetime(2026, 11, 10, 4, 10, tzinfo=timezone.utc)
UTC = timezone.utc
CI = "ci_duration_seconds_median"
SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40

def test_the_ci_source_exists():
    assert importlib.util.find_spec("daily_metrics.sources.ci") is not None


def test_the_sources_directory_publishes_the_ci_source():
    """Discovery over the real package sources imports it without a failure."""
    from daily_metrics import collect as collect_mod

    modules, failures = collect_mod.discover_sources(collect_mod.sources_dir())
    assert failures == []
    assert "ci" in [module.__name__.rsplit(".", 1)[-1] for module in modules]

@pytest.fixture()
def source():
    return importlib.import_module("daily_metrics.sources.ci")

@pytest.fixture()
def ctx(tmp_path):
    from daily_metrics.collect import CollectContext

    context = CollectContext("proj", DAY, NOW)
    context._project_root = tmp_path
    return context

def _row(number, sha):
    return {"number": number, "headRefOid": sha}

def _population(*rows):
    from daily_metrics.github_fetch import MergedPopulation

    return MergedPopulation(rows=tuple(rows), range_from=datetime(2026, 11, 9, tzinfo=UTC),
                            range_to=datetime(2026, 11, 9, 23, 59, 59, tzinfo=UTC),
                            fetched_at=NOW, truncated=False, error=None, calls=1)

def _run(start, end, status="completed"):
    return {"databaseId": 1, "status": status, "conclusion": "success",
            "event": "pull_request", "startedAt": start, "updatedAt": end,
            "workflowName": "VNX CI"}

def _measure(source, ctx, population, monkeypatch, responses):
    """Run ``collect`` with a fake population and a fake ``gh`` runner."""
    calls = []

    def runner(args, cwd):
        sha = args[args.index("--commit") + 1]
        calls.append(list(args))
        return responses[sha]

    monkeypatch.setattr(source, "fetch_merged", lambda *args, **kwargs: population)
    monkeypatch.setattr(source, "_default_runner", runner)
    return source.collect(ctx)[CI], calls

def test_two_completed_runs_span_min_start_to_max_end(source, ctx, monkeypatch):
    runs = [_run("2026-11-09T10:00:00Z", "2026-11-09T10:05:00Z"),
            _run("2026-11-09T10:01:00Z", "2026-11-09T10:10:00Z")]
    record, _ = _measure(source, ctx, _population(_row(1, SHA_A)), monkeypatch, {SHA_A: (0, _json(runs), "")})
    assert record.value == pytest.approx(600.0)
    assert record.sample_size == 1

def test_three_prs_are_the_median_of_their_durations(source, ctx, monkeypatch):
    responses = {
        SHA_A: (0, _json([_run("2026-11-09T10:00:00Z", "2026-11-09T10:02:00Z")]), ""),
        SHA_B: (0, _json([_run("2026-11-09T10:00:00Z", "2026-11-09T10:05:00Z")]), ""),
        SHA_C: (0, _json([_run("2026-11-09T10:00:00Z", "2026-11-09T10:20:00Z")]), ""),
    }
    population = _population(_row(1, SHA_A), _row(2, SHA_B), _row(3, SHA_C))
    record, _ = _measure(source, ctx, population, monkeypatch, responses)
    assert record.value == pytest.approx(300.0)
    assert record.sample_size == 3
    assert record.details["samples"] == [120.0, 300.0, 1200.0]
    assert record.details["p75"] == pytest.approx(750.0)

def test_a_run_that_is_not_completed_leaves_the_metric_unreadable(source, ctx, monkeypatch):
    runs = [_run("2026-11-09T10:00:00Z", "2026-11-09T10:05:00Z", status="in_progress")]
    record, _ = _measure(source, ctx, _population(_row(7, SHA_A)), monkeypatch, {SHA_A: (0, _json(runs), "")})
    assert record.value is None
    assert record.unknown_reason == "ci_run_unreadable_for_1_prs"
    assert record.details["unreadable_prs"] == [7]

def test_a_short_sha_is_unreadable_without_calling_gh(source, ctx, monkeypatch):
    short = "a" * 39
    record, calls = _measure(source, ctx, _population(_row(7, short)), monkeypatch, {})
    assert record.value is None
    assert record.unknown_reason == "ci_run_unreadable_for_1_prs"
    assert calls == []

def test_an_empty_run_list_is_a_measured_no_run(source, ctx, monkeypatch):
    responses = {
        SHA_A: (0, _json([_run("2026-11-09T10:00:00Z", "2026-11-09T10:02:00Z")]), ""),
        SHA_B: (0, "[]", ""),
    }
    population = _population(_row(1, SHA_A), _row(2, SHA_B))
    record, _ = _measure(source, ctx, population, monkeypatch, responses)
    assert record.value == pytest.approx(120.0)
    assert record.sample_size == 1
    assert record.details["no_run_prs"] == [2]

def test_no_runs_on_any_head_is_permanent_and_final(source, ctx, monkeypatch):
    from daily_metrics import metric

    responses = {SHA_A: (0, "[]", ""), SHA_B: (0, "[]", "")}
    population = _population(_row(1, SHA_A), _row(2, SHA_B))
    record, _ = _measure(source, ctx, population, monkeypatch, responses)
    assert record.value is None
    assert record.unknown_reason == "no_ci_runs_on_any_head"
    assert record.details["no_run_prs"] == [1, 2]
    assert metric.day_status({CI: record}) == "final"

def test_an_unreadable_pr_makes_the_day_incomplete(source, ctx, monkeypatch):
    from daily_metrics import metric

    runs = [_run("2026-11-09T10:00:00Z", "2026-11-09T10:05:00Z", status="queued")]
    record, _ = _measure(source, ctx, _population(_row(1, SHA_A)), monkeypatch, {SHA_A: (0, _json(runs), "")})
    assert metric.day_status({CI: record}) == "incomplete"

def test_the_runner_reads_the_runs_of_the_head_commit(source, ctx, monkeypatch):
    runs = [_run("2026-11-09T10:00:00Z", "2026-11-09T10:05:00Z")]
    _, calls = _measure(source, ctx, _population(_row(1, SHA_A)), monkeypatch, {SHA_A: (0, _json(runs), "")})
    argv = calls[0]
    assert argv[:3] == ["gh", "run", "list"]
    assert argv[argv.index("--commit") + 1] == SHA_A
    assert argv[argv.index("--limit") + 1] == "100"
    assert "startedAt" in argv[argv.index("--json") + 1]

def test_a_failed_gh_call_is_unreadable(source, ctx, monkeypatch):
    record, _ = _measure(source, ctx, _population(_row(1, SHA_A)), monkeypatch,
                         {SHA_A: (1, "", "boom")})
    assert record.value is None
    assert record.unknown_reason == "ci_run_unreadable_for_1_prs"

def test_unparseable_gh_output_is_unreadable(source, ctx, monkeypatch):
    record, _ = _measure(source, ctx, _population(_row(1, SHA_A)), monkeypatch,
                         {SHA_A: (0, "not json", "")})
    assert record.value is None
    assert record.unknown_reason == "ci_run_unreadable_for_1_prs"

def _json(value):
    import json

    return json.dumps(value)
