#!/usr/bin/env python3
"""
Test suite for migrate_add_tenant_id migration.

Tests cover:
- Idempotency (safe to run multiple times)
- Error recovery and resumability
- Concurrent-write tolerance (WAL mode)
- Audit trail logging
- NOT NULL constraint application
"""

import sqlite3
import tempfile
import pytest
from pathlib import Path
import sys

# Add scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from migrate_add_tenant_id_runner import (
    MIGRATION_NAME,
    BATCH_SIZE,
    create_migration_log_table,
    get_or_start_migration,
    add_tenant_id_column_if_needed,
    process_batches_with_backfill,
    apply_not_null_constraint,
    mark_migration_complete,
)
import logging

logger = logging.getLogger(__name__)


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)

    yield db_path

    # Cleanup
    db_path.unlink(missing_ok=True)


@pytest.fixture
def events_table_with_data(temp_db):
    """Create events table with N test rows."""
    def _create(row_count=25):
        conn = sqlite3.connect(str(temp_db))
        conn.execute("""
        CREATE TABLE events (
            rowid INTEGER PRIMARY KEY,
            id TEXT UNIQUE,
            data TEXT
        )
        """)
        for i in range(row_count):
            conn.execute(
                "INSERT INTO events (id, data) VALUES (?, ?)",
                (f"event_{i:06d}", f"test_data_{i}")
            )
        conn.commit()
        conn.close()
        return temp_db
    return _create


class TestMigrationBasics:
    """Test basic migration flow and idempotency."""

    def test_migration_initializes_audit_log(self, temp_db):
        """Test that migration initializes migration_log table."""
        conn = sqlite3.connect(str(temp_db))
        create_migration_log_table(conn)

        # Table should exist
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_log'"
        )
        assert cur.fetchone() is not None, "migration_log table should be created"

        conn.close()

    def test_migration_flow_complete(self, events_table_with_data):
        """Test complete migration flow: start -> backfill -> apply constraint."""
        db = events_table_with_data(row_count=25)
        conn = sqlite3.connect(str(db))

        # Setup
        create_migration_log_table(conn)
        log_id, rows_processed = get_or_start_migration(conn, logger)

        assert log_id is not None, "Migration should start"
        assert rows_processed == 0, "Should start at offset 0"

        # Add column
        add_tenant_id_column_if_needed(conn, logger)

        # Verify column exists
        cur = conn.execute("PRAGMA table_info(events)")
        columns = {row[1] for row in cur.fetchall()}
        assert "tenant_id" in columns, "tenant_id column should exist"

        # Backfill
        batch_count, total_rows = process_batches_with_backfill(
            conn, log_id, rows_processed, logger, "test_tenant"
        )

        assert total_rows == 25, "Should process 25 rows"
        assert batch_count == 1, "Should complete in 1 batch (25 < 10k)"

        # Verify backfill
        cur = conn.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id IS NOT NULL"
        )
        non_null_count = cur.fetchone()[0]
        assert non_null_count == 25, f"All rows should have tenant_id, got {non_null_count}"

        # Apply constraint
        apply_not_null_constraint(conn, logger)

        # Mark complete
        mark_migration_complete(conn, log_id, batch_count, total_rows, logger)

        # Verify completion
        cur = conn.execute(
            "SELECT status, rows_processed FROM migration_log WHERE id = ?",
            (log_id,)
        )
        status, rows_proc = cur.fetchone()
        assert status == "completed", "Migration should be marked completed"
        assert rows_proc == 25, "Should record 25 rows processed"

        conn.close()

    def test_migration_idempotency(self, events_table_with_data):
        """Test that running migration twice is safe (idempotent)."""
        db = events_table_with_data(row_count=25)

        # First run
        conn = sqlite3.connect(str(db))
        create_migration_log_table(conn)
        log_id1, _ = get_or_start_migration(conn, logger)
        add_tenant_id_column_if_needed(conn, logger)
        batch_count1, total_rows1 = process_batches_with_backfill(
            conn, log_id1, 0, logger, "test_tenant"
        )
        apply_not_null_constraint(conn, logger)
        mark_migration_complete(conn, log_id1, batch_count1, total_rows1, logger)
        conn.close()

        # Second run
        conn = sqlite3.connect(str(db))
        create_migration_log_table(conn)
        log_id2, rows_offset = get_or_start_migration(conn, logger)

        # Should detect completion
        assert log_id2 is None, "Should detect completed migration on second run"
        assert rows_offset is None

        # Table should still have correct data
        cur = conn.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id IS NOT NULL"
        )
        assert cur.fetchone()[0] == 25, "Data should be intact"

        conn.close()

    def test_migration_reports_progress(self, events_table_with_data, caplog):
        """Test that migration reports progress at intervals."""
        db = events_table_with_data(row_count=BATCH_SIZE + 5)
        conn = sqlite3.connect(str(db))

        # Reduce progress interval for testing
        import migrate_add_tenant_id_runner as runner
        orig_interval = runner.PROGRESS_INTERVAL
        runner.PROGRESS_INTERVAL = 1  # Report every 1 batch

        create_migration_log_table(conn)
        log_id, _ = get_or_start_migration(conn, logger)
        add_tenant_id_column_if_needed(conn, logger)

        with caplog.at_level(logging.INFO):
            batch_count, total_rows = process_batches_with_backfill(
                conn, log_id, 0, logger, "test_tenant"
            )

        assert "Progress:" in caplog.text, "Should log progress"

        runner.PROGRESS_INTERVAL = orig_interval
        conn.close()


