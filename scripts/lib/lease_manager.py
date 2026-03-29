#!/usr/bin/env python3
"""
VNX Lease Manager — High-level terminal lease management.

Provides a clean facade over runtime_coordination lease operations with:
  - All five durable lease operations (acquire, renew, release, expire, recover)
  - Heartbeat semantics with generation/version guard (G-R3)
  - Canonical projection to terminal_state.json (A-R4)
  - TTL-based expiry detection without blind cleanup
  - Routing helper to find available terminals

Design constraints:
  - Every state transition goes through runtime_coordination, which emits
    coordination events — no silent state changes (G-R3)
  - terminal_state.json is always a projection, never the canonical source (A-R4)
  - Blind TTL cleanup via _gc_expired_leases is NOT done here; expiry requires
    an explicit expire() call that creates an auditable event (G-R3, A-R10)
  - Generation/version field prevents stale heartbeat races on renew/release

Shadow mode:
  Set VNX_CANONICAL_LEASE_ACTIVE=1 in environment to signal that this manager
  is the active lease authority. terminal_state_shadow.py will skip its internal
  _gc_expired_leases GC when this flag is set, deferring expiry to expire_stale().
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import (
    InvalidTransitionError,
    acquire_lease,
    expire_lease,
    get_connection,
    get_lease,
    init_schema,
    project_terminal_state,
    recover_lease,
    release_lease,
    renew_lease,
)

TERMINAL_STATE_FILENAME = "terminal_state.json"
_CANONICAL_ENV_FLAG = "VNX_CANONICAL_LEASE_ACTIVE"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class LeaseResult:
    """Returned by every lease operation."""
    terminal_id: str
    state: str
    generation: int
    dispatch_id: Optional[str]
    leased_at: Optional[str]
    expires_at: Optional[str]
    last_heartbeat_at: Optional[str]

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "LeaseResult":
        return cls(
            terminal_id=row["terminal_id"],
            state=row["state"],
            generation=row["generation"],
            dispatch_id=row.get("dispatch_id"),
            leased_at=row.get("leased_at"),
            expires_at=row.get("expires_at"),
            last_heartbeat_at=row.get("last_heartbeat_at"),
        )


# ---------------------------------------------------------------------------
# LeaseManager
# ---------------------------------------------------------------------------

class LeaseManager:
    """High-level facade over runtime_coordination terminal lease operations.

    Usage::

        mgr = LeaseManager(state_dir)
        result = mgr.acquire("T1", dispatch_id="d-001", actor="dispatcher")
        generation = result.generation

        # Worker heartbeat loop:
        mgr.renew("T1", generation=generation, actor="worker")

        # On completion:
        mgr.release("T1", generation=generation, actor="worker")

        # Write terminal_state.json from canonical lease state:
        mgr.project_to_file()
    """

    def __init__(self, state_dir: str | Path, *, auto_init: bool = True) -> None:
        """
        Args:
            state_dir: Path to .vnx-data/state/ directory.
            auto_init: If True, initialize the schema on first use if needed.
                       Set False in tests that manage init themselves.
        """
        self.state_dir = Path(state_dir)
        self._auto_init = auto_init
        self._initialized = False

    def _ensure_init(self) -> None:
        if not self._initialized and self._auto_init:
            init_schema(self.state_dir)
            self._initialized = True

    # -----------------------------------------------------------------------
    # Core lease operations
    # -----------------------------------------------------------------------

    def acquire(
        self,
        terminal_id: str,
        dispatch_id: str,
        *,
        lease_seconds: int = 600,
        actor: str = "runtime",
        reason: Optional[str] = None,
    ) -> LeaseResult:
        """Acquire lease for terminal_id on behalf of dispatch_id.

        Raises InvalidTransitionError if terminal is not idle.
        Increments generation to prevent stale renewal races.
        """
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            row = acquire_lease(
                conn,
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                lease_seconds=lease_seconds,
                actor=actor,
                reason=reason,
            )
            conn.commit()
        return LeaseResult.from_row(row)

    def renew(
        self,
        terminal_id: str,
        generation: int,
        *,
        lease_seconds: int = 600,
        actor: str = "runtime",
    ) -> LeaseResult:
        """Renew lease heartbeat. Generation must match current lease.

        Raises ValueError on generation mismatch (stale renewal rejected).
        Raises InvalidTransitionError if terminal is not leased.
        """
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            row = renew_lease(
                conn,
                terminal_id=terminal_id,
                generation=generation,
                lease_seconds=lease_seconds,
                actor=actor,
            )
            conn.commit()
        return LeaseResult.from_row(row)

    def release(
        self,
        terminal_id: str,
        generation: int,
        *,
        actor: str = "runtime",
        reason: Optional[str] = None,
    ) -> LeaseResult:
        """Release lease and return terminal to idle.

        Generation must match (G-R3: stale reclaim cannot silently steal ownership).
        Raises ValueError on generation mismatch.
        """
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            row = release_lease(
                conn,
                terminal_id=terminal_id,
                generation=generation,
                actor=actor,
                reason=reason,
            )
            conn.commit()
        return LeaseResult.from_row(row)

    def expire(
        self,
        terminal_id: str,
        *,
        actor: str = "reconciler",
        reason: Optional[str] = None,
    ) -> LeaseResult:
        """Mark lease as expired. Used by reconciler for TTL enforcement.

        Creates an auditable lease_expired event (G-R3).
        Raises InvalidTransitionError if current state doesn't allow expiry.
        """
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            row = expire_lease(
                conn,
                terminal_id=terminal_id,
                actor=actor,
                reason=reason,
            )
            conn.commit()
        return LeaseResult.from_row(row)

    def recover(
        self,
        terminal_id: str,
        *,
        actor: str = "reconciler",
        reason: Optional[str] = None,
    ) -> LeaseResult:
        """Transition expired lease through recovering to idle.

        Creates auditable lease_recovering and lease_recovered events (G-R3).
        Raises InvalidTransitionError if current state is not expired.
        """
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            row = recover_lease(
                conn,
                terminal_id=terminal_id,
                actor=actor,
                reason=reason,
            )
            conn.commit()
        return LeaseResult.from_row(row)

    # -----------------------------------------------------------------------
    # Query helpers
    # -----------------------------------------------------------------------

    def get(self, terminal_id: str) -> Optional[LeaseResult]:
        """Return current lease state for terminal, or None if not found."""
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            row = get_lease(conn, terminal_id)
        return LeaseResult.from_row(row) if row else None

    def list_all(self) -> List[LeaseResult]:
        """Return all terminal lease rows ordered by terminal_id."""
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM terminal_leases ORDER BY terminal_id"
            ).fetchall()
        return [LeaseResult.from_row(dict(r)) for r in rows]

    def find_available(self, *, prefer_track: Optional[str] = None) -> Optional[str]:
        """Return the terminal_id of an idle terminal, or None if all are busy.

        Args:
            prefer_track: If given (e.g. "A", "B"), prefer terminals whose
                          terminal_id ends with the track letter. Falls back
                          to any idle terminal.

        Returns:
            A terminal_id string (e.g. "T1") or None.
        """
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM terminal_leases WHERE state = 'idle' ORDER BY terminal_id"
            ).fetchall()

        idle = [dict(r) for r in rows]
        if not idle:
            return None

        if prefer_track:
            track_map = {"A": "T1", "B": "T2", "C": "T3"}
            preferred = track_map.get(prefer_track.upper())
            for r in idle:
                if r["terminal_id"] == preferred:
                    return r["terminal_id"]

        return idle[0]["terminal_id"]

    # -----------------------------------------------------------------------
    # TTL helpers
    # -----------------------------------------------------------------------

    def is_expired_by_ttl(self, terminal_id: str) -> bool:
        """Return True if the terminal's lease TTL has elapsed.

        Pure timestamp check — does NOT write to the database or create events.
        Use expire() to formally record expiry.
        """
        lease = self.get(terminal_id)
        if lease is None or lease.state != "leased":
            return False
        if not lease.expires_at:
            return False
        try:
            expires = datetime.fromisoformat(
                lease.expires_at.replace("Z", "+00:00")
            )
            return expires <= datetime.now(timezone.utc)
        except (ValueError, AttributeError):
            return False

    def expire_stale(
        self,
        *,
        actor: str = "reconciler",
        reason: str = "TTL elapsed",
    ) -> List[str]:
        """Detect and expire all leased terminals whose TTL has elapsed.

        Creates a lease_expired coordination event for each expired terminal
        (G-R3: recovery must be auditable).

        Returns:
            List of terminal_ids that were expired.
        """
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM terminal_leases WHERE state = 'leased'"
            ).fetchall()

        now = datetime.now(timezone.utc)
        expired: List[str] = []

        for row in rows:
            r = dict(row)
            expires_at = r.get("expires_at")
            if not expires_at:
                continue
            try:
                expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            if expires <= now:
                try:
                    self.expire(r["terminal_id"], actor=actor, reason=reason)
                    expired.append(r["terminal_id"])
                except (InvalidTransitionError, KeyError):
                    # Already transitioned by another path; skip silently
                    pass

        return expired

    # -----------------------------------------------------------------------
    # Projection
    # -----------------------------------------------------------------------

    def project(self) -> Dict[str, Any]:
        """Return terminal_state.json-format dict from canonical lease state.

        Never reads terminal_state.json — always derives from terminal_leases.
        """
        self._ensure_init()
        with get_connection(self.state_dir) as conn:
            return project_terminal_state(conn)

    def project_to_file(self) -> Path:
        """Write terminal_state.json from canonical lease state atomically.

        terminal_state.json is always a projection (A-R4). This is the
        canonical way to update it from the lease manager.

        Returns:
            Path to the written terminal_state.json.
        """
        payload = self.project()
        out_path = self.state_dir / TERMINAL_STATE_FILENAME
        out_path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp = tempfile.mkstemp(
            prefix=f"{TERMINAL_STATE_FILENAME}.",
            suffix=".tmp",
            dir=str(out_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, out_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

        return out_path


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------

def load_manager(state_dir: str | Path) -> LeaseManager:
    """Return a LeaseManager for state_dir, auto-initializing the schema."""
    return LeaseManager(state_dir, auto_init=True)


def canonical_lease_active() -> bool:
    """Return True when VNX_CANONICAL_LEASE_ACTIVE=1 is set in environment."""
    return os.environ.get(_CANONICAL_ENV_FLAG, "0").strip() == "1"
