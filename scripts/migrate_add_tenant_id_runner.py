#!/usr/bin/env python3
"""
migrate_add_tenant_id_runner.py

Adds tenant_id TEXT NOT NULL to a 50M-row events table in three phases:

  Phase 0  — create migration_log audit table (idempotent)
  Phase 1  — ADD COLUMN tenant_id TEXT (idempotent: skipped if column exists)
  Phase 2  — batched backfill, 10 000 rows/tx, resume-safe
  Phase 3  — NOT NULL enforcement via 12-step SQLite table reconstruction

Concurrent-write safety: WAL journal mode allows readers throughout Phase 2.
Phase 3 requires a brief exclusive lock; announce downtime window if needed.

Usage:
  python3 scripts/migrate_add_tenant_id_runner.py path/to/events.db
  python3 scripts/migrate_add_tenant_id_runner.py path/to/events.db \\
      --tenant default_tenant --batch-size 10000
  python3 scripts/migrate_add_tenant_id_runner.py path/to/events.db --rollback
"""

import argparse
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIGRATION_NAME = "add_tenant_id_to_events_v1"
BATCH_SIZE = 10_000
PROGRESS_EVERY = 100          # audit + log line every N batches
DEFAULT_TENANT = "default"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# ---------------------------------------------------------------------------
# Schema introspection helpers
# ---------------------------------------------------------------------------

def column_info(conn: sqlite3.Connection, table: str) -> list:
    """Return list of (cid, name, type, notnull, dflt_value, pk)."""
    return conn.execute(f"PRAGMA table_info({table})").fetchall()


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in column_info(conn, table))


def column_is_not_null(conn: sqlite3.Connection, table: str, column: str) -> bool:
    for row in column_info(conn, table):
        if row[1] == column:
            return bool(row[3])
    return False


def get_index_ddl(conn: sqlite3.Connection, table: str) -> list:
    """Return CREATE INDEX statements for user-defined indexes on table."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    return [row[0] for row in rows]


def get_trigger_ddl(conn: sqlite3.Connection, table: str) -> list:
    """Return CREATE TRIGGER statements for triggers on table."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'trigger' AND tbl_name = ? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    return [row[0] for row in rows]


def get_create_table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Table '{table}' not found in sqlite_master.")
    return row[0]


def build_not_null_ddl(original_sql: str, column: str, new_name: str) -> str:
    """
    Return a modified CREATE TABLE statement that:
      - uses new_name as the table name
      - adds NOT NULL to the target column

    Strategy: capture the full column definition suffix (up to the next comma or
    closing paren) and check whether NOT NULL is already present before inserting.
    This avoids double-adding NOT NULL when the migration is re-run.
    """
    modified = re.sub(
        r"(?i)CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+)",
        f"CREATE TABLE {new_name}",
        original_sql,
        count=1,
    )

    # Capture: column_name + type-word + everything up to the next , or )
    # group(1) = "tenant_id TEXT", group(2) = rest of definition (" NOT NULL" etc.)
    pattern = re.compile(
        r"(\b" + re.escape(column) + r"\b\s+\w+)([^,)]*)",
        re.IGNORECASE,
    )

    def add_not_null(m: re.Match) -> str:
        col_and_type = m.group(1)
        suffix = m.group(2)
        if re.search(r"\bNOT\s+NULL\b", suffix, re.IGNORECASE):
            return m.group(0)  # already has NOT NULL — return unchanged
        return f"{col_and_type} NOT NULL{suffix}"

    modified = pattern.sub(add_not_null, modified, count=1)
    return modified


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def setup_audit_table(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS migration_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            migration TEXT    NOT NULL,
            event     TEXT    NOT NULL,
            detail    TEXT,
            row_count INTEGER,
            ts        TEXT    NOT NULL
                            DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
        """
    )
    conn.execute("COMMIT")
    log.info("Audit table: ready.")


def log_event(
    conn: sqlite3.Connection,
    event: str,
    detail: str = None,
    row_count: int = None,
) -> None:
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO migration_log (migration, event, detail, row_count) "
        "VALUES (?, ?, ?, ?)",
        (MIGRATION_NAME, event, detail, row_count),
    )
    conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Phase 1: Add nullable column
# ---------------------------------------------------------------------------

def phase1_add_column(conn: sqlite3.Connection) -> None:
    if column_exists(conn, "events", "tenant_id"):
        log.info("Phase 1: tenant_id already exists — skipping ADD COLUMN.")
        return
    conn.execute("BEGIN")
    conn.execute("ALTER TABLE events ADD COLUMN tenant_id TEXT")
    conn.execute("COMMIT")
    log.info("Phase 1: tenant_id column added (nullable).")


# ---------------------------------------------------------------------------
# Phase 2: Batched backfill
# ---------------------------------------------------------------------------

def get_resume_rowid(conn: sqlite3.Connection) -> int:
    """Highest rowid already backfilled (0 = start from beginning)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM events WHERE tenant_id IS NOT NULL"
    ).fetchone()
    return row[0]


