#!/usr/bin/env python3
"""
VNX Runtime Core — PR-5 cutover coordinator.

Combines dispatch_broker + lease_manager + tmux_adapter into a unified
high-level interface for dispatcher integration.

Feature flags (PR-5 defaults — all enabled after cutover):
  VNX_RUNTIME_PRIMARY      "1" = runtime core active (default after PR-5)
                           "0" = legacy dispatcher path only (rollback)
  VNX_BROKER_SHADOW        "0" = broker is authoritative (PR-5 default)
                           "1" = shadow mode, broker registers but does not own delivery
  VNX_CANONICAL_LEASE_ACTIVE "1" = canonical lease manager active (PR-5 default)

Rollback:
  Set VNX_RUNTIME_PRIMARY=0 to revert to legacy transport without
  changing any other component. See docs/operations/RUNTIME_CORE_ROLLBACK.md.

Governance invariants preserved:
  - T0 completion authority: broker only tracks delivery -> accepted.
    The receipt processor + T0 review still determine 'completed'.
  - Receipts: dispatch_id flows through broker metadata and receipt
    markdown, ensuring receipt_processor linkage is unaffected.
  - tmux operator workflow: legacy paste-buffer fallback always available.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from dispatch_broker import BrokerError, DispatchBroker, load_broker
from lease_manager import LeaseManager
from runtime_coordination import DuplicateTransitionError, InvalidTransitionError, init_schema, get_connection, release_all_leases
from failure_classifier import classify_failure
from runtime_state_reconciler import ZOMBIE_LEASE, RuntimeStateReconciler
from tmux_adapter import TmuxAdapter, load_adapter

_RUNTIME_PRIMARY_FLAG = "VNX_RUNTIME_PRIMARY"
_DEFAULT_LEASE_TTL = 600


# ---------------------------------------------------------------------------
# Feature flag helpers
# ---------------------------------------------------------------------------

def runtime_primary_active() -> bool:
    """Return True when VNX_RUNTIME_PRIMARY=1 (default after PR-5 cutover)."""
    return os.environ.get(_RUNTIME_PRIMARY_FLAG, "1").strip() != "0"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RegisterResult:
    dispatch_id: str
    registered: bool
    already_existed: bool
    bundle_dir: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "registered": self.registered,
            "already_existed": self.already_existed,
            "bundle_dir": self.bundle_dir,
            "error": self.error,
        }


@dataclass
class DeliveryStartResult:
    dispatch_id: str
    terminal_id: str
    started: bool
    attempt_id: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "terminal_id": self.terminal_id,
            "started": self.started,
            "attempt_id": self.attempt_id,
            "error": self.error,
        }


@dataclass
class LeaseAcquireResult:
    terminal_id: str
    acquired: bool
    generation: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "terminal_id": self.terminal_id,
            "acquired": self.acquired,
            "generation": self.generation,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# RuntimeCore
# ---------------------------------------------------------------------------

class RuntimeCore:
    """High-level coordinator combining broker + lease_manager + adapter.

    Used by the dispatcher as the primary coordination path after PR-5
    cutover. All operations are non-fatal by design: the caller decides
    whether to treat failures as hard stops or continue with legacy paths.
    """

    def __init__(
        self,
        broker: DispatchBroker,
        lease_mgr: LeaseManager,
        adapter: Optional[TmuxAdapter] = None,
    ) -> None:
        self._broker = broker
        self._lease_mgr = lease_mgr
        self._adapter = adapter

    # ------------------------------------------------------------------
    # Dispatch registration (broker)
    # ------------------------------------------------------------------

    def register(
        self,
        dispatch_id: str,
        prompt: str,
        *,
        terminal_id: Optional[str] = None,
        track: Optional[str] = None,
        skill: Optional[str] = None,
        gate: Optional[str] = None,
        pr_ref: Optional[str] = None,
        priority: str = "P1",
        expected_outputs: Optional[List[str]] = None,
        intelligence_refs: Optional[List[str]] = None,
    ) -> RegisterResult:
        """Register dispatch with broker before delivery.

        Idempotent: re-registration of the same dispatch_id is safe.
        Bundle is immutable after first write (G-R6).
        """
        try:
            result = self._broker.register(
                dispatch_id,
                prompt,
                terminal_id=terminal_id,
                track=track,
                pr_ref=pr_ref,
                gate=gate,
                priority=priority,
                expected_outputs=expected_outputs,
                intelligence_refs=intelligence_refs,
                metadata={"skill": skill} if skill else None,
            )
            return RegisterResult(
                dispatch_id=dispatch_id,
                registered=True,
                already_existed=result.already_existed,
                bundle_dir=str(result.bundle_path),
            )
        except Exception as exc:
            return RegisterResult(
                dispatch_id=dispatch_id,
                registered=False,
                already_existed=False,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Delivery lifecycle (broker)
    # ------------------------------------------------------------------

    def delivery_start(
        self,
        dispatch_id: str,
        terminal_id: str,
        attempt_number: int = 1,
    ) -> DeliveryStartResult:
        """Claim dispatch and record delivery start.

        Transitions: queued -> claimed -> delivering.
        Returns attempt_id needed for delivery_success / delivery_failure.
        """
        try:
            claim = self._broker.claim(
                dispatch_id,
                terminal_id,
                attempt_number,
                actor="dispatcher",
            )
            self._broker.deliver_start(
                dispatch_id,
                claim.attempt_id,
                actor="dispatcher",
            )
            return DeliveryStartResult(
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                started=True,
                attempt_id=claim.attempt_id,
            )
        except (BrokerError, InvalidTransitionError) as exc:
            return DeliveryStartResult(
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                started=False,
                error=str(exc),
            )

    def delivery_success(
        self,
        dispatch_id: str,
        attempt_id: str,
    ) -> Dict[str, Any]:
        """Record successful delivery. Transitions: delivering -> accepted.

        Idempotent: duplicate acceptance for an already-accepted dispatch
        returns success=True with noop=True. Terminal-state dispatches
        return success=False with noop_rejected=True.
        """
        try:
            result = self._broker.deliver_success(dispatch_id, attempt_id, actor="dispatcher")
            if result.get("noop"):
                return {
                    "success": True,
                    "dispatch_id": dispatch_id,
                    "noop": True,
                    "current_state": result.get("current_state"),
                    "reason": result.get("reason"),
                }
            return {"success": True, "dispatch_id": dispatch_id, "noop": False}
        except DuplicateTransitionError as exc:
            return {
                "success": False,
                "dispatch_id": dispatch_id,
                "noop_rejected": True,
                "current_state": exc.current_state,
                "error": str(exc),
            }
        except Exception as exc:
            return {"success": False, "dispatch_id": dispatch_id, "error": str(exc)}

    def delivery_failure(
        self,
        dispatch_id: str,
        attempt_id: str,
        reason: str = "delivery failed",
    ) -> Dict[str, Any]:
        """Record delivery failure durably (G-R3, G-R5).

        Transitions: delivering -> failed_delivery.
        Failures are never logs-only after this call.
        """
        classification = classify_failure(reason)
        try:
            self._broker.deliver_failure(dispatch_id, attempt_id, reason, actor="dispatcher")
            return {
                "recorded": True,
                "dispatch_id": dispatch_id,
                "failure_class": classification.failure_class,
                "retryable": classification.retryable,
                "operator_summary": classification.operator_summary,
            }
        except Exception as exc:
            return {
                "recorded": False,
                "dispatch_id": dispatch_id,
                "error": str(exc),
                "failure_class": classification.failure_class,
                "retryable": classification.retryable,
                "operator_summary": classification.operator_summary,
            }

    # ------------------------------------------------------------------
    # Terminal lease management (lease_manager)
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_zombie_lease(state_dir, terminal_id: str, lease) -> Optional[Dict[str, Any]]:
        """Detect zombie lease and return result dict if found, else None."""
        reconciler = RuntimeStateReconciler(state_dir)
        mismatches = reconciler.reconcile_for_terminal(terminal_id)
        zombie = next((m for m in mismatches if m.mismatch_type == ZOMBIE_LEASE), None)
        if zombie is None:
            return None
        classification = classify_failure(
            f"runtime_state_divergence:zombie_lease:{zombie.dispatch_state}"
        )
        return {
            "available": False, "terminal_id": terminal_id,
            "reason": f"zombie_lease:{lease.dispatch_id}:dispatch_state={zombie.dispatch_state}",
            "lease_state": lease.state, "dispatch_state": zombie.dispatch_state,
            "mismatch": ZOMBIE_LEASE, "mismatch_message": zombie.message,
            "claimed_by": lease.dispatch_id,
            "failure_class": classification.failure_class,
            "retryable": classification.retryable,
            "operator_summary": classification.operator_summary,
        }

    def check_terminal(
        self,
        terminal_id: str,
        dispatch_id: str,
    ) -> Dict[str, Any]:
        """Check terminal availability via canonical lease state.

        Fail-closed: returns available=False on DB error or runtime uncertainty.
        Ambiguous state blocks rather than dispatches per the fail-closed contract.

        PR-2: also runs mismatch detection via RuntimeStateReconciler so that
        dispatch safety checks see the same reconciled truth as operator tooling.
        Zombie leases (lease held by a dispatch that has already ended) are
        reported explicitly with mismatch=zombie_lease rather than silently
        blocking future dispatches indefinitely.
        """
        try:
            lease = self._lease_mgr.get(terminal_id)
            if lease is None or lease.state == "idle":
                return {"available": True, "terminal_id": terminal_id, "reason": "idle"}
            if lease.dispatch_id == dispatch_id:
                return {"available": True, "terminal_id": terminal_id, "reason": "same_dispatch"}
            if self._lease_mgr.is_expired_by_ttl(terminal_id):
                return {
                    "available": False, "terminal_id": terminal_id,
                    "reason": f"lease_expired_not_cleaned:{lease.dispatch_id}",
                }

            zombie_result = self._detect_zombie_lease(self._lease_mgr.state_dir, terminal_id, lease)
            if zombie_result:
                return zombie_result

            return {
                "available": False, "terminal_id": terminal_id,
                "reason": f"leased:{lease.dispatch_id}", "claimed_by": lease.dispatch_id,
            }
        except Exception as exc:
            return {
                "available": False, "terminal_id": terminal_id,
                "reason": f"check_error_fail_closed:{exc}",
            }

    def acquire_lease(
        self,
        terminal_id: str,
        dispatch_id: str,
        lease_seconds: int = _DEFAULT_LEASE_TTL,
    ) -> LeaseAcquireResult:
        """Acquire canonical lease for terminal. idle -> leased."""
        try:
            result = self._lease_mgr.acquire(
                terminal_id,
                dispatch_id,
                lease_seconds=lease_seconds,
                actor="dispatcher",
            )
            return LeaseAcquireResult(
                terminal_id=terminal_id,
                acquired=True,
                generation=result.generation,
            )
        except InvalidTransitionError as exc:
            return LeaseAcquireResult(
                terminal_id=terminal_id,
                acquired=False,
                error=str(exc),
            )
        except Exception as exc:
            return LeaseAcquireResult(
                terminal_id=terminal_id,
                acquired=False,
                error=str(exc),
            )

    def release_lease(
        self,
        terminal_id: str,
        generation: int,
    ) -> Dict[str, Any]:
        """Release canonical lease. leased -> idle."""
        try:
            self._lease_mgr.release(terminal_id, generation, actor="dispatcher")
            return {"released": True, "terminal_id": terminal_id}
        except Exception as exc:
            return {"released": False, "terminal_id": terminal_id, "error": str(exc)}

    def release_on_receipt(
        self,
        terminal_id: str,
        dispatch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Release canonical lease on task receipt without requiring generation.

        Called by receipt_processor after task_complete/task_failed/task_timeout.
        Looks up the current generation from the DB so the caller does not need to
        thread it through the receipt pipeline.

        Ownership guard: when dispatch_id is provided, the lease must be owned by
        that dispatch — a mismatched owner is rejected rather than silently stolen.

        Idempotent: terminal already in idle state returns released=True.

        OI-1100 fix: when the lease has already been marked `expired` by the
        reconciler (worker outlived the TTL but eventually delivered a completion
        receipt), recover the lease so the next dispatch is not blocked by
        `lease_expired_not_cleaned`. Release_lease() rejects state="expired";
        recover() is the canonical transition expired → recovering → idle.

        Returns a structured dict with released/skipped/reason for audit logging.
        """
        try:
            lease = self._lease_mgr.get(terminal_id)
            if lease is None:
                return {
                    "released": False,
                    "terminal_id": terminal_id,
                    "reason": "terminal_not_found",
                }

            if lease.state == "idle":
                return {
                    "released": True,
                    "terminal_id": terminal_id,
                    "reason": "already_idle",
                    "skipped": True,
                }

            if dispatch_id and lease.dispatch_id and lease.dispatch_id != dispatch_id:
                return {
                    "released": False,
                    "terminal_id": terminal_id,
                    "reason": f"ownership_mismatch:lease_owned_by={lease.dispatch_id}",
                    "claimed_by": lease.dispatch_id,
                }

            generation = lease.generation

            # OI-1100: if the lease was already expired by the reconciler, the
            # standard release path is invalid (LEASE_TRANSITIONS forbids
            # expired -> released). Recover it instead — the canonical
            # expired -> recovering -> idle path resolves the same condition
            # and emits auditable lease_recovering / lease_recovered events.
            if lease.state == "expired":
                self._lease_mgr.recover(
                    terminal_id,
                    actor="receipt_processor",
                    reason=f"task_receipt_expired:{dispatch_id or 'unknown'}",
                )
                return {
                    "released": True,
                    "terminal_id": terminal_id,
                    "generation": generation,
                    "dispatch_id": dispatch_id,
                    "reason": "receipt_triggered_recover_from_expired",
                    "recovered": True,
                }

            self._lease_mgr.release(
                terminal_id,
                generation,
                actor="receipt_processor",
                reason=f"task_receipt:{dispatch_id or 'unknown'}",
            )
            return {
                "released": True,
                "terminal_id": terminal_id,
                "generation": generation,
                "dispatch_id": dispatch_id,
                "reason": "receipt_triggered_release",
            }
        except Exception as exc:
            return {
                "released": False,
                "terminal_id": terminal_id,
                "error": str(exc),
            }

    def release_on_delivery_failure(
        self,
        dispatch_id: str,
        attempt_id: str,
        terminal_id: str,
        generation: int,
        reason: str = "delivery failed",
    ) -> Dict[str, Any]:
        """Record delivery failure and release canonical lease in one auditable call.

        Guarantees the canonical lease is always released when delivery fails,
        even when the delivery-failure bookkeeping step itself partially fails.
        Returns explicit success/failure markers for both operations so the
        caller can emit a structured audit entry rather than relying on logs.

        Contract (PR-1):
          - failure_recorded=False does NOT prevent lease release.
          - lease_released=False is explicit, never silently ignored.
          - cleanup_complete is True only when both succeed.
        """
        failure_recorded = False
        lease_released = False
        failure_error: Optional[str] = None
        lease_error: Optional[str] = None

        # Step 1: Record delivery failure durably (broker state machine).
        # A failure here must NOT prevent the lease from being released.
        if attempt_id:
            try:
                self._broker.deliver_failure(dispatch_id, attempt_id, reason, actor="dispatcher")
                failure_recorded = True
            except Exception as exc:
                failure_error = str(exc)

        # Step 2: Release canonical lease — always attempted regardless of step 1.
        try:
            self._lease_mgr.release(
                terminal_id,
                generation,
                actor="dispatcher",
                reason=f"delivery_failure:{reason}",
            )
            lease_released = True
        except Exception as exc:
            lease_error = str(exc)

        classification = classify_failure(reason)

        return {
            "dispatch_id": dispatch_id,
            "terminal_id": terminal_id,
            "failure_recorded": failure_recorded,
            "lease_released": lease_released,
            "cleanup_complete": failure_recorded and lease_released,
            "failure_error": failure_error,
            "lease_error": lease_error,
            "failure_class": classification.failure_class,
            "retryable": classification.retryable,
            "operator_summary": classification.operator_summary,
        }

    # ------------------------------------------------------------------
    # Chain-boundary lease cleanup (BOOT-9 through BOOT-12)
    # ------------------------------------------------------------------

    def chain_closeout(self, force: bool = False) -> Dict[str, Any]:
        """Release all terminal leases at chain boundary.

        BOOT-9: Releases all non-idle leases to idle state.
        BOOT-10: Follows verify -> release -> audit -> confirm sequence.
        BOOT-11: Increments generation to guard against stale delayed releases.
        BOOT-12: This is an explicit operator action — not called automatically.

        Args:
            force: Proceed even when non-terminal dispatches exist.

        Returns a dict with released, already_idle, all_idle, and optional error.
        """
        try:
            with get_connection(self._lease_mgr.state_dir) as conn:
                result = release_all_leases(conn, force=force)
                conn.commit()
            return result
        except Exception as exc:
            return {
                "released": [],
                "already_idle": [],
                "non_terminal_dispatches": [],
                "blocked": False,
                "all_idle": False,
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # Compatibility check
    # ------------------------------------------------------------------

    @staticmethod
    def _check_db(state_path: Path) -> Dict[str, Any]:
        """Validate DB connectivity."""
        try:
            init_schema(state_path)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _check_broker(state_path: Path, dispatch_path: Path) -> Dict[str, Any]:
        """Validate broker availability."""
        try:
            broker = load_broker(state_path, dispatch_path)
            return {
                "ok": broker is not None,
                "shadow_mode": broker.shadow_mode if broker else None,
                "reason": "disabled (VNX_BROKER_ENABLED=0)" if broker is None else "ok",
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _check_lease_manager(state_path: Path) -> Dict[str, Any]:
        """Validate lease manager connectivity."""
        try:
            mgr = LeaseManager(state_path)
            leases = mgr.list_all()
            return {"ok": True, "lease_count": len(leases)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _check_adapter(state_path: Path) -> Dict[str, Any]:
        """Validate tmux adapter availability."""
        try:
            adapter = load_adapter(state_path)
            return {
                "ok": True,
                "primary_path": adapter.primary_path if adapter else None,
                "reason": "disabled (VNX_TMUX_ADAPTER_ENABLED=0)" if adapter is None else "ok",
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _check_receipt_linkage(state_path: Path) -> Dict[str, Any]:
        """Validate dispatch_id flows through receipts."""
        try:
            receipts_path = state_path / "t0_receipts.ndjson"
            if not receipts_path.exists():
                return {"ok": True, "reason": "receipts_file_not_found"}
            lines = [l.strip() for l in receipts_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            if not lines:
                return {"ok": True, "reason": "no_receipts_yet"}
            last = json.loads(lines[-1])
            has_dispatch_id = (
                "dispatch_id" in last or "dispatch-id" in last
                or "dispatch_id" in last.get("metadata", {})
            )
            return {"ok": True, "has_dispatch_id": has_dispatch_id, "receipt_count": len(lines)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @classmethod
    def check_compatibility(
        cls,
        state_dir: str | Path,
        dispatch_dir: str | Path,
    ) -> Dict[str, Any]:
        """Validate all runtime core components are functional.

        Returns a dict with 'compatible' (bool) and per-component results.
        Used by runtime_cutover_check.py before cutover promotion.
        """
        state_path = Path(state_dir)
        dispatch_path = Path(dispatch_dir)

        results = {
            "db": cls._check_db(state_path),
            "broker": cls._check_broker(state_path, dispatch_path),
            "lease_manager": cls._check_lease_manager(state_path),
            "adapter": cls._check_adapter(state_path),
            "receipt_linkage": cls._check_receipt_linkage(state_path),
            "t0_authority": {
                "ok": True,
                "note": (
                    "broker tracks delivery state (queued->accepted), "
                    "completion authority (->completed) remains with receipt-processor + T0"
                ),
            },
        }

        all_ok = all(v.get("ok", False) for v in results.values())
        return {
            "compatible": all_ok,
            "components": results,
            "flags": {
                "VNX_RUNTIME_PRIMARY": os.environ.get("VNX_RUNTIME_PRIMARY", "1"),
                "VNX_BROKER_SHADOW": os.environ.get("VNX_BROKER_SHADOW", "0"),
                "VNX_CANONICAL_LEASE_ACTIVE": os.environ.get("VNX_CANONICAL_LEASE_ACTIVE", "1"),
                "VNX_TMUX_ADAPTER_ENABLED": os.environ.get("VNX_TMUX_ADAPTER_ENABLED", "1"),
                "VNX_ADAPTER_PRIMARY": os.environ.get("VNX_ADAPTER_PRIMARY", "1"),
            },
        }


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------

def load_runtime_core(
    state_dir: str | Path,
    dispatch_dir: str | Path,
) -> Optional[RuntimeCore]:
    """Return a RuntimeCore if VNX_RUNTIME_PRIMARY=1 (default after PR-5).

    When VNX_RUNTIME_PRIMARY=0 (rollback mode), returns None so the caller
    falls through to the legacy dispatcher path unmodified.

    Side effect: if runtime_primary is active but VNX_BROKER_ENABLED/SHADOW
    are still set to shadow-mode defaults, this function overrides them to
    authoritative mode so the full cutover path is active.
    """
    if not runtime_primary_active():
        return None

    state_path = Path(state_dir)
    dispatch_path = Path(dispatch_dir)

    # Ensure broker runs in non-shadow (authoritative) mode after cutover
    if os.environ.get("VNX_BROKER_SHADOW", "0") == "1":
        os.environ["VNX_BROKER_SHADOW"] = "0"

    # Ensure canonical lease is active after cutover
    if os.environ.get("VNX_CANONICAL_LEASE_ACTIVE", "1") != "1":
        os.environ["VNX_CANONICAL_LEASE_ACTIVE"] = "1"

    try:
        broker = load_broker(state_path, dispatch_path)
        if broker is None:
            os.environ["VNX_BROKER_ENABLED"] = "1"
            os.environ["VNX_BROKER_SHADOW"] = "0"
            broker = load_broker(state_path, dispatch_path)

        lease_mgr = LeaseManager(state_path)
        adapter = load_adapter(state_path)

        return RuntimeCore(broker=broker, lease_mgr=lease_mgr, adapter=adapter)
    except Exception:
        return None
