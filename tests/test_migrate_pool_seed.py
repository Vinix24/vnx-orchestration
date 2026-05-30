#!/usr/bin/env python3
"""Tests for migrate pool_config seeding and runtime_schema_version stamping.

Verifies:
- After vnx migrate, pool_config has a row for the derived project_id.
- After vnx migrate, worker_pools has a row for the derived project_id.
- Seeding is idempotent (running migrate twice produces no duplicate rows or errors).
- runtime_schema_version has at least version 10 after bootstrap.
- vnx init also seeds pool_config for the project_id passed to it.
- vnx pool status returns pool state (not "not initialized") after migrate.

ADR-007 binding: pool_config uses composite UNIQUE(project_id, pool_id).
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _clear_schema_preflight_hooks():
    import importlib
    sm = None
    try:
        sm = importlib.import_module("schema_migration")
        saved = {k: list(v) for k, v in sm._PREFLIGHT_HOOKS.items()}
        sm._PREFLIGHT_HOOKS.clear()
    except (ImportError, AttributeError):
        saved = None
    yield
    if saved is not None and sm is not None:
        sm._PREFLIGHT_HOOKS.clear()
        sm._PREFLIGHT_HOOKS.update(saved)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _migrate_args(project_dir):
    return argparse.Namespace(project_dir=str(project_dir))


def _init_args(project_dir, **overrides):
    ns = argparse.Namespace(
        project_path=None,
        project_dir=str(project_dir),
        project_id=None,
        template="default",
        force=False,
        non_interactive=False,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _row_count(db_path: Path, table: str, project_id: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE project_id = ? AND pool_id = 'default'",
            (project_id,),
        ).fetchone()
        return rows[0] if rows else 0
    finally:
        conn.close()


def _max_runtime_schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM runtime_schema_version"
        ).fetchone()
        return int(row[0]) if (row and row[0] is not None) else 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# pool_config row seeding after migrate
# ---------------------------------------------------------------------------

class TestMigratePoolConfigSeed:
    """vnx migrate must seed a pool_config row for the project's derived id."""

    def test_pool_config_row_seeded_after_migrate(self, tmp_path, monkeypatch):
        data_root = tmp_path / "data"
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        rc = vnx_migrate(_migrate_args(project_dir))
        assert rc == 0

        db = data_root / "state" / "runtime_coordination.db"
        assert db.exists()

        # project_id derived from directory name "myproject"
        count = _row_count(db, "pool_config", "myproject")
        assert count == 1, (
            f"pool_config must have exactly 1 row for project_id='myproject' after migrate; got {count}"
        )

    def test_worker_pools_row_seeded_after_migrate(self, tmp_path, monkeypatch):
        data_root = tmp_path / "data"
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        vnx_migrate(_migrate_args(project_dir))

        db = data_root / "state" / "runtime_coordination.db"
        count = _row_count(db, "worker_pools", "myproject")
        assert count == 1, (
            f"worker_pools must have exactly 1 row for project_id='myproject' after migrate; got {count}"
        )

    def test_pool_config_seed_idempotent(self, tmp_path, monkeypatch):
        """Running migrate twice must not duplicate pool_config rows or error."""
        data_root = tmp_path / "data"
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        assert vnx_migrate(_migrate_args(project_dir)) == 0
        assert vnx_migrate(_migrate_args(project_dir)) == 0

        db = data_root / "state" / "runtime_coordination.db"
        count = _row_count(db, "pool_config", "myproject")
        assert count == 1, (
            f"Idempotent migrate must not duplicate pool_config row; got {count} rows"
        )

    def test_pool_config_defaults_satisfy_check_constraints(self, tmp_path, monkeypatch):
        """Seeded pool_config values must satisfy DB CHECK constraints."""
        data_root = tmp_path / "data"
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        vnx_migrate(_migrate_args(project_dir))

        db = data_root / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT min_workers, max_workers, target_workers FROM pool_config "
                "WHERE project_id = 'myproject' AND pool_id = 'default'"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        min_w, max_w, target_w = row
        assert min_w >= 0, "min_workers must be >= 0"
        assert max_w >= min_w, "max_workers must be >= min_workers"
        assert target_w >= min_w and target_w <= max_w, (
            "target_workers must be in [min_workers, max_workers]"
        )


# ---------------------------------------------------------------------------
# runtime_schema_version stamped after bootstrap
# ---------------------------------------------------------------------------

