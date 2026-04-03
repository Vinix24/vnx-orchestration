#!/usr/bin/env python3
"""RuntimeAdapter protocol and shared types for transport abstraction.

Defines the protocol that all adapter implementations (TmuxAdapter,
HeadlessAdapter, LocalSessionAdapter) must satisfy. Shared result
types are imported from tmux_adapter.py where they were first defined.

Required capabilities: SPAWN, STOP, DELIVER, OBSERVE, HEALTH, SESSION_HEALTH.
Optional capabilities: ATTACH, INSPECT, REHEAL.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from tmux_adapter import (
    CAPABILITY_DELIVER,
    CAPABILITY_HEALTH,
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

REQUIRED_CAPABILITIES = frozenset({
    CAPABILITY_SPAWN, CAPABILITY_STOP, CAPABILITY_DELIVER,
    CAPABILITY_OBSERVE, CAPABILITY_HEALTH, CAPABILITY_SESSION_HEALTH,
})


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


def validate_required_capabilities(adapter: RuntimeAdapter) -> List[str]:
    """Return list of missing required capabilities, empty if all present."""
    caps = adapter.capabilities()
    return [c for c in REQUIRED_CAPABILITIES if c not in caps]
