#!/usr/bin/env python3
"""
VNX Incident Log — Durable incident substrate and retry budget helpers.

PR-1 deliverable: adds the durable incident log and bounded retry bookkeeping
in shadow mode so the supervisor can start recording the right truth before
restart authority moves.

Design:
  - Incident records are durable, typed, and tied to dispatch/terminal/component.
  - Retry budgets persist across process restarts (SQLite-backed).
  - Shadow mode: records incidents from the existing supervisor without changing
    its behavior. Controlled by VNX_INCIDENT_SHADOW env var (default "1" = on).
  - All helpers are idempotent and safe to call from the supervisor monitor loop.

Governance references:
  G-R1: No automatic recovery may hide a failure class
  G-R2: Retry budgets are mandatory — no infinite restart or resend loops
  G-R3: Every recovery action must emit an incident trail
  G-R5: Dead-letter is explicit
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from incident_taxonomy import (
    RECOVERY_CONTRACTS,
    IncidentClass,
    Severity,
    get_cooldown_seconds,
    should_dead_letter,
    should_escalate,
    validate_incident_class,
)
from runtime_coordination import get_connection

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SHADOW_MODE_ENV = "VNX_INCIDENT_SHADOW"
"""Environment variable that enables shadow mode. Default "1" (on)."""

_OPEN_STATES = frozenset({"open", "escalated"})
"""Incident states considered active for summary and budget checks."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_dt(ts: Optional[str]) -> Optional[datetime]:
    if ts is None:
        return None
    ts = ts.rstrip("Z")
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _budget_key(entity_type: str, entity_id: str, incident_class: str) -> str:
    return f"{entity_type}:{entity_id}:{incident_class}"


def _dump(obj: Any) -> str:
    return json.dumps(obj) if obj is not None else "{}"


# ---------------------------------------------------------------------------
# Shadow mode guard
# ---------------------------------------------------------------------------

def is_shadow_mode() -> bool:
    """Return True if VNX_INCIDENT_SHADOW is enabled (default on).

    Shadow mode means the incident log records supervisor outcomes without
    influencing recovery decisions. The existing supervisor behavior is
    authoritative while shadow mode is active.
    """
    return os.environ.get(SHADOW_MODE_ENV, "1") == "1"


# ---------------------------------------------------------------------------
# Incident creation
# ---------------------------------------------------------------------------

