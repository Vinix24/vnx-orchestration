#!/usr/bin/env python3
"""HeadlessAdapter — early transport abstraction for non-tmux execution.

Implements RuntimeAdapter for headless CLI subprocess sessions. This is
a constrained skeleton: supported operations return subprocess-based
results, unsupported operations (ATTACH, REHEAL) raise UnsupportedCapability.

This adapter does NOT replace the existing headless_adapter.py (which
handles headless review gate execution). It provides the RuntimeAdapter
contract boundary for future headless worker sessions.

Capabilities:
  Supported: SPAWN, STOP, DELIVER, OBSERVE, HEALTH, SESSION_HEALTH
  Partial:   INSPECT (process info only, no terminal content)
  Unsupported: ATTACH, REHEAL
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Any, Dict, List, Optional

from tmux_adapter import (
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

HEADLESS_CAPABILITIES = frozenset({
    CAPABILITY_SPAWN, CAPABILITY_STOP, CAPABILITY_DELIVER,
    CAPABILITY_OBSERVE, CAPABILITY_INSPECT, CAPABILITY_HEALTH,
    CAPABILITY_SESSION_HEALTH,
})


class HeadlessAdapter:
    """RuntimeAdapter for headless CLI subprocess sessions.

    Each terminal maps to a managed subprocess tracked in-memory.
    No tmux dependency. No interactive terminal surface.

    Security note: command input must be trusted/internal-only.
    This adapter is not exposed to untrusted user input.
    """

    def __init__(self) -> None:
        self._procs: Dict[str, subprocess.Popen] = {}  # terminal_id -> Popen

    def adapter_type(self) -> str:
        return "headless"

    def capabilities(self) -> frozenset:
        return HEADLESS_CAPABILITIES

    def spawn(self, terminal_id: str, config: Dict[str, Any]) -> SpawnResult:
        """Spawn a subprocess for terminal_id. Idempotent."""
        existing = self._procs.get(terminal_id)
        if existing is not None and existing.poll() is None:
            return SpawnResult(success=True, transport_ref=str(existing.pid))
        command = config.get("command", "")
        work_dir = config.get("work_dir", ".")
        if not command:
            return SpawnResult(success=False, error="command required in config")
        args = shlex.split(command) if isinstance(command, str) else list(command)
        try:
            proc = subprocess.Popen(
                args, cwd=work_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._procs[terminal_id] = proc
            return SpawnResult(success=True, transport_ref=str(proc.pid))
        except OSError as e:
            return SpawnResult(success=False, error=str(e))

    def stop(self, terminal_id: str) -> StopResult:
        """Stop subprocess with SIGTERM -> wait(5s) -> SIGKILL. Idempotent."""
        proc = self._procs.pop(terminal_id, None)
        if proc is None:
            return StopResult(success=True, was_running=False)
        if proc.poll() is not None:
            return StopResult(success=True, was_running=False)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        return StopResult(success=True, was_running=True)

    def deliver(self, terminal_id: str, dispatch_id: str,
                attempt_id: Optional[str] = None, **kwargs: Any) -> DeliveryResult:
        """Deliver dispatch reference. For headless, this is a no-op marker."""
        proc = self._procs.get(terminal_id)
        if proc is None or proc.poll() is not None:
            return DeliveryResult(success=False, terminal_id=terminal_id,
                dispatch_id=dispatch_id, pane_id=None, path_used="headless",
                failure_reason="No active process for terminal")
        return DeliveryResult(success=True, terminal_id=terminal_id,
            dispatch_id=dispatch_id, pane_id=str(proc.pid), path_used="headless")

    def attach(self, terminal_id: str) -> AttachResult:
        """Headless sessions have no interactive surface."""
        raise UnsupportedCapability("ATTACH", "headless",
            "Headless sessions have no interactive surface")

    def observe(self, terminal_id: str) -> ObservationResult:
        """Check process liveness."""
        proc = self._procs.get(terminal_id)
        if proc is None:
            return ObservationResult(exists=False)
        alive = proc.poll() is None
        return ObservationResult(
            exists=alive, responsive=alive,
            transport_state={"surface_exists": alive, "process_alive": alive, "pid": proc.pid},
        )

    def inspect(self, terminal_id: str) -> InspectionResult:
        """Partial: process info only, no terminal content."""
        proc = self._procs.get(terminal_id)
        if proc is None:
            return InspectionResult(exists=False)
        alive = proc.poll() is None
        return InspectionResult(
            exists=alive, transport_ref=str(proc.pid),
            transport_details={"pid": proc.pid, "alive": alive},
        )

    def health(self, terminal_id: str) -> HealthResult:
        """Process liveness check."""
        proc = self._procs.get(terminal_id)
        if proc is None:
            return HealthResult(healthy=False, surface_exists=False)
        alive = proc.poll() is None
        return HealthResult(healthy=alive, surface_exists=alive,
            process_alive=alive, details={"pid": proc.pid})

    def session_health(self, terminal_ids: List[str]) -> SessionHealthResult:
        """Aggregate health across terminals."""
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
        """Headless has no pane drift — reheal not supported."""
        raise UnsupportedCapability("REHEAL", "headless",
            "Headless sessions use process-based identity, not pane-based")

    def shutdown(self, graceful: bool = True) -> None:
        """Stop all tracked processes."""
        for tid in list(self._procs):
            self.stop(tid)
