#!/usr/bin/env python3
"""
VNX Recommendation Tracker — Usefulness metrics and acceptance loop.

Implements the PR-4 measurement loop so VNX can track whether a prompt patch,
routing preference, guardrail adjustment, or process improvement recommendation
actually helped.

Metrics tracked per recommendation class:
  - first_pass_success_rate: dispatches completing on first attempt
  - redispatch_rate: dispatches requiring re-dispatch after failure
  - open_item_carry_over: open items carried from one dispatch to the next
  - ack_timeout_rate: dispatches timing out before acknowledgment
  - repeated_failure_rate: dispatches failing more than once
  - operator_override_rate: dispatches where the operator overrode routing

Governance:
  G-R7: Recommendation adoption is measured before becoming policy
  G-R8: All recommendation decisions emit coordination_events
  Advisory-only: no automatic policy mutation
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import (
    _append_event,
    _now_utc,
    get_connection,
)
from recommendation_metrics import (
    COMPARISON_COMPUTED,
    COMPARISON_INSUFFICIENT,
    COMPARISON_PENDING,
    InvalidRecommendationClassError,
    METRIC_NAMES,
    OutcomeMetric,
    UsefulnessSummary,
    VALID_RECOMMENDATION_CLASSES,
    _validate_recommendation_class,
    compute_dispatch_metrics as _compute_dispatch_metrics,
    export_usefulness_report as _export_usefulness_report,
    summarize_usefulness as _summarize_usefulness,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_ACCEPTANCE_STATES = frozenset({
    "proposed",
    "accepted",
    "rejected",
    "expired",
    "superseded",
})

# Allowed transitions: {from_state: set_of_allowed_to_states}
ACCEPTANCE_TRANSITIONS: Dict[str, frozenset] = {
    "proposed":   frozenset({"accepted", "rejected", "expired", "superseded"}),
    "accepted":   frozenset({"superseded"}),
    "rejected":   frozenset(),
    "expired":    frozenset(),
    "superseded": frozenset(),
}

# Default outcome window duration (days) after acceptance
DEFAULT_OUTCOME_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RecommendationError(Exception):
    """Base error for recommendation operations."""


class RecommendationNotFoundError(RecommendationError):
    """Raised when a recommendation_id is not found."""


class InvalidAcceptanceTransitionError(RecommendationError):
    """Raised when a state transition is not permitted."""


class DuplicateRecommendationError(RecommendationError):
    """Raised when proposing a recommendation with an existing ID."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Recommendation:
    """A single operator-facing recommendation."""
    recommendation_id: str
    recommendation_class: str
    title: str
    description: str
    evidence_summary: str
    confidence: float
    scope_tags: List[str]
    acceptance_state: str
    proposed_at: str
    accepted_at: Optional[str] = None
    rejected_at: Optional[str] = None
    rejection_reason: Optional[str] = None
    expired_at: Optional[str] = None
    superseded_by: Optional[str] = None
    outcome_window_start: Optional[str] = None
    outcome_window_end: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> Recommendation:
        scope_tags = row.get("scope_tags_json", "[]")
        if isinstance(scope_tags, str):
            scope_tags = json.loads(scope_tags)
        meta = row.get("metadata_json", "{}")
        if isinstance(meta, str):
            meta = json.loads(meta)
        return cls(
            recommendation_id=row["recommendation_id"],
            recommendation_class=row["recommendation_class"],
            title=row["title"],
            description=row["description"],
            evidence_summary=row["evidence_summary"],
            confidence=float(row.get("confidence", 0.0)),
            scope_tags=scope_tags,
            acceptance_state=row["acceptance_state"],
            proposed_at=row.get("proposed_at", ""),
            accepted_at=row.get("accepted_at"),
            rejected_at=row.get("rejected_at"),
            rejection_reason=row.get("rejection_reason"),
            expired_at=row.get("expired_at"),
            superseded_by=row.get("superseded_by"),
            outcome_window_start=row.get("outcome_window_start"),
            outcome_window_end=row.get("outcome_window_end"),
            metadata=meta,
        )

    @property
    def is_terminal(self) -> bool:
        return self.acceptance_state in ("rejected", "expired", "superseded")

    @property
    def is_accepted(self) -> bool:
        return self.acceptance_state == "accepted"

    @property
    def has_outcome_window(self) -> bool:
        return self.outcome_window_start is not None and self.outcome_window_end is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "recommendation_class": self.recommendation_class,
            "title": self.title,
            "description": self.description,
            "evidence_summary": self.evidence_summary,
            "confidence": self.confidence,
            "scope_tags": self.scope_tags,
            "acceptance_state": self.acceptance_state,
            "proposed_at": self.proposed_at,
            "accepted_at": self.accepted_at,
            "rejected_at": self.rejected_at,
            "rejection_reason": self.rejection_reason,
            "outcome_window_start": self.outcome_window_start,
            "outcome_window_end": self.outcome_window_end,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return str(uuid.uuid4())


