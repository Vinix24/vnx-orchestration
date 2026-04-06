#!/usr/bin/env python3
"""LocalSessionAdapter — headless session lifecycle with attempt tracking.

Implements RuntimeAdapter for local subprocess sessions with explicit
lifecycle states and attempt correlation. Sessions are represented as
real runtime entities instead of thin subprocess side-effects.

Lifecycle states:
  CREATED -> RUNNING -> COMPLETED | FAILED | TIMED_OUT

Attempt tracking:
  Each spawn increments the attempt number. Attempts carry correlation
  metadata (dispatch_id, terminal_id, attempt_number, timestamps).

Capabilities:
  Supported: SPAWN, STOP, DELIVER, OBSERVE, HEALTH, SESSION_HEALTH
  Partial:   INSPECT
  Unsupported: ATTACH, REHEAL
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from adapter_types import (
    CAPABILITY_DELIVER,
    CAPABILITY_HEALTH,
    CAPABILITY_INSPECT,
    CAPABILITY_OBSERVE,
    CAPABILITY_SESSION_HEALTH,
    CAPABILITY_SPAWN,
    CAPABILITY_STOP,
    AttachResult,
    DeliveryResult,
    HealthResult,
    InspectionResult,
    ObservationResult,
    RehealResult,
    SessionHealthResult,
    SpawnResult,
    StopResult,
    UnsupportedCapability,
)

LOCAL_SESSION_CAPABILITIES = frozenset({
    CAPABILITY_SPAWN, CAPABILITY_STOP, CAPABILITY_DELIVER,
    CAPABILITY_OBSERVE, CAPABILITY_INSPECT, CAPABILITY_HEALTH,
    CAPABILITY_SESSION_HEALTH,
})


@dataclass
class SessionAttempt:
    """Tracks a single execution attempt within a session."""
    attempt_number: int
    state: str  # CREATED | RUNNING | COMPLETED | FAILED | TIMED_OUT
    dispatch_id: str = ""
    pid: Optional[int] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    exit_code: Optional[int] = None
    failure_reason: Optional[str] = None


@dataclass
class SessionRecord:
    """Tracks the full lifecycle of a local session."""
    terminal_id: str
    proc: Optional[subprocess.Popen] = None
    current_attempt: Optional[SessionAttempt] = None
    attempts: List[SessionAttempt] = field(default_factory=list)
    total_attempts: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalSessionAdapter:
    """RuntimeAdapter with explicit lifecycle and attempt tracking."""

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionRecord] = {}

    def adapter_type(self) -> str:
        return "local_session"

    def capabilities(self) -> frozenset:
        return LOCAL_SESSION_CAPABILITIES

    def get_session(self, terminal_id: str) -> Optional[SessionRecord]:
        """Query session state for a terminal."""
        return self._sessions.get(terminal_id)

    def get_attempt(self, terminal_id: str) -> Optional[SessionAttempt]:
        """Query current attempt for a terminal."""
        session = self._sessions.get(terminal_id)
        return session.current_attempt if session else None

    def spawn(self, terminal_id: str, config: Dict[str, Any]) -> SpawnResult:
        """Spawn subprocess with lifecycle tracking. Idempotent."""
        session = self._sessions.get(terminal_id)
        if session and session.proc and session.proc.poll() is None:
            return SpawnResult(success=True, transport_ref=str(session.proc.pid))

        command = config.get("command", "")
        work_dir = config.get("work_dir", ".")
        dispatch_id = config.get("dispatch_id", "")
        if not command:
            return SpawnResult(success=False, error="command required in config")

        args = shlex.split(command) if isinstance(command, str) else list(command)
        try:
            proc = subprocess.Popen(args, cwd=work_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            return SpawnResult(success=False, error=str(e))

        attempt_num = (session.total_attempts + 1) if session else 1
        attempt = SessionAttempt(
            attempt_number=attempt_num, state="RUNNING",
            dispatch_id=dispatch_id, pid=proc.pid, started_at=_now_iso(),
        )
        record = SessionRecord(
            terminal_id=terminal_id, proc=proc,
            current_attempt=attempt,
            attempts=(session.attempts if session else []) + [attempt],
            total_attempts=attempt_num,
        )
        self._sessions[terminal_id] = record
        return SpawnResult(success=True, transport_ref=str(proc.pid))

    def stop(self, terminal_id: str) -> StopResult:
        """Stop with SIGTERM -> wait(5s) -> SIGKILL. Updates lifecycle."""
        session = self._sessions.get(terminal_id)
        if session is None:
            return StopResult(success=True, was_running=False)
        proc = session.proc
        if proc is None or proc.poll() is not None:
            self._check_and_finalize(session)
            return StopResult(success=True, was_running=False)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        rc = proc.returncode
        # Negative rc = killed by signal (from our terminate/kill), treat as graceful stop.
        # Positive nonzero rc = process exited with error before we stopped it.
        state = "FAILED" if rc is not None and rc > 0 else "COMPLETED"
        self._finalize_attempt(session, state, exit_code=rc,
            failure_reason=f"exit code {rc}" if rc and rc > 0 else None)
        return StopResult(success=True, was_running=True)

    def deliver(self, terminal_id: str, dispatch_id: str,
                attempt_id: Optional[str] = None, **kwargs: Any) -> DeliveryResult:
        """Deliver dispatch reference to running session."""
        session = self._sessions.get(terminal_id)
        if session is None or session.proc is None or session.proc.poll() is not None:
            return DeliveryResult(success=False, terminal_id=terminal_id,
                dispatch_id=dispatch_id, pane_id=None, path_used="local_session",
                failure_reason="No active session for terminal")
        if session.current_attempt:
            session.current_attempt.dispatch_id = dispatch_id
        return DeliveryResult(success=True, terminal_id=terminal_id,
            dispatch_id=dispatch_id, pane_id=str(session.proc.pid),
            path_used="local_session")

    def attach(self, terminal_id: str) -> AttachResult:
        raise UnsupportedCapability("ATTACH", "local_session",
            "Local sessions have no interactive surface")

    def observe(self, terminal_id: str) -> ObservationResult:
        """Check session liveness and lifecycle state."""
        session = self._sessions.get(terminal_id)
        if session is None or session.proc is None:
            return ObservationResult(exists=False)
        alive = session.proc.poll() is None
        if not alive:
            self._check_and_finalize(session)
        state = session.current_attempt.state if session.current_attempt else "UNKNOWN"
        return ObservationResult(exists=True, responsive=alive,
            transport_state={"surface_exists": True, "process_alive": alive,
                "pid": session.proc.pid, "lifecycle_state": state,
                "attempt_number": session.total_attempts})

    def inspect(self, terminal_id: str) -> InspectionResult:
        """Process info with lifecycle metadata."""
        session = self._sessions.get(terminal_id)
        if session is None or session.proc is None:
            return InspectionResult(exists=False)
        alive = session.proc.poll() is None
        attempt = session.current_attempt
        return InspectionResult(exists=True, transport_ref=str(session.proc.pid),
            transport_details={"pid": session.proc.pid, "alive": alive,
                "attempt_number": session.total_attempts,
                "state": attempt.state if attempt else "UNKNOWN",
                "dispatch_id": attempt.dispatch_id if attempt else ""})

    def health(self, terminal_id: str) -> HealthResult:
        session = self._sessions.get(terminal_id)
        if session is None or session.proc is None:
            return HealthResult(healthy=False, surface_exists=False)
        alive = session.proc.poll() is None
        if not alive:
            self._check_and_finalize(session)
        return HealthResult(healthy=alive, surface_exists=True,
            process_alive=alive, details={"pid": session.proc.pid,
                "attempt": session.total_attempts})

    def session_health(self, terminal_ids: List[str]) -> SessionHealthResult:
        terminals: Dict[str, HealthResult] = {}
        degraded: List[str] = []
        for tid in terminal_ids:
            h = self.health(tid)
            terminals[tid] = h
            if not h.healthy:
                degraded.append(tid)
        session_exists = any(h.surface_exists for h in terminals.values())
        return SessionHealthResult(session_exists=session_exists,
            terminals=terminals, degraded_terminals=degraded)

    def reheal(self, terminal_id: str) -> RehealResult:
        raise UnsupportedCapability("REHEAL", "local_session",
            "Local sessions use process-based identity")

    def shutdown(self, graceful: bool = True) -> None:
        for tid in list(self._sessions):
            self.stop(tid)

    def mark_timed_out(self, terminal_id: str) -> None:
        """Mark current attempt as TIMED_OUT and kill process."""
        session = self._sessions.get(terminal_id)
        if session and session.proc and session.proc.poll() is None:
            session.proc.kill()
            try:
                session.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass  # process unkillable — finalize state anyway
        if session:
            self._finalize_attempt(session, "TIMED_OUT",
                failure_reason="Execution timed out")

    def mark_failed(self, terminal_id: str, reason: str = "") -> None:
        """Terminate process and mark current attempt as FAILED."""
        session = self._sessions.get(terminal_id)
        if not session:
            return
        if session.proc and session.proc.poll() is None:
            session.proc.terminate()
            try:
                session.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                session.proc.kill()
                try:
                    session.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        exit_code = session.proc.returncode if session.proc else None
        self._finalize_attempt(session, "FAILED",
            exit_code=exit_code, failure_reason=reason)

    def _finalize_attempt(self, session: SessionRecord, state: str, *,
                          exit_code: Optional[int] = None,
                          failure_reason: Optional[str] = None) -> None:
        if session.current_attempt and session.current_attempt.state == "RUNNING":
            session.current_attempt.state = state
            session.current_attempt.ended_at = _now_iso()
            session.current_attempt.exit_code = exit_code
            session.current_attempt.failure_reason = failure_reason

    def _check_and_finalize(self, session: SessionRecord) -> None:
        """Auto-finalize attempt if process exited."""
        if not session.proc or not session.current_attempt:
            return
        rc = session.proc.returncode
        if session.current_attempt.state == "RUNNING":
            state = "COMPLETED" if rc == 0 else "FAILED"
            self._finalize_attempt(session, state, exit_code=rc,
                failure_reason=f"exit code {rc}" if rc != 0 else None)
