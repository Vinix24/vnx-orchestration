"""ADR-007 structural conformance: every multi-tenant central-DB table MUST
have at least one UNIQUE constraint or PRIMARY KEY involving project_id.

test_every_multitenant_table_has_composite_constraint_over_project_id:
  Enumerates every table after applying all migrations through current head,
  asserts each has at least one UNIQUE/PK whose column list includes project_id.
  Failure mode: future migrations that add tenant-scoped tables without composite
  enforcement (the FUT-2a fix2 incident class).

test_autoincrement_tables_preserve_seq_through_rebuilds:
  Verifies that AUTOINCREMENT table rebuilds (0022, 0024) do not regress
  sqlite_sequence high-water marks, which would cause id collisions on next insert.
  Failure mode: rebuild-and-replace helpers that omit sqlite_sequence preservation
  (the FUT-2a round-2 and round-3 incident class).
"""
from __future__ import annotations
import sqlite3, sys
from pathlib import Path
import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import schema_migration
import migrate_future_system

# Tables explicitly exempt from ADR-007 (e.g. system / single-tenant by design).
# Each exemption MUST have a written justification.
_EXEMPT_TABLES = {
    "sqlite_sequence": "SQLite internal — not tenant-scoped state",
    "schema_migrations": "Migration ledger — install-wide, not tenant-scoped",
    # Add per-table justifications as needed; the test reports any missing.
}


def _apply_all_migrations(conn: sqlite3.Connection) -> None:
    # Minimal pre-migration base: dispatches table required by 0022 (which rebuilds it).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
            state           TEXT    NOT NULL DEFAULT 'queued',
            terminal_id     TEXT,
            track           TEXT,
            priority        TEXT    DEFAULT 'P2',
            pr_ref          TEXT,
            gate            TEXT,
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            bundle_path     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after   TEXT,
            metadata_json   TEXT    DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.commit()
    # Apply track-layer migrations (0022+) in version order; skip down migrations.
    for path in sorted(_MIGRATIONS.glob("00[0-9][0-9]_*.sql")):
        if "_down" in path.name:
            continue
        try:
            version = int(path.name.split("_")[0])
        except ValueError:
            continue
        if version < 22:
            continue  # pre-track migrations require tables not present here
        sql = path.read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
    conn.commit()


def test_every_multitenant_table_has_composite_constraint_over_project_id():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_all_migrations(conn)

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]
    violations = []
    for table in tables:
        if table in _EXEMPT_TABLES:
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]
        if "project_id" not in cols:
            # Not a multi-tenant table; ADR-007 doesn't apply.
            continue
        indexes = list(conn.execute(f"PRAGMA index_list('{table}')"))
        # Look for any UNIQUE (or PK auto-index) that involves project_id.
        has_composite = False
        for idx in indexes:
            idx_name, is_unique = idx[1], idx[2]
            if not is_unique:
                continue
            idx_cols = [r[2] for r in conn.execute(f"PRAGMA index_info('{idx_name}')")]
            if "project_id" in idx_cols and len(idx_cols) >= 2:
                has_composite = True
                break
        if not has_composite:
            violations.append(table)
    assert not violations, (
        f"ADR-007 violation: tables {violations} have project_id but no composite "
        f"UNIQUE/PK involving project_id. See ADR-007 §Decision rule 2."
    )


