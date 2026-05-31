"""apply_0017.py — Wave 5 PR-5.3 multi-tenant lease isolation migration.

Applies the 0017 schema changes to runtime_coordination.db. Adds composite
UNIQUE constraints on terminal_leases and dispatches; adds project_id to
worker_states; fixes dispatch_attempts FK.

Idempotent: reads MAX(version) from runtime_schema_version. Skips if already
at v12 or higher (the version stamped by the migration).

Atomic: all DDL/DML executes in a single explicit BEGIN/COMMIT transaction.
On failure the transaction is rolled back before the exception propagates.

Dynamic rebuild (OI-FIX): the dispatches, dispatch_attempts, and
terminal_leases rebuilds read the ACTUAL column list via PRAGMA table_info
before creating the replacement table. This makes the rebuild safe regardless
of how many columns the live table has — ALTERed-in columns (task_class,
target_type, target_id, channel_origin, intelligence_payload, worker_pid, …)
are preserved automatically. The old approach used a hardcoded 15-column
CREATE TABLE dispatches_v10 with INSERT … SELECT *, which failed with
"table dispatches_v10 has 15 columns but 20 values were supplied" when the
live dispatches table had grown beyond the original column set.

PRAGMA-built DDL (codex round-1): the replacement table DDL is constructed
PROGRAMMATICALLY from PRAGMA table_info (column name/type/notnull/default/pk)
and PRAGMA index_list (origin='u' UNIQUE constraints) — it is NOT produced by
regex-editing the old CREATE TABLE text. Regex-stripping was fragile: it only
matched column definitions anchored at the start of a line, so single-line
DDL (as stored in sqlite_master) or comma-prefixed column styles slipped
through, leaving the old single-column UNIQUE in place while the composite
UNIQUE was added on top — a false-positive "migrated" stamp that silently kept
the pre-migration single-column uniqueness. Building from the live schema
cannot false-positive: the old single-column UNIQUE is provably absent because
it is never copied into the new DDL, while non-replaced UNIQUE constraints
(e.g. dispatch_attempts.attempt_id) are preserved by enumerating index_list.

ADR-005: emits NDJSON audit events to .vnx-data/events/schema_migrations.ndjson
for migration_started, migration_completed, and migration_failed.

Tested via tests/test_schema_0017_migration.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))
from coordination_db import get_connection_for_db

log = logging.getLogger(__name__)

_TARGET_VERSION = 12

_DEFAULT_MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "schemas"
    / "migrations"
    / "0017_multi_tenant_lease_isolation.sql"
)


# ---------------------------------------------------------------------------
# Private schema helpers
# ---------------------------------------------------------------------------

def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if *table* exists and has *column* (PRAGMA table_info)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _get_table_ddl(conn: sqlite3.Connection, table: str) -> str:
    """Return the CREATE TABLE SQL from sqlite_master.

    After ALTER TABLE ADD COLUMN, SQLite updates the stored sql to include
    the new column(s) before the closing paren — so this always reflects
    the current live schema.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        raise sqlite3.OperationalError(
            f"Table '{table}' not found in sqlite_master"
        )
    return row[0]


def _composite_unique_exists(
    conn: sqlite3.Connection,
    table: str,
    columns: frozenset[str],
) -> bool:
    """Return True if *table* has a composite UNIQUE index over exactly *columns*."""
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if not idx[2]:  # unique flag
            continue
        info = conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
        if frozenset(r[2] for r in info) == columns:
            return True
    return False


# Default values that PRAGMA table_info returns as bare keywords — these must
# NOT be wrapped in parentheses when reconstructed.
_BARE_DEFAULT_KEYWORDS = frozenset(
    {"CURRENT_TIME", "CURRENT_DATE", "CURRENT_TIMESTAMP", "NULL", "TRUE", "FALSE"}
)
_NUMERIC_DEFAULT_RE = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")


