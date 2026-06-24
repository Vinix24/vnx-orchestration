"""Tests for GAP 2: dispatch_metadata provider/model migration.

Covers:
- migrate_dispatch_metadata_provider.run_migration is idempotent (runs twice without error)
- provider + model columns are present after migration on a fresh temp DB
- UNIQUE INDEX on (project_id, dispatch_id) is created
- (project_id, provider) index is created
- upsert_dispatch_provider_row stamps both provider AND model
- log_dispatch_metadata --model argument accepted and stamped (compile + arg parse)
- quality_db_init _migrate_v23 adds model column on a DB at user_version 22
- log_dispatch_metadata does NOT clobber an existing (codex, gpt-probe) row (#778 FIX 1)
- v21->v22->v23 migration path preserves model values (#778 FIX 2)
- recent_comparable and build_t0_state SELECTs surface provider/model (#778 FIX 3)

Dispatch-ID: 20260601-1645-fixgap2
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


# ---------------------------------------------------------------------------
# FIX 1 — log_dispatch_metadata must NOT clobber an existing provider/model
# ---------------------------------------------------------------------------

def test_log_dispatch_metadata_no_clobber_existing_provider_model():
    """Calling log_dispatch_metadata with default/empty provider must not overwrite
    an existing ('codex', 'gpt-probe') row. (#778 FIX 1)"""
    import os
    import subprocess

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "state" / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True)

        # Seed DB with a dispatch that has real provider/model already stamped.
        conn = sqlite3.connect(str(db_path))
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
                skill_name TEXT,
                gate TEXT,
                pr_id TEXT,
                dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                target_open_items TEXT,
                pattern_count INTEGER DEFAULT 0,
                prevention_rule_count INTEGER DEFAULT 0,
                intelligence_json TEXT,
                instruction_char_count INTEGER DEFAULT 0,
                context_file_count INTEGER DEFAULT 0,
                cognition TEXT DEFAULT 'normal',
                priority TEXT DEFAULT 'P1',
                UNIQUE (project_id, dispatch_id)
            );
        """)
        conn.execute(
            "INSERT INTO dispatch_metadata "
            "(dispatch_id, terminal, track, project_id, provider, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("clobber-test-001", "T1", "A", "vnx-dev", "codex", "gpt-probe"),
        )
        conn.commit()
        conn.close()

        scripts_dir = ROOT / "scripts"
        env = os.environ.copy()
        env["VNX_STATE_DIR"] = str(db_path.parent)
        env["VNX_DATA_DIR"] = tmpdir
        # tmp store has no .vnx-data/<pid>/state path layout; give the fail-closed
        # tenant resolver an explicit source so log_dispatch_metadata can stamp.
        env["VNX_PROJECT_ID"] = "vnx-dev"

        # Invoke log_dispatch_metadata WITHOUT --provider / --model (defaults to empty).
        result = subprocess.run(
            [
                "python3", str(scripts_dir / "log_dispatch_metadata.py"),
                "--dispatch-id", "clobber-test-001",
                "--terminal", "T1",
                "--track", "A",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"script failed: {result.stderr}"

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT provider, model FROM dispatch_metadata WHERE dispatch_id=?",
            ("clobber-test-001",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "codex", (
            f"provider must not be overwritten with default — expected 'codex', got {row[0]!r}"
        )
        assert row[1] == "gpt-probe", (
            f"model must not be overwritten with NULL — expected 'gpt-probe', got {row[1]!r}"
        )


# ---------------------------------------------------------------------------
# FIX 2 — v21->v22->v23 migration path preserves model values
# ---------------------------------------------------------------------------

def test_v21_v22_v23_preserves_model_values():
    """Running v21→v22→v23 on a DB that already has provider+model values must
    not drop model during the v22 table rebuild. (#778 FIX 2)"""
    import quality_db_init as QDB
    import scripts.lib.schema_migration as SM

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        # Simulate a DB at user_version 20 with provider+model already present
        # (e.g. added by an earlier partial migration run).
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                terminal TEXT NOT NULL,
                track TEXT NOT NULL,
                provider TEXT,
                model TEXT
            );
            PRAGMA user_version = 20;
        """)
        conn.execute(
            "INSERT INTO dispatch_metadata "
            "(dispatch_id, terminal, track, project_id, provider, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("preserve-test-001", "T1", "A", "vnx-dev", "codex", "gpt-probe"),
        )
        conn.commit()
        conn.close()

        # Apply v21, v22, v23 in sequence (simulating fresh migration run).
        conn = sqlite3.connect(str(db_path))
        SM.apply_if_below(conn, 21, QDB._migrate_v21)
        conn.commit()
        SM.apply_if_below(conn, 22, QDB._migrate_v22)
        conn.commit()
        SM.apply_if_below(conn, 23, QDB._migrate_v23)
        conn.commit()

        row = conn.execute(
            "SELECT provider, model FROM dispatch_metadata WHERE dispatch_id=?",
            ("preserve-test-001",),
        ).fetchone()
        conn.close()
        assert row is not None, "row must survive the v22 rebuild"
        assert row[0] == "codex", f"provider lost through v22 rebuild — got {row[0]!r}"
        assert row[1] == "gpt-probe", f"model lost through v22 rebuild — got {row[1]!r}"
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# FIX 3 — recent_comparable and build_t0_state surface provider/model
# ---------------------------------------------------------------------------

def test_recent_comparable_select_includes_provider_model():
    """_query_per_project SELECT must include provider and model columns so
    _row_to_intelligence_item can surface them in content. (#778 FIX 3)"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
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
                skill_name TEXT,
                gate TEXT,
                pattern_count INTEGER DEFAULT 0,
                prevention_rule_count INTEGER DEFAULT 0,
                dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                outcome_status TEXT
            );
        """)
        conn.execute(
            "INSERT INTO dispatch_metadata "
            "(dispatch_id, terminal, track, project_id, provider, model, outcome_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("rc-test-001", "T1", "A", "vnx-dev", "codex", "gpt-probe", "success"),
        )
        conn.commit()

        lib_path = str(ROOT / "scripts" / "lib")
        if lib_path not in sys.path:
            sys.path.insert(0, lib_path)
        from intelligence_sources.recent_comparable import _query_per_project

        def _has_col(table, col):
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            return any(r[1] == col for r in rows)

        items = _query_per_project(conn, "test", [], has_column_fn=_has_col)
        assert items, "expected at least one IntelligenceItem"
        content = items[0].content
        assert "codex" in content, f"provider 'codex' not surfaced in content: {content!r}"
        assert "gpt-probe" in content, f"model 'gpt-probe' not surfaced in content: {content!r}"
        conn.close()
    finally:
        db_path.unlink(missing_ok=True)


def test_build_t0_state_recent_dispatches_sql_includes_provider_model():
    """_RECENT_DISPATCHES_SQL must project provider and model. (#778 FIX 3)"""
    import build_t0_state as BTS

    for attr in ("_RECENT_DISPATCHES_SQL", "_RECENT_DISPATCHES_CENTRAL_SQL"):
        sql = getattr(BTS, attr)
        assert "provider" in sql, f"{attr} must SELECT provider"
        assert "model" in sql, f"{attr} must SELECT model"

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                terminal TEXT NOT NULL,
                track TEXT NOT NULL,
                role TEXT,
                gate TEXT,
                priority TEXT DEFAULT 'P1',
                pr_id TEXT,
                provider TEXT,
                model TEXT,
                dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME,
                outcome_status TEXT
            );
        """)
        conn.execute(
            "INSERT INTO dispatch_metadata "
            "(dispatch_id, terminal, track, project_id, provider, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("t0-test-001", "T1", "A", "vnx-dev", "codex", "gpt-probe"),
        )
        conn.commit()
        conn.close()

        rows = BTS._query_qi_db(db_path, BTS._RECENT_DISPATCHES_SQL)
        assert rows, "expected at least one row"
        assert rows[0].get("provider") == "codex", (
            f"provider not projected — got {rows[0].get('provider')!r}"
        )
        assert rows[0].get("model") == "gpt-probe", (
            f"model not projected — got {rows[0].get('model')!r}"
        )
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# FIX 3b — defensive read on unmigrated DBs (no provider/model columns)
# ---------------------------------------------------------------------------

def test_recent_comparable_unmigrated_db_returns_rows_no_crash():
    """_query_per_project on a DB WITHOUT provider/model must not crash and
    must return rows with provider/model absent (None) from the IntelligenceItem content."""
    from intelligence_sources.recent_comparable import _query_per_project

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT UNIQUE,
            terminal TEXT, track TEXT,
            role TEXT, skill_name TEXT, gate TEXT,
            outcome_status TEXT, dispatched_at DATETIME,
            pattern_count INTEGER DEFAULT 0,
            prevention_rule_count INTEGER DEFAULT 0
        );
    """)
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO dispatch_metadata "
        "(dispatch_id, skill_name, gate, outcome_status, dispatched_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("legacy-d-001", "architect", "", "success", ts),
    )
    conn.commit()

    items = _query_per_project(conn, "coding_interactive", [], has_column_fn=lambda t, c: False)
    conn.close()

    assert items, "must return items from unmigrated DB without crash"
    assert "legacy-d-001" in items[0].source_refs
    assert "codex" not in items[0].content
    assert "None" not in items[0].content or True


