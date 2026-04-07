#!/usr/bin/env python3
"""
VNX Recommendation Metrics — Dispatch-level metric computation and usefulness summaries.

Extracted from recommendation_tracker.py to keep module size manageable.
Provides standalone functions for computing dispatch metrics, usefulness
summaries, and full usefulness reports.

Governance:
  G-R7: Recommendation adoption is measured before becoming policy
  Advisory-only: no automatic policy mutation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from runtime_coordination import (
    _now_utc,
    get_connection,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_RECOMMENDATION_CLASSES = frozenset({
    "prompt_patch",
    "routing_preference",
    "guardrail_adjustment",
    "process_improvement",
})

# Metric names tracked for each recommendation
METRIC_NAMES = frozenset({
    "first_pass_success_rate",
    "redispatch_rate",
    "open_item_carry_over",
    "ack_timeout_rate",
    "repeated_failure_rate",
    "operator_override_rate",
})

# Comparison status values
COMPARISON_PENDING = "pending"
COMPARISON_COMPUTED = "computed"
COMPARISON_INSUFFICIENT = "insufficient_data"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InvalidRecommendationClassError(Exception):
    """Raised for unknown recommendation classes."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OutcomeMetric:
    """A single before/after metric comparison for a recommendation."""
    recommendation_id: str
    metric_name: str
    baseline_value: Optional[float]
    baseline_sample_size: int
    outcome_value: Optional[float]
    outcome_sample_size: int
    delta: Optional[float]
    direction: Optional[str]
    comparison_status: str
    computed_at: str

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> OutcomeMetric:
        return cls(
            recommendation_id=row["recommendation_id"],
            metric_name=row["metric_name"],
            baseline_value=row.get("baseline_value"),
            baseline_sample_size=int(row.get("baseline_sample_size", 0)),
            outcome_value=row.get("outcome_value"),
            outcome_sample_size=int(row.get("outcome_sample_size", 0)),
            delta=row.get("delta"),
            direction=row.get("direction"),
            comparison_status=row.get("comparison_status", COMPARISON_PENDING),
            computed_at=row.get("computed_at", ""),
        )


@dataclass
class UsefulnessSummary:
    """Aggregated usefulness summary for a recommendation class."""
    recommendation_class: str
    total_proposed: int
    total_accepted: int
    total_rejected: int
    total_expired: int
    metrics: Dict[str, Dict[str, Any]]  # metric_name -> {avg_delta, improved_count, total_measured}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recommendation_class": self.recommendation_class,
            "total_proposed": self.total_proposed,
            "total_accepted": self.total_accepted,
            "total_rejected": self.total_rejected,
            "total_expired": self.total_expired,
            "acceptance_rate": (
                self.total_accepted / self.total_proposed
                if self.total_proposed > 0 else 0.0
            ),
            "metrics": self.metrics,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_recommendation_class(rec_class: str) -> None:
    if rec_class not in VALID_RECOMMENDATION_CLASSES:
        raise InvalidRecommendationClassError(
            f"Unknown recommendation class: {rec_class!r}. "
            f"Valid: {sorted(VALID_RECOMMENDATION_CLASSES)}"
        )


# ---------------------------------------------------------------------------
# Standalone metric functions
# ---------------------------------------------------------------------------

