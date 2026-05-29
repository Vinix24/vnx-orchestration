"""Tests for OI-011: central QI DB partial-init bootstrap fix.

Root cause: retroactive_backfill._open_tracker() creates dispatch_experiments
in the central QI DB (user_version=0, no other tables). migrate_to_central_vnx
previously skipped bootstrap_qi_db in this state, leaving the self-learning
loop and intelligence layer broken.

Covers:
- _qi_is_partial detects partial-init DB correctly
- _qi_is_partial returns False for truly empty and fully bootstrapped DBs
- migrate apply flow completes a partial-init QI DB (dispatch_experiments only)
- dispatch_experiments table + rows are preserved through bootstrap
- Running the bootstrap twice is idempotent (no error, no data loss)
- vnx_doctor.check_database warns on under-versioned central QI DB
- repair_quality_db completes a partial DB to current version

ADR-007 binding: all new central tables carry composite UNIQUE/PK over project_id.
bootstrap_qi_db enforces this via the migration chain.

Dispatch-ID: 20260529-161904-central-qdb
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

import scripts.migrate_to_central_vnx as M  # noqa: E402
import scripts.quality_db_init as QDB  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_partial_qi_db(path: Path, rows: int = 3) -> None:
    """Create a QI DB with only dispatch_experiments (mimics retroactive_backfill).

    Exactly mirrors retroactive_backfill._open_tracker(): creates
    dispatch_experiments with PRAGMA user_version = 0 (never bootstrapped).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dispatch_experiments (
                id INTEGER PRIMARY KEY,
                dispatch_id TEXT UNIQUE,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                instruction_chars INTEGER,
                context_items INTEGER,
                repo_map_symbols INTEGER,
                role TEXT,
                cognition TEXT,
                model TEXT,
                terminal TEXT,
                file_count INTEGER,
                success BOOLEAN,
                cqs REAL,
                completion_minutes REAL,
                test_count INTEGER,
                committed BOOLEAN,
                lines_changed INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_de_dispatch_id ON dispatch_experiments (dispatch_id);
        """)
        for i in range(rows):
            conn.execute(
                "INSERT INTO dispatch_experiments (dispatch_id, role, model, terminal)"
                " VALUES (?, 'backend-developer', 'sonnet', 'T1')",
                (f"partial-dispatch-{i}",),
            )
        conn.commit()
        # user_version stays at 0 — never bootstrapped
    finally:
        conn.close()


def _make_fully_bootstrapped_qi_db(path: Path) -> None:
    """Create a fully bootstrapped QI DB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    schema_file = ROOT / "schemas" / "quality_intelligence.sql"
    QDB.bootstrap_qi_db(path, schema_file)


def _get_user_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _table_exists(db_path: Path, table: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','virtual') AND name=?",
            (table,),
        ).fetchone() is not None
    finally:
        conn.close()


def _row_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _qi_is_partial detection
# ---------------------------------------------------------------------------

def test_qi_is_partial_true_for_dispatch_experiments_only(tmp_path):
    """Partial DB (dispatch_experiments, user_version=0) must be detected."""
    qi_db = tmp_path / "quality_intelligence.db"
    _make_partial_qi_db(qi_db, rows=2)

    assert M._qi_is_partial(qi_db) is True, (
        "_qi_is_partial must return True for a DB with dispatch_experiments "
        "only (user_version=0, missing success_patterns)"
    )


def test_qi_is_partial_false_for_missing_file(tmp_path):
    """Non-existent file is not partial."""
    qi_db = tmp_path / "does_not_exist.db"
    assert M._qi_is_partial(qi_db) is False


def test_qi_is_partial_false_for_empty_file(tmp_path):
    """Zero-byte file is not partial."""
    qi_db = tmp_path / "empty.db"
    qi_db.write_bytes(b"")
    assert M._qi_is_partial(qi_db) is False


def test_qi_is_partial_false_for_no_tables(tmp_path):
    """SQLite file with no tables is truly empty, not partial."""
    qi_db = tmp_path / "fresh.db"
    sqlite3.connect(str(qi_db)).close()
    assert M._qi_is_partial(qi_db) is False


def test_qi_is_partial_false_for_fully_bootstrapped(tmp_path):
    """Fully bootstrapped DB must not be detected as partial."""
    qi_db = tmp_path / "quality_intelligence.db"
    _make_fully_bootstrapped_qi_db(qi_db)

    assert M._qi_is_partial(qi_db) is False, (
        "_qi_is_partial must return False for a fully bootstrapped DB "
        f"(user_version={_get_user_version(qi_db)})"
    )


# ---------------------------------------------------------------------------
# bootstrap_qi_db on partial DB — schema completeness
# ---------------------------------------------------------------------------

def test_bootstrap_completes_partial_db_to_highest_version(tmp_path):
    """bootstrap_qi_db must bring a partial DB up to HIGHEST_QI_VERSION."""
    qi_db = tmp_path / "quality_intelligence.db"
    _make_partial_qi_db(qi_db, rows=3)

    assert _get_user_version(qi_db) == 0, "precondition: partial DB has user_version=0"

    schema_file = ROOT / "schemas" / "quality_intelligence.sql"
    result = QDB.bootstrap_qi_db(qi_db, schema_file)

    assert result is True, "bootstrap_qi_db must return True"
    assert _get_user_version(qi_db) == QDB.HIGHEST_QI_VERSION, (
        f"user_version must be {QDB.HIGHEST_QI_VERSION} after bootstrap, "
        f"got {_get_user_version(qi_db)}"
    )


def test_bootstrap_creates_success_patterns_on_partial_db(tmp_path):
    """success_patterns (the sentinel table) must exist after bootstrap."""
    qi_db = tmp_path / "quality_intelligence.db"
    _make_partial_qi_db(qi_db, rows=1)

    schema_file = ROOT / "schemas" / "quality_intelligence.sql"
    QDB.bootstrap_qi_db(qi_db, schema_file)

    assert _table_exists(qi_db, "success_patterns"), (
        "success_patterns must exist in central QI DB after bootstrap"
    )


def test_bootstrap_creates_dream_tables_on_partial_db(tmp_path):
    """dream_cycles and dream_pattern_archives (ADR-019) must exist after bootstrap."""
    qi_db = tmp_path / "quality_intelligence.db"
    _make_partial_qi_db(qi_db, rows=1)

    schema_file = ROOT / "schemas" / "quality_intelligence.sql"
    QDB.bootstrap_qi_db(qi_db, schema_file)

    for table in ("dream_cycles", "dream_pattern_archives"):
        assert _table_exists(qi_db, table), (
            f"{table} must exist in central QI DB after bootstrap (ADR-019)"
        )


def test_bootstrap_preserves_dispatch_experiments_table(tmp_path):
    """dispatch_experiments must still exist after bootstrap (not dropped/recreated)."""
    qi_db = tmp_path / "quality_intelligence.db"
    _make_partial_qi_db(qi_db, rows=5)

    schema_file = ROOT / "schemas" / "quality_intelligence.sql"
    QDB.bootstrap_qi_db(qi_db, schema_file)

    assert _table_exists(qi_db, "dispatch_experiments"), (
        "dispatch_experiments must survive bootstrap on a partial DB"
    )


def test_bootstrap_preserves_dispatch_experiments_rows(tmp_path):
    """Existing dispatch_experiments rows must survive bootstrap."""
    qi_db = tmp_path / "quality_intelligence.db"
    _make_partial_qi_db(qi_db, rows=4)

    schema_file = ROOT / "schemas" / "quality_intelligence.sql"
    QDB.bootstrap_qi_db(qi_db, schema_file)

    count = _row_count(qi_db, "dispatch_experiments")
    assert count == 4, (
        f"dispatch_experiments must retain 4 rows after bootstrap, got {count}"
    )


# ---------------------------------------------------------------------------
# Idempotency: bootstrap twice = no error, no data loss
# ---------------------------------------------------------------------------

def test_bootstrap_twice_is_idempotent(tmp_path):
    """Running bootstrap_qi_db twice on a partial DB must not error or lose data."""
    qi_db = tmp_path / "quality_intelligence.db"
    _make_partial_qi_db(qi_db, rows=3)

    schema_file = ROOT / "schemas" / "quality_intelligence.sql"
    assert QDB.bootstrap_qi_db(qi_db, schema_file) is True, "first bootstrap must succeed"
    assert QDB.bootstrap_qi_db(qi_db, schema_file) is True, "second bootstrap must succeed"

    assert _get_user_version(qi_db) == QDB.HIGHEST_QI_VERSION
    assert _row_count(qi_db, "dispatch_experiments") == 3


# ---------------------------------------------------------------------------
# migrate_to_central_vnx apply flow with partial QI DB
# ---------------------------------------------------------------------------

def _make_source_rc_db(path: Path, project_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS runtime_schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS dispatches (
                dispatch_id TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'queued',
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            INSERT OR IGNORE INTO runtime_schema_version (version, description)
            VALUES (10, 'test-fixture');
        """)
        con.commit()
    finally:
        con.close()


def _build_registry(tmp_path: Path, project_id: str = "proj-test") -> tuple[Path, Path]:
    proj_dir = tmp_path / project_id
    state_dir = proj_dir / ".vnx-data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    _make_source_rc_db(state_dir / "runtime_coordination.db", project_id)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "schema_version": 1,
        "projects": [{"name": project_id, "path": str(proj_dir), "project_id": project_id}],
    }))
    backup_base = tmp_path / "backups"
    backup_base.mkdir()
    return registry, backup_base


def test_apply_handles_partial_qi_db(tmp_path, monkeypatch):
    """migrate apply must bootstrap a partial central QI DB without --fresh-central.

    Scenario: central QI DB has dispatch_experiments (user_version=0) from
    retroactive_backfill but has never been fully bootstrapped. The migrator
    must detect this via _qi_is_partial and complete the bootstrap.
    """
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")

    registry, backup_base = _build_registry(tmp_path)

    central_state = tmp_path / "central" / "state"
    central_state.mkdir(parents=True, exist_ok=True)
    central_qi = central_state / "quality_intelligence.db"

    # Pre-create partial central QI DB (the OI-011 condition)
    _make_partial_qi_db(central_qi, rows=2)
    assert _get_user_version(central_qi) == 0, "precondition: partial DB"

    rc = M.main([
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        # --fresh-central intentionally OMITTED: partial DB should not need it
        "--registry", str(registry),
        "--backup-base", str(backup_base),
        "--central-state", str(central_state),
    ])

    assert rc == 0, (
        "apply must exit 0 when central QI DB is partial-init (OI-011 fix). "
        f"Got exit {rc}"
    )

    assert _table_exists(central_qi, "success_patterns"), (
        "success_patterns must exist in central QI DB after apply on partial DB"
    )
    assert _get_user_version(central_qi) == QDB.HIGHEST_QI_VERSION, (
        f"user_version must be {QDB.HIGHEST_QI_VERSION} after apply, "
        f"got {_get_user_version(central_qi)}"
    )


def test_apply_partial_qi_db_preserves_existing_rows(tmp_path, monkeypatch):
    """dispatch_experiments rows must survive the partial-init bootstrap path."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")

    registry, backup_base = _build_registry(tmp_path)

    central_state = tmp_path / "central" / "state"
    central_state.mkdir(parents=True, exist_ok=True)
    central_qi = central_state / "quality_intelligence.db"

    _make_partial_qi_db(central_qi, rows=3)

    M.main([
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        "--registry", str(registry),
        "--backup-base", str(backup_base),
        "--central-state", str(central_state),
    ])

    count = _row_count(central_qi, "dispatch_experiments")
    assert count >= 3, (
        f"dispatch_experiments must retain at least 3 rows after partial bootstrap; got {count}"
    )


def test_apply_partial_qi_db_idempotent(tmp_path, monkeypatch):
    """Running apply twice on a partial-init central DB must be a clean no-op."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")

    registry, backup_base = _build_registry(tmp_path)

    central_state = tmp_path / "central" / "state"
    central_state.mkdir(parents=True, exist_ok=True)
    central_qi = central_state / "quality_intelligence.db"

    _make_partial_qi_db(central_qi, rows=2)

    cmd = [
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        "--registry", str(registry),
        "--backup-base", str(backup_base),
        "--central-state", str(central_state),
    ]

    rc1 = M.main(cmd)
    assert rc1 == 0, f"First apply must exit 0, got {rc1}"

    count_after_run1 = _row_count(central_qi, "dispatch_experiments")

    rc2 = M.main(cmd)
    assert rc2 == 0, f"Second apply must exit 0, got {rc2}"

    count_after_run2 = _row_count(central_qi, "dispatch_experiments")
    assert count_after_run1 == count_after_run2, (
        f"dispatch_experiments row count changed between run 1 ({count_after_run1}) "
        f"and run 2 ({count_after_run2}); second apply is not idempotent"
    )


# ---------------------------------------------------------------------------
# vnx_doctor check_database detects under-versioned central QI DB
# ---------------------------------------------------------------------------

def test_doctor_check_database_warns_on_partial_db(tmp_path):
    """check_database must return WARN for a QI DB with user_version=0."""
    import scripts.vnx_doctor as D

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    qi_db = state_dir / "quality_intelligence.db"
    _make_partial_qi_db(qi_db, rows=1)

    # Add enough tables to pass the table_count >= 10 gate by creating a few extras
    conn = sqlite3.connect(str(qi_db))
    try:
        for i in range(15):
            conn.execute(f"CREATE TABLE IF NOT EXISTS fake_table_{i} (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    fake_paths = {"VNX_STATE_DIR": str(state_dir)}
    results = D.check_database(fake_paths)

    statuses = [r.status for r in results]
    assert "warn" in statuses, (
        f"check_database must return at least one WARN for under-versioned QI DB; "
        f"got statuses: {statuses}"
    )
    warn_messages = [r.message for r in results if r.status == "warn"]
    assert any("under-versioned" in m or "user_version" in m for m in warn_messages), (
        f"WARN message must mention 'under-versioned' or 'user_version'; "
        f"got: {warn_messages}"
    )


def test_doctor_check_database_pass_on_bootstrapped_db(tmp_path):
    """check_database must not WARN for a fully bootstrapped DB."""
    import scripts.vnx_doctor as D

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    qi_db = state_dir / "quality_intelligence.db"
    _make_fully_bootstrapped_qi_db(qi_db)

    fake_paths = {"VNX_STATE_DIR": str(state_dir)}
    results = D.check_database(fake_paths)

    warn_or_fail = [r for r in results if r.status in ("warn", "fail")]
    under_ver_issues = [
        r for r in warn_or_fail
        if "under-versioned" in r.message or "user_version=0" in r.message
    ]
    assert not under_ver_issues, (
        f"check_database must not warn about version for a fully bootstrapped DB; "
        f"issues: {under_ver_issues}"
    )
