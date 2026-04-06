#!/usr/bin/env python3
"""
VNX Dispatch Broker — Durable dispatch registration and bundle management.

Shadow mode semantics
---------------------
The broker runs in two modes controlled by environment variables:

  VNX_BROKER_ENABLED (default "1")
    When "0", the broker is entirely disabled. load_broker() returns None.
    The existing dispatcher_v8_minimal.sh path operates unmodified.

  VNX_BROKER_SHADOW (default "1")
    When "1" (shadow mode ON), the broker registers dispatches and writes
    bundles durably but does NOT replace tmux delivery. The existing
    send-keys/paste-buffer path is still the active transport.
    When "0" (shadow mode OFF), the broker path is authoritative. This
    is used by PR-3 and later after shadow validation.

Immutability (G-R6)
-------------------
Dispatch bundles are immutable after initial write. If register() is called
with an existing dispatch_id that already has a valid bundle.json on disk,
the existing bundle is returned unchanged. The DB row is also not modified
(register_dispatch in runtime_coordination.py is idempotent).

Failure durability
------------------
Every delivery failure creates a failed_delivery attempt record in the
coordination database. Failures never disappear into logs alone.

Idempotency
-----------
Calling register() with the same dispatch_id multiple times is safe.
The second call returns already_existed=True and the original data.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import (
    ACCEPTED_OR_BEYOND_STATES,
    TERMINAL_DISPATCH_STATES,
    DuplicateTransitionError,
    InvalidStateError,
    InvalidTransitionError,
    create_attempt,
    get_connection,
    get_dispatch,
    increment_attempt_count,
    init_schema,
    is_accepted_or_beyond,
    is_terminal_dispatch_state,
    register_dispatch,
    transition_dispatch,
    transition_dispatch_idempotent,
    update_attempt,
)

try:
    from intelligence_selector import (
        IntelligenceSelector,
        InjectionResult,
        select_intelligence,
    )
    _SELECTOR_AVAILABLE = True
except ImportError:
    _SELECTOR_AVAILABLE = False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BrokerError(Exception):
    """Raised for broker-specific errors distinct from coordination errors."""


class BrokerDisabledError(BrokerError):
    """Raised when the broker is accessed but VNX_BROKER_ENABLED=0."""


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RegisterResult:
    dispatch_row: Dict[str, Any]
    bundle_path: Path
    already_existed: bool


@dataclass
class ClaimResult:
    dispatch_row: Dict[str, Any]
    attempt_id: str
    attempt_number: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _write_atomic(path: Path, content: str) -> None:
    """Write content to path atomically via a .tmp sibling then rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


