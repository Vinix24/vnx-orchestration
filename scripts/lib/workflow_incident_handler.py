#!/usr/bin/env python3
"""
VNX Workflow Incident Handler — Standalone incident recording helpers.

Extracted from workflow_supervisor.py to keep module size manageable.
Provides record_incident, record_escalation, apply_dead_letter, and
select_recovery_action as module-level functions used by WorkflowSupervisor.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from incident_taxonomy import (
    DEAD_LETTER_SOURCE_STATES,
    IncidentClass,
    RecoveryAction,
    Severity,
    get_contract,
)
from runtime_coordination import (
    DISPATCH_TRANSITIONS,
    _append_event,
    get_dispatch,
    transition_dispatch,
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class IncidentRecord:
    """A durable incident record emitted by the workflow supervisor."""
    incident_id: str
    incident_class: str
    severity: str
    dispatch_id: Optional[str]
    terminal_id: Optional[str]
    component: Optional[str]
    attempt_number: Optional[int]
    recovery_action: Optional[str]
    recovery_outcome: Optional[str]
    reason: str
    budget_remaining: Optional[int]
    escalated: bool
    dead_lettered: bool
    created_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EscalationRecord:
    """An escalation event to T0/operator."""
    escalation_id: str
    incident_id: str
    dispatch_id: Optional[str]
    terminal_id: Optional[str]
    incident_class: str
    severity: str
    escalated_to: str
    reason: str
    retry_count: int
    budget_exhausted: bool
    auto_recovery_halted: bool
    created_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------

def record_incident(
    conn,
    *,
    incident_class: IncidentClass,
    severity: Severity,
    dispatch_id: Optional[str],
    terminal_id: Optional[str],
    component: Optional[str],
    attempt_number: Optional[int],
    recovery_action: Optional[str],
    recovery_outcome: Optional[str],
    reason: str,
    budget_remaining: Optional[int],
    escalated: bool,
    dead_lettered: bool,
    metadata: Optional[Dict[str, Any]],
) -> IncidentRecord:
    """Write a durable incident record to the incident_log table.

    Adapts to PR-1's incident_log schema (entity_type/entity_id model).
    """
    incident_id = _new_id()
    now = _now_iso()

    # Determine entity_type and entity_id for PR-1 schema
    if dispatch_id:
        entity_type = "dispatch"
        entity_id = dispatch_id
    elif terminal_id:
        entity_type = "terminal"
        entity_id = terminal_id
    elif component:
        entity_type = "component"
        entity_id = component
    else:
        entity_type = "unknown"
        entity_id = "unknown"

    # Map state for PR-1 schema
    if dead_lettered:
        state = "dead_lettered"
    elif escalated:
        state = "escalated"
    else:
        state = "open"

    # Merge recovery_action into metadata for PR-1 schema
    full_metadata = dict(metadata or {})
    if recovery_action:
        full_metadata["recovery_action"] = recovery_action
    if recovery_outcome:
        full_metadata["recovery_outcome"] = recovery_outcome
    if budget_remaining is not None:
        full_metadata["budget_remaining"] = budget_remaining

    budget_exhausted = (budget_remaining is not None and budget_remaining == 0)

    conn.execute(
        """
        INSERT INTO incident_log
            (incident_id, incident_class, severity, entity_type, entity_id,
             dispatch_id, terminal_id, component_name, state,
             attempt_count, budget_exhausted, escalated, auto_recovery_halted,
             failure_detail, actor, occurred_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            incident_id,
            incident_class.value,
            severity.value,
            entity_type,
            entity_id,
            dispatch_id,
            terminal_id,
            component,
            state,
            attempt_number or 0,
            int(budget_exhausted),
            int(escalated),
            0,  # auto_recovery_halted tracked in retry_state
            reason,
            "workflow_supervisor",
            now,
            json.dumps(full_metadata),
        ),
    )

    _append_event(
        conn,
        event_type="incident_recorded",
        entity_type="incident",
        entity_id=incident_id,
        actor="workflow_supervisor",
        reason=reason,
        metadata={
            "incident_class": incident_class.value,
            "severity": severity.value,
            "dispatch_id": dispatch_id,
            "terminal_id": terminal_id,
            "recovery_action": recovery_action,
            "escalated": escalated,
            "dead_lettered": dead_lettered,
        },
    )

    return IncidentRecord(
        incident_id=incident_id,
        incident_class=incident_class.value,
        severity=severity.value,
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        component=component,
        attempt_number=attempt_number,
        recovery_action=recovery_action,
        recovery_outcome=recovery_outcome,
        reason=reason,
        budget_remaining=budget_remaining,
        escalated=escalated,
        dead_lettered=dead_lettered,
        created_at=now,
        metadata=metadata or {},
    )


