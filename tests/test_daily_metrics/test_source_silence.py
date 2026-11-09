"""The silence source: gaps over 30 minutes counted on the UTC day they close (PR-8)."""

from __future__ import annotations

import importlib
import importlib.util
import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

UTC = timezone.utc
DAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 10, 4, 10, tzinfo=UTC)
DAY_START = datetime.combine(DAY, time(0, 0, 0), tzinfo=UTC)
DAY_END = datetime.combine(DAY, time(23, 59, 59), tzinfo=UTC)
NOW_END = datetime.combine(date(2026, 11, 10), time(23, 59, 59), tzinfo=UTC)
LOOKBACK_START = DAY_START - timedelta(days=14)
METRIC = "silence_windows_over_30m"


def test_the_silence_source_exists():
    assert importlib.util.find_spec("daily_metrics.sources.silence") is not None


def test_discovery_finds_the_silence_source_without_failures():
    from daily_metrics import collect as collect_mod
    modules, failures = collect_mod.discover_sources(collect_mod.sources_dir())
    assert failures == []
    assert "silence" in [module.__name__.rsplit(".", 1)[-1] for module in modules]


@pytest.fixture()
def source():
    return importlib.import_module("daily_metrics.sources.silence")


def _open_runner(rows, returncode=0, stderr=""):
    def run(args, cwd):
        assert args[:4] == ["gh", "pr", "list", "--state"]
        return returncode, json.dumps(rows), stderr
    return run


@pytest.fixture(autouse=True)
def _never_real_gh(monkeypatch):
    try:
        module = importlib.import_module("daily_metrics.sources.silence")
    except ImportError:
        return
    monkeypatch.setattr(module, "_default_runner", _open_runner([]))


@pytest.fixture()
def ctx(tmp_path):
    from daily_metrics.collect import CollectContext
    context = CollectContext("proj", DAY, NOW)
    context._project_root = tmp_path
    return context


def _ts(month, day, hour=0, minute=0, second=0):
    return datetime(2026, month, day, hour, minute, second, tzinfo=UTC)


def _row(number, merged_at, created_at=None):
    stamp = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    created = created_at or merged_at - timedelta(hours=1)
    return {"number": number, "createdAt": stamp(created), "mergedAt": stamp(merged_at)}


def _population(rows, *, range_from, range_to, truncated=False, error=None):
    from daily_metrics.github_fetch import MergedPopulation
    return MergedPopulation(rows=tuple(rows), range_from=range_from, range_to=range_to,
                            fetched_at=NOW, truncated=truncated, error=error, calls=1)


def _run(source, ctx, monkeypatch, lookback_rows, *, day_rows=None, open_rows=None,
         day_truncated=False, lookback_truncated=False, day_error=None, open_returncode=0):
    if day_rows is None:
        day_rows = [row for row in lookback_rows
                    if datetime.strptime(row["mergedAt"], "%Y-%m-%dT%H:%M:%SZ").date() == DAY]
    day_pop = _population(day_rows, range_from=DAY_START, range_to=DAY_END,
                          truncated=day_truncated, error=day_error)
    look_pop = _population(lookback_rows, range_from=LOOKBACK_START, range_to=NOW_END,
                           truncated=lookback_truncated)

    def fake_fetch(from_day, to_day, **_kwargs):
        return day_pop if (from_day, to_day) == (DAY, DAY) else look_pop
    monkeypatch.setattr(source, "fetch_merged", fake_fetch)
    if open_rows is not None:
        monkeypatch.setattr(source, "_default_runner", _open_runner(open_rows, open_returncode))
    return source.collect(ctx)


def _quiet_chain(first, last, step_minutes=30):
    times = [first]
    while times[-1] + timedelta(minutes=step_minutes) <= last:
        times.append(times[-1] + timedelta(minutes=step_minutes))
    if times[-1] != last:
        times.append(last)
    return times


def test_three_gaps_closing_on_the_day_are_counted_with_open_pr_through(source, ctx, monkeypatch):
    p0, m1 = _ts(11, 6, 4, 18, 12), _ts(11, 9, 18, 29)
    m2, m3 = _ts(11, 9, 19, 0), _ts(11, 9, 20, 0)
    rows = [_row(1, p0), _row(2, m1), _row(3, m2), _row(4, m3)]
    rows += [_row(200 + n, _ts(11, 10, 1, 0), _ts(11, 9, 18, 45)) for n in range(11)]
    records = _run(source, ctx, monkeypatch, rows)
    assert set(records) == {METRIC}
    metric = records[METRIC]
    assert metric.value == 3 and metric.sample_size == 3
    windows = metric.details["windows"]
    assert [window["ended_at"] for window in windows] == [
        "2026-11-09T18:29:00Z", "2026-11-09T19:00:00Z", "2026-11-09T20:00:00Z"]
    assert [window["duration_minutes"] for window in windows] == [
        pytest.approx(5170.8), pytest.approx(31.0), pytest.approx(60.0)]
    assert windows[0]["open_pr_through"] == 0 and windows[2]["open_pr_through"] == 11
    assert all(not window["lookback_exhausted"] for window in windows)
    assert metric.details["work_ready_coverage"] == {
        "measured": 0, "total": 3, "reason": "no_historical_readiness_snapshot"}


