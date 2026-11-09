"""The cost-per-merged-PR source: unknown, with the day's numeric coverage.

Decision 10: this module never builds the merge/cost join itself. It reads
``events/provider_costs.ndjson`` through ``provider_costs._resolve_costs_path``
(import only, never a second copy of the resolution) and publishes
``cost_per_merged_pr_usd`` as ``value: None`` with the reason
``owner_track_kosten-per-gemergede-pr_not_landed``. The per-day numeric
coverage rides in ``details`` so the gap stays measurable while it lasts, and
no record is ever summed into ``value``.

The reason changes when the owning track ``kosten-per-gemergede-pr`` lands its
join: this module then reads that result instead of publishing the coverage.
``api_equivalent_usd`` is a shadow price and is never read.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import provider_costs

from daily_metrics import metric

SOURCE = "daily_metrics.costs"
UNKNOWN_REASON = "owner_track_kosten-per-gemergede-pr_not_landed"


def _record_day(value: Any) -> date | None:
    """The UTC day of a record's ``recorded_at``; ``None`` when it does not parse."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)
    return stamp.date()


def _is_numeric(value: Any) -> bool:
    """A real, finite number; ``bool`` and ``NaN`` are not a cost."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _coverage(path: Path, day: date) -> dict[str, Any]:
    """Count the day's records and how many carry a numeric top-level ``cost_usd``."""
    records = 0
    without = 0
    read_error: str | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError) as exc:
                    read_error = read_error or type(exc).__name__
                    continue
                if not isinstance(record, dict):
                    read_error = read_error or "TypeError"
                    continue
                if _record_day(record.get("recorded_at")) != day:
                    continue
                records += 1
                if not _is_numeric(record.get("cost_usd")):
                    without += 1
    except FileNotFoundError:
        return {"records": 0, "without_cost_usd": 0, "numeric_coverage": None}
    except OSError as exc:
        read_error = read_error or type(exc).__name__
    except UnicodeDecodeError as exc:
        read_error = read_error or type(exc).__name__

    details: dict[str, Any] = {
        "records": records,
        "without_cost_usd": without,
        "numeric_coverage": None if records == 0 else (records - without) / records,
    }
    if read_error is not None:
        details["read_error"] = read_error
    return details


def collect(ctx: Any) -> dict[str, metric.ScalarMetric]:
    """Publish ``cost_per_merged_pr_usd`` as unknown with the day's coverage."""
    details = _coverage(provider_costs._resolve_costs_path(), ctx.day)
    return {
        "cost_per_merged_pr_usd": metric.ScalarMetric(
            value=None,
            source=SOURCE,
            unknown_reason=UNKNOWN_REASON,
            details=details,
        )
    }
