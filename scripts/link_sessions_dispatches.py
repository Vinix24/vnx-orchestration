#!/usr/bin/env python3
"""Nightly cross-reference: link sessions, dispatches, and receipts.

Run as Phase 1.5 in conversation_analyzer_nightly.sh.
Performs three linkage passes:
  1. session_analytics.dispatch_id -> dispatch_metadata.session_id
  2. Receipts -> dispatch_metadata.outcome_status
  3. report_findings.dispatch_id from report metadata
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
    from report_findings_migration import ensure_report_findings_table
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths or report_findings_migration: {exc}")

PATHS = ensure_env()
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"
RECEIPTS_FILE = STATE_DIR / "t0_receipts.ndjson"

DISPATCH_ID_RE = re.compile(r'\|\s*\*\*Dispatch-ID\*\*\s*\|\s*([^\|]+?)\s*\|')
DISPATCH_HEADER_RE = re.compile(r'Dispatch-ID:\s*(\S+)')


def link_sessions_to_dispatches(conn: sqlite3.Connection) -> int:
    """Bidirectional link: set dispatch_metadata.session_id from session_analytics."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE dispatch_metadata
        SET session_id = (
            SELECT sa.session_id FROM session_analytics sa
            WHERE sa.dispatch_id = dispatch_metadata.dispatch_id
            LIMIT 1
        )
        WHERE session_id IS NULL
        AND EXISTS (
            SELECT 1 FROM session_analytics sa
            WHERE sa.dispatch_id = dispatch_metadata.dispatch_id
        )
    """)
    updated = cur.rowcount
    conn.commit()
    return updated


def link_receipts_to_dispatches(conn: sqlite3.Connection) -> int:
    """Update dispatch outcomes from receipt file."""
    if not RECEIPTS_FILE.exists():
        return 0

    cur = conn.cursor()
    linked = 0

    with open(RECEIPTS_FILE, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                receipt = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = receipt.get("event_type") or receipt.get("event", "")
            if event_type not in ("task_complete", "task_failed", "task_timeout"):
                continue

            dispatch_id = receipt.get("dispatch_id", "")
            if not dispatch_id:
                continue

            status = receipt.get("status", "unknown")
            report_path = receipt.get("report_path", "")
            timestamp = receipt.get("timestamp", "")

            cur.execute("""
                UPDATE dispatch_metadata
                SET outcome_status = ?, outcome_report_path = ?, completed_at = ?
                WHERE dispatch_id = ? AND outcome_status IS NULL
            """, (status, report_path or None, timestamp or None, dispatch_id))
            if cur.rowcount > 0:
                linked += 1

    conn.commit()
    return linked


def link_reports_to_dispatches(conn: sqlite3.Connection) -> int:
    """Extract dispatch_id from report files and update report_findings."""
    cur = conn.cursor()
    cur.execute("SELECT id, report_path FROM report_findings WHERE dispatch_id IS NULL")
    rows = cur.fetchall()

    updated = 0
    for row_id, report_path in rows:
        if not report_path:
            continue
        rp = Path(report_path)
        if not rp.exists():
            continue

        try:
            content = rp.read_text(encoding="utf-8", errors="replace")[:5000]
        except OSError:
            continue

        m = DISPATCH_ID_RE.search(content)
        if not m:
            m = DISPATCH_HEADER_RE.search(content)
        if not m:
            continue

        dispatch_id = m.group(1).strip()
        cur.execute(
            "UPDATE report_findings SET dispatch_id = ? WHERE id = ?",
            (dispatch_id, row_id)
        )
        updated += 1

    conn.commit()
    return updated


def main():
    if not DB_PATH.exists():
        print(f"ERROR: DB not found: {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)

    # Ensure report_findings exists even if Phase 0 (quality_db_init.py) failed.
    created = ensure_report_findings_table(conn)
    if created:
        print("  Migrated: created report_findings table (was missing)")

    print("=== Nightly Session-Dispatch Linkage ===")

    linked_sessions = link_sessions_to_dispatches(conn)
    print(f"  Sessions linked to dispatches: {linked_sessions}")

    linked_receipts = link_receipts_to_dispatches(conn)
    print(f"  Receipt outcomes linked: {linked_receipts}")

    linked_reports = link_reports_to_dispatches(conn)
    print(f"  Report findings linked: {linked_reports}")

    conn.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
