"""The verdict: an approved proposal judged on its seven post days (decision 26).

An approval freezes the baseline and names the approval day ``a`` (PR-12a). The
verdict pools ``a+1`` to ``a+7`` with :func:`daily_metrics.baseline.baseline`,
so a quantile pools the raw samples and a count averages the day values.

``unknown`` never reads as ``not_helped``: a ``None`` frozen baseline or post
value, fewer than five ``final`` post days, a pooled ``n`` below the rule's
minimum, a ``schema`` missing on either side (absent or ``None``) or a changed
``schema`` all yield ``undetermined`` with a reason; only enough data on the same
recorded metric version yields ``helped`` or ``not_helped`` -- a worsening
included. A schema nobody recorded is not evidence that the schema stayed the
same, so it can never produce a definitive verdict.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from .baseline import baseline
from .proposal_rules import RULES

HELPED = "helped"
NOT_HELPED = "not_helped"
UNDETERMINED = "undetermined"

BASELINE_UNKNOWN = "baseline_unknown"
POST_VALUE_UNKNOWN = "post_value_unknown"
TOO_FEW_FINAL_DAYS = "too_few_final_days"
SAMPLE_TOO_SMALL = "sample_too_small"
SCHEMA_MISSING = "schema_missing"
SCHEMA_CHANGED = "schema_changed"

LOWER = "lower"
POST_DAYS = 7
MIN_FINAL_DAYS = 5
MIN_IMPROVEMENT = 0.10


def approval_day(approved: Mapping[str, Any]) -> date:
    """The approval day ``a`` the approved event records; unreadable is a ``ValueError``."""
    value = approved.get("approval_day")
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"approved event has no usable approval_day: {value!r}")


def window(approved: Mapping[str, Any]) -> list[date]:
    """The seven post days ``a+1`` to ``a+7``, oldest first."""
    return [approval_day(approved) + timedelta(days=offset)
            for offset in range(1, POST_DAYS + 1)]


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def frozen_value(approved: Mapping[str, Any]) -> Any:
    """The baseline value frozen into the approved event; never recomputed here."""
    frozen = approved.get("frozen_baseline")
    return frozen.get("value") if isinstance(frozen, Mapping) else frozen


def _final_days(approved: Mapping[str, Any], post_days: Mapping[date, Mapping]) -> list[date]:
    """The post days whose document exists and is ``final``; anything else is not."""
    return [day for day in window(approved)
            if isinstance(post_days.get(day), Mapping)
            and post_days[day].get("status") == "final"]


def _schema_known(document: Mapping[str, Any]) -> bool:
    """A schema is evidence only when the document recorded one; absent or ``None`` is not."""
    return document.get("schema") is not None


def _schema_unknown(approved: Mapping[str, Any], finals: list[date],
                    post_days: Mapping[date, Mapping]) -> bool:
    """True when either the approval or any final post day has no recorded schema."""
    return not _schema_known(approved) or any(
        not _schema_known(post_days[day]) for day in finals)


def judge(approved: Mapping[str, Any], post_days: Mapping[date, Mapping]) -> dict[str, Any]:
    """Judge one approved event over ``a+1`` to ``a+7``; see the module doc."""
    rule = RULES.get(approved.get("metric_id"))
    if rule is None:
        raise ValueError(f"no rule for metric id: {approved.get('metric_id')!r}")
    approval = approval_day(approved)
    post = baseline(approved["metric_id"], approval + timedelta(days=POST_DAYS + 1),
                    post_days, key=approved.get("seat"))
    finals = _final_days(approved, post_days)
    frozen = _number(frozen_value(approved))
    post_value = _number(post.value)
    result: dict[str, Any] = {
        "verdict": UNDETERMINED, "reason": None, "post_value": post.value,
        "post_n": post.sample_size, "final_days": len(finals), "improvement": None,
    }
    # Decision 26: the pooled minimum is min_baseline_n when set, else min_day_n.
    minimum = rule.min_baseline_n if rule.min_baseline_n is not None else rule.min_day_n
    if frozen is None:
        reason = BASELINE_UNKNOWN
    elif post_value is None:
        reason = POST_VALUE_UNKNOWN
    elif len(finals) < MIN_FINAL_DAYS:
        reason = TOO_FEW_FINAL_DAYS
    elif minimum is not None and post.sample_size < minimum:
        reason = SAMPLE_TOO_SMALL
    elif _schema_unknown(approved, finals, post_days):
        reason = SCHEMA_MISSING
    elif any(post_days[day].get("schema") != approved.get("schema") for day in finals):
        reason = SCHEMA_CHANGED
    else:
        reason = None
    result["reason"] = reason
    if reason is not None:
        return result
    improvement = frozen - post_value if rule.direction == LOWER else post_value - frozen
    result["improvement"] = improvement
    floor = rule.difference if rule.difference is not None else 0.0
    helped = improvement >= MIN_IMPROVEMENT * frozen and improvement >= floor
    result["verdict"] = HELPED if helped else NOT_HELPED
    return result