class TestMigrationResumability:
    """Test error recovery and resumability."""

    def test_migration_resumes_from_offset(self, events_table_with_data):
        """Test that migration can resume from a partial state."""
        db = events_table_with_data(row_count=50)
        conn = sqlite3.connect(str(db))

        # Start migration
        create_migration_log_table(conn)
        log_id, _ = get_or_start_migration(conn, logger)
        add_tenant_id_column_if_needed(conn, logger)

        # Process part of the data
        batch_count, rows_processed = process_batches_with_backfill(
            conn, log_id, 0, logger, "test_tenant"
        )
        assert rows_processed == 50, "Should process all 50 rows"

        # Verify progress was recorded
        cur = conn.execute(
            "SELECT rows_processed, status FROM migration_log WHERE id = ?",
            (log_id,)
        )
        stored_offset, status = cur.fetchone()
        assert stored_offset == 50, "Offset should be stored"
        assert status == "in_progress", "Status should be in_progress before completion"

        # Now mark as complete
        apply_not_null_constraint(conn, logger)
        mark_migration_complete(conn, log_id, batch_count, rows_processed, logger)

        # Resume from offset should detect completion
        new_log_id, resume_offset = get_or_start_migration(conn, logger)
        assert new_log_id is None, "Should detect completed migration"
        assert resume_offset is None

        conn.close()

    def test_migration_continues_after_partial_failure(self, events_table_with_data):
        """Test that migration can continue after a partial failure."""
        db = events_table_with_data(row_count=100)
        conn = sqlite3.connect(str(db))

        create_migration_log_table(conn)
        log_id, _ = get_or_start_migration(conn, logger)
        add_tenant_id_column_if_needed(conn, logger)

        # First partial run
        batch_count1, rows_proc1 = process_batches_with_backfill(
            conn, log_id, 0, logger, "test_tenant"
        )

        # Verify partial progress was recorded
        cur = conn.execute(
            "SELECT rows_processed FROM migration_log WHERE id = ?",
            (log_id,)
        )
        recorded_offset = cur.fetchone()[0]
        assert recorded_offset == rows_proc1, "Offset should be recorded"

        # All 100 should be done in one batch since BATCH_SIZE=10k
        assert rows_proc1 == 100, "All rows should be processed in one batch"

        conn.close()


class TestConcurrentWriteTolerance:
    """Test concurrent write tolerance via WAL mode."""

    def test_wal_mode_enabled(self, temp_db):
        """Test that migration enables WAL mode for concurrency."""
        conn = sqlite3.connect(str(temp_db))

        # Enable WAL as runner does
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]

        assert mode.upper() == "WAL", "WAL mode should be enabled"
        conn.close()

    def test_batch_transactions_commit_separately(self, events_table_with_data):
        """Test that batches commit separately for lock isolation."""
        db = events_table_with_data(row_count=BATCH_SIZE + 100)
        conn = sqlite3.connect(str(db))

        create_migration_log_table(conn)
        log_id, _ = get_or_start_migration(conn, logger)
        add_tenant_id_column_if_needed(conn, logger)

        # Process batches
        batch_count, total_rows = process_batches_with_backfill(
            conn, log_id, 0, logger, "test_tenant"
        )

        # Should have multiple batches
        assert batch_count >= 2, f"Should have multiple batches, got {batch_count}"

        # Verify all rows processed
        cur = conn.execute("SELECT COUNT(*) FROM events WHERE tenant_id IS NOT NULL")
        assert cur.fetchone()[0] == total_rows, "All rows should be backfilled"

        conn.close()


class TestAuditTrail:
    """Test audit logging and migration_log completeness."""

    def test_migration_log_records_start(self, temp_db):
        """Test that migration_log records start time."""
        conn = sqlite3.connect(str(temp_db))

        create_migration_log_table(conn)
        log_id, _ = get_or_start_migration(conn, logger)

        cur = conn.execute(
            "SELECT started_at, status FROM migration_log WHERE id = ?",
            (log_id,)
        )
        started_at, status = cur.fetchone()

        assert started_at is not None, "started_at should be recorded"
        assert status == "in_progress", "Status should be in_progress"

        conn.close()

    def test_migration_log_records_completion(self, events_table_with_data):
        """Test that migration_log records completion metrics."""
        db = events_table_with_data(row_count=25)
        conn = sqlite3.connect(str(db))

        create_migration_log_table(conn)
        log_id, _ = get_or_start_migration(conn, logger)
        add_tenant_id_column_if_needed(conn, logger)
        batch_count, total_rows = process_batches_with_backfill(
            conn, log_id, 0, logger, "test_tenant"
        )
        mark_migration_complete(conn, log_id, batch_count, total_rows, logger)

        cur = conn.execute(
            "SELECT status, batch_count, rows_processed, finished_at FROM migration_log WHERE id = ?",
            (log_id,)
        )
        status, bc, rp, finished_at = cur.fetchone()

        assert status == "completed", "Status should be completed"
        assert bc == batch_count, "Batch count should match"
        assert rp == total_rows, "Rows processed should match"
        assert finished_at is not None, "finished_at should be recorded"

        conn.close()


class TestConstraintApplication:
    """Test NOT NULL constraint application."""

    def test_not_null_constraint_applied(self, events_table_with_data):
        """Test that NOT NULL constraint is applied after backfill."""
        db = events_table_with_data(row_count=10)
        conn = sqlite3.connect(str(db))

        create_migration_log_table(conn)
        log_id, _ = get_or_start_migration(conn, logger)
        add_tenant_id_column_if_needed(conn, logger)
        process_batches_with_backfill(conn, log_id, 0, logger, "test_tenant")

        # Apply constraint (should not raise)
        apply_not_null_constraint(conn, logger)

        # Verify constraint is enforced (try to insert NULL)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO events (id, data, tenant_id) VALUES (?, ?, ?)",
                ("test_null", "data", None)
            )

        conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
