"""pool_manager.py — Elastic worker pool manager.

Called from T0-tick. Reads state via PoolStateRepository, computes decision via
pool_decision_engine.decide(), executes spawns/reaps, records the decision.

Wave 6 PR-6.3 — ADR-018 elastic worker pool.
Wave 6 PR-6.6 — Health monitoring + dead-worker reap (tick = reap → decide → execute).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# Ensure scripts/lib is importable regardless of invocation path.
_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from pool_decision_engine import (  # noqa: E402
    Membership,
    PoolConfig,
    PoolDecision,
    PoolState,
    decide,
)
from pool_provider_allocator import (  # noqa: E402
    allocate_for_scale_up,
    select_for_scale_down,
)
from pool_reaper import ReapConfig, ReapTarget, identify_dead_pid_targets, identify_reap_targets  # noqa: E402
from pool_state_repo import PoolStateRepository  # noqa: E402

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Spawn protocol
# ---------------------------------------------------------------------------

@dataclass
class SpawnResult:
    terminal_id: str
    success: bool
    error: str = ""


SpawnFn = Callable[[str, str, str, str, str], SpawnResult]


def _default_db_path(project_id: str) -> Path:
    """Resolve default DB path under VNX_STATE_DIR (via vnx_paths.resolve_state_dir)."""
    try:
        from project_root import resolve_project_root  # type: ignore
        root = resolve_project_root(__file__)
        return root / ".vnx-data" / "state" / "runtime_coordination.db"
    except Exception:
        return Path.cwd() / ".vnx-data" / "state" / "runtime_coordination.db"


def _spawn_via_provider_dispatch(
    project_id: str,
    pool_id: str,
    terminal_id: str,
    provider: str,
    role: str,
) -> SpawnResult:
    """Delegate to Wave 4.6 provider spawn handlers.

    Provider-mix integration is completed in PR-6.5. For PR-6.3 this
    records the spawn intent and returns success so that PoolManager can
    insert the membership row. Actual subprocess spawning is wired in PR-6.5.
    """
    log.info(
        "spawn: project=%s pool=%s terminal=%s provider=%s role=%s",
        project_id,
        pool_id,
        terminal_id,
        provider,
        role,
    )
    return SpawnResult(terminal_id=terminal_id, success=True)


# ---------------------------------------------------------------------------
# ExecResult
# ---------------------------------------------------------------------------

@dataclass
class ExecResult:
    decision: PoolDecision
    spawned: List[str] = field(default_factory=list)   # terminal_ids spawned
    reaped: List[str] = field(default_factory=list)    # membership_ids reaped
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PoolManager
# ---------------------------------------------------------------------------

class PoolManager:
    """Orchestrator: read -> decide -> execute -> record.

    Instantiate with project_id and pool_id. Call tick() from T0.

    Args:
        project_id:  VNX project identifier.
        pool_id:     Pool name (default "default").
        db_path:     Path to runtime_coordination.db.
                     Defaults to <state-dir>/runtime_coordination.db where <state-dir> = VNX_STATE_DIR or vnx_paths default.
        spawn_fn:    Injected for testability. Signature matches SpawnFn.
    """

    def __init__(
        self,
        project_id: str,
        pool_id: str = "default",
        db_path: Optional[Path] = None,
        *,
        spawn_fn: Optional[SpawnFn] = None,
    ) -> None:
        self.project_id = project_id
        self.pool_id = pool_id
        db = db_path or _default_db_path(project_id)
        self.repo = PoolStateRepository(db, project_id)
        self._spawn_fn: SpawnFn = spawn_fn or _spawn_via_provider_dispatch
        self.reap_config = ReapConfig()  # use defaults; operator can override

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_state(self) -> Tuple[PoolConfig, PoolState, List[Membership]]:
        """Load config + state + members for this pool."""
        now = time.time()
        config = self.repo.get_config(self.pool_id)
        if config is None:
            raise RuntimeError(
                f"No pool_config row for project={self.project_id} pool={self.pool_id}. "
                "Run migration 0020 and bootstrap first."
            )
        state = self.repo.get_state(self.pool_id, now)
        members = self.repo.list_members(self.pool_id)
        return config, state, members

    def decide(self) -> PoolDecision:
        """Read state and return a pure decision. No side effects."""
        config, state, members = self.load_state()
        return decide(config, state, members)

    def execute(self, decision: PoolDecision) -> ExecResult:
        """Apply decision: spawn new workers or reap stale ones.

        Per-target outcome tracking: if 1 of 3 spawns fails, the other 2
        still succeed. Membership rows are only created on successful spawn.
        """
        result = ExecResult(decision=decision)
        now = time.time()

        if decision.action == "scale_up":
            result = self._execute_scale_up(decision, now)
        elif decision.action == "scale_down":
            result = self._execute_scale_down(decision, now)
        elif decision.action == "reap":
            result = self._execute_reap(decision, now)
        else:
            # noop — nothing to do
            pass

        return result

    def reap_dead(self) -> List[ReapTarget]:
        """Identify + kill + release stuck/stale workers.

        Two detection paths run in sequence:
        1. PID validation — os.kill(pid, 0) probe; dead process = immediate reap
        2. Heartbeat staleness — existing threshold-based detection

        Returns list of successfully reaped targets for audit/observability.
        Kill failures do not block membership release — process may already be gone.
        """
        _config, _state, members = self.load_state()
        now = time.time()

        pid_targets = identify_dead_pid_targets(members)
        heartbeat_targets = identify_reap_targets(members, now, self.reap_config)

        seen_ids: set[str] = set()
        targets: List[ReapTarget] = []
        for t in pid_targets + heartbeat_targets:
            if t.membership_id not in seen_ids:
                seen_ids.add(t.membership_id)
                targets.append(t)

        reaped: List[ReapTarget] = []

        for target in targets:
            try:
                self._kill_subprocess(target.terminal_id, target.pid)
            except Exception as exc:
                log.warning("reap: kill failed for %s: %s", target.terminal_id, exc)

            try:
                self.repo.mark_member_reaped(target.membership_id, target.reason, now)
                self.repo._emit_ledger("pool.worker.dead_reaped", {
                    "pool_id": self.pool_id,
                    "membership_id": target.membership_id,
                    "terminal_id": target.terminal_id,
                    "actor": "pool_reaper",
                    "reason": target.reason,
                    "now": now,
                })
                reaped.append(target)
            except Exception as exc:
                log.error(
                    "reap: membership release failed for %s: %s",
                    target.membership_id,
                    exc,
                )

        return reaped

    def _kill_subprocess(self, terminal_id: str, pid: Optional[int]) -> None:
        """Two-step SIGTERM → 5s wait → SIGKILL. pid <= 0 is never killed."""
        if pid is None or pid <= 0:
            log.warning("reap: no valid pid for %s; skipping kill", terminal_id)
            return

        try:
            from cleanup_worker_exit import terminate_subprocess  # type: ignore[attr-defined]
            terminate_subprocess(pid, terminal_id=terminal_id, timeout_s=5.0)
            return
        except ImportError:
            pass

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return  # Already dead

        time.sleep(5.0)

        try:
            os.kill(pid, 0)  # Probe: raises ProcessLookupError if already dead
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # Exited cleanly after SIGTERM

    def tick(self) -> ExecResult:
        """tick = reap → decide → execute.

        Reap runs FIRST so decide() sees post-reap pool state.
        """
        reaped_targets = self.reap_dead()

        decision = self.decide()
        result = self.execute(decision)

        result_with_reap = ExecResult(
            decision=result.decision,
            spawned=result.spawned,
            reaped=result.reaped + [t.membership_id for t in reaped_targets],
            errors=result.errors,
        )

        now = time.time()
        self.repo.record_decision(self.pool_id, decision, now)

        if result_with_reap.spawned or result_with_reap.reaped:
            self.repo.update_last_scaled_at(self.pool_id, now)
            current_size = self.repo.get_current_size(self.pool_id)
            self.repo.update_pool_size(self.pool_id, current_size)

        log.info(
            "tick: pool=%s action=%s spawned=%d reaped=%d errors=%d reason=%s",
            self.pool_id,
            decision.action,
            len(result_with_reap.spawned),
            len(result_with_reap.reaped),
            len(result_with_reap.errors),
            decision.reason,
        )
        return result_with_reap

    # ------------------------------------------------------------------
    # Private execution helpers
    # ------------------------------------------------------------------

    def _next_terminal_id(self) -> str:
        current_size = self.repo.get_current_size(self.pool_id)
        slot = current_size + 1
        prefix = self.project_id.split("-")[0] if "-" in self.project_id else self.project_id
        return f"{prefix[:3].upper()}-{slot}"

    def _provider_for_slot(self, config: PoolConfig, slot_index: int) -> str:
        mix = config.provider_mix
        if not mix:
            return "claude"
        return mix[slot_index % len(mix)]

    def _execute_scale_up(self, decision: PoolDecision, now: float) -> ExecResult:
        result = ExecResult(decision=decision)
        config, _, members = self.load_state()

        allocation = allocate_for_scale_up(
            members=members,
            provider_mix=config.provider_mix,
            delta=abs(decision.delta),
            fallback_provider="claude",
        )

        for i, provider in enumerate(allocation.providers):
            terminal_id = f"{self.project_id}-P{int(now * 1000) % 100000}-{i}"
            role = _resolve_role(config, i)

            try:
                spawn_result = self._spawn_fn(
                    self.project_id,
                    self.pool_id,
                    terminal_id,
                    provider,
                    role,
                )
                if spawn_result.success:
                    self.repo.add_member(
                        self.pool_id, terminal_id, provider, role, now
                    )
                    result.spawned.append(terminal_id)
                    log.info(
                        "scale_up: spawned terminal=%s provider=%s",
                        terminal_id,
                        provider,
                    )
                else:
                    err = f"spawn failed for terminal={terminal_id}: {spawn_result.error}"
                    result.errors.append(err)
                    log.warning("scale_up: %s", err)
            except Exception as exc:
                err = f"spawn exception for terminal={terminal_id}: {exc}"
                result.errors.append(err)
                log.exception("scale_up: unexpected spawn error terminal=%s", terminal_id)

        return result

    def _execute_scale_down(self, decision: PoolDecision, now: float) -> ExecResult:
        result = ExecResult(decision=decision)

        if decision.targets:  # OI-1483: use pre-computed targets from decide()
            membership_ids = list(decision.targets)
        else:
            config, _, members = self.load_state()
            membership_ids = select_for_scale_down(
                members=members,
                provider_mix=config.provider_mix,
                delta=decision.delta,
            )

        for membership_id in membership_ids:
            try:
                self.repo.mark_member_reaped(
                    membership_id, "scale_down", now
                )
                result.reaped.append(membership_id)
                log.info("scale_down: reaped membership=%s", membership_id)
            except Exception as exc:
                err = f"reap error for membership={membership_id}: {exc}"
                result.errors.append(err)
                log.exception("scale_down: reap error membership=%s", membership_id)

        return result

    def _execute_reap(self, decision: PoolDecision, now: float) -> ExecResult:
        result = ExecResult(decision=decision)

        for membership_id in decision.targets:
            try:
                self.repo.mark_member_reaped(
                    membership_id, "heartbeat_stale", now
                )
                result.reaped.append(membership_id)
                log.info("reap: reaped stale membership=%s", membership_id)
            except Exception as exc:
                err = f"reap error for membership={membership_id}: {exc}"
                result.errors.append(err)
                log.exception("reap: error membership=%s", membership_id)

        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_role(config: PoolConfig, slot_index: int) -> str:
    """Derive role for a slot from provider_mix naming convention.

    Provider_mix entries like 'claude:backend-developer' carry the role.
    Plain entries like 'claude' get role 'backend-developer' as default.
    Full role resolution via role_mix is implemented in PR-6.5.
    """
    mix = config.provider_mix
    if not mix:
        return "backend-developer"
    entry = mix[slot_index % len(mix)]
    if ":" in entry:
        parts = entry.split(":", 1)
        return parts[1] if parts[1] else "backend-developer"
    return "backend-developer"
