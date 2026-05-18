#!/usr/bin/env python3
"""
Quality Intelligence Database Initialization Script
Version: 8.0.2 (Phase 2)
Purpose: Initialize SQLite Quality Intelligence Database from schema
"""

import sqlite3
import sys
from pathlib import Path
from datetime import datetime
import json

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

import schema_migration

# Highest PRAGMA user_version stamped by bootstrap_qi_db.
# Increment this constant whenever a new migration block is added.
HIGHEST_QI_VERSION = 17

# VNX Base Configuration
PATHS = ensure_env()
VNX_BASE = Path(PATHS["VNX_HOME"])
SCHEMAS_DIR = VNX_BASE / "schemas"
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"
SCHEMA_FILE = SCHEMAS_DIR / "quality_intelligence.sql"

# Color codes for terminal output
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'

def log(level: str, message: str):
    """Log message with timestamp and color coding"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    color_map = {
        'INFO': Colors.BLUE,
        'SUCCESS': Colors.GREEN,
        'WARNING': Colors.YELLOW,
        'ERROR': Colors.RED
    }

    color = color_map.get(level, Colors.RESET)
    print(f"[{timestamp}] {color}[{level}]{Colors.RESET} {message}")

def check_prerequisites() -> bool:
    """Verify all required files and directories exist"""
    log('INFO', 'Checking prerequisites...')

    # Check schema file
    if not SCHEMA_FILE.exists():
        log('ERROR', f'Schema file not found: {SCHEMA_FILE}')
        return False

    log('SUCCESS', f'Schema file found: {SCHEMA_FILE}')

    # Ensure state directory exists
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log('SUCCESS', f'State directory ready: {STATE_DIR}')

    return True

def backup_existing_db() -> bool:
    """Backup existing database if it exists"""
    if not DB_PATH.exists():
        log('INFO', 'No existing database to backup')
        return True

    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = STATE_DIR / f"quality_intelligence.db.backup_{timestamp}"

        log('INFO', f'Backing up existing database to: {backup_path}')

        # Copy file
        import shutil
        shutil.copy2(DB_PATH, backup_path)

        log('SUCCESS', f'Database backed up successfully')
        return True

    except Exception as e:
        log('ERROR', f'Failed to backup database: {e}')
        return False

def initialize_database() -> bool:
    """Initialize database from schema file (uses module-level DB_PATH)."""
    return bootstrap_qi_db(DB_PATH, SCHEMA_FILE)


def bootstrap_qi_db(db_path: Path, schema_file: Path | None = None) -> bool:
    """Initialize a quality_intelligence DB at ``db_path`` using the canonical schema.

    Path-explicit variant of :func:`initialize_database` so callers
    (Phase 6 P4 migrator, tests) can target a specific DB without
    relying on module-level constants. ``schema_file`` defaults to the
    canonical ``schemas/quality_intelligence.sql`` resolved from
    ``VNX_HOME``.

    Idempotent via PRAGMA user_version: each migration block is skipped if
    user_version >= its target. Mid-run failures roll back cleanly via
    SAVEPOINT. Version stamps range from 1 (base schema) to HIGHEST_QI_VERSION.
    """
    schema_file = Path(schema_file) if schema_file is not None else SCHEMA_FILE
    log('INFO', f'Initializing quality intelligence database at {db_path}...')

    try:
        with open(schema_file, 'r') as f:
            schema_sql = f.read()

        log('INFO', f'Schema loaded: {len(schema_sql)} characters')

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.isolation_level = None  # manual transaction management for SAVEPOINT correctness

        # ---- V1: base schema applied atomically (codex round-3 fix) ----
        # schema_migration.apply_script_if_below splits SQL and runs all statements
        # + user_version stamp inside ONE SAVEPOINT — mid-script failure rolls back ALL
        if schema_migration.apply_script_if_below(conn, 1, schema_sql):
            log('INFO', 'Base schema applied atomically (v1)')

        # ---- V2: pattern_hash on snippet_metadata ----
        def _v2(c):
            cols = {r[1] for r in c.execute("PRAGMA table_info(snippet_metadata)").fetchall()}
            if "pattern_hash" not in cols:
                c.execute("ALTER TABLE snippet_metadata ADD COLUMN pattern_hash TEXT")
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_snippet_pattern_hash "
                    "ON snippet_metadata (pattern_hash)"
                )
                log('INFO', 'Migrated snippet_metadata: added pattern_hash column + index')
        schema_migration.apply_if_below(conn, 2, _v2)

        # ---- V3: session_analytics, improvement_suggestions, nightly_digests tables ----
        def _v3(c):
            if not c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='session_analytics'"
            ).fetchone():
                c.execute("""
                    CREATE TABLE IF NOT EXISTS session_analytics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL UNIQUE,
                        project_path TEXT NOT NULL,
                        terminal TEXT,
                        session_date DATE NOT NULL,
                        total_input_tokens INTEGER DEFAULT 0,
                        total_output_tokens INTEGER DEFAULT 0,
                        cache_creation_tokens INTEGER DEFAULT 0,
                        cache_read_tokens INTEGER DEFAULT 0,
                        tool_calls_total INTEGER DEFAULT 0,
                        tool_read_count INTEGER DEFAULT 0,
                        tool_edit_count INTEGER DEFAULT 0,
                        tool_bash_count INTEGER DEFAULT 0,
                        tool_grep_count INTEGER DEFAULT 0,
                        tool_write_count INTEGER DEFAULT 0,
                        tool_task_count INTEGER DEFAULT 0,
                        tool_other_count INTEGER DEFAULT 0,
                        message_count INTEGER DEFAULT 0,
                        user_message_count INTEGER DEFAULT 0,
                        assistant_message_count INTEGER DEFAULT 0,
                        duration_minutes REAL,
                        has_error_recovery BOOLEAN DEFAULT FALSE,
                        has_context_reset BOOLEAN DEFAULT FALSE,
                        context_reset_count INTEGER DEFAULT 0,
                        has_large_refactor BOOLEAN DEFAULT FALSE,
                        has_test_cycle BOOLEAN DEFAULT FALSE,
                        primary_activity TEXT,
                        deep_analysis_json TEXT,
                        deep_analysis_model TEXT,
                        deep_analysis_at DATETIME,
                        file_size_bytes INTEGER,
                        analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        analyzer_version TEXT DEFAULT '1.0.0'
                    )
                """)
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_session_terminal "
                    "ON session_analytics (terminal, session_date DESC)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_session_project "
                    "ON session_analytics (project_path, session_date DESC)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_session_date "
                    "ON session_analytics (session_date DESC)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_session_activity "
                    "ON session_analytics (primary_activity)"
                )
                c.execute("""
                    CREATE TABLE IF NOT EXISTS improvement_suggestions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        category TEXT NOT NULL,
                        component TEXT,
                        current_behavior TEXT NOT NULL,
                        suggested_improvement TEXT NOT NULL,
                        evidence TEXT,
                        priority TEXT DEFAULT 'medium',
                        status TEXT DEFAULT 'new',
                        digest_id TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        acted_on_at DATETIME
                    )
                """)
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_improvement_category "
                    "ON improvement_suggestions (category, status)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_improvement_priority "
                    "ON improvement_suggestions (priority, status)"
                )
                c.execute("""
                    CREATE TABLE IF NOT EXISTS nightly_digests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        digest_date DATE NOT NULL UNIQUE,
                        sessions_analyzed INTEGER DEFAULT 0,
                        deep_analyzed INTEGER DEFAULT 0,
                        new_suggestions INTEGER DEFAULT 0,
                        total_tokens_used INTEGER DEFAULT 0,
                        digest_markdown TEXT NOT NULL,
                        digest_path TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                log('INFO', 'Migrated: added session_analytics, improvement_suggestions, nightly_digests tables')
        schema_migration.apply_if_below(conn, 3, _v3)

        # ---- V4: session_model on session_analytics ----
        def _v4(c):
            cols = {r[1] for r in c.execute("PRAGMA table_info(session_analytics)").fetchall()}
            if "session_model" not in cols:
                c.execute(
                    "ALTER TABLE session_analytics ADD COLUMN session_model TEXT DEFAULT 'unknown'"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_session_model "
                    "ON session_analytics (session_model, session_date DESC)"
                )
                log('INFO', 'Migrated session_analytics: added session_model column + index')
        schema_migration.apply_if_below(conn, 4, _v4)

        # ---- V5: dispatch_id on session_analytics ----
        def _v5(c):
            cols = {r[1] for r in c.execute("PRAGMA table_info(session_analytics)").fetchall()}
            if "dispatch_id" not in cols:
                c.execute("ALTER TABLE session_analytics ADD COLUMN dispatch_id TEXT")
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_session_dispatch_id "
                    "ON session_analytics (dispatch_id)"
                )
                log('INFO', 'Migrated session_analytics: added dispatch_id column + index')
        schema_migration.apply_if_below(conn, 5, _v5)

        # ---- V6: context_reset_count on session_analytics ----
        def _v6(c):
            cols = {r[1] for r in c.execute("PRAGMA table_info(session_analytics)").fetchall()}
            if "context_reset_count" not in cols:
                c.execute(
                    "ALTER TABLE session_analytics ADD COLUMN context_reset_count INTEGER DEFAULT 0"
                )
                log('INFO', 'Migrated session_analytics: added context_reset_count column')
        schema_migration.apply_if_below(conn, 6, _v6)

        # ---- V7: report_findings table ----
        def _v7(c):
            if not c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='report_findings'"
            ).fetchone():
                c.execute("""
                    CREATE TABLE IF NOT EXISTS report_findings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        report_path TEXT NOT NULL,
                        report_date TIMESTAMP,
                        terminal TEXT,
                        task_type TEXT,
                        patterns_found INTEGER,
                        antipatterns_found INTEGER,
                        prevention_rules_found INTEGER,
                        tags_found TEXT,
                        summary TEXT,
                        age_category TEXT,
                        extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        dispatch_id TEXT
                    )
                """)
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_report_findings_extracted "
                    "ON report_findings (extracted_at DESC)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_report_findings_dispatch "
                    "ON report_findings (dispatch_id)"
                )
                log('INFO', 'Migrated: created report_findings table')
        schema_migration.apply_if_below(conn, 7, _v7)

        # ---- V8: dispatch_id on report_findings (for DBs with pre-v7 report_findings) ----
        def _v8(c):
            if c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='report_findings'"
            ).fetchone():
                cols = {r[1] for r in c.execute("PRAGMA table_info(report_findings)").fetchall()}
                if "dispatch_id" not in cols:
                    c.execute("ALTER TABLE report_findings ADD COLUMN dispatch_id TEXT")
                    log('INFO', 'Migrated report_findings: added dispatch_id column')
        schema_migration.apply_if_below(conn, 8, _v8)

        # ---- V9: CQS columns on dispatch_metadata ----
        def _v9(c):
            cols = {r[1] for r in c.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
            if "cqs" not in cols:
                c.execute("ALTER TABLE dispatch_metadata ADD COLUMN cqs REAL")
                c.execute("ALTER TABLE dispatch_metadata ADD COLUMN normalized_status TEXT")
                c.execute("ALTER TABLE dispatch_metadata ADD COLUMN cqs_components TEXT")
                log('INFO', 'Migrated dispatch_metadata: added cqs, normalized_status, cqs_components columns')
        schema_migration.apply_if_below(conn, 9, _v9)

        # ---- V10: governance_metrics, spc_control_limits, spc_alerts ----
        def _v10(c):
            if not c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='governance_metrics'"
            ).fetchone():
                c.execute("""
                    CREATE TABLE IF NOT EXISTS governance_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        period_start DATE NOT NULL,
                        period_end DATE NOT NULL,
                        scope_type TEXT NOT NULL,
                        scope_value TEXT NOT NULL,
                        metric_name TEXT NOT NULL,
                        metric_value REAL NOT NULL,
                        sample_size INTEGER NOT NULL,
                        computed_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_gov_metrics_lookup "
                    "ON governance_metrics (period_start, scope_type, metric_name)"
                )
                c.execute("""
                    CREATE TABLE IF NOT EXISTS spc_control_limits (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        metric_name TEXT NOT NULL,
                        scope_type TEXT NOT NULL,
                        scope_value TEXT NOT NULL,
                        center_line REAL NOT NULL,
                        ucl REAL NOT NULL,
                        lcl REAL NOT NULL,
                        sigma REAL NOT NULL,
                        sample_count INTEGER NOT NULL,
                        baseline_start DATE,
                        baseline_end DATE,
                        computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(metric_name, scope_type, scope_value)
                    )
                """)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS spc_alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        alert_type TEXT NOT NULL,
                        metric_name TEXT NOT NULL,
                        scope_type TEXT NOT NULL,
                        scope_value TEXT NOT NULL,
                        observed_value REAL NOT NULL,
                        control_limit REAL,
                        description TEXT,
                        severity TEXT DEFAULT 'warning',
                        detected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        acknowledged_at DATETIME
                    )
                """)
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_spc_alerts_lookup "
                    "ON spc_alerts (detected_at DESC, severity)"
                )
                log('INFO', 'Migrated: created governance_metrics, spc_control_limits, spc_alerts tables')
        schema_migration.apply_if_below(conn, 10, _v10)

        # ---- V11: confidence_events (F50-PR3 feedback loop) ----
        def _v11(c):
            if not c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='confidence_events'"
            ).fetchone():
                c.execute("""
                    CREATE TABLE IF NOT EXISTS confidence_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        dispatch_id TEXT NOT NULL,
                        terminal TEXT,
                        outcome TEXT NOT NULL,
                        patterns_boosted INTEGER DEFAULT 0,
                        patterns_decayed INTEGER DEFAULT 0,
                        confidence_change REAL NOT NULL,
                        occurred_at TEXT NOT NULL
                    )
                """)
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conf_events_dispatch "
                    "ON confidence_events (dispatch_id)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conf_events_occurred "
                    "ON confidence_events (occurred_at DESC)"
                )
                log('INFO', 'Migrated: added confidence_events table')
        schema_migration.apply_if_below(conn, 11, _v11)

        # ---- V12: T0 advisory + OI delta columns on dispatch_metadata ----
        def _v12(c):
            cols = {r[1] for r in c.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
            if "target_open_items" not in cols:
                c.execute("ALTER TABLE dispatch_metadata ADD COLUMN target_open_items TEXT")
                c.execute(
                    "ALTER TABLE dispatch_metadata ADD COLUMN open_items_created INTEGER DEFAULT 0"
                )
                c.execute(
                    "ALTER TABLE dispatch_metadata ADD COLUMN open_items_resolved INTEGER DEFAULT 0"
                )
                log('INFO', 'Migrated dispatch_metadata: added target_open_items, open_items_created, open_items_resolved columns')
        schema_migration.apply_if_below(conn, 12, _v12)

        # ---- V13: quality_advisory_json for CQS round-trip preservation (OI-1175) ----
        def _v13(c):
            cols = {r[1] for r in c.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
            if "quality_advisory_json" not in cols:
                c.execute("ALTER TABLE dispatch_metadata ADD COLUMN quality_advisory_json TEXT")
                log('INFO', 'Migrated dispatch_metadata: added quality_advisory_json column')
        schema_migration.apply_if_below(conn, 13, _v13)

        # ---- V14: dispatch_id on pattern_usage for dispatch-scoped traceability ----
        def _v14(c):
            if c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pattern_usage'"
            ).fetchone():
                cols = {r[1] for r in c.execute("PRAGMA table_info(pattern_usage)").fetchall()}
                if "dispatch_id" not in cols:
                    c.execute(
                        "ALTER TABLE pattern_usage ADD COLUMN dispatch_id TEXT DEFAULT NULL"
                    )
                    c.execute(
                        "CREATE INDEX IF NOT EXISTS idx_pattern_usage_dispatch_id "
                        "ON pattern_usage (dispatch_id)"
                    )
                    log('INFO', 'Migrated pattern_usage: added dispatch_id column + index')
        schema_migration.apply_if_below(conn, 14, _v14)

        # ---- V15: source_dispatch_id on prevention_rules for audit linkage ----
        def _v15(c):
            cols = {r[1] for r in c.execute("PRAGMA table_info(prevention_rules)").fetchall()}
            if "source_dispatch_id" not in cols:
                c.execute(
                    "ALTER TABLE prevention_rules ADD COLUMN source_dispatch_id TEXT DEFAULT NULL"
                )
                log('INFO', 'Migrated prevention_rules: added source_dispatch_id column')
        schema_migration.apply_if_below(conn, 15, _v15)

        # ---- V16: temporal validity columns (F54 bi-temporal pattern lifecycle) ----
        # SQLite ALTER TABLE does not support non-constant defaults (e.g. CURRENT_TIMESTAMP).
        # Add with DEFAULT NULL, then backfill valid_from for existing rows.
        def _v16(c):
            for tbl in ("success_patterns", "antipatterns", "prevention_rules"):
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if "valid_from" not in cols:
                    c.execute(f"ALTER TABLE {tbl} ADD COLUMN valid_from DATETIME DEFAULT NULL")
                    c.execute(
                        f"UPDATE {tbl} SET valid_from = datetime('now') WHERE valid_from IS NULL"
                    )
                    log('INFO', f'Migrated {tbl}: added valid_from column + backfilled existing rows')
                if "valid_until" not in cols:
                    c.execute(f"ALTER TABLE {tbl} ADD COLUMN valid_until DATETIME DEFAULT NULL")
                    log('INFO', f'Migrated {tbl}: added valid_until column')
        schema_migration.apply_if_below(conn, 16, _v16)

        # ---- V17: dispatch_pattern_offered junction + invalidation_reason columns ----
        # Merges two v17 migrations that landed independently:
        # - dispatch_pattern_offered (this branch — atomic via apply_if_below)
        # - invalidation_reason on success_patterns + antipatterns (from #593 AUDIT-IH-1 main)
        # Both wrapped in single _v17 → atomic via apply_if_below SAVEPOINT
        def _v17(c):
            if not c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='dispatch_pattern_offered'"
            ).fetchone():
                c.execute("""
                    CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
                        dispatch_id   TEXT NOT NULL,
                        pattern_id    TEXT NOT NULL,
                        pattern_title TEXT NOT NULL,
                        offered_at    TEXT NOT NULL,
                        PRIMARY KEY (dispatch_id, pattern_id)
                    )
                """)
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dpo_dispatch_id "
                    "ON dispatch_pattern_offered (dispatch_id)"
                )
                log('INFO', 'Migrated: created dispatch_pattern_offered table + index')
            # Catalog hygiene: invalidation_reason on success_patterns + antipatterns
            # (from #593 AUDIT-IH-1 fix — codex blocker about IF NOT EXISTS invalid on SQLite < 3.37)
            for _htbl in ("success_patterns", "antipatterns"):
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({_htbl})").fetchall()}
                if "invalidation_reason" not in cols:
                    c.execute(f"ALTER TABLE {_htbl} ADD COLUMN invalidation_reason TEXT")
                    log('INFO', f'Migrated {_htbl}: added invalidation_reason column')
        schema_migration.apply_if_below(conn, 17, _v17)

        log('SUCCESS', 'Database schema initialized successfully')
        conn.close()
        return True

    except Exception as e:
        log('ERROR', f'Failed to initialize database: {e}')
        return False

