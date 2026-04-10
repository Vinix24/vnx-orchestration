#!/usr/bin/env python3
"""Reusable manager/worker orchestration substrate (Feature 19, PR-1).

Extracts the stable patterns from the coding runtime/orchestration layer
into a domain-agnostic substrate so future domains (content, research,
Agent OS) gain a real integration seam rather than a conceptual one.

Components:
  StateTransitionSpec   — portable, DB-free state machine specification
  WorkerHandle          — domain-agnostic worker identity and status carrier
  ManagerProtocol       — stable seam for domain-specific manager implementations
  WorkerProtocol        — stable seam for domain-specific worker implementations
  CodingManagerAdapter  — maps WorkerStateManager → ManagerProtocol (coding domain)
  coding_lifecycle_spec — returns the coding domain's transition spec
  validate_transition   — stateless helper usable by any domain

Design invariants:
  - Substrate has no database dependency. All persistence is in domain adapters.
  - Coding domain behavior is unchanged. CodingManagerAdapter is additive only.
  - Any domain that implements ManagerProtocol is a first-class substrate citizen.
  - WorkerHandle.domain makes cross-domain orchestration explicit.

Usage (coding domain):
    from orchestration_substrate import CodingManagerAdapter
    from worker_state_manager import WorkerStateManager

    mgr = CodingManagerAdapter(WorkerStateManager(state_dir))
    handle = mgr.allocate_worker("T1", "d-001")
    handle = mgr.advance_worker("T1", "working")
    state = mgr.query_worker("T1")
    mgr.release_worker("T1")

Usage (future domain):
    class ContentManagerAdapter:
        def allocate_worker(...) -> WorkerHandle: ...
        def advance_worker(...) -> WorkerHandle: ...
        def release_worker(...) -> None: ...
        def query_worker(...) -> Optional[WorkerHandle]: ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Generic lifecycle state machine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StateTransitionSpec:
    """Portable state machine specification for a manager/worker domain.

    Attributes:
        domain:          Domain name (e.g., "coding", "content").
        states:          Complete set of valid state names.
        terminal_states: States from which no transition is possible.
        transitions:     Map of state -> allowed destination states.
    """
    domain: str
    states: FrozenSet[str]
    terminal_states: FrozenSet[str]
    transitions: Dict[str, FrozenSet[str]]

    def validate_transition(self, from_state: str, to_state: str) -> None:
        """Raise ValueError if the transition is not permitted."""
        if from_state not in self.states:
            raise ValueError(f"[{self.domain}] Unknown state: {from_state!r}")
        if to_state not in self.states:
            raise ValueError(f"[{self.domain}] Unknown state: {to_state!r}")
        allowed = self.transitions.get(from_state, frozenset())
        if to_state not in allowed:
            raise ValueError(
                f"[{self.domain}] Transition {from_state!r} -> {to_state!r} not permitted. "
                f"Allowed from {from_state!r}: {sorted(allowed) or 'none (terminal)'}"
            )

    def is_terminal(self, state: str) -> bool:
        return state in self.terminal_states

    def reachable_from(self, state: str) -> FrozenSet[str]:
        """Return the set of states directly reachable from the given state."""
        return self.transitions.get(state, frozenset())


# ---------------------------------------------------------------------------
# Domain-agnostic worker identity
# ---------------------------------------------------------------------------

@dataclass
class WorkerHandle:
    """Domain-agnostic handle representing one worker's identity and status.

    Attributes:
        worker_id:     Canonical worker identifier (e.g., "T1", "agent-7").
        domain:        Domain the worker belongs to (e.g., "coding", "content").
        current_state: Current lifecycle state in the domain's spec.
        task_id:       Current task/dispatch ID (empty if not assigned).
        metadata:      Domain-specific extra fields for downstream consumers.
    """
    worker_id: str
    domain: str
    current_state: str
    task_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_active(self, spec: StateTransitionSpec) -> bool:
        """Return True if current_state is not terminal in the given spec."""
        return not spec.is_terminal(self.current_state)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "domain": self.domain,
            "current_state": self.current_state,
            "task_id": self.task_id,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Stable domain seams (Protocols)
# ---------------------------------------------------------------------------

@runtime_checkable
class ManagerProtocol(Protocol):
    """Stable seam for domain-specific manager implementations.

    Any domain that provides these four operations is a first-class substrate
    citizen. CodingManagerAdapter is the reference implementation for the
    coding domain.
    """

    def allocate_worker(self, worker_id: str, task_id: str) -> WorkerHandle:
        """Reserve a worker for a task. Returns a handle in the initial state."""
        ...

    def advance_worker(self, worker_id: str, to_state: str) -> WorkerHandle:
        """Transition a worker to a new state. Returns updated handle."""
        ...

    def release_worker(self, worker_id: str) -> None:
        """Release a worker after task completion or abandonment."""
        ...

    def query_worker(self, worker_id: str) -> Optional[WorkerHandle]:
        """Return current handle for a worker, or None if not allocated."""
        ...


@runtime_checkable
class WorkerProtocol(Protocol):
    """Stable seam for domain-specific worker implementations."""

    def report_ready(self, worker_id: str) -> None:
        """Worker signals it is ready to receive tasks."""
        ...

    def report_progress(self, worker_id: str) -> None:
        """Worker signals active progress (heartbeat equivalent)."""
        ...

    def report_done(self, worker_id: str, *, success: bool) -> None:
        """Worker signals task completion (success or failure)."""
        ...


# ---------------------------------------------------------------------------
# Coding domain lifecycle spec
# ---------------------------------------------------------------------------

def coding_lifecycle_spec() -> StateTransitionSpec:
    """Return the state transition spec for the VNX coding domain.

    Mirrors WORKER_TRANSITIONS from worker_state_manager.py without
    importing it — substrate stays DB-free.
    """
    states = frozenset({
        "initializing", "working", "idle_between_tasks", "stalled",
        "blocked", "awaiting_input", "exited_clean", "exited_bad", "resume_unsafe",
    })
    terminal_states = frozenset({"exited_clean", "exited_bad", "resume_unsafe"})
    transitions: Dict[str, FrozenSet[str]] = {
        "initializing":       frozenset({"working", "stalled", "blocked", "exited_clean", "exited_bad", "resume_unsafe"}),
        "working":            frozenset({"idle_between_tasks", "stalled", "blocked", "awaiting_input", "exited_clean", "exited_bad", "resume_unsafe"}),
        "idle_between_tasks": frozenset({"working", "stalled", "exited_clean", "exited_bad", "resume_unsafe"}),
        "stalled":            frozenset({"working", "exited_bad", "resume_unsafe"}),
        "blocked":            frozenset({"working", "exited_bad", "resume_unsafe"}),
        "awaiting_input":     frozenset({"working", "exited_bad", "resume_unsafe"}),
        "exited_clean":       frozenset(),
        "exited_bad":         frozenset(),
        "resume_unsafe":      frozenset(),
    }
    return StateTransitionSpec(
        domain="coding",
        states=states,
        terminal_states=terminal_states,
        transitions=transitions,
    )


# ---------------------------------------------------------------------------
# Coding domain adapter (compatibility bridge)
# ---------------------------------------------------------------------------

class CodingManagerAdapter:
    """Adapts WorkerStateManager to ManagerProtocol.

    This is additive only — WorkerStateManager is unchanged. The adapter
    provides the substrate interface on top of the coding domain's existing
    manager, allowing substrate-level code to drive coding workers without
    knowing about SQLite, coordination events, or heartbeat thresholds.
    """

    def __init__(self, state_manager: Any) -> None:
        """Accept any WorkerStateManager-compatible object (duck-typed)."""
        self._mgr = state_manager

    def allocate_worker(self, worker_id: str, task_id: str) -> WorkerHandle:
        result = self._mgr.initialize(worker_id, task_id)
        return WorkerHandle(
            worker_id=result.terminal_id,
            domain="coding",
            current_state=result.state,
            task_id=result.dispatch_id,
        )

    def advance_worker(self, worker_id: str, to_state: str) -> WorkerHandle:
        result = self._mgr.transition(worker_id, to_state)
        return WorkerHandle(
            worker_id=result.terminal_id,
            domain="coding",
            current_state=result.state,
            task_id=result.dispatch_id,
        )

    def release_worker(self, worker_id: str) -> None:
        self._mgr.cleanup(worker_id)

    def query_worker(self, worker_id: str) -> Optional[WorkerHandle]:
        result = self._mgr.get(worker_id)
        if result is None:
            return None
        return WorkerHandle(
            worker_id=result.terminal_id,
            domain="coding",
            current_state=result.state,
            task_id=result.dispatch_id,
        )


# ---------------------------------------------------------------------------
# Stateless transition helper (usable without a spec instance)
# ---------------------------------------------------------------------------

def validate_transition(
    from_state: str,
    to_state: str,
    *,
    spec: StateTransitionSpec,
) -> None:
    """Stateless helper for one-off transition validation against a spec."""
    spec.validate_transition(from_state, to_state)
