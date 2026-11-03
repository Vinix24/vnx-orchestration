"""dispatch_lifecycle_tracker.py — Wave 5 PR-5.6: dispatch lifecycle tracker.

Maps (project_id, dispatch_id) → completion-receipt. Consumes a ReceiptTail
stream and tracks dispatch state transitions:

    PENDING → RUNNING → COMPLETED | FAILED | TIMEOUT

Per-project isolation is enforced via TrackingKey: the composite key prevents
a second project sharing the same dispatch_id from overwriting another project's
tracking entry. Events from mismatched projects are silently ignored.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterator, Optional

from scripts.control_centre.receipt_tail import MergedEvent, ReceiptTail
from scripts.lib.completion_reading import (
    COMPLETION_TYPES,
    completion_outcome,
    event_type_of,
    is_non_outcome_lifecycle,
    is_v3_end_line,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrackingKey:
    project_id: str
    dispatch_id: str


class DispatchStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


@dataclass
class DispatchOutcome:
    dispatch_id: str
    project_id: str
    status: DispatchStatus
    receipt: Optional[Dict] = None
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status == DispatchStatus.COMPLETED


@dataclass
class _DispatchEntry:
    dispatch_id: str
    project_id: str
    status: DispatchStatus = DispatchStatus.PENDING
    receipt: Optional[Dict] = None
    error: Optional[str] = None
    event: threading.Event = field(default_factory=threading.Event)


_RUNNING_EVENT_TYPES = frozenset({
    "worker_started",
    "dispatch_accepted",
    "dispatch_started",
    "task_started",
    "running",
})

# The tracker's own three literals are the only unconditional completions: they
# carry no governed outcome of their own. Every member of the canonical
# completion family (``COMPLETION_TYPES``) is classified through
# ``completion_outcome`` in _classify_receipt_status instead — a
# ``subprocess_completion`` that was ``blocked`` is a failure, and one with no
# status is no success at all (ADR-045 §4, audit-spoor-kern K-2c).
_COMPLETED_EVENT_TYPES = frozenset({
    "dispatch_completed",
    "quality_gate_verification",
    "receipt_written",
})

_FAILED_EVENT_TYPES = frozenset({
    "task_failed",
    "dispatch_failed",
    "worker_failed",
    "timeout",
})


def _classify_receipt_status(raw: Dict) -> Optional[DispatchStatus]:
    # ADR-045 §4: a v3 end line's outcome is its end_state, classified — a
    # dispatch that ended `failed` must not read as completed. No governed
    # outcome is no terminal status: never a success.
    if is_v3_end_line(raw):
        outcome = completion_outcome(raw)
        if outcome == "success":
            return DispatchStatus.COMPLETED
        if outcome == "failure":
            return DispatchStatus.FAILED
        return None

    # ADR-045 §2: a start line is RUNNING; a report line is never an outcome.
    event_type = event_type_of(raw)
    if is_non_outcome_lifecycle(raw):
        if event_type == "dispatch_started":
            return DispatchStatus.RUNNING
        return None

    # ADR-045 §4, K-2c: every canonical completion type is classified before it
    # counts. ``blocked``/``failure`` is a failure, ``timeout`` or an unknown or
    # missing status is no governed outcome at all — never COMPLETED. Returning
    # None leaves the entry open, so the next terminal receipt still lands.
    if event_type in COMPLETION_TYPES:
        outcome = completion_outcome(raw)
        if outcome == "success":
            return DispatchStatus.COMPLETED
        if outcome == "failure":
            return DispatchStatus.FAILED
        return None

    status_field = (raw.get("status") or "").lower()
    if status_field in ("success", "completed", "ok"):
        return DispatchStatus.COMPLETED
    if status_field in ("failure", "failed", "error"):
        return DispatchStatus.FAILED

    if event_type in _COMPLETED_EVENT_TYPES:
        return DispatchStatus.COMPLETED
    if event_type in _FAILED_EVENT_TYPES:
        return DispatchStatus.FAILED
    if event_type in _RUNNING_EVENT_TYPES:
        return DispatchStatus.RUNNING
    return None


class DispatchLifecycleTracker:
    """Track dispatch lifecycle via ReceiptTail event stream.

    Thread-safe. Multiple dispatches can be tracked concurrently — see
    ``test_parallel_dispatches_no_crosstalk`` for the acceptance criterion.

    Entry keys are ``TrackingKey(project_id, dispatch_id)`` tuples. Two
    projects that happen to share a dispatch_id are tracked independently.

    The tracker consumes events from ``receipt_tail.stream()`` in a background
    thread. ``track()`` blocks until a completion event arrives or timeout
    expires.
    """

    def __init__(self, receipt_tail: ReceiptTail) -> None:
        self._tail = receipt_tail
        self._entries: Dict[TrackingKey, _DispatchEntry] = {}
        self._lock = threading.Lock()
        self._consumer_thread: Optional[threading.Thread] = None
        self._started = False

    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        t = threading.Thread(target=self._consume_loop, daemon=True, name="lifecycle-tracker")
        t.start()
        self._consumer_thread = t

    def _consume_loop(self) -> None:
        try:
            for event in self._tail.stream():
                self._apply_event(event)
        except Exception as exc:
            log.error("lifecycle_tracker: consumer loop crashed: %s", exc)

    def _apply_event(self, event: MergedEvent) -> None:
        dispatch_id = event.dispatch_id
        if not dispatch_id:
            return

        key = TrackingKey(project_id=event.project_id, dispatch_id=dispatch_id)

        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            if entry.status in (
                DispatchStatus.COMPLETED,
                DispatchStatus.FAILED,
                DispatchStatus.TIMEOUT,
            ):
                return

        new_status = _classify_receipt_status(event.raw)
        if new_status is None:
            return

        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            if entry.status in (
                DispatchStatus.COMPLETED,
                DispatchStatus.FAILED,
                DispatchStatus.TIMEOUT,
            ):
                return
            entry.status = new_status
            entry.receipt = event.raw
            if new_status in (DispatchStatus.COMPLETED, DispatchStatus.FAILED):
                entry.event.set()

    def register(self, dispatch_id: str, project_id: str) -> None:
        """Register a dispatch for tracking before dropping it in pending/."""
        key = TrackingKey(project_id=project_id, dispatch_id=dispatch_id)
        with self._lock:
            if key not in self._entries:
                self._entries[key] = _DispatchEntry(
                    dispatch_id=dispatch_id,
                    project_id=project_id,
                )
        self._ensure_started()

    def status(self, dispatch_id: str, project_id: str) -> DispatchStatus:
        """Non-blocking: return current state."""
        key = TrackingKey(project_id=project_id, dispatch_id=dispatch_id)
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return DispatchStatus.PENDING
        return entry.status

    def track(
        self,
        dispatch_id: str,
        project_id: str,
        timeout_seconds: float = 600,
    ) -> DispatchOutcome:
        """Block until dispatch completes or timeout. Returns DispatchOutcome."""
        key = TrackingKey(project_id=project_id, dispatch_id=dispatch_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _DispatchEntry(
                    dispatch_id=dispatch_id,
                    project_id=project_id,
                )
                self._entries[key] = entry

        self._ensure_started()

        signaled = entry.event.wait(timeout=timeout_seconds)

        with self._lock:
            entry = self._entries[key]
            if not signaled and entry.status not in (
                DispatchStatus.COMPLETED,
                DispatchStatus.FAILED,
            ):
                entry.status = DispatchStatus.TIMEOUT
                entry.event.set()
            status = entry.status
            receipt = entry.receipt
            error = entry.error

        return DispatchOutcome(
            dispatch_id=dispatch_id,
            project_id=project_id,
            status=status,
            receipt=receipt,
            error=error,
        )
