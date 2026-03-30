#!/usr/bin/env python3
"""
VNX Headless Run Registry — Durable identity, heartbeat, and output tracking
for headless CLI runs.

Contract reference: docs/HEADLESS_RUN_CONTRACT.md

Provides:
  - Run creation with full identity fields (Section 1.2)
  - Lifecycle state transitions (Section 2)
  - Heartbeat and last-output timestamp updates (Section 3.2, 3.3)
  - Staleness and hang detection queries (Section 5.1: O-3, O-4)
  - Query helpers for operator inspection (Section 5.1: O-1 through O-10)

Invariants:
  I-1: run_id is assigned exactly once and never reused
  I-2: Each run links to exactly one dispatch_id and attempt_id
  I-3: Retry creates a new run_id and attempt_id under the same dispatch_id
  I-4: run_id appears in all coordination events for this run

State machine (no backward transitions):
  init -> running -> completing -> succeeded
                  -> failing    -> failed
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from runtime_coordination import (
    _append_event,
    _dump,
    _now_utc,
    get_connection,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RUN_STATES = frozenset({
    "init",
    "running",
    "completing",
    "failing",
    "succeeded",
    "failed",
})

TERMINAL_STATES = frozenset({"succeeded", "failed"})

RUN_TRANSITIONS: Dict[str, frozenset] = {
    "init":       frozenset({"running"}),
    "running":    frozenset({"completing", "failing"}),
    "completing": frozenset({"succeeded"}),
    "failing":    frozenset({"failed"}),
    "succeeded":  frozenset(),
    "failed":     frozenset(),
}

FAILURE_CLASSES = frozenset({
    "SUCCESS",
    "TOOL_FAIL",
    "INFRA_FAIL",
    "TIMEOUT",
    "NO_OUTPUT",
    "INTERRUPTED",
    "PROMPT_ERR",
    "UNKNOWN",
})

DEFAULT_HEARTBEAT_INTERVAL = 30   # seconds
DEFAULT_OUTPUT_HANG_THRESHOLD = 120  # seconds


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RunRegistryError(Exception):
    """Base error for run registry operations."""


class InvalidRunStateError(RunRegistryError):
    """Raised when a state value is not in the canonical set."""


class InvalidRunTransitionError(RunRegistryError):
    """Raised when a state transition is not permitted."""


class RunNotFoundError(RunRegistryError):
    """Raised when a run_id is not found in the registry."""


class InvalidFailureClassError(RunRegistryError):
    """Raised when an unrecognized failure class is provided."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HeadlessRun:
    """In-memory representation of a headless run record."""
    run_id: str
    dispatch_id: str
    attempt_id: str
    target_id: str
    target_type: str
    task_class: str
    terminal_id: Optional[str] = None
    pid: Optional[int] = None
    pgid: Optional[int] = None
    state: str = "init"
    failure_class: Optional[str] = None
    exit_code: Optional[int] = None
    started_at: Optional[str] = None
    subprocess_started_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    last_output_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    log_artifact_path: Optional[str] = None
    output_artifact_path: Optional[str] = None
    receipt_id: Optional[str] = None
    metadata_json: str = "{}"

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def is_stale(self, heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL) -> bool:
        """Stale = heartbeat_at older than 2x interval while running (Section 3.2)."""
        if self.state != "running" or not self.heartbeat_at:
            return False
        threshold = heartbeat_interval * 2
        return _seconds_since(self.heartbeat_at) > threshold

    @property
    def is_hung(self) -> bool:
        """Hung = last_output_at older than threshold while running (Section 3.3)."""
        if self.state != "running" or not self.last_output_at:
            return False
        return _seconds_since(self.last_output_at) > DEFAULT_OUTPUT_HANG_THRESHOLD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seconds_since(iso_ts: str) -> float:
    """Return seconds elapsed since an ISO8601 timestamp."""
    ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _new_run_id() -> str:
    return str(uuid.uuid4())


def _row_to_run(row) -> HeadlessRun:
    """Convert a sqlite3.Row to HeadlessRun."""
    d = dict(row)
    d.pop("id", None)
    return HeadlessRun(**d)


def _validate_state(state: str) -> None:
    if state not in RUN_STATES:
        raise InvalidRunStateError(
            f"Unknown run state: {state!r}. Valid: {sorted(RUN_STATES)}"
        )


def _validate_transition(from_state: str, to_state: str) -> None:
    _validate_state(from_state)
    _validate_state(to_state)
    allowed = RUN_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidRunTransitionError(
            f"Run transition {from_state!r} -> {to_state!r} not permitted. "
            f"Allowed from {from_state!r}: {sorted(allowed) or 'none (terminal)'}"
        )


