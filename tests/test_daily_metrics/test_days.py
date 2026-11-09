"""Calendar tests for the daily-metrics track (PR-2).

Every clock in this file is fixed. No test reads ``datetime.now()``: a
parametrize id built from the wall clock broke xdist collection on #2556.
"""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone

import pytest


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_overrides(monkeypatch):
    """The limits are process-wide; no ambient override may leak into a test."""
    for name in (
        "VNX_DAILY_METRICS_SETTLE_HOURS",
        "VNX_DAILY_METRICS_CATCHUP_DAYS",
        "VNX_DAILY_METRICS_FRESH_HOURS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def days():
    from daily_metrics import days as module

    return module


def test_days_module_is_importable():
    """Red run: this assert fails on the commit before ``days.py`` exists."""
    assert importlib.util.find_spec("daily_metrics.days") is not None


def test_module_constants_are_the_documented_defaults(days):
    assert (days.SETTLE_HOURS, days.CATCHUP_DAYS, days.FRESH_HOURS) == (2, 14, 30)


def test_is_settled_boundary_is_two_hours_after_the_utc_day(days):
    day = date(2026, 11, 7)
    assert days.is_settled(day, _utc(2026, 11, 8, 1, 59, 59)) is False
    assert days.is_settled(day, _utc(2026, 11, 8, 2, 0, 0)) is True


def test_completed_days_latest_is_the_last_settled_day(days):
    before = days.completed_days(_utc(2026, 11, 8, 1, 59, 59))
    after = days.completed_days(_utc(2026, 11, 8, 2, 0, 0))
    assert before[-1] == date(2026, 11, 6)
    assert after[-1] == date(2026, 11, 7)
    assert after[-2] == date(2026, 11, 6)


def test_completed_days_is_fourteen_days_oldest_first_and_never_today(days):
    now = _utc(2026, 11, 8, 3, 10, 0)
    completed = days.completed_days(now)
    assert len(completed) == 14
    assert completed == sorted(completed)
    assert completed[0] == date(2026, 10, 25)
    assert completed[-1] == date(2026, 11, 7)
    assert now.date() not in completed


def test_completed_days_never_includes_today_even_at_zero_settle(days, monkeypatch):
    monkeypatch.setenv(days.SETTLE_HOURS_ENV, "0")
    completed = days.completed_days(_utc(2026, 11, 8, 0, 0, 0))
    assert completed[-1] == date(2026, 11, 7)
    assert date(2026, 11, 8) not in completed


def test_plan_catchup_is_oldest_first_and_skips_final_days(days):
    now = _utc(2026, 11, 8, 3, 10, 0)
    window = days.completed_days(now)
    existing = {day: "final" for day in window[:-1]}
    existing[window[0]] = "incomplete"
    plan = days.plan_catchup(now, existing)
    assert plan == [window[0], window[-1]]


def test_plan_catchup_holds_incomplete_excludes_final_and_today(days):
    now = _utc(2026, 11, 8, 3, 10, 0)
    incomplete = date(2026, 11, 5)
    final = date(2026, 11, 6)
    plan = days.plan_catchup(now, {incomplete: "incomplete", final: "final"})
    assert incomplete in plan
    assert final not in plan
    assert date(2026, 11, 8) not in plan
    assert plan == sorted(plan)


def test_plan_catchup_is_at_most_fourteen_entries_oldest_first(days):
    plan = days.plan_catchup(_utc(2026, 11, 8, 3, 10, 0), {})
    assert len(plan) == 14
    assert plan[0] == date(2026, 10, 25)
    assert plan[-1] == date(2026, 11, 7)


def test_plan_catchup_cap_follows_the_catchup_days_override(days, monkeypatch):
    monkeypatch.setenv(days.CATCHUP_DAYS_ENV, "3")
    plan = days.plan_catchup(_utc(2026, 11, 8, 3, 10, 0), {})
    assert plan == [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 7)]


