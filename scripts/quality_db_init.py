#!/usr/bin/env python3
"""
Quality Intelligence Database Initialization Script
Version: 8.0.2 (Phase 2)
Purpose: Initialize SQLite Quality Intelligence Database from schema
"""

from __future__ import annotations  # PEP 563: lazy annotation evaluation — Python 3.9 compat

import sqlite3
import sys
from pathlib import Path
from datetime import datetime
from typing import Callable
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
HIGHEST_QI_VERSION = 23

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


# ---------------------------------------------------------------------------
# Module-level migration functions (V2–V18)
# Each function receives the live sqlite3.Connection and runs inside the
# SAVEPOINT already established by schema_migration.apply_if_below.
# Use conn.execute() only — never conn.executescript() (breaks SAVEPOINT).
# ---------------------------------------------------------------------------

def _migrate_v2(conn: sqlite3.Connection) -> None:
    """V2: pattern_hash on snippet_metadata."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(snippet_metadata)").fetchall()}
    if "pattern_hash" not in cols:
        conn.execute("ALTER TABLE snippet_metadata ADD COLUMN pattern_hash TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snippet_pattern_hash "
            "ON snippet_metadata (pattern_hash)"
        )
        log('INFO', 'Migrated snippet_metadata: added pattern_hash column + index')


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """V3: session_analytics, improvement_suggestions, nightly_digests tables."""
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_analytics'"
    ).fetchone():
        conn.execute("""
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_terminal "
            "ON session_analytics (terminal, session_date DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_project "
            "ON session_analytics (project_path, session_date DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_date "
            "ON session_analytics (session_date DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_activity "
            "ON session_analytics (primary_activity)"
        )
        conn.execute("""
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_improvement_category "
            "ON improvement_suggestions (category, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_improvement_priority "
            "ON improvement_suggestions (priority, status)"
        )
        conn.execute("""
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


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """V4: session_model on session_analytics."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(session_analytics)").fetchall()}
    if "session_model" not in cols:
        conn.execute(
            "ALTER TABLE session_analytics ADD COLUMN session_model TEXT DEFAULT 'unknown'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_model "
            "ON session_analytics (session_model, session_date DESC)"
        )
        log('INFO', 'Migrated session_analytics: added session_model column + index')


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """V5: dispatch_id on session_analytics."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(session_analytics)").fetchall()}
    if "dispatch_id" not in cols:
        conn.execute("ALTER TABLE session_analytics ADD COLUMN dispatch_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_dispatch_id "
            "ON session_analytics (dispatch_id)"
        )
        log('INFO', 'Migrated session_analytics: added dispatch_id column + index')


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """V6: context_reset_count on session_analytics."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(session_analytics)").fetchall()}
    if "context_reset_count" not in cols:
        conn.execute(
            "ALTER TABLE session_analytics ADD COLUMN context_reset_count INTEGER DEFAULT 0"
        )
        log('INFO', 'Migrated session_analytics: added context_reset_count column')


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """V7: report_findings table."""
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='report_findings'"
    ).fetchone():
        conn.execute("""
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_report_findings_extracted "
            "ON report_findings (extracted_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_report_findings_dispatch "
            "ON report_findings (dispatch_id)"
        )
        log('INFO', 'Migrated: created report_findings table')


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """V8: dispatch_id on report_findings (for DBs with pre-v7 report_findings)."""
    if conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='report_findings'"
    ).fetchone():
        cols = {r[1] for r in conn.execute("PRAGMA table_info(report_findings)").fetchall()}
        if "dispatch_id" not in cols:
            conn.execute("ALTER TABLE report_findings ADD COLUMN dispatch_id TEXT")
            log('INFO', 'Migrated report_findings: added dispatch_id column')


def _migrate_v9(conn: sqlite3.Connection) -> None:
    """V9: CQS columns on dispatch_metadata."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
    if "cqs" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN cqs REAL")
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN normalized_status TEXT")
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN cqs_components TEXT")
        log('INFO', 'Migrated dispatch_metadata: added cqs, normalized_status, cqs_components columns')


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """V10: governance_metrics, spc_control_limits, spc_alerts."""
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='governance_metrics'"
    ).fetchone():
        conn.execute("""
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gov_metrics_lookup "
            "ON governance_metrics (period_start, scope_type, metric_name)"
        )
        conn.execute("""
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
        conn.execute("""
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_spc_alerts_lookup "
            "ON spc_alerts (detected_at DESC, severity)"
        )
        log('INFO', 'Migrated: created governance_metrics, spc_control_limits, spc_alerts tables')


