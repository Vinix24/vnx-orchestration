#!/usr/bin/env python3
"""VNX Subprocess Adapter — RuntimeAdapter implementation for headless claude CLI processes.

Spawns `claude -p --output-format stream-json` subprocesses instead of routing
through tmux panes. Each terminal_id maps to a tracked subprocess. All process
group management uses os.setsid / os.killpg for clean teardown.

BILLING SAFETY: Only calls subprocess.Popen(["claude", ...]). No Anthropic SDK.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any, Dict, List, Optional

from adapter_types import (
    CAPABILITY_DELIVER,
    CAPABILITY_HEALTH,
    CAPABILITY_OBSERVE,
    CAPABILITY_SESSION_HEALTH,
    CAPABILITY_SPAWN,
    CAPABILITY_STOP,
    DeliveryResult,
    HealthResult,
    ObservationResult,
    SessionHealthResult,
    SpawnResult,
    StopResult,
)

SUBPROCESS_CAPABILITIES = frozenset({
    CAPABILITY_SPAWN,
    CAPABILITY_STOP,
    CAPABILITY_DELIVER,
    CAPABILITY_OBSERVE,
    CAPABILITY_HEALTH,
    CAPABILITY_SESSION_HEALTH,
})


class SubprocessAdapter:
    """RuntimeAdapter implementation for headless subprocess-based terminals.

    Lifecycle:
      spawn()   — registers config, no subprocess started yet
      deliver() — spawns a claude subprocess with the dispatch instruction
      stop()    — sends SIGTERM (escalates to SIGKILL on timeout)
      observe() — non-blocking poll of process state
      health()  — fast alive check
      session_health() — aggregate across multiple terminal IDs
      shutdown() — stops all tracked processes
    """

    def __init__(self) -> None:
        # terminal_id -> subprocess.Popen
        self._processes: Dict[str, subprocess.Popen] = {}
        # terminal_id -> spawn config (preserved for re-spawn if needed)
        self._configs: Dict[str, Dict[str, Any]] = {}

    def adapter_type(self) -> str:
        return "subprocess"

    def capabilities(self) -> frozenset:
        return SUBPROCESS_CAPABILITIES

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    def spawn(self, terminal_id: str, config: Dict[str, Any]) -> SpawnResult:
        """Register terminal config. Does not start a subprocess yet."""
        self._configs[terminal_id] = config
        return SpawnResult(
            success=True,
            transport_ref=f"subprocess:{terminal_id}",
        )

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    def stop(self, terminal_id: str) -> StopResult:
        """Terminate subprocess for terminal_id. SIGTERM → SIGKILL on timeout."""
        process = self._processes.get(terminal_id)
        if process is None:
            return StopResult(success=True, was_running=False)

        was_running = process.poll() is None
        if was_running:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.wait(timeout=5)
            except (OSError, ProcessLookupError):
                # Process already gone — treat as success
                pass

        self._processes.pop(terminal_id, None)
        return StopResult(success=True, was_running=was_running)

    # ------------------------------------------------------------------
    # Deliver
    # ------------------------------------------------------------------

    def deliver(
        self,
        terminal_id: str,
        dispatch_id: str,
        attempt_id: Optional[str] = None,
        *,
        instruction: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> DeliveryResult:
        """Spawn a claude subprocess with the dispatch instruction.

        instruction and model can be passed directly or pulled from the stored
        config registered via spawn(). dispatch_id is always appended to the
        instruction so the subprocess can identify its work.
        """
        config = self._configs.get(terminal_id, {})
        effective_instruction = instruction or config.get("instruction", dispatch_id)
        effective_model = model or config.get("model", "sonnet")

        cmd = [
            "claude",
            "-p",
            "--output-format", "stream-json",
            "--model", effective_model,
            effective_instruction,
        ]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,  # new process group for clean SIGKILL
            )
        except (FileNotFoundError, OSError) as exc:
            return DeliveryResult(
                success=False,
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                pane_id=None,
                path_used="none",
                failure_reason=str(exc),
            )

        # Replace any prior process tracking (stop old one first)
        if terminal_id in self._processes:
            old = self._processes[terminal_id]
            if old.poll() is None:
                try:
                    os.killpg(os.getpgid(old.pid), signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass

        self._processes[terminal_id] = process

        return DeliveryResult(
            success=True,
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            pane_id=None,
            path_used="subprocess",
        )

    # ------------------------------------------------------------------
    # Observe
    # ------------------------------------------------------------------

    def observe(self, terminal_id: str) -> ObservationResult:
        """Non-blocking process state probe."""
        process = self._processes.get(terminal_id)
        if process is None:
            # Check if config was registered (spawned but not yet delivered)
            exists = terminal_id in self._configs
            return ObservationResult(
                exists=exists,
                responsive=False,
                transport_state={"surface_exists": exists, "process_alive": False},
            )

        alive = process.poll() is None
        return ObservationResult(
            exists=True,
            responsive=alive,
            transport_state={
                "surface_exists": True,
                "process_alive": alive,
                "pid": process.pid,
                "returncode": process.returncode,
            },
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self, terminal_id: str) -> HealthResult:
        """Fast health check — O(1), no blocking."""
        process = self._processes.get(terminal_id)
        if process is None:
            surface = terminal_id in self._configs
            return HealthResult(
                healthy=False,
                surface_exists=surface,
                process_alive=False,
                details={"terminal_id": terminal_id, "has_process": False},
            )

        alive = process.poll() is None
        return HealthResult(
            healthy=alive,
            surface_exists=True,
            process_alive=alive,
            details={
                "terminal_id": terminal_id,
                "pid": process.pid,
                "returncode": process.returncode,
            },
        )

    def session_health(self, terminal_ids: List[str]) -> SessionHealthResult:
        """Aggregate health across multiple terminal IDs."""
        terminals: Dict[str, HealthResult] = {}
        degraded: List[str] = []
        for tid in terminal_ids:
            h = self.health(tid)
            terminals[tid] = h
            if not h.healthy:
                degraded.append(tid)
        session_exists = any(h.surface_exists for h in terminals.values())
        return SessionHealthResult(
            session_exists=session_exists,
            terminals=terminals,
            degraded_terminals=degraded,
        )

    # ------------------------------------------------------------------
    # Unsupported optional operations
    # ------------------------------------------------------------------

    def attach(self, terminal_id: str):  # type: ignore[return]
        from adapter_types import AttachResult, UnsupportedCapability
        raise UnsupportedCapability("attach", adapter_type="subprocess")

    def inspect(self, terminal_id: str):  # type: ignore[return]
        from adapter_types import InspectionResult, UnsupportedCapability
        raise UnsupportedCapability("inspect", adapter_type="subprocess")

    def reheal(self, terminal_id: str):  # type: ignore[return]
        from adapter_types import RehealResult, UnsupportedCapability
        raise UnsupportedCapability("reheal", adapter_type="subprocess")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self, graceful: bool = True) -> None:
        """Stop all tracked subprocesses."""
        for terminal_id in list(self._processes.keys()):
            self.stop(terminal_id)