def get_max_rowid(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM events"
    ).fetchone()
    return row[0]


def phase2_backfill(
    conn: sqlite3.Connection,
    default_tenant: str,
    batch_size: int = BATCH_SIZE,
) -> int:
    resume_rowid = get_resume_rowid(conn)
    max_rowid = get_max_rowid(conn)

    if max_rowid == 0:
        log.info("Phase 2: events table is empty — nothing to backfill.")
        return 0

    if resume_rowid >= max_rowid:
        log.info("Phase 2: all rows already backfilled — skipping.")
        return 0

    start_rowid = resume_rowid + 1
    total_rows = max_rowid - resume_rowid
    log.info(
        "Phase 2: backfilling rowid %d..%d (%d rows) in batches of %d.",
        start_rowid, max_rowid, total_rows, batch_size,
    )

    total_migrated = 0
    batch_num = 0
    current = start_rowid
    t_start = time.monotonic()

    while current <= max_rowid:
        batch_end = current + batch_size - 1

        conn.execute("BEGIN")
        result = conn.execute(
            """
            UPDATE events
            SET    tenant_id = ?
            WHERE  rowid BETWEEN ? AND ?
              AND  tenant_id IS NULL
            """,
            (default_tenant, current, batch_end),
        )
        rows_this_batch = result.rowcount
        conn.execute("COMMIT")

        total_migrated += rows_this_batch
        batch_num += 1
        current = batch_end + 1

        if batch_num % PROGRESS_EVERY == 0:
            elapsed = time.monotonic() - t_start
            pct = min(
                100.0,
                (current - start_rowid) / max(1, total_rows) * 100,
            )
            rate = total_migrated / max(1, elapsed)
            remaining_rows = max(0, max_rowid - current + 1)
            eta_s = remaining_rows / max(1, rate)
            log.info(
                "Batch %d: rowid cursor %d / %d  (%.1f%%)  "
                "migrated=%d  rate=%.0f rows/s  ETA ~%.0fs",
                batch_num, current, max_rowid, pct,
                total_migrated, rate, eta_s,
            )
            log_event(
                conn,
                "batch_progress",
                detail=f"batch={batch_num} cursor={current} pct={pct:.1f}",
                row_count=total_migrated,
            )

    log.info(
        "Phase 2: backfill complete. Total rows migrated: %d in %d batches.",
        total_migrated, batch_num,
    )
    return total_migrated


# ---------------------------------------------------------------------------
# Phase 3: NOT NULL enforcement via table reconstruction
# ---------------------------------------------------------------------------

TMP_TABLE = "events_migration_new"