def _migrate_v11(conn: sqlite3.Connection) -> None:
    """V11: confidence_events (F50-PR3 feedback loop)."""
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='confidence_events'"
    ).fetchone():
        conn.execute("""
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conf_events_dispatch "
            "ON confidence_events (dispatch_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conf_events_occurred "
            "ON confidence_events (occurred_at DESC)"
        )
        log('INFO', 'Migrated: added confidence_events table')


def _migrate_v12(conn: sqlite3.Connection) -> None:
    """V12: T0 advisory + OI delta columns on dispatch_metadata."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
    if "target_open_items" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN target_open_items TEXT")
        conn.execute(
            "ALTER TABLE dispatch_metadata ADD COLUMN open_items_created INTEGER DEFAULT 0"
        )
        conn.execute(
            "ALTER TABLE dispatch_metadata ADD COLUMN open_items_resolved INTEGER DEFAULT 0"
        )
        log('INFO', 'Migrated dispatch_metadata: added target_open_items, open_items_created, open_items_resolved columns')


def _migrate_v13(conn: sqlite3.Connection) -> None:
    """V13: quality_advisory_json for CQS round-trip preservation (OI-1175)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
    if "quality_advisory_json" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN quality_advisory_json TEXT")
        log('INFO', 'Migrated dispatch_metadata: added quality_advisory_json column')


def _migrate_v14(conn: sqlite3.Connection) -> None:
    """V14: dispatch_id on pattern_usage for dispatch-scoped traceability."""
    if conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pattern_usage'"
    ).fetchone():
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pattern_usage)").fetchall()}
        if "dispatch_id" not in cols:
            conn.execute(
                "ALTER TABLE pattern_usage ADD COLUMN dispatch_id TEXT DEFAULT NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pattern_usage_dispatch_id "
                "ON pattern_usage (dispatch_id)"
            )
            log('INFO', 'Migrated pattern_usage: added dispatch_id column + index')


def _migrate_v15(conn: sqlite3.Connection) -> None:
    """V15: source_dispatch_id on prevention_rules for audit linkage."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(prevention_rules)").fetchall()}
    if "source_dispatch_id" not in cols:
        conn.execute(
            "ALTER TABLE prevention_rules ADD COLUMN source_dispatch_id TEXT DEFAULT NULL"
        )
        log('INFO', 'Migrated prevention_rules: added source_dispatch_id column')


def _migrate_v16(conn: sqlite3.Connection) -> None:
    """V16: temporal validity columns (F54 bi-temporal pattern lifecycle).

    SQLite ALTER TABLE does not support non-constant defaults (e.g. CURRENT_TIMESTAMP).
    Add with DEFAULT NULL, then backfill valid_from for existing rows.
    """
    for tbl in ("success_patterns", "antipatterns", "prevention_rules"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
        if "valid_from" not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN valid_from DATETIME DEFAULT NULL")
            conn.execute(
                f"UPDATE {tbl} SET valid_from = datetime('now') WHERE valid_from IS NULL"
            )
            log('INFO', f'Migrated {tbl}: added valid_from column + backfilled existing rows')
        if "valid_until" not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN valid_until DATETIME DEFAULT NULL")
            log('INFO', f'Migrated {tbl}: added valid_until column')


def _migrate_v17(conn: sqlite3.Connection) -> None:
    """V17: dispatch_pattern_offered junction + invalidation_reason columns.

    Merges two v17 migrations that landed independently:
    - dispatch_pattern_offered (this branch — atomic via apply_if_below)
    - invalidation_reason on success_patterns + antipatterns (from #593 AUDIT-IH-1 main)
    Both wrapped in single _v17 → atomic via apply_if_below SAVEPOINT.
    """
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='dispatch_pattern_offered'"
    ).fetchone():
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
                dispatch_id   TEXT NOT NULL,
                pattern_id    TEXT NOT NULL,
                pattern_title TEXT NOT NULL,
                offered_at    TEXT NOT NULL,
                PRIMARY KEY (dispatch_id, pattern_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dpo_dispatch_id "
            "ON dispatch_pattern_offered (dispatch_id)"
        )
        log('INFO', 'Migrated: created dispatch_pattern_offered table + index')
    # Catalog hygiene: invalidation_reason on success_patterns + antipatterns
    # (from #593 AUDIT-IH-1 fix — codex blocker about IF NOT EXISTS invalid on SQLite < 3.37)
    for _htbl in ("success_patterns", "antipatterns"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_htbl})").fetchall()}
        if "invalidation_reason" not in cols:
            conn.execute(f"ALTER TABLE {_htbl} ADD COLUMN invalidation_reason TEXT")
            log('INFO', f'Migrated {_htbl}: added invalidation_reason column')