def _format_default(dflt: str) -> str:
    """Render a `DEFAULT ...` clause from PRAGMA table_info's dflt_value.

    table_info strips the outer parentheses from expression defaults: for
    `DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))` it returns the bare
    expression `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`. SQLite requires
    expression defaults to be parenthesized, so anything that is not a bare
    keyword (CURRENT_TIMESTAMP …), a string/blob literal, a numeric literal, or
    already parenthesized is re-wrapped. Literals are emitted verbatim.
    """
    token = dflt.strip()
    upper = token.upper()
    if upper in _BARE_DEFAULT_KEYWORDS:
        return f"DEFAULT {token}"
    if token.startswith("(") and token.endswith(")"):
        return f"DEFAULT {token}"
    is_string_literal = token.startswith("'") and token.endswith("'")
    is_blob_literal = upper.startswith("X'") and token.endswith("'")
    if is_string_literal or is_blob_literal or _NUMERIC_DEFAULT_RE.match(token):
        return f"DEFAULT {token}"
    return f"DEFAULT ({token})"


def _column_definitions(
    conn: sqlite3.Connection, table: str
) -> tuple[list[str], list[str], list[str]]:
    """Build column-definition SQL fragments from PRAGMA table_info.

    Returns (col_names, column_def_sql, pk_cols). Each column def faithfully
    carries the live type, NOT NULL, DEFAULT, and PRIMARY KEY (inline only for a
    single-column PK; composite PKs are emitted as a table-level clause by the
    caller). AUTOINCREMENT is preserved for an INTEGER single-column primary key
    when the current DDL declares it.

    UNIQUE, REFERENCES, and CHECK are intentionally NOT reconstructed here —
    UNIQUE constraints are handled selectively via _preserved_unique_constraints,
    and the old single-column FK/UNIQUE that 0017 replaces are dropped by simply
    never carrying them over.
    """
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # row: (cid, name, type, notnull, dflt_value, pk)
    pk_cols = [r[1] for r in info if r[5]]
    single_pk = len(pk_cols) == 1
    has_autoincrement = bool(
        re.search(r"AUTOINCREMENT", _get_table_ddl(conn, table), re.IGNORECASE)
    )

    col_names: list[str] = []
    defs: list[str] = []
    for _cid, name, ctype, notnull, dflt, pk in info:
        col_names.append(name)
        parts = [f'"{name}"']
        if ctype:
            parts.append(ctype)
        if pk and single_pk:
            parts.append("PRIMARY KEY")
            if has_autoincrement and (ctype or "").upper() == "INTEGER":
                parts.append("AUTOINCREMENT")
        if notnull:
            parts.append("NOT NULL")
        if dflt is not None:
            parts.append(_format_default(dflt))
        defs.append(" ".join(parts))
    return col_names, defs, pk_cols


def _preserved_unique_constraints(
    conn: sqlite3.Connection,
    table: str,
    drop_column_sets: list[frozenset[str]] | None = None,
) -> list[str]:
    """Return table-level `UNIQUE(...)` clauses to carry over to the new table.

    Enumerates UNIQUE constraints (origin='u') via PRAGMA index_list. Any
    constraint whose exact column-set matches an entry in *drop_column_sets* is
    dropped (it is being replaced by the composite constraint 0017 adds). Every
    other UNIQUE constraint — e.g. dispatch_attempts.attempt_id — is preserved.
    """
    drop_sets = [frozenset(s) for s in (drop_column_sets or [])]
    clauses: list[str] = []
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        name, is_unique = idx[1], idx[2]
        origin = idx[3] if len(idx) > 3 else None
        if not is_unique:
            continue
        # origin 'u' = UNIQUE constraint; 'pk' = primary key (handled via
        # table_info, skip here); 'c' = CREATE [UNIQUE] INDEX (not a table
        # constraint, recreated separately by the migration's index DDL).
        if origin is not None and origin != "u":
            continue
        if origin is None and not name.startswith("sqlite_autoindex_"):
            continue
        cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{name}")').fetchall()]
        if frozenset(cols) in drop_sets:
            continue
        quoted = ", ".join(f'"{c}"' for c in cols)
        clauses.append(f"UNIQUE({quoted})")
    return clauses