def _validate_failure_class(fc: str) -> None:
    if fc not in FAILURE_CLASSES:
        raise InvalidFailureClassError(
            f"Unknown failure class: {fc!r}. Valid: {sorted(FAILURE_CLASSES)}"
        )


# ---------------------------------------------------------------------------
# HeadlessRunRegistry
# ---------------------------------------------------------------------------

class HeadlessRunRegistry:
    """Registry for headless run state — create, transition, heartbeat, query.

    All mutations go through the runtime_coordination.db and emit
    coordination_events for the audit trail.

    Args:
        state_dir: Directory containing runtime_coordination.db.
        heartbeat_interval: Seconds between expected heartbeats (default 30).
        output_hang_threshold: Seconds of silence before hang detection (default 120).
    """

    def __init__(
        self,
        state_dir,
        *,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        output_hang_threshold: int = DEFAULT_OUTPUT_HANG_THRESHOLD,
    ) -> None:
        from pathlib import Path
        self._state_dir = Path(state_dir)
        self.heartbeat_interval = heartbeat_interval
        self.output_hang_threshold = output_hang_threshold

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_run(
        self,
        *,
        dispatch_id: str,
        attempt_id: str,
        target_id: str,
        target_type: str,
        task_class: str,
        terminal_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> HeadlessRun:
        """Create a new headless run in 'init' state.

        Assigns a unique run_id (I-1) and persists the identity fields
        before the subprocess starts.
        """
        run_id = _new_run_id()
        now = _now_utc()

        with get_connection(self._state_dir) as conn:
            conn.execute(
                """
                INSERT INTO headless_runs
                    (run_id, dispatch_id, attempt_id, target_id, target_type,
                     task_class, terminal_id, state, started_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'init', ?, ?)
                """,
                (
                    run_id, dispatch_id, attempt_id, target_id, target_type,
                    task_class, terminal_id, now, _dump(metadata),
                ),
            )
            _append_event(
                conn,
                event_type="headless_run_transition",
                entity_type="headless_run",
                entity_id=run_id,
                from_state=None,
                to_state="init",
                actor="headless_adapter",
                reason="run created",
                metadata={
                    "dispatch_id": dispatch_id,
                    "attempt_id": attempt_id,
                    "target_id": target_id,
                    "target_type": target_type,
                    "task_class": task_class,
                },
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM headless_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

        return _row_to_run(row)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def transition(
        self,
        run_id: str,
        to_state: str,
        *,
        pid: Optional[int] = None,
        pgid: Optional[int] = None,
        exit_code: Optional[int] = None,
        failure_class: Optional[str] = None,
        log_artifact_path: Optional[str] = None,
        output_artifact_path: Optional[str] = None,
        receipt_id: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        actor: str = "headless_adapter",
        reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> HeadlessRun:
        """Transition a run to a new lifecycle state.

        Validates the transition, updates the record, and emits a
        coordination event per Section 2.3.
        """
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM headless_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"Run not found: {run_id!r}")

            from_state = row["state"]
            _validate_transition(from_state, to_state)

            if failure_class is not None:
                _validate_failure_class(failure_class)

            now = _now_utc()
            updates = ["state = ?"]
            params: list = [to_state]

            if to_state == "running":
                updates.append("subprocess_started_at = ?")
                params.append(now)
                updates.append("heartbeat_at = ?")
                params.append(now)
                updates.append("last_output_at = ?")
                params.append(now)

            if pid is not None:
                updates.append("pid = ?")
                params.append(pid)
            if pgid is not None:
                updates.append("pgid = ?")
                params.append(pgid)
            if exit_code is not None:
                updates.append("exit_code = ?")
                params.append(exit_code)
            if failure_class is not None:
                updates.append("failure_class = ?")
                params.append(failure_class)
            if log_artifact_path is not None:
                updates.append("log_artifact_path = ?")
                params.append(log_artifact_path)
            if output_artifact_path is not None:
                updates.append("output_artifact_path = ?")
                params.append(output_artifact_path)
            if receipt_id is not None:
                updates.append("receipt_id = ?")
                params.append(receipt_id)
            if duration_seconds is not None:
                updates.append("duration_seconds = ?")
                params.append(duration_seconds)

            if to_state in TERMINAL_STATES:
                updates.append("completed_at = ?")
                params.append(now)

            params.append(run_id)
            conn.execute(
                f"UPDATE headless_runs SET {', '.join(updates)} WHERE run_id = ?",
                params,
            )

            event_metadata = {
                "dispatch_id": row["dispatch_id"],
                "attempt_id": row["attempt_id"],
            }
            if pid is not None:
                event_metadata["pid"] = pid
            if exit_code is not None:
                event_metadata["exit_code"] = exit_code
            if failure_class is not None:
                event_metadata["failure_class"] = failure_class
            if duration_seconds is not None:
                event_metadata["duration_seconds"] = duration_seconds
            if metadata:
                event_metadata.update(metadata)

            _append_event(
                conn,
                event_type="headless_run_transition",
                entity_type="headless_run",
                entity_id=run_id,
                from_state=from_state,
                to_state=to_state,
                actor=actor,
                reason=reason or f"transition to {to_state}",
                metadata=event_metadata,
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM headless_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

        return _row_to_run(row)

    # ------------------------------------------------------------------
    # Heartbeat and output tracking
    # ------------------------------------------------------------------

    def update_heartbeat(self, run_id: str) -> HeadlessRun:
        """Update heartbeat_at for a running run (Section 3.2).

        Called by the adapter polling subprocess liveness.
        """
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM headless_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"Run not found: {run_id!r}")
            if row["state"] != "running":
                raise InvalidRunStateError(
                    f"Cannot heartbeat run {run_id!r} in state {row['state']!r} (must be 'running')"
                )

            now = _now_utc()
            conn.execute(
                "UPDATE headless_runs SET heartbeat_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM headless_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _row_to_run(row)

    def update_last_output(self, run_id: str) -> HeadlessRun:
        """Update last_output_at for a running run (Section 3.3).

        Called when the subprocess writes to stdout or stderr.
        """
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM headless_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"Run not found: {run_id!r}")
            if row["state"] != "running":
                raise InvalidRunStateError(
                    f"Cannot update output timestamp for run {run_id!r} in state {row['state']!r}"
                )

            now = _now_utc()
            conn.execute(
                "UPDATE headless_runs SET last_output_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM headless_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _row_to_run(row)

    # ------------------------------------------------------------------
    # Queries (Section 5.1: O-1 through O-10)
    # ------------------------------------------------------------------

    def get(self, run_id: str) -> Optional[HeadlessRun]:
        """Return a run by run_id, or None."""
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM headless_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _row_to_run(row) if row else None

    def get_or_raise(self, run_id: str) -> HeadlessRun:
        """Return a run by run_id, or raise RunNotFoundError."""
        run = self.get(run_id)
        if run is None:
            raise RunNotFoundError(f"Run not found: {run_id!r}")
        return run

    def list_active(self) -> List[HeadlessRun]:
        """O-1: List active headless runs (state = running)."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM headless_runs WHERE state = 'running' ORDER BY started_at DESC"
            ).fetchall()
        return [_row_to_run(r) for r in rows]

    def list_by_state(self, state: str) -> List[HeadlessRun]:
        """List runs in a specific state."""
        _validate_state(state)
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM headless_runs WHERE state = ? ORDER BY started_at DESC",
                (state,),
            ).fetchall()
        return [_row_to_run(r) for r in rows]

    def list_by_dispatch(self, dispatch_id: str) -> List[HeadlessRun]:
        """O-8: List all runs for a dispatch (trace run back to dispatch)."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM headless_runs WHERE dispatch_id = ? ORDER BY started_at DESC",
                (dispatch_id,),
            ).fetchall()
        return [_row_to_run(r) for r in rows]

    def list_stale(self) -> List[HeadlessRun]:
        """O-4: Find running runs with stale heartbeats (Section 3.2).

        A run is stale when heartbeat_at is older than 2 * heartbeat_interval
        while still in running state.
        """
        threshold = self.heartbeat_interval * 2
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                """
                SELECT * FROM headless_runs
                WHERE state = 'running'
                  AND heartbeat_at IS NOT NULL
                  AND (julianday('now') - julianday(heartbeat_at)) * 86400 > ?
                ORDER BY heartbeat_at ASC
                """,
                (threshold,),
            ).fetchall()
        return [_row_to_run(r) for r in rows]

    def list_hung(self) -> List[HeadlessRun]:
        """O-3: Find running runs with no recent output (Section 3.3).

        A run is a hang candidate when last_output_at is older than the
        output_hang_threshold while still in running state.
        """
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                """
                SELECT * FROM headless_runs
                WHERE state = 'running'
                  AND last_output_at IS NOT NULL
                  AND (julianday('now') - julianday(last_output_at)) * 86400 > ?
                ORDER BY last_output_at ASC
                """,
                (self.output_hang_threshold,),
            ).fetchall()
        return [_row_to_run(r) for r in rows]

    def list_recent(self, limit: int = 20) -> List[HeadlessRun]:
        """List most recent runs regardless of state."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM headless_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_run(r) for r in rows]
