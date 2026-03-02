#!/usr/bin/env python3
"""
Quality Intelligence Database Initialization Script
Version: 8.0.2 (Phase 2)
Purpose: Initialize SQLite Quality Intelligence Database from schema
"""

import sqlite3
import os
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
            'prevention_rules'
        ]

        # Expected views
        expected_views = [
            'high_quality_snippets',
            'files_needing_attention',
            'open_alerts_summary'
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
