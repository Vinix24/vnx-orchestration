#!/usr/bin/env python3
"""
VNX Governance Evaluation Engine — Policy evaluation, escalation state, and override tracking.

Implements FP-D PR-1: runtime policy evaluation layer that classifies actions into
automatic, gated, or forbidden outcomes against the canonical autonomy policy matrix
(40_FPD_AUTONOMY_POLICY_MATRIX.md).

Escalation state machine follows the escalation model (41_FPD_ESCALATION_MODEL.md):
  info -> review_required -> hold -> escalate
  De-escalation requires operator/T0 authority.

Feature flag: VNX_AUTONOMY_EVALUATION
  0 (default) = shadow mode — evaluation runs, emits events, but does not gate or block
  1 = enforcement mode — evaluation outcomes are binding

All evaluations emit coordination_events with event_type='policy_evaluation'.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from runtime_coordination import _append_event, _now_utc, get_connection

# ---------------------------------------------------------------------------
# Policy version — bump on any policy matrix change
# ---------------------------------------------------------------------------

POLICY_VERSION = "fpd-pr1-v1"

# ---------------------------------------------------------------------------
# Canonical enumerations from PR-0 contract
# ---------------------------------------------------------------------------

POLICY_CLASSES = frozenset({
    "operational",
    "dispatch_lifecycle",
    "recovery",
    "routing",
    "intelligence",
    "escalation",
    "completion",
    "configuration",
    "merge",
    "override",
})

ACTION_CLASSES = frozenset({"automatic", "gated", "forbidden"})

ACTORS = frozenset({"runtime", "router", "broker", "t0", "operator"})

GATE_ACTORS = frozenset({"t0", "operator"})

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

# ---------------------------------------------------------------------------
# Canonical policy matrix — decision type -> (policy_class, action_class)
# ---------------------------------------------------------------------------

DECISION_TYPE_REGISTRY: Dict[str, Tuple[str, str]] = {
    # Operational — automatic
    "heartbeat_check":       ("operational", "automatic"),
    "health_state_update":   ("operational", "automatic"),
    "lease_renewal":         ("operational", "automatic"),
    "event_append":          ("operational", "automatic"),
    # Dispatch lifecycle — automatic
    "dispatch_create":       ("dispatch_lifecycle", "automatic"),
    "dispatch_claim":        ("dispatch_lifecycle", "automatic"),
    "dispatch_deliver":      ("dispatch_lifecycle", "automatic"),
    "dispatch_accept":       ("dispatch_lifecycle", "automatic"),
    "dispatch_run":          ("dispatch_lifecycle", "automatic"),
    "dispatch_timeout":      ("dispatch_lifecycle", "automatic"),
    "dispatch_fail":         ("dispatch_lifecycle", "automatic"),
    # Recovery — automatic (budget-limited)
    "process_restart":       ("recovery", "automatic"),
    "delivery_retry":        ("recovery", "automatic"),
    "lease_recover":         ("recovery", "automatic"),
    "dispatch_recover":      ("recovery", "automatic"),
    "inbox_retry":           ("recovery", "automatic"),
    # Routing — automatic (invariant-bound)
    "target_select":         ("routing", "automatic"),
    "fallback_route":        ("routing", "automatic"),
    "override_route":        ("routing", "gated"),
    # Intelligence — automatic (bounded)
    "intelligence_inject":   ("intelligence", "automatic"),
    "intelligence_suppress": ("intelligence", "automatic"),
    # Escalation — gated
    "escalation_emit":       ("escalation", "automatic"),
    "hold_enter":            ("escalation", "automatic"),
    "hold_release":          ("escalation", "gated"),
    "escalate_to_t0":        ("escalation", "automatic"),
    # Completion — gated
    "dispatch_complete":     ("completion", "gated"),
    "pr_close":              ("completion", "gated"),
    "feature_certify":       ("completion", "gated"),
    # Configuration — gated
    "policy_update":         ("configuration", "gated"),
    "feature_flag_toggle":   ("configuration", "gated"),
    "budget_adjust":         ("configuration", "gated"),
    # Merge — forbidden (autonomous)
    "branch_merge":          ("merge", "forbidden"),
    "force_push":            ("merge", "forbidden"),
    # Override — forbidden (autonomous)
    "gate_bypass":           ("override", "forbidden"),
    "invariant_override":    ("override", "forbidden"),
    "dispatch_force_promote": ("override", "forbidden"),
    "dead_letter_override":  ("override", "forbidden"),
}

# Budget-limited automatic actions and their default budgets
BUDGET_LIMITED_ACTIONS: Dict[str, int] = {
    "process_restart": 3,
    "delivery_retry": 3,
    "inbox_retry": 3,
}

# Actions that always trigger escalation on occurrence
ALWAYS_ESCALATE_ACTIONS = frozenset({
    "dispatch_timeout",
    "dispatch_fail",
})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GovernanceError(Exception):
    """Base error for governance evaluation failures."""


class ForbiddenActionError(GovernanceError):
    """Raised when a forbidden action is attempted without override authority."""


class InvalidEscalationTransition(GovernanceError):
    """Raised when an escalation state transition is not permitted."""


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

def is_enforcement_enabled() -> bool:
    """Check if autonomy evaluation enforcement is active."""
    return os.environ.get("VNX_AUTONOMY_EVALUATION", "0") == "1"


# ---------------------------------------------------------------------------
# Policy evaluation
# ---------------------------------------------------------------------------

def evaluate_policy(
    *,
    action: str,
    actor: str = "runtime",
    context: Optional[Dict[str, Any]] = None,
    conn: Optional[sqlite3.Connection] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate an action against the canonical policy matrix.

    Returns a policy evaluation result dict per the PR-0 contract (Section 4.2).

    If conn is provided, emits a coordination_event. If state_dir is provided
    and conn is not, opens a connection to emit the event.

    Args:
        action: Decision type to evaluate (e.g., 'dispatch_complete').
        actor: Who is performing the action ('runtime', 'router', 'broker', 't0', 'operator').
        context: Optional context dict with dispatch_id, retry_count, budget_remaining, etc.
        conn: Optional existing database connection for event emission.
        state_dir: Optional state directory path for opening a connection.

    Returns:
        Policy evaluation result dict.

    Raises:
        GovernanceError: If action is unknown or actor is invalid.
    """
    if actor not in ACTORS:
        raise GovernanceError(f"Unknown actor: {actor!r}. Valid: {sorted(ACTORS)}")

    if action not in DECISION_TYPE_REGISTRY:
        raise GovernanceError(f"Unknown decision type: {action!r}. Not in policy matrix.")

    ctx = context or {}
    policy_class, base_outcome = DECISION_TYPE_REGISTRY[action]

    outcome = base_outcome
    escalation_level = ctx.get("escalation_level")
    gate_authority = None
    reason_parts: List[str] = []

    # Rule 1: Lookup — base classification from matrix
    reason_parts.append(f"policy_class={policy_class}, base={base_outcome}")

    # Rule 2: Budget check — automatic + budget-limited -> promote to gated if exhausted
    if outcome == "automatic" and action in BUDGET_LIMITED_ACTIONS:
        budget_remaining = ctx.get("budget_remaining")
        if budget_remaining is not None and budget_remaining <= 0:
            outcome = "gated"
            escalation_level = "hold"
            gate_authority = "t0"
            reason_parts.append("budget exhausted -> gated+hold")

    # Rule 3: Escalation triggers for always-escalate actions
    if action in ALWAYS_ESCALATE_ACTIONS and outcome == "automatic":
        if not escalation_level or ESCALATION_SEVERITY.get(escalation_level, 0) < 1:
            escalation_level = "info"
            reason_parts.append("auto-escalation trigger")

    # Rule 4: Actor check — gated actions require gate authority
    if outcome == "gated":
        if actor in GATE_ACTORS:
            gate_authority = actor
            reason_parts.append(f"gate satisfied by {actor}")
        else:
            gate_authority = "t0"
            reason_parts.append(f"gate required — {actor} lacks authority")

    # Rule 5: Forbidden check — forbidden without human override flow
    if outcome == "forbidden":
        if actor in GATE_ACTORS:
            outcome = "gated"
            gate_authority = actor
            reason_parts.append(f"forbidden action permitted via {actor} override flow")
        else:
            escalation_level = "escalate"
            reason_parts.append("forbidden action by non-human actor")

    result = {
        "outcome": outcome,
        "action": action,
        "policy_class": policy_class,
        "reason": "; ".join(reason_parts),
        "escalation_level": escalation_level,
        "gate_authority": gate_authority,
        "evidence": {
            "evaluated_at": _now_utc(),
            "evaluated_by": "governance_evaluator",
            "policy_version": POLICY_VERSION,
        },
    }

    # Rule 6: Event emission
    _emit_evaluation_event(result, actor=actor, context=ctx, conn=conn, state_dir=state_dir)

    return result


