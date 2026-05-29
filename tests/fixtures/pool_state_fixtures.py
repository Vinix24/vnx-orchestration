"""pool_state_fixtures.py — Pytest fixtures and factory helpers for pool tests.

Provides:
- make_config() — PoolConfig factory with sensible defaults
- make_state() — PoolState factory
- make_member() — Membership factory
- pool_db() — in-memory SQLite with schema v14 tables initialized
- active_member_row() — inserts a live membership row into test DB

Wave 6 PR-6.3 — ADR-018 elastic worker pool.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from pool_decision_engine import Membership, PoolConfig, PoolState  # noqa: E402

# ---------------------------------------------------------------------------
# Domain object factories
# ---------------------------------------------------------------------------

def make_config(
    pool_id: str = "default",
    min_workers: int = 1,
    max_workers: int = 4,
    scaling_policy: str = "queue_depth_v1",
    provider_mix: Optional[list] = None,
    cooldown_seconds: float = 60.0,
    heartbeat_stale_seconds: float = 300.0,
) -> PoolConfig:
    return PoolConfig(
        pool_id=pool_id,
        min_workers=min_workers,
        max_workers=max_workers,
        scaling_policy=scaling_policy,
        provider_mix=provider_mix or ["claude"],
        cooldown_seconds=cooldown_seconds,
        heartbeat_stale_seconds=heartbeat_stale_seconds,
    )


def make_state(
    queue_depth: int = 0,
    last_scaled_at: Optional[float] = None,
    now: float = 1000.0,
) -> PoolState:
    return PoolState(
        queue_depth=queue_depth,
        last_scaled_at=last_scaled_at,
        now=now,
    )


def make_member(
    membership_id: str = "test-member-001",
    terminal_id: str = "T1",
    provider: str = "claude",
    pool_role: str = "backend-developer",
    status: str = "active",
    joined_at: float = 900.0,
    last_heartbeat: Optional[float] = None,
) -> Membership:
    return Membership(
        membership_id=membership_id,
        terminal_id=terminal_id,
        provider=provider,
        pool_role=pool_role,
        status=status,
        joined_at=joined_at,
        last_heartbeat=last_heartbeat,
    )


# ---------------------------------------------------------------------------
# SQLite test database setup
# ---------------------------------------------------------------------------

_BASE_SCHEMA = """
PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS runtime_schema_version (
    version     INTEGER PRIMARY KEY,
    description TEXT,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
INSERT OR IGNORE INTO runtime_schema_version(version, description)
VALUES (14, 'test-base-v14');

CREATE TABLE IF NOT EXISTS terminal_leases (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id       TEXT    NOT NULL,
    project_id        TEXT    NOT NULL,
    state             TEXT    NOT NULL DEFAULT 'idle',
    lease_token       TEXT    NOT NULL DEFAULT '',
    last_heartbeat_at TEXT,
    worker_pid        INTEGER,
    UNIQUE(terminal_id, project_id)
);

CREATE TABLE IF NOT EXISTS dispatches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    project_id  TEXT NOT NULL DEFAULT 'vnx-dev',
    state       TEXT NOT NULL DEFAULT 'queued',
    UNIQUE(dispatch_id, project_id)
);

