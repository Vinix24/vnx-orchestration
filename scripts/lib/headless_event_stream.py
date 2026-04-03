#!/usr/bin/env python3
"""Structured headless event stream with artifact correlation.

Provides a coherent, machine-readable timeline for headless session
execution. Events carry correlation keys linking session identity,
attempt identity, and artifact paths.

Event types (canonical order):
  session_created -> subprocess_launched -> heartbeat* ->
  (artifact_materialized*) -> session_completed | session_failed | session_timed_out

Correlation keys:
  session_id:  terminal_id (canonical)
  attempt_id:  terminal_id + attempt_number
  dispatch_id: from session config
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CorrelationKeys:
    """Links event to session, attempt, and dispatch identity."""
    session_id: str
    attempt_number: int
    dispatch_id: str = ""

    @property
    def attempt_id(self) -> str:
        return f"{self.session_id}-attempt-{self.attempt_number}"


@dataclass
class HeadlessEvent:
    """A single structured event in the headless timeline."""
    event_type: str
    timestamp: str
    correlation: CorrelationKeys
    details: Dict[str, Any] = field(default_factory=dict)
    artifact_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "session_id": self.correlation.session_id,
            "attempt_id": self.correlation.attempt_id,
            "attempt_number": self.correlation.attempt_number,
            "dispatch_id": self.correlation.dispatch_id,
        }
        if self.details:
            d["details"] = self.details
        if self.artifact_path:
            d["artifact_path"] = self.artifact_path
        return d

    def to_ndjson(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


VALID_EVENT_TYPES = frozenset({
    "session_created", "subprocess_launched", "heartbeat",
    "artifact_materialized", "session_completed", "session_failed",
    "session_timed_out",
})

TERMINAL_EVENT_TYPES = frozenset({
    "session_completed", "session_failed", "session_timed_out",
})

CANONICAL_ORDER = [
    "session_created", "subprocess_launched", "heartbeat",
    "artifact_materialized", "session_completed", "session_failed",
    "session_timed_out",
]


class HeadlessEventStream:
    """Collects and validates a structured event timeline for one session."""

    def __init__(self, session_id: str, dispatch_id: str = "",
                 attempt_number: int = 1) -> None:
        self._correlation = CorrelationKeys(
            session_id=session_id, attempt_number=attempt_number,
            dispatch_id=dispatch_id,
        )
        self._events: List[HeadlessEvent] = []
        self._artifacts: Dict[str, str] = {}  # artifact_name -> path
        self._terminated = False

    @property
    def correlation(self) -> CorrelationKeys:
        return self._correlation

    @property
    def events(self) -> List[HeadlessEvent]:
        return list(self._events)

    @property
    def artifacts(self) -> Dict[str, str]:
        return dict(self._artifacts)

    @property
    def is_terminated(self) -> bool:
        return self._terminated

    def emit(self, event_type: str, details: Optional[Dict[str, Any]] = None,
             artifact_path: Optional[str] = None) -> HeadlessEvent:
        """Emit a structured event. Terminal events close the stream."""
        if self._terminated:
            raise ValueError(f"Stream terminated; cannot emit {event_type}")
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"Unknown event type: {event_type}")
        event = HeadlessEvent(
            event_type=event_type, timestamp=_now_iso(),
            correlation=self._correlation, details=details or {},
            artifact_path=artifact_path,
        )
        self._events.append(event)
        if event_type in TERMINAL_EVENT_TYPES:
            self._terminated = True
        return event

    def record_artifact(self, name: str, path: str) -> HeadlessEvent:
        """Record an artifact and emit artifact_materialized event."""
        event = self.emit("artifact_materialized",
            details={"artifact_name": name}, artifact_path=path)
        self._artifacts[name] = path
        return event

    def timeline(self) -> List[Dict[str, Any]]:
        """Return the full timeline as a list of dicts."""
        return [e.to_dict() for e in self._events]

    def to_ndjson(self) -> str:
        """Serialize full timeline as NDJSON."""
        return "\n".join(e.to_ndjson() for e in self._events)

    def validate_order(self) -> List[str]:
        """Validate events follow canonical ordering. Returns violations."""
        violations: List[str] = []
        if not self._events:
            return violations
        if self._events[0].event_type != "session_created":
            violations.append(f"First event must be session_created, got {self._events[0].event_type}")
        seen_terminal = False
        max_phase = -1
        for i, event in enumerate(self._events):
            if seen_terminal:
                violations.append(f"Event {event.event_type} at position {i} after terminal event")
            if event.event_type in TERMINAL_EVENT_TYPES:
                seen_terminal = True
            if event.event_type in CANONICAL_ORDER:
                phase = CANONICAL_ORDER.index(event.event_type)
                if phase < max_phase:
                    violations.append(
                        f"Event {event.event_type} at position {i} violates canonical order "
                        f"(after {CANONICAL_ORDER[max_phase]})"
                    )
                else:
                    max_phase = phase
        return violations

    def validate_correlation(self) -> List[str]:
        """Validate all events share consistent correlation keys."""
        violations: List[str] = []
        for i, event in enumerate(self._events):
            if event.correlation.session_id != self._correlation.session_id:
                violations.append(f"Event {i} session_id mismatch")
            if event.correlation.attempt_number != self._correlation.attempt_number:
                violations.append(f"Event {i} attempt_number mismatch")
            if event.correlation.dispatch_id != self._correlation.dispatch_id:
                violations.append(f"Event {i} dispatch_id mismatch")
        return violations
