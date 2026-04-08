#!/usr/bin/env python3
"""
VNX Governance Evaluation Engine — Policy evaluation against the autonomy policy matrix.

Implements FP-D PR-1: runtime policy evaluation layer that classifies actions into
automatic, gated, or forbidden outcomes against the canonical autonomy policy matrix
(40_FPD_AUTONOMY_POLICY_MATRIX.md).

Escalation and override logic lives in governance_escalation.py.

Feature flag: VNX_AUTONOMY_EVALUATION
  0 (default) = shadow mode — evaluation runs, emits events, but does not gate or block
  1 = enforcement mode — evaluation outcomes are binding

All evaluations emit coordination_events with event_type='policy_evaluation'.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from runtime_coordination import _append_event, _now_utc, get_connection

# Re-export escalation/override API for backward compatibility
from governance_escalation import (  # noqa: F401
    ESCALATION_LEVELS,
    ESCALATION_SEVERITY,
    TRIGGER_CATEGORIES,
    DE_ESCALATION_AUTHORITY,
    OVERRIDE_TYPES,
    GATE_ACTORS,
    GovernanceError,
    InvalidEscalationTransition,
    get_escalation_state,
    get_escalation_level,
    transition_escalation,
    is_blocked,
    get_unresolved_escalations,
    record_override,
    get_overrides,
    escalation_summary,
)

# ---------------------------------------------------------------------------
# Errors (ForbiddenActionError stays here — it's policy-specific)
# ---------------------------------------------------------------------------


class ForbiddenActionError(GovernanceError):
    """Raised when a forbidden action is attempted without override authority."""


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
# Feature flag
# ---------------------------------------------------------------------------

def is_enforcement_enabled() -> bool:
    """Check if autonomy evaluation enforcement is active."""
    return os.environ.get("VNX_AUTONOMY_EVALUATION", "0") == "1"


# ---------------------------------------------------------------------------
# Policy evaluation
# ---------------------------------------------------------------------------

def _apply_policy_rules(
    action: str,
    actor: str,
    base_outcome: str,
    policy_class: str,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply policy rules to determine final outcome, escalation, and gate authority."""
    outcome = base_outcome
    escalation_level = context.get("escalation_level")
    gate_authority = None
    reason_parts: List[str] = [f"policy_class={policy_class}, base={base_outcome}"]

    # Rule 1: Budget check — automatic + budget-limited -> promote to gated if exhausted
    if outcome == "automatic" and action in BUDGET_LIMITED_ACTIONS:
        budget_remaining = context.get("budget_remaining")
        if budget_remaining is not None and budget_remaining <= 0:
            outcome = "gated"
            escalation_level = "hold"
            gate_authority = "t0"
            reason_parts.append("budget exhausted -> gated+hold")

    # Rule 2: Escalation triggers for always-escalate actions
    if action in ALWAYS_ESCALATE_ACTIONS and outcome == "automatic":
        if not escalation_level or ESCALATION_SEVERITY.get(escalation_level, 0) < 1:
            escalation_level = "info"
            reason_parts.append("auto-escalation trigger")

    # Rule 3: Actor check — gated actions require gate authority
    if outcome == "gated":
        if actor in GATE_ACTORS:
            gate_authority = actor
            reason_parts.append(f"gate satisfied by {actor}")
        else:
            gate_authority = "t0"
            reason_parts.append(f"gate required — {actor} lacks authority")

    # Rule 4: Forbidden check — forbidden without human override flow
    if outcome == "forbidden":
        if actor in GATE_ACTORS:
            outcome = "gated"
            gate_authority = actor
            reason_parts.append(f"forbidden action permitted via {actor} override flow")
        else:
            escalation_level = "escalate"
            reason_parts.append("forbidden action by non-human actor")

    return {
        "outcome": outcome,
        "escalation_level": escalation_level,
        "gate_authority": gate_authority,
        "reason": "; ".join(reason_parts),
    }


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

    Args:
        action: Decision type to evaluate (e.g., 'dispatch_complete').
        actor: Who is performing the action.
        context: Optional context dict with dispatch_id, retry_count, etc.
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

    rules = _apply_policy_rules(action, actor, base_outcome, policy_class, ctx)

    result = {
        "outcome": rules["outcome"],
        "action": action,
        "policy_class": policy_class,
        "reason": rules["reason"],
        "escalation_level": rules["escalation_level"],
        "gate_authority": rules["gate_authority"],
        "evidence": {
            "evaluated_at": _now_utc(),
            "evaluated_by": "governance_evaluator",
            "policy_version": POLICY_VERSION,
        },
    }

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

    if enforcement and result["outcome"] == "forbidden" and actor not in GATE_ACTORS:
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