def _rebuild_table_dynamic(
    conn: sqlite3.Connection,
    table: str,
    extra_constraints: list[str],
    drop_unique_column_sets: list[frozenset[str]] | None = None,
) -> None:
    """Rebuild *table* in-place from its live schema, preserving columns + data.

    Strategy (no regex-editing of the old DDL — see module docstring):
    1. Read columns via PRAGMA table_info → faithful type/notnull/default/pk.
    2. Read UNIQUE constraints via PRAGMA index_list → preserve all except the
       single-column UNIQUE(s) in *drop_unique_column_sets* (replaced by 0017).
    3. Emit CREATE TABLE <tmp> with those columns, the preserved UNIQUE clauses,
       a table-level PRIMARY KEY clause for composite PKs, and *extra_constraints*
       (the composite UNIQUE / FK that 0017 adds).
    4. INSERT with the explicit pragma-derived column list, DROP orig, RENAME tmp.

    The new DDL is built entirely from the live schema, so the old single-column
    UNIQUE that 0017 replaces is provably absent (never copied) and the rebuild
    works for any column count, single-line DDL, or comma-prefixed column style.
    """
    tmp = f"{table}__mig0017"
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')

    col_names, col_defs, pk_cols = _column_definitions(conn, table)
    preserved_unique = _preserved_unique_constraints(conn, table, drop_unique_column_sets)

    table_constraints: list[str] = []
    if len(pk_cols) > 1:
        quoted_pk = ", ".join(f'"{c}"' for c in pk_cols)
        table_constraints.append(f"PRIMARY KEY({quoted_pk})")
    table_constraints.extend(preserved_unique)
    table_constraints.extend(extra_constraints)

    body = ",\n    ".join(col_defs + table_constraints)
    conn.execute(f'CREATE TABLE "{tmp}" (\n    {body}\n)')

    quoted_cols = ", ".join(f'"{c}"' for c in col_names)
    conn.execute(
        f'INSERT INTO "{tmp}" ({quoted_cols}) SELECT {quoted_cols} FROM "{table}"'
    )

    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
    log.debug(
        "apply_0017: rebuilt '%s' from PRAGMA (%d cols, %d preserved UNIQUE)",
        table,
        len(col_names),
        len(preserved_unique),
    )


# ---------------------------------------------------------------------------
# Per-table rebuild wrappers
# ---------------------------------------------------------------------------

def _rebuild_dispatches(conn: sqlite3.Connection) -> None:
    """Rebuild dispatches with composite UNIQUE(dispatch_id, project_id).

    Preserves all columns present in the live table regardless of how many
    ALTER TABLE ADD COLUMN migrations have run since 0017 was written.
    """
    if _composite_unique_exists(
        conn, "dispatches", frozenset({"dispatch_id", "project_id"})
    ):
        log.info("apply_0017: dispatches composite UNIQUE already present; skipping rebuild")
        return

    _rebuild_table_dynamic(
        conn,
        "dispatches",
        extra_constraints=['UNIQUE("dispatch_id", "project_id")'],
        drop_unique_column_sets=[frozenset({"dispatch_id"})],
    )
    log.info("apply_0017: dispatches rebuilt with composite UNIQUE(dispatch_id, project_id)")


def _rebuild_dispatch_attempts(conn: sqlite3.Connection) -> None:
    """Rebuild dispatch_attempts with composite FK → dispatches(dispatch_id, project_id).

    The original single-column inline REFERENCES is replaced with the composite
    table-level FOREIGN KEY. Inline FKs are never reconstructed from table_info,
    so the old single-column REFERENCES is dropped simply by not carrying it
    over. The attempt_id UNIQUE constraint is preserved automatically (it is not
    in the drop set). All columns — including any added later — are preserved.
    """
    # Check whether the composite FK already exists via sqlite_master DDL inspection
    ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dispatch_attempts'"
    ).fetchone()
    if ddl_row and re.search(
        r"FOREIGN\s+KEY\s*\(.*dispatch_id.*project_id.*\)",
        ddl_row[0],
        re.IGNORECASE | re.DOTALL,
    ):
        log.info(
            "apply_0017: dispatch_attempts composite FK already present; skipping rebuild"
        )
        return

    _rebuild_table_dynamic(
        conn,
        "dispatch_attempts",
        extra_constraints=[
            'FOREIGN KEY ("dispatch_id", "project_id")'
            ' REFERENCES dispatches("dispatch_id", "project_id")',
        ],
    )
    log.info(
        "apply_0017: dispatch_attempts rebuilt with composite FK → dispatches(dispatch_id, project_id)"
    )