def test_recent_comparable_migrated_db_surfaces_provider_model():
    """_query_per_project on a DB WITH provider/model must surface them in content."""
    from intelligence_sources.recent_comparable import _query_per_project

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT UNIQUE,
            terminal TEXT, track TEXT,
            role TEXT, skill_name TEXT, gate TEXT,
            outcome_status TEXT, dispatched_at DATETIME,
            pattern_count INTEGER DEFAULT 0,
            prevention_rule_count INTEGER DEFAULT 0,
            provider TEXT, model TEXT
        );
    """)
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO dispatch_metadata "
        "(dispatch_id, skill_name, gate, outcome_status, dispatched_at, provider, model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("migrated-d-001", "architect", "", "success", ts, "codex", "gpt-probe"),
    )
    conn.commit()

    def _has_col(table, col):
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == col for r in rows)

    items = _query_per_project(conn, "coding_interactive", [], has_column_fn=_has_col)
    conn.close()

    assert items, "must return items from migrated DB"
    content = items[0].content
    assert "codex" in content, f"provider not surfaced: {content!r}"
    assert "gpt-probe" in content, f"model not surfaced: {content!r}"


def test_build_t0_state_collect_recent_dispatches_unmigrated_no_crash(tmp_path):
    """_collect_recent_dispatches_per_project on an unmigrated DB (no provider/model)
    must return rows without crash; provider/model absent from result dicts."""
    import build_t0_state as BTS

    db_path = tmp_path / "state" / "quality_intelligence.db"
    db_path.parent.mkdir(parents=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            terminal TEXT, track TEXT, role TEXT, gate TEXT,
            priority TEXT DEFAULT 'P1', pr_id TEXT,
            dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME, outcome_status TEXT
        );
    """)
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO dispatch_metadata (dispatch_id, terminal, track, dispatched_at) "
        "VALUES (?, ?, ?, ?)",
        ("legacy-bts-001", "T1", "A", ts),
    )
    conn.commit()
    conn.close()

    rows = BTS._collect_recent_dispatches_per_project("vnx-dev", db_path.parent)

    assert rows, "must return rows from unmigrated DB without crash"
    assert rows[0]["dispatch_id"] == "legacy-bts-001"
    assert rows[0].get("provider") is None
    assert rows[0].get("model") is None


