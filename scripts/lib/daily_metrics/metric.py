"""The metric contract: ten ids, a scalar and a keyed record, and day status.

Missing is unknown, never 0. A record whose ``value`` is ``None`` carries an
``unknown_reason``; there is no path that turns ``None`` into ``0.0``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

REQUIRED: frozenset[str] = frozenset({
    "merges", "pr_open_to_merge_median_hours", "pr_open_to_merge_p75_hours",
    "rounds_per_pr", "ci_duration_seconds_median", "gate_duration_seconds_by_seat",
    "silence_windows_over_30m", "operator_wait_hours", "cost_per_merged_pr_usd",
    "merges_without_receipt_count",
})
KEYED: frozenset[str] = frozenset({"gate_duration_seconds_by_seat"})

# Decision 20: a day is `incomplete` while any record carries one of these.
TRANSIENT_REASONS: frozenset[str] = frozenset({"gh_failed", "truncated_fetch", "population_mismatch"})
TRANSIENT_REASON_PREFIXES: tuple[str, ...] = ("source_import_failed", "ci_run_unreadable_for_")
# Every other reason is permanent by decision 20's rule (it keeps a day final).
PERMANENT_REASONS: frozenset[str] = frozenset({
    "no_request_timestamp_in_approval_ledger", "ledger-is-falsifiable not merged",
    "owner_track_kosten-per-gemergede-pr_not_landed", "no_results_for_seat_on_day",
    "no_historical_readiness_snapshot", "no_ci_runs_on_any_head",
})


def is_transient_reason(reason: str | None) -> bool:
    """True when a non-final reason makes the day `incomplete` (decision 20)."""
    if not reason:
        return False
    return reason in TRANSIENT_REASONS or reason.startswith(TRANSIENT_REASON_PREFIXES)


@dataclass(frozen=True)
class ScalarMetric:
    """One measured number, or ``None`` with a reason. Never a silent 0.0."""

    value: float | None
    source: str = ""
    baseline_7d: float | None = None
    unknown_reason: str | None = None
    sample_size: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("ScalarMetric requires a non-empty source")
        if self.value is None and not (self.unknown_reason or "").strip():
            raise ValueError("ScalarMetric value is None without an unknown_reason")
        if self.value is not None and self.unknown_reason is not None:
            raise ValueError("ScalarMetric carries a value and an unknown_reason")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "scalar", "value": self.value, "source": self.source,
            "baseline_7d": self.baseline_7d, "unknown_reason": self.unknown_reason,
            "sample_size": self.sample_size, "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ScalarMetric:
        return cls(
            value=data.get("value"),
            source=data.get("source", ""),
            baseline_7d=data.get("baseline_7d"),
            unknown_reason=data.get("unknown_reason"),
            sample_size=data.get("sample_size", 0),
            details=dict(data.get("details") or {}),
        )


@dataclass(frozen=True)
class KeyedMetric:
    """A per-key collection (decision 23). It has no own ``value``."""

    source: str = ""
    entries: dict[str, ScalarMetric] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("KeyedMetric requires a non-empty source")
        if not self.entries:
            raise ValueError("KeyedMetric requires at least one entry")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "keyed", "source": self.source,
            "entries": {key: entry.to_dict() for key, entry in self.entries.items()},
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> KeyedMetric:
        return cls(
            source=data.get("source", ""),
            entries={k: ScalarMetric.from_dict(v) for k, v in (data.get("entries") or {}).items()},
            details=dict(data.get("details") or {}),
        )


def _record_reasons(record: Any) -> list[str | None] | None:
    """The unknown reasons inside one record; ``None`` when its shape is unknown."""
    if isinstance(record, KeyedMetric):
        return [entry.unknown_reason for entry in record.entries.values()]
    if isinstance(record, ScalarMetric):
        return [record.unknown_reason]
    if isinstance(record, Mapping):
        if record.get("kind") == "keyed" or "entries" in record:
            entries = record.get("entries") or {}
            if not entries or not all(isinstance(entry, Mapping) for entry in entries.values()):
                return None
            return [entry.get("unknown_reason") for entry in entries.values()]
        return [record.get("unknown_reason")]
    return None


def day_status(metrics: Mapping[str, Any] | None) -> str:
    """``incomplete`` as soon as one record carries a transient reason, else ``final``."""
    if not isinstance(metrics, Mapping):
        return "incomplete"  # fail-closed: unreadable evidence is not a final day
    for record in metrics.values():
        reasons = _record_reasons(record)
        if reasons is None:
            return "incomplete"  # fail-closed: an unknown record shape is not a pass
        if any(is_transient_reason(reason) for reason in reasons):
            return "incomplete"
    return "final"
