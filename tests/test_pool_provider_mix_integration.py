"""Integration tests for provider-mix allocation via PoolManager mock-spawn."""

from __future__ import annotations

import sys
import time
import tempfile
import sqlite3
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from pool_decision_engine import Membership, PoolConfig, PoolDecision, PoolState
from pool_manager import PoolManager, SpawnResult, ExecResult
from pool_provider_allocator import allocate_for_scale_up, select_for_scale_down


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_membership(mid: str, provider: str, t: float, status: str = "active") -> Membership:
    return Membership(
        membership_id=mid,
        terminal_id=f"T-{mid}",
        provider=provider,
        pool_role="backend-developer",
        status=status,
        joined_at=t,
    )


def _spawn_ok(project_id, pool_id, terminal_id, provider, role) -> SpawnResult:
    return SpawnResult(terminal_id=terminal_id, success=True)


def _spawn_fail(project_id, pool_id, terminal_id, provider, role) -> SpawnResult:
    return SpawnResult(terminal_id=terminal_id, success=False, error="mock failure")


# ---------------------------------------------------------------------------
# Pure allocator integration — no DB needed
# ---------------------------------------------------------------------------

class TestProviderMixClaudeCodex:
    def test_target_4_gives_two_each(self):
        """provider_mix=["claude","codex"] target=4 → 2 claude + 2 codex spawned."""
        result = allocate_for_scale_up([], ["claude", "codex"], delta=4)
        counts = {p: result.providers.count(p) for p in set(result.providers)}
        assert counts.get("claude", 0) == 2
        assert counts.get("codex", 0) == 2

    def test_target_6_three_each(self):
        result = allocate_for_scale_up([], ["claude", "codex"], delta=6)
        counts = {p: result.providers.count(p) for p in set(result.providers)}
        assert counts.get("claude", 0) == 3
        assert counts.get("codex", 0) == 3

    def test_target_1_uses_first_provider(self):
        result = allocate_for_scale_up([], ["claude", "codex"], delta=1)
        assert len(result.providers) == 1
        assert result.providers[0] in ("claude", "codex")

    def test_scale_up_then_scale_down_net_zero(self):
        """Scale up to 4, then down by 2: net 2 workers."""
        up = allocate_for_scale_up([], ["claude", "codex"], delta=4)
        # Simulate members from those spawns
        members = [
            _make_membership(f"mid_{i}", p, float(i))
            for i, p in enumerate(up.providers)
        ]
        down_ids = select_for_scale_down(members, ["claude", "codex"], -2)
        assert len(down_ids) == 2


class TestProviderMixLitellmDeepseek:
    def test_claude_litellm_deepseek_two_each(self):
        """Mock-spawn with 1 claude + 1 litellm:deepseek works."""
        result = allocate_for_scale_up([], ["claude", "litellm:deepseek"], delta=2)
        assert len(result.providers) == 2
        assert "claude" in result.providers
        assert "litellm:deepseek" in result.providers

    def test_litellm_deepseek_heavy_mix(self):
        """["litellm:deepseek","litellm:deepseek","claude"] → 2:1 ratio."""
        result = allocate_for_scale_up(
            [], ["litellm:deepseek", "litellm:deepseek", "claude"], delta=3
        )
        counts = {p: result.providers.count(p) for p in set(result.providers)}
        assert counts.get("litellm:deepseek", 0) == 2
        assert counts.get("claude", 0) == 1

    def test_providers_in_result_are_valid_strings(self):
        result = allocate_for_scale_up([], ["claude", "litellm:deepseek"], delta=4)
        for p in result.providers:
            assert isinstance(p, str)
            assert len(p) > 0


class TestProviderMixWithExistingMembers:
    def test_rebalances_toward_target(self):
        """With 2 existing claude workers and mix ["claude","codex"], next spawn fills codex."""
        existing = [
            _make_membership("c1", "claude", 1.0),
            _make_membership("c2", "claude", 2.0),
        ]
        result = allocate_for_scale_up(existing, ["claude", "codex"], delta=2)
        # new_size=4, target={claude:2,codex:2}; current={claude:2}, need 2 codex
        assert result.providers.count("codex") == 2
        assert result.providers.count("claude") == 0

    def test_immutable_provider_binding(self):
        """Existing members keep their provider after scale_up call."""
        m1 = _make_membership("m1", "claude", 1.0)
        m2 = _make_membership("m2", "codex", 2.0)
        original_providers = [m1.provider, m2.provider]
        _ = allocate_for_scale_up([m1, m2], ["claude", "codex"], delta=2)
        # Immutability: dataclasses are frozen; providers unchanged
        assert m1.provider == original_providers[0]
        assert m2.provider == original_providers[1]

    def test_mixed_status_members_only_active_counted(self):
        """Reaped members don't count toward current_shares."""
        active = _make_membership("a1", "claude", 1.0, status="active")
        reaped = _make_membership("r1", "claude", 0.5, status="reaped")
        # Only 1 active claude; new_size=1+2=3, target={claude:2,codex:1}
        result = allocate_for_scale_up([active, reaped], ["claude", "codex"], delta=2)
        # pending starts as {claude:1}; step1 gap=1:1 tie -> claude; step2 codex gap=1 -> codex
        assert len(result.providers) == 2