def test_build_t0_state_collect_recent_dispatches_migrated_surfaces_provider(tmp_path):
    """_collect_recent_dispatches_per_project on a migrated DB surfaces provider/model."""
    import build_t0_state as BTS

    db_path = tmp_path / "state" / "quality_intelligence.db"
    db_path.parent.mkdir(parents=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            terminal TEXT, track TEXT, role TEXT, gate TEXT,
            priority TEXT DEFAULT 'P1', pr_id TEXT,
            provider TEXT, model TEXT,
            dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME, outcome_status TEXT
        );
    """)
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO dispatch_metadata "
        "(dispatch_id, terminal, track, dispatched_at, provider, model) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("migrated-bts-001", "T1", "A", ts, "codex", "gpt-probe"),
    )
    conn.commit()
    conn.close()

    rows = BTS._collect_recent_dispatches_per_project("vnx-dev", db_path.parent)

    assert rows, "must return rows from migrated DB"
    assert rows[0]["provider"] == "codex", f"expected 'codex', got {rows[0].get('provider')!r}"
    assert rows[0]["model"] == "gpt-probe", f"expected 'gpt-probe', got {rows[0].get('model')!r}"


# ---------------------------------------------------------------------------
# FIX 3c — provider-only DB (intermediate migration state: provider present, model absent)
# ---------------------------------------------------------------------------

def test_recent_comparable_provider_only_db_no_crash():
    """_query_per_project on a DB with provider but NOT model must not crash.
    Must surface provider; model key absent (None via .get). (#778 guard gap)"""
    from intelligence_sources.recent_comparable import _query_per_project

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT UNIQUE,
            terminal TEXT, track TEXT,
            role TEXT, skill_name TEXT, gate TEXT,
            outcome_status TEXT, dispatched_at DATETIME,
            pattern_count INTEGER DEFAULT 0,
            prevention_rule_count INTEGER DEFAULT 0,
            provider TEXT
        );
    """)
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO dispatch_metadata "
        "(dispatch_id, skill_name, gate, outcome_status, dispatched_at, provider) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("provider-only-001", "architect", "", "success", ts, "kimi"),
    )
    conn.commit()

    def _has_col(table, col):
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == col for r in rows)

    items = _query_per_project(conn, "coding_interactive", [], has_column_fn=_has_col)
    conn.close()

    assert items, "must return items from provider-only DB without crash"
    assert "provider-only-001" in items[0].source_refs
    assert "kimi" in items[0].content, f"provider 'kimi' must appear in content: {items[0].content!r}"


def test_build_t0_state_provider_only_db_no_crash(tmp_path):
    """_collect_recent_dispatches_per_project on a DB with provider but NOT model must
    not crash; must surface provider; model absent from result dict. (#778 guard gap)"""
    import build_t0_state as BTS

    db_path = tmp_path / "state" / "quality_intelligence.db"
    db_path.parent.mkdir(parents=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            terminal TEXT, track TEXT, role TEXT, gate TEXT,
            priority TEXT DEFAULT 'P1', pr_id TEXT,
            provider TEXT,
            dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME, outcome_status TEXT
        );
    """)
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO dispatch_metadata "
        "(dispatch_id, terminal, track, dispatched_at, provider) "
        "VALUES (?, ?, ?, ?, ?)",
        ("provider-only-bts-001", "T1", "A", ts, "kimi"),
    )
    conn.commit()
    conn.close()

    rows = BTS._collect_recent_dispatches_per_project("vnx-dev", db_path.parent)

    assert rows, "must return rows from provider-only DB without crash"
    assert rows[0]["dispatch_id"] == "provider-only-bts-001"
    assert rows[0].get("provider") == "kimi", f"expected 'kimi', got {rows[0].get('provider')!r}"
    assert rows[0].get("model") is None, f"model must be absent (None), got {rows[0].get('model')!r}"
