"""The 7-day baseline: pooled raw samples for quantiles, day means for counts.

A baseline is never 0 because a day is missing. Only ``final`` days count
(decision 5); fewer than ``MIN_FINAL_DAYS`` usable days yields ``None`` with
``insufficient_baseline_days``. Quantile metrics pool the raw ``details.samples``
of the window, because a mean of daily medians is not a median.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .metric import KEYED, REQUIRED, ScalarMetric

KIND: Mapping[str, str] = {
    "pr_open_to_merge_median_hours": "quantile",
    "pr_open_to_merge_p75_hours": "quantile",
    "rounds_per_pr": "quantile",
    "ci_duration_seconds_median": "quantile",
    "gate_duration_seconds_by_seat": "quantile",
    "merges": "mean",
    "silence_windows_over_30m": "mean",
    "merges_without_receipt_count": "mean",
    "operator_wait_hours": "mean",
    "cost_per_merged_pr_usd": "mean",
}
assert set(KIND) == REQUIRED, "KIND must cover exactly the required metric ids"

_QUANTILE_Q: Mapping[str, float] = {
    "pr_open_to_merge_median_hours": 0.5,
    "pr_open_to_merge_p75_hours": 0.75,
    "rounds_per_pr": 0.5,
    "ci_duration_seconds_median": 0.5,
    "gate_duration_seconds_by_seat": 0.5,
}

WINDOW_DAYS = 7
MIN_FINAL_DAYS = 4
MAX_SAMPLES_PER_DAY = 400
INSUFFICIENT = "insufficient_baseline_days"


@dataclass(frozen=True)
class _UsedDay:
    day: date
    value: float
    samples: tuple[float, ...] = ()
    truncated: bool = False


def quantile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (``inclusive`` semantics).

    ``q=0.5`` equals :func:`statistics.median`; ``q=0.75`` equals the last
    element of ``statistics.quantiles(values, n=4, method="inclusive")``.
    """
    data = [float(value) for value in values]
    if not data:
        raise ValueError("quantile requires a non-empty sequence")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be within [0, 1]")
    ordered = sorted(data)
    position = q * (len(ordered) - 1)
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _window(day: date) -> list[date]:
    return [day - timedelta(days=offset) for offset in range(WINDOW_DAYS, 0, -1)]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _samples(record: Mapping[str, Any]) -> tuple[tuple[float, ...], bool]:
    details = record.get("details")
    samples = details.get("samples") if isinstance(details, Mapping) else None
    if not isinstance(samples, (list, tuple)) or not samples:
        return (), False
    if not all(_is_number(sample) for sample in samples):
        return (), False
    return tuple(float(sample) for sample in samples), details.get("samples_truncated") is True


