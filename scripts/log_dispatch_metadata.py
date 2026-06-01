#!/usr/bin/env python3
"""Log dispatch metadata to quality_intelligence.db after successful dispatch.

Called non-fatally from dispatcher_minimal.sh after each successful dispatch.
Inserts a row into dispatch_metadata with all available context.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
    from project_scope import current_project_id
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        if row[1] == column:
            return True
    return False

PATHS = ensure_env()
DB_PATH = Path(PATHS["VNX_STATE_DIR"]) / "quality_intelligence.db"


def main():
    parser = argparse.ArgumentParser(description="Log dispatch metadata to DB")
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--terminal", required=True)
    parser.add_argument("--track", required=True)
    parser.add_argument("--role", default="")
    parser.add_argument("--skill-name", default="")
    parser.add_argument("--gate", default="")
    parser.add_argument("--provider", default="",
                        help="Provider that executed this dispatch (claude/codex/gemini/kimi/litellm:*). "
                             "Leave empty to preserve any existing provider value.")
    parser.add_argument("--model", default="",
                        help="AI model used (e.g. claude-sonnet-4-6, codex, kimi)")
    parser.add_argument("--cognition", default="normal")
    parser.add_argument("--priority", default="P1")
    parser.add_argument("--pr-id", default="")
    parser.add_argument("--pattern-count", type=int, default=0)
    parser.add_argument("--prevention-rule-count", type=int, default=0)
    parser.add_argument("--intelligence-json", default="")
    parser.add_argument("--instruction-char-count", type=int, default=0)
    parser.add_argument("--context-file-count", type=int, default=0)
    parser.add_argument("--target-open-items", default="", help="JSON array or comma-separated OI-IDs targeted by this dispatch")
    args = parser.parse_args()

    # Normalize target_open_items to JSON array string
    raw_toi = args.target_open_items.strip()
    if raw_toi and not raw_toi.startswith("["):
        # comma-separated OI-IDs → JSON array
        raw_toi = json.dumps([x.strip() for x in raw_toi.split(",") if x.strip()])
    args.target_open_items = raw_toi or None

    if not DB_PATH.exists():
        print(f"WARNING: DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # INSERT OR IGNORE: only inserts when the row is new so existing provider/model
    # values are never deleted by a REPLACE. dispatched_at is set on first insert only.
    # A follow-up UPDATE keeps all other dispatch fields current on re-calls.
    project_id_val = current_project_id()
    now_iso = datetime.utcnow().isoformat()
    _dispatch_vals = (
        args.role or None,
        args.skill_name or None,
        args.gate or None,
        args.cognition,
        args.priority,
        args.pr_id or None,
        args.pattern_count,
        args.prevention_rule_count,
        args.intelligence_json or None,
        args.instruction_char_count,
        args.context_file_count,
        args.target_open_items,
    )

    if _has_column(conn, "dispatch_metadata", "project_id"):
        cur.execute("""
            INSERT OR IGNORE INTO dispatch_metadata (
                dispatch_id, terminal, track, role, skill_name, gate,
                cognition, priority, pr_id,
                pattern_count, prevention_rule_count, intelligence_json,
                instruction_char_count, context_file_count, target_open_items,
                dispatched_at, project_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            args.dispatch_id, args.terminal, args.track,
            *_dispatch_vals, now_iso, project_id_val,
        ))
        cur.execute("""
            UPDATE dispatch_metadata SET
                terminal=?, track=?, role=?, skill_name=?, gate=?,
                cognition=?, priority=?, pr_id=?,
                pattern_count=?, prevention_rule_count=?, intelligence_json=?,
                instruction_char_count=?, context_file_count=?, target_open_items=?
            WHERE project_id=? AND dispatch_id=?
        """, (
            args.terminal, args.track, *_dispatch_vals,
            project_id_val, args.dispatch_id,
        ))
    else:
        cur.execute("""
            INSERT OR IGNORE INTO dispatch_metadata (
                dispatch_id, terminal, track, role, skill_name, gate,
                cognition, priority, pr_id,
                pattern_count, prevention_rule_count, intelligence_json,
                instruction_char_count, context_file_count, target_open_items, dispatched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            args.dispatch_id, args.terminal, args.track,
            *_dispatch_vals, now_iso,
        ))
        cur.execute("""
            UPDATE dispatch_metadata SET
                terminal=?, track=?, role=?, skill_name=?, gate=?,
                cognition=?, priority=?, pr_id=?,
                pattern_count=?, prevention_rule_count=?, intelligence_json=?,
                instruction_char_count=?, context_file_count=?, target_open_items=?
            WHERE dispatch_id=?
        """, (
            args.terminal, args.track, *_dispatch_vals,
            args.dispatch_id,
        ))
    # Stamp provider + model (provider/model-aware self-learning). Guarded: columns
    # land via migration v21/v23 (GAP 2); older DBs simply skip them. COALESCE keeps
    # any real value already stamped by provider_dispatch; only fills NULL.
    # ADR-007: scope by project_id when present to prevent cross-tenant overwrite.
    has_project = _has_column(conn, "dispatch_metadata", "project_id")
    if args.provider and _has_column(conn, "dispatch_metadata", "provider"):
        if has_project:
            cur.execute(
                "UPDATE dispatch_metadata SET provider = COALESCE(provider, ?) WHERE project_id = ? AND dispatch_id = ?",
                (args.provider, project_id_val, args.dispatch_id),
            )
        else:
            cur.execute(
                "UPDATE dispatch_metadata SET provider = COALESCE(provider, ?) WHERE dispatch_id = ?",
                (args.provider, args.dispatch_id),
            )
    if args.model and _has_column(conn, "dispatch_metadata", "model"):
        if has_project:
            cur.execute(
                "UPDATE dispatch_metadata SET model = COALESCE(model, ?) WHERE project_id = ? AND dispatch_id = ?",
                (args.model, project_id_val, args.dispatch_id),
            )
        else:
            cur.execute(
                "UPDATE dispatch_metadata SET model = COALESCE(model, ?) WHERE dispatch_id = ?",
                (args.model, args.dispatch_id),
            )

    conn.commit()
    conn.close()
    print(f"Logged dispatch metadata: {args.dispatch_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