def phase3_enforce_not_null(conn: sqlite3.Connection) -> None:
    if column_is_not_null(conn, "events", "tenant_id"):
        log.info("Phase 3: tenant_id already NOT NULL — skipping reconstruction.")
        return

    # Verify no NULLs remain before enforcing NOT NULL
    null_count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE tenant_id IS NULL"
    ).fetchone()[0]
    if null_count > 0:
        raise RuntimeError(
            f"Phase 3 aborted: {null_count} rows still have tenant_id IS NULL. "
            "Complete Phase 2 backfill before enforcing NOT NULL."
        )

    original_sql = get_create_table_sql(conn, "events")
    new_table_sql = build_not_null_ddl(original_sql, "tenant_id", TMP_TABLE)

    # Collect column names for the INSERT ... SELECT
    cols = column_info(conn, "events")
    col_names = ", ".join(row[1] for row in cols)

    # Collect indexes and triggers to recreate after reconstruction
    indexes = get_index_ddl(conn, "events")
    triggers = get_trigger_ddl(conn, "events")

    log.info(
        "Phase 3: reconstructing events table "
        "(%d columns, %d indexes, %d triggers).",
        len(cols), len(indexes), len(triggers),
    )

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    try:
        conn.execute(f"DROP TABLE IF EXISTS {TMP_TABLE}")
        conn.execute(new_table_sql)
        conn.execute(
            f"INSERT INTO {TMP_TABLE} SELECT {col_names} FROM events"
        )
        conn.execute("DROP TABLE events")
        conn.execute(f"ALTER TABLE {TMP_TABLE} RENAME TO events")
        for idx_sql in indexes:
            conn.execute(idx_sql)
        for trg_sql in triggers:
            conn.execute(trg_sql)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys = ON")
        raise
    conn.execute("PRAGMA foreign_keys = ON")

    log.info("Phase 3: NOT NULL constraint enforced. Table reconstruction complete.")


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def rollback(conn: sqlite3.Connection) -> None:
    """Remove tenant_id column. Uses DROP COLUMN (SQLite 3.35+) when available,
    falls back to table reconstruction on older versions."""

    if not column_exists(conn, "events", "tenant_id"):
        log.info("Rollback: tenant_id does not exist — nothing to undo.")
        log_event(conn, "rollback", detail="column_not_found_noop")
        return

    sqlite_version = tuple(
        int(x) for x in conn.execute("SELECT sqlite_version()").fetchone()[0].split(".")
    )
    supports_drop_column = sqlite_version >= (3, 35, 0)

    if supports_drop_column:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE events DROP COLUMN tenant_id")
        conn.execute("COMMIT")
        log.info("Rollback: tenant_id dropped via ALTER TABLE DROP COLUMN.")
    else:
        log.warning(
            "SQLite %s < 3.35.0: DROP COLUMN unsupported. "
            "Using table reconstruction for rollback.",
            ".".join(str(x) for x in sqlite_version),
        )
        original_sql = get_create_table_sql(conn, "events")
        cols = column_info(conn, "events")
        kept_cols = [row for row in cols if row[1] != "tenant_id"]
        col_names = ", ".join(row[1] for row in kept_cols)

        # Build CREATE TABLE without tenant_id column
        new_sql = re.sub(
            r"(?i)CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+)",
            f"CREATE TABLE {TMP_TABLE}",
            original_sql,
            count=1,
        )
        # Remove the tenant_id line from the DDL
        new_sql = re.sub(
            r",?\s*\btenant_id\b[^\n,)]*",
            "",
            new_sql,
            flags=re.IGNORECASE,
        )

        indexes = get_index_ddl(conn, "events")
        triggers = get_trigger_ddl(conn, "events")

        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            conn.execute(f"DROP TABLE IF EXISTS {TMP_TABLE}")
            conn.execute(new_sql)
            conn.execute(
                f"INSERT INTO {TMP_TABLE} SELECT {col_names} FROM events"
            )
            conn.execute("DROP TABLE events")
            conn.execute(f"ALTER TABLE {TMP_TABLE} RENAME TO events")
            for idx_sql in indexes:
                # Skip indexes that reference tenant_id
                if "tenant_id" not in idx_sql.lower():
                    conn.execute(idx_sql)
            for trg_sql in triggers:
                conn.execute(trg_sql)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            conn.execute("PRAGMA foreign_keys = ON")
            raise
        conn.execute("PRAGMA foreign_keys = ON")
        log.info("Rollback: tenant_id removed via table reconstruction.")

    log_event(conn, "rollback", detail="tenant_id_removed")
    log.info("Rollback complete.")


# ---------------------------------------------------------------------------
# Main migration orchestrator
# ---------------------------------------------------------------------------

def run_migration(
    db_path: str,
    default_tenant: str = DEFAULT_TENANT,
    batch_size: int = BATCH_SIZE,
) -> None:
    conn = open_db(db_path)
    try:
        setup_audit_table(conn)
        log_event(
            conn, "start",
            detail=f"db={db_path} batch_size={batch_size} default_tenant={default_tenant}",
        )

        phase1_add_column(conn)
        total = phase2_backfill(conn, default_tenant, batch_size)
        log_event(conn, "backfill_complete", row_count=total)

        phase3_enforce_not_null(conn)
        log_event(conn, "not_null_enforced", detail="reconstruction_complete")

        log_event(conn, "finish", detail="not_null_enforced=true", row_count=total)
        log.info("Migration complete. %d total rows migrated.", total)
    except Exception as exc:
        log.error("Migration failed: %s", exc)
        try:
            log_event(conn, "error", detail=str(exc))
        except Exception:
            pass
        sys.exit(1)
    finally:
        conn.close()


def run_rollback(db_path: str) -> None:
    conn = open_db(db_path)
    try:
        setup_audit_table(conn)
        rollback(conn)
    except Exception as exc:
        log.error("Rollback failed: %s", exc)
        sys.exit(1)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Add tenant_id NOT NULL to the events table.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("db", help="Path to the SQLite database file.")
    p.add_argument(
        "--tenant",
        default=DEFAULT_TENANT,
        metavar="VALUE",
        help=f"Default tenant_id value for backfill (default: {DEFAULT_TENANT!r}).",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        metavar="N",
        help=f"Rows per transaction (default: {BATCH_SIZE}).",
    )
    p.add_argument(
        "--rollback",
        action="store_true",
        help="Undo the migration: remove the tenant_id column.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.rollback:
        run_rollback(args.db)
    else:
        run_migration(args.db, default_tenant=args.tenant, batch_size=args.batch_size)
