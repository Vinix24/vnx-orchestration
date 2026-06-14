"""tests/migrations/test_auto_apply.py — Wave 6 PR-6.5d auto-apply hook tests.

Verifies that ``scripts/lib/migrations/auto_apply.auto_apply`` discovers
NNNN_*.sql migrations, delegates to the apply_NNNN.py runner, advances
``PRAGMA user_version``, and emits the documented INFO log per applied
migration. Errors are propagated unchanged.

Fixture DBs are written to ``tmp_path`` (real files; the runners open
sqlite3 connections by path, so :memory: is not usable). Each test sets
up the minimum schema state needed by the runner under test
(runtime_schema_version + terminal_leases for 0020).
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from migrations.auto_apply import (  # noqa: E402  (path bootstrap above)
    _discover_migrations,
    _load_runner,
    auto_apply,
)

_MIGRATIONS_DIR = _REPO_ROOT / "schemas" / "migrations"
_RUNNERS_DIR = _REPO_ROOT / "scripts" / "lib" / "migrations"

# The auto_apply lane builds the dispatches composite UNIQUE inside migration 0022;
# keep the migrate_future_system v22 preflight (leaked via collection-time imports
# in a shared pytest process) out of this lane. See conftest for the rationale.
pytestmark = pytest.mark.usefixtures("isolate_v22_composite_preflight")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _seed_db_at_v13(db_path: Path) -> None:
    """Create a runtime_coordination.db with prerequisite tables at v13.

    Mirrors the post-install state expected by apply_0020 (the runner is
    strict: it refuses to apply unless current_version == 13). The legacy
    ``dispatches`` table is created WITHOUT project_id (pre-0010 shape): the
    full auto_apply chain runs 0022/0024/0026, and apply_0022 self-heals the
    missing project_id (ADR-007 tenant key) before its in-place rebuild — so
    this fixture also exercises the #863 graceful-degrade fix.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE runtime_schema_version (
                version     INTEGER PRIMARY KEY,
                description TEXT,
                applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            INSERT INTO runtime_schema_version(version, description)
            VALUES (13, 'test seed v13');

            CREATE TABLE terminal_leases (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                terminal_id TEXT    NOT NULL,
                project_id  TEXT    NOT NULL,
                state       TEXT    NOT NULL DEFAULT 'free',
                lease_token TEXT    NOT NULL DEFAULT '',
                UNIQUE(terminal_id, project_id)
            );

            CREATE TABLE dispatches (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id   TEXT    NOT NULL,
                state         TEXT    NOT NULL DEFAULT 'queued',
                terminal_id   TEXT, track TEXT, priority TEXT DEFAULT 'P2',
                pr_ref        TEXT, gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
                bundle_path   TEXT,
                created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                expires_after TEXT, metadata_json TEXT DEFAULT '{}'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _dispatches_has_project_id(db_path: Path) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        return any(r[1] == "project_id" for r in conn.execute("PRAGMA table_info(dispatches)"))
    finally:
        conn.close()


def _user_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    finally:
        conn.close()


def _table_exists(db_path: Path, table: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discover_skips_down_migrations(tmp_path):
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0020_x.sql").write_text("-- up")
    (mig_dir / "0020_x_down.sql").write_text("-- down")
    (mig_dir / "not_a_migration.sql").write_text("-- noop")
    found = _discover_migrations(mig_dir)
    assert [n for n, _ in found] == [20]


def test_discover_sorts_ascending(tmp_path):
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    for name in ("0020_a.sql", "0017_b.sql", "0019_c.sql"):
        (mig_dir / name).write_text("-- noop")
    nums = [n for n, _ in _discover_migrations(mig_dir)]
    assert nums == [17, 19, 20]


def test_discover_missing_dir_returns_empty(tmp_path):
    assert _discover_migrations(tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# Runner loader
# ---------------------------------------------------------------------------

def test_load_runner_returns_none_when_missing(tmp_path):
    assert _load_runner(tmp_path, 9999) is None


def test_load_runner_loads_real_apply_0020():
    module = _load_runner(_RUNNERS_DIR, 20)
    assert module is not None
    assert hasattr(module, "apply_migration")


# ---------------------------------------------------------------------------
# Apply path (uses real 0020 migration against fixture DB)
# ---------------------------------------------------------------------------

def test_auto_apply_creates_pool_tables_and_bumps_user_version(tmp_path, caplog, monkeypatch):
    # B-N1: apply_0022's self-heal now resolves a VALIDATED tenant (never a silent
    # 'vnx-dev' default). The tmp DB has no canonical path nor marker, so supply the
    # tenant via VNX_PROJECT_ID — the production lane resolves it from the central
    # DB path or the repo's .vnx-project-id marker.
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    db_path = tmp_path / "runtime_coordination.db"
    _seed_db_at_v13(db_path)
    assert _user_version(db_path) == 0
    assert not _table_exists(db_path, "pool_config")

    with caplog.at_level(logging.INFO, logger="migrations.auto_apply"):
        applied = auto_apply(db_path)

    assert 20 in applied
    assert _table_exists(db_path, "pool_config")
    assert _table_exists(db_path, "worker_pools")
    assert _table_exists(db_path, "worker_pool_membership")
    # The full chain (0020 → 0022 → 0024 → 0026) runs; apply_0022 self-heals the
    # legacy dispatches table with project_id before its in-place rebuild (#863).
    assert _dispatches_has_project_id(db_path)
    # PRAGMA advances to the highest migration number with a runner (0026).
    assert _user_version(db_path) == 26
    assert any("migration 0020 auto-applied" in rec.message for rec in caplog.records)


def test_auto_apply_is_idempotent_on_second_run(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")  # B-N1: validated tenant for the self-heal
    db_path = tmp_path / "runtime_coordination.db"
    _seed_db_at_v13(db_path)
    auto_apply(db_path)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="migrations.auto_apply"):
        applied = auto_apply(db_path)

    assert applied == []
    assert _user_version(db_path) == 26
    # No re-application log line on the second run.
    assert not any("auto-applied" in rec.message for rec in caplog.records)


def test_auto_apply_skips_when_user_version_already_high(tmp_path):
    db_path = tmp_path / "runtime_coordination.db"
    _seed_db_at_v13(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    finally:
        conn.close()

    applied = auto_apply(db_path)
    assert applied == []
    # Pool tables not created because every migration was below user_version.
    assert not _table_exists(db_path, "pool_config")
    assert _user_version(db_path) == 99


def test_auto_apply_propagates_runner_error(tmp_path, monkeypatch):
    """When the underlying runner raises, the exception propagates unchanged
    and PRAGMA user_version is not advanced. Uses a stub runners_dir whose
    apply_0020.py deliberately raises sqlite3.Error.
    """
    db_path = tmp_path / "runtime_coordination.db"
    _seed_db_at_v13(db_path)

    fake_runners = tmp_path / "fake_runners"
    fake_runners.mkdir()
    (fake_runners / "apply_0020.py").write_text(
        "import sqlite3\n"
        "def apply_migration(db_path, sql_path):\n"
        "    raise sqlite3.OperationalError('forced failure for test')\n"
    )

    # Restrict migrations_dir to only 0020 so we don't pick up unrelated runners.
    fake_migrations = tmp_path / "fake_migrations"
    fake_migrations.mkdir()
    (fake_migrations / "0020_test.sql").write_text("-- placeholder")

    with pytest.raises(sqlite3.OperationalError, match="forced failure"):
        auto_apply(db_path, migrations_dir=fake_migrations, runners_dir=fake_runners)
    # PRAGMA must NOT have advanced on failure.
    assert _user_version(db_path) == 0
