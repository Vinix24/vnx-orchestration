#!/usr/bin/env python3
"""Retroactive backfill: link existing session_analytics rows to dispatch_ids.

Scans JSONL session files for Dispatch-ID markers in early user messages,
then updates session_analytics.dispatch_id. Also imports completed dispatch
.md files into dispatch_metadata.
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

PATHS = ensure_env()
VNX_HOME = Path(PATHS["VNX_HOME"])
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"
DISPATCH_DIR = Path(PATHS.get("VNX_DISPATCH_DIR", VNX_HOME / "dispatch"))
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

DISPATCH_TABLE_RE = re.compile(r'\|\s*\*\*Dispatch-ID\*\*\s*\|\s*(\d{8}-\d{6}-[0-9a-f]{8}-[A-C]|[\w-]{10,})\s*\|')
DISPATCH_HEADER_RE = re.compile(r'Dispatch-ID:\s*(\d{8}-\d{6}-[0-9a-f]{8}-[A-C]|[\w-]{10,})')

# Dispatch .md file metadata extractors
TRACK_RE = re.compile(r'\[\[TARGET:([A-C])\]\]')
ROLE_RE = re.compile(r'^Role:\s*(.+)', re.MULTILINE | re.IGNORECASE)
GATE_RE = re.compile(r'^Gate:\s*(.+)', re.MULTILINE | re.IGNORECASE)
COGNITION_RE = re.compile(r'^Cognition:\s*(.+)', re.MULTILINE | re.IGNORECASE)
PRIORITY_RE = re.compile(r'^Priority:\s*(.+)', re.MULTILINE | re.IGNORECASE)
PR_ID_RE = re.compile(r'^PR-ID:\s*(.+)', re.MULTILINE | re.IGNORECASE)
INTEL_SECTION_RE = re.compile(
    r'\[INTELLIGENCE_DATA\]\s*\n(.*?)\n\[/INTELLIGENCE_DATA\]',
    re.DOTALL
)

TERMINAL_PATTERNS = {
    "T-MANAGER": re.compile(r"T-MANAGER", re.IGNORECASE),
    "T0": re.compile(r"(?:^|-)T0(?:$|-)", re.IGNORECASE),
    "T1": re.compile(r"(?:^|-)T1(?:$|-)", re.IGNORECASE),
    "T2": re.compile(r"(?:^|-)T2(?:$|-)", re.IGNORECASE),
    "T3": re.compile(r"(?:^|-)T3(?:$|-)", re.IGNORECASE),
}

TRACK_TO_TERMINAL = {"A": "T1", "B": "T2", "C": "T3"}


def detect_terminal(dir_name: str) -> str:
    for terminal, pattern in TERMINAL_PATTERNS.items():
        if pattern.search(dir_name):
            return terminal
    return "unknown"


def extract_dispatch_id_from_jsonl(jsonl_path: Path) -> Optional[str]:
    """Scan first few user messages for dispatch_id."""
    user_count = 0
    with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "user":
                continue
            user_count += 1
            if user_count > 5:
                break
            content = record.get("message", {}).get("content", "")
            text = content if isinstance(content, str) else " ".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            ) if isinstance(content, list) else ""
            m = DISPATCH_TABLE_RE.search(text)
            if not m:
                m = DISPATCH_HEADER_RE.search(text)
            if m:
                return m.group(1).strip()
    return None


def backfill_session_dispatch_ids(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """Update session_analytics rows with dispatch_id from JSONL files."""
    cur = conn.cursor()
    cur.execute("SELECT session_id, project_path FROM session_analytics WHERE dispatch_id IS NULL")
    rows = cur.fetchall()

    updated = 0
    for session_id, project_path in rows:
        # Find the JSONL file
        jsonl_path = None
        for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.exists():
                jsonl_path = candidate
                break

        if not jsonl_path:
            continue

        dispatch_id = extract_dispatch_id_from_jsonl(jsonl_path)
        if not dispatch_id:
            continue

        if dry_run:
            print(f"  [DRY RUN] {session_id[:12]}... -> {dispatch_id}")
        else:
            cur.execute(
                "UPDATE session_analytics SET dispatch_id = ? WHERE session_id = ?",
                (dispatch_id, session_id)
            )
        updated += 1

    if not dry_run:
        conn.commit()
    return updated


def import_completed_dispatches(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """Import .md dispatch files from completed/ into dispatch_metadata."""
    completed_dir = DISPATCH_DIR / "completed"
    if not completed_dir.exists():
        print(f"No completed directory: {completed_dir}")
        return 0

    cur = conn.cursor()
    imported = 0

    for md_file in completed_dir.glob("*.md"):
        dispatch_id = md_file.stem
        # Skip if already imported
        cur.execute("SELECT 1 FROM dispatch_metadata WHERE dispatch_id = ?", (dispatch_id,))
        if cur.fetchone():
            continue

        content = md_file.read_text(encoding="utf-8", errors="replace")

        track_m = TRACK_RE.search(content)
        track = track_m.group(1) if track_m else "A"
        terminal = TRACK_TO_TERMINAL.get(track, "T1")

        role_m = ROLE_RE.search(content)
        role = role_m.group(1).strip().split()[0] if role_m else None

        gate_m = GATE_RE.search(content)
        gate = gate_m.group(1).strip() if gate_m else None

        cognition_m = COGNITION_RE.search(content)
        cognition = cognition_m.group(1).strip().lower() if cognition_m else "normal"

        priority_m = PRIORITY_RE.search(content)
        priority = priority_m.group(1).strip().split(";")[0].strip() if priority_m else "P1"

        pr_id_m = PR_ID_RE.search(content)
        pr_id = pr_id_m.group(1).strip() if pr_id_m else None

        intel_m = INTEL_SECTION_RE.search(content)
        intel_json = intel_m.group(1).strip() if intel_m else None

        pattern_count = 0
        rule_count = 0
        if intel_json:
            try:
                intel_data = json.loads(intel_json)
                pattern_count = intel_data.get("pattern_count", 0)
                rule_count = intel_data.get("prevention_rule_count", 0)
            except (json.JSONDecodeError, AttributeError):
                pass

        instruction_chars = len(content)

        # Use file mtime as dispatched_at
        mtime = datetime.fromtimestamp(md_file.stat().st_mtime)

        if dry_run:
            print(f"  [DRY RUN] Import: {dispatch_id} track={track} role={role}")
        else:
            cur.execute("""
                INSERT OR IGNORE INTO dispatch_metadata (
                    dispatch_id, terminal, track, role, skill_name, gate,
                    cognition, priority, pr_id,
                    pattern_count, prevention_rule_count, intelligence_json,
                    instruction_char_count, dispatched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                dispatch_id, terminal, track, role, role, gate,
                cognition, priority, pr_id,
                pattern_count, rule_count, intel_json,
                instruction_chars, mtime.isoformat(),
            ))
        imported += 1

    if not dry_run:
        conn.commit()
    return imported


