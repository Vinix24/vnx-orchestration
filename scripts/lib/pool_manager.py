"""pool_manager.py — Elastic worker pool manager.

Called from T0-tick. Reads state via PoolStateRepository, computes decision via
pool_decision_engine.decide(), executes spawns/reaps, records the decision.

Wave 6 PR-6.3 — ADR-018 elastic worker pool.
"""

from __future__ import annotations

import logging
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

    def tick(self) -> ExecResult:
        """Full T0-tick cycle: decide + execute + record.

        Called once per T0 tick. Records the decision in the DB and updates
        last_scaled_at only when workers were actually spawned or reaped.
        """
        config, state, members = self.load_state()
        decision = decide(config, state, members)

        result = self.execute(decision)

        now = time.time()
        self.repo.record_decision(self.pool_id, decision, now)

        if result.spawned or result.reaped:
            self.repo.update_last_scaled_at(self.pool_id, now)
            current_size = self.repo.get_current_size(self.pool_id)
            self.repo.update_pool_size(self.pool_id, current_size)

        log.info(
            "tick: pool=%s action=%s spawned=%d reaped=%d errors=%d reason=%s",
            self.pool_id,
            decision.action,
            len(result.spawned),
            len(result.reaped),
            len(result.errors),
            decision.reason,
        )
        return result

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
        config, _, _ = self.load_state()

        for i in range(abs(decision.delta)):
            terminal_id = f"{self.project_id}-P{int(now * 1000) % 100000}-{i}"
            provider = self._provider_for_slot(config, i)
            role = config.provider_mix[0] if config.provider_mix else "backend-developer"
            # role comes from pool config role_mix; provider_mix has providers
            # For PR-6.3, derive role from worker_registry if available
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

        for membership_id in decision.targets:
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