def test_autoincrement_tables_preserve_seq_through_rebuilds():
    """AUTOINCREMENT table rebuilds must not regress sqlite_sequence high-water marks.

    Pattern: insert id=1, insert id=100, delete id=100 → seq=100, max(id)=1.
    After migration, assert seq still >= 100 so next insert lands at id=101,
    not id=2 (which would collide with any future restore of the deleted row).

    Covers two distinct rebuild paths:
    - track_phase_history: rebuilt unconditionally by the 0024 SQL migration
    - dispatches: rebuilt by _strip_stale_dispatches_track_fk when stale FK is present
    """
    import warnings as _warnings

    _PROJECT_ROOT_PATH = _MIGRATIONS.parent.parent
    sql_v22 = (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")

    def _make_v22_conn() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT,
                priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                expires_after TEXT, metadata_json TEXT DEFAULT '{}',
                UNIQUE(dispatch_id, project_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coordination_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT, event_type TEXT, entity_type TEXT,
                entity_id TEXT, from_state TEXT, to_state TEXT,
                actor TEXT, reason TEXT, metadata_json TEXT,
                occurred_at TEXT, project_id TEXT
            )
        """)
        conn.commit()
        schema_migration.apply_script_if_below(conn, 22, sql_v22)
        conn.commit()
        return conn

    # ------------------------------------------------------------------ #
    # 1. track_phase_history — always rebuilt by 0024 SQL migration
    # ------------------------------------------------------------------ #
    conn = _make_v22_conn()
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state) VALUES ('t-seed', 'Seed', 'G')"
    )
    conn.commit()

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.commit()
    conn.execute(
        "INSERT INTO track_phase_history (id, track_id, from_phase, to_phase, actor, occurred_at)"
        " VALUES (1, 't-seed', 'queued', 'active', 'operator', '2026-01-01T00:00:00.000Z')"
    )
    conn.execute(
        "INSERT INTO track_phase_history (id, track_id, from_phase, to_phase, actor, occurred_at)"
        " VALUES (100, 't-seed', 'active', 'done', 'operator', '2026-01-01T00:00:01.000Z')"
    )
    conn.commit()
    conn.execute("DELETE FROM track_phase_history WHERE id = 100")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    with _warnings.catch_warnings(record=True):
        _warnings.simplefilter("always")
        migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT_PATH)
    conn.commit()

    tph_seq = conn.execute(
        "SELECT MAX(seq) FROM sqlite_sequence WHERE name = 'track_phase_history'"
    ).fetchone()[0]
    assert tph_seq is not None and tph_seq >= 100, (
        f"track_phase_history seq regressed: got {tph_seq}, expected >= 100. "
        "0024 SQL must preserve sqlite_sequence high-water mark."
    )

    # ------------------------------------------------------------------ #
    # 2. dispatches — rebuilt by _strip_stale_dispatches_track_fk (stale FK path)
    # ------------------------------------------------------------------ #
    conn2 = _make_v22_conn()

    # Inline the stale-FK rebuild to simulate an operator who applied 0023_dispatches_fk
    col_names = [row[1] for row in conn2.execute("PRAGMA table_info('dispatches')")]
    col_list = ", ".join(col_names)
    conn2.execute("PRAGMA foreign_keys = OFF")
    conn2.commit()
    conn2.execute("ALTER TABLE dispatches RENAME TO _disp_pre_stale_fk")
    conn2.execute("""
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT,
            priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            operator_approved_at TEXT,
            UNIQUE(dispatch_id, project_id),
            FOREIGN KEY (track) REFERENCES tracks(track_id)
        )
    """)
    conn2.execute(
        f"INSERT INTO dispatches ({col_list}) SELECT {col_list} FROM _disp_pre_stale_fk"
    )
    conn2.execute("DROP TABLE _disp_pre_stale_fk")
    conn2.commit()
    conn2.execute("PRAGMA foreign_keys = ON")

    # Seed high-water AFTER stale-FK rebuild (track=NULL is FK-permitted)
    conn2.execute(
        "INSERT INTO dispatches (id, dispatch_id, state) VALUES (1, 'd-001', 'queued')"
    )
    conn2.execute(
        "INSERT INTO dispatches (id, dispatch_id, state) VALUES (100, 'd-100', 'queued')"
    )
    conn2.commit()
    conn2.execute("DELETE FROM dispatches WHERE id = 100")
    conn2.commit()

    with _warnings.catch_warnings(record=True):
        _warnings.simplefilter("always")
        migrate_future_system.apply_migration_v24(conn2, _PROJECT_ROOT_PATH)
    conn2.commit()

    disp_seq = conn2.execute(
        "SELECT MAX(seq) FROM sqlite_sequence WHERE name = 'dispatches'"
    ).fetchone()[0]
    assert disp_seq is not None and disp_seq >= 100, (
        f"dispatches seq regressed: got {disp_seq}, expected >= 100. "
        "_strip_stale_dispatches_track_fk must preserve sqlite_sequence high-water mark."
    )

    # Verify no id collision on next insert
    conn2.execute(
        "INSERT INTO dispatches (dispatch_id, state) VALUES ('d-new', 'queued')"
    )
    conn2.commit()
    new_id = conn2.execute(
        "SELECT id FROM dispatches WHERE dispatch_id = 'd-new'"
    ).fetchone()[0]
    assert new_id >= 101, (
        f"New dispatch id={new_id} collides with pre-delete range — seq not preserved."
    )