def _migrate_v19(conn: sqlite3.Connection) -> None:
    """V19: adrs table + FTS5 virtual table + sync triggers (PR-INT-1).

    Table/FTS5 may already exist on fresh installs (created by V1 base schema).
    Triggers are always checked separately because trigger bodies contain
    semicolons that the schema SQL splitter cannot handle.
    """
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='adrs'"
    ).fetchone():
        conn.execute("""
            CREATE TABLE IF NOT EXISTS adrs (
                adr_id              TEXT    NOT NULL,
                project_id          TEXT    NOT NULL DEFAULT 'vnx-dev',
                status              TEXT    NOT NULL,
                title               TEXT    NOT NULL,
                decision_summary    TEXT    NOT NULL,
                binding_rules       TEXT    NOT NULL DEFAULT '[]',
                applies_to_tables   TEXT    NOT NULL DEFAULT '[]',
                applies_to_skills   TEXT    NOT NULL DEFAULT '[]',
                triggers            TEXT    NOT NULL DEFAULT '[]',
                file_path           TEXT    NOT NULL,
                indexed_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                source_hash         TEXT    NOT NULL,
                PRIMARY KEY (adr_id, project_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_adrs_status ON adrs(status)")
        log('INFO', 'Migrated: created adrs table (PR-INT-1)')

    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='adrs_fts'"
    ).fetchone():
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS adrs_fts USING fts5(
                adr_id UNINDEXED,
                title,
                decision_summary,
                binding_rules,
                content='adrs',
                content_rowid='rowid'
            )
        """)
        log('INFO', 'Migrated: created adrs_fts FTS5 virtual table (PR-INT-1)')

    # Triggers checked separately — trigger bodies contain semicolons that the
    # schema SQL splitter cannot handle, so they live here, not in quality_intelligence.sql.
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='adrs_ai'"
    ).fetchone():
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS adrs_ai AFTER INSERT ON adrs BEGIN
                INSERT INTO adrs_fts(rowid, adr_id, title, decision_summary, binding_rules)
                VALUES (new.rowid, new.adr_id, new.title, new.decision_summary, new.binding_rules);
            END
        """)
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='adrs_ad'"
    ).fetchone():
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS adrs_ad AFTER DELETE ON adrs BEGIN
                INSERT INTO adrs_fts(adrs_fts, rowid, adr_id, title, decision_summary, binding_rules)
                VALUES ('delete', old.rowid, old.adr_id, old.title, old.decision_summary, old.binding_rules);
            END
        """)
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='adrs_au'"
    ).fetchone():
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS adrs_au AFTER UPDATE ON adrs BEGIN
                INSERT INTO adrs_fts(adrs_fts, rowid, adr_id, title, decision_summary, binding_rules)
                VALUES ('delete', old.rowid, old.adr_id, old.title, old.decision_summary, old.binding_rules);
                INSERT INTO adrs_fts(rowid, adr_id, title, decision_summary, binding_rules)
                VALUES (new.rowid, new.adr_id, new.title, new.decision_summary, new.binding_rules);
            END
        """)
        log('INFO', 'Migrated: created adrs FTS5 sync triggers (PR-INT-1)')


def _migrate_v18(conn: sqlite3.Connection) -> None:
    """V18: dispatch_experiments (unified from dispatch_tracker.db).

    dispatch_experiments was historically written to dispatch_tracker.db by
    dispatch_parameter_tracker.py. retroactive_backfill.py unified it into
    quality_intelligence.db. The canonical bootstrap did not include a
    CREATE TABLE for it, causing BootstrapFailure when _assert_central_tables_exist
    checked for its presence in source DBs and found it missing in central.
    Schema mirrors scripts/lib/dispatch_parameter_tracker.py::init_schema() plus the
    project_id column that migration 0015 would later ADD COLUMN on existing DBs
    (here we create it upfront so fresh installs get the full schema immediately).

    OI-011 fix: if the table already exists (e.g. created by
    retroactive_backfill._open_tracker() without project_id), add the
    project_id column so apply_composite_unique_constraints can proceed.
    """
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dispatch_experiments'"
    ).fetchone():
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatch_experiments (
                id                  INTEGER PRIMARY KEY,
                dispatch_id         TEXT UNIQUE,
                timestamp           DATETIME DEFAULT CURRENT_TIMESTAMP,
                instruction_chars   INTEGER,
                context_items       INTEGER,
                repo_map_symbols    INTEGER,
                role                TEXT,
                cognition           TEXT,
                model               TEXT,
                terminal            TEXT,
                file_count          INTEGER,
                success             BOOLEAN,
                cqs                 REAL,
                completion_minutes  REAL,
                test_count          INTEGER,
                committed           BOOLEAN,
                lines_changed       INTEGER,
                project_id          TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_de_dispatch_id "
            "ON dispatch_experiments (dispatch_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_de_role "
            "ON dispatch_experiments (role)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_de_timestamp "
            "ON dispatch_experiments (timestamp DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_de_project_id "
            "ON dispatch_experiments (project_id)"
        )
        log('INFO', 'Migrated: created dispatch_experiments table + indexes')
    else:
        # Table already exists (e.g. from retroactive_backfill._open_tracker() which
        # creates it without project_id). Ensure project_id is present so the
        # composite UNIQUE rebuild in apply_composite_unique_constraints can proceed.
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(dispatch_experiments)"
        ).fetchall()}
        if "project_id" not in cols:
            conn.execute(
                "ALTER TABLE dispatch_experiments ADD COLUMN project_id TEXT"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_de_project_id "
                "ON dispatch_experiments (project_id)"
            )
            log('INFO', 'Migrated dispatch_experiments: added missing project_id column + index')


def _migrate_v20(conn: sqlite3.Connection) -> None:
    """V20: dream_cycles + dream_pattern_archives (ADR-019 auto-dream, ADR-007 composite PKs).

    Wires schemas/migrations/0025_dream_consolidation.sql into the bootstrap.
    File is named 0025 following sequential SQL file numbering; internal
    version is v20 (next after v19). ADR-007: both tables carry composite PKs
    over project_id.
    """
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dream_cycles'"
    ).fetchone():
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dream_cycles (
                cycle_id          TEXT    NOT NULL,
                project_id        TEXT    NOT NULL DEFAULT 'vnx-dev',
                started_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                completed_at      TEXT,
                status            TEXT    NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending','running','completed','failed','reviewed','rejected')),
                provider          TEXT    NOT NULL DEFAULT 'kimi',
                insights_input    INTEGER NOT NULL DEFAULT 0,
                merged_count      INTEGER NOT NULL DEFAULT 0,
                dropped_count     INTEGER NOT NULL DEFAULT 0,
                archived_count    INTEGER NOT NULL DEFAULT 0,
                flagged_count     INTEGER NOT NULL DEFAULT 0,
                operator_reviewed INTEGER NOT NULL DEFAULT 0,
                report_path       TEXT,
                PRIMARY KEY (cycle_id, project_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dream_cycles_project_status "
            "ON dream_cycles(project_id, status, started_at DESC)"
        )
        log('INFO', 'Migrated: created dream_cycles table + index (ADR-019, ADR-007 composite PK)')
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dream_pattern_archives'"
    ).fetchone():
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dream_pattern_archives (
                archive_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id            TEXT    NOT NULL,
                project_id          TEXT    NOT NULL DEFAULT 'vnx-dev',
                original_pattern_id INTEGER NOT NULL,
                original_table      TEXT    NOT NULL
                                    CHECK (original_table IN ('success_patterns','antipatterns','intelligence_injections')),
                archived_reason     TEXT    NOT NULL
                                    CHECK (archived_reason IN ('stale_30d','exact_duplicate','merged_into_other','operator_rejected')),
                archived_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (cycle_id, project_id) REFERENCES dream_cycles(cycle_id, project_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dream_archives_cycle "
            "ON dream_pattern_archives(cycle_id, project_id)"
        )
        # Pre-initialize sqlite_sequence high-water-mark (FUT-2A lesson)
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'dream_pattern_archives'")
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES ('dream_pattern_archives', 0)"
        )
        log('INFO', 'Migrated: created dream_pattern_archives table + index (ADR-019, ADR-007)')


def _migrate_v21(conn: sqlite3.Connection) -> None:
    """V21: provider column on dispatch_metadata (provider-aware self-learning).

    Non-Claude dispatches (codex/gemini/kimi/litellm) flow through
    provider_dispatch._emit_governance, which now stamps the provider into the
    dispatch_metadata row so the self-learning/intelligence layer is no longer
    provider-blind.

    ADR-007: Drop-then-recreate ensures the composite (project_id, provider) index
    is the correct shape. The base schema now includes project_id, so this branch
    is always taken on fresh DBs. Legacy DBs without project_id get a plain index
    upgraded to composite by _migrate_v22 after the 0010 migration lands.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
    if "provider" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN provider TEXT")
        log('INFO', 'Migrated dispatch_metadata: added provider column')
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
    # Drop first so the correct shape is always (re)created — idempotent on
    # re-runs (DROP IF EXISTS is a no-op when the index is already absent).
    conn.execute("DROP INDEX IF EXISTS idx_dispatch_meta_provider")
    if "project_id" in cols:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dispatch_meta_provider "
            "ON dispatch_metadata (project_id, provider)"
        )
        log('INFO', 'Migrated dispatch_metadata: composite (project_id, provider) index (ADR-007)')
    else:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dispatch_meta_provider "
            "ON dispatch_metadata (provider)"
        )