class TestRuntimeSchemaVersionAfterBootstrap:
    """runtime_schema_version must have at least version 10 after bootstrap."""

    def test_runtime_schema_version_stamped_after_migrate(self, tmp_path, monkeypatch):
        data_root = tmp_path / "data"
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        vnx_migrate(_migrate_args(project_dir))

        db = data_root / "state" / "runtime_coordination.db"
        assert db.exists()
        max_ver = _max_runtime_schema_version(db)
        assert max_ver >= 10, (
            f"runtime_schema_version must have at least version 10 after migrate; got {max_ver}"
        )

    def test_runtime_schema_version_stamped_after_bootstrap_call(self, tmp_path, monkeypatch):
        """_bootstrap_runtime_dbs alone must stamp runtime_schema_version >= 10."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import _bootstrap_runtime_dbs
        _bootstrap_runtime_dbs(tmp_path)

        db = tmp_path / "state" / "runtime_coordination.db"
        max_ver = _max_runtime_schema_version(db)
        assert max_ver >= 10, (
            f"runtime_schema_version must be >= 10 after bootstrap; got {max_ver}"
        )


# ---------------------------------------------------------------------------
# vnx init seeds pool_config for the project_id
# ---------------------------------------------------------------------------

class TestInitPoolConfigSeed:
    """vnx init must seed pool_config for the derived project_id."""

    def test_init_seeds_pool_config_row(self, tmp_path, monkeypatch):
        data_root = tmp_path / "data"
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import vnx_init
        rc = vnx_init(_init_args(project_dir))
        assert rc == 0

        db = data_root / "state" / "runtime_coordination.db"
        if not db.exists():
            db = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"

        assert db.exists(), "runtime_coordination.db must exist after vnx init"
        count = _row_count(db, "pool_config", "myproject")
        assert count == 1, (
            f"pool_config must have 1 row for 'myproject' after vnx init; got {count}"
        )


# ---------------------------------------------------------------------------
# End-to-end: pool status not "not initialized" after init+migrate
# ---------------------------------------------------------------------------

class TestPoolStatusAfterMigrate:
    """After migrate, pool_manager.load_state must not raise RuntimeError."""

    def test_load_state_does_not_raise_after_migrate(self, tmp_path, monkeypatch):
        data_root = tmp_path / "data"
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        vnx_migrate(_migrate_args(project_dir))

        db = data_root / "state" / "runtime_coordination.db"
        assert db.exists()

        from pool_manager import PoolManager
        mgr = PoolManager(project_id="myproject", pool_id="default", db_path=db)
        # Must not raise RuntimeError("No pool_config row...")
        config, state, members = mgr.load_state()
        assert config.pool_id == "default"
        assert config.min_workers >= 0
        assert config.max_workers >= config.min_workers
        assert len(members) == 0  # fresh pool has no members

    def test_load_state_idempotent_after_double_migrate(self, tmp_path, monkeypatch):
        """Double migrate must leave pool in a clean, loadable state."""
        data_root = tmp_path / "data"
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        vnx_migrate(_migrate_args(project_dir))
        vnx_migrate(_migrate_args(project_dir))

        db = data_root / "state" / "runtime_coordination.db"
        from pool_manager import PoolManager
        mgr = PoolManager(project_id="myproject", pool_id="default", db_path=db)
        config, state, members = mgr.load_state()
        assert config.pool_id == "default"


# ---------------------------------------------------------------------------
# Resolver alignment: pool + dream must derive the SAME project_id + DB path
# as migrate/init (PR-RESOLVER-UNIFY)
# ---------------------------------------------------------------------------

class TestResolverAlignment:
    """pool and dream must resolve the same project_id + db_path as migrate.

    ADR-007: every resolved id must be marker-backed (never 'default').
    """

    def test_pool_resolves_same_project_id_as_migrate(self, tmp_path, monkeypatch):
        """vnx pool derive_project_id matches what migrate stamps in pool_config."""
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        data_root = tmp_path / "data"

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        vnx_migrate(_migrate_args(project_dir))

        from vnx_cli import _engine
        derived_id = _engine.derive_project_id(project_dir)
        assert derived_id == "myproject", f"derive_project_id returned {derived_id!r}"

        db = _engine.resolve_data_root(project_dir) / "state" / "runtime_coordination.db"
        assert db.exists(), f"DB not found at {db}"

        count = _row_count(db, "pool_config", derived_id)
        assert count == 1, (
            f"pool_config row must exist for derived_id={derived_id!r}; got {count}"
        )

    def test_pool_db_path_matches_migrate_db_path(self, tmp_path, monkeypatch):
        """_engine.resolve_data_root(project_dir)/state/runtime_coordination.db == migrate's DB."""
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        data_root = tmp_path / "data"

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        vnx_migrate(_migrate_args(project_dir))

        from vnx_cli import _engine
        pool_db = _engine.resolve_data_root(project_dir) / "state" / "runtime_coordination.db"
        migrate_db = data_root / "state" / "runtime_coordination.db"

        assert pool_db.resolve() == migrate_db.resolve(), (
            f"pool db_path {pool_db} != migrate db_path {migrate_db}"
        )

    def test_runtime_schema_version_table_created_on_bootstrap(self, tmp_path, monkeypatch):
        """CREATE TABLE IF NOT EXISTS guard ensures runtime_schema_version never raises."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import _bootstrap_runtime_dbs
        _bootstrap_runtime_dbs(tmp_path)

        import sqlite3
        db = tmp_path / "state" / "runtime_coordination.db"
        assert db.exists()
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_schema_version'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1, "runtime_schema_version table must exist after bootstrap"
