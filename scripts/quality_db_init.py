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
    """Initialize database from schema file"""
    log('INFO', 'Initializing quality intelligence database...')

    try:
        # Read schema file
        with open(SCHEMA_FILE, 'r') as f:
            schema_sql = f.read()

        log('INFO', f'Schema loaded: {len(schema_sql)} characters')

        # Connect to database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Execute schema
        cursor.executescript(schema_sql)
        conn.commit()

        # Migration: add pattern_hash column if missing (for existing databases)
        cursor.execute("PRAGMA table_info(snippet_metadata)")
        columns = {row[1] for row in cursor.fetchall()}
        if "pattern_hash" not in columns:
            cursor.execute("ALTER TABLE snippet_metadata ADD COLUMN pattern_hash TEXT")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_snippet_pattern_hash ON snippet_metadata (pattern_hash)")
            conn.commit()
            log('INFO', 'Migrated snippet_metadata: added pattern_hash column + index')

        # Migration: add session_analytics tables if missing (for existing databases)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_analytics'")
        if not cursor.fetchone():
            cursor.executescript("""
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
                );
                CREATE INDEX IF NOT EXISTS idx_session_terminal ON session_analytics (terminal, session_date DESC);
                CREATE INDEX IF NOT EXISTS idx_session_project ON session_analytics (project_path, session_date DESC);
                CREATE INDEX IF NOT EXISTS idx_session_date ON session_analytics (session_date DESC);
                CREATE INDEX IF NOT EXISTS idx_session_activity ON session_analytics (primary_activity);

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
                );
                CREATE INDEX IF NOT EXISTS idx_improvement_category ON improvement_suggestions (category, status);
                CREATE INDEX IF NOT EXISTS idx_improvement_priority ON improvement_suggestions (priority, status);

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
                );
            """)
            conn.commit()
            log('INFO', 'Migrated: added session_analytics, improvement_suggestions, nightly_digests tables')

        # Migration: add session_model column if missing (for existing databases)
        cursor.execute("PRAGMA table_info(session_analytics)")
        sa_columns = {row[1] for row in cursor.fetchall()}
        if "session_model" not in sa_columns:
            cursor.execute("ALTER TABLE session_analytics ADD COLUMN session_model TEXT DEFAULT 'unknown'")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_model ON session_analytics (session_model, session_date DESC)")
            conn.commit()
            log('INFO', 'Migrated session_analytics: added session_model column + index')

        # Migration: add dispatch_id column to session_analytics if missing
        cursor.execute("PRAGMA table_info(session_analytics)")
        sa_cols = {row[1] for row in cursor.fetchall()}
        if "dispatch_id" not in sa_cols:
            cursor.execute("ALTER TABLE session_analytics ADD COLUMN dispatch_id TEXT")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_dispatch_id ON session_analytics (dispatch_id)")
            conn.commit()
            log('INFO', 'Migrated session_analytics: added dispatch_id column + index')

        # Migration: add context_reset_count to session_analytics if missing
        cursor.execute("PRAGMA table_info(session_analytics)")
        sa_cols2 = {row[1] for row in cursor.fetchall()}
        if "context_reset_count" not in sa_cols2:
            cursor.execute("ALTER TABLE session_analytics ADD COLUMN context_reset_count INTEGER DEFAULT 0")
            conn.commit()
            log('INFO', 'Migrated session_analytics: added context_reset_count column')

        # Migration: add dispatch_id column to report_findings if missing
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='report_findings'")
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(report_findings)")
            rf_cols = {row[1] for row in cursor.fetchall()}
            if "dispatch_id" not in rf_cols:
                cursor.execute("ALTER TABLE report_findings ADD COLUMN dispatch_id TEXT")
                conn.commit()
                log('INFO', 'Migrated report_findings: added dispatch_id column')

        # Migration: add CQS columns to dispatch_metadata if missing
        cursor.execute("PRAGMA table_info(dispatch_metadata)")
        dm_cols = {row[1] for row in cursor.fetchall()}
        if "cqs" not in dm_cols:
            cursor.execute("ALTER TABLE dispatch_metadata ADD COLUMN cqs REAL")
            cursor.execute("ALTER TABLE dispatch_metadata ADD COLUMN normalized_status TEXT")
            cursor.execute("ALTER TABLE dispatch_metadata ADD COLUMN cqs_components TEXT")
            conn.commit()
            log('INFO', 'Migrated dispatch_metadata: added cqs, normalized_status, cqs_components columns')

        # Migration: create governance tables if missing
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='governance_metrics'")
        if not cursor.fetchone():
            cursor.executescript("""
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
                );
                CREATE INDEX IF NOT EXISTS idx_gov_metrics_lookup
                    ON governance_metrics (period_start, scope_type, metric_name);

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
                );

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
                );
                CREATE INDEX IF NOT EXISTS idx_spc_alerts_lookup
                    ON spc_alerts (detected_at DESC, severity);
            """)
            conn.commit()
            log('INFO', 'Migrated: created governance_metrics, spc_control_limits, spc_alerts tables')

        # Migration: add CQS enhancement columns (T0 advisory + OI delta) if missing
        cursor.execute("PRAGMA table_info(dispatch_metadata)")
        dm_cols_v2 = {row[1] for row in cursor.fetchall()}
        if "target_open_items" not in dm_cols_v2:
            cursor.execute("ALTER TABLE dispatch_metadata ADD COLUMN target_open_items TEXT")
            cursor.execute("ALTER TABLE dispatch_metadata ADD COLUMN open_items_created INTEGER DEFAULT 0")
            cursor.execute("ALTER TABLE dispatch_metadata ADD COLUMN open_items_resolved INTEGER DEFAULT 0")
            conn.commit()
            log('INFO', 'Migrated dispatch_metadata: added target_open_items, open_items_created, open_items_resolved columns')

        log('SUCCESS', 'Database schema initialized successfully')

        # Close connection
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
            'spc_alerts'
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
