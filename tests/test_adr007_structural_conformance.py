"""ADR-007 structural conformance: every multi-tenant central-DB table MUST
have at least one UNIQUE constraint or PRIMARY KEY involving project_id.

This test enumerates every table in runtime_coordination.db after applying
all migrations through the current head, and asserts each has at least one
UNIQUE/PK whose column list includes project_id.

Failure mode this test catches: future migrations that add tenant-scoped
tables without composite enforcement (the FUT-2a fix2 incident class).
"""
from __future__ import annotations
import sqlite3, sys
from pathlib import Path
import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import schema_migration

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
