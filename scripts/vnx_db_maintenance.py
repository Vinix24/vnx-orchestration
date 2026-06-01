#!/usr/bin/env python3
"""Opt-in maintenance CLI for quality_intelligence.db.

Prunes old rows from high-growth tables and VACUUMs the database to reclaim
space. NEVER auto-deletes — must be run explicitly with --apply.

Protected tables (never touched):
    dispatch_metadata, receipts (NDJSON-only, not in DB)

Prunable tables:
    code_snippets      FTS5 virtual table — pruned by last_updated
    snippet_metadata   companion table   — pruned by created_at
    session_analytics  session log       — pruned by session_date

Usage:
    python3 scripts/vnx_db_maintenance.py --dry-run          # show what would be pruned
    python3 scripts/vnx_db_maintenance.py --apply            # prune + VACUUM
    python3 scripts/vnx_db_maintenance.py --apply --retention-days 90

Environment variables:
    VNX_DB_RETENTION_DAYS   Retention window in days (default: 180)
    VNX_STATE_DIR           State directory (resolved via vnx_paths if unset)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

DEFAULT_RETENTION_DAYS = 180

PROTECTED_TABLES = frozenset({"dispatch_metadata"})

PRUNABLE_TABLES = [
    {
        "table": "code_snippets",
        "date_col": "last_updated",
        "label": "code_snippets (FTS5)",
    },
    {
        "table": "snippet_metadata",
        "date_col": "created_at",
        "label": "snippet_metadata",
    },
    {
        "table": "session_analytics",
        "date_col": "session_date",
        "label": "session_analytics",
    },
]


def _resolve_db_path(db_path: Optional[str] = None) -> Path:
    if db_path:
        return Path(db_path).expanduser().resolve()
    try:
        from vnx_paths import ensure_env
        paths = ensure_env()
        return Path(paths["VNX_STATE_DIR"]) / "quality_intelligence.db"
    except Exception:
        state_dir = os.environ.get("VNX_STATE_DIR", "")
        if not state_dir:
            raise RuntimeError("Cannot resolve db path: VNX_STATE_DIR not set")
        return Path(state_dir) / "quality_intelligence.db"


def _db_size_bytes(db_path: Path) -> int:
    return db_path.stat().st_size if db_path.exists() else 0


def _count_prunable(conn: sqlite3.Connection, table: str, date_col: str, cutoff: str) -> int:
    try:
        cur = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {date_col} IS NOT NULL AND {date_col} < ?",
            (cutoff,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _total_rows(conn: sqlite3.Connection, table: str) -> int:
    try:
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _get_page_info(conn: sqlite3.Connection) -> tuple[int, int]:
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    return page_size, page_count


def _estimate_reclaimable(conn: sqlite3.Connection, total_prunable: int, db_size: int) -> int:
    try:
        page_size, page_count = _get_page_info(conn)
        total_pages = page_count
        if total_pages == 0 or db_size == 0:
            return 0

        all_rows = sum(_total_rows(conn, spec["table"]) for spec in PRUNABLE_TABLES)
        if all_rows == 0:
            return 0

        fraction = min(total_prunable / all_rows, 1.0)
        return int(db_size * fraction)
    except Exception:
        return 0


def dry_run(db_path: Optional[str] = None, retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    path = _resolve_db_path(db_path)
    if not path.exists():
        return {"error": f"Database not found: {path}", "db_path": str(path)}

    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)).date().isoformat()
    db_size = _db_size_bytes(path)

    conn = sqlite3.connect(str(path))
    try:
        table_reports = []
        total_prunable = 0

        for spec in PRUNABLE_TABLES:
            prunable = _count_prunable(conn, spec["table"], spec["date_col"], cutoff)
            total = _total_rows(conn, spec["table"])
            total_prunable += prunable
            table_reports.append({
                "table": spec["table"],
                "label": spec["label"],
                "date_col": spec["date_col"],
                "total_rows": total,
                "would_prune": prunable,
                "would_keep": total - prunable,
            })

        reclaimable = _estimate_reclaimable(conn, total_prunable, db_size)

        return {
            "dry_run": True,
            "db_path": str(path),
            "db_size_bytes": db_size,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "retention_days": retention_days,
            "cutoff_date": cutoff,
            "tables": table_reports,
            "total_would_prune": total_prunable,
            "estimated_reclaimable_bytes": reclaimable,
            "estimated_reclaimable_mb": round(reclaimable / (1024 * 1024), 2),
        }
    finally:
        conn.close()


def apply(db_path: Optional[str] = None, retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    path = _resolve_db_path(db_path)
    if not path.exists():
        return {"error": f"Database not found: {path}", "db_path": str(path)}

    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)).date().isoformat()
    size_before = _db_size_bytes(path)

    conn = sqlite3.connect(str(path))
    pruned_counts = {}

    try:
        for spec in PRUNABLE_TABLES:
            table = spec["table"]
            date_col = spec["date_col"]
            try:
                if table == "code_snippets":
                    # Prune snippet_metadata first (FK reference to code_snippets.rowid),
                    # then prune the FTS5 table itself.
                    conn.execute(
                        "DELETE FROM snippet_metadata WHERE snippet_rowid IN "
                        "(SELECT rowid FROM code_snippets WHERE last_updated IS NOT NULL AND last_updated < ?)",
                        (cutoff,),
                    )
                    cur = conn.execute(
                        "DELETE FROM code_snippets WHERE last_updated IS NOT NULL AND last_updated < ?",
                        (cutoff,),
                    )
                    pruned_counts[table] = cur.rowcount
                    pruned_counts["snippet_metadata"] = pruned_counts.get("snippet_metadata", 0)
                elif table == "snippet_metadata":
                    # Already handled above when pruning code_snippets.
                    # Only run standalone if code_snippets was skipped or missing.
                    if "code_snippets" not in pruned_counts:
                        cur = conn.execute(
                            f"DELETE FROM {table} WHERE {date_col} IS NOT NULL AND {date_col} < ?",
                            (cutoff,),
                        )
                        pruned_counts[table] = cur.rowcount
                else:
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE {date_col} IS NOT NULL AND {date_col} < ?",
                        (cutoff,),
                    )
                    pruned_counts[table] = cur.rowcount
            except sqlite3.OperationalError as exc:
                pruned_counts[table] = 0
                pruned_counts[f"{table}_error"] = str(exc)

        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    size_after = _db_size_bytes(path)
    reclaimed = max(0, size_before - size_after)

    # Write append-only audit ledger (durable record of destructive maintenance).
    audit_path = path.parent / "db_maintenance_audit.ndjson"
    audit_record = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "op": "db_maintenance",
        "db_path": str(path),
        "retention_days": retention_days,
        "pruned": pruned_counts,
        "bytes_reclaimed": reclaimed,
        "vacuumed": True,
    }
    try:
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(audit_record, separators=(",", ":")) + "\n")
    except Exception as exc:
        print(f"[vnx_db_maintenance] Failed to write audit ledger: {exc}", file=sys.stderr)

    return {
        "dry_run": False,
        "applied": True,
        "db_path": str(path),
        "retention_days": retention_days,
        "cutoff_date": cutoff,
        "size_before_bytes": size_before,
        "size_after_bytes": size_after,
        "size_before_mb": round(size_before / (1024 * 1024), 2),
        "size_after_mb": round(size_after / (1024 * 1024), 2),
        "reclaimed_bytes": reclaimed,
        "reclaimed_mb": round(reclaimed / (1024 * 1024), 2),
        "pruned": pruned_counts,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="VNX DB maintenance: prune old rows and VACUUM quality_intelligence.db")
    parser.add_argument("--db-path", default=None, help="Override database path")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=int(os.environ.get("VNX_DB_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
        help=f"Rows older than N days are pruned (env: VNX_DB_RETENTION_DAYS, default: {DEFAULT_RETENTION_DAYS})",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Show what would be pruned without deleting")
    mode.add_argument("--apply", action="store_true", help="Prune rows and VACUUM the database")
    args = parser.parse_args(argv)

    import json
    if args.dry_run:
        result = dry_run(db_path=args.db_path, retention_days=args.retention_days)
    else:
        result = apply(db_path=args.db_path, retention_days=args.retention_days)

    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