def _migrate_v22(conn: sqlite3.Connection) -> None:
    """V22: dispatch_metadata composite UNIQUE (project_id, dispatch_id) + composite provider index.

    ADR-007: the original UNIQUE(dispatch_id) constraint is single-tenant — a cross-
    project UPDATE scoped only by dispatch_id would overwrite any tenant's row. Rebuild
    the table with UNIQUE(project_id, dispatch_id) so each tenant's rows are isolated.

    Also ensures the composite (project_id, provider) index exists for legacy DBs where
    project_id was added by the 0010 migration AFTER _migrate_v21 ran with a plain index.
    Table recreation is the only SQLite-safe way to alter a UNIQUE constraint.
    No destructive data drops — INSERT OR IGNORE preserves all existing rows.

    View-ordering fix (failure mode 2): on legacy DBs (user_version=21) the base-schema
    apply_script_if_below(conn, 1, ...) is skipped (version >= 1), so the three views
    that reference dispatch_metadata already exist in the DB.  SQLite validates all views
    that reference a table when that table is renamed *back* into scope — the RENAME in
    step 4 below was therefore throwing:
        "error in view dispatch_success_by_role: no such table: main.dispatch_metadata"
    because dispatch_metadata was still missing (dropped in step 2, not yet renamed in
    step 4).  Fix: DROP the three dependent views before step 2, recreate them after
    step 4 using the canonical SQL from the base schema.  All three views are idempotent
    (CREATE VIEW IF NOT EXISTS in the base schema) so this is safe on fresh DBs too.
    """
    # Ensure project_id exists before table recreation.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
    if "project_id" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}

    # Check if the composite UNIQUE already exists (idempotent guard).
    tbl_sql = (conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dispatch_metadata'"
    ).fetchone() or ("",))[0]
    needs_rebuild = (
        "UNIQUE (project_id, dispatch_id)" not in tbl_sql
        and "UNIQUE(project_id,dispatch_id)" not in tbl_sql
        and "UNIQUE(project_id, dispatch_id)" not in tbl_sql
    )

    if needs_rebuild:
        # Step 1: Drop views that reference dispatch_metadata so the subsequent
        # DROP TABLE + RENAME sequence succeeds on legacy DBs (user_version=21)
        # where these views already exist.  They are recreated after the rename.
        for _view in (
            "dispatch_success_by_role",
            "intelligence_effectiveness",
            "cost_per_dispatch",
        ):
            conn.execute(f"DROP VIEW IF EXISTS {_view}")

        # Step 2: Build the column list from the live table so any future migration-added
        # columns are preserved without hardcoding them here.
        cols_info = conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()
        col_names = [r[1] for r in cols_info]
        # Exclude id — AUTOINCREMENT PK is re-stamped by the new table.
        non_id_cols = [c for c in col_names if c != "id"]

        # Step 3: Create staging table and copy data.
        conn.execute(f"""
            CREATE TABLE _dispatch_metadata_v22 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                terminal TEXT NOT NULL,
                track TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                role TEXT,
                skill_name TEXT,
                gate TEXT,
                cognition TEXT DEFAULT 'normal',
                priority TEXT DEFAULT 'P1',
                pr_id TEXT,
                parent_dispatch TEXT,
                pattern_count INTEGER DEFAULT 0,
                prevention_rule_count INTEGER DEFAULT 0,
                intelligence_json TEXT,
                instruction_char_count INTEGER DEFAULT 0,
                context_file_count INTEGER DEFAULT 0,
                dispatched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME,
                outcome_status TEXT,
                outcome_report_path TEXT,
                session_id TEXT,
                cqs REAL,
                normalized_status TEXT,
                cqs_components TEXT,
                target_open_items TEXT,
                open_items_created INTEGER DEFAULT 0,
                open_items_resolved INTEGER DEFAULT 0,
                quality_advisory_json TEXT,
                UNIQUE (project_id, dispatch_id)
            )
        """)
        # Copy only columns that exist in both source and destination.
        dest_cols_info = conn.execute("PRAGMA table_info(_dispatch_metadata_v22)").fetchall()
        dest_cols = {r[1] for r in dest_cols_info}
        shared_cols = [c for c in non_id_cols if c in dest_cols]
        shared_list = ", ".join(shared_cols)
        conn.execute(
            f"INSERT OR IGNORE INTO _dispatch_metadata_v22 ({shared_list}) "
            f"SELECT {shared_list} FROM dispatch_metadata"
        )

        # Step 4: Swap the table.
        conn.execute("DROP TABLE dispatch_metadata")
        conn.execute("ALTER TABLE _dispatch_metadata_v22 RENAME TO dispatch_metadata")
        log('INFO', 'Migrated dispatch_metadata: composite UNIQUE (project_id, dispatch_id) (ADR-007)')

        # Step 5: Recreate the three views that were dropped in step 1.
        # These are the canonical view definitions from schemas/quality_intelligence.sql.
        conn.execute("""
            CREATE VIEW IF NOT EXISTS dispatch_success_by_role AS
            SELECT
                role,
                COUNT(*) as total_dispatches,
                SUM(CASE WHEN outcome_status = 'success' THEN 1 ELSE 0 END) as successes,
                ROUND(AVG(CASE WHEN outcome_status = 'success' THEN 1.0 ELSE 0.0 END), 3) as success_rate,
                AVG(pattern_count) as avg_patterns,
                AVG(prevention_rule_count) as avg_rules,
                AVG(instruction_char_count) as avg_instruction_chars
            FROM dispatch_metadata
            WHERE outcome_status IS NOT NULL
            GROUP BY role
            ORDER BY total_dispatches DESC
        """)
        conn.execute("""
            CREATE VIEW IF NOT EXISTS intelligence_effectiveness AS
            SELECT
                CASE WHEN intelligence_json IS NOT NULL AND intelligence_json != '' THEN 'with_intelligence' ELSE 'without_intelligence' END as intelligence_used,
                COUNT(*) as total,
                SUM(CASE WHEN outcome_status = 'success' THEN 1 ELSE 0 END) as successes,
                ROUND(AVG(CASE WHEN outcome_status = 'success' THEN 1.0 ELSE 0.0 END), 3) as success_rate,
                AVG(pattern_count) as avg_patterns
            FROM dispatch_metadata
            WHERE outcome_status IS NOT NULL
            GROUP BY intelligence_used
        """)
        conn.execute("""
            CREATE VIEW IF NOT EXISTS cost_per_dispatch AS
            SELECT
                dm.dispatch_id,
                dm.terminal,
                dm.role,
                dm.gate,
                dm.outcome_status,
                sa.session_model,
                sa.total_input_tokens,
                sa.total_output_tokens,
                sa.tool_calls_total,
                sa.duration_minutes,
                dm.pattern_count,
                dm.instruction_char_count
            FROM dispatch_metadata dm
            LEFT JOIN session_analytics sa ON sa.dispatch_id = dm.dispatch_id
            WHERE dm.outcome_status IS NOT NULL
        """)
        log('INFO', 'Recreated dispatch_metadata-dependent views after table rebuild')

    # Ensure composite (project_id, provider) index — covers legacy DBs where
    # _migrate_v21 ran before project_id existed and left a plain (provider) index.
    conn.execute("DROP INDEX IF EXISTS idx_dispatch_meta_provider")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_meta_provider "
        "ON dispatch_metadata (project_id, provider)"
    )
    # Recreate other indexes (idempotent IF NOT EXISTS).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_meta_id ON dispatch_metadata (dispatch_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_meta_terminal ON dispatch_metadata (terminal)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_meta_role ON dispatch_metadata (role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_meta_gate ON dispatch_metadata (gate)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_meta_outcome ON dispatch_metadata (outcome_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_meta_dispatched ON dispatch_metadata (dispatched_at DESC)")
    log('INFO', 'Migrated dispatch_metadata: composite (project_id, provider) index ensured (ADR-007)')