def test_a_gap_of_exactly_thirty_minutes_is_not_counted(source, ctx, monkeypatch):
    rows = [_row(1, _ts(11, 8, 23, 59)), _row(2, _ts(11, 9, 0, 29))]
    metric = _run(source, ctx, monkeypatch, rows)[METRIC]
    assert metric.value == 0 and metric.details["windows"] == []


def test_a_gap_that_closes_on_the_next_day_is_not_counted(source, ctx, monkeypatch):
    chain = _quiet_chain(_ts(11, 9, 0, 10), _ts(11, 9, 23, 40))
    rows = [_row(1, chain[0] - timedelta(minutes=30))]
    rows += [_row(10 + index, moment) for index, moment in enumerate(chain)]
    rows += [_row(900, _ts(11, 10, 0, 20), _ts(11, 9, 22, 0))]
    metric = _run(source, ctx, monkeypatch, rows)[METRIC]
    assert metric.value == 0 and "open_window_at_day_end" not in metric.details


def test_the_window_after_the_last_merge_running_past_midnight_is_reported(source, ctx, monkeypatch):
    chain = _quiet_chain(_ts(11, 9, 0, 10), _ts(11, 9, 22, 0))
    rows = [_row(1, chain[0] - timedelta(minutes=30))]
    rows += [_row(10 + index, moment) for index, moment in enumerate(chain)]
    rows += [_row(900, _ts(11, 10, 2, 0), _ts(11, 9, 21, 0))]
    metric = _run(source, ctx, monkeypatch, rows)[METRIC]
    assert metric.value == 0
    assert metric.details["open_window_at_day_end"] == {
        "started_at": "2026-11-09T22:00:00Z", "closed_at": "2026-11-10T02:00:00Z"}


def test_a_merge_without_an_earlier_merge_in_the_lookback_counts_exhausted(source, ctx, monkeypatch):
    rows = [_row(1, _ts(11, 9, 15, 0))]
    metric = _run(source, ctx, monkeypatch, rows)[METRIC]
    window = metric.details["windows"][0]
    assert metric.value == 1
    assert window["started_at"] is None and window["lookback_exhausted"] is True
    assert window["duration_minutes_min"] == pytest.approx((15 * 60) + 14 * 24 * 60)


def test_open_pr_through_counts_an_open_and_a_later_merged_pr_only(source, ctx, monkeypatch):
    rows = [_row(1, _ts(11, 9, 9, 50)), _row(104, _ts(11, 9, 9, 55), _ts(11, 9, 9, 0)),
            _row(2, _ts(11, 9, 10, 0)), _row(3, _ts(11, 9, 11, 0)),
            _row(101, _ts(11, 9, 11, 30), _ts(11, 9, 9, 0))]
    open_rows = [{"number": 102, "createdAt": "2026-11-09T09:30:00Z"},
                 {"number": 103, "createdAt": "2026-11-09T10:30:00Z"}]
    windows = _run(source, ctx, monkeypatch, rows, open_rows=open_rows)[METRIC].details["windows"]
    window = next(w for w in windows if w["started_at"] == "2026-11-09T10:00:00Z")
    assert window["open_pr_through"] == 2


def test_a_lookback_row_missing_from_the_day_population_is_a_population_mismatch(source, ctx, monkeypatch):
    m1, m2 = _row(1, _ts(11, 9, 10, 0)), _row(2, _ts(11, 9, 11, 0))
    metric = _run(source, ctx, monkeypatch, [m1, m2], day_rows=[m1])[METRIC]
    assert metric.value is None and metric.unknown_reason == "population_mismatch"
    assert metric.details["population"]["n"] == 1


def test_an_open_list_at_the_limit_is_a_truncated_fetch(source, ctx, monkeypatch):
    rows = [_row(1, _ts(11, 8, 23, 50)), _row(2, _ts(11, 9, 0, 10))]
    open_rows = [{"number": n, "createdAt": "2026-11-09T09:00:00Z"} for n in range(1000)]
    metric = _run(source, ctx, monkeypatch, rows, open_rows=open_rows)[METRIC]
    assert metric.value is None and metric.unknown_reason == "truncated_fetch"
    assert metric.details["population"]["n"] == 1


def test_a_truncated_population_is_a_truncated_fetch(source, ctx, monkeypatch):
    rows = [_row(1, _ts(11, 9, 10, 0))]
    for kwargs in ({"day_truncated": True}, {"lookback_truncated": True}):
        metric = _run(source, ctx, monkeypatch, rows, **kwargs)[METRIC]
        assert metric.value is None and metric.unknown_reason == "truncated_fetch"


def test_a_failed_read_or_checkout_is_gh_failed(source, ctx, monkeypatch):
    rows = [_row(1, _ts(11, 9, 10, 0))]
    assert _run(source, ctx, monkeypatch, rows, day_error="gh_failed")[METRIC].unknown_reason == "gh_failed"
    metric = _run(source, ctx, monkeypatch, rows, open_rows=[], open_returncode=1)[METRIC]
    assert metric.value is None and metric.unknown_reason == "gh_failed"


def test_an_unavailable_checkout_is_gh_failed(source, monkeypatch):
    from daily_metrics.collect import CollectContext
    context = CollectContext("proj", DAY, NOW)
    monkeypatch.setattr(context, "project_root", lambda: (_ for _ in ()).throw(RuntimeError("no checkout")))
    metric = source.collect(context)[METRIC]
    assert metric.value is None and metric.unknown_reason == "gh_failed"
    assert "no checkout" in metric.details["error"]
