#!/usr/bin/env python3
"""
VNX Governance Escalation & Override — Escalation state machine and override recording.

Extracted from governance_evaluator.py to keep each module under 800 lines.

Escalation state machine follows the escalation model (41_FPD_ESCALATION_MODEL.md):
  info -> review_required -> hold -> escalate
  De-escalation requires operator/T0 authority.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Dict, FrozenSet, List, Optional

from runtime_coordination import _append_event, _now_utc, get_connection

# ---------------------------------------------------------------------------
# Canonical enumerations
# ---------------------------------------------------------------------------

ESCALATION_LEVELS = frozenset({
    "info",
    "review_required",
    "hold",
    "escalate",
})

ESCALATION_SEVERITY = {
    "info": 0,
    "review_required": 1,
    "hold": 2,
    "escalate": 3,
}

TRIGGER_CATEGORIES = frozenset({
    "budget_exhausted",
    "repeated_failure",
    "no_target",
    "forbidden_action",
    "timeout_promotion",
    "dead_letter_accumulation",
    "operator_escalation",
    "policy_violation",
})

DE_ESCALATION_AUTHORITY: Dict[str, FrozenSet[str]] = {
    "hold": frozenset({"t0", "operator"}),
    "escalate": frozenset({"t0"}),
}

OVERRIDE_TYPES = frozenset({
    "gate_bypass",
    "invariant_override",
    "dispatch_force_promote",
    "dead_letter_override",
    "hold_release",
    "escalation_resolve",
})

GATE_ACTORS = frozenset({"t0", "operator"})

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GovernanceError(Exception):
    """Base error for governance evaluation failures."""


class InvalidEscalationTransition(GovernanceError):
    """Raised when an escalation state transition is not permitted."""


# ---------------------------------------------------------------------------
# Escalation state management
# ---------------------------------------------------------------------------

def get_escalation_state(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
) -> Optional[Dict[str, Any]]:
    """Get current escalation state for an entity. Returns None if no record."""
    row = conn.execute(
        "SELECT * FROM escalation_state WHERE entity_type = ? AND entity_id = ?",
        (entity_type, entity_id),
    ).fetchone()
    return dict(row) if row else None


def get_escalation_level(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
) -> str:
    """Get current escalation level for an entity. Returns 'info' if no record."""
    state = get_escalation_state(conn, entity_type, entity_id)
    return state["escalation_level"] if state else "info"


def _validate_escalation_transition(
    actor: str,
    current_level: str,
    new_level: str,
) -> None:
    """Validate escalation transition constraints. Raises on violation."""
    current_severity = ESCALATION_SEVERITY[current_level]
    new_severity = ESCALATION_SEVERITY[new_level]

    if actor not in GATE_ACTORS and new_severity < current_severity:
        raise InvalidEscalationTransition(
            f"Actor {actor!r} cannot de-escalate from {current_level!r} to {new_level!r}. "
            f"Only {sorted(DE_ESCALATION_AUTHORITY.get(current_level, GATE_ACTORS))} can de-escalate."
        )

    if new_severity < current_severity:
        required_authority = DE_ESCALATION_AUTHORITY.get(current_level, GATE_ACTORS)
        if actor not in required_authority:
            raise InvalidEscalationTransition(
                f"Actor {actor!r} lacks authority to de-escalate from {current_level!r}. "
                f"Required: {sorted(required_authority)}"
            )


def _upsert_escalation_state(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    new_level: str,
    actor: str,
    trigger_category: Optional[str],
    trigger_description: Optional[str],
    policy_class: Optional[str],
    decision_type: Optional[str],
    retry_count: Optional[int],
    budget_remaining: Optional[int],
    exists: bool,
) -> None:
    """Insert or update escalation state row."""
    now = _now_utc()
    if exists:
        conn.execute(
            """
            UPDATE escalation_state
            SET escalation_level = ?, trigger_category = ?, trigger_description = ?,
                policy_class = ?, decision_type = ?, retry_count = ?,
                budget_remaining = ?, updated_at = ?,
                resolved_at = CASE WHEN ? = 'info' THEN ? ELSE resolved_at END,
                resolved_by = CASE WHEN ? = 'info' THEN ? ELSE resolved_by END
            WHERE entity_type = ? AND entity_id = ?
            """,
            (
                new_level, trigger_category, trigger_description,
                policy_class, decision_type, retry_count,
                budget_remaining, now,
                new_level, now,
                new_level, actor,
                entity_type, entity_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO escalation_state
                (entity_type, entity_id, escalation_level, trigger_category,
                 trigger_description, policy_class, decision_type, retry_count,
                 budget_remaining, escalated_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type, entity_id, new_level, trigger_category,
                trigger_description, policy_class, decision_type, retry_count,
                budget_remaining, now, now,
            ),
        )


def transition_escalation(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    new_level: str,
    actor: str = "runtime",
    trigger_category: Optional[str] = None,
    trigger_description: Optional[str] = None,
    policy_class: Optional[str] = None,
    decision_type: Optional[str] = None,
    retry_count: Optional[int] = None,
    budget_remaining: Optional[int] = None,
) -> Dict[str, Any]:
    """Transition an entity's escalation level. Returns updated state dict.

    Raises InvalidEscalationTransition if level is invalid or actor lacks authority.
    """
    if new_level not in ESCALATION_LEVELS:
        raise InvalidEscalationTransition(
            f"Unknown escalation level: {new_level!r}. Valid: {sorted(ESCALATION_LEVELS)}"
        )

    current = get_escalation_state(conn, entity_type, entity_id)
    current_level = current["escalation_level"] if current else "info"

    _validate_escalation_transition(actor, current_level, new_level)

    _upsert_escalation_state(
        conn,
        entity_type=entity_type,
        entity_id=entity_id,
        new_level=new_level,
        actor=actor,
        trigger_category=trigger_category,
        trigger_description=trigger_description,
        policy_class=policy_class,
        decision_type=decision_type,
        retry_count=retry_count,
        budget_remaining=budget_remaining,
        exists=current is not None,
    )

    _append_event(
        conn,
        event_type="escalation_transition",
        entity_type=entity_type,
        entity_id=entity_id,
        from_state=current_level,
        to_state=new_level,
        actor=actor,
        reason=trigger_description,
        metadata={
            "trigger_category": trigger_category,
            "policy_class": policy_class,
            "decision_type": decision_type,
            "retry_count": retry_count,
            "budget_remaining": budget_remaining,
        },
    )

    try:
        from t0_escalations_log import (  # noqa: PLC0415
            record_from_governance_transition,
            write_escalation,
        )
        write_escalation(record_from_governance_transition(
            entity_type=entity_type,
            entity_id=entity_id,
            from_level=current_level,
            new_level=new_level,
            actor=actor,
            trigger_category=trigger_category,
            trigger_description=trigger_description,
        ))
    except Exception:
        pass  # JSONL write is best-effort; never block governance transitions

    return dict(
        conn.execute(
            "SELECT * FROM escalation_state WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        ).fetchone()
    )


def is_blocked(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
) -> bool:
    """Check if an entity is blocked by escalation (hold or escalate)."""
    level = get_escalation_level(conn, entity_type, entity_id)
    return ESCALATION_SEVERITY.get(level, 0) >= ESCALATION_SEVERITY["hold"]


def get_unresolved_escalations(
    conn: sqlite3.Connection,
    *,
    min_level: str = "review_required",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Get unresolved escalation records at or above min_level severity."""
    min_sev = ESCALATION_SEVERITY.get(min_level, 1)
    rows = conn.execute(
        """
        SELECT * FROM escalation_state
        WHERE resolved_at IS NULL
        ORDER BY
            CASE escalation_level
                WHEN 'escalate' THEN 3
                WHEN 'hold' THEN 2
                WHEN 'review_required' THEN 1
                ELSE 0
            END DESC,
            updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        if ESCALATION_SEVERITY.get(d["escalation_level"], 0) >= min_sev:
            result.append(d)
    return result


# ---------------------------------------------------------------------------
# Override recording
# ---------------------------------------------------------------------------

def _determine_override_level(
    override_type: str,
    previous_level: str,
    outcome: str,
) -> str:
    """Determine new escalation level after an override."""
    if outcome != "granted":
        return previous_level

    if override_type in ("hold_release", "escalation_resolve"):
        return "info"
    if override_type in ("gate_bypass", "dispatch_force_promote", "dead_letter_override"):
        if previous_level in ("hold", "escalate"):
            return "review_required"
    return previous_level


def _validate_override_params(
    actor: str, override_type: str, justification: str, outcome: str,
) -> None:
    """Validate override parameters. Raises GovernanceError on invalid input."""
    if actor not in GATE_ACTORS:
        raise GovernanceError(f"Actor {actor!r} lacks override authority. Required: {sorted(GATE_ACTORS)}")
    if not justification or not justification.strip():
        raise GovernanceError("Override justification is required and cannot be empty")
    if override_type not in OVERRIDE_TYPES:
        raise GovernanceError(f"Unknown override type: {override_type!r}. Valid: {sorted(OVERRIDE_TYPES)}")
    if outcome not in ("granted", "denied"):
        raise GovernanceError(f"Override outcome must be 'granted' or 'denied', got: {outcome!r}")


def _insert_override_record(
    conn: sqlite3.Connection,
    *,
    override_id: str,
    entity_type: str,
    entity_id: str,
    actor: str,
    override_type: str,
    justification: str,
    outcome: str,
    previous_level: str,
    new_level: str,
    policy_class: Optional[str],
    decision_type: Optional[str],
    override_scope: Optional[str],
) -> None:
    """Insert override record and emit governance_override event."""
    now = _now_utc()
    conn.execute(
        """
        INSERT INTO governance_overrides
            (override_id, entity_type, entity_id, actor, override_type,
             justification, outcome, previous_level, new_level,
             policy_class, decision_type, override_scope, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            override_id, entity_type, entity_id, actor, override_type,
            justification, outcome, previous_level, new_level,
            policy_class, decision_type, override_scope, now,
        ),
    )
    _append_event(
        conn,
        event_type="governance_override",
        entity_type=entity_type,
        entity_id=entity_id,
        from_state=previous_level,
        to_state=new_level,
        actor=actor,
        reason=justification,
        metadata={
            "override_id": override_id,
            "override_type": override_type,
            "outcome": outcome,
            "policy_class": policy_class,
            "decision_type": decision_type,
            "override_scope": override_scope,
        },
    )


