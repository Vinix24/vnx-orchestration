#!/usr/bin/env python3
"""Shared types for VNX RuntimeAdapter abstraction layer.

Contains all result dataclasses, capability constants, exception hierarchy,
and the RuntimeAdapter Protocol. Canonical source of truth for adapter types —
all adapters and consumers import from here.

Extracted from tmux_adapter.py and adapter_protocol.py as part of F28 PR-0
to decouple shared types from the tmux transport implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Capability constants
# ---------------------------------------------------------------------------

CAPABILITY_SPAWN = "SPAWN"
CAPABILITY_STOP = "STOP"
CAPABILITY_DELIVER = "DELIVER"
CAPABILITY_ATTACH = "ATTACH"
CAPABILITY_OBSERVE = "OBSERVE"
CAPABILITY_INSPECT = "INSPECT"
CAPABILITY_HEALTH = "HEALTH"
CAPABILITY_SESSION_HEALTH = "SESSION_HEALTH"
CAPABILITY_REHEAL = "REHEAL"

REQUIRED_CAPABILITIES = frozenset({
    CAPABILITY_SPAWN, CAPABILITY_STOP, CAPABILITY_DELIVER,
    CAPABILITY_OBSERVE, CAPABILITY_HEALTH, CAPABILITY_SESSION_HEALTH,
})


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SpawnResult:
    """Result of spawning an execution surface."""
    success: bool
    transport_ref: str = ""
    error: Optional[str] = None


@dataclass
class StopResult:
    """Result of stopping an execution surface."""
    success: bool
    was_running: bool = False
    error: Optional[str] = None


@dataclass
class DeliveryResult:
    """Result of a single delivery attempt."""
    success: bool
    terminal_id: str
    dispatch_id: str
    pane_id: Optional[str]
    path_used: str          # "primary" | "legacy" | "none"
    failure_reason: Optional[str] = None
    tmux_returncode: Optional[int] = None


@dataclass
class AttachResult:
    """Result of switching operator focus."""
    success: bool
    error: Optional[str] = None


@dataclass
class ObservationResult:
    """Read-only state probe result."""
    exists: bool
    responsive: bool = False
    transport_state: Dict[str, Any] = field(default_factory=dict)
    last_output_fragment: Optional[str] = None
    error: Optional[str] = None


@dataclass
class InspectionResult:
    """Deep diagnostic inspection result."""
    exists: bool
    transport_ref: str = ""
    transport_details: Dict[str, Any] = field(default_factory=dict)
    pane_content: Optional[str] = None
    environment: Optional[Dict[str, str]] = None
    error: Optional[str] = None


@dataclass
class HealthResult:
    """Fast health check result."""
    healthy: bool
    surface_exists: bool = False
    process_alive: bool = False
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class SessionHealthResult:
    """Aggregate health check result."""
    session_exists: bool
    terminals: Dict[str, HealthResult] = field(default_factory=dict)
    degraded_terminals: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class RehealResult:
    """Transport drift recovery result."""
    rehealed: bool
    old_ref: Optional[str] = None
    new_ref: Optional[str] = None
    strategy: str = ""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class RuntimeAdapterError(Exception):
    """Base error for all runtime adapter failures."""
    def __init__(self, message: str, adapter_type: str = "tmux", operation: str = ""):
        self.adapter_type = adapter_type
        self.operation = operation
        super().__init__(message)


class UnsupportedCapability(RuntimeAdapterError):
    """Raised when an operation is invoked on an adapter that does not support it."""
    def __init__(self, operation: str, adapter_type: str = "tmux", reason: str = ""):
        self.reason = reason or f"{adapter_type} adapter does not support {operation}"
        super().__init__(self.reason, adapter_type=adapter_type, operation=operation)


# ---------------------------------------------------------------------------
# RuntimeAdapter Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class RuntimeAdapter(Protocol):
    """Protocol defining the canonical runtime adapter interface."""

    def adapter_type(self) -> str: ...
    def capabilities(self) -> frozenset: ...
    def spawn(self, terminal_id: str, config: Dict[str, Any]) -> SpawnResult: ...
    def stop(self, terminal_id: str) -> StopResult: ...
    def deliver(self, terminal_id: str, dispatch_id: str,
                attempt_id: Optional[str] = None, **kwargs: Any) -> DeliveryResult: ...
    def attach(self, terminal_id: str) -> AttachResult: ...
    def observe(self, terminal_id: str) -> ObservationResult: ...
    def inspect(self, terminal_id: str) -> InspectionResult: ...
    def health(self, terminal_id: str) -> HealthResult: ...
    def session_health(self, terminal_ids: List[str]) -> SessionHealthResult: ...
    def reheal(self, terminal_id: str) -> RehealResult: ...
    def shutdown(self, graceful: bool = True) -> None: ...


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_required_capabilities(adapter: RuntimeAdapter) -> List[str]:
    """Return list of missing required capabilities, empty if all present."""
    caps = adapter.capabilities()
    return [c for c in REQUIRED_CAPABILITIES if c not in caps]