def test_plan_catchup_treats_an_unrecognised_status_as_not_final(days):
    """Fail closed: only the exact status ``final`` marks a day as done."""
    status = {date(2026, 11, 6): "done"}
    assert date(2026, 11, 6) in days.plan_catchup(_utc(2026, 11, 8, 3, 10, 0), status)


def test_freshness_boundary_is_thirty_hours_after_the_utc_day(days):
    day = date(2026, 11, 6)
    day_end = _utc(2026, 11, 7)
    assert days.is_fresh(day, day_end + timedelta(hours=30)) is True
    assert days.is_fresh(day, day_end + timedelta(hours=30, minutes=1)) is False


def test_is_backfilled_is_the_negation_of_is_fresh(days):
    day = date(2026, 11, 6)
    day_end = _utc(2026, 11, 7)
    assert days.is_backfilled(day, day_end + timedelta(hours=30)) is False
    assert days.is_backfilled(day, day_end + timedelta(hours=31)) is True


@pytest.mark.parametrize(
    ("local_date", "expected_utc"),
    [
        (date(2026, 7, 15), datetime(2026, 7, 15, 2, 10, tzinfo=timezone.utc)),
        (date(2026, 11, 8), datetime(2026, 11, 8, 3, 10, tzinfo=timezone.utc)),
    ],
    ids=["cest-2026-07-15", "cet-2026-11-08"],
)
def test_local_run_time_is_after_settle_in_both_dst_regimes(days, local_date, expected_utc):
    run_utc = days.local_run_utc(local_date)
    assert run_utc == expected_utc
    assert run_utc.utcoffset() == timedelta(0)
    assert days.settled(run_utc, local_date - timedelta(days=1)) is True


@pytest.mark.parametrize(
    "call",
    [
        lambda d: d.is_settled(date(2026, 11, 6), datetime(2026, 11, 8, 2, 0)),
        lambda d: d.completed_days(datetime(2026, 11, 8, 2, 0)),
        lambda d: d.plan_catchup(datetime(2026, 11, 8, 2, 0), {}),
        lambda d: d.is_fresh(date(2026, 11, 6), datetime(2026, 11, 8, 2, 0)),
        lambda d: d.is_backfilled(date(2026, 11, 6), datetime(2026, 11, 8, 2, 0)),
        lambda d: d.settled(datetime(2026, 11, 8, 2, 0), date(2026, 11, 6)),
    ],
    ids=["is_settled", "completed_days", "plan_catchup", "is_fresh", "is_backfilled", "settled"],
)
def test_a_naive_datetime_raises(days, call):
    with pytest.raises(ValueError):
        call(days)


def test_settle_hours_override_moves_the_boundary(days, monkeypatch):
    now = _utc(2026, 11, 8, 2, 0, 0)
    assert days.completed_days(now)[-1] == date(2026, 11, 7)
    monkeypatch.setenv(days.SETTLE_HOURS_ENV, "6")
    assert days.is_settled(date(2026, 11, 7), now) is False
    assert days.completed_days(now)[-1] == date(2026, 11, 6)


def test_fresh_hours_override_moves_the_boundary(days, monkeypatch):
    day = date(2026, 11, 6)
    day_end = _utc(2026, 11, 7)
    assert days.is_fresh(day, day_end + timedelta(hours=30)) is True
    monkeypatch.setenv(days.FRESH_HOURS_ENV, "6")
    assert days.is_fresh(day, day_end + timedelta(hours=30)) is False


def test_an_invalid_override_is_refused(days, monkeypatch):
    monkeypatch.setenv(days.SETTLE_HOURS_ENV, "soon")
    with pytest.raises(ValueError):
        days.completed_days(_utc(2026, 11, 8, 2, 0, 0))


def test_a_negative_override_is_refused(days, monkeypatch):
    monkeypatch.setenv(days.CATCHUP_DAYS_ENV, "-1")
    with pytest.raises(ValueError):
        days.completed_days(_utc(2026, 11, 8, 2, 0, 0))