def _read_bundle_json(bundle_json_path: Path) -> Optional[Dict[str, Any]]:
    """Return parsed bundle.json or None if missing or corrupt."""
    if not bundle_json_path.exists():
        return None
    try:
        return json.loads(bundle_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# DispatchBroker
# ---------------------------------------------------------------------------

class DispatchBroker:
    """Durable dispatch broker for VNX runtime coordination.

    Responsibilities:
    - Register dispatches durably in SQLite before any terminal delivery.
    - Write immutable dispatch bundles (bundle.json + prompt.txt) to the
      filesystem under {dispatch_dir}/{dispatch_id}/.
    - Record every attempt lifecycle event (claim, deliver_start,
      deliver_success, deliver_failure) in the coordination database.
    - In shadow mode, operate alongside the existing tmux transport
      without replacing it.

    Args:
        state_dir:   Directory containing runtime_coordination.db.
        dispatch_dir: Root directory for dispatch bundles
                      (e.g. .vnx-data/dispatches/).
        shadow_mode: When True, broker registers but does not replace
                     the existing tmux delivery transport.
    """

    def __init__(
        self,
        state_dir: str | Path,
        dispatch_dir: str | Path,
        *,
        shadow_mode: bool = True,
        quality_db_path: str | Path | None = None,
        intelligence_enabled: bool = True,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._dispatch_dir = Path(dispatch_dir)
        self._shadow_mode = shadow_mode
        self._quality_db_path = Path(quality_db_path) if quality_db_path else None
        self._intelligence_enabled = intelligence_enabled and _SELECTOR_AVAILABLE

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    @property
    def enabled(self) -> bool:
        """Always True when directly instantiated. Use load_broker() for env-based toggling."""
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(
        self,
        dispatch_id: str,
        prompt: str,
        *,
        terminal_id: Optional[str] = None,
        track: Optional[str] = None,
        pr_ref: Optional[str] = None,
        gate: Optional[str] = None,
        priority: str = "P2",
        expected_outputs: Optional[List[Any]] = None,
        intelligence_refs: Optional[List[Any]] = None,
        target_profile: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        task_class: Optional[str] = None,
        skill_name: Optional[str] = None,
    ) -> RegisterResult:
        """Register a dispatch durably in the DB and write its bundle to disk.

        Idempotent: if dispatch_id already has a valid bundle.json, returns
        the existing data with already_existed=True. The DB row is also
        preserved unchanged (G-R6: immutability after send).

        Args:
            dispatch_id:       Unique dispatch identifier.
            prompt:            Raw prompt payload for the worker.
            terminal_id:       Target terminal (e.g. "T2") or None.
            track:             Worker track label (e.g. "B") or None.
            pr_ref:            PR reference (e.g. "PR-1") or None.
            gate:              Quality gate identifier or None.
            priority:          Dispatch priority ("P1", "P2", etc.).
            expected_outputs:  List of expected output filenames/specs.
            intelligence_refs: List of intelligence document references.
            target_profile:    Routing profile for the target terminal.
            metadata:          Arbitrary extra metadata dict.

        Returns:
            RegisterResult with the dispatch DB row, bundle directory path,
            and a flag indicating whether the bundle already existed on disk.
        """
        bundle_dir = self.get_bundle_path(dispatch_id)
        bundle_json_path = bundle_dir / "bundle.json"

        # Check for existing bundle (immutability guard, G-R6)
        existing_bundle = _read_bundle_json(bundle_json_path)
        if existing_bundle is not None:
            with get_connection(self._state_dir) as conn:
                row = get_dispatch(conn, dispatch_id)
            if row is None:
                # Bundle exists but DB row is missing — re-register DB only.
                # This can happen if the DB was reset while bundles survived.
                row = self._register_db_row(
                    dispatch_id=dispatch_id,
                    terminal_id=terminal_id,
                    track=track,
                    pr_ref=pr_ref,
                    gate=gate,
                    priority=priority,
                    bundle_dir=bundle_dir,
                    metadata=metadata,
                )
            return RegisterResult(
                dispatch_row=row,
                bundle_path=bundle_dir,
                already_existed=True,
            )

        # Write bundle directory and files atomically
        bundle_dir.mkdir(parents=True, exist_ok=True)

        # Run bounded intelligence selection (FP-C PR-3)
        intelligence_payload = None
        if self._intelligence_enabled:
            try:
                selector = IntelligenceSelector(
                    quality_db_path=self._quality_db_path,
                    coord_db_state_dir=self._state_dir,
                )
                injection_result = selector.select(
                    dispatch_id=dispatch_id,
                    injection_point="dispatch_create",
                    task_class=task_class,
                    skill_name=skill_name,
                    scope_tags=None,
                    track=track,
                    gate=gate,
                )
                selector.emit_event(injection_result)
                selector.record_injection(injection_result)
                intelligence_payload = injection_result.to_payload_dict()
                selector.close()
            except Exception:
                intelligence_payload = None

        bundle_data: Dict[str, Any] = {
            "dispatch_id": dispatch_id,
            "bundle_version": 1,
            "created_at": _now_iso(),
            "terminal_id": terminal_id,
            "track": track,
            "pr_ref": pr_ref,
            "gate": gate,
            "priority": priority,
            "expected_outputs": expected_outputs or [],
            "intelligence_refs": intelligence_refs or [],
            "target_profile": target_profile or {},
            "metadata": metadata or {},
        }
        if intelligence_payload is not None:
            bundle_data["intelligence_payload"] = intelligence_payload

        prompt_path = bundle_dir / "prompt.txt"
        _write_atomic(prompt_path, prompt)
        _write_atomic(bundle_json_path, json.dumps(bundle_data, indent=2))

        # Register in DB (idempotent — returns existing row if present)
        row = self._register_db_row(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            track=track,
            pr_ref=pr_ref,
            gate=gate,
            priority=priority,
            bundle_dir=bundle_dir,
            metadata=metadata,
        )

        return RegisterResult(
            dispatch_row=row,
            bundle_path=bundle_dir,
            already_existed=False,
        )

    def claim(
        self,
        dispatch_id: str,
        terminal_id: str,
        attempt_number: int = 1,
        *,
        actor: str = "broker",
    ) -> ClaimResult:
        """Claim a dispatch for delivery to terminal_id.

        Transitions the dispatch from queued -> claimed and creates a
        dispatch_attempt record. Increments the attempt counter.

        Args:
            dispatch_id:    Dispatch to claim.
            terminal_id:    Terminal that will execute the dispatch.
            attempt_number: Attempt sequence number (1-based).
            actor:          Actor label recorded in coordination events.

        Returns:
            ClaimResult with the updated dispatch row, attempt_id, and
            attempt_number.

        Raises:
            BrokerError: If the dispatch does not exist or is not in queued state.
            InvalidTransitionError: If the state transition is not permitted.
        """
        with get_connection(self._state_dir) as conn:
            row = get_dispatch(conn, dispatch_id)
            if row is None:
                raise BrokerError(f"Cannot claim: dispatch not found: {dispatch_id!r}")

            current_state = row["state"]
            if current_state != "queued":
                raise BrokerError(
                    f"Cannot claim dispatch {dispatch_id!r}: "
                    f"expected state 'queued', found {current_state!r}"
                )

            updated_row = transition_dispatch(
                conn,
                dispatch_id=dispatch_id,
                to_state="claimed",
                actor=actor,
                reason=f"claimed by {terminal_id} attempt {attempt_number}",
                metadata={"terminal_id": terminal_id, "attempt_number": attempt_number},
            )
            increment_attempt_count(conn, dispatch_id)

            attempt_row = create_attempt(
                conn,
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                attempt_number=attempt_number,
                metadata={"actor": actor},
                actor=actor,
            )
            conn.commit()

        return ClaimResult(
            dispatch_row=updated_row,
            attempt_id=attempt_row["attempt_id"],
            attempt_number=attempt_number,
        )

    def deliver_start(
        self,
        dispatch_id: str,
        attempt_id: str,
        *,
        actor: str = "broker",
    ) -> None:
        """Record that delivery of dispatch_id has begun.

        Transitions: claimed -> delivering.
        Updates the attempt state to 'delivering'.

        Args:
            dispatch_id: Dispatch being delivered.
            attempt_id:  Attempt ID returned from claim().
            actor:       Actor label recorded in coordination events.

        Raises:
            BrokerError: If the dispatch does not exist.
            InvalidTransitionError: If the transition is not permitted.
        """
        with get_connection(self._state_dir) as conn:
            row = get_dispatch(conn, dispatch_id)
            if row is None:
                raise BrokerError(f"deliver_start: dispatch not found: {dispatch_id!r}")

            transition_dispatch(
                conn,
                dispatch_id=dispatch_id,
                to_state="delivering",
                actor=actor,
                reason="delivery started",
                metadata={"attempt_id": attempt_id},
            )
            update_attempt(
                conn,
                attempt_id=attempt_id,
                state="delivering",
                actor=actor,
            )
            conn.commit()

    def deliver_success(
        self,
        dispatch_id: str,
        attempt_id: str,
        *,
        actor: str = "broker",
    ) -> Dict[str, Any]:
        """Record that delivery succeeded and the terminal ACKed receipt.

        Transitions: delivering -> accepted.
        Updates the attempt state to 'succeeded'.

        Idempotent: if the dispatch is already in 'accepted' or has
        progressed beyond it (running, completed, etc.), this returns a
        no-op result instead of raising. Terminal states (completed,
        expired, dead_letter) are explicitly rejected with a
        DuplicateTransitionError.

        Args:
            dispatch_id: Dispatch that was delivered.
            attempt_id:  Attempt ID returned from claim().
            actor:       Actor label recorded in coordination events.

        Returns:
            Dict with 'transitioned' (bool) and 'noop' (bool) keys.
            When noop=True, 'current_state' and 'reason' explain why.

        Raises:
            BrokerError: If the dispatch does not exist.
            DuplicateTransitionError: If the dispatch is in a terminal state.
        """
        with get_connection(self._state_dir) as conn:
            row = get_dispatch(conn, dispatch_id)
            if row is None:
                raise BrokerError(f"deliver_success: dispatch not found: {dispatch_id!r}")

            current_state = row["state"]

            # Idempotent: already accepted or beyond — safe no-op
            if is_accepted_or_beyond(current_state):
                reason = (
                    f"duplicate acceptance no-op: dispatch already in {current_state!r}"
                    if current_state == "accepted"
                    else f"acceptance no-op: dispatch already progressed to {current_state!r}"
                )

                # Terminal states get rejected, not silently swallowed
                if is_terminal_dispatch_state(current_state):
                    from runtime_coordination import _append_event, _now_utc
                    _append_event(
                        conn,
                        event_type="dispatch_acceptance_rejected",
                        entity_type="dispatch",
                        entity_id=dispatch_id,
                        from_state=current_state,
                        to_state="accepted",
                        actor=actor,
                        reason=f"rejected: dispatch in terminal state {current_state!r}",
                        metadata={"attempt_id": attempt_id},
                    )
                    conn.commit()
                    raise DuplicateTransitionError(
                        f"Dispatch {dispatch_id!r} is in terminal state {current_state!r}; "
                        f"duplicate acceptance rejected",
                        dispatch_id=dispatch_id,
                        current_state=current_state,
                        requested_state="accepted",
                    )

                # Non-terminal but already accepted/running — auditable no-op
                from runtime_coordination import _append_event
                _append_event(
                    conn,
                    event_type="dispatch_noop",
                    entity_type="dispatch",
                    entity_id=dispatch_id,
                    from_state=current_state,
                    to_state="accepted",
                    actor=actor,
                    reason=reason,
                    metadata={"attempt_id": attempt_id},
                )
                conn.commit()
                return {
                    "transitioned": False,
                    "noop": True,
                    "current_state": current_state,
                    "reason": reason,
                }

            # Normal forward transition: delivering -> accepted
            transition_dispatch(
                conn,
                dispatch_id=dispatch_id,
                to_state="accepted",
                actor=actor,
                reason="delivery succeeded",
                metadata={"attempt_id": attempt_id},
            )
            update_attempt(
                conn,
                attempt_id=attempt_id,
                state="succeeded",
                actor=actor,
            )
            conn.commit()
            return {"transitioned": True, "noop": False}

    def deliver_failure(
        self,
        dispatch_id: str,
        attempt_id: str,
        reason: str,
        *,
        actor: str = "broker",
    ) -> None:
        """Record a delivery failure durably (G-R3, G-R5).

        Transitions: delivering -> failed_delivery.
        Updates the attempt state to 'failed' with the failure reason.

        Failures are always recorded in the coordination database so they
        never disappear into logs alone.

        Args:
            dispatch_id: Dispatch whose delivery failed.
            attempt_id:  Attempt ID returned from claim().
            reason:      Human-readable description of the failure.
            actor:       Actor label recorded in coordination events.

        Raises:
            BrokerError: If the dispatch does not exist.
            InvalidTransitionError: If the transition is not permitted.
        """
        with get_connection(self._state_dir) as conn:
            row = get_dispatch(conn, dispatch_id)
            if row is None:
                raise BrokerError(f"deliver_failure: dispatch not found: {dispatch_id!r}")

            transition_dispatch(
                conn,
                dispatch_id=dispatch_id,
                to_state="failed_delivery",
                actor=actor,
                reason=reason,
                metadata={"attempt_id": attempt_id, "failure_reason": reason},
            )
            update_attempt(
                conn,
                attempt_id=attempt_id,
                state="failed",
                failure_reason=reason,
                actor=actor,
            )
            conn.commit()

    def inject_intelligence_on_resume(
        self,
        dispatch_id: str,
        *,
        task_class: Optional[str] = None,
        skill_name: Optional[str] = None,
        track: Optional[str] = None,
        gate: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run bounded intelligence selection for a resumed dispatch.

        Called when a recovered dispatch is re-delivered. Returns the
        intelligence_payload dict or None if selection is disabled or
        no items meet thresholds.

        Does NOT modify the immutable bundle on disk. The caller is
        responsible for including the payload in the re-delivery context.
        """
        if not self._intelligence_enabled:
            return None

        try:
            selector = IntelligenceSelector(
                quality_db_path=self._quality_db_path,
                coord_db_state_dir=self._state_dir,
            )
            result = selector.select(
                dispatch_id=dispatch_id,
                injection_point="dispatch_resume",
                task_class=task_class,
                skill_name=skill_name,
                track=track,
                gate=gate,
            )
            selector.emit_event(result)
            selector.record_injection(result)
            payload = result.to_payload_dict()
            selector.close()
            return payload if result.items_injected > 0 else None
        except Exception:
            return None

    def get_bundle(self, dispatch_id: str) -> Optional[Dict[str, Any]]:
        """Return parsed bundle.json for dispatch_id, or None if not found.

        Args:
            dispatch_id: Dispatch whose bundle to retrieve.

        Returns:
            Parsed bundle dict or None if bundle.json does not exist or is
            not valid JSON.
        """
        bundle_json_path = self.get_bundle_path(dispatch_id) / "bundle.json"
        return _read_bundle_json(bundle_json_path)

    def get_bundle_path(self, dispatch_id: str) -> Path:
        """Return the bundle directory path for dispatch_id.

        The directory may or may not exist. Use register() to create it.

        Args:
            dispatch_id: Dispatch identifier.

        Returns:
            Path to the bundle directory: {dispatch_dir}/{dispatch_id}/
        """
        return self._dispatch_dir / dispatch_id

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _register_db_row(
        self,
        *,
        dispatch_id: str,
        terminal_id: Optional[str],
        track: Optional[str],
        pr_ref: Optional[str],
        gate: Optional[str],
        priority: str,
        bundle_dir: Path,
        metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Register dispatch in DB (idempotent). Returns the dispatch row dict."""
        with get_connection(self._state_dir) as conn:
            row = register_dispatch(
                conn,
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                track=track,
                priority=priority,
                pr_ref=pr_ref,
                gate=gate,
                bundle_path=str(bundle_dir),
                metadata=metadata,
                actor="broker",
            )
            conn.commit()
        return row


# ---------------------------------------------------------------------------
# Module-level factory functions
# ---------------------------------------------------------------------------

def load_broker(
    state_dir: str | Path,
    dispatch_dir: str | Path,
    *,
    quality_db_path: str | Path | None = None,
) -> Optional[DispatchBroker]:
    """Return a DispatchBroker if VNX_BROKER_ENABLED=1 (default), else None.

    Shadow mode is determined by VNX_BROKER_SHADOW (default "1" = shadow ON).
    Intelligence injection is determined by VNX_INTELLIGENCE_INJECTION
    (default "1" = enabled).

    This function is the standard entry point for production code that
    needs to optionally use the broker without hard-failing when disabled.

    Args:
        state_dir:       Directory containing runtime_coordination.db.
        dispatch_dir:    Root directory for dispatch bundle storage.
        quality_db_path: Path to quality_intelligence.db. If None, intelligence
                         injection queries run but return empty results.

    Returns:
        Configured DispatchBroker instance, or None if broker is disabled.
    """
    config = broker_config_from_env()
    if not config["enabled"]:
        return None
    return DispatchBroker(
        state_dir,
        dispatch_dir,
        shadow_mode=config["shadow_mode"],
        quality_db_path=quality_db_path,
        intelligence_enabled=config["intelligence_enabled"],
    )


def broker_config_from_env() -> Dict[str, Any]:
    """Return broker configuration derived from environment variables.

    Reads:
        VNX_BROKER_ENABLED  "1" (default) = enabled, "0" = disabled
        VNX_BROKER_SHADOW   "1" (default) = shadow mode ON, "0" = OFF

    Returns:
        Dict with keys:
            enabled (bool):     Whether the broker is enabled.
            shadow_mode (bool): Whether shadow mode is active.
    """
    enabled = os.environ.get("VNX_BROKER_ENABLED", "1").strip() != "0"
    shadow_mode = os.environ.get("VNX_BROKER_SHADOW", "1").strip() != "0"
    intelligence_enabled = os.environ.get("VNX_INTELLIGENCE_INJECTION", "1").strip() != "0"
    return {
        "enabled": enabled,
        "shadow_mode": shadow_mode,
        "intelligence_enabled": intelligence_enabled,
    }