def _emit_evaluation_event(
    result: Dict[str, Any],
    *,
    actor: str,
    context: Dict[str, Any],
    conn: Optional[sqlite3.Connection] = None,
    state_dir: Optional[str] = None,
) -> None:
    """Emit a policy_evaluation coordination event."""
    entity_type = _infer_entity_type(result["action"], context)
    entity_id = (
        context.get("dispatch_id")
        or context.get("target_id")
        or context.get("terminal_id")
        or "system"
    )
    metadata = {
        "action": result["action"],
        "policy_class": result["policy_class"],
        "outcome": result["outcome"],
        "escalation_level": result.get("escalation_level"),
        "gate_authority": result.get("gate_authority"),
        "budget_remaining": context.get("budget_remaining"),
        "policy_version": POLICY_VERSION,
        "enforcement": is_enforcement_enabled(),
    }

    if conn is not None:
        _append_event(
            conn,
            event_type="policy_evaluation",
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
            reason=result["reason"],
            metadata=metadata,
        )
    elif state_dir is not None:
        with get_connection(state_dir) as c:
            _append_event(
                c,
                event_type="policy_evaluation",
                entity_type=entity_type,
                entity_id=entity_id,
                actor=actor,
                reason=result["reason"],
                metadata=metadata,
            )
            c.commit()


