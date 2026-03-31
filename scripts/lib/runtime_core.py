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
  changing any other component. See docs/runtime_core_rollback.md.

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
from runtime_coordination import DuplicateTransitionError, InvalidTransitionError, init_schema
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
        try:
            self._broker.deliver_failure(dispatch_id, attempt_id, reason, actor="dispatcher")
            return {"recorded": True, "dispatch_id": dispatch_id}
        except Exception as exc:
            return {"recorded": False, "dispatch_id": dispatch_id, "error": str(exc)}

    # ------------------------------------------------------------------
    # Terminal lease management (lease_manager)
    # ------------------------------------------------------------------

    def check_terminal(
        self,
        terminal_id: str,
        dispatch_id: str,
    ) -> Dict[str, Any]:
        """Check terminal availability via canonical lease state.

        Non-fatal: returns available=True with reason on DB error so
        the dispatcher can fall through to legacy lock check.
        """
        try:
            lease = self._lease_mgr.get(terminal_id)
            if lease is None or lease.state == "idle":
                return {"available": True, "terminal_id": terminal_id, "reason": "idle"}
            if lease.dispatch_id == dispatch_id:
                return {"available": True, "terminal_id": terminal_id, "reason": "same_dispatch"}
            if self._lease_mgr.is_expired_by_ttl(terminal_id):
                return {
                    "available": False,
                    "terminal_id": terminal_id,
                    "reason": f"lease_expired_not_cleaned:{lease.dispatch_id}",
                }
            return {
                "available": False,
                "terminal_id": terminal_id,
                "reason": f"leased:{lease.dispatch_id}",
                "claimed_by": lease.dispatch_id,
            }
        except Exception as exc:
            # Non-fatal: DB unavailable — fall through to available so legacy path decides
            return {
                "available": True,
                "terminal_id": terminal_id,
                "reason": f"check_error_fallback:{exc}",
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

    # ------------------------------------------------------------------
    # Compatibility check
    # ------------------------------------------------------------------

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
        results: Dict[str, Dict[str, Any]] = {}
        state_path = Path(state_dir)
        dispatch_path = Path(dispatch_dir)

        # DB connectivity
        try:
            init_schema(state_path)
            results["db"] = {"ok": True}
        except Exception as exc:
            results["db"] = {"ok": False, "error": str(exc)}

        # Broker
        try:
            broker = load_broker(state_path, dispatch_path)
            results["broker"] = {
                "ok": broker is not None,
                "shadow_mode": broker.shadow_mode if broker else None,
                "reason": "disabled (VNX_BROKER_ENABLED=0)" if broker is None else "ok",
            }
        except Exception as exc:
            results["broker"] = {"ok": False, "error": str(exc)}

        # Lease manager
        try:
            mgr = LeaseManager(state_path)
            leases = mgr.list_all()
            results["lease_manager"] = {"ok": True, "lease_count": len(leases)}
        except Exception as exc:
            results["lease_manager"] = {"ok": False, "error": str(exc)}

        # Adapter
        try:
            adapter = load_adapter(state_path)
            results["adapter"] = {
                "ok": True,
                "primary_path": adapter.primary_path if adapter else None,
                "reason": "disabled (VNX_TMUX_ADAPTER_ENABLED=0)" if adapter is None else "ok",
            }
        except Exception as exc:
            results["adapter"] = {"ok": False, "error": str(exc)}

        # Receipt linkage: verify dispatch_id flows through receipts
        try:
            receipts_path = state_path / "t0_receipts.ndjson"
            if receipts_path.exists():
                lines = [
                    line.strip()
                    for line in receipts_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if lines:
                    last = json.loads(lines[-1])
                    has_dispatch_id = (
                        "dispatch_id" in last
                        or "dispatch-id" in last
                        or "dispatch_id" in last.get("metadata", {})
                    )
                    results["receipt_linkage"] = {
                        "ok": True,
                        "has_dispatch_id": has_dispatch_id,
                        "receipt_count": len(lines),
                    }
                else:
                    results["receipt_linkage"] = {"ok": True, "reason": "no_receipts_yet"}
            else:
                results["receipt_linkage"] = {"ok": True, "reason": "receipts_file_not_found"}
        except Exception as exc:
            results["receipt_linkage"] = {"ok": False, "error": str(exc)}

        # Governance: T0 completion authority preserved
        # The broker advances state to 'accepted' on delivery success,
        # but 'completed' requires a receipt + T0 review. This is by design.
        results["t0_authority"] = {
            "ok": True,
            "note": (
                "broker tracks delivery state (queued→accepted), "
                "completion authority (→completed) remains with receipt-processor + T0"
            ),
        }

        all_ok = all(v.get("ok", False) for v in results.values())
        return {
            "compatible": all_ok,
            "components": results,
            "flags": {
                "VNX_RUNTIME_PRIMARY": os.environ.get("VNX_RUNTIME_PRIMARY", "1"),
                "VNX_BROKER_SHADOW": os.environ.get("VNX_BROKER_SHADOW", "0"),
                "VNX_CANONICAL_LEASE_ACTIVE": os.environ.get(
                    "VNX_CANONICAL_LEASE_ACTIVE", "1"
                ),
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
