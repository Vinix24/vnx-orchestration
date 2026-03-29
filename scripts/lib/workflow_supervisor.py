#!/usr/bin/env python3
"""
VNX Workflow Supervisor — Workflow-aware supervision with incident classification,
dead-letter routing, and escalation semantics.

PR-2 deliverable: introduces supervision that reasons about dispatch state,
incident class, retry budget, and dead-letter transitions. Process crashes
and workflow failures are handled by different logic paths.

Design rules:
  - Workflow supervisor differentiates incident classes before choosing recovery (G-R1)
  - Retry budgets are mandatory — no infinite loops (G-R2)
  - Every recovery action emits an incident trail (G-R3)
  - Dead-letter is explicit — unrecoverable dispatches stop in a reviewable state (G-R5)
  - Final recovery authority remains governance-aware (G-R8)
  - Process restart decisions are separate from workflow resume decisions (A-R1)

Architecture:
  - Layers on top of FP-A coordination state (runtime_coordination.py)
  - Consumes incident taxonomy from PR-0 (incident_taxonomy.py)
  - Records incidents durably in incident_log table
  - Tracks retry budgets in retry_state table
  - Emits escalation events to escalation_log table
  - Compatible with existing simple supervisor (does not replace it)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from incident_taxonomy import (
    DEAD_LETTER_ELIGIBLE_CLASSES,
    DEAD_LETTER_SOURCE_STATES,
    IncidentClass,
    RECOVERY_CONTRACTS,
    RecoveryAction,
    REPEATED_FAILURE_THRESHOLD,
    Severity,
    get_contract,
    get_cooldown_seconds,
    should_dead_letter,
    should_escalate,
)
from runtime_coordination import (
    DISPATCH_TRANSITIONS,
    InvalidTransitionError,
    _append_event,
    _now_utc,
    get_connection,
    get_dispatch,
    init_schema,
    transition_dispatch,
)


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


@dataclass
class SupervisionDecision:
    """The outcome of a workflow supervision evaluation."""
    dispatch_id: str
    incident_class: str
    action_taken: str
    should_retry: bool
    should_escalate: bool
    should_dead_letter: bool
    auto_recovery_halted: bool
    budget_remaining: int
    cooldown_seconds: int
    incident_record: Optional[IncidentRecord] = None
    escalation_record: Optional[EscalationRecord] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "incident_class": self.incident_class,
            "action_taken": self.action_taken,
            "should_retry": self.should_retry,
            "should_escalate": self.should_escalate,
            "should_dead_letter": self.should_dead_letter,
            "auto_recovery_halted": self.auto_recovery_halted,
            "budget_remaining": self.budget_remaining,
            "cooldown_seconds": self.cooldown_seconds,
            "reason": self.reason,
        }


@dataclass
class SupervisionSummary:
    """Summary of a workflow supervision pass."""
    run_at: str
    incidents_recorded: int = 0
    escalations_emitted: int = 0
    dead_lettered: int = 0
    retries_permitted: int = 0
    halted: int = 0
    decisions: List[SupervisionDecision] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Workflow supervision at {self.run_at}",
            f"  Incidents recorded:  {self.incidents_recorded}",
            f"  Escalations emitted: {self.escalations_emitted}",
            f"  Dead-lettered:       {self.dead_lettered}",
            f"  Retries permitted:   {self.retries_permitted}",
            f"  Halted:              {self.halted}",
            f"  Errors:              {len(self.errors)}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ---------------------------------------------------------------------------
# WorkflowSupervisor
# ---------------------------------------------------------------------------

class WorkflowSupervisor:
    """Workflow-aware supervision engine for the VNX runtime.

    Separates process-level incidents from workflow-level incidents and applies
    recovery contracts from the canonical incident taxonomy. Tracks retry
    budgets durably and routes unrecoverable dispatches to dead-letter.

    Args:
        state_dir: Path to .vnx-data/state/ containing runtime_coordination.db.
        auto_init: Initialize schema on construction (default True).
    """

    def __init__(self, state_dir: str | Path, *, auto_init: bool = True) -> None:
        self._state_dir = Path(state_dir)
        if auto_init:
            init_schema(self._state_dir)

    # ------------------------------------------------------------------
    # Core: evaluate and handle an incident
    # ------------------------------------------------------------------

    def handle_incident(
        self,
        *,
        incident_class: IncidentClass,
        dispatch_id: Optional[str] = None,
        terminal_id: Optional[str] = None,
        component: Optional[str] = None,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SupervisionDecision:
        """Evaluate an incident and apply the appropriate recovery contract.

        This is the main entry point for the workflow supervisor. It:
        1. Classifies the incident using the canonical taxonomy
        2. Checks retry budget for the (dispatch, incident_class) pair
        3. Records a durable incident record
        4. Decides: retry, escalate, dead-letter, or halt
        5. Applies the decision to dispatch state if applicable

        Returns a SupervisionDecision with full audit trail.
        """
        contract = get_contract(incident_class)
        now = _now_iso()

        with get_connection(self._state_dir) as conn:
            # Get current retry state
            retry = self._get_or_create_retry_state(
                conn, dispatch_id=dispatch_id, incident_class=incident_class
            )
            attempts_used = retry["attempts_used"]
            max_retries = contract.retry_budget.max_retries

            # Check for repeated failure loop (only for dead-letter-eligible classes)
            effective_class = incident_class
            effective_contract = contract
            if (
                dispatch_id
                and attempts_used >= REPEATED_FAILURE_THRESHOLD
                and incident_class in DEAD_LETTER_ELIGIBLE_CLASSES
            ):
                loop_detected = self._detect_repeated_failure_loop(
                    conn, dispatch_id=dispatch_id, incident_class=incident_class
                )
                if loop_detected:
                    effective_class = IncidentClass.REPEATED_FAILURE_LOOP
                    effective_contract = get_contract(effective_class)

            # Can this attempt proceed within budget?
            effective_max = effective_contract.retry_budget.max_retries
            can_retry = attempts_used < effective_max
            # Budget remaining AFTER this attempt is processed
            budget_remaining = max(0, effective_max - attempts_used - 1) if can_retry else 0

            # Determine recovery action
            do_escalate = should_escalate(effective_class, attempts_used)
            halt = effective_contract.escalation.halt_auto_recovery and do_escalate

            # Check dead-letter eligibility
            do_dead_letter = False
            dispatch_state = None
            if dispatch_id:
                dispatch = get_dispatch(conn, dispatch_id)
                if dispatch:
                    dispatch_state = dispatch["state"]
                    do_dead_letter = (
                        not can_retry
                        and should_dead_letter(effective_class, attempts_used, dispatch_state)
                    )

            # Determine if retry is permitted
            do_retry = (
                can_retry
                and not halt
                and not do_dead_letter
                and self._cooldown_elapsed(retry, contract)
            )

            # Select recovery action
            action_taken = self._select_recovery_action(
                incident_class=effective_class,
                contract=effective_contract,
                do_retry=do_retry,
                do_dead_letter=do_dead_letter,
                do_escalate=do_escalate,
                halt=halt,
            )

            # Record the incident
            incident_record = self._record_incident(
                conn,
                incident_class=effective_class,
                severity=effective_contract.default_severity,
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                component=component,
                attempt_number=attempts_used + 1,
                recovery_action=action_taken,
                recovery_outcome="pending",
                reason=reason,
                budget_remaining=budget_remaining,
                escalated=do_escalate,
                dead_lettered=do_dead_letter,
                metadata=metadata,
            )

            # Update retry state
            if dispatch_id:
                self._increment_retry(
                    conn,
                    dispatch_id=dispatch_id,
                    incident_class=incident_class,
                    budget_exhausted=(not can_retry),
                    escalated=do_escalate,
                    halted=halt,
                )

            # Apply dead-letter transition
            if do_dead_letter and dispatch_id and dispatch_state:
                self._apply_dead_letter(
                    conn,
                    dispatch_id=dispatch_id,
                    incident_class=effective_class,
                    reason=reason,
                    incident_id=incident_record.incident_id,
                )

            # Record escalation
            escalation_record = None
            if do_escalate:
                escalation_record = self._record_escalation(
                    conn,
                    incident_id=incident_record.incident_id,
                    dispatch_id=dispatch_id,
                    terminal_id=terminal_id,
                    incident_class=effective_class,
                    severity=effective_contract.default_severity,
                    reason=reason,
                    retry_count=attempts_used,
                    budget_exhausted=(not can_retry),
                    auto_recovery_halted=halt,
                    metadata=metadata,
                )

            cooldown = get_cooldown_seconds(effective_class, attempts_used) if do_retry else 0

            conn.commit()

        decision = SupervisionDecision(
            dispatch_id=dispatch_id or "",
            incident_class=effective_class.value,
            action_taken=action_taken,
            should_retry=do_retry,
            should_escalate=do_escalate,
            should_dead_letter=do_dead_letter,
            auto_recovery_halted=halt,
            budget_remaining=budget_remaining if not do_dead_letter else 0,
            cooldown_seconds=cooldown,
            incident_record=incident_record,
            escalation_record=escalation_record,
            reason=reason,
        )

        return decision

    # ------------------------------------------------------------------
    # Resume path validation
    # ------------------------------------------------------------------

    def can_resume(self, dispatch_id: str) -> Dict[str, Any]:
        """Check whether a dispatch can safely resume.

        Resume is only valid when:
        1. Dispatch exists and is in a resumable state (recovered)
        2. No active halt is in effect for this dispatch
        3. The retry budget is not exhausted for all incident classes

        Returns a dict with 'allowed', 'reason', and context.
        """
        with get_connection(self._state_dir) as conn:
            dispatch = get_dispatch(conn, dispatch_id)
            if not dispatch:
                return {
                    "allowed": False,
                    "reason": f"Dispatch not found: {dispatch_id}",
                    "dispatch_state": None,
                }

            state = dispatch["state"]
            if state not in ("recovered", "queued"):
                return {
                    "allowed": False,
                    "reason": f"Dispatch in state '{state}' cannot resume (requires 'recovered' or 'queued')",
                    "dispatch_state": state,
                }

            # Check for active halts
            halt_row = conn.execute(
                """
                SELECT * FROM retry_state
                WHERE dispatch_id = ? AND halted = 1
                LIMIT 1
                """,
                (dispatch_id,),
            ).fetchone()

            if halt_row:
                return {
                    "allowed": False,
                    "reason": (
                        f"Auto-recovery halted for incident class "
                        f"'{halt_row['incident_class']}' — operator must clear halt"
                    ),
                    "dispatch_state": state,
                    "halted_by": halt_row["incident_class"],
                }

            # Check for budget exhaustion across all classes
            exhausted_rows = conn.execute(
                """
                SELECT incident_class, attempts_used FROM retry_state
                WHERE dispatch_id = ? AND budget_exhausted = 1
                """,
                (dispatch_id,),
            ).fetchall()

            if exhausted_rows:
                exhausted = [
                    {"class": r["incident_class"], "attempts": r["attempts_used"]}
                    for r in exhausted_rows
                ]
                return {
                    "allowed": False,
                    "reason": "Retry budget exhausted for one or more incident classes",
                    "dispatch_state": state,
                    "exhausted_budgets": exhausted,
                }

            return {
                "allowed": True,
                "reason": "Dispatch eligible for resume",
                "dispatch_state": state,
                "attempt_count": dispatch.get("attempt_count", 0),
            }

    # ------------------------------------------------------------------
    # Query: incident pressure and summaries
    # ------------------------------------------------------------------

    def get_incident_summary(
        self,
        *,
        dispatch_id: Optional[str] = None,
        terminal_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return incident records for a dispatch or terminal."""
        with get_connection(self._state_dir) as conn:
            clauses = []
            params: list = []
            if dispatch_id:
                clauses.append("dispatch_id = ?")
                params.append(dispatch_id)
            if terminal_id:
                clauses.append("terminal_id = ?")
                params.append(terminal_id)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM incident_log {where} ORDER BY occurred_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_pending_escalations(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Return unacknowledged escalation events."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                """
                SELECT * FROM escalation_log
                WHERE acknowledged = 0
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_dead_letter_dispatches(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Return dispatches in dead_letter state."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                """
                SELECT * FROM dispatches
                WHERE state = 'dead_letter'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def acknowledge_escalation(self, escalation_id: str) -> bool:
        """Mark an escalation as acknowledged by operator."""
        now = _now_iso()
        with get_connection(self._state_dir) as conn:
            cursor = conn.execute(
                """
                UPDATE escalation_log
                SET acknowledged = 1, acknowledged_at = ?
                WHERE escalation_id = ? AND acknowledged = 0
                """,
                (now, escalation_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def clear_halt(self, dispatch_id: str, incident_class: str) -> bool:
        """Clear an auto-recovery halt for a specific dispatch and incident class.

        Operator action: allows retry to resume after manual investigation.
        """
        now = _now_iso()
        with get_connection(self._state_dir) as conn:
            cursor = conn.execute(
                """
                UPDATE retry_state
                SET halted = 0, updated_at = ?
                WHERE dispatch_id = ? AND incident_class = ? AND halted = 1
                """,
                (now, dispatch_id, incident_class),
            )
            if cursor.rowcount > 0:
                _append_event(
                    conn,
                    event_type="halt_cleared",
                    entity_type="dispatch",
                    entity_id=dispatch_id,
                    actor="operator",
                    reason=f"Halt cleared for {incident_class}",
                    metadata={"incident_class": incident_class},
                )
            conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Internal: retry state management
    # ------------------------------------------------------------------

    def _get_or_create_retry_state(
        self,
        conn,
        *,
        dispatch_id: Optional[str],
        incident_class: IncidentClass,
    ) -> Dict[str, Any]:
        """Get or create retry state for a (dispatch, incident_class) pair."""
        if not dispatch_id:
            return {
                "dispatch_id": None,
                "incident_class": incident_class.value,
                "attempts_used": 0,
                "last_attempt_at": None,
                "next_eligible_at": None,
                "budget_exhausted": 0,
                "escalated": 0,
                "halted": 0,
            }

        row = conn.execute(
            """
            SELECT * FROM retry_state
            WHERE dispatch_id = ? AND incident_class = ?
            """,
            (dispatch_id, incident_class.value),
        ).fetchone()

        if row:
            return dict(row)

        now = _now_iso()
        conn.execute(
            """
            INSERT INTO retry_state
                (dispatch_id, incident_class, attempts_used, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            (dispatch_id, incident_class.value, now, now),
        )
        return {
            "dispatch_id": dispatch_id,
            "incident_class": incident_class.value,
            "attempts_used": 0,
            "last_attempt_at": None,
            "next_eligible_at": None,
            "budget_exhausted": 0,
            "escalated": 0,
            "halted": 0,
        }

    def _increment_retry(
        self,
        conn,
        *,
        dispatch_id: str,
        incident_class: IncidentClass,
        budget_exhausted: bool,
        escalated: bool,
        halted: bool,
    ) -> None:
        """Increment retry attempt count and update state."""
        now = _now_iso()
        cooldown = get_cooldown_seconds(incident_class, 0)
        next_eligible = datetime.now(timezone.utc)
        from datetime import timedelta
        next_eligible_iso = (
            (next_eligible + timedelta(seconds=cooldown))
            .strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        )

        conn.execute(
            """
            UPDATE retry_state
            SET attempts_used = attempts_used + 1,
                last_attempt_at = ?,
                next_eligible_at = ?,
                budget_exhausted = ?,
                escalated = ?,
                halted = ?,
                updated_at = ?
            WHERE dispatch_id = ? AND incident_class = ?
            """,
            (
                now,
                next_eligible_iso,
                int(budget_exhausted),
                int(escalated),
                int(halted),
                now,
                dispatch_id,
                incident_class.value,
            ),
        )

    def _cooldown_elapsed(self, retry: Dict[str, Any], contract) -> bool:
        """Check if cooldown period has elapsed since last attempt."""
        next_eligible = retry.get("next_eligible_at")
        if not next_eligible:
            return True
        try:
            raw = next_eligible.strip().replace("Z", "+00:00")
            eligible_dt = datetime.fromisoformat(raw)
            if eligible_dt.tzinfo is None:
                eligible_dt = eligible_dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= eligible_dt
        except (ValueError, AttributeError):
            return True

    def _detect_repeated_failure_loop(
        self,
        conn,
        *,
        dispatch_id: str,
        incident_class: IncidentClass,
    ) -> bool:
        """Detect if a dispatch has hit the repeated failure loop threshold."""
        row = conn.execute(
            """
            SELECT COUNT(*) as cnt FROM incident_log
            WHERE dispatch_id = ? AND incident_class = ?
            """,
            (dispatch_id, incident_class.value),
        ).fetchone()
        return (row["cnt"] if row else 0) >= REPEATED_FAILURE_THRESHOLD

    # ------------------------------------------------------------------
    # Internal: incident recording
    # ------------------------------------------------------------------

    def _record_incident(
        self,
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

    def _record_escalation(
        self,
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

    # ------------------------------------------------------------------
    # Internal: dead-letter routing
    # ------------------------------------------------------------------

    def _apply_dead_letter(
        self,
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
            reason=(
                f"Dead-letter: {incident_class.value} — {reason}"
            ),
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

    # ------------------------------------------------------------------
    # Internal: action selection
    # ------------------------------------------------------------------

    def _select_recovery_action(
        self,
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
            # Select first applicable retry action from permitted set
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
