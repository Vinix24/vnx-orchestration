#!/usr/bin/env python3
"""
VNX Worker State Manager — Canonical worker lifecycle state machine.

Implements the worker layer from the Runtime State Machine Contract
(docs/core/130_RUNTIME_STATE_MACHINE_CONTRACT.md):

  - Canonical worker states with deterministic transitions (§3)
  - Heartbeat freshness classification (§4)
  - last_output_at tracking (§4.1)
  - Coordination events for every state change (§8.2)
  - T0-readable state surface without terminal scraping (§6)

The worker layer sits between lease acquisition and lease release,
providing runtime observability that the lease layer cannot.

Key invariant: worker state is only valid while a lease is leased.
If the lease is idle, no worker state exists.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import (
    InvalidTransitionError,
    get_connection,
    init_schema,
)

# ---------------------------------------------------------------------------
# Canonical worker states (§3.1)
# ---------------------------------------------------------------------------

WORKER_STATES = frozenset({
    "initializing",
    "working",
    "idle_between_tasks",
    "stalled",
    "blocked",
    "awaiting_input",
    "exited_clean",
    "exited_bad",
    "resume_unsafe",
})

TERMINAL_WORKER_STATES = frozenset({
    "exited_clean",
    "exited_bad",
    "resume_unsafe",
})

ACTIVE_WORKER_STATES = WORKER_STATES - TERMINAL_WORKER_STATES

# ---------------------------------------------------------------------------
# State transition matrix (§3.2)
# ---------------------------------------------------------------------------

WORKER_TRANSITIONS: Dict[str, frozenset] = {
    "initializing":       frozenset({"working", "stalled", "blocked", "exited_clean", "exited_bad", "resume_unsafe"}),
    "working":            frozenset({"idle_between_tasks", "stalled", "blocked", "awaiting_input", "exited_clean", "exited_bad", "resume_unsafe"}),
    "idle_between_tasks": frozenset({"working", "stalled", "exited_clean", "exited_bad", "resume_unsafe"}),
    "stalled":            frozenset({"working", "exited_bad", "resume_unsafe"}),
    "blocked":            frozenset({"working", "exited_bad", "resume_unsafe"}),
    "awaiting_input":     frozenset({"working", "exited_bad", "resume_unsafe"}),
    "exited_clean":       frozenset(),
    "exited_bad":         frozenset(),
    "resume_unsafe":      frozenset(),
}

# ---------------------------------------------------------------------------
# Heartbeat thresholds (§4.3, Appendix B)
# ---------------------------------------------------------------------------

DEFAULT_HEARTBEAT_INTERVAL = 30
DEFAULT_HEARTBEAT_STALE_THRESHOLD = 90
DEFAULT_HEARTBEAT_DEAD_THRESHOLD = 300
DEFAULT_STARTUP_GRACE_PERIOD = 120
DEFAULT_STALL_THRESHOLD = 180
DEFAULT_IDLE_BETWEEN_TASKS_GRACE = 120
DEFAULT_INTERACTIVE_STALL_MULTIPLIER = 1.5


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class InvalidWorkerStateError(ValueError):
    """Raised when a worker state value is not in the canonical set."""


class InvalidWorkerTransitionError(InvalidTransitionError):
    """Raised when a worker state transition is not permitted."""


def validate_worker_state(state: str) -> None:
    if state not in WORKER_STATES:
        raise InvalidWorkerStateError(
            f"Unknown worker state: {state!r}. Valid: {sorted(WORKER_STATES)}"
        )


def validate_worker_transition(from_state: str, to_state: str) -> None:
    validate_worker_state(from_state)
    validate_worker_state(to_state)
    allowed = WORKER_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidWorkerTransitionError(
            f"Worker transition {from_state!r} -> {to_state!r} is not permitted. "
            f"Allowed from {from_state!r}: {sorted(allowed) or 'none (terminal state)'}"
        )


def is_terminal_worker_state(state: str) -> bool:
    return state in TERMINAL_WORKER_STATES


# ---------------------------------------------------------------------------
# Heartbeat classification (§4.2)
# ---------------------------------------------------------------------------

def classify_heartbeat(
    last_heartbeat_at: Optional[str],
    *,
    now: Optional[datetime] = None,
    stale_threshold: int = DEFAULT_HEARTBEAT_STALE_THRESHOLD,
    dead_threshold: int = DEFAULT_HEARTBEAT_DEAD_THRESHOLD,
) -> str:
    """Classify heartbeat freshness as 'fresh', 'stale', or 'dead'.

    Returns 'dead' if last_heartbeat_at is None (no heartbeat ever received).
    """
    if last_heartbeat_at is None:
        return "dead"

    if now is None:
        now = datetime.now(timezone.utc)

    try:
        hb = datetime.fromisoformat(last_heartbeat_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "dead"

    age = (now - hb).total_seconds()
    if age < stale_threshold:
        return "fresh"
    elif age < dead_threshold:
        return "stale"
    else:
        return "dead"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class WorkerStateResult:
    """Returned by worker state operations."""
    terminal_id: str
    dispatch_id: str
    state: str
    last_output_at: Optional[str]
    state_entered_at: str
    stall_count: int
    blocked_reason: Optional[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "WorkerStateResult":
        return cls(
            terminal_id=row["terminal_id"],
            dispatch_id=row["dispatch_id"],
            state=row["state"],
            last_output_at=row.get("last_output_at"),
            state_entered_at=row["state_entered_at"],
            stall_count=row["stall_count"],
            blocked_reason=row.get("blocked_reason"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _new_event_id() -> str:
    return str(uuid.uuid4())


def _dump(obj: Any) -> str:
    return json.dumps(obj) if obj is not None else "{}"


def _append_worker_event(
    conn,
    *,
    event_type: str,
    terminal_id: str,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
    actor: str = "worker_supervisor",
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Append a worker state coordination event. Returns the event_id."""
    event_id = _new_event_id()
    conn.execute(
        """
        INSERT INTO coordination_events
            (event_id, event_type, entity_type, entity_id,
             from_state, to_state, actor, reason, metadata_json, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event_type,
            "worker",
            terminal_id,
            from_state,
            to_state,
            actor,
            reason,
            _dump(metadata),
            _now_utc(),
        ),
    )
    return event_id


# ---------------------------------------------------------------------------
# WorkerStateManager
# ---------------------------------------------------------------------------

class WorkerStateManager:
    """High-level facade for worker state lifecycle operations.

    Usage::

        mgr = WorkerStateManager(state_dir)

        # On lease acquisition (§8.3 step 1):
        result = mgr.initialize("T1", dispatch_id="d-001")

        # On first output detection:
        result = mgr.transition("T1", "working", reason="first stdout output")

        # Record output events:
        mgr.record_output("T1")

        # On clean exit:
        result = mgr.transition("T1", "exited_clean", reason="exit code 0")

        # On lease release (§8.3 step 4):
        mgr.cleanup("T1")

        # T0 reads state:
        state = mgr.get("T1")
        summary = mgr.get_all_states()
    """

    def __init__(self, state_dir: str | Path, *, auto_init: bool = True) -> None:
        self.state_dir = Path(state_dir)
        self._auto_init = auto_init
        self._initialized = False

    def _ensure_init(self) -> None:
        if not self._initialized and self._auto_init:
            init_schema(self.state_dir)
            self._initialized = True

    # -----------------------------------------------------------------------
    # Lifecycle operations (§8.3)
    # -----------------------------------------------------------------------

    def initialize(
        self,
        terminal_id: str,
        dispatch_id: str,
        *,
        actor: str = "worker_supervisor",
        reason: Optional[str] = None,
    ) -> WorkerStateResult:
        """Create worker state row in 'initializing' for a newly leased terminal.

        If a row already exists for this terminal, it is replaced (new dispatch
        overwrites previous worker state — §8.1 design: single row per terminal).
        """
        self._ensure_init()
        now = _now_utc()

        with get_connection(self.state_dir) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO worker_states
                    (terminal_id, dispatch_id, state, last_output_at,
                     state_entered_at, stall_count, blocked_reason,
                     metadata_json, created_at, updated_at)
                VALUES (?, ?, 'initializing', NULL, ?, 0, NULL, NULL, ?, ?)
                """,
                (terminal_id, dispatch_id, now, now, now),
            )

            _append_worker_event(
                conn,
                event_type="worker_state_changed",
                terminal_id=terminal_id,
                from_state=None,
                to_state="initializing",
                actor=actor,
                reason=reason or f"worker initialized for dispatch {dispatch_id}",
                metadata={"dispatch_id": dispatch_id},
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM worker_states WHERE terminal_id = ?",
                (terminal_id,),
            ).fetchone()

        return WorkerStateResult.from_row(dict(row))

    def _apply_transition(
        self,
        conn,
        current: dict,
        terminal_id: str,
        to_state: str,
        *,
        actor: str,
        reason: Optional[str],
        blocked_reason: Optional[str],
    ) -> WorkerStateResult:
        """Execute the DB update + event for a validated transition."""
        from_state = current["state"]
        now = _now_utc()
        stall_count = current["stall_count"]
        if to_state == "stalled":
            stall_count += 1

        new_blocked_reason = blocked_reason if to_state == "blocked" else None

        conn.execute(
            """
            UPDATE worker_states
            SET state = ?, state_entered_at = ?, stall_count = ?,
                blocked_reason = ?, updated_at = ?
            WHERE terminal_id = ?
            """,
            (to_state, now, stall_count, new_blocked_reason, now, terminal_id),
        )

        event_type = _event_type_for_transition(from_state, to_state)
        _append_worker_event(
            conn,
            event_type=event_type,
            terminal_id=terminal_id,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            reason=reason,
            metadata={
                "dispatch_id": current["dispatch_id"],
                "stall_count": stall_count,
                "blocked_reason": new_blocked_reason,
            },
        )
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM worker_states WHERE terminal_id = ?",
            (terminal_id,),
        ).fetchone()
        return WorkerStateResult.from_row(dict(updated))

    def transition(
        self,
        terminal_id: str,
        to_state: str,
        *,
        actor: str = "worker_supervisor",
        reason: Optional[str] = None,
        blocked_reason: Optional[str] = None,
    ) -> WorkerStateResult:
        """Transition worker to a new state with validation against §3.2.

        For 'blocked' transitions, blocked_reason should describe the obstacle.
        Transitions to 'stalled' auto-increment stall_count.
        """
        self._ensure_init()
        validate_worker_state(to_state)

        with get_connection(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM worker_states WHERE terminal_id = ?",
                (terminal_id,),
            ).fetchone()

            if row is None:
                raise KeyError(
                    f"No worker state for terminal {terminal_id!r}. "
                    "Was initialize() called after lease acquisition?"
                )

            current = dict(row)
            validate_worker_transition(current["state"], to_state)

            return self._apply_transition(
                conn, current, terminal_id, to_state,
                actor=actor, reason=reason, blocked_reason=blocked_reason,
            )

    def record_output(
        self,
        terminal_id: str,
        *,
        actor: str = "output_monitor",
    ) -> WorkerStateResult:
        """Record an output event — updates last_output_at timestamp.

        Per §4.1 H-2: output events update last_output_at independently of
        heartbeat. Heartbeat proves liveness; output proves progress.
        """
        self._ensure_init()
        now = _now_utc()

        with get_connection(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM worker_states WHERE terminal_id = ?",
                (terminal_id,),
            ).fetchone()

            if row is None:
                raise KeyError(f"No worker state for terminal {terminal_id!r}.")

            conn.execute(
                """
                UPDATE worker_states
                SET last_output_at = ?, updated_at = ?
                WHERE terminal_id = ?
                """,
                (now, now, terminal_id),
            )

            _append_worker_event(
                conn,
                event_type="worker_output_detected",
                terminal_id=terminal_id,
                from_state=row["state"],
                to_state=row["state"],
                actor=actor,
                reason="output event detected",
                metadata={
                    "dispatch_id": row["dispatch_id"],
                    "last_output_at": now,
                },
            )
            conn.commit()

            updated = conn.execute(
                "SELECT * FROM worker_states WHERE terminal_id = ?",
                (terminal_id,),
            ).fetchone()

        return WorkerStateResult.from_row(dict(updated))

    def cleanup(
        self,
        terminal_id: str,
        *,
        actor: str = "worker_supervisor",
        reason: Optional[str] = None,
    ) -> None:
        """Remove worker state row on lease release (§8.3 step 4).

        The worker state row is deleted so the terminal can accept a new dispatch.
        A coordination event is appended for audit trail.
        """
        self._ensure_init()

        with get_connection(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM worker_states WHERE terminal_id = ?",
                (terminal_id,),
            ).fetchone()

            if row is None:
                return

            current = dict(row)
            conn.execute(
                "DELETE FROM worker_states WHERE terminal_id = ?",
                (terminal_id,),
            )

            _append_worker_event(
                conn,
                event_type="worker_state_cleaned",
                terminal_id=terminal_id,
                from_state=current["state"],
                to_state=None,
                actor=actor,
                reason=reason or "lease released, worker state cleaned up",
                metadata={
                    "dispatch_id": current["dispatch_id"],
                    "final_state": current["state"],
                    "stall_count": current["stall_count"],
                },
            )
            conn.commit()

    # -----------------------------------------------------------------------
    # Query helpers — T0-readable state surface (§6)
    # -----------------------------------------------------------------------

    def get(self, terminal_id: str) -> Optional[WorkerStateResult]:
        """Return current worker state for a terminal, or None if no active worker."""
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM worker_states WHERE terminal_id = ?",
                (terminal_id,),
            ).fetchone()
        return WorkerStateResult.from_row(dict(row)) if row else None

    def get_all_states(self) -> List[WorkerStateResult]:
        """Return worker state for all terminals with active workers."""
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM worker_states ORDER BY terminal_id"
            ).fetchall()
        return [WorkerStateResult.from_row(dict(r)) for r in rows]

    def get_state_summary(self) -> Dict[str, Any]:
        """Return T0-readable summary of all worker states with heartbeat classification.

        This is the canonical read-model surface for T0 and downstream consumers.
        No terminal scraping required.
        """
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            workers = conn.execute(
                "SELECT * FROM worker_states ORDER BY terminal_id"
            ).fetchall()
            leases = conn.execute(
                "SELECT terminal_id, last_heartbeat_at FROM terminal_leases"
            ).fetchall()

        heartbeat_map = {r["terminal_id"]: r["last_heartbeat_at"] for r in leases}
        now = datetime.now(timezone.utc)

        terminals: Dict[str, Any] = {}
        for row in workers:
            r = dict(row)
            tid = r["terminal_id"]
            hb = heartbeat_map.get(tid)
            hb_class = classify_heartbeat(hb, now=now)

            terminals[tid] = {
                "terminal_id": tid,
                "dispatch_id": r["dispatch_id"],
                "worker_state": r["state"],
                "state_entered_at": r["state_entered_at"],
                "last_output_at": r["last_output_at"],
                "last_heartbeat_at": hb,
                "heartbeat_classification": hb_class,
                "stall_count": r["stall_count"],
                "blocked_reason": r["blocked_reason"],
                "is_terminal": is_terminal_worker_state(r["state"]),
            }

        return {"terminals": terminals}


# ---------------------------------------------------------------------------
# Event type mapping (§8.2)
# ---------------------------------------------------------------------------

def _event_type_for_transition(from_state: str, to_state: str) -> str:
    """Map a state transition to its coordination event type per §8.2."""
    if to_state == "stalled":
        return "worker_stall_detected"
    if to_state == "working" and from_state in ("stalled", "initializing", "blocked", "awaiting_input"):
        return "worker_output_detected"
    if to_state == "blocked":
        return "worker_blocked"
    if to_state in TERMINAL_WORKER_STATES:
        return "worker_exited"
    return "worker_state_changed"


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------

def load_manager(state_dir: str | Path) -> WorkerStateManager:
    """Return a WorkerStateManager for state_dir, auto-initializing the schema."""
    return WorkerStateManager(state_dir, auto_init=True)
