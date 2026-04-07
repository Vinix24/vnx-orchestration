#!/usr/bin/env python3
"""
VNX Mixed Execution Router — PR-5 cutover orchestration.

Wires together DispatchRouter, HeadlessAdapter, IntelligenceSelector,
InboundInbox, and RecommendationTracker into a single orchestration
entry point with cutover controls and rollback.

This is the live dispatch path after FP-C cutover:
  1. Resolve task class from dispatch metadata
  2. Run bounded intelligence injection (dispatch_create or dispatch_resume)
  3. Route dispatch to the correct execution target (interactive or headless)
  4. If headless: execute via HeadlessAdapter with receipt production
  5. If interactive: return routing decision for tmux delivery
  6. Emit routing + intelligence events for audit trail

Cutover controls:
  VNX_MIXED_EXECUTION    "0" (default) = cutover disabled, "1" = enabled
  VNX_HEADLESS_ROUTING   "0" (default) = headless routing disabled, "1" = enabled
  VNX_BROKER_SHADOW      "1" (default) = shadow mode, "0" = authoritative

Rollback:
  Set VNX_MIXED_EXECUTION=0 or VNX_HEADLESS_ROUTING=0 to revert to
  all-interactive routing. No data migration needed — routing decisions
  are stateless and the next dispatch uses the current flag values.

Governance:
  G-R1: Routing is explicit and reviewable (routing_decision events)
  G-R2: Coding stays interactive by default
  G-R3: Headless execution is durable and receipt-producing
  G-R4: Inbound events become dispatches before work starts
  G-R5: Intelligence injection bounded to 3 items
  G-R6: Evidence metadata on every intelligence item
  G-R7: Recommendations are advisory-only
  G-R8: No execution-mode change bypasses T0 authority
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from dispatch_router import (
    DispatchRouter,
    RoutingDecision,
    RoutingError,
    headless_routing_enabled,
)
from dispatch_broker import (
    DispatchBroker,
    load_broker,
)
from execution_target_registry import (
    ExecutionTargetRegistry,
    HEADLESS_TARGET_TYPES,
    INTERACTIVE_TARGET_TYPES,
)
from headless_adapter import (
    HeadlessAdapter,
    HeadlessExecutionResult,
    headless_enabled,
    load_headless_adapter,
)
from inbound_inbox import InboundInbox
from intelligence_selector import IntelligenceSelector
from recommendation_tracker import RecommendationTracker
from runtime_coordination import (
    _append_event,
    _now_utc,
    get_connection,
)


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

def mixed_execution_enabled() -> bool:
    """Return True when VNX_MIXED_EXECUTION == '1'."""
    return os.environ.get("VNX_MIXED_EXECUTION", "0").strip() == "1"


def cutover_config_from_env() -> Dict[str, Any]:
    """Return the full cutover configuration from environment."""
    return {
        "mixed_execution": mixed_execution_enabled(),
        "headless_routing": headless_routing_enabled(),
        "headless_enabled": headless_enabled(),
        "broker_shadow": os.environ.get("VNX_BROKER_SHADOW", "1").strip() != "0",
        "intelligence_injection": os.environ.get(
            "VNX_INTELLIGENCE_INJECTION", "1"
        ).strip() != "0",
    }


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class MixedRoutingResult:
    """Complete result of a mixed execution routing decision."""
    dispatch_id: str
    task_class: str
    routing_decision: Optional[RoutingDecision] = None
    intelligence_payload: Optional[Dict[str, Any]] = None
    headless_result: Optional[HeadlessExecutionResult] = None
    execution_mode: str = "interactive"  # "interactive" | "headless" | "queued"
    cutover_active: bool = False
    rollback_available: bool = True
    evidence_trail: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def routed(self) -> bool:
        return self.routing_decision is not None and self.routing_decision.routed

    @property
    def headless_executed(self) -> bool:
        return self.headless_result is not None

    @property
    def headless_succeeded(self) -> bool:
        return self.headless_result is not None and self.headless_result.success

    def to_evidence_dict(self) -> Dict[str, Any]:
        """Return a dict suitable for audit trail inclusion."""
        result: Dict[str, Any] = {
            "dispatch_id": self.dispatch_id,
            "task_class": self.task_class,
            "execution_mode": self.execution_mode,
            "cutover_active": self.cutover_active,
            "rollback_available": self.rollback_available,
        }
        if self.routing_decision:
            result["routing"] = {
                "routed": self.routing_decision.routed,
                "target_id": self.routing_decision.selected_target_id,
                "target_type": self.routing_decision.selected_target_type,
                "fallback_used": self.routing_decision.fallback_used,
                "candidates_evaluated": self.routing_decision.candidates_evaluated,
            }
        if self.intelligence_payload:
            items = self.intelligence_payload.get("items", [])
            result["intelligence"] = {
                "items_injected": len(items),
                "injection_point": self.intelligence_payload.get("injection_point"),
            }
        if self.headless_result:
            result["headless"] = {
                "success": self.headless_result.success,
                "duration_seconds": self.headless_result.duration_seconds,
                "exit_code": self.headless_result.exit_code,
            }
        if self.error:
            result["error"] = self.error
        result["evidence_trail"] = self.evidence_trail
        return result


# ---------------------------------------------------------------------------
# MixedExecutionRouter
# ---------------------------------------------------------------------------

class MixedExecutionRouter:
    """Orchestrates mixed execution routing with cutover controls.

    Wires together all FP-C subsystems:
      - DispatchRouter for task-class-based target selection
      - HeadlessAdapter for CLI subprocess execution
      - IntelligenceSelector for bounded injection
      - InboundInbox for channel event intake
      - RecommendationTracker for usefulness metrics

    Args:
        state_dir:       Directory containing runtime_coordination.db.
        dispatch_dir:    Root directory for dispatch bundles.
        output_dir:      Directory for headless execution output.
        quality_db_path: Path to quality_intelligence.db.
    """

    def __init__(
        self,
        state_dir: str | Path,
        dispatch_dir: str | Path,
        output_dir: Optional[str | Path] = None,
        quality_db_path: Optional[str | Path] = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._dispatch_dir = Path(dispatch_dir)
        self._output_dir = Path(output_dir) if output_dir else self._state_dir.parent / "headless_output"
        self._quality_db_path = Path(quality_db_path) if quality_db_path else None

        self._router = DispatchRouter(state_dir)
        self._inbox = InboundInbox(state_dir)
        self._tracker = RecommendationTracker(state_dir)

    @property
    def router(self) -> DispatchRouter:
        return self._router

    @property
    def registry(self) -> ExecutionTargetRegistry:
        return self._router.registry

    @property
    def inbox(self) -> InboundInbox:
        return self._inbox

    @property
    def tracker(self) -> RecommendationTracker:
        return self._tracker

    # ------------------------------------------------------------------
    # Core routing entry point
    # ------------------------------------------------------------------

    def route_dispatch(
        self,
        dispatch_id: str,
        *,
        task_class: Optional[str] = None,
        skill_name: Optional[str] = None,
        terminal_id: Optional[str] = None,
        target_id_override: Optional[str] = None,
        channel_origin: Optional[str] = None,
        track: Optional[str] = None,
        gate: Optional[str] = None,
        injection_point: str = "dispatch_create",
        actor: str = "mixed_router",
    ) -> MixedRoutingResult:
        """Route a dispatch through the mixed execution pipeline.

        Steps:
          1. Check cutover state and resolve task class
          2. Run intelligence injection
          3. Route via DispatchRouter (if cutover active)
          4. Execute headless or return interactive routing decision

        Returns MixedRoutingResult with full audit trail.
        """
        config = cutover_config_from_env()
        evidence: List[Dict[str, Any]] = []

        cutover_active = config["mixed_execution"]
        evidence.append({"step": "cutover_check", "config": config, "timestamp": _now_utc()})

        resolved_class = DispatchRouter.resolve_task_class(
            skill=skill_name, explicit_task_class=task_class,
        )
        evidence.append({
            "step": "task_class_resolution",
            "resolved": resolved_class, "from_skill": skill_name, "explicit": task_class,
        })

        intelligence_payload = self._run_intelligence_injection(
            config, dispatch_id, injection_point, resolved_class, skill_name, track, gate, evidence,
        )

        if not cutover_active:
            return MixedRoutingResult(
                dispatch_id=dispatch_id, task_class=resolved_class,
                intelligence_payload=intelligence_payload, execution_mode="interactive",
                cutover_active=False, rollback_available=True, evidence_trail=evidence,
            )

        return self._route_and_execute(
            dispatch_id=dispatch_id, resolved_class=resolved_class,
            intelligence_payload=intelligence_payload, terminal_id=terminal_id,
            target_id_override=target_id_override, channel_origin=channel_origin,
            actor=actor, config=config, evidence=evidence,
        )

    def _run_intelligence_injection(
        self,
        config: Dict[str, Any],
        dispatch_id: str,
        injection_point: str,
        task_class: str,
        skill_name: Optional[str],
        track: Optional[str],
        gate: Optional[str],
        evidence: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Run bounded intelligence injection if enabled, appending to evidence trail."""
        if not config["intelligence_injection"]:
            return None

        payload = self._inject_intelligence(
            dispatch_id=dispatch_id, injection_point=injection_point,
            task_class=task_class, skill_name=skill_name, track=track, gate=gate,
        )
        evidence.append({
            "step": "intelligence_injection",
            "injection_point": injection_point,
            "items_injected": len(payload.get("items", [])) if payload else 0,
        })
        return payload

    def _route_and_execute(
        self,
        *,
        dispatch_id: str,
        resolved_class: str,
        intelligence_payload: Optional[Dict[str, Any]],
        terminal_id: Optional[str],
        target_id_override: Optional[str],
        channel_origin: Optional[str],
        actor: str,
        config: Dict[str, Any],
        evidence: List[Dict[str, Any]],
    ) -> MixedRoutingResult:
        """Route via DispatchRouter and execute (headless or interactive)."""
        try:
            routing = self._router.route(
                dispatch_id=dispatch_id, task_class=resolved_class,
                terminal_id=terminal_id, target_id_override=target_id_override,
                channel_origin=channel_origin, actor=actor,
            )
        except RoutingError as exc:
            return MixedRoutingResult(
                dispatch_id=dispatch_id, task_class=resolved_class,
                intelligence_payload=intelligence_payload, execution_mode="queued",
                cutover_active=True, evidence_trail=evidence, error=str(exc),
            )

        evidence.append({
            "step": "routing_decision", "routed": routing.routed,
            "target_id": routing.selected_target_id,
            "target_type": routing.selected_target_type,
            "fallback_used": routing.fallback_used,
        })

        if not routing.routed:
            return MixedRoutingResult(
                dispatch_id=dispatch_id, task_class=resolved_class,
                routing_decision=routing, intelligence_payload=intelligence_payload,
                execution_mode="queued", cutover_active=True,
                evidence_trail=evidence, error=routing.escalation_reason,
            )

        target_type = routing.selected_target_type or ""
        is_headless = target_type in HEADLESS_TARGET_TYPES and config["headless_enabled"]
        execution_mode = "headless" if is_headless else "interactive"

        headless_result = None
        if is_headless:
            headless_result = self._execute_headless(
                dispatch_id=dispatch_id, target_id=routing.selected_target_id or "",
                target_type=target_type, task_class=resolved_class,
                terminal_id=terminal_id, actor=actor,
            )
            evidence.append({
                "step": "headless_execution",
                "success": headless_result.success if headless_result else False,
                "duration": headless_result.duration_seconds if headless_result else 0,
            })

        self._emit_mixed_routing_event(
            dispatch_id=dispatch_id, execution_mode=execution_mode,
            routing=routing, intelligence_payload=intelligence_payload, actor=actor,
        )

        return MixedRoutingResult(
            dispatch_id=dispatch_id, task_class=resolved_class,
            routing_decision=routing, intelligence_payload=intelligence_payload,
            headless_result=headless_result, execution_mode=execution_mode,
            cutover_active=True, evidence_trail=evidence,
        )

    # ------------------------------------------------------------------
    # Channel event intake
    # ------------------------------------------------------------------

    def route_channel_event(
        self,
        channel_id: str,
        payload: Dict[str, Any],
        *,
        dedupe_key: Optional[str] = None,
        routing_hints: Optional[Dict[str, Any]] = None,
        dispatch_id_generator: Optional[Any] = None,
        actor: str = "mixed_router",
    ) -> MixedRoutingResult:
        """Ingest a channel event through the inbox and route the resulting dispatch.

        Full lifecycle: receive -> process -> route -> execute (if headless eligible).
        """
        # Step 1: Receive into inbox
        receive_result = self._inbox.receive(
            channel_id=channel_id,
            payload=payload,
            dedupe_key=dedupe_key,
            routing_hints=routing_hints,
        )

        event_id = receive_result.event.event_id

        if receive_result.already_existed:
            return MixedRoutingResult(
                dispatch_id="",
                task_class="channel_response",
                execution_mode="queued",
                cutover_active=mixed_execution_enabled(),
                error=f"Duplicate event: {event_id}",
                evidence_trail=[{
                    "step": "inbox_receive",
                    "dedupe_hit": True,
                    "event_id": event_id,
                }],
            )

        # Step 2: Process inbox event into dispatch
        process_result = self._inbox.process(
            event_id=event_id,
            dispatch_id_generator=dispatch_id_generator,
        )

        if process_result.outcome not in ("dispatched",):
            return MixedRoutingResult(
                dispatch_id="",
                task_class="channel_response",
                execution_mode="queued",
                cutover_active=mixed_execution_enabled(),
                error=process_result.failure_reason or f"Inbox processing outcome: {process_result.outcome}",
                evidence_trail=[{
                    "step": "inbox_process",
                    "success": False,
                    "event_id": event_id,
                }],
            )

        # Step 3: Route the resulting dispatch
        hints = routing_hints or {}
        return self.route_dispatch(
            dispatch_id=process_result.dispatch_id,
            task_class=hints.get("task_class", "channel_response"),
            terminal_id=hints.get("terminal_id"),
            channel_origin=channel_id,
            track=hints.get("track"),
            gate=hints.get("gate"),
            actor=actor,
        )

    # ------------------------------------------------------------------
    # Rollback controls
    # ------------------------------------------------------------------

    def rollback_to_interactive(self) -> Dict[str, Any]:
        """Return instructions for rolling back to all-interactive routing.

        Does NOT modify environment — the operator sets env vars externally.
        Returns a dict describing what to set and current state.
        """
        config = cutover_config_from_env()
        return {
            "action": "rollback_to_interactive",
            "current_config": config,
            "instructions": {
                "VNX_MIXED_EXECUTION": "0",
                "VNX_HEADLESS_ROUTING": "0",
            },
            "effect": "All dispatches will route to interactive tmux targets. "
                      "No headless execution. No data migration needed.",
            "reversible": True,
            "timestamp": _now_utc(),
        }

    def cutover_status(self) -> Dict[str, Any]:
        """Return current cutover status with health summary."""
        config = cutover_config_from_env()
        registry = self._router.registry

        headless_targets = registry.list_headless_targets(healthy_only=False)
        healthy_headless = registry.list_headless_targets(healthy_only=True)

        return {
            "cutover_config": config,
            "execution_targets": {
                "total_headless": len(headless_targets),
                "healthy_headless": len(healthy_headless),
            },
            "routing_mode": (
                "mixed" if config["mixed_execution"] and config["headless_routing"]
                else "interactive_only"
            ),
            "rollback_available": True,
            "timestamp": _now_utc(),
        }

    # ------------------------------------------------------------------
    # Intelligence injection
    # ------------------------------------------------------------------

    def _inject_intelligence(
        self,
        dispatch_id: str,
        injection_point: str,
        task_class: str,
        skill_name: Optional[str],
        track: Optional[str],
        gate: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Run bounded intelligence selection and return the payload dict."""
        try:
            selector = IntelligenceSelector(
                quality_db_path=self._quality_db_path,
                coord_db_state_dir=self._state_dir,
            )
            result = selector.select(
                dispatch_id=dispatch_id,
                injection_point=injection_point,
                task_class=task_class,
                skill_name=skill_name,
                track=track,
                gate=gate,
            )
            selector.emit_event(result)
            selector.record_injection(result)
            payload = result.to_payload_dict()
            selector.close()
            return payload if result.items_injected > 0 else {
                "injection_point": injection_point,
                "injected_at": result.injected_at,
                "items": [],
                "suppressed": [s.to_dict() for s in result.suppressed],
            }
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Headless execution
    # ------------------------------------------------------------------

    def _execute_headless(
        self,
        dispatch_id: str,
        target_id: str,
        target_type: str,
        task_class: str,
        terminal_id: Optional[str],
        actor: str,
    ) -> Optional[HeadlessExecutionResult]:
        """Execute a dispatch headlessly via the HeadlessAdapter."""
        try:
            adapter = HeadlessAdapter(
                self._state_dir,
                self._dispatch_dir,
                self._output_dir,
            )
            return adapter.execute(
                dispatch_id=dispatch_id,
                target_id=target_id,
                target_type=target_type,
                task_class=task_class,
                terminal_id=terminal_id,
                actor=actor,
            )
        except Exception as exc:
            self._emit_event(
                "headless_execution_error",
                dispatch_id=dispatch_id,
                metadata={"error": str(exc), "target_id": target_id},
                actor=actor,
            )
            return HeadlessExecutionResult(
                success=False,
                dispatch_id=dispatch_id,
                target_id=target_id,
                target_type=target_type,
                failure_reason=str(exc),
            )

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_mixed_routing_event(
        self,
        dispatch_id: str,
        execution_mode: str,
        routing: RoutingDecision,
        intelligence_payload: Optional[Dict[str, Any]],
        actor: str,
    ) -> None:
        """Emit a mixed_routing_decision coordination event."""
        metadata: Dict[str, Any] = {
            "execution_mode": execution_mode,
            "task_class": routing.task_class,
            "cutover_active": True,
        }
        if routing.selected_target_id:
            metadata["target_id"] = routing.selected_target_id
            metadata["target_type"] = routing.selected_target_type
        if routing.fallback_used:
            metadata["fallback_used"] = True
            metadata["fallback_reason"] = routing.fallback_reason
        if intelligence_payload:
            items = intelligence_payload.get("items", [])
            metadata["intelligence_items_injected"] = len(items)

        self._emit_event(
            "mixed_routing_decision",
            dispatch_id=dispatch_id,
            metadata=metadata,
            actor=actor,
        )

    def _emit_event(
        self,
        event_type: str,
        *,
        dispatch_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "mixed_router",
    ) -> None:
        """Append a coordination event. Silently no-ops if DB unavailable."""
        try:
            with get_connection(self._state_dir) as conn:
                _append_event(
                    conn,
                    event_type=event_type,
                    entity_type="dispatch",
                    entity_id=dispatch_id,
                    actor=actor,
                    metadata=metadata,
                )
                conn.commit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------

def load_mixed_router(
    state_dir: str | Path,
    dispatch_dir: str | Path,
    output_dir: Optional[str | Path] = None,
    quality_db_path: Optional[str | Path] = None,
) -> Optional[MixedExecutionRouter]:
    """Return a MixedExecutionRouter if VNX_MIXED_EXECUTION=1, else None.

    Returns None when cutover is disabled — callers fall through to the
    legacy all-interactive tmux path.
    """
    if not mixed_execution_enabled():
        return None
    return MixedExecutionRouter(
        state_dir=state_dir,
        dispatch_dir=dispatch_dir,
        output_dir=output_dir,
        quality_db_path=quality_db_path,
    )