def _infer_entity_type(action: str, context: Dict[str, Any]) -> str:
    """Infer entity_type from action and context."""
    if context.get("dispatch_id"):
        return "dispatch"
    if context.get("target_id"):
        return "target"
    if context.get("terminal_id"):
        return "lease"
    action_prefix = action.split("_")[0]
    prefix_map = {
        "dispatch": "dispatch",
        "lease": "lease",
        "target": "target",
        "heartbeat": "target",
        "health": "target",
        "process": "target",
        "delivery": "dispatch",
        "inbox": "inbox_event",
        "intelligence": "dispatch",
        "branch": "dispatch",
        "force": "dispatch",
        "gate": "dispatch",
        "invariant": "dispatch",
        "dead": "dispatch",
        "pr": "dispatch",
        "feature": "dispatch",
        "policy": "dispatch",
        "budget": "dispatch",
        "escalation": "dispatch",
        "hold": "dispatch",
        "escalate": "dispatch",
        "override": "dispatch",
        "fallback": "target",
    }
    return prefix_map.get(action_prefix, "dispatch")


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
    """Transition an entity's escalation level.

    Validates that:
    - new_level is a valid escalation level
    - Runtime actors can only increase severity
    - De-escalation requires appropriate authority

    Returns the updated escalation state dict.

    Raises:
        InvalidEscalationTransition: If the transition violates constraints.
    """
    if new_level not in ESCALATION_LEVELS:
        raise InvalidEscalationTransition(
            f"Unknown escalation level: {new_level!r}. Valid: {sorted(ESCALATION_LEVELS)}"
        )

    current = get_escalation_state(conn, entity_type, entity_id)
    current_level = current["escalation_level"] if current else "info"

    current_severity = ESCALATION_SEVERITY[current_level]
    new_severity = ESCALATION_SEVERITY[new_level]

    # Enforce: runtime can only increase severity
    if actor not in GATE_ACTORS and new_severity < current_severity:
        raise InvalidEscalationTransition(
            f"Actor {actor!r} cannot de-escalate from {current_level!r} to {new_level!r}. "
            f"Only {sorted(DE_ESCALATION_AUTHORITY.get(current_level, GATE_ACTORS))} can de-escalate."
        )

    # Enforce: de-escalation authority constraints
    if new_severity < current_severity:
        required_authority = DE_ESCALATION_AUTHORITY.get(current_level, GATE_ACTORS)
        if actor not in required_authority:
            raise InvalidEscalationTransition(
                f"Actor {actor!r} lacks authority to de-escalate from {current_level!r}. "
                f"Required: {sorted(required_authority)}"
            )

    now = _now_utc()

    if current:
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

    # Emit coordination event
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
    """Record a governance override event.

    Validates:
    - Actor has override authority (must be t0 or operator)
    - Justification is non-empty
    - Override type is recognized
    - Outcome is granted or denied

    If outcome is 'granted' and entity has an escalation state,
    updates escalation level appropriately.

    Returns the override record dict.
    """
    if actor not in GATE_ACTORS:
        raise GovernanceError(
            f"Actor {actor!r} lacks override authority. Required: {sorted(GATE_ACTORS)}"
        )

    if not justification or not justification.strip():
        raise GovernanceError("Override justification is required and cannot be empty")

    if override_type not in OVERRIDE_TYPES:
        raise GovernanceError(
            f"Unknown override type: {override_type!r}. Valid: {sorted(OVERRIDE_TYPES)}"
        )

    if outcome not in ("granted", "denied"):
        raise GovernanceError(f"Override outcome must be 'granted' or 'denied', got: {outcome!r}")

    override_id = str(uuid.uuid4())
    now = _now_utc()

    # Get previous escalation level
    current = get_escalation_state(conn, entity_type, entity_id)
    previous_level = current["escalation_level"] if current else "info"
    new_level = previous_level

    # If granted, determine new escalation level
    if outcome == "granted":
        if override_type in ("hold_release", "escalation_resolve"):
            new_level = "info"
        elif override_type in ("gate_bypass", "dispatch_force_promote", "dead_letter_override"):
            if previous_level in ("hold", "escalate"):
                new_level = "review_required"

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

    # Emit governance_override coordination event
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

    # Apply escalation state change if granted and level changed
    if outcome == "granted" and new_level != previous_level:
        transition_escalation(
            conn,
            entity_type=entity_type,
            entity_id=entity_id,
            new_level=new_level,
            actor=actor,
            trigger_category="operator_escalation",
            trigger_description=f"Override {override_type}: {justification}",
            policy_class=policy_class,
            decision_type=decision_type,
        )

    row = conn.execute(
        "SELECT * FROM governance_overrides WHERE override_id = ?",
        (override_id,),
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


# ---------------------------------------------------------------------------
# Governance-aware action guard
# ---------------------------------------------------------------------------

def check_action(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: str = "runtime",
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate a policy and check escalation state — convenience wrapper.

    Combines evaluate_policy() with escalation blocking checks.
    In enforcement mode, raises ForbiddenActionError for forbidden outcomes.

    Returns the evaluation result augmented with 'blocked' and 'enforcement' fields.
    """
    result = evaluate_policy(action=action, actor=actor, context=context, conn=conn)
    ctx = context or {}

    entity_type = _infer_entity_type(action, ctx)
    entity_id = ctx.get("dispatch_id") or ctx.get("target_id") or ctx.get("terminal_id") or "system"

    blocked = is_blocked(conn, entity_type, entity_id)
    enforcement = is_enforcement_enabled()

    result["blocked"] = blocked
    result["enforcement"] = enforcement

    # In enforcement mode, block forbidden actions from non-human actors
    if enforcement and result["outcome"] == "forbidden" and actor not in GATE_ACTORS:
        # Record the forbidden attempt as an escalation
        transition_escalation(
            conn,
            entity_type=entity_type,
            entity_id=entity_id,
            new_level="escalate",
            actor=actor,
            trigger_category="forbidden_action",
            trigger_description=f"Forbidden action attempted: {action}",
            policy_class=result["policy_class"],
            decision_type=action,
        )
        raise ForbiddenActionError(
            f"Forbidden action {action!r} by {actor!r}. "
            f"This action requires T0 or operator override."
        )

    return result
