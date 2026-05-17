"""test_pool_blockers_fix.py — Regression tests for Wave 6 pool cluster 3-blocker fix.

Covers:
1. _spawn_via_provider_dispatch is real subprocess.Popen (not a stub)
2. get_config reads cost_ceiling_usd and heartbeat_stale_seconds from DB
3. Single heartbeat threshold constant (POOL_HEARTBEAT_STALE_SECONDS) used everywhere
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from pool_decision_engine import POOL_HEARTBEAT_STALE_SECONDS, PoolConfig  # noqa: E402
from pool_reaper import ReapConfig  # noqa: E402
from pool_state_repo import PoolStateRepository  # noqa: E402
from pool_state_fixtures import create_test_db_file  # noqa: E402


# ---------------------------------------------------------------------------
# Blocker 1: spawn is a real subprocess.Popen call, not a stub
# ---------------------------------------------------------------------------

class TestSpawnIsReal:
    def test_spawn_calls_popen(self):
        from pool_manager import _spawn_via_provider_dispatch, SpawnResult

        fake_worktree = Path("/tmp/fake-worktree")
        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with (
            patch("pool_worktree_manager.create_worker_worktree", return_value=fake_worktree),
            patch("pool_manager.subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("pool_manager.os.kill"),
        ):
            result = _spawn_via_provider_dispatch(
                "proj-1", "default", "T-spawn-1", "claude", "backend-developer",
            )

        assert isinstance(result, SpawnResult)
        assert result.success is True
        assert result.pid == 99999
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args
        assert call_kwargs.kwargs["cwd"] == str(fake_worktree)
        cmd = call_kwargs.args[0]
        assert sys.executable in cmd[0]
        assert "scripts.lib.subprocess_dispatch" in cmd

    def test_spawn_captures_worktree_failure(self):
        from pool_manager import _spawn_via_provider_dispatch

        with patch(
            "pool_worktree_manager.create_worker_worktree",
            side_effect=RuntimeError("no worktree"),
        ):
            result = _spawn_via_provider_dispatch(
                "proj-1", "default", "T-fail", "claude", "backend-developer",
            )

        assert result.success is False
        assert "worktree creation failed" in result.error

    def test_spawn_captures_popen_failure(self):
        from pool_manager import _spawn_via_provider_dispatch

        with (
            patch("pool_worktree_manager.create_worker_worktree", return_value=Path("/tmp/wt")),
            patch(
                "pool_manager.subprocess.Popen",
                side_effect=OSError("exec failed"),
            ),
        ):
            result = _spawn_via_provider_dispatch(
                "proj-1", "default", "T-oserr", "claude", "backend-developer",
            )

        assert result.success is False
        assert "Popen failed" in result.error


# ---------------------------------------------------------------------------
# Blocker 2: get_config reads cost_ceiling_usd and heartbeat_stale_seconds
# ---------------------------------------------------------------------------

class TestGetConfigFields:
    def test_reads_cost_ceiling_usd(self, tmp_path):
        (tmp_path / "state").mkdir()
        db = create_test_db_file(
            tmp_path / "state" / "runtime_coordination.db",
            cost_ceiling_usd=12.5,
        )
        repo = PoolStateRepository(db, "vnx-dev")
        config = repo.get_config("default")

        assert config is not None
        assert config.cost_ceiling_usd == 12.5

    def test_reads_heartbeat_stale_seconds(self, tmp_path):
        (tmp_path / "state").mkdir()
        db = create_test_db_file(
            tmp_path / "state" / "runtime_coordination.db",
            heartbeat_stale_seconds=90.0,
        )
        repo = PoolStateRepository(db, "vnx-dev")
        config = repo.get_config("default")

        assert config is not None
        assert config.heartbeat_stale_seconds == 90.0

    def test_cost_ceiling_defaults_to_none(self, tmp_path):
        (tmp_path / "state").mkdir()
        db = create_test_db_file(tmp_path / "state" / "runtime_coordination.db")
        repo = PoolStateRepository(db, "vnx-dev")
        config = repo.get_config("default")

        assert config is not None
        assert config.cost_ceiling_usd is None

    def test_heartbeat_defaults_to_constant(self, tmp_path):
        (tmp_path / "state").mkdir()
        db = create_test_db_file(tmp_path / "state" / "runtime_coordination.db")
        repo = PoolStateRepository(db, "vnx-dev")
        config = repo.get_config("default")

        assert config is not None
        assert config.heartbeat_stale_seconds == POOL_HEARTBEAT_STALE_SECONDS

    def test_both_fields_round_trip(self, tmp_path):
        (tmp_path / "state").mkdir()
        db = create_test_db_file(
            tmp_path / "state" / "runtime_coordination.db",
            cost_ceiling_usd=25.0,
            heartbeat_stale_seconds=120.0,
        )
        repo = PoolStateRepository(db, "vnx-dev")
        config = repo.get_config("default")

        assert config is not None
        assert config.cost_ceiling_usd == 25.0
        assert config.heartbeat_stale_seconds == 120.0


# ---------------------------------------------------------------------------
# Blocker 3: single heartbeat threshold constant
# ---------------------------------------------------------------------------

class TestHeartbeatThresholdUnified:
    def test_constant_is_180(self):
        assert POOL_HEARTBEAT_STALE_SECONDS == 180.0

    def test_pool_config_default_matches_constant(self):
        config = PoolConfig(
            pool_id="test",
            min_workers=1,
            max_workers=4,
            scaling_policy="fixed",
            provider_mix=["claude"],
        )
        assert config.heartbeat_stale_seconds == POOL_HEARTBEAT_STALE_SECONDS

    def test_reap_config_default_matches_constant(self):
        reap_cfg = ReapConfig()
        assert reap_cfg.heartbeat_stale_threshold_s == POOL_HEARTBEAT_STALE_SECONDS

    def test_both_defaults_are_equal(self):
        config = PoolConfig(
            pool_id="test",
            min_workers=1,
            max_workers=4,
            scaling_policy="fixed",
            provider_mix=["claude"],
        )
        reap_cfg = ReapConfig()
        assert config.heartbeat_stale_seconds == reap_cfg.heartbeat_stale_threshold_s


# ---------------------------------------------------------------------------
# Blocker 2 + 3 combined: get_config fallback on old schema without columns
# ---------------------------------------------------------------------------

class TestGetConfigOldSchema:
    def test_fallback_on_missing_columns(self, tmp_path):
        """get_config gracefully falls back when DB lacks cost/heartbeat columns."""
        import sqlite3

        (tmp_path / "state").mkdir()
        db_path = tmp_path / "state" / "runtime_coordination.db"

        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runtime_schema_version (
                version INTEGER PRIMARY KEY, description TEXT,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE IF NOT EXISTS pool_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                pool_id TEXT NOT NULL DEFAULT 'default',
                min_workers INTEGER NOT NULL DEFAULT 1,
                max_workers INTEGER NOT NULL DEFAULT 6,
                target_workers INTEGER NOT NULL DEFAULT 3,
                provider_mix_json TEXT NOT NULL DEFAULT '["claude"]',
                scale_policy TEXT NOT NULL DEFAULT 'queue_depth_v1',
                cooldown_seconds INTEGER NOT NULL DEFAULT 120,
                created_at TEXT, updated_at TEXT,
                UNIQUE(project_id, pool_id)
            );
            CREATE TABLE IF NOT EXISTS worker_pools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL, pool_id TEXT NOT NULL DEFAULT 'default',
                state TEXT NOT NULL DEFAULT 'idle',
                current_size INTEGER NOT NULL DEFAULT 0,
                target_size INTEGER NOT NULL DEFAULT 0,
                healthy_count INTEGER NOT NULL DEFAULT 0,
                stuck_count INTEGER NOT NULL DEFAULT 0,
                last_scaled_at TEXT, last_scale_action TEXT,
                last_decision_json TEXT DEFAULT '{}',
                metadata_json TEXT DEFAULT '{}',
                UNIQUE(project_id, pool_id)
            );
            INSERT OR IGNORE INTO pool_config (project_id, pool_id, min_workers, max_workers, target_workers)
            VALUES ('vnx-dev', 'default', 1, 4, 2);
            INSERT OR IGNORE INTO worker_pools (project_id, pool_id, state, current_size, target_size)
            VALUES ('vnx-dev', 'default', 'idle', 0, 2);
        """)
        conn.commit()
        conn.close()

        repo = PoolStateRepository(db_path, "vnx-dev")
        config = repo.get_config("default")

        assert config is not None
        assert config.cost_ceiling_usd is None
        assert config.heartbeat_stale_seconds == POOL_HEARTBEAT_STALE_SECONDS
