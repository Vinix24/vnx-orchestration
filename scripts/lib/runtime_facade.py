#!/usr/bin/env python3
"""Runtime facade — adapter-backed boundary for orchestration and dashboard.

Provides a single entry point for runtime operations. Orchestration,
dashboard, and operator commands call the facade instead of transport-
specific helpers. Transport failures surface as explicit RuntimeOutcome
results instead of transport exceptions.

The facade:
  - Checks adapter capabilities before delegating
  - Translates transport results into uniform RuntimeOutcome
  - Preserves runtime truth compatibility (adapter never owns canonical state)
  - Provides observation/health aggregation for dashboard surfaces
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from result_contract import Result, result_error, result_ok
from tmux_adapter import (
    CAPABILITY_ATTACH,
    CAPABILITY_DELIVER,
    CAPABILITY_HEALTH,
    CAPABILITY_INSPECT,
    CAPABILITY_OBSERVE,
    CAPABILITY_REHEAL,
    CAPABILITY_SESSION_HEALTH,
    CAPABILITY_SPAWN,
    CAPABILITY_STOP,
    TmuxAdapter,
    UnsupportedCapability,
)


@dataclass
class RuntimeOutcome:
    """Uniform result from a runtime facade operation."""
    success: bool
    operation: str
    terminal_id: str
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


CANONICAL_TERMINALS = ("T0", "T1", "T2", "T3")


class RuntimeFacade:
    """Adapter-backed runtime boundary for orchestration and dashboard.

    All runtime operations flow through this facade. Transport-specific
    behavior is delegated to the configured adapter. The facade never
    owns canonical state (leases, dispatches, receipts).
    """

    def __init__(self, adapter: TmuxAdapter) -> None:
        self._adapter = adapter

    @property
    def adapter_type(self) -> str:
        return self._adapter.adapter_type()

    def has_capability(self, capability: str) -> bool:
        return capability in self._adapter.capabilities()

    def launch(self, terminal_id: str, config: Dict[str, Any]) -> RuntimeOutcome:
        """Spawn an execution surface for terminal_id."""
        if not self.has_capability(CAPABILITY_SPAWN):
            return RuntimeOutcome(success=False, operation="launch",
                terminal_id=terminal_id, error="SPAWN not supported")
        result = self._adapter.spawn(terminal_id, config)
        return RuntimeOutcome(
            success=result.success, operation="launch", terminal_id=terminal_id,
            details={"transport_ref": result.transport_ref}, error=result.error,
        )

    def stop(self, terminal_id: str) -> RuntimeOutcome:
        """Stop the execution surface for terminal_id."""
        if not self.has_capability(CAPABILITY_STOP):
            return RuntimeOutcome(success=False, operation="stop",
                terminal_id=terminal_id, error="STOP not supported")
        result = self._adapter.stop(terminal_id)
        return RuntimeOutcome(
            success=result.success, operation="stop", terminal_id=terminal_id,
            details={"was_running": result.was_running}, error=result.error,
        )

    def deliver(self, terminal_id: str, dispatch_id: str,
                attempt_id: Optional[str] = None, **kwargs: Any) -> RuntimeOutcome:
        """Deliver dispatch to terminal_id."""
        if not self.has_capability(CAPABILITY_DELIVER):
            return RuntimeOutcome(success=False, operation="deliver",
                terminal_id=terminal_id, error="DELIVER not supported")
        result = self._adapter.deliver(terminal_id, dispatch_id, attempt_id, **kwargs)
        return RuntimeOutcome(
            success=result.success, operation="deliver", terminal_id=terminal_id,
            details={"path_used": result.path_used, "dispatch_id": dispatch_id},
            error=result.failure_reason,
        )

    def attach(self, terminal_id: str) -> RuntimeOutcome:
        """Switch operator focus to terminal_id."""
        if not self.has_capability(CAPABILITY_ATTACH):
            return RuntimeOutcome(success=False, operation="attach",
                terminal_id=terminal_id, error="ATTACH not supported")
        result = self._adapter.attach(terminal_id)
        return RuntimeOutcome(
            success=result.success, operation="attach",
            terminal_id=terminal_id, error=result.error,
        )

    def observe(self, terminal_id: str) -> RuntimeOutcome:
        """Read-only state probe for terminal_id."""
        if not self.has_capability(CAPABILITY_OBSERVE):
            return RuntimeOutcome(success=False, operation="observe",
                terminal_id=terminal_id, error="OBSERVE not supported")
        result = self._adapter.observe(terminal_id)
        return RuntimeOutcome(
            success=result.exists, operation="observe", terminal_id=terminal_id,
            details={"exists": result.exists, "responsive": result.responsive,
                      "transport_state": result.transport_state},
            error=result.error,
        )

    def inspect(self, terminal_id: str) -> RuntimeOutcome:
        """Deep diagnostic inspection of terminal_id."""
        if not self.has_capability(CAPABILITY_INSPECT):
            return RuntimeOutcome(success=False, operation="inspect",
                terminal_id=terminal_id, error="INSPECT not supported")
        result = self._adapter.inspect(terminal_id)
        return RuntimeOutcome(
            success=result.exists, operation="inspect", terminal_id=terminal_id,
            details={"transport_ref": result.transport_ref,
                      "transport_details": result.transport_details,
                      "has_content": result.pane_content is not None},
            error=result.error,
        )

    def health(self, terminal_id: str) -> RuntimeOutcome:
        """Fast health check for terminal_id."""
        if not self.has_capability(CAPABILITY_HEALTH):
            return RuntimeOutcome(success=False, operation="health",
                terminal_id=terminal_id, error="HEALTH not supported")
        result = self._adapter.health(terminal_id)
        return RuntimeOutcome(
            success=result.healthy, operation="health", terminal_id=terminal_id,
            details={"healthy": result.healthy, "surface_exists": result.surface_exists,
                      "process_alive": result.process_alive},
            error=result.error,
        )

    def session_health(self, terminal_ids: Optional[List[str]] = None) -> Result:
        """Aggregate health across terminals. Returns Result with dict."""
        if not self.has_capability(CAPABILITY_SESSION_HEALTH):
            return result_error("unsupported", "SESSION_HEALTH not supported")
        ids = list(CANONICAL_TERMINALS if terminal_ids is None else terminal_ids)
        result = self._adapter.session_health(ids)
        summary = {
            "session_exists": result.session_exists,
            "total": len(ids),
            "healthy": sum(1 for h in result.terminals.values() if h.healthy),
            "degraded": result.degraded_terminals,
            "terminals": {
                tid: {"healthy": h.healthy, "surface_exists": h.surface_exists,
                       "process_alive": h.process_alive, "error": h.error}
                for tid, h in result.terminals.items()
            },
        }
        return result_ok(summary)

    def reheal(self, terminal_id: str) -> RuntimeOutcome:
        """Attempt transport drift recovery for terminal_id."""
        if not self.has_capability(CAPABILITY_REHEAL):
            return RuntimeOutcome(success=False, operation="reheal",
                terminal_id=terminal_id, error="REHEAL not supported")
        result = self._adapter.reheal(terminal_id)
        return RuntimeOutcome(
            success=result.rehealed, operation="reheal", terminal_id=terminal_id,
            details={"old_ref": result.old_ref, "new_ref": result.new_ref,
                      "strategy": result.strategy},
            error=result.error,
        )