CREATE TABLE IF NOT EXISTS pool_config (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          TEXT    NOT NULL,
    pool_id             TEXT    NOT NULL DEFAULT 'default',
    min_workers         INTEGER NOT NULL DEFAULT 1,
    max_workers         INTEGER NOT NULL DEFAULT 6,
    target_workers      INTEGER NOT NULL DEFAULT 3,
    role_mix_json       TEXT    NOT NULL DEFAULT '["backend-developer"]',
    provider_mix_json   TEXT    NOT NULL DEFAULT '["claude"]',
    scale_policy        TEXT    NOT NULL DEFAULT 'queue_depth_v1',
    cooldown_seconds    INTEGER NOT NULL DEFAULT 120,
    cost_ceiling_usd    REAL,
    heartbeat_stale_seconds REAL NOT NULL DEFAULT 180,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
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
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id         TEXT    NOT NULL,
    project_id          TEXT    NOT NULL,
    pool_id             TEXT    NOT NULL DEFAULT 'default',
    provider            TEXT    NOT NULL,
    role                TEXT    NOT NULL,
    joined_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    released_at         TEXT,
    release_reason      TEXT,
    spawn_generation    INTEGER NOT NULL DEFAULT 1,
    metadata_json       TEXT    DEFAULT '{}',
    FOREIGN KEY (terminal_id, project_id)
        REFERENCES terminal_leases(terminal_id, project_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pool_membership_active
    ON worker_pool_membership(terminal_id, project_id)
    WHERE released_at IS NULL;

PRAGMA foreign_keys = ON;
"""


def create_test_db(
    project_id: str = "vnx-dev",
    pool_id: str = "default",
    min_workers: int = 1,
    max_workers: int = 4,
    target_workers: int = 2,
    scale_policy: str = "queue_depth_v1",
    cooldown_seconds: int = 60,
    provider_mix_json: str = '["claude"]',
    cost_ceiling_usd: Optional[float] = None,
    heartbeat_stale_seconds: float = 180.0,
) -> sqlite3.Connection:
    """Create an in-memory SQLite connection with schema v14 pool tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_BASE_SCHEMA)
    conn.execute(
        """
        INSERT OR IGNORE INTO pool_config
            (project_id, pool_id, min_workers, max_workers, target_workers,
             scale_policy, cooldown_seconds, provider_mix_json,
             cost_ceiling_usd, heartbeat_stale_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id, pool_id, min_workers, max_workers, target_workers,
            scale_policy, cooldown_seconds, provider_mix_json,
            cost_ceiling_usd, heartbeat_stale_seconds,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO worker_pools
            (project_id, pool_id, state, current_size, target_size)
        VALUES (?, ?, 'idle', 0, ?)
        """,
        (project_id, pool_id, target_workers),
    )
    conn.commit()
    return conn


def create_test_db_file(
    db_path: Path,
    project_id: str = "vnx-dev",
    pool_id: str = "default",
    min_workers: int = 1,
    max_workers: int = 4,
    target_workers: int = 2,
    scale_policy: str = "queue_depth_v1",
    cooldown_seconds: int = 60,
    provider_mix_json: str = '["claude"]',
    cost_ceiling_usd: Optional[float] = None,
    heartbeat_stale_seconds: float = 180.0,
) -> Path:
    """Initialize a file-backed SQLite DB at db_path with schema v14 pool tables.

    Returns db_path. Used by integration tests that need real file connections
    (PoolStateRepository._connect() opens/closes per-call).
    """
    target_workers = max(target_workers, min_workers)
    target_workers = min(target_workers, max_workers)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_BASE_SCHEMA)
    conn.execute(
        """
        INSERT OR IGNORE INTO pool_config
            (project_id, pool_id, min_workers, max_workers, target_workers,
             scale_policy, cooldown_seconds, provider_mix_json,
             cost_ceiling_usd, heartbeat_stale_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id, pool_id, min_workers, max_workers, target_workers,
            scale_policy, cooldown_seconds, provider_mix_json,
            cost_ceiling_usd, heartbeat_stale_seconds,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO worker_pools
            (project_id, pool_id, state, current_size, target_size)
        VALUES (?, ?, 'idle', 0, ?)
        """,
        (project_id, pool_id, target_workers),
    )
    conn.commit()
    conn.close()
    return db_path


def insert_lease(
    conn: sqlite3.Connection,
    terminal_id: str,
    project_id: str = "vnx-dev",
    last_heartbeat_at: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO terminal_leases
            (terminal_id, project_id, state, lease_token, last_heartbeat_at)
        VALUES (?, ?, 'idle', '', ?)
        """,
        (terminal_id, project_id, last_heartbeat_at),
    )
    conn.commit()


def insert_membership(
    conn: sqlite3.Connection,
    terminal_id: str,
    project_id: str = "vnx-dev",
    pool_id: str = "default",
    provider: str = "claude",
    role: str = "backend-developer",
    joined_at: Optional[str] = None,
    membership_id: Optional[str] = None,
) -> str:
    import json
    import uuid as _uuid
    mid = membership_id or str(_uuid.uuid4())
    jat = joined_at or "2026-01-01T00:00:00.000000Z"
    conn.execute(
        """
        INSERT INTO worker_pool_membership
            (terminal_id, project_id, pool_id, provider, role, joined_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (terminal_id, project_id, pool_id, provider, role, jat,
         json.dumps({"membership_id": mid})),
    )
    conn.commit()
    return mid


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def pool_config_default() -> PoolConfig:
    return make_config()


@pytest.fixture()
def pool_state_empty() -> PoolState:
    return make_state(queue_depth=0, now=1000.0)


@pytest.fixture()
def pool_state_with_queue() -> PoolState:
    return make_state(queue_depth=4, now=1000.0)


@pytest.fixture()
def pool_state_cooldown() -> PoolState:
    return make_state(queue_depth=4, last_scaled_at=980.0, now=1000.0)


@pytest.fixture()
def one_active_member() -> Membership:
    return make_member(membership_id="m-001", joined_at=900.0, last_heartbeat=990.0)


@pytest.fixture()
def stale_member() -> Membership:
    return make_member(
        membership_id="m-stale",
        joined_at=100.0,
        last_heartbeat=100.0,  # stale: now=1000, threshold=300 => 900s ago
    )
