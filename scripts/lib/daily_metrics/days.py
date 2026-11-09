"""The calendar of the daily-metrics job (decision 20).

A UTC day ``D`` is *settled* once ``now >= D + 1 day + SETTLE_HOURS``: gate
results and receipts carry their ``recorded_at`` after the fact, so a day that
just ended is not measurable yet. The job runs at 04:10 local time, which is
after 02:00 UTC in both CET and CEST, so it never writes the day it runs in.
Days the Mac missed are caught up over the last ``CATCHUP_DAYS`` settled days,
oldest first. A day collected later than ``D + 1 day + FRESH_HOURS`` after its
end is *backfilled*: it still counts in the baseline, but not in the seven-day
freshness check.

Every limit is a module constant with an environment override that is read at
call time (decision 18: no ``config_registry`` key):

- ``SETTLE_HOURS`` (default 2) — ``VNX_DAILY_METRICS_SETTLE_HOURS``
- ``CATCHUP_DAYS`` (default 14) — ``VNX_DAILY_METRICS_CATCHUP_DAYS``
- ``FRESH_HOURS`` (default 30) — ``VNX_DAILY_METRICS_FRESH_HOURS``

All day arithmetic is in UTC and every function takes its clock. A naive
datetime is a ``ValueError`` — there is no "assume UTC" path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

SETTLE_HOURS = 2
CATCHUP_DAYS = 14
FRESH_HOURS = 30

SETTLE_HOURS_ENV = "VNX_DAILY_METRICS_SETTLE_HOURS"
CATCHUP_DAYS_ENV = "VNX_DAILY_METRICS_CATCHUP_DAYS"
FRESH_HOURS_ENV = "VNX_DAILY_METRICS_FRESH_HOURS"

#: The only day status that means "do not collect this day again" (decision 20).
FINAL_STATUS = "final"

ONE_DAY = timedelta(days=1)


def _int_override(env_name: str, default: int) -> int:
    """The override for ``env_name``, or ``default``; an unusable value raises."""
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{env_name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{env_name} must not be negative, got {value}")
    return value


def settle_hours() -> int:
    """``SETTLE_HOURS`` with its env override, read at call time."""
    return _int_override(SETTLE_HOURS_ENV, SETTLE_HOURS)


def catchup_days() -> int:
    """``CATCHUP_DAYS`` with its env override, read at call time."""
    return _int_override(CATCHUP_DAYS_ENV, CATCHUP_DAYS)


def fresh_hours() -> int:
    """``FRESH_HOURS`` with its env override, read at call time."""
    return _int_override(FRESH_HOURS_ENV, FRESH_HOURS)


def _as_utc(moment: datetime) -> datetime:
    """``moment`` as a UTC-aware datetime; a naive one raises ``ValueError``."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("naive datetime: the day calendar is UTC-only, pass an aware datetime")
    return moment.astimezone(timezone.utc)


def _utc_midnight(day: date) -> datetime:
    """00:00 UTC of ``day``."""
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def fresh_until(day: date) -> datetime:
    """The last instant a collection of ``day`` is fresh: ``day end + FRESH_HOURS``."""
    return _utc_midnight(day) + ONE_DAY + timedelta(hours=fresh_hours())


def settled_at(day: date) -> datetime:
    """The first instant ``day`` is settled: ``day + 1 day + SETTLE_HOURS``, UTC."""
    return _utc_midnight(day) + ONE_DAY + timedelta(hours=settle_hours())


def is_settled(day: date, now: datetime) -> bool:
    """True when ``now`` is at or past ``day + 1 day + SETTLE_HOURS``, all in UTC."""
    return _as_utc(now) >= settled_at(day)


def latest_settled_day(now: datetime) -> date:
    """The most recent settled UTC day; never the day of ``now``."""
    return (_as_utc(now) - ONE_DAY - timedelta(hours=settle_hours())).date()


def completed_days(now: datetime) -> list[date]:
    """The settled days of the last ``CATCHUP_DAYS``, oldest first; never today."""
    latest = latest_settled_day(now)
    return [latest - timedelta(days=offset) for offset in range(catchup_days() - 1, -1, -1)]


def plan_catchup(now: datetime, existing: Mapping[date, str]) -> list[date]:
    """Settled days with no ``final`` file, oldest first, capped at ``CATCHUP_DAYS``.

    An ``incomplete`` day is planned again. Only the exact status ``final``
    excludes a day: a missing entry, ``None`` and an unrecognised status all
    stay in the plan (fail closed — unknown is not done). Today is never planned.
    """
    planned = [day for day in completed_days(now) if existing.get(day) != FINAL_STATUS]
    return planned[:catchup_days()]


def is_fresh(day: date, collected_at: datetime) -> bool:
    """True when ``collected_at <= day end + FRESH_HOURS`` (decision 20)."""
    return _as_utc(collected_at) <= fresh_until(day)


def is_backfilled(day: date, collected_at: datetime) -> bool:
    """True when the collection came later than the fresh window; the negation of fresh."""
    return not is_fresh(day, collected_at)


def settled(run_utc: datetime, day: date) -> bool:
    """The check the 04:10 job uses: was ``day`` settled at ``run_utc``?"""
    return is_settled(day, run_utc)


def local_run_utc(
    local_date: date,
    hour: int = 4,
    minute: int = 10,
    tz: str = "Europe/Amsterdam",
) -> datetime:
    """The UTC instant of the local launchd launch time on ``local_date``."""
    local = datetime.combine(local_date, time(hour=hour, minute=minute), tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc)