def create_incident(
    state_dir: str | Path,
    *,
    incident_class: str | IncidentClass,
    entity_type: str,
    entity_id: str,
    dispatch_id: Optional[str] = None,
    terminal_id: Optional[str] = None,
    component_name: Optional[str] = None,
    failure_detail: Optional[str] = None,
    actor: str = "supervisor",
    metadata: Optional[Dict[str, Any]] = None,
    severity_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a durable incident record in the incident_log table.

    The incident is tied to a dispatch, terminal, and/or component context.
    Severity defaults to the contract's default_severity but can be elevated
    via severity_override.

    Args:
        state_dir: Path to the .vnx-data/state directory.
        incident_class: Canonical incident class string or IncidentClass enum.
        entity_type: 'dispatch' | 'terminal' | 'component'
        entity_id: Identifier for the entity (dispatch_id, terminal_id, etc.)
        dispatch_id: Optional dispatch context.
        terminal_id: Optional terminal context.
        component_name: Optional supervised component name.
        failure_detail: Raw failure message from supervisor or transport.
        actor: Caller identity for audit (default 'supervisor').
        metadata: Extra context dict.
        severity_override: Override the contract's default severity.

    Returns:
        The inserted incident row as a dict.
    """
    # Normalize and validate incident class
    if isinstance(incident_class, IncidentClass):
        ic = incident_class
    else:
        ic = validate_incident_class(incident_class)

    contract = RECOVERY_CONTRACTS[ic]
    severity = severity_override or contract.default_severity.value

    incident_id = str(uuid.uuid4())
    now = _now_utc()

    with get_connection(state_dir) as conn:
        conn.execute(
            """
            INSERT INTO incident_log
                (incident_id, incident_class, severity, entity_type, entity_id,
                 dispatch_id, terminal_id, component_name, state, attempt_count,
                 budget_exhausted, escalated, auto_recovery_halted,
                 failure_detail, actor, occurred_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, 0, 0, 0, ?, ?, ?, ?)
            """,
            (
                incident_id,
                ic.value,
                severity,
                entity_type,
                entity_id,
                dispatch_id,
                terminal_id,
                component_name,
                failure_detail,
                actor,
                now,
                _dump(metadata),
            ),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM incident_log WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        return dict(row)


# ---------------------------------------------------------------------------
# Retry budget management
# ---------------------------------------------------------------------------

def get_budget(
    state_dir: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    incident_class: str | IncidentClass,
) -> Dict[str, Any]:
    """Return the retry budget row for an entity+class pair.

    Creates the row from the canonical recovery contract if it doesn't exist yet.
    Always returns a valid dict.
    """
    if isinstance(incident_class, IncidentClass):
        ic = incident_class
    else:
        ic = validate_incident_class(incident_class)

    contract = RECOVERY_CONTRACTS[ic]
    budget = contract.retry_budget
    key = _budget_key(entity_type, entity_id, ic.value)
    now = _now_utc()

    with get_connection(state_dir) as conn:
        existing = conn.execute(
            "SELECT * FROM retry_budgets WHERE budget_key = ?", (key,)
        ).fetchone()

        if existing:
            return dict(existing)

        # Create from contract defaults
        conn.execute(
            """
            INSERT OR IGNORE INTO retry_budgets
                (budget_key, entity_type, entity_id, incident_class,
                 attempts_used, max_retries, cooldown_seconds, backoff_factor,
                 max_cooldown_seconds, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                entity_type,
                entity_id,
                ic.value,
                budget.max_retries,
                budget.cooldown_seconds,
                budget.backoff_factor,
                budget.max_cooldown_seconds,
                now,
                now,
            ),
        )
        conn.commit()

        return dict(
            conn.execute(
                "SELECT * FROM retry_budgets WHERE budget_key = ?", (key,)
            ).fetchone()
        )


def check_budget(
    state_dir: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    incident_class: str | IncidentClass,
) -> Dict[str, Any]:
    """Check whether a recovery attempt is permitted under the current budget.

    Returns a result dict with:
      - allowed (bool): True if a retry is permitted now.
      - reason (str): Human-readable reason if not allowed.
      - attempts_used (int): Current attempt count.
      - max_retries (int): Budget ceiling.
      - in_cooldown (bool): Whether the entity is currently cooling down.
      - next_allowed_at (str | None): Cooldown expiry timestamp if in cooldown.
      - should_escalate (bool): Whether this attempt count triggers escalation.
    """
    budget = get_budget(
        state_dir,
        entity_type=entity_type,
        entity_id=entity_id,
        incident_class=incident_class,
    )

    if isinstance(incident_class, IncidentClass):
        ic = incident_class
    else:
        ic = validate_incident_class(incident_class)

    now_dt = datetime.now(timezone.utc)
    attempts = budget["attempts_used"]
    max_retries = budget["max_retries"]
    in_cooldown = False
    next_allowed_at = budget.get("next_allowed_at")

    if next_allowed_at:
        cooldown_dt = _parse_dt(next_allowed_at)
        if cooldown_dt and now_dt < cooldown_dt:
            in_cooldown = True

    if budget["auto_recovery_halted"]:
        return {
            "allowed": False,
            "reason": "auto_recovery_halted: lease_conflict or repeated_failure_loop requires operator review",
            "attempts_used": attempts,
            "max_retries": max_retries,
            "in_cooldown": in_cooldown,
            "next_allowed_at": next_allowed_at,
            "should_escalate": True,
        }

    if attempts >= max_retries:
        return {
            "allowed": False,
            "reason": f"budget_exhausted: {attempts}/{max_retries} attempts used",
            "attempts_used": attempts,
            "max_retries": max_retries,
            "in_cooldown": False,
            "next_allowed_at": None,
            "should_escalate": True,
        }

    if in_cooldown:
        return {
            "allowed": False,
            "reason": f"in_cooldown: next retry allowed at {next_allowed_at}",
            "attempts_used": attempts,
            "max_retries": max_retries,
            "in_cooldown": True,
            "next_allowed_at": next_allowed_at,
            "should_escalate": should_escalate(ic, attempts),
        }

    return {
        "allowed": True,
        "reason": "ok",
        "attempts_used": attempts,
        "max_retries": max_retries,
        "in_cooldown": False,
        "next_allowed_at": next_allowed_at,
        "should_escalate": should_escalate(ic, attempts),
    }


