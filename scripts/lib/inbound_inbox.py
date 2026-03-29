#!/usr/bin/env python3
"""
VNX Inbound Inbox — Durable event intake and channel-to-dispatch routing.

External signals (webhooks, notifications, channel events) land in the
inbound_inbox table before being translated into canonical dispatches.
This ensures no work starts without governance (G-R4) and no events are
lost during processing (A-R4, A-R10).

Inbox states:
  received    — event durably persisted, not yet processed
  processing  — translation to dispatch in progress
  dispatched  — canonical dispatch created via broker
  rejected    — event rejected (invalid, duplicate, policy)
  dead_letter — processing failed after retry budget exhausted

Contracts:
  G-R4: Inbound channel events must become canonical dispatches before work starts
  G-R8: No execution-mode change bypasses T0 authority or receipts
  A-R4: Inbound events land in a durable inbox before broker routing
  A-R10: No channel/event intake may directly mutate runtime state without broker registration
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import (
    _append_event,
    _now_utc,
    get_connection,
    register_dispatch,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INBOX_STATES = frozenset({
    "received",
    "processing",
    "dispatched",
    "rejected",
    "dead_letter",
})

INBOX_TRANSITIONS: Dict[str, frozenset] = {
    "received":   frozenset({"processing", "rejected"}),
    "processing": frozenset({"dispatched", "received", "dead_letter"}),
    "dispatched":  frozenset(),
    "rejected":    frozenset(),
    "dead_letter": frozenset(),
}

DEFAULT_MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InboxError(Exception):
    """Base error for inbox operations."""


class DuplicateEventError(InboxError):
    """Raised when an event with the same (channel_id, dedupe_key) already exists."""


class InboxEventNotFoundError(InboxError):
    """Raised when an event_id is not in the inbox."""


class InvalidInboxTransitionError(InboxError):
    """Raised when an inbox state transition is not permitted."""


class InboxRetryExhaustedError(InboxError):
    """Raised when retry budget is exhausted."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class InboxEvent:
    """Parsed inbound inbox row."""
    event_id: str
    channel_id: str
    dedupe_key: str
    state: str
    payload: Dict[str, Any]
    routing_hints: Dict[str, Any]
    dispatch_id: Optional[str]
    attempt_count: int
    max_retries: int
    failure_reason: Optional[str]
    received_at: str
    processed_at: Optional[str]
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> InboxEvent:
        payload = row.get("payload_json", "{}")
        if isinstance(payload, str):
            payload = json.loads(payload)
        hints = row.get("routing_hints_json", "{}")
        if isinstance(hints, str):
            hints = json.loads(hints)
        meta = row.get("metadata_json", "{}")
        if isinstance(meta, str):
            meta = json.loads(meta)
        return cls(
            event_id=row["event_id"],
            channel_id=row["channel_id"],
            dedupe_key=row["dedupe_key"],
            state=row["state"],
            payload=payload,
            routing_hints=hints,
            dispatch_id=row.get("dispatch_id"),
            attempt_count=row.get("attempt_count", 0),
            max_retries=row.get("max_retries", DEFAULT_MAX_RETRIES),
            failure_reason=row.get("failure_reason"),
            received_at=row.get("received_at", ""),
            processed_at=row.get("processed_at"),
            metadata=meta,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in ("dispatched", "rejected", "dead_letter")

    @property
    def retries_remaining(self) -> int:
        return max(0, self.max_retries - self.attempt_count)


@dataclass
class ReceiveResult:
    """Result of receiving an inbound event."""
    event: InboxEvent
    already_existed: bool


@dataclass
class ProcessResult:
    """Result of processing an inbox event into a dispatch."""
    event_id: str
    outcome: str  # "dispatched" | "rejected" | "retry" | "dead_letter"
    dispatch_id: Optional[str] = None
    failure_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_inbox_transition(from_state: str, to_state: str) -> None:
    if from_state not in INBOX_STATES:
        raise InvalidInboxTransitionError(f"Unknown inbox state: {from_state!r}")
    if to_state not in INBOX_STATES:
        raise InvalidInboxTransitionError(f"Unknown inbox state: {to_state!r}")
    allowed = INBOX_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidInboxTransitionError(
            f"Inbox transition {from_state!r} -> {to_state!r} not permitted. "
            f"Allowed from {from_state!r}: {sorted(allowed) or 'none (terminal)'}"
        )


def generate_dedupe_key(channel_id: str, payload: Dict[str, Any]) -> str:
    """Generate a deterministic dedupe key from channel + payload content.

    Uses SHA-256 of the canonical JSON representation. Callers may provide
    their own dedupe_key if they have a natural idempotency key (e.g. webhook ID).
    """
    canonical = json.dumps({"channel_id": channel_id, "payload": payload}, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# InboundInbox
# ---------------------------------------------------------------------------

class InboundInbox:
    """Durable inbox for inbound channel/event payloads.

    All inbound events are persisted before any dispatch creation (G-R4, A-R4).
    Deduplication prevents event storms from creating multiple dispatches.
    Retry semantics handle transient failures without losing events.

    Args:
        state_dir: Directory containing runtime_coordination.db.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self._state_dir = Path(state_dir)

    # ------------------------------------------------------------------
    # Receive (persist inbound event)
    # ------------------------------------------------------------------

    def receive(
        self,
        channel_id: str,
        payload: Dict[str, Any],
        *,
        dedupe_key: Optional[str] = None,
        routing_hints: Optional[Dict[str, Any]] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "inbox",
    ) -> ReceiveResult:
        """Durably persist an inbound event in the inbox.

        Idempotent: if (channel_id, dedupe_key) already exists, returns
        the existing event with already_existed=True.

        Args:
            channel_id:    Channel identifier (e.g. "slack-ops", "webhook-ci").
            payload:       Raw event payload as dict.
            dedupe_key:    Idempotency key. Auto-generated from payload if None.
            routing_hints: Task class hints, priority hints, terminal preferences.
            max_retries:   Maximum processing attempts before dead-lettering.
            metadata:      Arbitrary extra metadata.
            actor:         Actor label for coordination events.

        Returns:
            ReceiveResult with the persisted InboxEvent.
        """
        if dedupe_key is None:
            dedupe_key = generate_dedupe_key(channel_id, payload)

        event_id = str(uuid.uuid4())
        payload_json = json.dumps(payload)
        hints_json = json.dumps(routing_hints or {})
        meta_json = json.dumps(metadata or {})

        with get_connection(self._state_dir) as conn:
            # Check for existing event with same dedupe key
            existing = conn.execute(
                "SELECT * FROM inbound_inbox WHERE channel_id = ? AND dedupe_key = ?",
                (channel_id, dedupe_key),
            ).fetchone()

            if existing:
                event = InboxEvent.from_row(dict(existing))
                self._emit_event(
                    conn, "inbox_dedupe_hit",
                    event_id=event.event_id,
                    reason=f"Duplicate event for channel={channel_id!r} dedupe_key={dedupe_key!r}",
                    actor=actor,
                    metadata={"channel_id": channel_id, "dedupe_key": dedupe_key},
                )
                conn.commit()
                return ReceiveResult(event=event, already_existed=True)

            try:
                conn.execute(
                    """
                    INSERT INTO inbound_inbox
                        (event_id, channel_id, dedupe_key, state, payload_json,
                         routing_hints_json, max_retries, metadata_json)
                    VALUES (?, ?, ?, 'received', ?, ?, ?, ?)
                    """,
                    (event_id, channel_id, dedupe_key, payload_json,
                     hints_json, max_retries, meta_json),
                )
            except sqlite3.IntegrityError:
                # Race condition: another process inserted between our check and insert
                existing = conn.execute(
                    "SELECT * FROM inbound_inbox WHERE channel_id = ? AND dedupe_key = ?",
                    (channel_id, dedupe_key),
                ).fetchone()
                if existing:
                    return ReceiveResult(
                        event=InboxEvent.from_row(dict(existing)),
                        already_existed=True,
                    )
                raise

            self._emit_event(
                conn, "inbox_event_received",
                event_id=event_id,
                to_state="received",
                reason=f"Event received from channel={channel_id!r}",
                actor=actor,
                metadata={
                    "channel_id": channel_id,
                    "dedupe_key": dedupe_key,
                    "payload_size": len(payload_json),
                },
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM inbound_inbox WHERE event_id = ?", (event_id,)
            ).fetchone()

        return ReceiveResult(
            event=InboxEvent.from_row(dict(row)),
            already_existed=False,
        )

    # ------------------------------------------------------------------
    # Process (translate to dispatch)
    # ------------------------------------------------------------------

    def process(
        self,
        event_id: str,
        *,
        dispatch_id_generator: Optional[callable] = None,
        actor: str = "inbox_processor",
    ) -> ProcessResult:
        """Process an inbox event: translate to a canonical dispatch.

        Lifecycle:
          1. Transition received -> processing
          2. Validate payload and extract routing hints
          3. Generate dispatch_id and register dispatch in DB
          4. Transition processing -> dispatched (or retry/dead_letter on failure)
          5. Emit coordination events for each decision

        Args:
            event_id:               Event to process.
            dispatch_id_generator:  Optional callable() -> str for custom dispatch IDs.
            actor:                  Actor label for coordination events.

        Returns:
            ProcessResult with outcome and optional dispatch_id.
        """
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM inbound_inbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise InboxEventNotFoundError(f"Event not found: {event_id!r}")

            event = InboxEvent.from_row(dict(row))

            if event.is_terminal:
                return ProcessResult(
                    event_id=event_id,
                    outcome=event.state,
                    dispatch_id=event.dispatch_id,
                    failure_reason=f"Event already in terminal state: {event.state}",
                )

            # Transition to processing
            if event.state == "received":
                self._transition(conn, event_id, "received", "processing", actor=actor)

            # Increment attempt count
            conn.execute(
                "UPDATE inbound_inbox SET attempt_count = attempt_count + 1 WHERE event_id = ?",
                (event_id,),
            )

            # Validate payload
            validation = self._validate_payload(event)
            if validation is not None:
                return self._reject(conn, event_id, validation, actor=actor)

            # Extract dispatch parameters from payload and routing hints
            dispatch_params = self._extract_dispatch_params(event)

            # Generate dispatch ID
            if dispatch_id_generator:
                dispatch_id = dispatch_id_generator()
            else:
                dispatch_id = self._generate_dispatch_id(event)

            # Register dispatch in the canonical dispatches table
            try:
                dispatch_row = register_dispatch(
                    conn,
                    dispatch_id=dispatch_id,
                    terminal_id=dispatch_params.get("terminal_id"),
                    track=dispatch_params.get("track"),
                    priority=dispatch_params.get("priority", "P2"),
                    pr_ref=dispatch_params.get("pr_ref"),
                    gate=dispatch_params.get("gate"),
                    metadata={
                        "channel_origin": event.channel_id,
                        "inbox_event_id": event.event_id,
                        "routing_hints": event.routing_hints,
                    },
                    actor=actor,
                )

                # Set FP-C fields on dispatch
                task_class = dispatch_params.get("task_class", "channel_response")
                conn.execute(
                    "UPDATE dispatches SET task_class = ?, channel_origin = ? WHERE dispatch_id = ?",
                    (task_class, event.channel_id, dispatch_id),
                )

                # Link dispatch to inbox event
                now = _now_utc()
                conn.execute(
                    "UPDATE inbound_inbox SET dispatch_id = ?, state = 'dispatched', processed_at = ? "
                    "WHERE event_id = ?",
                    (dispatch_id, now, event_id),
                )

                self._emit_event(
                    conn, "inbox_event_dispatched",
                    event_id=event_id,
                    from_state="processing",
                    to_state="dispatched",
                    reason=f"Dispatch {dispatch_id!r} created from channel={event.channel_id!r}",
                    actor=actor,
                    metadata={
                        "dispatch_id": dispatch_id,
                        "channel_id": event.channel_id,
                        "task_class": task_class,
                        "priority": dispatch_params.get("priority", "P2"),
                    },
                )
                conn.commit()

                return ProcessResult(
                    event_id=event_id,
                    outcome="dispatched",
                    dispatch_id=dispatch_id,
                )

            except Exception as exc:
                conn.rollback()
                return self._handle_processing_failure(
                    conn, event_id, event, str(exc), actor=actor,
                )

    # ------------------------------------------------------------------
    # Retry and dead-letter
    # ------------------------------------------------------------------

    def retry(
        self,
        event_id: str,
        *,
        actor: str = "inbox_processor",
    ) -> ProcessResult:
        """Retry processing of a failed inbox event.

        Resets state from processing -> received for reprocessing.
        If retry budget is exhausted, transitions to dead_letter.
        """
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM inbound_inbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise InboxEventNotFoundError(f"Event not found: {event_id!r}")

            event = InboxEvent.from_row(dict(row))

            if event.is_terminal:
                return ProcessResult(
                    event_id=event_id,
                    outcome=event.state,
                    failure_reason=f"Event in terminal state: {event.state}",
                )

            if event.attempt_count >= event.max_retries:
                return self._dead_letter(
                    conn, event_id,
                    f"Retry budget exhausted ({event.attempt_count}/{event.max_retries})",
                    actor=actor,
                )

            # Reset to received for reprocessing
            self._transition(conn, event_id, "processing", "received", actor=actor,
                             reason=f"Retry {event.attempt_count + 1}/{event.max_retries}")
            conn.commit()

        return ProcessResult(event_id=event_id, outcome="retry")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, event_id: str) -> Optional[InboxEvent]:
        """Return a single inbox event or None."""
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM inbound_inbox WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        return InboxEvent.from_row(dict(row))

    def list_pending(self, limit: int = 50) -> List[InboxEvent]:
        """Return events in 'received' state, oldest first."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM inbound_inbox WHERE state = 'received' "
                "ORDER BY received_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [InboxEvent.from_row(dict(r)) for r in rows]

    def list_by_channel(self, channel_id: str, limit: int = 50) -> List[InboxEvent]:
        """Return events for a specific channel."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM inbound_inbox WHERE channel_id = ? "
                "ORDER BY received_at DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        return [InboxEvent.from_row(dict(r)) for r in rows]

    def list_dead_letters(self, limit: int = 50) -> List[InboxEvent]:
        """Return dead-lettered events for operator review."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM inbound_inbox WHERE state = 'dead_letter' "
                "ORDER BY received_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [InboxEvent.from_row(dict(r)) for r in rows]

    def count_by_state(self) -> Dict[str, int]:
        """Return event counts grouped by state."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) as cnt FROM inbound_inbox GROUP BY state"
            ).fetchall()
        return {r["state"]: r["cnt"] for r in rows}

    # ------------------------------------------------------------------
    # Reject
    # ------------------------------------------------------------------

    def reject(
        self,
        event_id: str,
        reason: str,
        *,
        actor: str = "inbox",
    ) -> ProcessResult:
        """Explicitly reject an inbox event."""
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM inbound_inbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise InboxEventNotFoundError(f"Event not found: {event_id!r}")
            return self._reject(conn, event_id, reason, actor=actor)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_payload(self, event: InboxEvent) -> Optional[str]:
        """Validate event payload. Returns rejection reason or None if valid."""
        if not event.payload:
            return "Empty payload"
        if not event.channel_id:
            return "Missing channel_id"
        return None

    def _extract_dispatch_params(self, event: InboxEvent) -> Dict[str, Any]:
        """Extract dispatch registration parameters from event payload and routing hints."""
        hints = event.routing_hints or {}
        payload = event.payload or {}

        return {
            "task_class": hints.get("task_class", "channel_response"),
            "terminal_id": hints.get("terminal_id"),
            "track": hints.get("track"),
            "priority": hints.get("priority", payload.get("priority", "P2")),
            "pr_ref": hints.get("pr_ref", payload.get("pr_ref")),
            "gate": hints.get("gate", payload.get("gate")),
        }

    def _generate_dispatch_id(self, event: InboxEvent) -> str:
        """Generate a dispatch ID from event metadata."""
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%d-%H%M%S")
        short_channel = event.channel_id[:20].replace("/", "-").replace(" ", "-")
        short_id = event.event_id[:8]
        return f"{ts}-ch-{short_channel}-{short_id}"

    def _transition(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        from_state: str,
        to_state: str,
        *,
        actor: str,
        reason: Optional[str] = None,
    ) -> None:
        """Transition inbox event state with validation and event emission."""
        _validate_inbox_transition(from_state, to_state)

        now = _now_utc()
        update_fields = "state = ?"
        params: list = [to_state]

        if to_state in ("dispatched", "rejected", "dead_letter"):
            update_fields += ", processed_at = ?"
            params.append(now)

        params.append(event_id)
        conn.execute(
            f"UPDATE inbound_inbox SET {update_fields} WHERE event_id = ?",
            params,
        )

        self._emit_event(
            conn, f"inbox_event_{to_state}",
            event_id=event_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason or f"transition {from_state} -> {to_state}",
            actor=actor,
        )

    def _reject(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        reason: str,
        *,
        actor: str,
    ) -> ProcessResult:
        """Reject an event and record the reason."""
        now = _now_utc()
        conn.execute(
            "UPDATE inbound_inbox SET state = 'rejected', failure_reason = ?, processed_at = ? "
            "WHERE event_id = ?",
            (reason, now, event_id),
        )
        self._emit_event(
            conn, "inbox_event_rejected",
            event_id=event_id,
            to_state="rejected",
            reason=reason,
            actor=actor,
        )
        conn.commit()
        return ProcessResult(event_id=event_id, outcome="rejected", failure_reason=reason)

    def _dead_letter(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        reason: str,
        *,
        actor: str,
    ) -> ProcessResult:
        """Move event to dead letter and record reason."""
        now = _now_utc()
        conn.execute(
            "UPDATE inbound_inbox SET state = 'dead_letter', failure_reason = ?, processed_at = ? "
            "WHERE event_id = ?",
            (reason, now, event_id),
        )
        self._emit_event(
            conn, "inbox_event_dead_letter",
            event_id=event_id,
            to_state="dead_letter",
            reason=reason,
            actor=actor,
            metadata={"escalation": "T0"},
        )
        conn.commit()
        return ProcessResult(event_id=event_id, outcome="dead_letter", failure_reason=reason)

    def _handle_processing_failure(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        event: InboxEvent,
        error_msg: str,
        *,
        actor: str,
    ) -> ProcessResult:
        """Handle a processing failure: retry or dead-letter."""
        if event.attempt_count >= event.max_retries:
            return self._dead_letter(
                conn, event_id,
                f"Processing failed after {event.attempt_count} attempts: {error_msg}",
                actor=actor,
            )

        # Return to received for retry
        conn.execute(
            "UPDATE inbound_inbox SET state = 'received', failure_reason = ? WHERE event_id = ?",
            (error_msg, event_id),
        )
        self._emit_event(
            conn, "inbox_event_retry",
            event_id=event_id,
            from_state="processing",
            to_state="received",
            reason=f"Processing failed, will retry: {error_msg}",
            actor=actor,
            metadata={"attempt_count": event.attempt_count, "max_retries": event.max_retries},
        )
        conn.commit()
        return ProcessResult(event_id=event_id, outcome="retry", failure_reason=error_msg)

    def _emit_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        *,
        event_id: str,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        reason: Optional[str] = None,
        actor: str = "inbox",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a coordination event for inbox operations."""
        try:
            _append_event(
                conn,
                event_type=event_type,
                entity_type="inbox_event",
                entity_id=event_id,
                from_state=from_state,
                to_state=to_state,
                actor=actor,
                reason=reason,
                metadata=metadata,
            )
        except Exception:
            pass  # Non-fatal: inbox operation still succeeds