def link_receipts_to_dispatches(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """Cross-reference receipts file to update dispatch outcome_status."""
    receipts_file = STATE_DIR / "t0_receipts.ndjson"
    if not receipts_file.exists():
        return 0

    cur = conn.cursor()
    linked = 0

    with open(receipts_file, "r", encoding="utf-8", errors="replace") as f:
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

            if dry_run:
                print(f"  [DRY RUN] Link receipt: {dispatch_id} -> {status}")
            else:
                cur.execute("""
                    UPDATE dispatch_metadata
                    SET outcome_status = ?, outcome_report_path = ?, completed_at = ?
                    WHERE dispatch_id = ? AND outcome_status IS NULL
                """, (status, report_path or None, timestamp or None, dispatch_id))
                if cur.rowcount > 0:
                    linked += 1

    if not dry_run:
        conn.commit()
    return linked


def main():
    parser = argparse.ArgumentParser(description="Backfill dispatch_ids in session_analytics")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: DB not found: {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=== Dispatch Analytics Backfill ===\n")

    # Phase 1: Backfill session_analytics.dispatch_id from JSONL
    print("Phase 1: Scanning JSONL sessions for dispatch_id...")
    updated = backfill_session_dispatch_ids(conn, args.dry_run)
    print(f"  Sessions updated: {updated}\n")

    # Phase 2: Import completed dispatch .md files
    print("Phase 2: Importing completed dispatch files...")
    imported = import_completed_dispatches(conn, args.dry_run)
    print(f"  Dispatches imported: {imported}\n")

    # Phase 3: Link receipts to dispatch outcomes
    print("Phase 3: Linking receipts to dispatch outcomes...")
    linked = link_receipts_to_dispatches(conn, args.dry_run)
    print(f"  Receipts linked: {linked}\n")

    # Summary
    if not args.dry_run:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM session_analytics WHERE dispatch_id IS NOT NULL")
        sessions_with_id = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM dispatch_metadata")
        total_dispatches = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM dispatch_metadata WHERE outcome_status IS NOT NULL")
        with_outcome = cur.fetchone()[0]
        print(f"Summary:")
        print(f"  Sessions with dispatch_id: {sessions_with_id}")
        print(f"  Dispatch metadata records: {total_dispatches}")
        print(f"  Dispatches with outcome:   {with_outcome}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