def _validate_acceptance_transition(from_state: str, to_state: str) -> None:
    allowed = ACCEPTANCE_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidAcceptanceTransitionError(
            f"Recommendation transition {from_state!r} -> {to_state!r} not permitted. "
            f"Allowed from {from_state!r}: {sorted(allowed) or 'none (terminal state)'}"
        )


# ---------------------------------------------------------------------------
# RecommendationTracker
# ---------------------------------------------------------------------------

class RecommendationTracker:
    """Manages recommendation lifecycle, acceptance tracking, and usefulness metrics.

    Advisory-only: computes and stores metrics but never auto-applies recommendations.

    Args:
        state_dir: Directory containing runtime_coordination.db.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self._state_dir = Path(state_dir)

    def propose(
        self,
        recommendation_class: str,
        title: str,
        description: str,
        evidence_summary: str,
        confidence: float,
        *,
        scope_tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        recommendation_id: Optional[str] = None,
        actor: str = "intelligence",
    ) -> Recommendation:
        """Propose a new recommendation. Returns the created Recommendation."""
        _validate_recommendation_class(recommendation_class)
        rec_id = recommendation_id or _new_id()
        scope_json = json.dumps(scope_tags or [])
        meta_json = json.dumps(metadata or {})
        now = _now_utc()

        with get_connection(self._state_dir) as conn:
            existing = conn.execute(
                "SELECT recommendation_id FROM recommendations WHERE recommendation_id = ?",
                (rec_id,),
            ).fetchone()
            if existing:
                raise DuplicateRecommendationError(
                    f"Recommendation already exists: {rec_id!r}"
                )

            conn.execute(
                """
                INSERT INTO recommendations
                    (recommendation_id, recommendation_class, title, description,
                     evidence_summary, confidence, scope_tags_json,
                     acceptance_state, proposed_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
                """,
                (rec_id, recommendation_class, title, description,
                 evidence_summary, confidence, scope_json, now, meta_json),
            )

            _append_event(
                conn,
                event_type="recommendation_proposed",
                entity_type="recommendation",
                entity_id=rec_id,
                to_state="proposed",
                actor=actor,
                reason=f"proposed {recommendation_class}: {title}",
                metadata={
                    "recommendation_class": recommendation_class,
                    "confidence": confidence,
                },
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?", (rec_id,)
            ).fetchone()

        return Recommendation.from_row(dict(row))

    def accept(
        self,
        recommendation_id: str,
        *,
        outcome_window_days: int = DEFAULT_OUTCOME_WINDOW_DAYS,
        actor: str = "operator",
    ) -> Recommendation:
        """Accept a recommendation and open its outcome measurement window."""
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
            if row is None:
                raise RecommendationNotFoundError(
                    f"Recommendation not found: {recommendation_id!r}"
                )

            from_state = row["acceptance_state"]
            _validate_acceptance_transition(from_state, "accepted")

            now = _now_utc()
            now_dt = datetime.now(timezone.utc)
            window_end = (now_dt + timedelta(days=outcome_window_days)).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            ) + "Z"

            conn.execute(
                """
                UPDATE recommendations
                SET acceptance_state = 'accepted',
                    accepted_at = ?,
                    outcome_window_start = ?,
                    outcome_window_end = ?
                WHERE recommendation_id = ?
                """,
                (now, now, window_end, recommendation_id),
            )

            # Initialize outcome metric rows for all tracked metrics
            for metric_name in sorted(METRIC_NAMES):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO recommendation_outcomes
                        (recommendation_id, metric_name, comparison_status)
                    VALUES (?, ?, ?)
                    """,
                    (recommendation_id, metric_name, COMPARISON_PENDING),
                )

            _append_event(
                conn,
                event_type="recommendation_accepted",
                entity_type="recommendation",
                entity_id=recommendation_id,
                from_state=from_state,
                to_state="accepted",
                actor=actor,
                reason=f"accepted with {outcome_window_days}-day outcome window",
                metadata={
                    "outcome_window_days": outcome_window_days,
                    "outcome_window_end": window_end,
                },
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()

        return Recommendation.from_row(dict(row))

    def reject(
        self,
        recommendation_id: str,
        *,
        reason: str = "",
        actor: str = "operator",
    ) -> Recommendation:
        """Reject a recommendation."""
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
            if row is None:
                raise RecommendationNotFoundError(
                    f"Recommendation not found: {recommendation_id!r}"
                )

            from_state = row["acceptance_state"]
            _validate_acceptance_transition(from_state, "rejected")

            now = _now_utc()
            conn.execute(
                """
                UPDATE recommendations
                SET acceptance_state = 'rejected',
                    rejected_at = ?,
                    rejection_reason = ?
                WHERE recommendation_id = ?
                """,
                (now, reason, recommendation_id),
            )

            _append_event(
                conn,
                event_type="recommendation_rejected",
                entity_type="recommendation",
                entity_id=recommendation_id,
                from_state=from_state,
                to_state="rejected",
                actor=actor,
                reason=reason or "rejected by operator",
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()

        return Recommendation.from_row(dict(row))

    def expire(
        self,
        recommendation_id: str,
        *,
        actor: str = "system",
    ) -> Recommendation:
        """Expire a recommendation that was not acted upon."""
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
            if row is None:
                raise RecommendationNotFoundError(
                    f"Recommendation not found: {recommendation_id!r}"
                )

            from_state = row["acceptance_state"]
            _validate_acceptance_transition(from_state, "expired")

            now = _now_utc()
            conn.execute(
                """
                UPDATE recommendations
                SET acceptance_state = 'expired', expired_at = ?
                WHERE recommendation_id = ?
                """,
                (now, recommendation_id),
            )

            _append_event(
                conn,
                event_type="recommendation_expired",
                entity_type="recommendation",
                entity_id=recommendation_id,
                from_state=from_state,
                to_state="expired",
                actor=actor,
                reason="recommendation expired without action",
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()

        return Recommendation.from_row(dict(row))

    def supersede(
        self,
        recommendation_id: str,
        superseded_by: str,
        *,
        actor: str = "intelligence",
    ) -> Recommendation:
        """Mark a recommendation as superseded by a newer one."""
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
            if row is None:
                raise RecommendationNotFoundError(
                    f"Recommendation not found: {recommendation_id!r}"
                )

            from_state = row["acceptance_state"]
            _validate_acceptance_transition(from_state, "superseded")

            conn.execute(
                """
                UPDATE recommendations
                SET acceptance_state = 'superseded', superseded_by = ?
                WHERE recommendation_id = ?
                """,
                (superseded_by, recommendation_id),
            )

            _append_event(
                conn,
                event_type="recommendation_superseded",
                entity_type="recommendation",
                entity_id=recommendation_id,
                from_state=from_state,
                to_state="superseded",
                actor=actor,
                reason=f"superseded by {superseded_by}",
                metadata={"superseded_by": superseded_by},
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()

        return Recommendation.from_row(dict(row))

    def get(self, recommendation_id: str) -> Optional[Recommendation]:
        """Return a single recommendation or None."""
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
        if row is None:
            return None
        return Recommendation.from_row(dict(row))

    def list_by_class(
        self,
        recommendation_class: str,
        *,
        state: Optional[str] = None,
    ) -> List[Recommendation]:
        """List recommendations by class, optionally filtered by acceptance state."""
        _validate_recommendation_class(recommendation_class)
        with get_connection(self._state_dir) as conn:
            if state:
                rows = conn.execute(
                    "SELECT * FROM recommendations "
                    "WHERE recommendation_class = ? AND acceptance_state = ? "
                    "ORDER BY proposed_at DESC",
                    (recommendation_class, state),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM recommendations "
                    "WHERE recommendation_class = ? ORDER BY proposed_at DESC",
                    (recommendation_class,),
                ).fetchall()
        return [Recommendation.from_row(dict(r)) for r in rows]

    def list_by_state(self, state: str) -> List[Recommendation]:
        """List all recommendations in a given acceptance state."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM recommendations WHERE acceptance_state = ? "
                "ORDER BY proposed_at DESC",
                (state,),
            ).fetchall()
        return [Recommendation.from_row(dict(r)) for r in rows]

    def list_pending_measurement(self) -> List[Recommendation]:
        """List accepted recommendations whose outcome window has ended but metrics are pending."""
        now = _now_utc()
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                """
                SELECT r.* FROM recommendations r
                WHERE r.acceptance_state = 'accepted'
                  AND r.outcome_window_end IS NOT NULL
                  AND r.outcome_window_end <= ?
                  AND EXISTS (
                      SELECT 1 FROM recommendation_outcomes ro
                      WHERE ro.recommendation_id = r.recommendation_id
                        AND ro.comparison_status = 'pending'
                  )
                ORDER BY r.outcome_window_end ASC
                """,
                (now,),
            ).fetchall()
        return [Recommendation.from_row(dict(r)) for r in rows]

    def record_baseline(
        self,
        recommendation_id: str,
        metric_name: str,
        value: float,
        sample_size: int,
    ) -> None:
        """Record the baseline (before) value for a metric."""
        if metric_name not in METRIC_NAMES:
            raise RecommendationError(f"Unknown metric: {metric_name!r}")

        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT recommendation_id FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
            if row is None:
                raise RecommendationNotFoundError(
                    f"Recommendation not found: {recommendation_id!r}"
                )

            conn.execute(
                """
                INSERT INTO recommendation_outcomes
                    (recommendation_id, metric_name, baseline_value,
                     baseline_sample_size, comparison_status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(recommendation_id, metric_name)
                DO UPDATE SET
                    baseline_value = excluded.baseline_value,
                    baseline_sample_size = excluded.baseline_sample_size,
                    computed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (recommendation_id, metric_name, value, sample_size, COMPARISON_PENDING),
            )
            conn.commit()

    def record_outcome(
        self,
        recommendation_id: str,
        metric_name: str,
        value: float,
        sample_size: int,
    ) -> OutcomeMetric:
        """Record the outcome (after) value and compute delta.

        Automatically computes delta and direction if baseline exists.
        """
        if metric_name not in METRIC_NAMES:
            raise RecommendationError(f"Unknown metric: {metric_name!r}")

        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT recommendation_id FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
            if row is None:
                raise RecommendationNotFoundError(
                    f"Recommendation not found: {recommendation_id!r}"
                )

            existing = conn.execute(
                "SELECT * FROM recommendation_outcomes "
                "WHERE recommendation_id = ? AND metric_name = ?",
                (recommendation_id, metric_name),
            ).fetchone()

            now = _now_utc()
            baseline_val = dict(existing).get("baseline_value") if existing else None

            delta = None
            direction = None
            status = COMPARISON_COMPUTED

            if baseline_val is not None:
                delta = value - baseline_val
                if delta > 0:
                    direction = "improved"
                elif delta < 0:
                    direction = "degraded"
                else:
                    direction = "unchanged"
            else:
                status = COMPARISON_INSUFFICIENT

            conn.execute(
                """
                INSERT INTO recommendation_outcomes
                    (recommendation_id, metric_name, outcome_value,
                     outcome_sample_size, delta, direction,
                     comparison_status, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recommendation_id, metric_name)
                DO UPDATE SET
                    outcome_value = excluded.outcome_value,
                    outcome_sample_size = excluded.outcome_sample_size,
                    delta = excluded.delta,
                    direction = excluded.direction,
                    comparison_status = excluded.comparison_status,
                    computed_at = excluded.computed_at
                """,
                (recommendation_id, metric_name, value, sample_size,
                 delta, direction, status, now),
            )
            conn.commit()

            result_row = conn.execute(
                "SELECT * FROM recommendation_outcomes "
                "WHERE recommendation_id = ? AND metric_name = ?",
                (recommendation_id, metric_name),
            ).fetchone()

        return OutcomeMetric.from_row(dict(result_row))

    def get_outcomes(self, recommendation_id: str) -> List[OutcomeMetric]:
        """Get all outcome metrics for a recommendation."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM recommendation_outcomes "
                "WHERE recommendation_id = ? ORDER BY metric_name",
                (recommendation_id,),
            ).fetchall()
        return [OutcomeMetric.from_row(dict(r)) for r in rows]

    def compute_dispatch_metrics(
        self,
        *,
        window_start: str,
        window_end: str,
        scope_tags: Optional[List[str]] = None,
    ) -> Dict[str, float]:
        """Compute aggregate metrics from dispatches within a time window."""
        return _compute_dispatch_metrics(
            self._state_dir,
            window_start=window_start,
            window_end=window_end,
            scope_tags=scope_tags,
        )

    def compute_outcomes_for_recommendation(
        self,
        recommendation_id: str,
    ) -> List[OutcomeMetric]:
        """Compute before/after outcomes for an accepted recommendation.

        Uses the recommendation's outcome window to query dispatch metrics
        for the baseline (before acceptance) and outcome (after acceptance) periods.
        Records results in the recommendation_outcomes table.

        Returns the computed OutcomeMetric list.
        """
        rec = self.get(recommendation_id)
        if rec is None:
            raise RecommendationNotFoundError(
                f"Recommendation not found: {recommendation_id!r}"
            )
        if not rec.is_accepted:
            raise RecommendationError(
                f"Recommendation {recommendation_id!r} is not accepted "
                f"(state: {rec.acceptance_state!r})"
            )
        if not rec.has_outcome_window:
            raise RecommendationError(
                f"Recommendation {recommendation_id!r} has no outcome window"
            )

        # Compute baseline: same duration before acceptance
        window_start = rec.outcome_window_start
        window_end = rec.outcome_window_end

        # Parse window duration for baseline calculation
        try:
            start_dt = datetime.fromisoformat(window_start.rstrip("Z"))
            end_dt = datetime.fromisoformat(window_end.rstrip("Z"))
            duration = end_dt - start_dt
            baseline_start = (start_dt - duration).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            ) + "Z"
        except (ValueError, AttributeError):
            raise RecommendationError(
                f"Invalid outcome window timestamps for {recommendation_id!r}"
            )

        # Compute baseline metrics (before acceptance)
        baseline_metrics = self.compute_dispatch_metrics(
            window_start=baseline_start,
            window_end=window_start,
        )

        # Compute outcome metrics (after acceptance)
        outcome_metrics = self.compute_dispatch_metrics(
            window_start=window_start,
            window_end=window_end,
        )

        # Record baselines and outcomes
        for metric_name in sorted(METRIC_NAMES):
            baseline_val = baseline_metrics.get(metric_name, 0.0)
            outcome_val = outcome_metrics.get(metric_name, 0.0)

            self.record_baseline(recommendation_id, metric_name, baseline_val, 1)
            self.record_outcome(recommendation_id, metric_name, outcome_val, 1)

        return self.get_outcomes(recommendation_id)

    def summarize_usefulness(
        self,
        recommendation_class: str,
    ) -> UsefulnessSummary:
        """Generate an operator-readable usefulness summary for a recommendation class.

        Advisory-only: this summary is for operator review, not auto-policy mutation.
        """
        return _summarize_usefulness(self._state_dir, recommendation_class)

    def export_usefulness_report(self) -> Dict[str, Any]:
        """Export a full usefulness report across all recommendation classes.

        Returns a dict suitable for JSON serialization and operator review.
        Advisory-only: G-R7.
        """
        return _export_usefulness_report(self._state_dir)
