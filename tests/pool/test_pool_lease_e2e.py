"""test_pool_lease_e2e.py — E2E tests for pool lease + membership lifecycle.

Covers the FK gap that allowed scale_up to succeed without inserting a
terminal_leases row (the FK on worker_pool_membership was missing from the
test fixture schema).

Tests run against a real on-disk SQLite DB with PRAGMA foreign_keys = ON
and a production-accurate schema (including the FK constraint).

Dispatch-ID: 20260529-131408-pool-lease-fix
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from pool_manager import ExecResult, PoolManager, SpawnResult  # noqa: E402
from pool_state_repo import PoolStateRepository  # noqa: E402

# ---------------------------------------------------------------------------
# Production-accurate schema fixture (includes FK on worker_pool_membership)
# ---------------------------------------------------------------------------

_PROD_SCHEMA = """
PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS runtime_schema_version (
    version     INTEGER PRIMARY KEY,
    description TEXT,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
INSERT OR IGNORE INTO runtime_schema_version(version, description)
VALUES (14, 'e2e-prod-schema');

CREATE TABLE IF NOT EXISTS terminal_leases (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id       TEXT    NOT NULL,
    project_id        TEXT    NOT NULL,
    state             TEXT    NOT NULL DEFAULT 'idle',
    lease_token       TEXT    NOT NULL DEFAULT '',
    last_heartbeat_at TEXT,
    worker_pid        INTEGER,
    released_at       TEXT,
    generation        INTEGER NOT NULL DEFAULT 1,
    dispatch_id       TEXT,
    UNIQUE(terminal_id, project_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_terminal_leases_token
    ON terminal_leases(lease_token) WHERE lease_token != '';

CREATE TABLE IF NOT EXISTS dispatches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    project_id  TEXT NOT NULL DEFAULT 'vnx-dev',
    state       TEXT NOT NULL DEFAULT 'queued',
    UNIQUE(dispatch_id, project_id)
);

CREATE TABLE IF NOT EXISTS pool_config (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id              TEXT    NOT NULL,
    pool_id                 TEXT    NOT NULL DEFAULT 'default',
    min_workers             INTEGER NOT NULL DEFAULT 1,
    max_workers             INTEGER NOT NULL DEFAULT 6,
    target_workers          INTEGER NOT NULL DEFAULT 3,
    role_mix_json           TEXT    NOT NULL DEFAULT '["backend-developer"]',
    provider_mix_json       TEXT    NOT NULL DEFAULT '["claude"]',
    scale_policy            TEXT    NOT NULL DEFAULT 'queue_depth_v1',
    cooldown_seconds        INTEGER NOT NULL DEFAULT 120,
    cost_ceiling_usd        REAL,
    heartbeat_stale_seconds REAL    NOT NULL DEFAULT 180,
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(project_id, pool_id),
    CHECK (min_workers >= 0),
    CHECK (max_workers >= min_workers),
    CHECK (target_workers >= min_workers AND target_workers <= max_workers)
);

CREATE TABLE IF NOT EXISTS worker_pools (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          TEXT    NOT NULL,
    pool_id             TEXT    NOT NULL DEFAULT 'default',
    state               TEXT    NOT NULL DEFAULT 'idle',
    current_size        INTEGER NOT NULL DEFAULT 0,
    target_size         INTEGER NOT NULL DEFAULT 0,
    healthy_count       INTEGER NOT NULL DEFAULT 0,
    stuck_count         INTEGER NOT NULL DEFAULT 0,
    last_scaled_at      TEXT,
    last_scale_action   TEXT,
    last_decision_json  TEXT    DEFAULT '{}',
    metadata_json       TEXT    DEFAULT '{}',
    UNIQUE(project_id, pool_id),
    FOREIGN KEY (project_id, pool_id) REFERENCES pool_config(project_id, pool_id)
);

CREATE TABLE IF NOT EXISTS worker_pool_membership (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id      TEXT    NOT NULL,
    project_id       TEXT    NOT NULL,
    pool_id          TEXT    NOT NULL DEFAULT 'default',
    provider         TEXT    NOT NULL,
    role             TEXT    NOT NULL,
    joined_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    released_at      TEXT,
    release_reason   TEXT,
    spawn_generation INTEGER NOT NULL DEFAULT 1,
    metadata_json    TEXT    DEFAULT '{}',
    FOREIGN KEY (terminal_id, project_id)
        REFERENCES terminal_leases(terminal_id, project_id),
    FOREIGN KEY (project_id, pool_id)
        REFERENCES pool_config(project_id, pool_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pool_membership_active
    ON worker_pool_membership(terminal_id, project_id)
    WHERE released_at IS NULL;

PRAGMA foreign_keys = ON;
"""

PROJECT_ID = "vnx-dev"
POOL_ID = "default"


def _make_prod_db(tmp_path: Path) -> Path:
    """Create an on-disk DB with production-accurate schema and FK enabled."""
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "events").mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_PROD_SCHEMA)
    conn.execute(
        """
        INSERT OR IGNORE INTO pool_config
            (project_id, pool_id, min_workers, max_workers, target_workers,
             scale_policy, cooldown_seconds, provider_mix_json, heartbeat_stale_seconds)
        VALUES (?, ?, 0, 4, 1, 'queue_depth_v1', 60, '["claude"]', 180)
        """,
        (PROJECT_ID, POOL_ID),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO worker_pools (project_id, pool_id, state, current_size, target_size)
        VALUES (?, ?, 'idle', 0, 1)
        """,
        (PROJECT_ID, POOL_ID),
    )
    conn.commit()
    conn.close()
    return db_path


def _stub_spawn(pid: int = 42000):
    """Return a spawn_fn that never actually spawns a subprocess."""
    def _fn(project_id, pool_id, terminal_id, provider, role):
        return SpawnResult(terminal_id=terminal_id, success=True, pid=pid)
    return _fn


def _count_rows(db_path: Path, table: str, where: str = "1=1") -> int:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()
    conn.close()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# E2E: scale 0→1 creates both lease + membership rows (FK enforced)
# ---------------------------------------------------------------------------

class TestPoolLeaseE2E:
    def test_scale_up_creates_lease_and_membership(self, tmp_path):
        """scale 0→1 must create terminal_leases AND worker_pool_membership.

        If add_or_refresh_pool_lease is missing (the original bug), add_member
        raises sqlite3.IntegrityError: FOREIGN KEY constraint failed because the
        FK terminal_leases(terminal_id, project_id) is enforced.
        """
        db_path = _make_prod_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, ?)",
            ("d-e2e-001", PROJECT_ID, "queued"),
        )
        conn.commit()
        conn.close()

        mgr = PoolManager(PROJECT_ID, POOL_ID, db_path, spawn_fn=_stub_spawn(42001))

        with patch("pool_worktree_manager.create_worker_worktree", return_value=tmp_path / "wt"):
            result = mgr.tick()

        assert len(result.errors) == 0, f"Unexpected errors: {result.errors}"
        assert len(result.spawned) == 1

        # Both rows must exist
        lease_count = _count_rows(
            db_path, "terminal_leases",
            f"project_id='{PROJECT_ID}' AND state='leased'"
        )
        membership_count = _count_rows(
            db_path, "worker_pool_membership",
            f"project_id='{PROJECT_ID}' AND released_at IS NULL"
        )
        assert lease_count == 1, f"Expected 1 lease row, got {lease_count}"
        assert membership_count == 1, f"Expected 1 membership row, got {membership_count}"

        # Lease token must be non-empty (partial unique index compliance)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT lease_token, worker_pid FROM terminal_leases WHERE project_id = ?",
            (PROJECT_ID,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] != "", "lease_token must be non-empty"
        assert row[1] == 42001, "worker_pid must match spawn result"

    def test_scale_up_lease_missing_raises_fk_error_without_fix(self, tmp_path):
        """Regression guard: inserting membership WITHOUT lease raises FK error when FKs are on."""
        db_path = _make_prod_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO worker_pool_membership
                    (terminal_id, project_id, pool_id, provider, role, joined_at, metadata_json)
                VALUES ('ghost-terminal', ?, ?, 'claude', 'backend-developer',
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), '{}')
                """,
                (PROJECT_ID, POOL_ID),
            )
        conn.close()

    def test_reap_releases_lease_row(self, tmp_path):
        """reap_dead must release the lease row so no dangling lease remains."""
        db_path = _make_prod_db(tmp_path)
        repo = PoolStateRepository(db_path, PROJECT_ID)
        now = time.time() - 400  # far in the past so heartbeat is stale

        repo.add_or_refresh_pool_lease("vnx-dev-T-reap", 99001, now)
        repo.add_member(POOL_ID, "vnx-dev-T-reap", "claude", "backend-developer", now, pid=99001)

        mgr = PoolManager(PROJECT_ID, POOL_ID, db_path, spawn_fn=_stub_spawn())
        from pool_reaper import ReapConfig
        mgr.reap_config = ReapConfig(heartbeat_stale_threshold_s=60.0, warmup_window_s=0.0)

        with patch("pool_manager.PoolManager._kill_subprocess"), \
             patch("pool_worktree_manager.reap_worker_worktree"):
            reaped = mgr.reap_dead()

        assert len(reaped) >= 1

        # Lease must be released (not in 'leased' state)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT state FROM terminal_leases WHERE terminal_id = ? AND project_id = ?",
            ("vnx-dev-T-reap", PROJECT_ID),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "released", f"Expected released, got {row[0]}"

    def test_scale_down_releases_lease_row(self, tmp_path):
        """scale_down must release the lease row so no dangling lease remains."""
        db_path = _make_prod_db(tmp_path)
        repo = PoolStateRepository(db_path, PROJECT_ID)
        now = time.time()

        repo.add_or_refresh_pool_lease("vnx-dev-T-sd", 88001, now)
        mid = repo.add_member(POOL_ID, "vnx-dev-T-sd", "claude", "backend-developer", now, pid=88001)

        from pool_decision_engine import PoolDecision
        decision = PoolDecision(
            action="scale_down", delta=-1, reason="test", targets={mid}
        )
        mgr = PoolManager(PROJECT_ID, POOL_ID, db_path, spawn_fn=_stub_spawn())
        mgr.execute(decision)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT state FROM terminal_leases WHERE terminal_id = ? AND project_id = ?",
            ("vnx-dev-T-sd", PROJECT_ID),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "released", f"Expected released, got {row[0]}"

    def test_no_leaked_worktree_on_registration_failure(self, tmp_path):
        """If add_member raises (e.g. FK violation), the worktree must be cleaned up."""
        db_path = _make_prod_db(tmp_path)

        reap_calls = []

        def _spawn(project_id, pool_id, terminal_id, provider, role):
            return SpawnResult(terminal_id=terminal_id, success=True, pid=77777)

        mgr = PoolManager(PROJECT_ID, POOL_ID, db_path, spawn_fn=_spawn)

        # Patch add_member to simulate FK failure after lease insert
        original_add_member = mgr.repo.add_member

        def _bad_add_member(*args, **kwargs):
            raise sqlite3.IntegrityError("simulated FK failure")

        mgr.repo.add_member = _bad_add_member

        with patch("pool_manager.PoolManager._kill_subprocess") as mock_kill, \
             patch("pool_worktree_manager.reap_worker_worktree") as mock_reap_wt, \
             patch("pool_worktree_manager.create_worker_worktree", return_value=tmp_path / "wt"):
            from pool_decision_engine import PoolDecision
            decision = PoolDecision(action="scale_up", delta=1, reason="test")
            result = mgr.execute(decision)

        assert len(result.spawned) == 0
        assert len(result.errors) == 1
        mock_kill.assert_called_once()
        mock_reap_wt.assert_called_once()


# ---------------------------------------------------------------------------
# CLI project_id resolution
# ---------------------------------------------------------------------------

class TestCliProjectIdResolution:
    def test_resolve_project_id_returns_explicit_when_given(self):
        from vnx_cli.commands.pool import _resolve_project_id
        assert _resolve_project_id("my-project") == "my-project"

    def test_resolve_project_id_derives_from_marker_when_none(self, tmp_path):
        from vnx_cli.commands.pool import _resolve_project_id

        # Write a .vnx-project-id marker in tmp_path
        marker = tmp_path / ".vnx-project-id"
        marker.write_text("test-project\n")

        with patch("vnx_paths.resolve_state_dir", return_value=tmp_path / ".vnx-data" / "state"), \
             patch("vnx_paths.project_id_from_state_dir") as mock_pid:
            mock_pid.return_value = "test-project"
            result = _resolve_project_id(None)

        assert result == "test-project"

    def test_resolve_project_id_falls_back_to_default_on_error(self):
        from vnx_cli.commands.pool import _resolve_project_id

        with patch("vnx_paths.project_id_from_state_dir", return_value=""):
            result = _resolve_project_id(None)

        assert result == "default"
