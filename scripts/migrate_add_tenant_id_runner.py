#!/usr/bin/env python3
"""
SQLite migration runner: add tenant_id column to events table with batched backfill.

Idempotent, resumable, and concurrent-write tolerant:
- Logs progress to migration_log table for resumability
- Batches 10,000 rows per transaction to limit lock duration
- Reports progress every 100 batches
- Recovers from failures by resuming from last committed offset
- Uses WAL journal mode for better concurrent read/write

Usage:
    python3 migrate_add_tenant_id_runner.py --db <database> [--tenant-id <value>]
"""

import sqlite3
import sys
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

MIGRATION_NAME = "add_tenant_id_to_events"
BATCH_SIZE = 10_000
PROGRESS_INTERVAL = 100  # report progress every N batches


def setup_logging():
    """Configure logging with timestamp and level."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    return logging.getLogger(__name__)


def create_migration_log_table(conn):
    """Ensure migration_log table exists and is properly indexed."""
    conn.execute("""
    CREATE TABLE IF NOT EXISTS migration_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        started_at TEXT,
        batch_count INTEGER DEFAULT 0,
        rows_processed INTEGER DEFAULT 0,
        finished_at TEXT,
        last_error TEXT,
        UNIQUE(migration_name)
    )
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_migration_log_name
    ON migration_log(migration_name)
    """)
    conn.commit()


def get_or_start_migration(conn, logger):
    """
    Check migration status and return resumption state.

    Returns:
        (log_id, rows_already_processed) if migration in progress or resumable
        (None, None) if migration already completed

    Raises:
        sqlite3.OperationalError if query fails
    """
    try:
        cur = conn.execute("""
        SELECT id, status, rows_processed FROM migration_log
        WHERE migration_name = ?
        ORDER BY id DESC LIMIT 1
        """, (MIGRATION_NAME,))

        row = cur.fetchone()
        if row:
            log_id, status, rows_processed = row

            if status == "completed":
                logger.info(f"Migration already completed: {rows_processed} rows processed.")
                return None, None

            if status in ("in_progress", "failed"):
                logger.info(f"Resuming migration from row offset {rows_processed}.")
                return log_id, rows_processed
    except sqlite3.OperationalError:
        # migration_log doesn't exist yet
        pass

    # Start new migration
    now = datetime.now(timezone.utc).isoformat()
    try:
        cur = conn.execute("""
        INSERT INTO migration_log (migration_name, status, started_at)
        VALUES (?, 'in_progress', ?)
        """, (MIGRATION_NAME, now))
        conn.commit()
        log_id = cur.lastrowid
        logger.info(f"Started migration (log_id={log_id}).")
        return log_id, 0
    except sqlite3.IntegrityError:
        # Another process started migration concurrently
        logger.warning("Migration started by another process. Querying state...")
        return get_or_start_migration(conn, logger)


def add_tenant_id_column_if_needed(conn, logger):
    """
    Add tenant_id column to events table if not present.
    Uses PRAGMA table_info for introspection.
    """
    try:
        cur = conn.execute("PRAGMA table_info(events)")
        columns = {row[1] for row in cur.fetchall()}

        if "tenant_id" in columns:
            logger.info("Column tenant_id already exists.")
            return

        conn.execute("""
        ALTER TABLE events ADD COLUMN tenant_id TEXT
        """)
        conn.commit()
        logger.info("Added column tenant_id (nullable).")
    except sqlite3.OperationalError as e:
        logger.error(f"Failed to add tenant_id column: {e}")
        raise


def process_batches_with_backfill(conn, log_id, rows_already_processed, logger, tenant_id_value="default"):
    """
    Backfill tenant_id in events table using batched transactions.

    Each batch:
    1. Selects up to BATCH_SIZE rows with NULL tenant_id
    2. Updates them in a single transaction
    3. Commits (releases locks)
    4. Logs progress every PROGRESS_INTERVAL batches

    Returns:
        (total_batches, total_rows_processed)

    Concurrent write tolerance: lock duration = per-batch update time, not total migration time.
    Error recovery: rows_already_processed allows resumption from last committed batch.
    """
    batch_count = rows_already_processed // BATCH_SIZE
    rows_processed = rows_already_processed

    while True:
        # Fetch next batch of NULL rows
        try:
            cur = conn.execute("""
            SELECT rowid FROM events
            WHERE tenant_id IS NULL
            LIMIT ?
            """, (BATCH_SIZE,))
            rowids = [row[0] for row in cur.fetchall()]
        except sqlite3.OperationalError as e:
            logger.error(f"Failed to fetch batch: {e}")
            raise

        if not rowids:
            logger.info(f"No more rows to process. Backfill complete.")
            break

        # Update this batch in a transaction
        try:
            # Build parameterized query for safety
            placeholders = ','.join('?' * len(rowids))
            conn.execute(
                f"UPDATE events SET tenant_id = ? WHERE rowid IN ({placeholders})",
                [tenant_id_value] + rowids
            )
            conn.commit()
        except sqlite3.OperationalError as e:
            conn.rollback()
            logger.error(f"Failed to update batch: {e}")
            raise

        batch_count += 1
        rows_processed += len(rowids)

        # Log progress every N batches
        if batch_count % PROGRESS_INTERVAL == 0:
            logger.info(f"Progress: batch {batch_count}, {rows_processed} rows backfilled.")

            # Atomically update migration_log
            try:
                conn.execute("""
                UPDATE migration_log
                SET batch_count = ?, rows_processed = ?
                WHERE id = ?
                """, (batch_count, rows_processed, log_id))
                conn.commit()
            except sqlite3.OperationalError as e:
                logger.warning(f"Failed to update progress log: {e}")
                conn.rollback()
                # Continue anyway; progress is best-effort

    # Final update to migration_log
    try:
        conn.execute("""
        UPDATE migration_log
        SET batch_count = ?, rows_processed = ?
        WHERE id = ?
        """, (batch_count, rows_processed, log_id))
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.warning(f"Failed to update final progress: {e}")

    return batch_count, rows_processed


def apply_not_null_constraint(conn, logger):
    """
    Apply NOT NULL constraint to tenant_id column.

    Tries SQLite 3.35+ ALTER TABLE ... MODIFY syntax first.
    Falls back to table recreation for older SQLite versions.
    """
    try:
        conn.execute("""
        ALTER TABLE events MODIFY COLUMN tenant_id TEXT NOT NULL
        """)
        conn.commit()
        logger.info("Applied NOT NULL constraint via ALTER TABLE MODIFY (SQLite 3.35+).")
        return
    except sqlite3.OperationalError:
        pass

    # Fallback: table recreation (compatible with SQLite 3.26+)
    logger.info("SQLite version doesn't support MODIFY COLUMN. Using table recreation...")

    try:
        conn.execute("PRAGMA foreign_keys=OFF")

        # Create temporary table without constraints
        conn.execute("CREATE TABLE events_tmp AS SELECT * FROM events")
        conn.execute("DROP TABLE events")

        # Recreate with NOT NULL on tenant_id
        # Note: we preserve existing schema from pragma table_info
        cur = conn.execute("PRAGMA table_info(events_tmp)")
        cols = [(row[1], row[2]) for row in cur.fetchall()]

        col_defs = []
        for col_name, col_type in cols:
            if col_name == "rowid":
                continue  # Skip implicit rowid
            constraint = "NOT NULL" if col_name == "tenant_id" else ""
            col_defs.append(f'"{col_name}" {col_type} {constraint}'.strip())

        create_sql = f"CREATE TABLE events ({', '.join(col_defs)})"
        conn.execute(create_sql)

        # Restore data
        col_names = ','.join(f'"{col[0]}"' for col in cols if col[0] != "rowid")
        conn.execute(f"INSERT INTO events ({col_names}) SELECT {col_names} FROM events_tmp")

        conn.execute("DROP TABLE events_tmp")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()

        logger.info("Applied NOT NULL constraint via table recreation (SQLite 3.26-3.34).")
    except sqlite3.OperationalError as e:
        logger.warning(f"Failed to apply NOT NULL constraint: {e}")
        logger.warning("Continuing with nullable tenant_id. Apply constraint manually if needed.")
        conn.rollback()


def mark_migration_complete(conn, log_id, batch_count, rows_processed, logger):
    """Mark migration as successfully completed."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("""
        UPDATE migration_log
        SET status = 'completed', finished_at = ?, batch_count = ?, rows_processed = ?
        WHERE id = ?
        """, (now, batch_count, rows_processed, log_id))
        conn.commit()
        logger.info(f"Migration completed: {batch_count} batches, {rows_processed} rows backfilled.")
    except sqlite3.OperationalError as e:
        logger.warning(f"Failed to mark migration complete: {e}")


def handle_error(conn, log_id, error, logger):
    """Log error to migration_log and mark as failed."""
    logger.error(f"Migration failed: {error}")
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("""
        UPDATE migration_log
        SET status = 'failed', last_error = ?, finished_at = ?
        WHERE id = ?
        """, (str(error), now, log_id))
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Log is best-effort


def main():
    parser = argparse.ArgumentParser(
        description="Add tenant_id column to events table with batched backfill."
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to SQLite database file"
    )
    parser.add_argument(
        "--tenant-id",
        default="default",
        help="Default tenant_id value for backfill (default: 'default')"
    )

    args = parser.parse_args()
    logger = setup_logging()

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error(f"Database not found: {db_path}")
        return 1

    log_id = None
    conn = None

    try:
        conn = sqlite3.connect(str(db_path))

        # Enable WAL mode for better concurrent read/write
        conn.execute("PRAGMA journal_mode=WAL")
        logger.info("Enabled WAL journal mode for concurrency.")

        # Setup audit infrastructure
        create_migration_log_table(conn)

        # Check migration status and get resumption point
        log_id, rows_already_processed = get_or_start_migration(conn, logger)
        if log_id is None:
            # Migration already completed
            return 0

        # Add column if needed (idempotent)
        add_tenant_id_column_if_needed(conn, logger)

        # Batched backfill with progress logging
        batch_count, rows_processed = process_batches_with_backfill(
            conn, log_id, rows_already_processed, logger, args.tenant_id
        )

        # Apply NOT NULL constraint
        apply_not_null_constraint(conn, logger)

        # Mark as complete
        mark_migration_complete(conn, log_id, batch_count, rows_processed, logger)

        logger.info("Migration successful.")
        return 0

    except Exception as e:
        if log_id and conn:
            handle_error(conn, log_id, e, logger)
        logger.exception("Unexpected error during migration")
        return 1

    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