def verify_database_structure() -> bool:
    """Verify all tables, views, and indexes were created"""
    log('INFO', 'Verifying database structure...')

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Expected tables (including FTS5 virtual tables)
        expected_tables = [
            'vnx_code_quality',
            'code_snippets',
            'snippet_metadata',
            'quality_trends',
            'quality_alerts',
            'success_patterns',
            'antipatterns',
            'dispatch_quality_context',
            'quality_system_metrics',
            'scan_history',
            'schema_version',
            'pattern_usage',
            'tag_combinations',
            'prevention_rules',
            'session_analytics',
            'improvement_suggestions',
            'nightly_digests',
            'dispatch_metadata',
            'governance_metrics',
            'spc_control_limits',
            'spc_alerts',
            'confidence_events',
            'report_findings'
        ]

        # Expected views
        expected_views = [
            'high_quality_snippets',
            'files_needing_attention',
            'open_alerts_summary',
            'dispatch_success_by_role',
            'intelligence_effectiveness',
            'cost_per_dispatch'
        ]

        # Check tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        actual_tables = [row[0] for row in cursor.fetchall()]

        missing_tables = set(expected_tables) - set(actual_tables)
        if missing_tables:
            log('ERROR', f'Missing tables: {missing_tables}')
            conn.close()
            return False

        log('SUCCESS', f'All {len(expected_tables)} tables created')

        # Check views
        cursor.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")
        actual_views = [row[0] for row in cursor.fetchall()]

        missing_views = set(expected_views) - set(actual_views)
        if missing_views:
            log('WARNING', f'Missing views: {missing_views}')
            # Views are not critical, continue
        else:
            log('SUCCESS', f'All {len(expected_views)} views created')

        # Check indexes
        cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'")
        index_count = cursor.fetchone()[0]
        log('SUCCESS', f'{index_count} indexes created')

        conn.close()
        return True

    except Exception as e:
        log('ERROR', f'Failed to verify database: {e}')
        return False

