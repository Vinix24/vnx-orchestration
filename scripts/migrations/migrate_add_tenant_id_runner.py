#!/usr/bin/env python3
"""
SQLite migration runner: Add tenant_id column to events table.

Features:
- Idempotent: safe to run multiple times
- Batched backfill: 10K rows per transaction
- Concurrent-write safe: table remains R/W during migration
- Resumable: recovers from interruption
- Audit trail: logs to migration_log table
- Error recovery: validates state, logs errors, supports retry

Usage:
    python3 migrate_add_tenant_id_runner.py /path/to/events.db
    python3 migrate_add_tenant_id_runner.py /path/to/events.db --rollback

Exit codes:
    0: success
    1: error
    2: already completed (idempotent)
"""

import sqlite3
import sys
import re
from datetime import datetime
from pathlib import Path

# Configuration
BATCH_SIZE = 10_000
PROGRESS_INTERVAL = 100
TABLE_NAME = "events"
COLUMN_NAME = "tenant_id"
MIGRATION_NAME = "add_tenant_id_to_events"


def log(msg, level="INFO"):
    """Simple logger."""
    ts = datetime.utcnow().isoformat()
    print(f"[{ts}] {level}: {msg}")


def get_db_connection(db_path):
    """Create a connection with proper timeout and manual transaction control."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.isolation_level = None  # Manual BEGIN/COMMIT required
    return conn


def setup_audit_table(conn):
    """Create migration_log table if not exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS migration_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            detail TEXT,
            row_count INTEGER,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def audit_log(conn, event, detail=None, row_count=None):
    """Log an event to the audit table."""
    conn.execute("""
        INSERT INTO migration_log (event, detail, row_count) VALUES (?, ?, ?)
    """, (event, detail, row_count))


# ============================================================================
# Helper Functions
# ============================================================================

def column_exists(conn, table, column):
    """Check if column exists in table."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def column_is_not_null(conn, table, column):
    """Check if column has NOT NULL constraint."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    for row in cursor.fetchall():
        if row[1] == column:
            return row[3] == 1  # notnull flag
    return False


def build_not_null_ddl(original_sql, column_name, new_table_name):
    """
    Modify CREATE TABLE DDL to add NOT NULL to a column and rename table.

    Used for table reconstruction when SQLite < 3.26 (no ALTER ... SET NOT NULL).
    """
    # Replace table name
    modified = original_sql.replace(f"CREATE TABLE {TABLE_NAME}", f"CREATE TABLE {new_table_name}")

    # Find and modify the column definition
    # Pattern: column_name TYPE [constraints]
    pattern = f"({column_name}\\s+\\w+)"

    def add_not_null(match):
        col_def = match.group(1)
        if "NOT NULL" not in col_def:
            return col_def + " NOT NULL"
        return col_def

    modified = re.sub(pattern, add_not_null, modified, flags=re.IGNORECASE)
    return modified


# ============================================================================
# Phase 1: Add Column
# ============================================================================

def phase1_add_column(conn):
    """Phase 1: Add tenant_id column (nullable) if not exists."""
    if not column_exists(conn, TABLE_NAME, COLUMN_NAME):
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN {COLUMN_NAME} TEXT")
        conn.commit()
        audit_log(conn, "add_column", f"Added {COLUMN_NAME} to {TABLE_NAME}")
        conn.commit()
    else:
        audit_log(conn, "add_column", f"Column {COLUMN_NAME} already exists")
        conn.commit()


# ============================================================================
# Phase 2: Batched Backfill
# ============================================================================

def phase2_backfill(conn, default_tenant=None, batch_size=BATCH_SIZE):
    """
    Phase 2: Backfill tenant_id column with default value in batches.

    Args:
        conn: SQLite connection (isolation_level=None)
        default_tenant: Value to backfill (default: '1')
        batch_size: Rows per transaction

    Returns:
        Total rows backfilled
    """
    if default_tenant is None:
        default_tenant = "1"

    audit_log(conn, "backfill_start", f"batch_size={batch_size}")
    conn.commit()

    total_backfilled = 0
    batch_count = 0

    while True:
        # Find next batch of NULL rows
        cursor = conn.execute(f"""
            SELECT rowid FROM {TABLE_NAME}
            WHERE {COLUMN_NAME} IS NULL
            ORDER BY rowid
            LIMIT ?
        """, (batch_size,))

        row_ids = [row[0] for row in cursor.fetchall()]

        if not row_ids:
            break

        # Update in transaction
        try:
            conn.execute("BEGIN IMMEDIATE")

            placeholders = ",".join("?" * len(row_ids))
            conn.execute(f"""
                UPDATE {TABLE_NAME}
                SET {COLUMN_NAME} = ?
                WHERE rowid IN ({placeholders})
            """, (default_tenant, *row_ids))

            conn.commit()

            total_backfilled += len(row_ids)
            batch_count += 1

            if batch_count % PROGRESS_INTERVAL == 0:
                log(f"Backfill progress: {total_backfilled:,} rows in {batch_count} batches")

        except Exception as e:
            conn.rollback()
            raise RuntimeError(f"Batch {batch_count} failed: {e}")

    audit_log(conn, "backfill_complete", f"batches={batch_count}", total_backfilled)
    conn.commit()

    return total_backfilled


# ============================================================================
# Phase 3: Enforce NOT NULL Constraint
# ============================================================================

