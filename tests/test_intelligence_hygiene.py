"""Tests for intelligence catalogue hygiene: governance-event filter,
memory_consolidation antipattern filter, recency decay, and migration SQL.

Dispatch: audit-ih-1-catalog-hygiene-20260517-224858
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Ensure scripts/lib is importable without a full package install
_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from pattern_dedup import _is_governance_event
from success_pattern_extractor import insert_filtered_success_pattern
from antipattern_extractor import _is_meta_consolidation, insert_filtered_antipattern
from confidence_reconcile import _recency_decay, _parse_last_used, reconcile_pattern_confidence


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_db() -> sqlite3.Connection:
    """In-memory DB with minimal success_patterns and antipatterns schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT DEFAULT 'approach',
            category TEXT DEFAULT 'governance',
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            pattern_data TEXT DEFAULT '{}',
            confidence_score REAL DEFAULT 0.5,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT DEFAULT '[]',
            first_seen TEXT,
            last_used TEXT,
            valid_until TEXT DEFAULT NULL,
            invalidation_reason TEXT DEFAULT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT DEFAULT 'approach',
            category TEXT DEFAULT 'governance',
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            pattern_data TEXT DEFAULT '{}',
            why_problematic TEXT DEFAULT '',
            severity TEXT DEFAULT 'medium',
            occurrence_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT DEFAULT '[]',
            first_seen TEXT,
            last_seen TEXT,
            valid_until TEXT DEFAULT NULL,
            invalidation_reason TEXT DEFAULT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE runtime_schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT,
            applied_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)
    conn.commit()
    return conn


