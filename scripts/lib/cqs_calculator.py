#!/usr/bin/env python3
"""Composite Quality Score (CQS) calculator for VNX dispatches.

Computes an objective 0-100 quality score per dispatch from receipt signals,
replacing self-reported status with measurable indicators.

Scoring components (weighted, 7 total):
  - Status normalization (25%): Maps raw status to 5 categories
  - Completion signals (20%): Report path, PR merge, gate passed
  - Effort efficiency (15%): Token usage vs median for role
  - Error density (10%): Error/fail messages ratio in JSONL
  - Rework indicator (10%): Same gate+pr_id dispatched before?
  - T0 Advisory (10%): quality_advisory.t0_recommendation decision + risk_score
  - Open Items Delta (10%): Created vs resolved open items balance
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

# Normalize the ~166 unique status values into 5 categories
STATUS_MAP: Dict[str, str] = {
    # success (score contribution: 100)
    "task_complete": "success",
    "success": "success",
    "merged": "success",
    "completed": "success",
    "done": "success",
    "approved": "success",
    "gate_passed": "success",
    # partial (score contribution: 60)
    "partial": "partial",
    "needs_review": "partial",
    "partial_success": "partial",
    "in_progress": "partial",
    "pending_review": "partial",
    # failure (score contribution: 0)
    "task_failed": "failure",
    "error": "failure",
    "rejected": "failure",
    "failed": "failure",
    "blocked": "failure",
    # timeout (excluded from quality metrics)
    "no_confirmation": "timeout",
    "timeout": "timeout",
    "receipt_timeout": "timeout",
}

STATUS_SCORES = {"success": 100, "partial": 60, "failure": 0}


def normalize_status(raw_status: str | None) -> str:
    """Map raw status string to normalized category."""
    if not raw_status:
        return "unknown"
    key = raw_status.strip().lower().replace(" ", "_").replace("-", "_")
    return STATUS_MAP.get(key, "unknown")


def _score_status(normalized: str) -> float | None:
    """Score for status component. None = exclude from CQS."""
    return STATUS_SCORES.get(normalized)


def _score_completion(receipt: Dict[str, Any]) -> float:
    """Score based on completion signals (0-100)."""
    score = 0.0
    signals = 0
    total = 3

    if receipt.get("report_path"):
        score += 100
        signals += 1

    pr_merged = receipt.get("pr_merged") or receipt.get("provenance", {}).get("pr_merged")
    if pr_merged:
        score += 100
        signals += 1

    gate = receipt.get("gate_passed") or receipt.get("quality_advisory", {}).get("t0_recommendation", {}).get("decision") == "approve"
    if gate:
        score += 100
        signals += 1

    return (score / total) if total > 0 else 0.0


def _score_effort(session: Dict[str, Any] | None, db_path: Path | None = None, role: str | None = None) -> float:
    """Score based on token efficiency vs median for same role (0-100)."""
    if not session:
        return 50.0  # neutral when no session data

    total_tokens = (session.get("total_input_tokens") or 0) + (session.get("total_output_tokens") or 0)
    if total_tokens == 0:
        return 50.0

    median_tokens = _get_role_median_tokens(db_path, role)
    if median_tokens is None or median_tokens == 0:
        return 50.0

    ratio = total_tokens / median_tokens
    if ratio <= 0.5:
        return 100.0
    elif ratio <= 1.0:
        return 80.0 + (1.0 - ratio) * 40
    elif ratio <= 2.0:
        return 80.0 - (ratio - 1.0) * 60
    else:
        return max(0.0, 20.0 - (ratio - 2.0) * 10)


def _get_role_median_tokens(db_path: Path | None, role: str | None) -> float | None:
    """Get median total tokens for a role from session_analytics."""
    if not db_path or not role or not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            """SELECT total_input_tokens + total_output_tokens as total
               FROM session_analytics sa
               JOIN dispatch_metadata dm ON sa.dispatch_id = dm.dispatch_id
               WHERE dm.role = ?
               AND sa.total_input_tokens IS NOT NULL
               ORDER BY total""",
            (role,),
        ).fetchall()
        conn.close()
        if not rows:
            return None
        mid = len(rows) // 2
        return rows[mid][0]
    except Exception:
        return None


def _score_error_density(session: Dict[str, Any] | None) -> float:
    """Score based on error message density (0-100). Lower density = higher score."""
    if not session:
        return 50.0

    error_count = session.get("error_count") or 0
    total_messages = session.get("total_messages") or session.get("tool_calls_total") or 1

    if total_messages == 0:
        return 50.0

    ratio = error_count / total_messages
    if ratio == 0:
        return 100.0
    elif ratio < 0.05:
        return 80.0
    elif ratio < 0.15:
        return 50.0
    elif ratio < 0.30:
        return 25.0
    else:
        return 0.0


def _score_rework(dispatch_id: str, gate: str | None, pr_id: str | None, db_path: Path | None) -> float:
    """Score based on rework detection (0-100). First attempt = 100, rework = 0."""
    if not db_path or not db_path.exists() or not gate:
        return 100.0  # no data = assume first attempt
    try:
        conn = sqlite3.connect(str(db_path))
        count = conn.execute(
            """SELECT COUNT(*) FROM dispatch_metadata
               WHERE gate = ? AND (pr_id = ? OR (pr_id IS NULL AND ? IS NULL))
               AND dispatch_id != ? AND dispatched_at IS NOT NULL""",
            (gate, pr_id, pr_id, dispatch_id),
        ).fetchone()[0]
        conn.close()
        return 0.0 if count > 0 else 100.0
    except Exception:
        return 100.0


def _score_t0_advisory(receipt: Dict[str, Any]) -> float:
    """Score from quality_advisory.t0_recommendation (0-100)."""
    advisory = receipt.get("quality_advisory")
    if not isinstance(advisory, dict):
        return 50.0  # neutral

    rec = advisory.get("t0_recommendation")
    if not isinstance(rec, dict):
        return 50.0

    decision = rec.get("decision", "approve")
    risk_score = advisory.get("summary", {}).get("risk_score", 0)

    decision_scores = {"approve": 100.0, "approve_with_followup": 60.0, "hold": 0.0}
    decision_score = decision_scores.get(decision, 50.0)

    # Blend: 70% decision weight, 30% inverse risk score
    return decision_score * 0.7 + max(0, 100 - risk_score) * 0.3


def _score_open_items_delta(receipt: Dict[str, Any]) -> float:
    """Score based on open items created vs resolved (0-100)."""
    created = receipt.get("open_items_created", 0) or 0
    resolved = receipt.get("open_items_resolved", 0) or 0
    targeted = len(receipt.get("target_open_items") or [])

    if not created and not resolved and not targeted:
        return 50.0  # neutral — dispatch unrelated to open items

    score = 50.0
    score += min(30, resolved * 15)        # bonus per resolved item
    score -= min(30, created * 10)          # penalty per created item
    if targeted and resolved < targeted:    # penalty for unresolved targets
        score -= min(20, (targeted - resolved) * 20)

    return max(0, min(100, score))


def calculate_cqs(
    receipt: Dict[str, Any],
    session: Dict[str, Any] | None,
    db_path: Path | None = None,
    dispatch_id: str | None = None,
) -> Dict[str, Any]:
    """Calculate Composite Quality Score for a dispatch.

    Args:
        receipt: Receipt data with status, report_path, etc.
        session: Session analytics data (tokens, errors, etc.) or None.
        db_path: Path to quality_intelligence.db for median lookups.
        dispatch_id: Dispatch ID for rework detection.

    Returns:
        {cqs: float|None, normalized_status: str, components: dict}
        cqs is None when status is timeout/unknown (excluded from metrics).
    """
    raw_status = receipt.get("status") or receipt.get("outcome_status") or ""
    normalized = normalize_status(raw_status)

    status_score = _score_status(normalized)
    if status_score is None:
        return {
            "cqs": None,
            "normalized_status": normalized,
            "components": {"excluded_reason": f"status={normalized} excluded from quality metrics"},
        }

    role = receipt.get("role")
    gate = receipt.get("gate")
    pr_id = receipt.get("pr_id")

    completion = _score_completion(receipt)
    effort = _score_effort(session, db_path, role)
    error_density = _score_error_density(session)
    rework = _score_rework(dispatch_id or "", gate, pr_id, db_path)
    t0_advisory = _score_t0_advisory(receipt)
    oi_delta = _score_open_items_delta(receipt)

    # Weighted composite (7 components)
    cqs = (
        status_score * 0.25
        + completion * 0.20
        + effort * 0.15
        + error_density * 0.10
        + rework * 0.10
        + t0_advisory * 0.10
        + oi_delta * 0.10
    )

    components = {
        "status": round(status_score, 1),
        "completion": round(completion, 1),
        "effort": round(effort, 1),
        "error_density": round(error_density, 1),
        "rework": round(rework, 1),
        "t0_advisory": round(t0_advisory, 1),
        "oi_delta": round(oi_delta, 1),
        "weights": {
            "status": 0.25, "completion": 0.20, "effort": 0.15,
            "error_density": 0.10, "rework": 0.10,
            "t0_advisory": 0.10, "oi_delta": 0.10,
        },
    }

    return {
        "cqs": round(cqs, 2),
        "normalized_status": normalized,
        "components": components,
    }