def consume_budget(
    state_dir: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    incident_class: str | IncidentClass,
    incident_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a recovery attempt against the budget.

    Increments attempt_count, sets the cooldown window, and marks escalation
    or auto_recovery_halted if the contract requires it.

    Also updates the linked incident_log row (if incident_id provided) with
    the new attempt_count and any escalation/exhaustion flags.

    Returns the updated budget row.
    """
    if isinstance(incident_class, IncidentClass):
        ic = incident_class
    else:
        ic = validate_incident_class(incident_class)

    contract = RECOVERY_CONTRACTS[ic]
    key = _budget_key(entity_type, entity_id, ic.value)

    # Ensure budget row exists
    get_budget(
        state_dir,
        entity_type=entity_type,
        entity_id=entity_id,
        incident_class=ic,
    )

    now = _now_utc()
    now_dt = datetime.now(timezone.utc)

    with get_connection(state_dir) as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM retry_budgets WHERE budget_key = ?", (key,)
            ).fetchone()
        )

        new_attempts = row["attempts_used"] + 1
        cooldown = get_cooldown_seconds(ic, new_attempts - 1)
        next_allowed = (now_dt + timedelta(seconds=cooldown)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        ) + "Z" if cooldown > 0 else None

        escalated_at = row.get("escalated_at")
        halt_recovery = bool(row["auto_recovery_halted"])

        if should_escalate(ic, new_attempts):
            if not escalated_at:
                escalated_at = now
        if contract.escalation.halt_auto_recovery and new_attempts >= contract.escalation.escalate_after_retries:
            halt_recovery = True

        conn.execute(
            """
            UPDATE retry_budgets
            SET attempts_used = ?,
                last_attempt_at = ?,
                next_allowed_at = ?,
                escalated_at = ?,
                auto_recovery_halted = ?,
                updated_at = ?
            WHERE budget_key = ?
            """,
            (
                new_attempts,
                now,
                next_allowed,
                escalated_at,
                1 if halt_recovery else 0,
                now,
                key,
            ),
        )

        # Update linked incident row if provided
        if incident_id:
            budget_exhausted = 1 if new_attempts >= row["max_retries"] else 0
            conn.execute(
                """
                UPDATE incident_log
                SET attempt_count = ?,
                    budget_exhausted = ?,
                    escalated = ?,
                    auto_recovery_halted = ?
                WHERE incident_id = ?
                """,
                (
                    new_attempts,
                    budget_exhausted,
                    1 if escalated_at else 0,
                    1 if halt_recovery else 0,
                    incident_id,
                ),
            )

        conn.commit()

        return dict(
            conn.execute(
                "SELECT * FROM retry_budgets WHERE budget_key = ?", (key,)
            ).fetchone()
        )


def reset_budget(
    state_dir: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    incident_class: str | IncidentClass,
    actor: str = "runtime",
) -> Dict[str, Any]:
    """Reset a retry budget to zero (e.g., after a successful recovery).

    Clears attempts, cooldown, escalation, and halt flags.
    Returns the reset budget row.
    """
    if isinstance(incident_class, IncidentClass):
        ic = incident_class
    else:
        ic = validate_incident_class(incident_class)

    key = _budget_key(entity_type, entity_id, ic.value)
    now = _now_utc()

    with get_connection(state_dir) as conn:
        conn.execute(
            """
            UPDATE retry_budgets
            SET attempts_used = 0,
                last_attempt_at = NULL,
                next_allowed_at = NULL,
                escalated_at = NULL,
                auto_recovery_halted = 0,
                updated_at = ?
            WHERE budget_key = ?
            """,
            (now, key),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM retry_budgets WHERE budget_key = ?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Budget not found after reset: {key!r}")
        return dict(row)


# ---------------------------------------------------------------------------
# Incident resolution
# ---------------------------------------------------------------------------

def resolve_incident(
    state_dir: str | Path,
    incident_id: str,
    *,
    actor: str = "supervisor",
) -> Dict[str, Any]:
    """Mark an incident as resolved.

    Returns the updated incident row.
    """
    now = _now_utc()
    with get_connection(state_dir) as conn:
        conn.execute(
            "UPDATE incident_log SET state = 'resolved', resolved_at = ? WHERE incident_id = ?",
            (now, incident_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM incident_log WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Incident not found: {incident_id!r}")
        return dict(row)


def escalate_incident(
    state_dir: str | Path,
    incident_id: str,
    *,
    actor: str = "supervisor",
) -> Dict[str, Any]:
    """Mark an incident as escalated to T0/operator.

    Returns the updated incident row.
    """
    with get_connection(state_dir) as conn:
        conn.execute(
            "UPDATE incident_log SET state = 'escalated', escalated = 1 WHERE incident_id = ?",
            (incident_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM incident_log WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Incident not found: {incident_id!r}")
        return dict(row)


# ---------------------------------------------------------------------------
# Repeated failure loop detection
# ---------------------------------------------------------------------------

def detect_repeated_failure_loop(
    state_dir: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    incident_class: str | IncidentClass,
    threshold: Optional[int] = None,
) -> bool:
    """Return True if the entity has triggered REPEATED_FAILURE_LOOP conditions.

    A repeated failure loop is detected when the same incident class has been
    opened >= threshold times for the same entity. Default threshold comes from
    incident_taxonomy.REPEATED_FAILURE_THRESHOLD.

    This check reads the incident_log, not retry_budgets, so it survives
    budget resets and captures the full history.
    """
    from incident_taxonomy import REPEATED_FAILURE_THRESHOLD as DEFAULT_THRESHOLD

    if threshold is None:
        threshold = DEFAULT_THRESHOLD

    if isinstance(incident_class, IncidentClass):
        ic_value = incident_class.value
    else:
        ic_value = validate_incident_class(incident_class).value

    with get_connection(state_dir) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM incident_log
            WHERE entity_type = ?
              AND entity_id = ?
              AND incident_class = ?
            """,
            (entity_type, entity_id, ic_value),
        ).fetchone()
        count = row["cnt"] if row else 0
        return count >= threshold


