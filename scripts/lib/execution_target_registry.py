#!/usr/bin/env python3
"""
VNX Execution Target Registry — CRUD and query operations for execution targets.

Manages the lifecycle of execution targets (interactive tmux, headless CLI,
channel adapters) in the canonical runtime state. Provides target registration,
deregistration, health management, capability queries, and routing-eligible
target selection.

Contracts (from 30_FPC_EXECUTION_CONTRACTS.md):
  - One active target per terminal maximum (2.1-1)
  - Target type fixed per registration; change requires deregister + re-register (2.1-2)
  - Capabilities declared, not inferred (2.1-3)
  - Health queryable; unhealthy/offline targets excluded from routing (2.1-4)
  - Channel adapters do not occupy T1/T2/T3 slots (2.1-5)

Governance:
  G-R1: Execution target selection is explicit and reviewable
  G-R8: All routing decisions emit coordination_events
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from runtime_coordination import (
    _append_event,
    _now_utc,
    get_connection,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_TARGET_TYPES = frozenset({
    "interactive_tmux_claude",
    "interactive_tmux_codex",
    "headless_claude_cli",
    "headless_codex_cli",
    "channel_adapter",
})

VALID_TASK_CLASSES = frozenset({
    "coding_interactive",
    "research_structured",
    "docs_synthesis",
    "ops_watchdog",
    "channel_response",
})

VALID_HEALTH_STATES = frozenset({
    "healthy",
    "degraded",
    "unhealthy",
    "offline",
})

ROUTING_ELIGIBLE_HEALTH = frozenset({"healthy", "degraded"})

INTERACTIVE_TARGET_TYPES = frozenset({
    "interactive_tmux_claude",
    "interactive_tmux_codex",
})

HEADLESS_TARGET_TYPES = frozenset({
    "headless_claude_cli",
    "headless_codex_cli",
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TargetRegistryError(Exception):
    """Base error for target registry operations."""


class TargetExistsError(TargetRegistryError):
    """Raised when registering a target_id that already exists."""


class TargetNotFoundError(TargetRegistryError):
    """Raised when a target_id is not in the registry."""


class TerminalOccupiedError(TargetRegistryError):
    """Raised when a terminal already has an active (non-offline) target."""


class InvalidTargetTypeError(TargetRegistryError):
    """Raised for unknown target types."""


class InvalidHealthStateError(TargetRegistryError):
    """Raised for unknown health states."""


class InvalidCapabilityError(TargetRegistryError):
    """Raised for unknown task class capabilities."""


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TargetRecord:
    """Parsed execution target from the registry."""
    target_id: str
    target_type: str
    terminal_id: Optional[str]
    capabilities: List[str]
    health: str
    health_checked_at: Optional[str]
    model: Optional[str]
    registered_at: str
    updated_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> TargetRecord:
        caps = row.get("capabilities_json", "[]")
        if isinstance(caps, str):
            caps = json.loads(caps)
        meta = row.get("metadata_json", "{}")
        if isinstance(meta, str):
            meta = json.loads(meta)
        return cls(
            target_id=row["target_id"],
            target_type=row["target_type"],
            terminal_id=row.get("terminal_id"),
            capabilities=caps,
            health=row.get("health", "offline"),
            health_checked_at=row.get("health_checked_at"),
            model=row.get("model"),
            registered_at=row.get("registered_at", ""),
            updated_at=row.get("updated_at", ""),
            metadata=meta,
        )

    @property
    def is_routing_eligible(self) -> bool:
        return self.health in ROUTING_ELIGIBLE_HEALTH

    @property
    def is_interactive(self) -> bool:
        return self.target_type in INTERACTIVE_TARGET_TYPES

    @property
    def is_headless(self) -> bool:
        return self.target_type in HEADLESS_TARGET_TYPES

    def supports_task_class(self, task_class: str) -> bool:
        return task_class in self.capabilities


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_target_type(target_type: str) -> None:
    if target_type not in VALID_TARGET_TYPES:
        raise InvalidTargetTypeError(
            f"Unknown target type: {target_type!r}. Valid: {sorted(VALID_TARGET_TYPES)}"
        )


def _validate_health(health: str) -> None:
    if health not in VALID_HEALTH_STATES:
        raise InvalidHealthStateError(
            f"Unknown health state: {health!r}. Valid: {sorted(VALID_HEALTH_STATES)}"
        )


def _validate_capabilities(capabilities: List[str]) -> None:
    for cap in capabilities:
        if cap not in VALID_TASK_CLASSES:
            raise InvalidCapabilityError(
                f"Unknown task class capability: {cap!r}. Valid: {sorted(VALID_TASK_CLASSES)}"
            )


# ---------------------------------------------------------------------------
# ExecutionTargetRegistry
# ---------------------------------------------------------------------------

class ExecutionTargetRegistry:
    """CRUD and query operations for the execution_targets table.

    All mutations emit coordination_events for audit (G-R1, G-R8).

    Args:
        state_dir: Directory containing runtime_coordination.db.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self._state_dir = Path(state_dir)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        target_id: str,
        target_type: str,
        *,
        terminal_id: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        health: str = "offline",
        model: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "registry",
    ) -> TargetRecord:
        """Register a new execution target.

        Enforces:
          - target_id uniqueness
          - One active (non-offline) target per terminal (2.1-1)
          - Channel adapters must have terminal_id=None (2.1-5)
          - Valid target_type, health, and capabilities

        Returns the created TargetRecord.
        """
        _validate_target_type(target_type)
        _validate_health(health)

        if capabilities is None:
            capabilities = []
        _validate_capabilities(capabilities)

        if target_type == "channel_adapter" and terminal_id is not None:
            raise TargetRegistryError(
                "Channel adapters must not occupy terminal slots (2.1-5). "
                f"Got terminal_id={terminal_id!r} for channel_adapter target."
            )

        caps_json = json.dumps(capabilities)
        meta_json = json.dumps(metadata or {})
        now = _now_utc()

        with get_connection(self._state_dir) as conn:
            existing = conn.execute(
                "SELECT target_id FROM execution_targets WHERE target_id = ?",
                (target_id,),
            ).fetchone()
            if existing:
                raise TargetExistsError(f"Target already registered: {target_id!r}")

            if terminal_id is not None and health != "offline":
                occupied = conn.execute(
                    "SELECT target_id FROM execution_targets "
                    "WHERE terminal_id = ? AND health != 'offline' AND target_id != ?",
                    (terminal_id, target_id),
                ).fetchone()
                if occupied:
                    raise TerminalOccupiedError(
                        f"Terminal {terminal_id!r} already has active target: "
                        f"{occupied['target_id']!r} (2.1-1)"
                    )

            conn.execute(
                """
                INSERT INTO execution_targets
                    (target_id, target_type, terminal_id, capabilities_json,
                     health, health_checked_at, model, registered_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (target_id, target_type, terminal_id, caps_json,
                 health, now if health != "offline" else None, model, now, now, meta_json),
            )

            _append_event(
                conn,
                event_type="target_registered",
                entity_type="execution_target",
                entity_id=target_id,
                to_state=health,
                actor=actor,
                reason=f"registered {target_type} target",
                metadata={
                    "target_type": target_type,
                    "terminal_id": terminal_id,
                    "capabilities": capabilities,
                    "model": model,
                },
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM execution_targets WHERE target_id = ?", (target_id,)
            ).fetchone()

        return TargetRecord.from_row(dict(row))

    def deregister(
        self,
        target_id: str,
        *,
        actor: str = "registry",
        reason: Optional[str] = None,
    ) -> None:
        """Deregister a target by setting health to offline.

        The row remains for audit. Re-registration requires a new register() call
        with a different target_id, or deregister + register with the same ID after
        the row is removed.
        """
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM execution_targets WHERE target_id = ?", (target_id,)
            ).fetchone()
            if row is None:
                raise TargetNotFoundError(f"Target not found: {target_id!r}")

            from_health = row["health"]
            now = _now_utc()
            conn.execute(
                "UPDATE execution_targets SET health = 'offline', updated_at = ? WHERE target_id = ?",
                (now, target_id),
            )

            _append_event(
                conn,
                event_type="target_deregistered",
                entity_type="execution_target",
                entity_id=target_id,
                from_state=from_health,
                to_state="offline",
                actor=actor,
                reason=reason or "deregistered",
            )
            conn.commit()

    def remove(
        self,
        target_id: str,
        *,
        actor: str = "registry",
    ) -> None:
        """Permanently remove a target row. Use deregister() for soft removal."""
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM execution_targets WHERE target_id = ?", (target_id,)
            ).fetchone()
            if row is None:
                raise TargetNotFoundError(f"Target not found: {target_id!r}")

            conn.execute(
                "DELETE FROM execution_targets WHERE target_id = ?", (target_id,)
            )
            _append_event(
                conn,
                event_type="target_removed",
                entity_type="execution_target",
                entity_id=target_id,
                from_state=row["health"],
                actor=actor,
                reason="permanently removed from registry",
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Health management
    # ------------------------------------------------------------------

    def update_health(
        self,
        target_id: str,
        health: str,
        *,
        actor: str = "health_checker",
        reason: Optional[str] = None,
    ) -> TargetRecord:
        """Update a target's health state.

        Enforces one-active-per-terminal when transitioning to a routing-eligible state.
        """
        _validate_health(health)

        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM execution_targets WHERE target_id = ?", (target_id,)
            ).fetchone()
            if row is None:
                raise TargetNotFoundError(f"Target not found: {target_id!r}")

            from_health = row["health"]
            terminal_id = row["terminal_id"]

            if (terminal_id is not None
                    and health in ROUTING_ELIGIBLE_HEALTH
                    and from_health not in ROUTING_ELIGIBLE_HEALTH):
                occupied = conn.execute(
                    "SELECT target_id FROM execution_targets "
                    "WHERE terminal_id = ? AND health IN ('healthy', 'degraded') "
                    "AND target_id != ?",
                    (terminal_id, target_id),
                ).fetchone()
                if occupied:
                    raise TerminalOccupiedError(
                        f"Terminal {terminal_id!r} already has active target: "
                        f"{occupied['target_id']!r}. Deregister it first."
                    )

            now = _now_utc()
            conn.execute(
                "UPDATE execution_targets SET health = ?, health_checked_at = ?, updated_at = ? "
                "WHERE target_id = ?",
                (health, now, now, target_id),
            )

            _append_event(
                conn,
                event_type="target_health_changed",
                entity_type="execution_target",
                entity_id=target_id,
                from_state=from_health,
                to_state=health,
                actor=actor,
                reason=reason or f"health changed: {from_health} -> {health}",
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM execution_targets WHERE target_id = ?", (target_id,)
            ).fetchone()

        return TargetRecord.from_row(dict(row))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, target_id: str) -> Optional[TargetRecord]:
        """Return a single target or None."""
        with get_connection(self._state_dir) as conn:
            row = conn.execute(
                "SELECT * FROM execution_targets WHERE target_id = ?", (target_id,)
            ).fetchone()
        if row is None:
            return None
        return TargetRecord.from_row(dict(row))

    def list_all(self) -> List[TargetRecord]:
        """Return all registered targets."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute("SELECT * FROM execution_targets ORDER BY target_id").fetchall()
        return [TargetRecord.from_row(dict(r)) for r in rows]

    def list_by_type(self, target_type: str) -> List[TargetRecord]:
        """Return targets of a specific type."""
        _validate_target_type(target_type)
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM execution_targets WHERE target_type = ? ORDER BY target_id",
                (target_type,),
            ).fetchall()
        return [TargetRecord.from_row(dict(r)) for r in rows]

    def list_by_terminal(self, terminal_id: str) -> List[TargetRecord]:
        """Return all targets bound to a terminal."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM execution_targets WHERE terminal_id = ? ORDER BY target_id",
                (terminal_id,),
            ).fetchall()
        return [TargetRecord.from_row(dict(r)) for r in rows]

    def list_routing_eligible(
        self,
        task_class: str,
        *,
        terminal_id: Optional[str] = None,
    ) -> List[TargetRecord]:
        """Return targets eligible to execute a given task class.

        Filters by:
          - Health in (healthy, degraded) — R-6
          - Capability includes task_class — R-5
          - Terminal match if specified
        Sorted: healthy before degraded, then by target_id for stability.
        """
        with get_connection(self._state_dir) as conn:
            query = (
                "SELECT * FROM execution_targets "
                "WHERE health IN ('healthy', 'degraded')"
            )
            params: list = []

            if terminal_id is not None:
                query += " AND terminal_id = ?"
                params.append(terminal_id)

            query += " ORDER BY CASE health WHEN 'healthy' THEN 0 ELSE 1 END, target_id"
            rows = conn.execute(query, params).fetchall()

        targets = [TargetRecord.from_row(dict(r)) for r in rows]
        return [t for t in targets if t.supports_task_class(task_class)]

    def list_headless_targets(
        self,
        *,
        healthy_only: bool = True,
    ) -> List[TargetRecord]:
        """Return headless CLI targets, optionally filtered to routing-eligible only."""
        with get_connection(self._state_dir) as conn:
            if healthy_only:
                rows = conn.execute(
                    "SELECT * FROM execution_targets "
                    "WHERE target_type IN ('headless_claude_cli', 'headless_codex_cli') "
                    "AND health IN ('healthy', 'degraded') "
                    "ORDER BY target_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM execution_targets "
                    "WHERE target_type IN ('headless_claude_cli', 'headless_codex_cli') "
                    "ORDER BY target_id"
                ).fetchall()
        return [TargetRecord.from_row(dict(r)) for r in rows]
