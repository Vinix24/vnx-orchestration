#!/usr/bin/env python3
"""Tests for reusable manager/worker orchestration substrate (Feature 19, PR-1).

Covers:
  1. StateTransitionSpec — generic state machine
  2. WorkerHandle — domain-agnostic identity carrier
  3. ManagerProtocol — domain seam conformance
  4. coding_lifecycle_spec — coding domain spec fidelity
  5. CodingManagerAdapter — compatibility bridge with coding WorkerStateManager
  6. Custom domain seam — future domain can implement substrate without change
  7. Coding compatibility preservation — existing behavior unchanged
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from orchestration_substrate import (
    CodingManagerAdapter,
    ManagerProtocol,
    StateTransitionSpec,
    WorkerHandle,
    WorkerProtocol,
    coding_lifecycle_spec,
    validate_transition,
)


# ---------------------------------------------------------------------------
# 1. StateTransitionSpec — generic state machine
# ---------------------------------------------------------------------------

class TestStateTransitionSpec:

    def _minimal_spec(self) -> StateTransitionSpec:
        return StateTransitionSpec(
            domain="test",
            states=frozenset({"idle", "running", "done", "failed"}),
            terminal_states=frozenset({"done", "failed"}),
            transitions={
                "idle":    frozenset({"running", "failed"}),
                "running": frozenset({"done", "failed", "idle"}),
                "done":    frozenset(),
                "failed":  frozenset(),
            },
        )

    def test_valid_transition_passes(self) -> None:
        spec = self._minimal_spec()
        spec.validate_transition("idle", "running")  # no exception

    def test_invalid_transition_raises(self) -> None:
        spec = self._minimal_spec()
        with pytest.raises(ValueError, match="not permitted"):
            spec.validate_transition("done", "running")

    def test_unknown_from_state_raises(self) -> None:
        spec = self._minimal_spec()
        with pytest.raises(ValueError, match="Unknown state"):
            spec.validate_transition("nonexistent", "running")

    def test_unknown_to_state_raises(self) -> None:
        spec = self._minimal_spec()
        with pytest.raises(ValueError, match="Unknown state"):
            spec.validate_transition("idle", "nonexistent")

    def test_terminal_state_has_no_transitions(self) -> None:
        spec = self._minimal_spec()
        assert spec.reachable_from("done") == frozenset()
        assert spec.reachable_from("failed") == frozenset()

    def test_is_terminal_true_for_terminal_states(self) -> None:
        spec = self._minimal_spec()
        assert spec.is_terminal("done") is True
        assert spec.is_terminal("failed") is True

    def test_is_terminal_false_for_active_states(self) -> None:
        spec = self._minimal_spec()
        assert spec.is_terminal("idle") is False
        assert spec.is_terminal("running") is False

    def test_reachable_from_returns_allowed_targets(self) -> None:
        spec = self._minimal_spec()
        assert spec.reachable_from("idle") == frozenset({"running", "failed"})

    def test_domain_name_preserved(self) -> None:
        spec = self._minimal_spec()
        assert spec.domain == "test"

    def test_validate_transition_helper(self) -> None:
        spec = self._minimal_spec()
        validate_transition("idle", "running", spec=spec)  # no exception

    def test_validate_transition_helper_raises(self) -> None:
        spec = self._minimal_spec()
        with pytest.raises(ValueError):
            validate_transition("done", "idle", spec=spec)


# ---------------------------------------------------------------------------
# 2. WorkerHandle
# ---------------------------------------------------------------------------

class TestWorkerHandle:

    def _handle(self, state: str = "idle") -> WorkerHandle:
        return WorkerHandle(
            worker_id="T1", domain="test", current_state=state, task_id="t-001")

    def test_worker_handle_construction(self) -> None:
        h = self._handle("running")
        assert h.worker_id == "T1"
        assert h.domain == "test"
        assert h.current_state == "running"
        assert h.task_id == "t-001"

    def test_is_active_non_terminal(self) -> None:
        spec = StateTransitionSpec(
            domain="test",
            states=frozenset({"active", "done"}),
            terminal_states=frozenset({"done"}),
            transitions={"active": frozenset({"done"}), "done": frozenset()},
        )
        h = WorkerHandle(worker_id="T1", domain="test", current_state="active")
        assert h.is_active(spec) is True

    def test_is_active_terminal(self) -> None:
        spec = StateTransitionSpec(
            domain="test",
            states=frozenset({"active", "done"}),
            terminal_states=frozenset({"done"}),
            transitions={"active": frozenset({"done"}), "done": frozenset()},
        )
        h = WorkerHandle(worker_id="T1", domain="test", current_state="done")
        assert h.is_active(spec) is False

    def test_to_dict_includes_all_fields(self) -> None:
        h = self._handle("running")
        d = h.to_dict()
        assert d["worker_id"] == "T1"
        assert d["domain"] == "test"
        assert d["current_state"] == "running"
        assert d["task_id"] == "t-001"

    def test_metadata_default_empty(self) -> None:
        h = WorkerHandle(worker_id="T1", domain="test", current_state="idle")
        assert h.metadata == {}

    def test_metadata_custom(self) -> None:
        h = WorkerHandle(worker_id="T1", domain="test", current_state="idle",
                         metadata={"priority": "high"})
        assert h.metadata["priority"] == "high"


# ---------------------------------------------------------------------------
# 3. ManagerProtocol domain seam — stub domain
# ---------------------------------------------------------------------------

class _StubManager:
    """A minimal domain implementing ManagerProtocol — no DB dependency."""
    def __init__(self) -> None:
        self._workers: Dict[str, WorkerHandle] = {}

    def allocate_worker(self, worker_id: str, task_id: str) -> WorkerHandle:
        h = WorkerHandle(worker_id=worker_id, domain="stub",
                         current_state="initializing", task_id=task_id)
        self._workers[worker_id] = h
        return h

    def advance_worker(self, worker_id: str, to_state: str) -> WorkerHandle:
        h = self._workers[worker_id]
        h.current_state = to_state
        return h

    def release_worker(self, worker_id: str) -> None:
        self._workers.pop(worker_id, None)

    def query_worker(self, worker_id: str) -> Optional[WorkerHandle]:
        return self._workers.get(worker_id)


class TestManagerProtocolSeam:

    def test_stub_satisfies_manager_protocol(self) -> None:
        assert isinstance(_StubManager(), ManagerProtocol)

    def test_allocate_returns_handle_in_initial_state(self) -> None:
        mgr = _StubManager()
        h = mgr.allocate_worker("T1", "task-1")
        assert h.worker_id == "T1"
        assert h.task_id == "task-1"
        assert h.current_state == "initializing"

    def test_advance_updates_state(self) -> None:
        mgr = _StubManager()
        mgr.allocate_worker("T1", "task-1")
        h = mgr.advance_worker("T1", "working")
        assert h.current_state == "working"

    def test_release_removes_worker(self) -> None:
        mgr = _StubManager()
        mgr.allocate_worker("T1", "task-1")
        mgr.release_worker("T1")
        assert mgr.query_worker("T1") is None

    def test_query_returns_current_handle(self) -> None:
        mgr = _StubManager()
        mgr.allocate_worker("T1", "task-1")
        h = mgr.query_worker("T1")
        assert h is not None
        assert h.worker_id == "T1"

    def test_query_returns_none_when_not_allocated(self) -> None:
        mgr = _StubManager()
        assert mgr.query_worker("T99") is None


# ---------------------------------------------------------------------------
# 4. coding_lifecycle_spec — fidelity to WorkerStateManager
# ---------------------------------------------------------------------------

class TestCodingLifecycleSpec:

    def setup_method(self) -> None:
        self.spec = coding_lifecycle_spec()

    def test_spec_domain_is_coding(self) -> None:
        assert self.spec.domain == "coding"

    def test_initializing_is_non_terminal(self) -> None:
        assert not self.spec.is_terminal("initializing")

    def test_exited_clean_is_terminal(self) -> None:
        assert self.spec.is_terminal("exited_clean")

    def test_exited_bad_is_terminal(self) -> None:
        assert self.spec.is_terminal("exited_bad")

    def test_resume_unsafe_is_terminal(self) -> None:
        assert self.spec.is_terminal("resume_unsafe")

    def test_initializing_to_working_valid(self) -> None:
        self.spec.validate_transition("initializing", "working")

    def test_working_to_stalled_valid(self) -> None:
        self.spec.validate_transition("working", "stalled")

    def test_stalled_to_working_valid(self) -> None:
        self.spec.validate_transition("stalled", "working")

    def test_exited_clean_has_no_transitions(self) -> None:
        assert self.spec.reachable_from("exited_clean") == frozenset()

    def test_invalid_coding_transition_rejected(self) -> None:
        with pytest.raises(ValueError, match="not permitted"):
            self.spec.validate_transition("exited_clean", "working")

    def test_spec_matches_worker_state_manager_states(self) -> None:
        """Coding spec must include all states from WorkerStateManager."""
        from worker_state_manager import WORKER_STATES, WORKER_TRANSITIONS
        assert self.spec.states == WORKER_STATES
        for state, allowed in WORKER_TRANSITIONS.items():
            assert self.spec.transitions[state] == allowed, (
                f"Mismatch for state {state!r}")

    def test_terminal_states_match_worker_state_manager(self) -> None:
        from worker_state_manager import TERMINAL_WORKER_STATES
        assert self.spec.terminal_states == TERMINAL_WORKER_STATES


# ---------------------------------------------------------------------------
# 5. CodingManagerAdapter — compatibility bridge (requires SQLite)
# ---------------------------------------------------------------------------

class TestCodingManagerAdapter:

    def setup_method(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def _seed_lease(self, terminal_id: str, dispatch_id: str) -> None:
        """Seed terminal_leases + dispatch so WorkerStateManager FK constraints pass."""
        from runtime_coordination import (acquire_lease, get_connection,
                                          init_schema, register_dispatch)
        init_schema(self._tmpdir)
        with get_connection(self._tmpdir) as conn:
            register_dispatch(conn, dispatch_id=dispatch_id,
                              terminal_id=terminal_id, track="B")
            acquire_lease(conn, terminal_id=terminal_id, dispatch_id=dispatch_id)
            conn.commit()

    def _make_adapter(self) -> CodingManagerAdapter:
        from worker_state_manager import WorkerStateManager
        mgr = WorkerStateManager(self._tmpdir)
        return CodingManagerAdapter(mgr)

    def test_adapter_satisfies_manager_protocol(self) -> None:
        assert isinstance(self._make_adapter(), ManagerProtocol)

    def test_allocate_returns_coding_domain_handle(self) -> None:
        self._seed_lease("T1", "d-001")
        adapter = self._make_adapter()
        h = adapter.allocate_worker("T1", "d-001")
        assert h.domain == "coding"
        assert h.worker_id == "T1"
        assert h.task_id == "d-001"
        assert h.current_state == "initializing"

    def test_advance_updates_state(self) -> None:
        self._seed_lease("T1", "d-001")
        adapter = self._make_adapter()
        adapter.allocate_worker("T1", "d-001")
        h = adapter.advance_worker("T1", "working")
        assert h.current_state == "working"

    def test_advance_invalid_transition_raises(self) -> None:
        self._seed_lease("T1", "d-001")
        adapter = self._make_adapter()
        adapter.allocate_worker("T1", "d-001")
        adapter.advance_worker("T1", "working")
        adapter.advance_worker("T1", "exited_clean")
        with pytest.raises(Exception):  # InvalidWorkerTransitionError
            adapter.advance_worker("T1", "working")

    def test_release_removes_worker(self) -> None:
        self._seed_lease("T1", "d-001")
        adapter = self._make_adapter()
        adapter.allocate_worker("T1", "d-001")
        adapter.release_worker("T1")
        assert adapter.query_worker("T1") is None

    def test_query_returns_handle_after_allocate(self) -> None:
        self._seed_lease("T1", "d-001")
        adapter = self._make_adapter()
        adapter.allocate_worker("T1", "d-001")
        h = adapter.query_worker("T1")
        assert h is not None
        assert h.current_state == "initializing"

    def test_query_returns_none_before_allocate(self) -> None:
        adapter = self._make_adapter()
        assert adapter.query_worker("T99") is None

    def test_full_coding_lifecycle_via_substrate(self) -> None:
        """Full lifecycle: init → working → exited_clean → released."""
        self._seed_lease("T2", "d-full")
        adapter = self._make_adapter()
        h = adapter.allocate_worker("T2", "d-full")
        assert h.current_state == "initializing"
        h = adapter.advance_worker("T2", "working")
        assert h.current_state == "working"
        h = adapter.advance_worker("T2", "exited_clean")
        assert h.current_state == "exited_clean"
        adapter.release_worker("T2")
        assert adapter.query_worker("T2") is None


# ---------------------------------------------------------------------------
# 6. Custom domain seam — future domain integration
# ---------------------------------------------------------------------------

class _ContentWorkerManager:
    """Stub for a hypothetical 'content' domain using the substrate seam."""
    def __init__(self) -> None:
        self._spec = StateTransitionSpec(
            domain="content",
            states=frozenset({"drafting", "reviewing", "published", "rejected"}),
            terminal_states=frozenset({"published", "rejected"}),
            transitions={
                "drafting":  frozenset({"reviewing", "rejected"}),
                "reviewing": frozenset({"published", "rejected", "drafting"}),
                "published": frozenset(),
                "rejected":  frozenset(),
            },
        )
        self._workers: Dict[str, WorkerHandle] = {}

    def allocate_worker(self, worker_id: str, task_id: str) -> WorkerHandle:
        h = WorkerHandle(worker_id=worker_id, domain="content",
                         current_state="drafting", task_id=task_id)
        self._workers[worker_id] = h
        return h

    def advance_worker(self, worker_id: str, to_state: str) -> WorkerHandle:
        h = self._workers[worker_id]
        self._spec.validate_transition(h.current_state, to_state)
        h.current_state = to_state
        return h

    def release_worker(self, worker_id: str) -> None:
        self._workers.pop(worker_id, None)

    def query_worker(self, worker_id: str) -> Optional[WorkerHandle]:
        return self._workers.get(worker_id)


class TestCustomDomainSeam:

    def test_content_domain_satisfies_manager_protocol(self) -> None:
        assert isinstance(_ContentWorkerManager(), ManagerProtocol)

    def test_content_domain_has_different_states(self) -> None:
        mgr = _ContentWorkerManager()
        h = mgr.allocate_worker("agent-1", "story-001")
        assert h.current_state == "drafting"
        assert h.domain == "content"

    def test_content_domain_transition(self) -> None:
        mgr = _ContentWorkerManager()
        mgr.allocate_worker("agent-1", "story-001")
        h = mgr.advance_worker("agent-1", "reviewing")
        assert h.current_state == "reviewing"

    def test_content_domain_invalid_transition(self) -> None:
        mgr = _ContentWorkerManager()
        mgr.allocate_worker("agent-1", "story-001")
        with pytest.raises(ValueError, match="not permitted"):
            mgr.advance_worker("agent-1", "published")  # must go through reviewing

    def test_substrate_treats_both_domains_uniformly(self) -> None:
        """Substrate code that accepts ManagerProtocol works for any domain."""
        def _run_lifecycle(manager: ManagerProtocol, worker_id: str, task_id: str,
                           active_state: str, terminal_state: str) -> Optional[WorkerHandle]:
            manager.allocate_worker(worker_id, task_id)
            manager.advance_worker(worker_id, active_state)
            manager.advance_worker(worker_id, terminal_state)
            result = manager.query_worker(worker_id)
            manager.release_worker(worker_id)
            return result

        content_mgr = _ContentWorkerManager()
        h = _run_lifecycle(content_mgr, "agent-1", "story-1", "reviewing", "published")
        assert h is not None
        assert h.current_state == "published"
        assert h.domain == "content"
