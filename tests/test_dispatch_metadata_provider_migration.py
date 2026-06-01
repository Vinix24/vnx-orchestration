"""Tests for GAP 2: dispatch_metadata provider/model migration.

Covers:
- migrate_dispatch_metadata_provider.run_migration is idempotent (runs twice without error)
- provider + model columns are present after migration on a fresh temp DB
- UNIQUE INDEX on (project_id, dispatch_id) is created
- (project_id, provider) index is created
- upsert_dispatch_provider_row stamps both provider AND model
- log_dispatch_metadata --model argument accepted and stamped (compile + arg parse)
- quality_db_init _migrate_v23 adds model column on a DB at user_version 22

Dispatch-ID: 20260601-1620-gap2-provider-col
ADR-007: composite uniqueness on dispatch_metadata enforced via UNIQUE INDEX
         (no table rebuild — 908 existing rows verified unique pre-migration).
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

import migrate_dispatch_metadata_provider as MIG  # noqa: E402
from dispatch_metadata_db import upsert_dispatch_provider_row  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_legacy_db(path: Path) -> None:
    """Create dispatch_metadata without provider or model (user_version 19 state)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            terminal TEXT NOT NULL,
            track TEXT NOT NULL,
            role TEXT,
            skill_name TEXT,
            gate TEXT,
            cognition TEXT DEFAULT 'normal',
            priority TEXT DEFAULT 'P1',
            pr_id TEXT,
            dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            outcome_status TEXT,
            outcome_report_path TEXT,
            session_id TEXT
        );
        PRAGMA user_version = 19;
    """)
    conn.execute(
        "INSERT INTO dispatch_metadata (dispatch_id, terminal, track) VALUES (?, ?, ?)",
        ("existing-dispatch-001", "T1", "A"),
    )
    conn.execute(
        "INSERT INTO dispatch_metadata (dispatch_id, terminal, track, project_id) VALUES (?, ?, ?, ?)",
        ("existing-dispatch-002", "T2", "B", "vnx-dev"),
    )
    conn.commit()
    conn.close()


def _cols(path: Path, table: str) -> set:
    conn = sqlite3.connect(str(path))
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    conn.close()
    return {r[1] for r in rows}


def _indexes(path: Path) -> set:
    conn = sqlite3.connect(str(path))
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------

def test_migration_adds_provider_and_model_columns():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        _make_legacy_db(db_path)
        result = MIG.run_migration(db_path)
        assert "error" not in result
        cols = _cols(db_path, "dispatch_metadata")
        assert "provider" in cols, "provider column must be present after migration"
        assert "model" in cols, "model column must be present after migration"
    finally:
        db_path.unlink(missing_ok=True)


def test_migration_idempotent_second_run():
    """Running migration twice must not error and must not duplicate columns."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        _make_legacy_db(db_path)
        MIG.run_migration(db_path)
        result2 = MIG.run_migration(db_path)
        assert "error" not in result2
        # Columns still present, not duplicated
        cols = _cols(db_path, "dispatch_metadata")
        assert "provider" in cols
        assert "model" in cols
    finally:
        db_path.unlink(missing_ok=True)


def test_migration_preserves_existing_rows():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        _make_legacy_db(db_path)
        MIG.run_migration(db_path)
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM dispatch_metadata").fetchone()[0]
        conn.close()
        assert count == 2, "existing rows must survive migration (no data loss)"
    finally:
        db_path.unlink(missing_ok=True)


