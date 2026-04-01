#!/usr/bin/env python3
"""
VNX Runtime State Reconciler — Canonical mismatch detection between
LeaseManager and runtime core broker state.

Detects:
  - zombie_lease: terminal holds a live lease but its dispatch is in a
    terminal or failed state (completed, expired, failed_delivery, etc.)
  - ghost_dispatch: dispatch is in an active delivery state but the
    terminal's lease is idle — work is claimed but ownership is invisible
  - queue_projection_stale: at least one dispatch is active in the DB but
    the queue projection (pr_queue_state.json) shows nothing in progress
  - generation_snapshot_drift: a caller-supplied generation snapshot
    disagrees with the current DB generation for that terminal

Design invariants:
  - Read-only by default: detection never transitions state (G-R3)
  - Every mismatch has a deterministic message suitable for operator review
  - Mismatches are classified by severity so callers can gate on blocking
    conditions without parsing message text
  - RuntimeCore.check_terminal() calls reconcile_for_terminal() so dispatch
    safety checks see the same truth as operator tooling (PR-2 gate)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import get_connection, init_schema

# ---------------------------------------------------------------------------
# Mismatch type constants
# ---------------------------------------------------------------------------

ZOMBIE_LEASE = "zombie_lease"
GHOST_DISPATCH = "ghost_dispatch"
QUEUE_PROJECTION_STALE = "queue_projection_stale"
GENERATION_SNAPSHOT_DRIFT = "generation_snapshot_drift"

# Dispatch states where the terminal is no longer being used but a lease
# might still be held — these trigger zombie_lease detection.
_INACTIVE_DISPATCH_STATES = frozenset({
    "completed",
    "expired",
    "dead_letter",
    "failed_delivery",
    "timed_out",
    "recovered",
})

# Dispatch states where the terminal is actively being used — a missing
# or idle lease is a ghost_dispatch.
_ACTIVE_DELIVERY_STATES = frozenset({
    "claimed",
    "delivering",
    "accepted",
    "running",
})


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RuntimeStateMismatch:
    """A single detected divergence between lease and dispatch state."""

    mismatch_type: str
    """One of: zombie_lease, ghost_dispatch, queue_projection_stale, generation_snapshot_drift."""

    severity: str
    """'blocking' or 'warning'. Blocking mismatches prevent safe dispatch."""

    message: str
    """Operator-readable diagnosis — complete sentence, no jargon."""

    terminal_id: Optional[str] = None
    dispatch_id: Optional[str] = None
    lease_state: Optional[str] = None
    dispatch_state: Optional[str] = None
    generation: Optional[int] = None
    snapshot_generation: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mismatch_type": self.mismatch_type,
            "severity": self.severity,
            "message": self.message,
            "terminal_id": self.terminal_id,
            "dispatch_id": self.dispatch_id,
            "lease_state": self.lease_state,
            "dispatch_state": self.dispatch_state,
            "generation": self.generation,
            "snapshot_generation": self.snapshot_generation,
            "metadata": self.metadata,
        }


@dataclass
class RuntimeStateDiagnostic:
    """Full result of a reconciliation pass."""

    checked_at: str
    mismatches: List[RuntimeStateMismatch] = field(default_factory=list)
    terminal_count: int = 0
    dispatch_count: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def has_blocking(self) -> bool:
        return any(m.severity == "blocking" for m in self.mismatches)

    @property
    def is_clean(self) -> bool:
        return len(self.mismatches) == 0 and len(self.errors) == 0

    def mismatches_for_terminal(self, terminal_id: str) -> List[RuntimeStateMismatch]:
        return [m for m in self.mismatches if m.terminal_id == terminal_id]

    def summary(self) -> str:
        lines = [
            f"Runtime state diagnostic at {self.checked_at}",
            f"  Terminals checked: {self.terminal_count}",
            f"  Dispatches checked: {self.dispatch_count}",
            f"  Mismatches: {len(self.mismatches)} "
            f"({'blocking' if self.has_blocking else 'none blocking'})",
        ]
        for m in self.mismatches:
            lines.append(f"  [{m.severity.upper()}] {m.mismatch_type}: {m.message}")
        if self.errors:
            lines.append("  Errors:")
            for e in self.errors:
                lines.append(f"    {e}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "terminal_count": self.terminal_count,
            "dispatch_count": self.dispatch_count,
            "has_blocking": self.has_blocking,
            "is_clean": self.is_clean,
            "mismatch_count": len(self.mismatches),
            "mismatches": [m.to_dict() for m in self.mismatches],
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# RuntimeStateReconciler
# ---------------------------------------------------------------------------

class RuntimeStateReconciler:
    """Detects divergence between LeaseManager and runtime core broker state.

    Read-only by default — never transitions state. Returns a
    RuntimeStateDiagnostic so callers can decide how to react.

    Args:
        state_dir:       Directory containing runtime_coordination.db.
        projection_file: Optional path to pr_queue_state.json for
                         queue-projection staleness detection.
    """

    def __init__(
        self,
        state_dir: str | Path,
        projection_file: Optional[str | Path] = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._projection_file = Path(projection_file) if projection_file else None
        init_schema(self._state_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reconcile(
        self,
        *,
        snapshot_generations: Optional[Dict[str, int]] = None,
    ) -> RuntimeStateDiagnostic:
        """Run a full mismatch-detection pass.

        Args:
            snapshot_generations: Optional dict mapping terminal_id to the
                generation number recorded in a previous snapshot. When
                provided, terminals whose current DB generation differs will
                be reported as generation_snapshot_drift.

        Returns:
            RuntimeStateDiagnostic with all detected mismatches.
        """
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        diag = RuntimeStateDiagnostic(checked_at=now_iso)

        try:
            leases, dispatches = self._load_state()
        except Exception as exc:
            diag.errors.append(f"Failed to load runtime state: {exc}")
            return diag

        diag.terminal_count = len(leases)
        diag.dispatch_count = len(dispatches)

        self._detect_zombie_leases(diag, leases, dispatches)
        self._detect_ghost_dispatches(diag, leases, dispatches)

        if self._projection_file:
            self._detect_queue_projection_stale(diag, dispatches)

        if snapshot_generations:
            self._detect_generation_drift(diag, leases, snapshot_generations)

        return diag

    def reconcile_for_terminal(
        self,
        terminal_id: str,
        *,
        snapshot_generation: Optional[int] = None,
    ) -> List[RuntimeStateMismatch]:
        """Return mismatches affecting a single terminal.

        Used by RuntimeCore.check_terminal() so dispatch safety checks see
        the same truth as operator tooling.

        Args:
            terminal_id:        Terminal to check (e.g. "T2").
            snapshot_generation: If provided, also check generation drift.

        Returns:
            List of RuntimeStateMismatch (empty if terminal state is clean).
        """
        snap = {terminal_id: snapshot_generation} if snapshot_generation is not None else None
        result = self.reconcile(snapshot_generations=snap)
        return result.mismatches_for_terminal(terminal_id)

    # ------------------------------------------------------------------
    # Detection helpers
    # ------------------------------------------------------------------

    def _load_state(self):
        """Return (leases_by_terminal, dispatches_by_id) from the DB."""
        with get_connection(self._state_dir) as conn:
            lease_rows = conn.execute(
                "SELECT * FROM terminal_leases"
            ).fetchall()
            dispatch_rows = conn.execute(
                "SELECT * FROM dispatches"
            ).fetchall()

        leases: Dict[str, Dict[str, Any]] = {
            dict(r)["terminal_id"]: dict(r) for r in lease_rows
        }
        dispatches: Dict[str, Dict[str, Any]] = {
            dict(r)["dispatch_id"]: dict(r) for r in dispatch_rows
        }
        return leases, dispatches

    def _detect_zombie_leases(
        self,
        diag: RuntimeStateDiagnostic,
        leases: Dict[str, Dict[str, Any]],
        dispatches: Dict[str, Dict[str, Any]],
    ) -> None:
        """Detect leases that are held but whose dispatch is done/failed."""
        for terminal_id, lease in leases.items():
            if lease.get("state") != "leased":
                continue

            dispatch_id = lease.get("dispatch_id")
            if not dispatch_id:
                continue

            dispatch = dispatches.get(dispatch_id)
            if dispatch is None:
                # Lease references a dispatch not in DB at all — also a mismatch
                diag.mismatches.append(RuntimeStateMismatch(
                    mismatch_type=ZOMBIE_LEASE,
                    severity="blocking",
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    lease_state="leased",
                    dispatch_state=None,
                    generation=lease.get("generation"),
                    message=(
                        f"Terminal {terminal_id} holds an active lease for dispatch "
                        f"{dispatch_id!r} but that dispatch does not exist in the "
                        f"coordination DB. The lease was not released and cannot be "
                        f"attributed to any known dispatch. Manual lease release required."
                    ),
                ))
                continue

            dispatch_state = dispatch.get("state", "")
            if dispatch_state in _INACTIVE_DISPATCH_STATES:
                diag.mismatches.append(RuntimeStateMismatch(
                    mismatch_type=ZOMBIE_LEASE,
                    severity="blocking",
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    lease_state="leased",
                    dispatch_state=dispatch_state,
                    generation=lease.get("generation"),
                    message=(
                        f"Terminal {terminal_id} holds an active lease for dispatch "
                        f"{dispatch_id!r} but that dispatch is in state {dispatch_state!r}. "
                        f"The terminal appears blocked but the associated work has already "
                        f"ended. The lease must be released to allow new dispatches."
                    ),
                ))

    def _detect_ghost_dispatches(
        self,
        diag: RuntimeStateDiagnostic,
        leases: Dict[str, Dict[str, Any]],
        dispatches: Dict[str, Dict[str, Any]],
    ) -> None:
        """Detect dispatches in active delivery state whose terminal has no lease."""
        for dispatch_id, dispatch in dispatches.items():
            dispatch_state = dispatch.get("state", "")
            if dispatch_state not in _ACTIVE_DELIVERY_STATES:
                continue

            terminal_id = dispatch.get("terminal_id")
            if not terminal_id:
                continue

            lease = leases.get(terminal_id)
            lease_state = lease.get("state") if lease else None

            if lease_state == "idle" or lease is None:
                diag.mismatches.append(RuntimeStateMismatch(
                    mismatch_type=GHOST_DISPATCH,
                    severity="blocking",
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    lease_state=lease_state or "not_found",
                    dispatch_state=dispatch_state,
                    message=(
                        f"Dispatch {dispatch_id!r} is in state {dispatch_state!r} on "
                        f"terminal {terminal_id} but the terminal's lease state is "
                        f"{lease_state or 'not found'!r}. Work is executing without "
                        f"visible lease ownership — operator tooling would show the "
                        f"terminal as idle while a dispatch is actively in progress."
                    ),
                ))

    def _detect_queue_projection_stale(
        self,
        diag: RuntimeStateDiagnostic,
        dispatches: Dict[str, Dict[str, Any]],
    ) -> None:
        """Detect when active dispatches exist but queue projection says nothing active."""
        import json

        if not self._projection_file or not self._projection_file.is_file():
            return

        try:
            projection = json.loads(self._projection_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            diag.errors.append(f"Could not read projection file {self._projection_file}: {exc}")
            return

        # Active PRs according to projection
        active_in_projection = set(projection.get("active", []))

        # Dispatches that are actively executing
        for dispatch_id, dispatch in dispatches.items():
            if dispatch.get("state") not in _ACTIVE_DELIVERY_STATES:
                continue

            terminal_id = dispatch.get("terminal_id")

            # Look for a PR reference in dispatch metadata
            pr_ref = dispatch.get("pr_ref") or dispatch.get("pr_id")

            if pr_ref and pr_ref not in active_in_projection:
                # A dispatch for this PR is executing but projection doesn't list it
                diag.mismatches.append(RuntimeStateMismatch(
                    mismatch_type=QUEUE_PROJECTION_STALE,
                    severity="warning",
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    dispatch_state=dispatch.get("state"),
                    metadata={
                        "pr_ref": pr_ref,
                        "active_in_projection": sorted(active_in_projection),
                    },
                    message=(
                        f"Dispatch {dispatch_id!r} for {pr_ref} is in state "
                        f"{dispatch.get('state')!r} on terminal {terminal_id} but "
                        f"the queue projection lists {pr_ref} as "
                        f"{'queued' if not active_in_projection else 'not active'!r}. "
                        f"Operators would see 'In Progress: None' while this dispatch "
                        f"is executing. The projection must be updated to match runtime truth."
                    ),
                ))
            elif not pr_ref and not active_in_projection:
                # No PR ref but projection has no active work at all
                diag.mismatches.append(RuntimeStateMismatch(
                    mismatch_type=QUEUE_PROJECTION_STALE,
                    severity="warning",
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    dispatch_state=dispatch.get("state"),
                    metadata={"active_in_projection": []},
                    message=(
                        f"Dispatch {dispatch_id!r} is in state {dispatch.get('state')!r} "
                        f"on terminal {terminal_id} but the queue projection shows no "
                        f"active dispatches. Operators would see 'In Progress: None' "
                        f"while a dispatch is executing."
                    ),
                ))

    def _detect_generation_drift(
        self,
        diag: RuntimeStateDiagnostic,
        leases: Dict[str, Dict[str, Any]],
        snapshot_generations: Dict[str, int],
    ) -> None:
        """Detect when a stored generation snapshot disagrees with current DB generation."""
        for terminal_id, snap_gen in snapshot_generations.items():
            lease = leases.get(terminal_id)
            if lease is None:
                diag.mismatches.append(RuntimeStateMismatch(
                    mismatch_type=GENERATION_SNAPSHOT_DRIFT,
                    severity="warning",
                    terminal_id=terminal_id,
                    snapshot_generation=snap_gen,
                    message=(
                        f"Snapshot recorded generation {snap_gen} for terminal "
                        f"{terminal_id} but that terminal has no lease row in the DB. "
                        f"The snapshot is stale or references a different state directory."
                    ),
                ))
                continue

            current_gen = lease.get("generation")
            if current_gen is None or current_gen != snap_gen:
                diag.mismatches.append(RuntimeStateMismatch(
                    mismatch_type=GENERATION_SNAPSHOT_DRIFT,
                    severity="warning",
                    terminal_id=terminal_id,
                    dispatch_id=lease.get("dispatch_id"),
                    lease_state=lease.get("state"),
                    generation=current_gen,
                    snapshot_generation=snap_gen,
                    message=(
                        f"Terminal {terminal_id} is at lease generation {current_gen} "
                        f"but a snapshot recorded generation {snap_gen}. The snapshot "
                        f"is stale — a lease cycle occurred since it was written. "
                        f"Any heartbeat or release using the snapshot generation will "
                        f"be rejected by the generation guard."
                    ),
                ))


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------

def load_reconciler(
    state_dir: str | Path,
    projection_file: Optional[str | Path] = None,
) -> RuntimeStateReconciler:
    """Return a RuntimeStateReconciler for state_dir."""
    return RuntimeStateReconciler(state_dir, projection_file=projection_file)