def _make_db_with_patterns(conn: sqlite3.Connection) -> None:
    """Seed the DB with rows that should be invalidated by the migration."""
    now = "2026-03-15T10:00:00"
    conn.execute(
        "INSERT INTO success_patterns (title, description, category, first_seen, last_used) "
        "VALUES (?, ?, ?, ?, ?)",
        ("gate codex_gate passed", "Gate pass event", "governance", now, now),
    )
    conn.execute(
        "INSERT INTO success_patterns (title, description, category, first_seen, last_used) "
        "VALUES (?, ?, ?, ?, ?)",
        ("Implements async crawling", "Real code pattern", "code", now, now),
    )
    conn.execute(
        "INSERT INTO antipatterns (title, description, category, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        ("52 dispatches: 52% success rate", "meta stat", "memory_consolidation", now, now),
    )
    conn.execute(
        "INSERT INTO antipatterns (title, description, category, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        ("Missing error handling in extractor", "Real antipattern", "code", now, now),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# _is_governance_event tests
# ---------------------------------------------------------------------------

class TestIsGovernanceEvent:
    def test_gate_passed_exact(self):
        assert _is_governance_event("gate codex_gate passed") is True

    def test_gate_passed_mixed_case(self):
        assert _is_governance_event("Gate Gemini_Review Passed") is True

    def test_gate_passed_with_spaces(self):
        assert _is_governance_event("gate pr0_input_ready_contract passed") is True

    def test_recent_dispatch_blocked(self):
        assert _is_governance_event("Recent: backend-developer dispatch (success)") is True

    def test_recent_dispatch_case_insensitive(self):
        assert _is_governance_event("recent: architect dispatch (failure)") is True

    def test_real_pattern_not_blocked(self):
        assert _is_governance_event("Use atomic writes for NDJSON appenders") is False

    def test_empty_title_not_blocked(self):
        assert _is_governance_event("") is False

    def test_none_title_not_blocked(self):
        assert _is_governance_event(None) is False

    def test_partial_gate_not_blocked(self):
        # "gate" somewhere in title but not the exact shape
        assert _is_governance_event("Always gate database writes") is False


# ---------------------------------------------------------------------------
# insert_filtered_success_pattern tests
# ---------------------------------------------------------------------------

class TestInsertFilteredSuccessPattern:
    def test_governance_event_skipped_at_insert(self):
        conn = _make_db()
        result = insert_filtered_success_pattern(
            conn,
            title="gate codex_gate passed",
            description="Gate codex_gate passed",
        )
        assert result == 0
        count = conn.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
        assert count == 0

    def test_recent_style_title_skipped(self):
        conn = _make_db()
        result = insert_filtered_success_pattern(
            conn,
            title="Recent: backend-developer dispatch (success)",
            description="dispatch success",
        )
        assert result == 0
        count = conn.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
        assert count == 0

    def test_real_pattern_inserted(self):
        conn = _make_db()
        result = insert_filtered_success_pattern(
            conn,
            title="Use atomic writes for shared files",
            description="Write to .tmp then os.replace() for atomicity",
        )
        assert result == 1
        count = conn.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# _is_meta_consolidation + insert_filtered_antipattern tests
# ---------------------------------------------------------------------------

class TestMetaConsolidationFilter:
    def test_memory_consolidation_category_blocked(self):
        assert _is_meta_consolidation("memory_consolidation", "anything") is True

    def test_memory_consolidation_case_insensitive(self):
        assert _is_meta_consolidation("Memory_Consolidation", "anything") is True

    def test_dispatches_success_rate_title_blocked(self):
        assert _is_meta_consolidation("governance", "52 dispatches: 52% success rate") is True

    def test_dispatches_success_rate_case_insensitive(self):
        assert _is_meta_consolidation("code", "10 Dispatches: 80% Success Rate") is True

    def test_real_antipattern_not_blocked(self):
        assert _is_meta_consolidation("code", "Missing rollback on DB migration failure") is False

    def test_none_inputs_not_blocked(self):
        assert _is_meta_consolidation(None, None) is False


class TestInsertFilteredAntipattern:
    def test_memory_consolidation_skipped(self):
        conn = _make_db()
        result = insert_filtered_antipattern(
            conn,
            title="52 dispatches: 52% success rate",
            description="meta stat",
            category="memory_consolidation",
        )
        assert result == 0
        count = conn.execute("SELECT COUNT(*) FROM antipatterns").fetchone()[0]
        assert count == 0

    def test_meta_stat_title_skipped(self):
        conn = _make_db()
        result = insert_filtered_antipattern(
            conn,
            title="100 dispatches: 70% success rate",
            description="meta stat",
            category="governance",
        )
        assert result == 0

    def test_real_antipattern_inserted(self):
        conn = _make_db()
        result = insert_filtered_antipattern(
            conn,
            title="Skipping gates before merge",
            description="Gate skipping leads to regressions",
            category="governance",
        )
        assert result == 1
        count = conn.execute("SELECT COUNT(*) FROM antipatterns").fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# _parse_last_used tests (timezone regression)
# ---------------------------------------------------------------------------

class TestParseLastUsed:
    def test_naive_datetime_string(self):
        dt = _parse_last_used("2026-03-15T10:00:00")
        assert dt == datetime(2026, 3, 15, 10, 0, 0)
        assert dt.tzinfo is None

    def test_naive_datetime_with_microseconds(self):
        dt = _parse_last_used("2026-03-15T10:00:00.123456")
        assert dt == datetime(2026, 3, 15, 10, 0, 0, 123456)

    def test_sqlite_space_separator(self):
        dt = _parse_last_used("2026-03-15 10:00:00")
        assert dt == datetime(2026, 3, 15, 10, 0, 0)

    def test_utc_z_suffix(self):
        dt = _parse_last_used("2026-03-15T10:00:00Z")
        assert dt == datetime(2026, 3, 15, 10, 0, 0)
        assert dt.tzinfo is None

    def test_positive_offset_converted_to_utc(self):
        dt = _parse_last_used("2026-03-15T12:00:00+02:00")
        assert dt == datetime(2026, 3, 15, 10, 0, 0)
        assert dt.tzinfo is None

    def test_negative_offset_converted_to_utc(self):
        dt = _parse_last_used("2026-03-15T07:00:00-03:00")
        assert dt == datetime(2026, 3, 15, 10, 0, 0)
        assert dt.tzinfo is None

    def test_offset_with_microseconds(self):
        dt = _parse_last_used("2026-03-15T12:00:00.000000+02:00")
        assert dt == datetime(2026, 3, 15, 10, 0, 0)

    def test_none_returns_none(self):
        assert _parse_last_used(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_last_used("") is None

    def test_invalid_string_returns_none(self):
        assert _parse_last_used("not-a-date") is None


# ---------------------------------------------------------------------------
# Recency decay tests
# ---------------------------------------------------------------------------

class TestRecencyDecay:
    def test_fresh_pattern_minimal_decay(self):
        """Pattern used today should have negligible decay."""
        now = datetime.utcnow()
        decayed = _recency_decay(0.8, now)
        assert decayed > 0.79  # less than 1 week → < 5% decay

    def test_four_week_old_pattern_decayed(self):
        """Pattern unused for 4 weeks should decay by ~0.95^4 ≈ 0.815×."""
        four_weeks_ago = datetime.utcnow() - timedelta(weeks=4)
        original = 0.8
        decayed = _recency_decay(original, four_weeks_ago)
        expected = original * (0.95 ** 4)
        assert abs(decayed - expected) < 0.001
        assert decayed < original

    def test_eight_week_old_pattern_further_decayed(self):
        """Pattern unused for 8 weeks should decay by ~0.95^8 ≈ 0.663×."""
        eight_weeks_ago = datetime.utcnow() - timedelta(weeks=8)
        original = 0.8
        decayed = _recency_decay(original, eight_weeks_ago)
        expected = original * (0.95 ** 8)
        assert abs(decayed - expected) < 0.001
        assert decayed < _recency_decay(original, datetime.utcnow() - timedelta(weeks=4))

    def test_very_old_pattern_hits_floor(self):
        """Pattern unused for 60 weeks should be clamped to floor 0.1."""
        very_old = datetime.utcnow() - timedelta(weeks=60)
        decayed = _recency_decay(0.9, very_old)
        assert decayed == pytest.approx(0.1, abs=1e-9)

    def test_floor_applies_even_to_high_confidence(self):
        """Even a 1.0 confidence pattern cannot decay below 0.1."""
        very_old = datetime.utcnow() - timedelta(weeks=100)
        decayed = _recency_decay(1.0, very_old)
        assert decayed >= 0.1

    def test_reconcile_applies_decay(self, tmp_path):
        """reconcile_pattern_confidence writes decayed scores back to the DB."""
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                confidence_score REAL DEFAULT 0.5,
                last_used TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                used_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 10,
                failure_count INTEGER DEFAULT 2,
                confidence REAL DEFAULT 0.8,
                last_used TEXT,
                last_offered TEXT
            )
        """)
        eight_weeks_ago = (datetime.utcnow() - timedelta(weeks=8)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn.execute(
            "INSERT INTO success_patterns (confidence_score, last_used) VALUES (?, ?)",
            (0.5, eight_weeks_ago),
        )
        sp_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO pattern_usage (pattern_id, success_count, failure_count, used_count) "
            "VALUES (?, 10, 2, 12)",
            (f"intel_sp_{sp_id}",),
        )
        conn.commit()
        conn.close()

        updated = reconcile_pattern_confidence(db_path)
        assert updated == 1

        conn2 = sqlite3.connect(str(db_path))
        row = conn2.execute(
            "SELECT confidence_score FROM success_patterns WHERE id = ?", (sp_id,)
        ).fetchone()
        conn2.close()

        beta = (10 + 1) / (10 + 2 + 2)  # (s+1)/(s+f+2) = 11/14 ≈ 0.786
        expected = beta * (0.95 ** 8)
        assert abs(row[0] - round(expected, 6)) < 1e-5


# ---------------------------------------------------------------------------
# Migration SQL test
# ---------------------------------------------------------------------------

class TestMigrationSQL:
    def _apply_migration_sql(self, conn: sqlite3.Connection) -> None:
        """Apply the hygiene migration SQL to the given connection."""
        sql_path = (
            Path(__file__).resolve().parent.parent
            / "schemas" / "migrations" / "2026_05_intelligence_hygiene.sql"
        )
        sql = sql_path.read_text(encoding="utf-8")
        for raw_stmt in sql.split(";"):
            # Strip comment-only lines to expose the actual SQL keyword
            lines = [
                ln for ln in raw_stmt.splitlines()
                if ln.strip() and not ln.strip().startswith("--")
            ]
            stmt = "\n".join(lines).strip()
            if not stmt:
                continue
            upper = stmt.upper()
            # Skip control statements and PRAGMA (not needed in in-memory tests)
            if upper in ("BEGIN TRANSACTION", "BEGIN", "COMMIT", "ROLLBACK"):
                continue
            if upper.startswith("PRAGMA"):
                continue
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                # ALTER TABLE ... ADD COLUMN IF NOT EXISTS requires SQLite 3.37+
                if "syntax error" in str(exc).lower() and "IF NOT EXISTS" in stmt:
                    fallback = stmt.replace(" IF NOT EXISTS", "")
                    try:
                        conn.execute(fallback)
                    except sqlite3.OperationalError:
                        pass  # Column already exists
                else:
                    raise
        conn.commit()

    def test_migration_invalidates_gate_passed_rows(self):
        conn = _make_db()
        _make_db_with_patterns(conn)

        self._apply_migration_sql(conn)

        # Gate pass row should be invalidated
        row = conn.execute(
            "SELECT valid_until, invalidation_reason FROM success_patterns "
            "WHERE title = 'gate codex_gate passed'"
        ).fetchone()
        assert row is not None
        assert row[0] is not None  # valid_until set
        assert "governance_event_noise" in row[1]

    def test_migration_leaves_real_patterns_intact(self):
        conn = _make_db()
        _make_db_with_patterns(conn)

        self._apply_migration_sql(conn)

        row = conn.execute(
            "SELECT valid_until FROM success_patterns "
            "WHERE title = 'Implements async crawling'"
        ).fetchone()
        assert row is not None
        assert row[0] is None  # not invalidated

    def test_migration_invalidates_memory_consolidation_antipatterns(self):
        conn = _make_db()
        _make_db_with_patterns(conn)

        self._apply_migration_sql(conn)

        row = conn.execute(
            "SELECT valid_until, invalidation_reason FROM antipatterns "
            "WHERE title = '52 dispatches: 52% success rate'"
        ).fetchone()
        assert row is not None
        assert row[0] is not None  # valid_until set
        assert "meta_stats" in row[1]

    def test_migration_leaves_real_antipatterns_intact(self):
        conn = _make_db()
        _make_db_with_patterns(conn)

        self._apply_migration_sql(conn)

        row = conn.execute(
            "SELECT valid_until FROM antipatterns "
            "WHERE title = 'Missing error handling in extractor'"
        ).fetchone()
        assert row is not None
        assert row[0] is None  # not invalidated

    def test_migration_idempotent(self):
        """Running migration twice does not change already-invalidated rows."""
        conn = _make_db()
        _make_db_with_patterns(conn)

        self._apply_migration_sql(conn)
        # Capture first valid_until value
        first_ts = conn.execute(
            "SELECT valid_until FROM success_patterns WHERE title = 'gate codex_gate passed'"
        ).fetchone()[0]

        self._apply_migration_sql(conn)
        second_ts = conn.execute(
            "SELECT valid_until FROM success_patterns WHERE title = 'gate codex_gate passed'"
        ).fetchone()[0]

        # Timestamp should not change on second run (WHERE valid_until IS NULL guard)
        assert first_ts == second_ts
