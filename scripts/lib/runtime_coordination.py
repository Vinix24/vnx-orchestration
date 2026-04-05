#!/usr/bin/env python3
"""
VNX Runtime Coordination — Facade module.

Backward-compatible re-export surface. All symbols previously defined here
are still importable from this module.

Sub-modules:
  coordination_db       — State types, DB connection, schema, raw queries (leaf)
  runtime_state_machine — Validation helpers, dispatch/attempt operations
  lease_operations      — Raw lease lifecycle functions
"""

from __future__ import annotations

# Re-exports from coordination_db (leaf module — constants, exceptions, DB)
from coordination_db import (  # noqa: F401
    ACCEPTED_OR_BEYOND_STATES,
    DB_FILENAME,
    DISPATCH_STATES,
    DISPATCH_TRANSITIONS,
    LEASE_STATES,
    LEASE_TRANSITIONS,
    TERMINAL_DISPATCH_STATES,
    DuplicateTransitionError,
    InvalidStateError,
    InvalidTransitionError,
    _append_event,
    _dump,
    _new_event_id,
    _now_utc,
    db_path_from_state_dir,
    get_connection,
    get_dispatch,
    get_events,
    get_lease,
    init_schema,
    project_terminal_state,
)

# Re-exports from runtime_state_machine
from runtime_state_machine import (  # noqa: F401
    _check_idempotent_noop,
    create_attempt,
    increment_attempt_count,
    is_accepted_or_beyond,
    is_terminal_dispatch_state,
    register_dispatch,
    transition_dispatch,
    transition_dispatch_idempotent,
    update_attempt,
    validate_dispatch_state,
    validate_dispatch_transition,
    validate_lease_state,
    validate_lease_transition,
)

# Re-exports from lease_operations
from lease_operations import (  # noqa: F401
    _default_expires,
    _release_all_leases_bulk,
    acquire_lease,
    expire_lease,
    recover_lease,
    release_all_leases,
    release_lease,
    renew_lease,
)
