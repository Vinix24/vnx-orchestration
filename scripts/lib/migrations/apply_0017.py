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


def _get_column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return ordered list of column names from PRAGMA table_info."""
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


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


def _strip_inline_unique(ddl: str, col: str) -> str:
    """Remove the UNIQUE keyword from *col*'s inline column-definition line.

    Handles both quoted and unquoted column names. Table-level UNIQUE(col)
    entries are handled by _strip_table_level_single_unique separately.
    """
    lines = ddl.split("\n")
    for i, line in enumerate(lines):
        # Column definitions start with optional whitespace + col name (quoted or bare)
        # followed by whitespace. We match both the inline case on its own line
        # and the ALTER-TABLE-style compact line (, col TYPE...).
        if re.match(r'\s*"?' + re.escape(col) + r'"?\s+', line):
            lines[i] = re.sub(r"\bUNIQUE\b", "", line, flags=re.IGNORECASE)
            lines[i] = re.sub(r"  +", " ", lines[i])
    return "\n".join(lines)


def _strip_inline_references(ddl: str, col: str) -> str:
    """Remove an inline REFERENCES clause from *col*'s column-definition line.

    Matches: REFERENCES table_name (col) with optional whitespace.
    """
    lines = ddl.split("\n")
    for i, line in enumerate(lines):
        if re.match(r'\s*"?' + re.escape(col) + r'"?\s+', line):
            lines[i] = re.sub(
                r"\s*REFERENCES\s+\w+\s*\([^)]*\)",
                "",
                lines[i],
                flags=re.IGNORECASE,
            )
    return "\n".join(lines)


def _strip_table_level_single_unique(ddl: str, col: str) -> str:
    """Remove a table-level UNIQUE(col) constraint (single-column).

    Handles the pattern: ,<ws>UNIQUE(<ws>col<ws>) which appears when the
    constraint was added as a table-level clause rather than inline.
    """
    return re.sub(
        r",\s*\bUNIQUE\s*\(\s*\"?" + re.escape(col) + r"\"?\s*\)",
        "",
        ddl,
        flags=re.IGNORECASE,
    )


def _rebuild_table_dynamic(
    conn: sqlite3.Connection,
    table: str,
    extra_constraints: list[str],
    strip_inline_unique_cols: list[str] | None = None,
    strip_inline_refs_cols: list[str] | None = None,
    strip_single_unique_cols: list[str] | None = None,
) -> None:
    """Rebuild *table* in-place, preserving all columns and data.

    Strategy:
    1. Read current column names via PRAGMA table_info (always accurate).
    2. Read the current DDL from sqlite_master (reflects ALTER TABLE additions).
    3. Rename table reference in DDL to a temporary name.
    4. Strip specified inline UNIQUE / REFERENCES modifiers.
    5. Strip specified table-level UNIQUE(single_col) constraints.
    6. Add extra_constraints (composite UNIQUE, FK) before the closing paren.
    7. CREATE TABLE tmp, INSERT with explicit column list, DROP orig, RENAME tmp.

    The explicit column-list INSERT (not SELECT *) ensures the rebuild works
    regardless of how many columns the source table has accumulated via
    ALTER TABLE ADD COLUMN migrations since this migration was originally written.
    """
    tmp = f"{table}__mig0017"
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')

    col_names = _get_column_names(conn, table)
    ddl = _get_table_ddl(conn, table)

    # Rename table in DDL
    ddl = re.sub(
        r"(?i)(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)"
        r'(?:"?' + re.escape(table) + r'"?)',
        lambda m: m.group(1) + f'"{tmp}"',
        ddl,
        count=1,
    )

    # Strip inline UNIQUE from specified column definition lines
    for col in (strip_inline_unique_cols or []):
        ddl = _strip_inline_unique(ddl, col)

    # Strip inline REFERENCES from specified column definition lines
    for col in (strip_inline_refs_cols or []):
        ddl = _strip_inline_references(ddl, col)

    # Strip table-level UNIQUE(single_col) constraints
    for col in (strip_single_unique_cols or []):
        ddl = _strip_table_level_single_unique(ddl, col)

    # Insert extra constraints before the final closing paren.
    # rfind(')') gives the outermost closing paren that ends CREATE TABLE,
    # even when column defaults contain nested parens like strftime(...).
    last_paren = ddl.rfind(")")
    if last_paren == -1:
        raise sqlite3.OperationalError(
            f"Malformed DDL for table '{table}': no closing paren found"
        )
    constraints_sql = ",\n    ".join(extra_constraints)
    ddl = ddl[:last_paren] + f",\n    {constraints_sql}\n" + ddl[last_paren:]

    conn.execute(ddl)

    # Explicit column-list INSERT — safe for any number of columns
    quoted_cols = ", ".join(f'"{c}"' for c in col_names)
    conn.execute(
        f'INSERT INTO "{tmp}" ({quoted_cols}) SELECT {quoted_cols} FROM "{table}"'
    )

    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
    log.debug("apply_0017: rebuilt table '%s' dynamically (%d cols)", table, len(col_names))


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
        extra_constraints=["UNIQUE(\"dispatch_id\", \"project_id\")"],
        strip_inline_unique_cols=["dispatch_id"],
        strip_single_unique_cols=["dispatch_id"],
    )
    log.info("apply_0017: dispatches rebuilt with composite UNIQUE(dispatch_id, project_id)")


def _rebuild_dispatch_attempts(conn: sqlite3.Connection) -> None:
    """Rebuild dispatch_attempts with composite FK → dispatches(dispatch_id, project_id).

    The original single-column inline REFERENCES is replaced with the composite
    table-level FOREIGN KEY. All other columns (including any added later) are
    preserved dynamically.
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
        strip_inline_refs_cols=["dispatch_id"],
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
        strip_inline_unique_cols=["terminal_id"],
        strip_inline_refs_cols=["dispatch_id"],
        strip_single_unique_cols=["terminal_id"],
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
                # ── 1. worker_states: add project_id (missed in v9) ────────────────
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

                # ── 2. dispatches: dynamic rebuild (UNIQUE(dispatch_id, project_id)) ─
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

                # ── 3. dispatch_attempts: dynamic rebuild (composite FK) ────────────
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

                # ── 5. Version stamp ───────────────────────────────────────────────
                conn.execute(
                    "INSERT OR IGNORE INTO runtime_schema_version (version, description)"
                    " VALUES (12, 'Wave 5 PR-5.3: composite UNIQUE on terminal_leases"
                    " + dispatches; project_id on worker_states')"
                )

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