class TestScaleDownProviderAware:
    def test_release_oldest_of_highest_excess_provider(self):
        """scale_down releases oldest worker of highest-excess provider."""
        m1 = _make_membership("m1", "claude", 1.0)
        m2 = _make_membership("m2", "claude", 2.0)
        m3 = _make_membership("m3", "codex", 3.0)
        # mix=["claude","codex"], current=2+1, new_size=2, target={claude:1,codex:1}
        # excess: {claude:1,codex:0} -> release oldest claude = m1
        result = select_for_scale_down([m1, m2, m3], ["claude", "codex"], -1)
        assert result == ["m1"]

    def test_scale_down_two_releases_correct_pair(self):
        """Scale down by 2 releases 2 oldest of overrepresented provider."""
        members = [
            _make_membership("c1", "claude", 1.0),
            _make_membership("c2", "claude", 2.0),
            _make_membership("c3", "claude", 3.0),
            _make_membership("x1", "codex", 4.0),
        ]
        # mix=["claude","codex"], current=3+1, new_size=2, target={claude:1,codex:1}
        # excess: {claude:2,codex:0} -> release c1, then c2
        result = select_for_scale_down(members, ["claude", "codex"], -2)
        assert set(result) == {"c1", "c2"}

    def test_empty_provider_mix_scale_down_oldest_first(self):
        """Empty mix falls back to global oldest-first."""
        members = [
            _make_membership("old", "claude", 1.0),
            _make_membership("new", "codex", 10.0),
        ]
        result = select_for_scale_down(members, [], -1)
        assert result == ["old"]


class TestPoolManagerMockSpawn:
    def _build_manager_with_db(self) -> tuple[PoolManager, Path]:
        """Build a PoolManager backed by a real in-memory SQLite DB."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(tmp.name)
        tmp.close()

        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE pool_config (
                project_id TEXT,
                pool_id TEXT,
                min_workers INTEGER,
                max_workers INTEGER,
                scale_policy TEXT,
                provider_mix_json TEXT,
                cooldown_seconds REAL,
                PRIMARY KEY (project_id, pool_id)
            )
        """)
        conn.execute("""
            CREATE TABLE worker_pools (
                project_id TEXT,
                pool_id TEXT,
                current_size INTEGER DEFAULT 0,
                last_scaled_at TEXT,
                last_decision_json TEXT,
                last_scale_action TEXT,
                PRIMARY KEY (project_id, pool_id)
            )
        """)
        conn.execute("""
            CREATE TABLE worker_pool_membership (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id TEXT,
                project_id TEXT,
                pool_id TEXT,
                provider TEXT,
                role TEXT,
                joined_at TEXT,
                released_at TEXT,
                release_reason TEXT,
                metadata_json TEXT DEFAULT '{}'
            )
        """)
        conn.execute("""
            CREATE TABLE terminal_leases (
                terminal_id TEXT,
                project_id TEXT,
                last_heartbeat_at TEXT,
                PRIMARY KEY (terminal_id, project_id)
            )
        """)
        conn.execute(
            "INSERT INTO pool_config VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("proj-test", "default", 0, 4, "queue_depth_v1", '["claude","codex"]', 0.0),
        )
        conn.execute(
            "INSERT INTO worker_pools VALUES (?, ?, ?, ?, ?, ?)",
            ("proj-test", "default", 0, None, None, None),
        )
        conn.commit()
        conn.close()

        mgr = PoolManager(
            project_id="proj-test",
            pool_id="default",
            db_path=db_path,
            spawn_fn=_spawn_ok,
        )
        return mgr, db_path

    def test_execute_scale_up_spawns_correct_providers(self):
        mgr, db_path = self._build_manager_with_db()
        decision = PoolDecision(action="scale_up", delta=4, reason="test")
        result = mgr.execute(decision)
        assert len(result.errors) == 0
        assert len(result.spawned) == 4

        # Verify provider distribution via repo
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT provider FROM worker_pool_membership WHERE project_id=? AND released_at IS NULL",
            ("proj-test",),
        ).fetchall()
        conn.close()
        providers = [r[0] for r in rows]
        assert providers.count("claude") == 2
        assert providers.count("codex") == 2

    def test_execute_scale_up_with_spawn_failure(self):
        mgr, db_path = self._build_manager_with_db()
        mgr._spawn_fn = _spawn_fail
        decision = PoolDecision(action="scale_up", delta=2, reason="test")
        result = mgr.execute(decision)
        assert len(result.spawned) == 0
        assert len(result.errors) == 2

    def test_noop_decision_no_spawns(self):
        mgr, db_path = self._build_manager_with_db()
        decision = PoolDecision(action="noop", reason="at target")
        result = mgr.execute(decision)
        assert result.spawned == []
        assert result.reaped == []
        assert result.errors == []