def _rebuild_terminal_leases(conn: sqlite3.Connection) -> None:
    """Rebuild terminal_leases with composite UNIQUE(terminal_id, project_id).

    Preserves all columns — including worker_pid added by PR #636 — regardless
    of which columns were present when this migration was originally written.
    The original single-column UNIQUE(terminal_id) is replaced by the composite
    constraint. The inline REFERENCES on dispatch_id is replaced with a
    table-level composite FK.
    """
    if _composite_unique_exists(
        conn, "terminal_leases", frozenset({"terminal_id", "project_id"})
    ):
        log.info(
            "apply_0017: terminal_leases composite UNIQUE already present; skipping rebuild"
        )
        return

    _rebuild_table_dynamic(
        conn,
        "terminal_leases",
        extra_constraints=[
            'FOREIGN KEY ("dispatch_id", "project_id")'
            ' REFERENCES dispatches("dispatch_id", "project_id")',
            'UNIQUE("terminal_id", "project_id")',
        ],
        drop_unique_column_sets=[frozenset({"terminal_id"})],
    )
    log.info(
        "apply_0017: terminal_leases rebuilt with composite UNIQUE(terminal_id, project_id)"
    )


# ---------------------------------------------------------------------------
# Audit event helper
# ---------------------------------------------------------------------------

def _emit_migration_event(vnx_data_dir: Path, event_type: str, payload: dict) -> None:
    events_path = vnx_data_dir / "events" / "schema_migrations.ndjson"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_type": event_type,
        "source": "schema_migration",
        "migration": "0017_multi_tenant_lease_isolation",
        **payload,
    }
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# In-transaction schema-change sequence
# ---------------------------------------------------------------------------

