#!/usr/bin/env python3
"""Import git-tracked NDJSON from .vnx-intelligence/ into SQLite quality DB.

Hydrates the database from NDJSON exports, rebuilds FTS5 index, and syncs
text files back to .vnx-data/state/ for backward compatibility.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Add lib/ to path for vnx_paths
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from vnx_paths import ensure_env


def _db_path(paths: dict[str, str]) -> Path:
    return Path(paths["VNX_STATE_DIR"]) / "quality_intelligence.db"


def _intelligence_dir(paths: dict[str, str]) -> Path:
    return Path(paths.get("VNX_INTELLIGENCE_DIR", "")) or (
        Path(paths["PROJECT_ROOT"]) / ".vnx-intelligence"
    )


def _schema_path(paths: dict[str, str]) -> Path:
    return Path(paths["VNX_HOME"]) / "schemas" / "quality_intelligence.sql"


def _ensure_schema(conn: sqlite3.Connection, schema_file: Path) -> None:
    """Apply schema if tables don't exist yet."""
    if not schema_file.is_file():
        return
    # Check if any of our tables exist
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "vnx_code_quality" in tables:
        return  # Schema already applied
    schema_sql = schema_file.read_text(encoding="utf-8")
    conn.executescript(schema_sql)


def _get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Get column names for a table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in rows]


def _infer_sqlite_type(value) -> str:
    """Infer SQLite column type from a Python value."""
    if value is None:
        return "TEXT"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    return "TEXT"


def _auto_create_table(conn: sqlite3.Connection, table: str, ndjson_path: Path) -> bool:
    """Create a table by inferring schema from the first NDJSON record. Returns True if created."""
    if not ndjson_path.is_file():
        return False
    with open(ndjson_path, "r", encoding="utf-8") as f:
        first_line = f.readline().strip()
    if not first_line:
        return False
    record = json.loads(first_line)
    if not record:
        return False
    cols = []
    for col, val in record.items():
        col_type = _infer_sqlite_type(val)
        if col == "id":
            cols.append(f"{col} INTEGER PRIMARY KEY AUTOINCREMENT")
        else:
            cols.append(f"{col} {col_type}")
    ddl = f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(cols)})"
    conn.execute(ddl)
    return True


def _ensure_columns(conn: sqlite3.Connection, table: str, record: dict, db_columns: set[str]) -> set[str]:
    """Add any columns present in the NDJSON record but missing from the DB table.
    Returns the updated set of DB columns."""
    missing = set(record.keys()) - db_columns
    for col in sorted(missing):
        col_type = _infer_sqlite_type(record[col])
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        db_columns.add(col)
    return db_columns


def _import_table(conn: sqlite3.Connection, table: str, ndjson_path: Path) -> int:
    """Import NDJSON into a table using INSERT OR REPLACE. Returns row count."""
    if not ndjson_path.is_file():
        return 0

    db_columns = set(_get_columns(conn, table))
    if not db_columns:
        return 0

    columns_extended = False
    count = 0
    with open(ndjson_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            # On first record (or if new columns appear), extend the table schema
            if not columns_extended or not set(record.keys()).issubset(db_columns):
                db_columns = _ensure_columns(conn, table, record, db_columns)
                columns_extended = True
            cols = [c for c in record if c in db_columns]
            if not cols:
                continue
            placeholders = ", ".join("?" for _ in cols)
            col_names = ", ".join(cols)
            values = [record[c] for c in cols]
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})",
                values,
            )
            count += 1

    return count


def _sync_text_file(src: Path, dest: Path) -> bool:
    """Copy a text file if it exists. Returns True if copied."""
    if not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dest))
    return True


# Text files to restore from .vnx-intelligence/ back to .vnx-data/
TEXT_FILE_RESTORES = [
    ("receipts/t0_receipts.ndjson", "state/t0_receipts.ndjson"),
    ("open_items/open_items.json", "state/open_items.json"),
    ("queue/pr_queue_state.json", "state/pr_queue_state.json"),
]


def import_intelligence(paths: dict[str, str] | None = None) -> dict:
    """Run the full intelligence import. Returns metadata dict."""
    if paths is None:
        paths = ensure_env()

    intel_dir = _intelligence_dir(paths)
    if not intel_dir.is_dir():
        print(f"[intelligence-import] No .vnx-intelligence/ found at: {intel_dir}", file=sys.stderr)
        return {"error": "intelligence_dir_not_found"}

    db_export_dir = intel_dir / "db_export"
    if not db_export_dir.is_dir():
        print(f"[intelligence-import] No db_export/ found in: {intel_dir}", file=sys.stderr)
        return {"error": "db_export_not_found"}

    db = _db_path(paths)
    schema_file = _schema_path(paths)

    # Ensure state directory exists
    db.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")

    # Apply schema if needed
    _ensure_schema(conn, schema_file)

    # Read export metadata
    meta_file = db_export_dir / "_export_meta.json"
    export_meta = {}
    if meta_file.is_file():
        export_meta = json.loads(meta_file.read_text(encoding="utf-8"))

    result = {
        "source": str(intel_dir),
        "db": str(db),
        "tables_imported": {},
        "export_timestamp": export_meta.get("export_timestamp"),
    }

    # Import each NDJSON file
    for ndjson_file in sorted(db_export_dir.glob("*.ndjson")):
        table = ndjson_file.stem
        # Verify table exists in DB
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if table not in tables:
            # Auto-create table from NDJSON schema (handles tables added by migrations)
            if not _auto_create_table(conn, table, ndjson_file):
                continue
        count = _import_table(conn, table, ndjson_file)
        if count > 0:
            result["tables_imported"][table] = count

    conn.commit()
    conn.close()

    # Restore text files to .vnx-data/ for backward compatibility
    data_dir = Path(paths["VNX_DATA_DIR"])
    for intel_rel, data_rel in TEXT_FILE_RESTORES:
        _sync_text_file(intel_dir / intel_rel, data_dir / data_rel)

    total_rows = sum(result["tables_imported"].values())
    table_count = len(result["tables_imported"])
    print(f"[intelligence-import] Imported {total_rows} rows into {table_count} tables from {intel_dir}")
    return result


if __name__ == "__main__":
    result = import_intelligence()
    if result.get("error"):
        sys.exit(1)