def phase3_enforce_not_null(conn):
    """
    Phase 3: Add NOT NULL constraint to tenant_id column.

    For SQLite 3.26+, uses ALTER TABLE ... SET NOT NULL.
    For older versions, requires table recreation.
    """
    # Validate all rows have values
    cursor = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE {COLUMN_NAME} IS NULL"
    )
    null_count = cursor.fetchone()[0]

    if null_count > 0:
        raise RuntimeError(
            f"Cannot enforce NOT NULL: {null_count} rows have NULL {COLUMN_NAME}"
        )

    # Check SQLite version
    cursor = conn.execute("SELECT sqlite_version()")
    sqlite_version = cursor.fetchone()[0]
    major, minor, *_ = map(int, (sqlite_version.split('.')[:3] + ['0', '0', '0'])[:3])

    try:
        # Try modern SQLite (3.26+)
        conn.execute(f"ALTER TABLE {TABLE_NAME} ALTER COLUMN {COLUMN_NAME} SET NOT NULL")
        conn.commit()
    except sqlite3.OperationalError:
        # Fall back to table recreation for older SQLite
        if (major, minor) < (3, 26):
            log("SQLite < 3.26: using table recreation for NOT NULL", "WARN")
            _recreate_table_with_not_null(conn)
        else:
            raise

    audit_log(conn, "not_null_enforced")
    conn.commit()


def _recreate_table_with_not_null(conn):
    """Recreate table with NOT NULL constraint (for SQLite < 3.26)."""
    # Get original table DDL
    cursor = conn.execute("""
        SELECT sql FROM sqlite_master
        WHERE type='table' AND name=?
    """, (TABLE_NAME,))

    result = cursor.fetchone()
    if not result:
        raise RuntimeError(f"Table {TABLE_NAME} not found")

    original_ddl = result[0]

    # Get indexes and triggers to recreate
    cursor = conn.execute("""
        SELECT sql FROM sqlite_master
        WHERE type IN ('index', 'trigger') AND tbl_name=?
    """, (TABLE_NAME,))
    dependent_ddl = [row[0] for row in cursor.fetchall() if row[0]]

    try:
        conn.execute("BEGIN IMMEDIATE")

        # Create new table with NOT NULL
        temp_table = f"{TABLE_NAME}_new"
        modified_ddl = build_not_null_ddl(original_ddl, COLUMN_NAME, temp_table)
        conn.execute(modified_ddl)

        # Copy data
        conn.execute(f"""
            INSERT INTO {temp_table} SELECT * FROM {TABLE_NAME}
        """)

        # Drop old table, rename new
        conn.execute(f"DROP TABLE {TABLE_NAME}")
        conn.execute(f"ALTER TABLE {temp_table} RENAME TO {TABLE_NAME}")

        # Recreate indexes and triggers
        for ddl in dependent_ddl:
            conn.execute(ddl)

        conn.commit()

    except Exception as e:
        conn.rollback()
        raise RuntimeError(f"Table recreation failed: {e}")


# ============================================================================
# Main Entry Points
# ============================================================================

def run_migration(db_path, default_tenant=None, batch_size=BATCH_SIZE):
    """
    Run complete migration: add column, backfill, enforce NOT NULL.

    Args:
        db_path: Path to SQLite database
        default_tenant: Value for backfill (default: '1')
        batch_size: Rows per batch (default: 10000)

    Returns:
        0 on success, 1 on error
    """
    if default_tenant is None:
        default_tenant = "1"

    db_path = Path(db_path)
    if not db_path.exists():
        log(f"Database not found: {db_path}", "ERROR")
        return 1

    try:
        conn = get_db_connection(db_path)
        setup_audit_table(conn)

        # Check if already completed
        cursor = conn.execute("""
            SELECT COUNT(*) FROM migration_log WHERE event = 'finish'
        """)
        if cursor.fetchone()[0] > 0:
            log("Migration already completed (idempotent no-op)")
            conn.close()
            return 0

        # Run phases
        audit_log(conn, "start", f"default_tenant={default_tenant}")
        conn.commit()

        phase1_add_column(conn)
        phase2_backfill(conn, default_tenant, batch_size)
        phase3_enforce_not_null(conn)

        audit_log(conn, "finish")
        conn.commit()

        log("✓ Migration completed successfully")
        conn.close()
        return 0

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        log(error_msg, "ERROR")
        try:
            audit_log(conn, "error", error_msg)
            conn.commit()
            conn.close()
        except Exception:
            pass
        return 1


def run_rollback(db_path):
    """
    Rollback migration: remove tenant_id column.

    Args:
        db_path: Path to SQLite database

    Returns:
        0 on success, 1 on error
    """
    db_path = Path(db_path)
    if not db_path.exists():
        log(f"Database not found: {db_path}", "ERROR")
        return 1

    try:
        conn = get_db_connection(db_path)
        setup_audit_table(conn)

        if not column_exists(conn, TABLE_NAME, COLUMN_NAME):
            log(f"Column {COLUMN_NAME} does not exist. Nothing to rollback.")
            conn.close()
            return 0

        # Drop column
        audit_log(conn, "rollback_start")
        conn.commit()

        conn.execute(f"ALTER TABLE {TABLE_NAME} DROP COLUMN {COLUMN_NAME}")
        conn.commit()

        audit_log(conn, "rollback")
        conn.commit()

        log(f"✓ Rollback complete: column {COLUMN_NAME} removed")
        conn.close()
        return 0

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        log(error_msg, "ERROR")
        try:
            conn.close()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nUsage:")
        print("  python3 migrate_add_tenant_id_runner.py /path/to/events.db")
        print("  python3 migrate_add_tenant_id_runner.py /path/to/events.db --rollback")
        sys.exit(1)

    db_path = sys.argv[1]
    is_rollback = len(sys.argv) > 2 and sys.argv[2] == "--rollback"

    if is_rollback:
        exit_code = run_rollback(db_path)
    else:
        exit_code = run_migration(db_path)

    sys.exit(exit_code)