def _apply_schema_changes(conn: sqlite3.Connection) -> None:
    """Execute the full 0017 DDL/DML sequence inside the open transaction.

    Steps, in dependency order:
    1. worker_states.project_id (added if missing) + its index.
    2. dispatches dynamic rebuild (composite UNIQUE) + indexes.
    3. dispatch_attempts dynamic rebuild (composite FK) + indexes.
    4. terminal_leases dynamic rebuild (composite UNIQUE + composite FK) + indexes.
    5. Version stamp to v12.

    dispatches is rebuilt BEFORE terminal_leases because terminal_leases carries
    a composite FK → dispatches(dispatch_id, project_id); the referenced composite
    UNIQUE must exist before the referencing table is created.

    Extracted from apply_migration so that entry point stays well under the
    70-executable-line threshold. The caller owns the BEGIN/COMMIT/ROLLBACK and
    foreign_keys pragma; this helper only issues DDL/DML.
    """
    # ── 1. worker_states: add project_id (missed in v9) ────────────────────
    if not _column_exists(conn, "worker_states", "project_id"):
        conn.execute(
            "ALTER TABLE worker_states ADD COLUMN"
            " project_id TEXT NOT NULL DEFAULT 'vnx-dev'"
        )
        log.info("apply_0017: added worker_states.project_id")
    else:
        log.info(
            "apply_0017: worker_states.project_id already present;"
            " ADD COLUMN skipped (column-guard, OI-095)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_states_project"
        " ON worker_states(project_id)"
    )

    # ── 2. dispatches: dynamic rebuild (UNIQUE(dispatch_id, project_id)) ────
    _rebuild_dispatches(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_state"
        " ON dispatches(state, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_terminal"
        " ON dispatches(terminal_id, state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_created"
        " ON dispatches(created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatches_project"
        " ON dispatches(project_id)"
    )

    # ── 3. dispatch_attempts: dynamic rebuild (composite FK) ───────────────
    _rebuild_dispatch_attempts(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempt_dispatch"
        " ON dispatch_attempts(dispatch_id, attempt_number)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempt_state"
        " ON dispatch_attempts(state, started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempt_terminal"
        " ON dispatch_attempts(terminal_id, started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempt_project"
        " ON dispatch_attempts(project_id)"
    )

    # ── 4. terminal_leases: dynamic rebuild (UNIQUE(terminal_id, project_id)) ─
    _rebuild_terminal_leases(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lease_state"
        " ON terminal_leases(state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lease_dispatch"
        " ON terminal_leases(dispatch_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lease_project"
        " ON terminal_leases(project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lease_terminal_project"
        " ON terminal_leases(terminal_id, project_id)"
    )

    # ── 5. Version stamp ───────────────────────────────────────────────────
    conn.execute(
        "INSERT OR IGNORE INTO runtime_schema_version (version, description)"
        " VALUES (12, 'Wave 5 PR-5.3: composite UNIQUE on terminal_leases"
        " + dispatches; project_id on worker_states')"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply_migration(
    db_path: Path,
    migration_sql_path: Path,
    vnx_data_dir: Path | None = None,
) -> bool:
    """Apply the 0017 migration to db_path.

    Returns True when the migration was applied, False when the DB was
    already at the target version and the migration was skipped.

    The migration_sql_path argument is accepted for API compatibility with
    the auto_apply framework but is no longer used to executescript the
    static SQL file — the rebuild logic is implemented entirely in Python so
    it can dynamically adapt to the actual live column set of each table.

    Raises sqlite3.Error on failure (the failing transaction is rolled back
    before the exception propagates).
    """
    if vnx_data_dir is None:
        vnx_data_dir = Path(db_path).parent.parent

    current_version = 0

    with get_connection_for_db(db_path) as conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT MAX(version) FROM runtime_schema_version")
            row = cur.fetchone()
            current_version = int(row[0]) if (row and row[0] is not None) else 0

            if current_version >= _TARGET_VERSION:
                log.info(
                    "apply_0017: already at v%s (target v%s), skip",
                    current_version,
                    _TARGET_VERSION,
                )
                return False

            _emit_migration_event(
                vnx_data_dir,
                "migration_started",
                {"from_version": current_version, "to_version": _TARGET_VERSION},
            )

            # Switch to manual transaction control so DDL and DML share a single
            # atomic BEGIN/COMMIT. isolation_level=None disables Python's implicit
            # transaction wrapping and lets us issue explicit BEGIN/ROLLBACK/COMMIT.
            conn.isolation_level = None
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")

            try:
                _apply_schema_changes(conn)
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise

            conn.execute("PRAGMA foreign_keys = ON")

            log.info(
                "apply_0017: migrated from v%s to v%s", current_version, _TARGET_VERSION
            )
            _emit_migration_event(
                vnx_data_dir,
                "migration_completed",
                {"from_version": current_version, "to_version": _TARGET_VERSION},
            )
            return True

        except sqlite3.Error as e:
            log.error("apply_0017: error during migration; transaction rolled back")
            _emit_migration_event(
                vnx_data_dir,
                "migration_failed",
                {"from_version": current_version, "error": str(e)},
            )
            raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(
        description="Apply 0017 multi-tenant lease isolation migration"
    )
    p.add_argument("--db", required=True, help="Path to runtime_coordination.db")
    p.add_argument(
        "--migration",
        default=str(_DEFAULT_MIGRATION_SQL),
        help="Path to 0017_multi_tenant_lease_isolation.sql (accepted for API compat)",
    )
    p.add_argument(
        "--vnx-data-dir",
        default=None,
        help="Path to .vnx-data directory for audit events (default: db_path/../..)",
    )
    args = p.parse_args()
    applied = apply_migration(
        Path(args.db),
        Path(args.migration),
        Path(args.vnx_data_dir) if args.vnx_data_dir else None,
    )
    print("applied" if applied else "skipped (already at target version)")
