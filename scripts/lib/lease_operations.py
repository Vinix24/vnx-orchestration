#!/usr/bin/env python3
"""VNX Lease Operations — raw terminal lease lifecycle functions."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from coordination_db import InvalidTransitionError, _append_event, _now_utc
from runtime_state_machine import validate_lease_transition


def _default_expires(seconds: int = 600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"


def acquire_lease(
    conn: sqlite3.Connection,
    *,
    terminal_id: str,
    dispatch_id: str,
    lease_seconds: int = 600,
    actor: str = "runtime",
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Acquire a lease for a terminal. Raises InvalidTransitionError if not idle."""
    row = conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Terminal lease record not found for: {terminal_id!r}. "
                       f"Was the schema initialized?")

    from_state = row["state"]
    validate_lease_transition(from_state, "leased")

    now = _now_utc()
    new_generation = row["generation"] + 1
    expires_at = _default_expires(lease_seconds)

    conn.execute(
        """
        UPDATE terminal_leases
        SET state = 'leased', dispatch_id = ?, generation = ?,
            leased_at = ?, expires_at = ?, last_heartbeat_at = ?,
            released_at = NULL
        WHERE terminal_id = ?
        """,
        (dispatch_id, new_generation, now, expires_at, now, terminal_id),
    )
    _append_event(
        conn, event_type="lease_acquired", entity_type="lease", entity_id=terminal_id,
        from_state=from_state, to_state="leased", actor=actor,
        reason=reason or f"dispatch {dispatch_id}",
        metadata={"dispatch_id": dispatch_id, "generation": new_generation, "expires_at": expires_at},
    )
    return dict(conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone())


def renew_lease(
    conn: sqlite3.Connection,
    *,
    terminal_id: str,
    generation: int,
    lease_seconds: int = 600,
    actor: str = "runtime",
) -> Dict[str, Any]:
    """Renew a lease heartbeat. Generation must match to prevent stale renewal."""
    row = conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Terminal not found: {terminal_id!r}")

    if row["state"] != "leased":
        raise InvalidTransitionError(
            f"Cannot renew lease for {terminal_id!r}: current state is {row['state']!r}, expected 'leased'"
        )
    if row["generation"] != generation:
        raise ValueError(
            f"Lease generation mismatch for {terminal_id!r}: "
            f"provided {generation}, current {row['generation']}. Stale renewal rejected."
        )

    now = _now_utc()
    expires_at = _default_expires(lease_seconds)
    conn.execute(
        "UPDATE terminal_leases SET expires_at = ?, last_heartbeat_at = ? WHERE terminal_id = ?",
        (expires_at, now, terminal_id),
    )
    _append_event(
        conn, event_type="lease_renewed", entity_type="lease", entity_id=terminal_id,
        from_state="leased", to_state="leased", actor=actor,
        metadata={"generation": generation, "new_expires_at": expires_at},
    )
    return dict(conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone())


