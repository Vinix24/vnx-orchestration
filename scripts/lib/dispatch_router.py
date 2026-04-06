#!/usr/bin/env python3
"""
VNX Dispatch Router — Task-class-based execution target selection.

Routes dispatches to execution targets based on task class, health,
capabilities, and the routing invariants defined in 30_FPC_EXECUTION_CONTRACTS.md.

Routing Invariants:
  R-1: coding_interactive MUST route to interactive_tmux_* (hard)
  R-2: research_structured/docs_synthesis MAY route to headless_*_cli (soft)
  R-3: channel_response must have entered via inbox first (hard)
  R-4: ops_watchdog prefers interactive_tmux_* (soft)
  R-5: No routing to target without task_class in capabilities (hard)
  R-6: No routing to unhealthy/offline targets (hard)
  R-7: All routing decisions emit routing_decision coordination event (audit)
  R-8: T0 may override routing via target_id metadata (respects R-5, R-6)

Feature flags:
  VNX_HEADLESS_ROUTING  "0" (default) = headless routing disabled, "1" = enabled
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from runtime_coordination import (
    _append_event,
    _now_utc,
    get_connection,
    get_dispatch,
)
from execution_target_registry import (
    ExecutionTargetRegistry,
    TargetRecord,
    INTERACTIVE_TARGET_TYPES,
    HEADLESS_TARGET_TYPES,
    VALID_TASK_CLASSES,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILL_TO_TASK_CLASS = {
    "backend-developer": "coding_interactive",
    "frontend-developer": "coding_interactive",
    "api-developer": "coding_interactive",
    "python-optimizer": "coding_interactive",
    "supabase-expert": "coding_interactive",
    "monitoring-specialist": "coding_interactive",
    "vnx-manager": "coding_interactive",
    "debugger": "coding_interactive",
    "test-engineer": "coding_interactive",
    "quality-engineer": "coding_interactive",
    "architect": "research_structured",
    "reviewer": "research_structured",
    "planner": "research_structured",
    "data-analyst": "research_structured",
    "performance-profiler": "research_structured",
    "security-engineer": "research_structured",
    "t0-orchestrator": "research_structured",
    "excel-reporter": "docs_synthesis",
    "technical-writer": "docs_synthesis",
}

HEADLESS_ELIGIBLE_TASK_CLASSES = frozenset({
    "research_structured",
    "docs_synthesis",
})


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

def headless_routing_enabled() -> bool:
    """Return True when VNX_HEADLESS_ROUTING == "1"."""
    return os.environ.get("VNX_HEADLESS_ROUTING", "0").strip() == "1"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RoutingError(Exception):
    """Base error for routing failures."""


class NoEligibleTargetError(RoutingError):
    """Raised when no target matches the routing criteria."""


class RoutingInvariantViolation(RoutingError):
    """Raised when a routing decision would violate a hard invariant."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class RoutingDecision:
    """Result of a routing decision."""
    dispatch_id: str
    task_class: str
    selected_target: Optional[TargetRecord]
    selected_target_id: Optional[str] = None
    selected_target_type: Optional[str] = None
    candidates_evaluated: int = 0
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    queued: bool = False
    escalation_reason: Optional[str] = None

    @property
    def routed(self) -> bool:
        return self.selected_target is not None


# ---------------------------------------------------------------------------
# DispatchRouter
# ---------------------------------------------------------------------------