def add_initial_metrics() -> bool:
    """Add initial system metrics entry"""
    log('INFO', 'Adding initial system metrics...')

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Add database initialization metric
        cursor.execute("""
            INSERT INTO quality_system_metrics (metric_name, metric_value, metric_unit)
            VALUES (?, ?, ?)
        """, ('database_initialized', 1.0, 'boolean'))

        # Add database size metric
        db_size_bytes = DB_PATH.stat().st_size
        db_size_kb = db_size_bytes / 1024
        cursor.execute("""
            INSERT INTO quality_system_metrics (metric_name, metric_value, metric_unit)
            VALUES (?, ?, ?)
        """, ('database_size', db_size_kb, 'kilobytes'))

        conn.commit()
        conn.close()

        log('SUCCESS', f'Initial metrics added (DB size: {db_size_kb:.2f} KB)')
        return True

    except Exception as e:
        log('ERROR', f'Failed to add initial metrics: {e}')
        return False

def generate_status_report() -> dict:
    """Generate comprehensive status report"""
    log('INFO', 'Generating status report...')

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Database size
        db_size_bytes = DB_PATH.stat().st_size

        # Table counts
        cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        table_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'")
        view_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'")
        index_count = cursor.fetchone()[0]

        # Schema version
        cursor.execute("SELECT version, applied_at, description FROM schema_version ORDER BY applied_at DESC LIMIT 1")
        version_info = cursor.fetchone()

        conn.close()

        report = {
            'database_path': str(DB_PATH),
            'database_size_bytes': db_size_bytes,
            'database_size_kb': round(db_size_bytes / 1024, 2),
            'initialization_time': datetime.now().isoformat(),
            'schema_version': version_info[0] if version_info else 'unknown',
            'schema_applied_at': version_info[1] if version_info else 'unknown',
            'schema_description': version_info[2] if version_info else 'unknown',
            'structure': {
                'tables': table_count,
                'views': view_count,
                'indexes': index_count
            },
            'status': 'operational'
        }

        log('SUCCESS', 'Status report generated')
        return report

    except Exception as e:
        log('ERROR', f'Failed to generate status report: {e}')
        return {'status': 'error', 'error': str(e)}

