"""The proposal rule table (decision 26): a deterministic candidate per metric.

A candidate exists only when every condition of its rule holds on the newest
final day, measured against the 7-day baseline. Nothing here reads a clock, a
file or the environment: :func:`evaluate` is a pure function of the day and the
window documents. Every metric key is either a candidate or carries the reason
it was skipped; a missing value or baseline is never a pass.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from types import MappingProxyType
from typing import Any

from .baseline import baseline, keyed_baselines
from .metric import KEYED, REQUIRED, ScalarMetric

_LOWER = "lower"
_HIGHER = "higher"
_EPSILON = 1e-9

NO_RULE = "no_rule"
VALUE_UNKNOWN = "value_unknown"
BASELINE_UNKNOWN = "baseline_unknown"
SAMPLE_TOO_SMALL = "sample_too_small"
BELOW_TRIGGER = "below_trigger"
DAY_NOT_FINAL = "day_not_final"


@dataclass(frozen=True)
class Rule:
    """One row of decision 26: direction, trigger and sample minimums."""

    direction: str
    ratio: float | None = None
    difference: float | None = None
    min_day_n: int | None = None
    min_baseline_n: int | None = None
    min_baseline_value: float | None = None
    min_value: float | None = None

    def __post_init__(self) -> None:
        if self.direction not in (_LOWER, _HIGHER):
            raise ValueError(f"unknown direction: {self.direction!r}")


#: Decision 26. ``operator_wait_hours``, ``cost_per_merged_pr_usd`` and
#: ``merges_without_receipt_count`` have no row: their source is unknown.
RULES: Mapping[str, Rule] = MappingProxyType({
    "pr_open_to_merge_median_hours": Rule(
        _LOWER, ratio=1.5, difference=0.5, min_day_n=8, min_baseline_n=30),
    "pr_open_to_merge_p75_hours": Rule(
        _LOWER, ratio=1.5, difference=1.0, min_day_n=8, min_baseline_n=30),
    "rounds_per_pr": Rule(_LOWER, ratio=1.25, difference=0.5, min_day_n=8),
    "ci_duration_seconds_median": Rule(
        _LOWER, ratio=1.25, difference=120.0, min_day_n=8),
    "gate_duration_seconds_by_seat": Rule(
        _LOWER, ratio=1.5, difference=60.0, min_day_n=5),
    "silence_windows_over_30m": Rule(_LOWER, difference=2.0, min_value=2.0),
    "merges": Rule(_HIGHER, ratio=0.5, min_baseline_value=10.0),
})


def rule_version(rules: Mapping[str, Rule] | None = None) -> str:
    """The first 12 hex characters of the sha256 of the serialised table."""
    table = RULES if rules is None else rules
    payload = json.dumps(
        {metric_id: asdict(rule) for metric_id, rule in table.items()},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


#: Changes whenever a threshold changes; PR-11b puts it in ``proposal_id``.
RULE_VERSION = rule_version()


@dataclass(frozen=True)
class Candidate:
    """One metric key that clears every condition of its rule."""

    key: str
    metric_id: str
    seat: str | None
    value: float
    baseline: float
    baseline_n: int
    day_n: int
    exceedance: float


@dataclass(frozen=True)
class Evaluation:
    """The candidates of one final day plus the skip reason of every other key."""

    candidates: tuple[Candidate, ...]
    skipped: Mapping[str, str]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _record(metric_id: str, doc: Mapping[str, Any], seat: str | None) -> Mapping[str, Any] | None:
    metrics = doc.get("metrics")
    record = metrics.get(metric_id) if isinstance(metrics, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    if seat is None:
        return record
    entries = record.get("entries")
    entry = entries.get(seat) if isinstance(entries, Mapping) else None
    return entry if isinstance(entry, Mapping) else None


def _sample_size(record: Mapping[str, Any] | None) -> int:
    size = record.get("sample_size") if isinstance(record, Mapping) else None
    return size if isinstance(size, int) and not isinstance(size, bool) else 0


def _day_seats(doc: Any, metric_id: str) -> set[str]:
    metrics = doc.get("metrics") if isinstance(doc, Mapping) else None
    record = metrics.get(metric_id) if isinstance(metrics, Mapping) else None
    entries = record.get("entries") if isinstance(record, Mapping) else None
    return {name for name in entries if isinstance(name, str)} if isinstance(entries, Mapping) else set()


def _keys(day: date, days: Mapping[date, Mapping], doc: Any) -> dict[str, tuple[str, str | None]]:
    """Every required metric key, plus one key per seat seen on the day or in the window."""
    keys = {metric_id: (metric_id, None) for metric_id in REQUIRED}
    for metric_id in sorted(KEYED):
        seats = set(keyed_baselines(metric_id, day, days)) | _day_seats(doc, metric_id)
        for seat in sorted(seats):
            keys[f"{metric_id}:{seat}"] = (metric_id, seat)
    return keys


def _minimums_ok(rule: Rule, base: ScalarMetric, day_n: int) -> bool:
    if rule.min_day_n is not None and day_n < rule.min_day_n:
        return False
    if rule.min_baseline_n is not None and base.sample_size < rule.min_baseline_n:
        return False
    if rule.min_baseline_value is not None and base.value < rule.min_baseline_value:
        return False
    return True


def _trigger_ok(rule: Rule, value: float, base: float) -> bool:
    if rule.ratio is not None:
        if rule.direction == _LOWER and value < rule.ratio * base:
            return False
        if rule.direction == _HIGHER and value > rule.ratio * base:
            return False
    if rule.difference is not None:
        delta = value - base if rule.direction == _LOWER else base - value
        if delta < rule.difference:
            return False
    if rule.min_value is not None and value < rule.min_value:
        return False
    return True


def _exceedance(rule: Rule, value: float, base: float) -> float:
    floor = max(base, _EPSILON)
    return (value - base) / floor if rule.direction == _LOWER else (base - value) / floor


def _baseline(
    metric_id: str, seat: str | None, day: date, days: Mapping[date, Mapping]
) -> ScalarMetric:
    return baseline(metric_id, day, days) if seat is None else baseline(
        metric_id, day, days, key=seat)


def evaluate(day: date, days: Mapping[date, Mapping]) -> Evaluation:
    """The candidates of ``day`` and the skip reason of every other rule key."""
    doc = days.get(day)
    keys = _keys(day, days, doc)
    if not (isinstance(doc, Mapping) and doc.get("status") == "final"):
        return Evaluation((), MappingProxyType({key: DAY_NOT_FINAL for key in keys}))
    candidates: list[Candidate] = []
    skipped: dict[str, str] = {}
    for key in sorted(keys):
        metric_id, seat = keys[key]
        rule = RULES.get(metric_id)
        if rule is None:
            skipped[key] = NO_RULE
            continue
        record = _record(metric_id, doc, seat)
        value = record.get("value") if isinstance(record, Mapping) else None
        if not _is_number(value):
            skipped[key] = VALUE_UNKNOWN
            continue
        base = _baseline(metric_id, seat, day, days)
        if not _is_number(base.value):
            skipped[key] = BASELINE_UNKNOWN
            continue
        day_n = _sample_size(record)
        if not _minimums_ok(rule, base, day_n):
            skipped[key] = SAMPLE_TOO_SMALL
            continue
        if not _trigger_ok(rule, float(value), float(base.value)):
            skipped[key] = BELOW_TRIGGER
            continue
        candidates.append(Candidate(
            key=key, metric_id=metric_id, seat=seat, value=float(value),
            baseline=float(base.value), baseline_n=base.sample_size, day_n=day_n,
            exceedance=_exceedance(rule, float(value), float(base.value))))
    candidates.sort(key=lambda candidate: (-candidate.exceedance, candidate.key))
    return Evaluation(tuple(candidates), MappingProxyType(skipped))
