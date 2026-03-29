#!/usr/bin/env python3
"""
VNX Runtime Reconciler — Canonical reconciliation for dispatch and lease state.

Detects and recovers:
  - Expired leases (TTL elapsed without heartbeat renewal)
  - Orphaned dispatch attempts (delivering without ACK, pending too long)
  - Unresolved dispatches (stuck in intermediate states past timeout)

Design constraints:
  - Never deletes dispatches or leases — only transitions to explicit states (G-R3)
  - Every recovery action appends a durable coordination event with timestamp and reason
  - Idempotent: repeated runs produce no duplicate state transitions (A-R8)
  - Reconciliation classifies stale state for operator review, reducing manual cleanup
  - Surfaces recovery summaries for later supervisor integration

Usage::

    reconciler = RuntimeReconciler(state_dir)
    result = reconciler.run()
    # result.expired_leases, result.recovered_leases, etc.
    print(result.summary())
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import (
    DISPATCH_STATES,
    DISPATCH_TRANSITIONS,
    InvalidTransitionError,
    get_connection,
    get_events,
    init_schema,
    transition_dispatch,
    update_attempt,
)
from lease_manager import LeaseManager, LeaseResult


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class ReconcilerConfig:
    """Tunable thresholds for reconciliation detection."""

    lease_ttl_grace_seconds: int = 0
    """Extra grace period beyond lease expires_at before marking expired."""

    attempt_stale_seconds: int = 300
    """Seconds after which a 'pending' or 'delivering' attempt is considered orphaned."""

    dispatch_stuck_seconds: int = 600
    """Seconds after which a dispatch in 'claimed' or 'delivering' without
    progress is considered stuck and eligible for timeout."""

    auto_recover_expired_leases: bool = True
    """When True, expired leases are automatically recovered to idle.
    When False, they are left in 'expired' state for operator review."""

    auto_recover_dispatches: bool = False
    """When True, timed-out dispatches are automatically transitioned to 'recovered'.
    When False, they are left in 'timed_out' for operator review."""

    max_dispatch_attempts: int = 3
    """Dispatches that have exceeded this attempt count and are in a
    recoverable state will be transitioned to 'expired' (terminal)."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ReconciliationAction:
    """A single reconciliation action taken or detected."""
    entity_type: str  # "lease", "dispatch", "attempt"
    entity_id: str
    action: str  # "expired", "recovered", "timed_out", "failed", "flagged"
    from_state: str
    to_state: str
    reason: str
    timestamp: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconciliationResult:
    """Complete result of a reconciliation run."""

    run_at: str
    dry_run: bool
    config: Dict[str, Any]

    # Actions taken
    expired_leases: List[ReconciliationAction] = field(default_factory=list)
    recovered_leases: List[ReconciliationAction] = field(default_factory=list)
    timed_out_dispatches: List[ReconciliationAction] = field(default_factory=list)
    recovered_dispatches: List[ReconciliationAction] = field(default_factory=list)
    expired_dispatches: List[ReconciliationAction] = field(default_factory=list)
    failed_attempts: List[ReconciliationAction] = field(default_factory=list)

    # Items flagged for operator review (not auto-resolved)
    needs_review: List[ReconciliationAction] = field(default_factory=list)

    # Errors encountered during reconciliation
    errors: List[str] = field(default_factory=list)

    @property
    def total_actions(self) -> int:
        return (
            len(self.expired_leases)
            + len(self.recovered_leases)
            + len(self.timed_out_dispatches)
            + len(self.recovered_dispatches)
            + len(self.expired_dispatches)
            + len(self.failed_attempts)
        )

    @property
    def is_clean(self) -> bool:
        """True when no actions were taken and nothing needs review."""
        return self.total_actions == 0 and len(self.needs_review) == 0

    def summary(self) -> str:
        """Human-readable summary of reconciliation results."""
        lines = [
            f"Reconciliation run at {self.run_at} ({'dry-run' if self.dry_run else 'live'})",
            f"  Expired leases:       {len(self.expired_leases)}",
            f"  Recovered leases:     {len(self.recovered_leases)}",
            f"  Timed-out dispatches: {len(self.timed_out_dispatches)}",
            f"  Recovered dispatches: {len(self.recovered_dispatches)}",
            f"  Expired dispatches:   {len(self.expired_dispatches)}",
            f"  Failed attempts:      {len(self.failed_attempts)}",
            f"  Needs operator review:{len(self.needs_review)}",
            f"  Errors:               {len(self.errors)}",
        ]

        if self.needs_review:
            lines.append("")
            lines.append("Items requiring operator review:")
            for item in self.needs_review:
                lines.append(f"  [{item.entity_type}] {item.entity_id}: {item.reason}")

        if self.errors:
            lines.append("")
            lines.append("Errors:")
            for err in self.errors:
                lines.append(f"  {err}")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON output."""
        def _actions(lst: List[ReconciliationAction]) -> List[Dict[str, Any]]:
            return [
                {
                    "entity_type": a.entity_type,
                    "entity_id": a.entity_id,
                    "action": a.action,
                    "from_state": a.from_state,
                    "to_state": a.to_state,
                    "reason": a.reason,
                    "timestamp": a.timestamp,
                    "metadata": a.metadata,
                }
                for a in lst
            ]

        return {
            "run_at": self.run_at,
            "dry_run": self.dry_run,
            "config": self.config,
            "total_actions": self.total_actions,
            "is_clean": self.is_clean,
            "expired_leases": _actions(self.expired_leases),
            "recovered_leases": _actions(self.recovered_leases),
            "timed_out_dispatches": _actions(self.timed_out_dispatches),
            "recovered_dispatches": _actions(self.recovered_dispatches),
            "expired_dispatches": _actions(self.expired_dispatches),
            "failed_attempts": _actions(self.failed_attempts),
            "needs_review": _actions(self.needs_review),
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _config_to_dict(cfg: ReconcilerConfig) -> Dict[str, Any]:
    return {
        "lease_ttl_grace_seconds": cfg.lease_ttl_grace_seconds,
        "attempt_stale_seconds": cfg.attempt_stale_seconds,
        "dispatch_stuck_seconds": cfg.dispatch_stuck_seconds,
        "auto_recover_expired_leases": cfg.auto_recover_expired_leases,
        "auto_recover_dispatches": cfg.auto_recover_dispatches,
        "max_dispatch_attempts": cfg.max_dispatch_attempts,
    }


# ---------------------------------------------------------------------------
# RuntimeReconciler
# ---------------------------------------------------------------------------

class RuntimeReconciler:
    """Canonical reconciliation engine for VNX runtime coordination state.

    Detects and transitions:
    1. Expired leases — leased terminals whose TTL has elapsed
    2. Orphaned attempts — attempts stuck in pending/delivering past threshold
    3. Unresolved dispatches — dispatches stuck in intermediate states
    4. Over-attempted dispatches — dispatches exceeding max attempt count

    All transitions are idempotent: running reconciliation twice produces
    no duplicate state changes because each detection query only matches
    entities in the specific pre-transition state.

    Args:
        state_dir: Path to .vnx-data/state/ directory containing
                   runtime_coordination.db.
        config:    Reconciliation thresholds and behavior toggles.
    """

    def __init__(
        self,
        state_dir: str | Path,
        config: Optional[ReconcilerConfig] = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._config = config or ReconcilerConfig()
        self._lease_mgr = LeaseManager(self._state_dir, auto_init=True)

    def run(self, *, dry_run: bool = False, now: Optional[datetime] = None) -> ReconciliationResult:
        """Execute a full reconciliation pass.

        Args:
            dry_run: When True, detect issues but do not modify state.
            now:     Override current time (for testing). Defaults to UTC now.

        Returns:
            ReconciliationResult with all actions taken and items needing review.
        """
        now_dt = (now or _now_utc()).astimezone(timezone.utc)
        result = ReconciliationResult(
            run_at=now_dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
            dry_run=dry_run,
            config=_config_to_dict(self._config),
        )

        self._reconcile_leases(result, dry_run=dry_run, now=now_dt)
        self._reconcile_attempts(result, dry_run=dry_run, now=now_dt)
        self._reconcile_dispatches(result, dry_run=dry_run, now=now_dt)

        return result

    # ------------------------------------------------------------------
    # Lease reconciliation
    # ------------------------------------------------------------------

    def _reconcile_leases(
        self,
        result: ReconciliationResult,
        *,
        dry_run: bool,
        now: datetime,
    ) -> None:
        """Detect and handle expired leases."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM terminal_leases WHERE state = 'leased'"
            ).fetchall()

        grace = timedelta(seconds=self._config.lease_ttl_grace_seconds)

        for row in rows:
            r = dict(row)
            terminal_id = r["terminal_id"]
            expires_at = _parse_iso(r.get("expires_at"))

            if expires_at is None:
                continue

            if expires_at + grace > now:
                continue  # Not yet expired

            ts = _now_iso()
            reason = (
                f"Lease TTL elapsed: expires_at={r['expires_at']}, "
                f"last_heartbeat_at={r.get('last_heartbeat_at')}, "
                f"dispatch_id={r.get('dispatch_id')}"
            )

            if not dry_run:
                try:
                    self._lease_mgr.expire(
                        terminal_id,
                        actor="reconciler",
                        reason=reason,
                    )
                except (InvalidTransitionError, KeyError) as exc:
                    result.errors.append(
                        f"Failed to expire lease {terminal_id}: {exc}"
                    )
                    continue

            action = ReconciliationAction(
                entity_type="lease",
                entity_id=terminal_id,
                action="expired",
                from_state="leased",
                to_state="expired",
                reason=reason,
                timestamp=ts,
                metadata={
                    "dispatch_id": r.get("dispatch_id"),
                    "generation": r.get("generation"),
                    "expires_at": r.get("expires_at"),
                    "last_heartbeat_at": r.get("last_heartbeat_at"),
                },
            )
            result.expired_leases.append(action)

        # Phase 2: auto-recover expired leases if configured
        if self._config.auto_recover_expired_leases:
            self._recover_expired_leases(result, dry_run=dry_run)

    def _recover_expired_leases(
        self,
        result: ReconciliationResult,
        *,
        dry_run: bool,
    ) -> None:
        """Recover expired leases back to idle."""
        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM terminal_leases WHERE state = 'expired'"
            ).fetchall()

        for row in rows:
            r = dict(row)
            terminal_id = r["terminal_id"]
            ts = _now_iso()
            reason = (
                f"Auto-recovered expired lease: "
                f"dispatch_id={r.get('dispatch_id')}, "
                f"generation={r.get('generation')}"
            )

            if not dry_run:
                try:
                    self._lease_mgr.recover(
                        terminal_id,
                        actor="reconciler",
                        reason=reason,
                    )
                except (InvalidTransitionError, KeyError) as exc:
                    result.errors.append(
                        f"Failed to recover lease {terminal_id}: {exc}"
                    )
                    continue

            action = ReconciliationAction(
                entity_type="lease",
                entity_id=terminal_id,
                action="recovered",
                from_state="expired",
                to_state="idle",
                reason=reason,
                timestamp=ts,
                metadata={
                    "dispatch_id": r.get("dispatch_id"),
                    "generation": r.get("generation"),
                },
            )
            result.recovered_leases.append(action)

    # ------------------------------------------------------------------
    # Attempt reconciliation
    # ------------------------------------------------------------------

    def _reconcile_attempts(
        self,
        result: ReconciliationResult,
        *,
        dry_run: bool,
        now: datetime,
    ) -> None:
        """Detect and handle orphaned dispatch attempts."""
        threshold = now - timedelta(seconds=self._config.attempt_stale_seconds)

        with get_connection(self._state_dir) as conn:
            rows = conn.execute(
                """
                SELECT * FROM dispatch_attempts
                WHERE state IN ('pending', 'delivering')
                  AND started_at < ?
                  AND ended_at IS NULL
                ORDER BY started_at ASC
                """,
                (threshold.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",),
            ).fetchall()

            for row in rows:
                r = dict(row)
                attempt_id = r["attempt_id"]
                from_state = r["state"]
                ts = _now_iso()
                reason = (
                    f"Orphaned attempt: state={from_state}, "
                    f"started_at={r['started_at']}, "
                    f"dispatch_id={r['dispatch_id']}, "
                    f"terminal_id={r['terminal_id']}"
                )

                if not dry_run:
                    try:
                        update_attempt(
                            conn,
                            attempt_id=attempt_id,
                            state="failed",
                            failure_reason=f"reconciler: orphaned {from_state} attempt past threshold",
                            actor="reconciler",
                        )
                    except KeyError as exc:
                        result.errors.append(
                            f"Failed to mark attempt {attempt_id} as failed: {exc}"
                        )
                        continue

                action = ReconciliationAction(
                    entity_type="attempt",
                    entity_id=attempt_id,
                    action="failed",
                    from_state=from_state,
                    to_state="failed",
                    reason=reason,
                    timestamp=ts,
                    metadata={
                        "dispatch_id": r["dispatch_id"],
                        "terminal_id": r["terminal_id"],
                        "attempt_number": r["attempt_number"],
                        "started_at": r["started_at"],
                    },
                )
                result.failed_attempts.append(action)

            if not dry_run:
                conn.commit()

    # ------------------------------------------------------------------
    # Dispatch reconciliation
    # ------------------------------------------------------------------

    def _reconcile_dispatches(
        self,
        result: ReconciliationResult,
        *,
        dry_run: bool,
        now: datetime,
    ) -> None:
        """Detect and handle unresolved dispatches."""
        threshold = now - timedelta(seconds=self._config.dispatch_stuck_seconds)
        threshold_iso = threshold.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

        with get_connection(self._state_dir) as conn:
            # Dispatches stuck in intermediate states
            stuck_rows = conn.execute(
                """
                SELECT * FROM dispatches
                WHERE state IN ('claimed', 'delivering', 'accepted', 'running')
                  AND updated_at < ?
                ORDER BY updated_at ASC
                """,
                (threshold_iso,),
            ).fetchall()

            for row in stuck_rows:
                r = dict(row)
                dispatch_id = r["dispatch_id"]
                from_state = r["state"]
                ts = _now_iso()

                # Determine the correct target state based on valid transitions.
                # - claimed -> expired (claim expired without delivery starting)
                # - delivering/accepted/running -> timed_out
                allowed = DISPATCH_TRANSITIONS.get(from_state, frozenset())
                if "timed_out" in allowed:
                    target_state = "timed_out"
                elif "expired" in allowed:
                    target_state = "expired"
                else:
                    # No valid stuck-recovery transition — flag for review
                    result.needs_review.append(ReconciliationAction(
                        entity_type="dispatch",
                        entity_id=dispatch_id,
                        action="flagged",
                        from_state=from_state,
                        to_state=from_state,
                        reason=(
                            f"Dispatch stuck in '{from_state}' past threshold "
                            f"but no valid recovery transition available. "
                            f"Updated at: {r['updated_at']}"
                        ),
                        timestamp=ts,
                        metadata={"terminal_id": r.get("terminal_id"), "updated_at": r["updated_at"]},
                    ))
                    continue

                reason = (
                    f"Dispatch stuck in '{from_state}': "
                    f"updated_at={r['updated_at']}, "
                    f"terminal_id={r.get('terminal_id')}"
                )

                if not dry_run:
                    try:
                        transition_dispatch(
                            conn,
                            dispatch_id=dispatch_id,
                            to_state=target_state,
                            actor="reconciler",
                            reason=reason,
                            metadata={
                                "reconciler_action": "timeout_stuck_dispatch",
                                "original_state": from_state,
                            },
                        )
                    except (InvalidTransitionError, KeyError) as exc:
                        result.errors.append(
                            f"Failed to transition dispatch {dispatch_id}: {exc}"
                        )
                        continue

                action_label = "timed_out" if target_state == "timed_out" else "expired"
                action = ReconciliationAction(
                    entity_type="dispatch",
                    entity_id=dispatch_id,
                    action=action_label,
                    from_state=from_state,
                    to_state=target_state,
                    reason=reason,
                    timestamp=ts,
                    metadata={
                        "terminal_id": r.get("terminal_id"),
                        "attempt_count": r.get("attempt_count"),
                        "updated_at": r["updated_at"],
                    },
                )
                if target_state == "timed_out":
                    result.timed_out_dispatches.append(action)
                else:
                    result.expired_dispatches.append(action)

            # Dispatches in recoverable states (timed_out, failed_delivery)
            recoverable_rows = conn.execute(
                """
                SELECT * FROM dispatches
                WHERE state IN ('timed_out', 'failed_delivery')
                ORDER BY updated_at ASC
                """
            ).fetchall()

            for row in recoverable_rows:
                r = dict(row)
                dispatch_id = r["dispatch_id"]
                from_state = r["state"]
                attempt_count = r.get("attempt_count", 0)
                ts = _now_iso()

                # Exceeded max attempts → expire (terminal state)
                if attempt_count >= self._config.max_dispatch_attempts:
                    reason = (
                        f"Dispatch exceeded max attempts ({attempt_count}/{self._config.max_dispatch_attempts}): "
                        f"state={from_state}, terminal_id={r.get('terminal_id')}"
                    )

                    if not dry_run:
                        try:
                            transition_dispatch(
                                conn,
                                dispatch_id=dispatch_id,
                                to_state="expired",
                                actor="reconciler",
                                reason=reason,
                                metadata={
                                    "reconciler_action": "expire_over_attempted",
                                    "attempt_count": attempt_count,
                                    "max_attempts": self._config.max_dispatch_attempts,
                                },
                            )
                        except (InvalidTransitionError, KeyError) as exc:
                            result.errors.append(
                                f"Failed to expire dispatch {dispatch_id}: {exc}"
                            )
                            continue

                    action = ReconciliationAction(
                        entity_type="dispatch",
                        entity_id=dispatch_id,
                        action="expired",
                        from_state=from_state,
                        to_state="expired",
                        reason=reason,
                        timestamp=ts,
                        metadata={
                            "terminal_id": r.get("terminal_id"),
                            "attempt_count": attempt_count,
                        },
                    )
                    result.expired_dispatches.append(action)

                elif self._config.auto_recover_dispatches:
                    reason = (
                        f"Auto-recovered dispatch from '{from_state}': "
                        f"attempts={attempt_count}/{self._config.max_dispatch_attempts}, "
                        f"terminal_id={r.get('terminal_id')}"
                    )

                    if not dry_run:
                        try:
                            transition_dispatch(
                                conn,
                                dispatch_id=dispatch_id,
                                to_state="recovered",
                                actor="reconciler",
                                reason=reason,
                                metadata={
                                    "reconciler_action": "auto_recover",
                                    "attempt_count": attempt_count,
                                },
                            )
                        except (InvalidTransitionError, KeyError) as exc:
                            result.errors.append(
                                f"Failed to recover dispatch {dispatch_id}: {exc}"
                            )
                            continue

                    action = ReconciliationAction(
                        entity_type="dispatch",
                        entity_id=dispatch_id,
                        action="recovered",
                        from_state=from_state,
                        to_state="recovered",
                        reason=reason,
                        timestamp=ts,
                        metadata={
                            "terminal_id": r.get("terminal_id"),
                            "attempt_count": attempt_count,
                        },
                    )
                    result.recovered_dispatches.append(action)

                else:
                    # Flag for operator review
                    result.needs_review.append(ReconciliationAction(
                        entity_type="dispatch",
                        entity_id=dispatch_id,
                        action="flagged",
                        from_state=from_state,
                        to_state=from_state,
                        reason=(
                            f"Dispatch in '{from_state}' with {attempt_count} attempts — "
                            f"requires operator review (auto_recover_dispatches=False)"
                        ),
                        timestamp=ts,
                        metadata={
                            "terminal_id": r.get("terminal_id"),
                            "attempt_count": attempt_count,
                        },
                    ))

            if not dry_run:
                conn.commit()


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------

def load_reconciler(
    state_dir: str | Path,
    config: Optional[ReconcilerConfig] = None,
) -> RuntimeReconciler:
    """Return a RuntimeReconciler for the given state directory."""
    return RuntimeReconciler(state_dir, config=config)