def _migrate_v23(conn: sqlite3.Connection) -> None:
    """V23: model column on dispatch_metadata (GAP 2 — provider/model-aware intelligence).

    Adds the ``model`` column so the AI model string (e.g. "claude-sonnet-4-6",
    "codex", "kimi") is recorded alongside the provider. Populating model on
    write paths closes GAP 2 (POST-TMUX-LANE-GAPS-2026-06-01.md).

    ADR-007 compliance note: the composite UNIQUE (project_id, dispatch_id) is
    enforced on existing DBs via idx_dispatch_meta_composite_unique (a UNIQUE
    INDEX added by migrate_dispatch_metadata_provider.py without a table rebuild).
    Fresh DBs receive it from the base schema (v22 table rebuild runs on user_version
    < 22 DBs). This migration only adds the model column (ALTER TABLE — no rebuild).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
    if "model" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN model TEXT")
        log('INFO', 'Migrated dispatch_metadata: added model column (GAP 2, v23)')


# Registry mapping version → migration function.
# bootstrap_qi_db iterates this in sorted key order after V1.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v2,
    3: _migrate_v3,
    4: _migrate_v4,
    5: _migrate_v5,
    6: _migrate_v6,
    7: _migrate_v7,
    8: _migrate_v8,
    9: _migrate_v9,
    10: _migrate_v10,
    11: _migrate_v11,
    12: _migrate_v12,
    13: _migrate_v13,
    14: _migrate_v14,
    15: _migrate_v15,
    16: _migrate_v16,
    17: _migrate_v17,
    18: _migrate_v18,
    19: _migrate_v19,
    20: _migrate_v20,
    21: _migrate_v21,
    22: _migrate_v22,
    23: _migrate_v23,
}


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

        # ---- V2–V18: incremental migrations from registry ----
        for version in sorted(MIGRATIONS):
            schema_migration.apply_if_below(conn, version, MIGRATIONS[version])

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
            'report_findings',
            'adrs',
            'dream_cycles',
            'dream_pattern_archives',
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

def run_qi_three_phase_migration(
    qi_db_path: Path,
    pid: str,
) -> dict:
    """Run the W1 3-phase tenant-isolation migration on quality_intelligence.db.

    This is the QI-specific runner (separate transaction from RC, same 3-phase
    logic). Called by the two-DB orchestrator in tenant_stamping.py.

    Phase 1 (DDL): add project_id to composite UNIQUE/PK for tables lacking it.
    Phase 2 (data): re-stamp NULL/'vnx-dev'/'' -> pid (fail-closed guard).
    Phase 3 (DDL): enforce project_id TEXT NOT NULL (no DEFAULT 'vnx-dev').

    Each phase has its own checkpoint + rollback. The function is idempotent:
    already-correct tables are skipped by the phase-level guards.

    Returns a result dict with per-phase outcomes (same schema as RC runner).
    """
    import sys as _sys
    _scripts_lib = Path(__file__).resolve().parent / "lib"
    if str(_scripts_lib) not in _sys.path:
        _sys.path.insert(0, str(_scripts_lib))
    from tenant_stamping import run_three_phase_migration_on_db  # noqa: PLC0415

    return run_three_phase_migration_on_db(
        Path(qi_db_path),
        pid,
        db_label="QI",
        skip_phase3=False,
    )


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

    # Step 2: Backup existing database (non-fatal — migrations are idempotent)
    if not backup_existing_db():
        log('WARNING', 'Database backup failed — continuing without backup (migrations are idempotent)')

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
