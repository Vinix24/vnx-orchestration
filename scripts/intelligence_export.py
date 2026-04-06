#!/usr/bin/env python3
"""Export SQLite quality intelligence to git-tracked NDJSON in .vnx-intelligence/.

Produces deterministic output: ORDER BY PK, sort_keys=True, consistent datetime
formatting. Identical DB state produces identical output (no git diff noise).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add lib/ to path for vnx_paths
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from vnx_paths import ensure_env

# Tables to export from SQLite (skip FTS5 shadow tables, internal sqlite tables, views)
EXPORTABLE_TABLES = [
    "vnx_code_quality",
    "session_analytics",
    "dispatch_metadata",
    "dispatch_quality_context",
    "antipatterns",
    "success_patterns",
    "governance_metrics",
    "pattern_usage",
    "prevention_rules",
    "tag_combinations",
    "improvement_suggestions",
    "snippet_metadata",
    "spc_control_limits",
    "spc_alerts",
    "schema_version",
    "quality_trends",
    "quality_alerts",
    "quality_system_metrics",
    "scan_history",
    "nightly_digests",
    "report_findings",
]

# Text files to sync from .vnx-data/ into .vnx-intelligence/
TEXT_FILE_SYNCS = [
    # (source relative to VNX_DATA_DIR, dest relative to VNX_INTELLIGENCE_DIR)
    ("state/t0_receipts.ndjson", "receipts/t0_receipts.ndjson"),
    ("state/open_items.json", "open_items/open_items.json"),
    ("state/pr_queue_state.json", "queue/pr_queue_state.json"),
]

SKIP_TABLE_PREFIXES = (
    "code_snippets_",  # FTS5 shadow tables
    "sqlite_",         # internal sqlite tables
)

SKIP_TABLE_NAMES = {
    "code_snippets",   # FTS5 virtual table (not directly exportable)
}


def _db_path(paths: dict[str, str]) -> Path:
    return Path(paths["VNX_STATE_DIR"]) / "quality_intelligence.db"


def _intelligence_dir(paths: dict[str, str]) -> Path:
    configured = paths.get("VNX_INTELLIGENCE_DIR")
    if configured:
        return Path(configured)
    return Path(paths.get("VNX_CANONICAL_ROOT") or paths["VNX_HOME"]) / ".vnx-intelligence"


def _should_skip_table(name: str) -> bool:
    if name in SKIP_TABLE_NAMES:
        return True
    for prefix in SKIP_TABLE_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def _get_primary_key(conn: sqlite3.Connection, table: str) -> str:
    """Get the primary key column for ORDER BY. Falls back to rowid."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    for row in rows:
        if row[5]:  # pk flag
            return row[1]  # column name
    return "rowid"


def _export_table(conn: sqlite3.Connection, table: str, out_path: Path) -> int:
    """Export a single table to NDJSON. Returns row count."""
    pk = _get_primary_key(conn, table)
    cursor = conn.execute(f"SELECT * FROM {table} ORDER BY {pk}")
    columns = [desc[0] for desc in cursor.description]
    count = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for row in cursor:
            record = dict(zip(columns, row))
            f.write(json.dumps(record, sort_keys=True, ensure_ascii=False, default=str))
            f.write("\n")
            count += 1
    return count


def _sync_text_file(src: Path, dest: Path) -> bool:
    """Copy a text file if it exists. Returns True if copied."""
    if not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dest))
    return True


def _sync_directory(src_dir: Path, dest_dir: Path, pattern: str = "*") -> int:
    """Sync all matching files from src to dest. Returns count."""
    if not src_dir.is_dir():
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in sorted(src_dir.glob(pattern)):
        if f.is_file():
            shutil.copy2(str(f), str(dest_dir / f.name))
            count += 1
    return count


def export_intelligence(paths: dict[str, str] | None = None) -> dict:
    """Run the full intelligence export. Returns metadata dict."""
    if paths is None:
        paths = ensure_env()

    db = _db_path(paths)
    if not db.is_file():
        print(f"[intelligence-export] WARN: Database not found: {db}", file=sys.stderr)
        return {"error": "database_not_found"}

    intel_dir = _intelligence_dir(paths)
    data_dir = Path(paths["VNX_DATA_DIR"])

    # Atomic write: export to temp dir, then move in place
    tmp_dir = intel_dir.parent / ".vnx-intelligence.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)

    db_export_dir = tmp_dir / "db_export"
    db_export_dir.mkdir(parents=True)

    # Connect read-only
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = None

    # Discover actual tables in DB
    actual_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    meta = {
        "export_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_db": str(db),
        "schema_version": None,
        "tables": {},
    }

    # Get schema version
    if "schema_version" in actual_tables:
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY applied_at DESC LIMIT 1"
        ).fetchone()
        if row:
            meta["schema_version"] = row[0]

    # Export each table
    for table in EXPORTABLE_TABLES:
        if table not in actual_tables:
            continue
        if _should_skip_table(table):
            continue
        out_file = db_export_dir / f"{table}.ndjson"
        count = _export_table(conn, table, out_file)
        meta["tables"][table] = count

    conn.close()

    # Write export metadata
    with open(db_export_dir / "_export_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")

    # Sync text files
    for src_rel, dest_rel in TEXT_FILE_SYNCS:
        src = data_dir / src_rel
        dest = tmp_dir / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        _sync_text_file(src, dest)

    # Sync evidence directory
    evidence_src = data_dir / "evidence"
    if evidence_src.is_dir():
        evidence_dest = tmp_dir / "evidence"
        for pr_dir in sorted(evidence_src.iterdir()):
            if pr_dir.is_dir():
                _sync_directory(pr_dir, evidence_dest / pr_dir.name)

    # Sync completed dispatches
    completed_src = data_dir / "dispatches" / "completed"
    completed_dest = tmp_dir / "dispatches" / "completed"
    _sync_directory(completed_src, completed_dest, "*.md")

    # Sync rejected dispatches
    rejected_src = data_dir / "dispatches" / "rejected"
    rejected_dest = tmp_dir / "dispatches" / "rejected"
    _sync_directory(rejected_src, rejected_dest, "*.md")

    # Write schema version marker
    with open(tmp_dir / ".export_version", "w") as f:
        f.write(meta.get("schema_version") or "unknown")
        f.write("\n")

    # Atomic swap: remove old, rename tmp
    if intel_dir.exists():
        shutil.rmtree(intel_dir)
    tmp_dir.rename(intel_dir)

    total_rows = sum(meta["tables"].values())
    table_count = len(meta["tables"])
    print(f"[intelligence-export] Exported {total_rows} rows from {table_count} tables to {intel_dir}")
    return meta


if __name__ == "__main__":
    result = export_intelligence()
    if result.get("error"):
        sys.exit(1)
