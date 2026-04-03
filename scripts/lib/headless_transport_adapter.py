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

import os
import signal
import subprocess
from dataclasses import dataclass, field
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

    Each terminal maps to a subprocess PID tracked in-memory.
    No tmux dependency. No interactive terminal surface.
    """

    def __init__(self) -> None:
        self._processes: Dict[str, int] = {}  # terminal_id -> pid

    def adapter_type(self) -> str:
        return "headless"

    def capabilities(self) -> frozenset:
        return HEADLESS_CAPABILITIES

    def spawn(self, terminal_id: str, config: Dict[str, Any]) -> SpawnResult:
        """Spawn a subprocess for terminal_id. Idempotent."""
        if terminal_id in self._processes:
            pid = self._processes[terminal_id]
            if self._pid_alive(pid):
                return SpawnResult(success=True, transport_ref=str(pid))
        command = config.get("command", "")
        work_dir = config.get("work_dir", ".")
        if not command:
            return SpawnResult(success=False, error="command required in config")
        try:
            proc = subprocess.Popen(
                command, shell=True, cwd=work_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self._processes[terminal_id] = proc.pid
            return SpawnResult(success=True, transport_ref=str(proc.pid))
        except OSError as e:
            return SpawnResult(success=False, error=str(e))

    def stop(self, terminal_id: str) -> StopResult:
        """Stop subprocess. Idempotent."""
        pid = self._processes.pop(terminal_id, None)
        if pid is None:
            return StopResult(success=True, was_running=False)
        if not self._pid_alive(pid):
            return StopResult(success=True, was_running=False)
        try:
            os.kill(pid, signal.SIGTERM)
            return StopResult(success=True, was_running=True)
        except OSError:
            return StopResult(success=True, was_running=False)

    def deliver(self, terminal_id: str, dispatch_id: str,
                attempt_id: Optional[str] = None, **kwargs: Any) -> DeliveryResult:
        """Deliver dispatch reference. For headless, this is a no-op marker."""
        pid = self._processes.get(terminal_id)
        if pid is None or not self._pid_alive(pid):
            return DeliveryResult(success=False, terminal_id=terminal_id,
                dispatch_id=dispatch_id, pane_id=None, path_used="headless",
                failure_reason="No active process for terminal")
        return DeliveryResult(success=True, terminal_id=terminal_id,
            dispatch_id=dispatch_id, pane_id=str(pid), path_used="headless")

    def attach(self, terminal_id: str) -> AttachResult:
        """Headless sessions have no interactive surface."""
        raise UnsupportedCapability("ATTACH", "headless",
            "Headless sessions have no interactive surface")

    def observe(self, terminal_id: str) -> ObservationResult:
        """Check process liveness."""
        pid = self._processes.get(terminal_id)
        if pid is None:
            return ObservationResult(exists=False)
        alive = self._pid_alive(pid)
        return ObservationResult(
            exists=alive, responsive=alive,
            transport_state={"surface_exists": alive, "process_alive": alive, "pid": pid},
        )

    def inspect(self, terminal_id: str) -> InspectionResult:
        """Partial: process info only, no terminal content."""
        pid = self._processes.get(terminal_id)
        if pid is None:
            return InspectionResult(exists=False)
        alive = self._pid_alive(pid)
        return InspectionResult(
            exists=alive, transport_ref=str(pid),
            transport_details={"pid": pid, "alive": alive},
        )

    def health(self, terminal_id: str) -> HealthResult:
        """Process liveness check."""
        pid = self._processes.get(terminal_id)
        if pid is None:
            return HealthResult(healthy=False, surface_exists=False)
        alive = self._pid_alive(pid)
        return HealthResult(healthy=alive, surface_exists=alive,
            process_alive=alive, details={"pid": pid})

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
        for tid in list(self._processes):
            self.stop(tid)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Check if a process is alive."""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