def _lookup(metric_id: str, doc: Mapping[str, Any], key: str | None) -> Mapping[str, Any] | None:
    metrics = doc.get("metrics")
    record = metrics.get(metric_id) if isinstance(metrics, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    if key is None:
        return record
    entries = record.get("entries")
    entry = entries.get(key) if isinstance(entries, Mapping) else None
    return entry if isinstance(entry, Mapping) else None


def _scan(
    metric_id: str, day: date, days: Mapping[date, Mapping], key: str | None
) -> tuple[str, list[_UsedDay], dict[str, str]]:
    kind = KIND[metric_id]
    used: list[_UsedDay] = []
    excluded: dict[str, str] = {}
    for window_day in _window(day):
        stamp = window_day.isoformat()
        doc = days.get(window_day)
        if not isinstance(doc, Mapping):
            excluded[stamp] = "missing_day"
            continue
        if doc.get("status") != "final":
            excluded[stamp] = "not_final"
            continue
        record = _lookup(metric_id, doc, key)
        if record is None:
            excluded[stamp] = "no_entry_for_key" if key is not None else "no_record"
            continue
        value = record.get("value")
        if not _is_number(value):
            excluded[stamp] = "value_unknown"
            continue
        samples: tuple[float, ...] = ()
        truncated = False
        if kind == "quantile":
            samples, truncated = _samples(record)
            if not samples:
                excluded[stamp] = "no_samples"
                continue
        used.append(_UsedDay(window_day, float(value), samples, truncated))
    return kind, used, excluded


def _validate(metric_id: str, key: str | None) -> None:
    if metric_id not in KIND:
        raise ValueError(f"unknown metric id: {metric_id!r}")
    if metric_id in KEYED and not key:
        raise ValueError(f"{metric_id} is keyed and requires a key")
    if key is not None and metric_id not in KEYED:
        raise ValueError(f"{metric_id} is scalar and takes no key")


def baseline(
    metric_id: str, day: date, days: Mapping[date, Mapping], key: str | None = None
) -> ScalarMetric:
    """The 7-day baseline ending the day before ``day``; ``day`` never counts."""
    _validate(metric_id, key)
    kind, used, excluded = _scan(metric_id, day, days, key)
    details: dict[str, Any] = {
        "window": {"from": (day - timedelta(days=WINDOW_DAYS)).isoformat(),
                   "to": (day - timedelta(days=1)).isoformat()},
        "used_days": [item.day.isoformat() for item in used],
        "excluded_days": excluded,
        "kind": kind,
    }
    source = f"baseline:{metric_id}" + (f":{key}" if key is not None else "")
    if kind == "mean":
        if len(used) < MIN_FINAL_DAYS:
            return ScalarMetric(None, source=source, unknown_reason=INSUFFICIENT,
                                sample_size=len(used), details=details)
        return ScalarMetric(statistics.fmean([item.value for item in used]), source=source,
                            sample_size=len(used), details=details)
    pooled = [sample for item in used for sample in item.samples[:MAX_SAMPLES_PER_DAY]]
    truncated_days = [item.day.isoformat() for item in used
                      if item.truncated or len(item.samples) > MAX_SAMPLES_PER_DAY]
    details["samples_truncated"] = bool(truncated_days)
    details["samples_truncated_days"] = truncated_days
    if len(used) < MIN_FINAL_DAYS:
        return ScalarMetric(None, source=source, unknown_reason=INSUFFICIENT,
                            sample_size=len(pooled), details=details)
    return ScalarMetric(quantile(pooled, _QUANTILE_Q[metric_id]), source=source,
                        sample_size=len(pooled), details=details)


def keyed_baselines(
    metric_id: str, day: date, days: Mapping[date, Mapping]
) -> dict[str, ScalarMetric]:
    """One baseline per key seen in any window day, sorted by key."""
    if metric_id not in KEYED:
        raise ValueError(f"{metric_id} is not a keyed metric")
    keys: set[str] = set()
    for window_day in _window(day):
        doc = days.get(window_day)
        metrics = doc.get("metrics") if isinstance(doc, Mapping) else None
        record = metrics.get(metric_id) if isinstance(metrics, Mapping) else None
        entries = record.get("entries") if isinstance(record, Mapping) else None
        if isinstance(entries, Mapping):
            keys.update(name for name in entries if isinstance(name, str))
    return {name: baseline(metric_id, day, days, key=name) for name in sorted(keys)}


def median_of_daily_medians(
    metric_id: str, day: date, days: Mapping[date, Mapping]
) -> float | None:
    """The median of the used days' own values — a contrast, never a baseline.

    It exists so the test can show a mean-of-medians differs from the pooled
    quantile; :func:`baseline` never calls it.
    """
    if metric_id not in KIND:
        raise ValueError(f"unknown metric id: {metric_id!r}")
    if KIND[metric_id] != "quantile":
        raise ValueError(f"{metric_id} is not a quantile metric")
    _, used, _ = _scan(metric_id, day, days, None)
    return statistics.median([item.value for item in used]) if used else None