def compute_dispatch_metrics(
    state_dir,
    *,
    window_start: str,
    window_end: str,
    scope_tags: Optional[List[str]] = None,
) -> Dict[str, float]:
    """Compute aggregate metrics from dispatches within a time window.

    Queries the dispatches and dispatch_attempts tables to derive:
      - first_pass_success_rate
      - redispatch_rate
      - ack_timeout_rate
      - repeated_failure_rate
      - operator_override_rate

    open_item_carry_over requires external input and is not computed here.

    Returns metric_name -> value dict.
    """
    metrics: Dict[str, float] = {}

    with get_connection(state_dir) as conn:
        # Total dispatches in window
        total_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM dispatches "
            "WHERE created_at >= ? AND created_at <= ?",
            (window_start, window_end),
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        if total == 0:
            return {m: 0.0 for m in METRIC_NAMES}

        # First-pass success: completed on attempt_count <= 1
        first_pass = conn.execute(
            "SELECT COUNT(*) as cnt FROM dispatches "
            "WHERE created_at >= ? AND created_at <= ? "
            "AND state = 'completed' AND attempt_count <= 1",
            (window_start, window_end),
        ).fetchone()
        metrics["first_pass_success_rate"] = (
            first_pass["cnt"] / total if first_pass else 0.0
        )

        # Redispatch rate: dispatches that went through recovered state
        redispatched = conn.execute(
            """
            SELECT COUNT(DISTINCT ce.entity_id) as cnt
            FROM coordination_events ce
            JOIN dispatches d ON d.dispatch_id = ce.entity_id
            WHERE d.created_at >= ? AND d.created_at <= ?
              AND ce.entity_type = 'dispatch'
              AND ce.to_state = 'recovered'
            """,
            (window_start, window_end),
        ).fetchone()
        metrics["redispatch_rate"] = (
            redispatched["cnt"] / total if redispatched else 0.0
        )

        # Ack timeout rate: dispatches that reached timed_out state
        timed_out = conn.execute(
            """
            SELECT COUNT(DISTINCT ce.entity_id) as cnt
            FROM coordination_events ce
            JOIN dispatches d ON d.dispatch_id = ce.entity_id
            WHERE d.created_at >= ? AND d.created_at <= ?
              AND ce.entity_type = 'dispatch'
              AND ce.to_state = 'timed_out'
            """,
            (window_start, window_end),
        ).fetchone()
        metrics["ack_timeout_rate"] = (
            timed_out["cnt"] / total if timed_out else 0.0
        )

        # Repeated failure rate: dispatches with attempt_count > 1 that ended in failure
        repeated_fail = conn.execute(
            "SELECT COUNT(*) as cnt FROM dispatches "
            "WHERE created_at >= ? AND created_at <= ? "
            "AND attempt_count > 1 "
            "AND state IN ('failed_delivery', 'dead_letter')",
            (window_start, window_end),
        ).fetchone()
        metrics["repeated_failure_rate"] = (
            repeated_fail["cnt"] / total if repeated_fail else 0.0
        )

        # Operator override rate: routing_decision events with override metadata
        overrides = conn.execute(
            """
            SELECT COUNT(DISTINCT ce.entity_id) as cnt
            FROM coordination_events ce
            JOIN dispatches d ON d.dispatch_id = ce.entity_id
            WHERE d.created_at >= ? AND d.created_at <= ?
              AND ce.event_type = 'routing_decision'
              AND ce.metadata_json LIKE '%"override"%'
            """,
            (window_start, window_end),
        ).fetchone()
        metrics["operator_override_rate"] = (
            overrides["cnt"] / total if overrides else 0.0
        )

        # open_item_carry_over defaults to 0 — requires external input
        metrics["open_item_carry_over"] = 0.0

    return metrics


def summarize_usefulness(
    state_dir,
    recommendation_class: str,
) -> UsefulnessSummary:
    """Generate an operator-readable usefulness summary for a recommendation class.

    Advisory-only: this summary is for operator review, not auto-policy mutation.
    """
    _validate_recommendation_class(recommendation_class)

    with get_connection(state_dir) as conn:
        # Count by state
        counts = {}
        for state in ("proposed", "accepted", "rejected", "expired"):
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM recommendations "
                "WHERE recommendation_class = ? AND acceptance_state = ?",
                (recommendation_class, state),
            ).fetchone()
            counts[state] = row["cnt"] if row else 0

        # Aggregate metrics across accepted recommendations
        metric_agg: Dict[str, Dict[str, Any]] = {}
        for metric_name in sorted(METRIC_NAMES):
            rows = conn.execute(
                """
                SELECT ro.delta, ro.direction, ro.comparison_status
                FROM recommendation_outcomes ro
                JOIN recommendations r
                  ON r.recommendation_id = ro.recommendation_id
                WHERE r.recommendation_class = ?
                  AND ro.metric_name = ?
                  AND ro.comparison_status = ?
                """,
                (recommendation_class, metric_name, COMPARISON_COMPUTED),
            ).fetchall()

            if not rows:
                metric_agg[metric_name] = {
                    "avg_delta": None,
                    "improved_count": 0,
                    "degraded_count": 0,
                    "unchanged_count": 0,
                    "total_measured": 0,
                }
                continue

            deltas = [dict(r)["delta"] for r in rows if dict(r)["delta"] is not None]
            directions = [dict(r)["direction"] for r in rows]

            metric_agg[metric_name] = {
                "avg_delta": sum(deltas) / len(deltas) if deltas else None,
                "improved_count": sum(1 for d in directions if d == "improved"),
                "degraded_count": sum(1 for d in directions if d == "degraded"),
                "unchanged_count": sum(1 for d in directions if d == "unchanged"),
                "total_measured": len(rows),
            }

    return UsefulnessSummary(
        recommendation_class=recommendation_class,
        total_proposed=counts.get("proposed", 0) + counts.get("accepted", 0)
            + counts.get("rejected", 0) + counts.get("expired", 0),
        total_accepted=counts.get("accepted", 0),
        total_rejected=counts.get("rejected", 0),
        total_expired=counts.get("expired", 0),
        metrics=metric_agg,
    )


def export_usefulness_report(state_dir) -> Dict[str, Any]:
    """Export a full usefulness report across all recommendation classes.

    Returns a dict suitable for JSON serialization and operator review.
    Advisory-only: G-R7.
    """
    report: Dict[str, Any] = {
        "generated_at": _now_utc(),
        "advisory_only": True,
        "classes": {},
    }
    for rec_class in sorted(VALID_RECOMMENDATION_CLASSES):
        summary = summarize_usefulness(state_dir, rec_class)
        report["classes"][rec_class] = summary.to_dict()

    return report
