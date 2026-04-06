#!/usr/bin/env python3
"""RuntimeAdapter protocol and shared types for transport abstraction.

Backward-compatibility shim: all types now live in adapter_types.py.
This module re-exports them so existing imports continue to work.
"""

from adapter_types import (  # noqa: F401
    CAPABILITY_ATTACH,
    CAPABILITY_DELIVER,
    CAPABILITY_HEALTH,
    CAPABILITY_INSPECT,
    CAPABILITY_OBSERVE,
    CAPABILITY_REHEAL,
    CAPABILITY_SESSION_HEALTH,
    CAPABILITY_SPAWN,
    CAPABILITY_STOP,
    REQUIRED_CAPABILITIES,
    AttachResult,
    DeliveryResult,
    HealthResult,
    InspectionResult,
    ObservationResult,
    RehealResult,
    RuntimeAdapter,
    RuntimeAdapterError,
    SessionHealthResult,
    SpawnResult,
    StopResult,
    UnsupportedCapability,
    validate_required_capabilities,
)
