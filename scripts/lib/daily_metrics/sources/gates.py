"""The gate-duration source: one record per seat, plus what was not a seat.

Decision 23: gate duration is not one number. Seats differ by orders of
magnitude (``ci_gate`` under a second, ``deepseek_gate`` minutes), so
``gate_duration_seconds_by_seat`` is a :class:`metric.KeyedMetric` with a full
record per seat. Nothing is dropped silently: a result file that is no seat,
has no duration or has no timestamp is counted in ``details`` instead.

A result's day is the UTC date of ``recorded_at``, else ``completed_at``
(decision 6). A seat seen in the 14 days before the day keeps an entry with
``no_results_for_seat_on_day`` so its baseline keeps running. When no seat has
an entry at all the source returns a scalar unknown record, because
``KeyedMetric`` refuses zero entries.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from project_root import resolve_central_data_dir

from daily_metrics import metric
from daily_metrics.baseline import MAX_SAMPLES_PER_DAY, quantile

SOURCE = "daily_metrics.gates"
METRIC_ID = "gate_duration_seconds_by_seat"
NO_RESULTS_REASON = "no_results_for_seat_on_day"
NO_DURATION_REASON = "no_duration_on_day"
LOOKBACK_DAYS = 14

_FILE_RE = re.compile(r"^pr-\d+-(?P<seat>.+)\.json$")
_SEAT_RE = re.compile(r"^[a-z0-9]+_(gate|review)$")


def _results_dir(project_id: str) -> Path:
    """``<central data dir>/state/review_gates/results`` for ``project_id``."""
    return resolve_central_data_dir(project_id) / "state" / "review_gates" / "results"


def _seat_from_name(path: Path) -> str | None:
    """The seat a file name names, or ``None`` when it names no seat."""
    match = _FILE_RE.match(path.name)
    if match is None:
        return None
    seat = match.group("seat")
    return seat if _SEAT_RE.match(seat) else None


def _read_result(path: Path) -> dict[str, Any] | None:
    """The result record, or ``None`` when the file is not a readable JSON object."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _stamp_day(value: Any) -> date | None:
    """The UTC date of an ISO timestamp; ``None`` when it is absent or does not parse."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)
    return stamp.date()


def _result_day(record: dict[str, Any]) -> date | None:
    """The day of ``recorded_at``, else ``completed_at`` (decision 6)."""
    return _stamp_day(record.get("recorded_at")) or _stamp_day(record.get("completed_at"))


def _is_numeric(value: Any) -> bool:
    """A real, finite number; ``bool`` and ``NaN`` are not a duration."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _scan(results_dir: Path, day: date) -> tuple[dict[str, int], dict[str, dict], set[str]]:
    """Split the day's results into per-seat buckets, lookback seats and counters."""
    counters = {"unrecognised_files": 0, "unattributed_results": 0, "unreadable_files": 0}
    on_day: dict[str, dict] = {}
    seen_before: set[str] = set()
    window_start = day - timedelta(days=LOOKBACK_DAYS)
    if not results_dir.is_dir():
        return counters, on_day, seen_before
    for path in sorted(results_dir.glob("*.json")):
        seat = _seat_from_name(path)
        if seat is None:
            counters["unrecognised_files"] += 1
            continue
        record = _read_result(path)
        if record is None:
            counters["unreadable_files"] += 1
            continue
        result_day = _result_day(record)
        if result_day is None:
            counters["unattributed_results"] += 1
            continue
        if result_day == day:
            bucket = on_day.setdefault(seat, {"durations": [], "no_duration": 0})
            duration = record.get("duration_seconds")
            if _is_numeric(duration):
                bucket["durations"].append(float(duration))
            else:
                bucket["no_duration"] += 1
        elif window_start <= result_day < day:
            seen_before.add(seat)
    return counters, on_day, seen_before


def _day_entry(bucket: dict) -> metric.ScalarMetric:
    """A seat's record for the day: the median, or ``no_duration_on_day``."""
    durations: list[float] = bucket["durations"]
    details = {
        "p75": quantile(durations, 0.75) if durations else None,
        "samples": durations[:MAX_SAMPLES_PER_DAY],
        "samples_truncated": len(durations) > MAX_SAMPLES_PER_DAY,
        "no_duration_n": bucket["no_duration"],
    }
    if not durations:
        return metric.ScalarMetric(
            value=None, source=SOURCE, unknown_reason=NO_DURATION_REASON, details=details
        )
    return metric.ScalarMetric(
        value=quantile(durations, 0.5),
        source=SOURCE,
        sample_size=len(durations),
        details=details,
    )


def _missing_entry() -> metric.ScalarMetric:
    """A seat seen in the lookback with no result on the day."""
    return metric.ScalarMetric(
        value=None,
        source=SOURCE,
        unknown_reason=NO_RESULTS_REASON,
        details={"lookback_days": LOOKBACK_DAYS},
    )


def collect(ctx: Any) -> dict[str, metric.ScalarMetric | metric.KeyedMetric]:
    """Publish ``gate_duration_seconds_by_seat`` for ``ctx.day`` (decision 23)."""
    counters, on_day, seen_before = _scan(_results_dir(ctx.project_id), ctx.day)
    details = {
        "unrecognised_files": counters["unrecognised_files"],
        "unattributed_results": counters["unattributed_results"],
        "unreadable_files": counters["unreadable_files"],
        "no_duration_n": sum(bucket["no_duration"] for bucket in on_day.values()),
    }
    entries = {seat: _day_entry(bucket) for seat, bucket in on_day.items()}
    for seat in seen_before - set(on_day):
        entries[seat] = _missing_entry()
    if not entries:
        return {
            METRIC_ID: metric.ScalarMetric(
                value=None, source=SOURCE, unknown_reason=NO_RESULTS_REASON, details=details
            )
        }
    return {
        METRIC_ID: metric.KeyedMetric(
            source=SOURCE, entries=dict(sorted(entries.items())), details=details
        )
    }