def release_lease(
    conn: sqlite3.Connection,
    *,
    terminal_id: str,
    generation: int,
    actor: str = "runtime",
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Release a lease, returning terminal to idle. Generation must match (G-R3)."""
    row = conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Terminal not found: {terminal_id!r}")

    from_state = row["state"]
    if from_state not in ("leased", "recovering"):
        raise InvalidTransitionError(
            f"Cannot release lease for {terminal_id!r}: current state is {from_state!r}"
        )
    if row["generation"] != generation:
        raise ValueError(
            f"Lease generation mismatch for {terminal_id!r}: "
            f"provided {generation}, current {row['generation']}. Stale release rejected."
        )

    now = _now_utc()
    conn.execute(
        """
        UPDATE terminal_leases
        SET state = 'released', dispatch_id = NULL,
            expires_at = NULL, released_at = ?
        WHERE terminal_id = ?
        """,
        (now, terminal_id),
    )
    _append_event(
        conn, event_type="lease_released", entity_type="lease", entity_id=terminal_id,
        from_state=from_state, to_state="released", actor=actor,
        reason=reason, metadata={"generation": generation},
    )
    # Immediately move released -> idle (released is a transient state)
    conn.execute(
        "UPDATE terminal_leases SET state = 'idle' WHERE terminal_id = ?", (terminal_id,)
    )
    _append_event(
        conn, event_type="lease_returned_idle", entity_type="lease", entity_id=terminal_id,
        from_state="released", to_state="idle", actor=actor,
    )
    return dict(conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone())


def expire_lease(
    conn: sqlite3.Connection,
    *,
    terminal_id: str,
    actor: str = "reconciler",
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark a lease as expired (used by reconciler for TTL enforcement)."""
    row = conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Terminal not found: {terminal_id!r}")

    from_state = row["state"]
    validate_lease_transition(from_state, "expired")
    conn.execute(
        "UPDATE terminal_leases SET state = 'expired' WHERE terminal_id = ?", (terminal_id,)
    )
    _append_event(
        conn, event_type="lease_expired", entity_type="lease", entity_id=terminal_id,
        from_state=from_state, to_state="expired", actor=actor,
        reason=reason or "TTL elapsed",
        metadata={"dispatch_id": row["dispatch_id"], "generation": row["generation"]},
    )
    return dict(conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone())


def recover_lease(
    conn: sqlite3.Connection,
    *,
    terminal_id: str,
    actor: str = "reconciler",
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Transition an expired lease to recovering, then idle."""
    row = conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Terminal not found: {terminal_id!r}")

    from_state = row["state"]
    validate_lease_transition(from_state, "recovering")
    conn.execute(
        "UPDATE terminal_leases SET state = 'recovering' WHERE terminal_id = ?", (terminal_id,)
    )
    _append_event(
        conn, event_type="lease_recovering", entity_type="lease", entity_id=terminal_id,
        from_state=from_state, to_state="recovering", actor=actor,
        reason=reason or "expired lease recovery",
        metadata={"dispatch_id": row["dispatch_id"], "generation": row["generation"]},
    )
    now = _now_utc()
    conn.execute(
        """
        UPDATE terminal_leases
        SET state = 'idle', dispatch_id = NULL, expires_at = NULL,
            leased_at = NULL, released_at = ?
        WHERE terminal_id = ?
        """,
        (now, terminal_id),
    )
    _append_event(
        conn, event_type="lease_recovered", entity_type="lease", entity_id=terminal_id,
        from_state="recovering", to_state="idle", actor=actor, reason=reason,
    )
    return dict(conn.execute(
        "SELECT * FROM terminal_leases WHERE terminal_id = ?", (terminal_id,)
    ).fetchone())


# ---------------------------------------------------------------------------
# Bulk lease operations (BOOT-9/10/11)
# ---------------------------------------------------------------------------


def _release_all_leases_bulk(
    conn: sqlite3.Connection, *, actor: str, reason: str,
) -> tuple:
    """BOOT-10: Release all non-idle leases with generation increment."""
    now = _now_utc()
    released: List[str] = []
    already_idle: List[str] = []
    for row in conn.execute("SELECT * FROM terminal_leases").fetchall():
        terminal_id = row["terminal_id"]
        old_state = row["state"]
        old_generation = row["generation"]
        if old_state == "idle":
            already_idle.append(terminal_id)
            continue
        new_generation = old_generation + 1
        conn.execute(
            """
            UPDATE terminal_leases
            SET state = 'idle', dispatch_id = NULL, leased_at = NULL,
                expires_at = NULL, last_heartbeat_at = NULL,
                released_at = ?, generation = ?
            WHERE terminal_id = ?
            """,
            (now, new_generation, terminal_id),
        )
        _append_event(
            conn, event_type="lease_released", entity_type="lease", entity_id=terminal_id,
            from_state=old_state, to_state="idle", actor=actor, reason=reason,
            metadata={"generation": old_generation, "new_generation": new_generation,
                      "dispatch_id": row["dispatch_id"]},
        )
        released.append(terminal_id)
    return released, already_idle


def release_all_leases(
    conn: sqlite3.Connection,
    *,
    actor: str = "chain_closeout",
    reason: str = "chain_boundary_cleanup",
    force: bool = False,
) -> Dict[str, Any]:
    """Release all non-idle leases at chain boundary (BOOT-9/10/11 sequence)."""
    # BOOT-9: Check for non-terminal dispatches
    non_terminal = [dict(r) for r in conn.execute(
        "SELECT dispatch_id, state FROM dispatches WHERE state NOT IN (?, ?, ?)",
        ("completed", "expired", "dead_letter"),
    ).fetchall()]

    if non_terminal and not force:
        return {
            "released": [], "already_idle": [],
            "non_terminal_dispatches": non_terminal, "blocked": True, "all_idle": False,
            "message": (
                f"WARN: {len(non_terminal)} non-terminal dispatch(es) exist. "
                "Use --force to proceed with lease cleanup."
            ),
        }

    released, already_idle = _release_all_leases_bulk(conn, actor=actor, reason=reason)

    # BOOT-11: Verify all leases are idle
    remaining = [dict(r) for r in conn.execute(
        "SELECT terminal_id, state FROM terminal_leases WHERE state != 'idle'"
    ).fetchall()]
    if remaining:
        return {
            "released": released, "already_idle": already_idle,
            "non_terminal_dispatches": non_terminal, "blocked": False, "all_idle": False,
            "error": (
                f"Verification failed: {len(remaining)} non-idle lease(s) remain "
                "after closeout cleanup"
            ),
            "remaining": remaining,
        }

    return {
        "released": released, "already_idle": already_idle,
        "non_terminal_dispatches": non_terminal, "blocked": False, "all_idle": True,
    }