def main():
    """Main execution flow"""
    print(f"\n{Colors.BLUE}{'='*70}")
    print(f"VNX Quality Intelligence Database Initialization")
    print(f"Version: 8.0.2 (Phase 2)")
    print(f"{'='*70}{Colors.RESET}\n")

    # Step 1: Check prerequisites
    if not check_prerequisites():
        log('ERROR', 'Prerequisites check failed')
        sys.exit(1)

    # Step 2: Backup existing database
    if not backup_existing_db():
        log('ERROR', 'Database backup failed')
        sys.exit(1)

    # Step 3: Initialize database
    if not initialize_database():
        log('ERROR', 'Database initialization failed')
        sys.exit(1)

    # Step 4: Verify structure
    if not verify_database_structure():
        log('ERROR', 'Database verification failed')
        sys.exit(1)

    # Step 5: Add initial metrics
    if not add_initial_metrics():
        log('WARNING', 'Failed to add initial metrics (non-critical)')

    # Step 6: Generate status report
    report = generate_status_report()

    # Print summary
    print(f"\n{Colors.GREEN}{'='*70}")
    print(f"Database Initialization Complete!")
    print(f"{'='*70}{Colors.RESET}\n")

    print(f"Database Path: {report.get('database_path')}")
    print(f"Database Size: {report.get('database_size_kb')} KB")
    print(f"Schema Version: {report.get('schema_version')}")
    print(f"Tables: {report.get('structure', {}).get('tables')}")
    print(f"Views: {report.get('structure', {}).get('views')}")
    print(f"Indexes: {report.get('structure', {}).get('indexes')}")
    print(f"Status: {report.get('status')}")

    # Save report to file
    report_path = STATE_DIR / "quality_db_init_report.json"
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\nStatus report saved to: {report_path}")
    print(f"\n{Colors.GREEN}✅ Ready for quality monitoring operations{Colors.RESET}\n")

if __name__ == "__main__":
    main()