# ---------------------------------------------------------------------------
# Incident queries
# ---------------------------------------------------------------------------

def get_active_incidents(
    state_dir: str | Path,
    *,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    dispatch_id: Optional[str] = None,
    terminal_id: Optional[str] = None,
    incident_class: Optional[str | IncidentClass] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Query open and escalated incidents, optionally filtered.

    Returns a list of incident dicts, most recent first.
    """
    clauses = ["state IN ('open', 'escalated')"]
    params: list = []

    if entity_type:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    if entity_id:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    if dispatch_id:
        clauses.append("dispatch_id = ?")
        params.append(dispatch_id)
    if terminal_id:
        clauses.append("terminal_id = ?")
        params.append(terminal_id)
    if incident_class is not None:
        ic_value = incident_class.value if isinstance(incident_class, IncidentClass) else incident_class
        clauses.append("incident_class = ?")
        params.append(ic_value)

    where = "WHERE " + " AND ".join(clauses)
    params.append(limit)

    with get_connection(state_dir) as conn:
        rows = conn.execute(
            f"SELECT * FROM incident_log {where} ORDER BY occurred_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_incident_history(
    state_dir: str | Path,
    *,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    dispatch_id: Optional[str] = None,
    terminal_id: Optional[str] = None,
    incident_class: Optional[str | IncidentClass] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return all incidents (any state) for an entity, most recent first."""
    clauses = []
    params: list = []

    if entity_type:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    if entity_id:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    if dispatch_id:
        clauses.append("dispatch_id = ?")
        params.append(dispatch_id)
    if terminal_id:
        clauses.append("terminal_id = ?")
        params.append(terminal_id)
    if incident_class is not None:
        ic_value = incident_class.value if isinstance(incident_class, IncidentClass) else incident_class
        clauses.append("incident_class = ?")
        params.append(ic_value)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    with get_connection(state_dir) as conn:
        rows = conn.execute(
            f"SELECT * FROM incident_log {where} ORDER BY occurred_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Operator-readable incident summary
# ---------------------------------------------------------------------------

def generate_incident_summary(
    state_dir: str | Path,
    *,
    include_resolved: bool = False,
    limit: int = 200,
) -> Dict[str, Any]:
    """Generate an operator-readable incident summary from canonical state.

    No shell log parsing required. Returns a structured dict with:
      - total_open: count of open incidents
      - total_escalated: count of escalated incidents
      - critical_count: count of CRITICAL severity open incidents
      - budgets_exhausted: count of exhausted budget rows
      - auto_recovery_halted_count: count of halted recovery entities
      - incidents_by_class: {class_name: {open, escalated, resolved}}
      - active_incidents: list of open/escalated incident dicts (most recent first)
      - exhausted_budgets: list of budget rows where attempts_used >= max_retries
      - halted_recoveries: list of budget rows where auto_recovery_halted = 1
      - generated_at: ISO timestamp

    This output is suitable for display by `vnx doctor` and `vnx recover`.
    """
    now = _now_utc()

    with get_connection(state_dir) as conn:
        # Aggregate by class and state
        class_rows = conn.execute(
            """
            SELECT incident_class, state, COUNT(*) AS cnt
            FROM incident_log
            GROUP BY incident_class, state
            """
        ).fetchall()

        incidents_by_class: Dict[str, Dict[str, int]] = {}
        for row in class_rows:
            cls = row["incident_class"]
            state = row["state"]
            if cls not in incidents_by_class:
                incidents_by_class[cls] = {"open": 0, "escalated": 0, "resolved": 0, "dead_lettered": 0}
            incidents_by_class[cls][state] = row["cnt"]

        # Counts
        open_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM incident_log WHERE state = 'open'"
        ).fetchone()["cnt"]

        escalated_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM incident_log WHERE state = 'escalated'"
        ).fetchone()["cnt"]

        critical_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM incident_log WHERE state IN ('open', 'escalated') AND severity = 'critical'"
        ).fetchone()["cnt"]

        # Active incidents
        state_filter = "state IN ('open', 'escalated')" if not include_resolved else "1=1"
        active_rows = conn.execute(
            f"SELECT * FROM incident_log WHERE {state_filter} ORDER BY occurred_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        active_incidents = [dict(r) for r in active_rows]

        # Exhausted budgets
        exhausted_rows = conn.execute(
            "SELECT * FROM retry_budgets WHERE attempts_used >= max_retries ORDER BY updated_at DESC"
        ).fetchall()
        exhausted_budgets = [dict(r) for r in exhausted_rows]

        # Halted recoveries
        halted_rows = conn.execute(
            "SELECT * FROM retry_budgets WHERE auto_recovery_halted = 1 ORDER BY updated_at DESC"
        ).fetchall()
        halted_recoveries = [dict(r) for r in halted_rows]

    return {
        "total_open": open_count,
        "total_escalated": escalated_count,
        "critical_count": critical_count,
        "budgets_exhausted": len(exhausted_budgets),
        "auto_recovery_halted_count": len(halted_recoveries),
        "incidents_by_class": incidents_by_class,
        "active_incidents": active_incidents,
        "exhausted_budgets": exhausted_budgets,
        "halted_recoveries": halted_recoveries,
        "generated_at": now,
    }


# ---------------------------------------------------------------------------
# Cooldown helpers
# ---------------------------------------------------------------------------

def is_in_cooldown(
    state_dir: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    incident_class: str | IncidentClass,
) -> bool:
    """Return True if the entity is currently in a cooldown window.

    Convenience wrapper around check_budget for simple boolean use.
    """
    result = check_budget(
        state_dir,
        entity_type=entity_type,
        entity_id=entity_id,
        incident_class=incident_class,
    )
    return result["in_cooldown"]