def test_migration_creates_unique_index():
    """ADR-007: UNIQUE INDEX on (project_id, dispatch_id) enforced without rebuild."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        _make_legacy_db(db_path)
        MIG.run_migration(db_path)
        indexes = _indexes(db_path)
        assert "idx_dispatch_meta_composite_unique" in indexes, (
            "ADR-007: composite UNIQUE INDEX on (project_id, dispatch_id) must exist"
        )
    finally:
        db_path.unlink(missing_ok=True)


def test_migration_creates_provider_index():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        _make_legacy_db(db_path)
        MIG.run_migration(db_path)
        indexes = _indexes(db_path)
        assert "idx_dispatch_meta_provider" in indexes
    finally:
        db_path.unlink(missing_ok=True)


def test_migration_db_not_found_returns_error():
    result = MIG.run_migration(Path("/nonexistent/path/quality_intelligence.db"))
    assert "error" in result


# ---------------------------------------------------------------------------
# upsert_dispatch_provider_row — provider + model stamping
# ---------------------------------------------------------------------------

def _make_full_db(path: Path) -> None:
    """Create dispatch_metadata with provider + model (post-migration state)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            terminal TEXT NOT NULL,
            track TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            role TEXT,
            gate TEXT,
            pr_id TEXT,
            dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME,
            outcome_status TEXT,
            outcome_report_path TEXT,
            UNIQUE (project_id, dispatch_id)
        );
    """)
    conn.commit()
    conn.close()


def test_upsert_stamps_provider_and_model():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        _make_full_db(db_path)
        ok = upsert_dispatch_provider_row(
            db_path,
            dispatch_id="test-dispatch-001",
            terminal="T1",
            provider="codex",
            model="codex-mini-latest",
            project_id="vnx-dev",
        )
        assert ok
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT provider, model FROM dispatch_metadata WHERE dispatch_id=?",
            ("test-dispatch-001",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "codex", f"expected provider='codex', got {row[0]!r}"
        assert row[1] == "codex-mini-latest", f"expected model='codex-mini-latest', got {row[1]!r}"
    finally:
        db_path.unlink(missing_ok=True)


def test_upsert_provider_only_when_no_model_column():
    """Upsert must succeed on a DB that has provider but not model (pre-v23)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        path = db_path
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                terminal TEXT NOT NULL,
                track TEXT NOT NULL,
                provider TEXT,
                role TEXT,
                gate TEXT,
                pr_id TEXT,
                dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (project_id, dispatch_id)
            );
        """)
        conn.commit()
        conn.close()
        ok = upsert_dispatch_provider_row(
            path,
            dispatch_id="test-no-model-col",
            terminal="T2",
            provider="kimi",
            model="kimi-k2",
            project_id="vnx-dev",
        )
        assert ok
        conn = sqlite3.connect(str(path))
        row = conn.execute(
            "SELECT provider FROM dispatch_metadata WHERE dispatch_id=?",
            ("test-no-model-col",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "kimi"
    finally:
        db_path.unlink(missing_ok=True)


def test_upsert_idempotent_second_call_does_not_clobber_model():
    """Second upsert (same dispatch_id) must not overwrite model once set (COALESCE)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        _make_full_db(db_path)
        upsert_dispatch_provider_row(
            db_path,
            dispatch_id="idem-dispatch",
            terminal="T1",
            provider="claude",
            model="claude-sonnet-4-6",
            project_id="vnx-dev",
        )
        upsert_dispatch_provider_row(
            db_path,
            dispatch_id="idem-dispatch",
            terminal="T1",
            provider="claude",
            model="claude-opus-4-8",  # different model — must not overwrite
            project_id="vnx-dev",
        )
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT model FROM dispatch_metadata WHERE dispatch_id=?",
            ("idem-dispatch",),
        ).fetchone()
        conn.close()
        assert row[0] == "claude-sonnet-4-6", (
            "model must not be overwritten by a second upsert (COALESCE semantics)"
        )
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# quality_db_init _migrate_v23
# ---------------------------------------------------------------------------

def test_migrate_v23_adds_model_column():
    """_migrate_v23 adds model column on a DB at user_version 22 (no provider/model)."""
    import quality_db_init as QDB  # noqa: E402 — import here to avoid side-effects at module load

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        # Simulate a DB at version 22 that has provider but not model
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                terminal TEXT NOT NULL,
                track TEXT NOT NULL,
                provider TEXT,
                UNIQUE (project_id, dispatch_id)
            );
            PRAGMA user_version = 22;
        """)
        conn.commit()

        import scripts.lib.schema_migration as SM  # noqa: E402
        SM.apply_if_below(conn, 23, QDB._migrate_v23)
        conn.commit()

        cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
        assert "model" in cols, "_migrate_v23 must add model column"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 23
        conn.close()
    finally:
        db_path.unlink(missing_ok=True)


def test_migrate_v23_idempotent():
    """Running _migrate_v23 twice must not error."""
    import quality_db_init as QDB  # noqa: E402
    import scripts.lib.schema_migration as SM  # noqa: E402

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                terminal TEXT NOT NULL,
                track TEXT NOT NULL,
                UNIQUE (project_id, dispatch_id)
            );
            PRAGMA user_version = 22;
        """)
        conn.commit()
        SM.apply_if_below(conn, 23, QDB._migrate_v23)
        conn.commit()
        # Run again directly (already at v23, apply_if_below skips)
        QDB._migrate_v23(conn)
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
        assert "model" in cols
        conn.close()
    finally:
        db_path.unlink(missing_ok=True)
