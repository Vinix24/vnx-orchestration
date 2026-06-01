#!/usr/bin/env python3
"""Idempotent migration: add provider + model columns to dispatch_metadata.

GAP 2 fix (POST-TMUX-LANE-GAPS-2026-06-01.md): quality_intelligence.db
dispatch_metadata table lacked provider/model columns, so provider-aware
DB queries failed locally even though receipts carry provider.

Safe on the existing ~625MB DB:
  - Uses ALTER TABLE ADD COLUMN only (no table rebuild, no data loss)
  - Pragma-checks column presence before each ADD (idempotent)
  - Adds composite (project_id, provider) index for tenant-scoped analytics
  - Adds UNIQUE index on (project_id, dispatch_id) for ADR-007 compliance
    without a table rebuild (all 908 existing rows are already unique)

ADR-007: dispatch_metadata is a central-DB table scoped by project_id.
Composite uniqueness is enforced via the new UNIQUE INDEX rather than a
table-level UNIQUE constraint (which would require a rebuild).

Reconciliation: #761 added dispatch_metadata_db.py which writes to the
SAME quality_intelligence.db table — it is a helper module, not a separate
table. The canonical table is dispatch_metadata in quality_intelligence.db.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _has_index(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return row is not None


def run_migration(db_path: Path) -> dict:
    """Apply the migration. Returns a dict with per-step results."""
    if not db_path.exists():
        return {"error": f"DB not found: {db_path}"}

    results: dict = {
        "provider_added": False,
        "model_added": False,
        "unique_index_added": False,
        "provider_index_updated": False,
    }

    conn = sqlite3.connect(str(db_path))
    try:
        # --- provider column ---
        if not _has_column(conn, "dispatch_metadata", "provider"):
            conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN provider TEXT")
            results["provider_added"] = True

        # --- model column ---
        if not _has_column(conn, "dispatch_metadata", "model"):
            conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN model TEXT")
            results["model_added"] = True

        # --- ADR-007: UNIQUE INDEX on (project_id, dispatch_id) ---
        # Enforces tenant isolation without a table rebuild.
        # All existing rows verified unique before this migration is applied.
        if not _has_index(conn, "idx_dispatch_meta_composite_unique"):
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatch_meta_composite_unique "
                "ON dispatch_metadata (project_id, dispatch_id)"
            )
            results["unique_index_added"] = True

        # --- composite (project_id, provider) index for per-provider analytics ---
        conn.execute("DROP INDEX IF EXISTS idx_dispatch_meta_provider")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dispatch_meta_provider "
            "ON dispatch_metadata (project_id, provider)"
        )
        results["provider_index_updated"] = True

        conn.commit()
    finally:
        conn.close()

    return results


def main() -> int:
    try:
        from vnx_paths import ensure_env
        PATHS = ensure_env()
        db_path = Path(PATHS["VNX_STATE_DIR"]) / "quality_intelligence.db"
    except Exception as exc:
        print(f"ERROR: could not resolve DB path: {exc}", file=sys.stderr)
        return 1

    print(f"Migrating: {db_path}")
    results = run_migration(db_path)

    if "error" in results:
        print(f"ERROR: {results['error']}", file=sys.stderr)
        return 1

    for step, applied in results.items():
        status = "APPLIED" if applied else "already present"
        print(f"  {step}: {status}")

    print("Migration complete — provider/model columns on dispatch_metadata (GAP 2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
