#!/usr/bin/env python3
"""
Intelligence Daemon Monitor - Adds intelligence daemon status to dashboard
To be called by unified_state_manager_v2.py
"""

import os
import json
import psutil
import sqlite3
from pathlib import Path
from datetime import datetime
import sys
from typing import Dict, Iterable

HIGH_QUALITY_SNIPPET_THRESHOLD = 80.0
HIGH_CONFIDENCE_PATTERN_THRESHOLD = 0.7
REQUIRED_MONITOR_TABLES = {
    "code_snippets": {"quality_score", "last_updated"},
    "success_patterns": {"confidence_score", "first_seen", "last_used"},
}


class SchemaCompatibilityError(RuntimeError):
    """Raised when monitor queries are incompatible with the DB schema."""


def _table_names(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    return {str(row[0]) for row in cursor.fetchall()}


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return {str(row[1]) for row in cursor.fetchall()}


def validate_monitor_schema_compatibility(conn: sqlite3.Connection) -> None:
    """Fail fast when expected monitor tables/columns are missing."""
    tables = _table_names(conn)

    for table_name, required_columns in REQUIRED_MONITOR_TABLES.items():
        if table_name not in tables:
            raise SchemaCompatibilityError(
                f"Missing required table '{table_name}' for intelligence monitor queries"
            )

        existing_columns = _table_columns(conn, table_name)
        missing_columns = sorted(required_columns - existing_columns)
        if missing_columns:
            missing = ", ".join(missing_columns)
            raise SchemaCompatibilityError(
                f"Table '{table_name}' missing required monitor columns: {missing}"
            )


def _latest_timestamp(values: Iterable[str | None]) -> str | None:
    concrete = [value for value in values if value]
    if not concrete:
        return None
    return max(concrete)


def _collect_pattern_metrics(conn: sqlite3.Connection) -> tuple[int, str | None]:
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT COUNT(*) FROM code_snippets
        WHERE CAST(quality_score AS REAL) > ?
        """,
        (HIGH_QUALITY_SNIPPET_THRESHOLD,),
    )
    snippet_count = int(cursor.fetchone()[0] or 0)

    cursor.execute(
        """
        SELECT COUNT(*) FROM success_patterns
        WHERE COALESCE(confidence_score, 0) >= ?
        """,
        (HIGH_CONFIDENCE_PATTERN_THRESHOLD,),
    )
    success_pattern_count = int(cursor.fetchone()[0] or 0)

    cursor.execute("SELECT MAX(last_updated) FROM code_snippets")
    snippets_last_updated = cursor.fetchone()[0]

    cursor.execute("SELECT MAX(first_seen), MAX(last_used) FROM success_patterns")
    first_seen_max, last_used_max = cursor.fetchone()

    return snippet_count + success_pattern_count, _latest_timestamp(
        [snippets_last_updated, first_seen_max, last_used_max]
    )


def run_startup_schema_check(paths: Dict[str, str]) -> None:
    """Compatibility guard for startup flows."""
    db_path = Path(paths["VNX_STATE_DIR"]) / "quality_intelligence.db"
    if not db_path.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        validate_monitor_schema_compatibility(conn)
    finally:
        conn.close()


def get_intelligence_daemon_status(paths: Dict[str, str]) -> dict[str, object]:
    """Get intelligence daemon health status"""

    try:
        # Check if daemon process is running
        pid_file = Path(paths["VNX_PIDS_DIR"]) / "intelligence_daemon.pid"
        daemon_running = False
        pid = None

        if pid_file.exists():
            with open(pid_file, 'r') as f:
                pid = int(f.read().strip())

            # Check if process is actually running
            try:
                process = psutil.Process(pid)
                if process.is_running() and "intelligence_daemon" in ' '.join(process.cmdline()):
                    daemon_running = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                daemon_running = False

        # Check intelligence database for pattern count
        db_path = Path(paths["VNX_STATE_DIR"]) / "quality_intelligence.db"
        pattern_count = 0
        last_extraction = None

        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                validate_monitor_schema_compatibility(conn)
                pattern_count, last_extraction = _collect_pattern_metrics(conn)
            finally:
                conn.close()

        # Calculate uptime if running
        uptime_seconds = 0
        if daemon_running and pid:
            try:
                process = psutil.Process(pid)
                create_time = process.create_time()
                uptime_seconds = int(datetime.now().timestamp() - create_time)
            except Exception:
                pass

        return {
            "status": "healthy" if daemon_running else "offline",
            "last_extraction": last_extraction or datetime.now().isoformat(),
            "patterns_available": pattern_count,
            "extraction_errors": 0,  # Would need to check logs for actual errors
            "uptime_seconds": uptime_seconds,
            "last_update": datetime.now().isoformat()
        }

    except SchemaCompatibilityError:
        raise
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "patterns_available": 0,
            "last_update": datetime.now().isoformat()
        }


def update_dashboard_with_intelligence(dashboard_file: str, paths: Dict[str, str]) -> dict[str, object]:
    """Legacy dashboard updater (disabled by default for single-writer ownership)."""

    # Get intelligence daemon status
    intel_status = get_intelligence_daemon_status(paths)

    if os.getenv("VNX_INTELLIGENCE_DASHBOARD_WRITE", "0") == "1":
        dashboard = {}
        if os.path.exists(dashboard_file):
            with open(dashboard_file, 'r') as f:
                dashboard = json.load(f)
        dashboard["intelligence_daemon"] = intel_status
        with open(dashboard_file, 'w') as f:
            json.dump(dashboard, f, indent=2)

    return intel_status


if __name__ == "__main__":
    # Standalone test mode
    script_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(script_dir / "lib"))
    try:
        from vnx_paths import ensure_env
    except Exception as exc:
        raise SystemExit(f"Failed to load vnx_paths: {exc}")

    paths = ensure_env()
    dashboard_file = str(Path(paths["VNX_STATE_DIR"]) / "dashboard_status.json")

    try:
        run_startup_schema_check(paths)
        status = update_dashboard_with_intelligence(dashboard_file, paths)
        print(f"Intelligence daemon status: {json.dumps(status, indent=2)}")
    except SchemaCompatibilityError as exc:
        raise SystemExit(f"Schema compatibility check failed: {exc}")