def record_escalation(
    conn,
    *,
    incident_id: str,
    dispatch_id: Optional[str],
    terminal_id: Optional[str],
    incident_class: IncidentClass,
    severity: Severity,
    reason: str,
    retry_count: int,
    budget_exhausted: bool,
    auto_recovery_halted: bool,
    metadata: Optional[Dict[str, Any]],
) -> EscalationRecord:
    """Write a durable escalation record."""
    escalation_id = _new_id()
    now = _now_iso()
    escalated_to = get_contract(incident_class).escalation.escalate_to

    conn.execute(
        """
        INSERT INTO escalation_log
            (escalation_id, incident_id, dispatch_id, terminal_id,
             incident_class, severity, escalated_to, reason,
             retry_count, budget_exhausted, auto_recovery_halted,
             metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            escalation_id,
            incident_id,
            dispatch_id,
            terminal_id,
            incident_class.value,
            severity.value,
            escalated_to,
            reason,
            retry_count,
            int(budget_exhausted),
            int(auto_recovery_halted),
            json.dumps(metadata or {}),
            now,
        ),
    )

    _append_event(
        conn,
        event_type="escalation_emitted",
        entity_type="escalation",
        entity_id=escalation_id,
        actor="workflow_supervisor",
        reason=reason,
        metadata={
            "incident_class": incident_class.value,
            "dispatch_id": dispatch_id,
            "escalated_to": escalated_to,
            "auto_recovery_halted": auto_recovery_halted,
        },
    )

    return EscalationRecord(
        escalation_id=escalation_id,
        incident_id=incident_id,
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        incident_class=incident_class.value,
        severity=severity.value,
        escalated_to=escalated_to,
        reason=reason,
        retry_count=retry_count,
        budget_exhausted=budget_exhausted,
        auto_recovery_halted=auto_recovery_halted,
        created_at=now,
        metadata=metadata or {},
    )


def apply_dead_letter(
    conn,
    *,
    dispatch_id: str,
    incident_class: IncidentClass,
    reason: str,
    incident_id: str,
) -> None:
    """Transition a dispatch to dead_letter state.

    Only valid from DEAD_LETTER_SOURCE_STATES.
    """
    dispatch = get_dispatch(conn, dispatch_id)
    if not dispatch:
        return

    current_state = dispatch["state"]
    if current_state not in DEAD_LETTER_SOURCE_STATES:
        return

    if "dead_letter" not in DISPATCH_TRANSITIONS.get(current_state, frozenset()):
        return

    transition_dispatch(
        conn,
        dispatch_id=dispatch_id,
        to_state="dead_letter",
        actor="workflow_supervisor",
        reason=f"Dead-letter: {incident_class.value} — {reason}",
        metadata={
            "incident_class": incident_class.value,
            "incident_id": incident_id,
            "from_state": current_state,
        },
    )

    _append_event(
        conn,
        event_type="dispatch_dead_lettered",
        entity_type="dispatch",
        entity_id=dispatch_id,
        from_state=current_state,
        to_state="dead_letter",
        actor="workflow_supervisor",
        reason=f"Dead-letter: {incident_class.value} — {reason}",
        metadata={
            "incident_class": incident_class.value,
            "incident_id": incident_id,
        },
    )


def select_recovery_action(
    *,
    incident_class: IncidentClass,
    contract,
    do_retry: bool,
    do_dead_letter: bool,
    do_escalate: bool,
    halt: bool,
) -> str:
    """Select the appropriate recovery action from the contract's permitted set."""
    if do_dead_letter:
        return RecoveryAction.DEAD_LETTER_DISPATCH.value

    if halt:
        if RecoveryAction.HALT_TERMINAL in contract.permitted_actions:
            return RecoveryAction.HALT_TERMINAL.value
        return RecoveryAction.ESCALATE_TO_OPERATOR.value

    if do_escalate and not do_retry:
        return RecoveryAction.ESCALATE_TO_OPERATOR.value

    if do_retry:
        retry_actions = [
            RecoveryAction.RESTART_PROCESS,
            RecoveryAction.REDELIVER_DISPATCH,
            RecoveryAction.RECOVER_DISPATCH,
            RecoveryAction.EXPIRE_LEASE,
            RecoveryAction.RECOVER_LEASE,
            RecoveryAction.REMAP_PANE,
            RecoveryAction.TIMEOUT_DISPATCH,
        ]
        for action in retry_actions:
            if action in contract.permitted_actions:
                return action.value
        return RecoveryAction.ESCALATE_TO_OPERATOR.value

    return RecoveryAction.ESCALATE_TO_OPERATOR.value