def record_override(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    actor: str,
    override_type: str,
    justification: str,
    outcome: str = "granted",
    policy_class: Optional[str] = None,
    decision_type: Optional[str] = None,
    override_scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a governance override event. Returns the override record dict."""
    _validate_override_params(actor, override_type, justification, outcome)

    override_id = str(uuid.uuid4())
    current = get_escalation_state(conn, entity_type, entity_id)
    previous_level = current["escalation_level"] if current else "info"
    new_level = _determine_override_level(override_type, previous_level, outcome)

    _insert_override_record(
        conn, override_id=override_id, entity_type=entity_type, entity_id=entity_id,
        actor=actor, override_type=override_type, justification=justification,
        outcome=outcome, previous_level=previous_level, new_level=new_level,
        policy_class=policy_class, decision_type=decision_type, override_scope=override_scope,
    )

    if outcome == "granted" and new_level != previous_level:
        transition_escalation(
            conn, entity_type=entity_type, entity_id=entity_id, new_level=new_level,
            actor=actor, trigger_category="operator_escalation",
            trigger_description=f"Override {override_type}: {justification}",
            policy_class=policy_class, decision_type=decision_type,
        )

    row = conn.execute(
        "SELECT * FROM governance_overrides WHERE override_id = ?", (override_id,),
    ).fetchone()
    return dict(row)


def get_overrides(
    conn: sqlite3.Connection,
    *,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Get governance override records, newest first."""
    clauses = []
    params: list = []
    if entity_type:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    if entity_id:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM governance_overrides {where} ORDER BY occurred_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Operator-readable summaries
# ---------------------------------------------------------------------------

def escalation_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Generate operator-readable summary of current escalation state.

    Returns a dict with counts by level and detailed lists of holds/escalations.
    """
    from governance_evaluator import is_enforcement_enabled

    rows = conn.execute(
        "SELECT * FROM escalation_state WHERE resolved_at IS NULL"
    ).fetchall()

    counts = {"info": 0, "review_required": 0, "hold": 0, "escalate": 0}
    holds = []
    escalations = []

    for row in rows:
        d = dict(row)
        level = d["escalation_level"]
        counts[level] = counts.get(level, 0) + 1
        if level == "hold":
            holds.append({
                "entity": f"{d['entity_type']}:{d['entity_id']}",
                "trigger": d.get("trigger_description", "unknown"),
                "since": d.get("updated_at", d.get("escalated_at")),
            })
        elif level == "escalate":
            escalations.append({
                "entity": f"{d['entity_type']}:{d['entity_id']}",
                "trigger": d.get("trigger_description", "unknown"),
                "since": d.get("updated_at", d.get("escalated_at")),
            })

    blocking_count = counts["hold"] + counts["escalate"]

    return {
        "total_unresolved": sum(counts.values()),
        "blocking_count": blocking_count,
        "counts": counts,
        "holds": holds,
        "escalations": escalations,
        "enforcement_active": is_enforcement_enabled(),
    }