class DispatchRouter:
    """Routes dispatches to execution targets per FP-C routing invariants.

    All routing decisions emit coordination_events (R-7).
    Respects hard constraints (R-1, R-3, R-5, R-6) and soft preferences (R-2, R-4).
    Supports T0 override via target_id metadata (R-8).

    Args:
        state_dir: Directory containing runtime_coordination.db.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self._state_dir = Path(state_dir)
        self._registry = ExecutionTargetRegistry(state_dir)

    @property
    def registry(self) -> ExecutionTargetRegistry:
        return self._registry

    # ------------------------------------------------------------------
    # Task class resolution
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_task_class(
        skill: Optional[str] = None,
        explicit_task_class: Optional[str] = None,
    ) -> str:
        """Resolve the canonical task class for a dispatch.

        Priority:
          1. explicit_task_class (T0 override)
          2. Skill-to-task-class mapping
          3. Default: coding_interactive

        Returns a valid task class string.
        """
        if explicit_task_class and explicit_task_class in VALID_TASK_CLASSES:
            return explicit_task_class

        if skill:
            normalized = skill.strip().lstrip("/").lower()
            return SKILL_TO_TASK_CLASS.get(normalized, "coding_interactive")

        return "coding_interactive"

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def route(
        self,
        dispatch_id: str,
        task_class: str,
        *,
        terminal_id: Optional[str] = None,
        target_id_override: Optional[str] = None,
        channel_origin: Optional[str] = None,
        actor: str = "router",
    ) -> RoutingDecision:
        """Select the best execution target for a dispatch.

        Implements the full routing decision flow from Section 3.1:
          1. Handle T0 override (R-8)
          2. Enforce hard invariants (R-1, R-3, R-5, R-6)
          3. Apply soft preferences (R-2, R-4)
          4. Emit routing_decision event (R-7)

        Returns a RoutingDecision. If no target is available, decision.routed
        is False and decision.queued or decision.escalation_reason explain why.
        """
        # R-3: channel_response must have entered via inbox
        if task_class == "channel_response" and not channel_origin:
            decision = RoutingDecision(
                dispatch_id=dispatch_id,
                task_class=task_class,
                selected_target=None,
                escalation_reason="R-3: channel_response dispatch has no channel_origin; "
                                  "must enter via inbound inbox first",
            )
            self._emit_routing_event(decision, actor=actor)
            return decision

        # R-8: T0 override
        if target_id_override:
            return self._route_with_override(
                dispatch_id, task_class, target_id_override, actor=actor,
            )

        # Get all routing-eligible candidates for this task class
        candidates = self._registry.list_routing_eligible(
            task_class, terminal_id=terminal_id,
        )

        if not candidates:
            return self._handle_no_candidates(
                dispatch_id, task_class, terminal_id, actor=actor,
            )

        # Apply routing strategy based on task class
        selected, fallback_used, fallback_reason = self._select_target(
            task_class, candidates,
        )

        decision = RoutingDecision(
            dispatch_id=dispatch_id,
            task_class=task_class,
            selected_target=selected,
            selected_target_id=selected.target_id if selected else None,
            selected_target_type=selected.target_type if selected else None,
            candidates_evaluated=len(candidates),
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )

        if not selected:
            decision.queued = True
            decision.escalation_reason = (
                f"No suitable target after evaluating {len(candidates)} candidates"
            )

        self._emit_routing_event(decision, actor=actor)
        return decision

    # ------------------------------------------------------------------
    # Override routing (R-8)
    # ------------------------------------------------------------------

    def _route_with_override(
        self,
        dispatch_id: str,
        task_class: str,
        target_id: str,
        *,
        actor: str,
    ) -> RoutingDecision:
        """Route to a specific target_id override, still enforcing R-5 and R-6."""
        target = self._registry.get(target_id)

        if target is None:
            decision = RoutingDecision(
                dispatch_id=dispatch_id,
                task_class=task_class,
                selected_target=None,
                escalation_reason=f"R-8 override target not found: {target_id!r}",
            )
            self._emit_routing_event(decision, actor=actor)
            return decision

        # R-6: health check
        if not target.is_routing_eligible:
            decision = RoutingDecision(
                dispatch_id=dispatch_id,
                task_class=task_class,
                selected_target=None,
                candidates_evaluated=1,
                escalation_reason=(
                    f"R-6: override target {target_id!r} health is {target.health!r}, "
                    f"not routing-eligible"
                ),
            )
            self._emit_routing_event(decision, actor=actor)
            return decision

        # R-5: capability check
        if not target.supports_task_class(task_class):
            decision = RoutingDecision(
                dispatch_id=dispatch_id,
                task_class=task_class,
                selected_target=None,
                candidates_evaluated=1,
                escalation_reason=(
                    f"R-5: override target {target_id!r} does not declare "
                    f"capability {task_class!r}"
                ),
            )
            self._emit_routing_event(decision, actor=actor)
            return decision

        decision = RoutingDecision(
            dispatch_id=dispatch_id,
            task_class=task_class,
            selected_target=target,
            selected_target_id=target.target_id,
            selected_target_type=target.target_type,
            candidates_evaluated=1,
        )
        self._emit_routing_event(decision, actor=actor)
        return decision

    # ------------------------------------------------------------------
    # Target selection strategy
    # ------------------------------------------------------------------

    def _select_target(
        self,
        task_class: str,
        candidates: List[TargetRecord],
    ) -> tuple[Optional[TargetRecord], bool, Optional[str]]:
        """Select the best target from candidates based on task class rules.

        Returns (selected_target, fallback_used, fallback_reason).
        """
        # R-1: coding_interactive MUST route to interactive_tmux_*
        if task_class == "coding_interactive":
            interactive = [c for c in candidates if c.is_interactive]
            if interactive:
                return interactive[0], False, None
            return None, False, None

        # R-2: research_structured/docs_synthesis MAY route headless
        if task_class in HEADLESS_ELIGIBLE_TASK_CLASSES:
            if headless_routing_enabled():
                headless = [c for c in candidates if c.is_headless]
                if headless:
                    return headless[0], False, None
                # Fallback to interactive
                interactive = [c for c in candidates if c.is_interactive]
                if interactive:
                    return interactive[0], True, "No headless target available; fell back to interactive"
                return None, False, None
            else:
                # Headless routing disabled: route interactive only
                interactive = [c for c in candidates if c.is_interactive]
                if interactive:
                    return interactive[0], False, None
                return None, False, None

        # R-4: ops_watchdog prefers interactive
        if task_class == "ops_watchdog":
            interactive = [c for c in candidates if c.is_interactive]
            if interactive:
                return interactive[0], False, None
            # Allow headless if no interactive and headless routing is enabled
            if headless_routing_enabled():
                headless = [c for c in candidates if c.is_headless]
                if headless:
                    return headless[0], True, "No interactive target; fell back to headless for ops"
            return None, False, None

        # channel_response and any other: take first available
        if candidates:
            return candidates[0], False, None

        return None, False, None

    # ------------------------------------------------------------------
    # No-candidate handling
    # ------------------------------------------------------------------

    def _handle_no_candidates(
        self,
        dispatch_id: str,
        task_class: str,
        terminal_id: Optional[str],
        *,
        actor: str,
    ) -> RoutingDecision:
        """Handle the case when no candidates are routing-eligible."""
        if task_class == "coding_interactive":
            reason = "No interactive target available for coding dispatch; queued for T0 escalation"
        elif task_class == "channel_response":
            reason = "Channel adapter offline; event dead-lettered"
        else:
            reason = f"All targets unhealthy/offline for task_class={task_class!r}; T0 escalation"

        decision = RoutingDecision(
            dispatch_id=dispatch_id,
            task_class=task_class,
            selected_target=None,
            candidates_evaluated=0,
            queued=True,
            escalation_reason=reason,
        )
        self._emit_routing_event(decision, actor=actor)
        return decision

    # ------------------------------------------------------------------
    # Event emission (R-7)
    # ------------------------------------------------------------------

    def _emit_routing_event(
        self,
        decision: RoutingDecision,
        *,
        actor: str,
    ) -> None:
        """Emit a routing_decision coordination event per R-7."""
        metadata = {
            "task_class": decision.task_class,
            "candidates_evaluated": decision.candidates_evaluated,
            "fallback_used": decision.fallback_used,
        }
        if decision.selected_target_id:
            metadata["selected_target_id"] = decision.selected_target_id
            metadata["selected_target_type"] = decision.selected_target_type
        if decision.fallback_reason:
            metadata["fallback_reason"] = decision.fallback_reason
        if decision.escalation_reason:
            metadata["escalation_reason"] = decision.escalation_reason

        reason_parts = []
        if decision.routed:
            reason_parts.append(
                f"task_class={decision.task_class} routed to {decision.selected_target_id}"
            )
        else:
            reason_parts.append(
                f"task_class={decision.task_class} not routed"
            )
        if decision.escalation_reason:
            reason_parts.append(decision.escalation_reason)

        try:
            with get_connection(self._state_dir) as conn:
                _append_event(
                    conn,
                    event_type="routing_decision",
                    entity_type="dispatch",
                    entity_id=decision.dispatch_id,
                    from_state="queued" if not decision.routed else "queued",
                    to_state="claimed" if decision.routed else "queued",
                    actor=actor,
                    reason="; ".join(reason_parts),
                    metadata=metadata,
                )
                conn.commit()
        except Exception:
            pass  # Shadow mode: DB may not exist
