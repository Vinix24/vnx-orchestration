"""The merges source: count and lead time from one proven population (PR-4)."""

from __future__ import annotations

import importlib
import importlib.util
from datetime import date, datetime, timezone

import pytest

DAY, NOW = date(2026, 11, 9), datetime(2026, 11, 10, 4, 10, tzinfo=timezone.utc)
UTC = timezone.utc
MEDIAN, P75 = "pr_open_to_merge_median_hours", "pr_open_to_merge_p75_hours"

def test_the_github_merges_source_exists():
    assert importlib.util.find_spec("daily_metrics.sources.github_merges") is not None

@pytest.fixture()
def source():
    return importlib.import_module("daily_metrics.sources.github_merges")

@pytest.fixture()
def ctx(tmp_path):
    from daily_metrics.collect import CollectContext

    context = CollectContext("proj", DAY, NOW)
    context._project_root = tmp_path
    return context

def _row(number, created_at="2026-11-09T00:00:00Z", merged_at="2026-11-09T01:00:00Z"):
    return {"number": number, "createdAt": created_at, "mergedAt": merged_at}

def _population(rows, *, truncated=False, error=None):
    from daily_metrics.github_fetch import MergedPopulation

    return MergedPopulation(rows=tuple(rows), range_from=datetime(2026, 11, 9, tzinfo=UTC),
                            range_to=datetime(2026, 11, 9, 23, 59, 59, tzinfo=UTC),
                            fetched_at=NOW, truncated=truncated, error=error, calls=1)

def _collect(source, ctx, population, monkeypatch):
    monkeypatch.setattr(source, "fetch_merged", lambda *args, **kwargs: population)
    return source.collect(ctx)

def test_three_merges_are_a_measured_three_with_one_population(source, ctx, monkeypatch):
    rows = [_row(1, merged_at="2026-11-09T01:00:00Z"),
            _row(2, merged_at="2026-11-09T02:00:00Z"),
            _row(3, merged_at="2026-11-09T04:00:00Z")]
    records = _collect(source, ctx, _population(rows), monkeypatch)
    merges = records["merges"]
    assert merges.value == 3 and merges.sample_size == 3
    assert merges.details["population"]["n"] == 3
    assert merges.details["population"]["truncated"] is False
    assert sum(merges.details["hour_histogram"]) == 3
    assert records[MEDIAN].value == pytest.approx(2.0)
    assert records[P75].value == pytest.approx(3.0)
    assert records[MEDIAN].details["samples"] == [1.0, 2.0, 4.0]
    assert records[MEDIAN].details["population"] == merges.details["population"]

def test_a_truncated_population_leaves_all_three_unknown(source, ctx, monkeypatch):
    records = _collect(source, ctx, _population([_row(1)], truncated=True), monkeypatch)
    assert {record.unknown_reason for record in records.values()} == {"truncated_fetch"}
    assert all(record.value is None for record in records.values())

@pytest.mark.parametrize("error", ["gh_failed", "row_outside_range"])
def test_a_fetch_error_leaves_all_three_unknown(source, ctx, monkeypatch, error):
    records = _collect(source, ctx, _population([_row(1)], error=error), monkeypatch)
    assert {record.unknown_reason for record in records.values()} == {"gh_failed"}
    assert all(record.details["population_error"] == error for record in records.values())

def _refuse():
    raise RuntimeError("no checkout")

def test_a_refusing_project_root_is_a_named_unknown(source, monkeypatch):
    from daily_metrics.collect import CollectContext

    context = CollectContext("proj", DAY, NOW)
    monkeypatch.setattr(context, "project_root", _refuse)
    records = source.collect(context)
    assert {record.unknown_reason for record in records.values()} == {"gh_failed"}
    assert records["merges"].details["error"] == "RuntimeError: no checkout"

def test_a_day_without_merges_is_a_measured_zero(source, ctx, monkeypatch):
    records = _collect(source, ctx, _population([]), monkeypatch)
    assert records["merges"].value == 0 and records["merges"].sample_size == 0
    assert records["merges"].details["hour_histogram"] == [0] * 24
    assert {records[MEDIAN].unknown_reason, records[P75].unknown_reason} == {"no_merges_on_day"}

def test_a_row_without_created_at_is_a_population_mismatch(source, ctx, monkeypatch):
    rows = [_row(1), {"number": 2, "mergedAt": "2026-11-09T03:00:00Z"}]
    records = _collect(source, ctx, _population(rows), monkeypatch)
    assert {record.unknown_reason for record in records.values()} == {"population_mismatch"}
    assert all(record.value is None for record in records.values())
