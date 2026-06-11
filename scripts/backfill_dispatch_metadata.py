#!/usr/bin/env python3
"""Backfill dispatch_metadata rows from t0_receipts.ndjson.

Context: dispatch_metadata has ~23 rows while t0_receipts.ndjson has 6,400+.
568 receipts carry a real dispatch_id but have no corresponding
dispatch_metadata row. This tool reads the NDJSON ledger, selects
completion-class receipts (task_complete / task_failed / task_timeout)
with a non-'unknown' dispatch_id, and INSERT-OR-IGNOREs missing
dispatch_metadata rows.

Safety rules (enforced unconditionally):
  - --dry-run is the DEFAULT. Pass --apply to mutate the database.
  - Existing rows are NEVER updated unless outcome_status IS NULL
    (same rule as link_sessions_dispatches.link_receipts_to_dispatches).
  - Every INSERT stamps project_id via project_scope.current_project_id()
    (ADR-007: UNIQUE (project_id, dispatch_id) composite key).
  - --backup creates a timestamped .backup_<ts> copy before any write.
  - A second run is idempotent: INSERT OR IGNORE + outcome_status IS NULL
    guard means zero mutations on a database that already has the rows.

Status normalization follows the canonical vocabulary from #837
(check_active_drain.py + weekly_digest.py):
  SUCCESS_STATUSES = {"success","completed","complete","ok","","done"}
  FAILURE_STATUSES = {"failed","failure","error","blocked","timeout","contract_invalid"}
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Path bootstrap — scripts/lib must be importable without a live VNX env.
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

try:
    from vnx_paths import ensure_env
    from project_scope import current_project_id
except Exception as exc:
    raise SystemExit(f"backfill_dispatch_metadata: failed to load vnx_paths / project_scope: {exc}")

# ---------------------------------------------------------------------------
# Canonical status vocabulary (#837 — keep in sync with check_active_drain,
# weekly_digest, receipt_classifier, payload).
# ---------------------------------------------------------------------------

_SUCCESS_STATUSES = frozenset({"success", "completed", "complete", "ok", "", "done"})
_FAILURE_STATUSES = frozenset({
    "failed", "failure", "error", "blocked", "timeout", "contract_invalid",
})

# Completion event types that carry an outcome signal.
_COMPLETION_EVENTS = frozenset({"task_complete", "task_completed", "task_failed", "task_timeout"})


def _normalize_status(raw: Optional[str]) -> str:
    """Map a raw receipt status to the canonical three-value vocabulary.

    Returns "success", "failure", or "unknown".
    The caller may store NULL for "unknown" if preferred — that is
    intentionally kept consistent with link_receipts_to_dispatches.
    """
    if raw is None:
        return "unknown"
    s = str(raw).strip().lower()
    if s in _SUCCESS_STATUSES:
        return "success"
    if s in _FAILURE_STATUSES:
        return "failure"
    # Substring fallback for non-canonical variants ("task_failed_hard" etc.)
    if "fail" in s or "error" in s:
        return "failure"
    return "unknown"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(row[1] == column for row in rows)


def _has_project_id_column(conn: sqlite3.Connection) -> bool:
    return _has_column(conn, "dispatch_metadata", "project_id")


def _load_completion_receipts(receipts_file: Path) -> list[Dict[str, Any]]:
    """Read t0_receipts.ndjson and return only completion-class records.

    Filters:
      - event_type in _COMPLETION_EVENTS
      - dispatch_id present and not 'unknown'
    """
    if not receipts_file.exists():
        return []

    results: list[Dict[str, Any]] = []
    with open(receipts_file, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue

            event_type = str(rec.get("event_type") or rec.get("event") or "").lower().strip()
            if event_type not in _COMPLETION_EVENTS:
                continue

            dispatch_id = str(rec.get("dispatch_id") or "").strip()
            if not dispatch_id or dispatch_id.lower() == "unknown":
                continue

            results.append(rec)

    return results


def _existing_dispatch_ids(
    conn: sqlite3.Connection,
    project_id: str,
    has_project_col: bool,
) -> frozenset[str]:
    """Return the set of dispatch_ids already present in dispatch_metadata."""
    if has_project_col:
        rows = conn.execute(
            "SELECT dispatch_id FROM dispatch_metadata WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT dispatch_id FROM dispatch_metadata").fetchall()
    return frozenset(r[0] for r in rows if r[0])


def _dispatches_needing_outcome_update(
    conn: sqlite3.Connection,
    project_id: str,
    has_project_col: bool,
) -> frozenset[str]:
    """Return dispatch_ids with outcome_status IS NULL in dispatch_metadata."""
    if not _has_column(conn, "dispatch_metadata", "outcome_status"):
        return frozenset()
    if has_project_col:
        rows = conn.execute(
            "SELECT dispatch_id FROM dispatch_metadata "
            "WHERE project_id = ? AND outcome_status IS NULL",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT dispatch_id FROM dispatch_metadata WHERE outcome_status IS NULL"
        ).fetchall()
    return frozenset(r[0] for r in rows if r[0])


def _best_receipt_per_dispatch(
    receipts: list[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Collapse multiple completion receipts per dispatch_id to the 'best' one.

    Selection rule (mirrors link_receipts_to_dispatches fail-closed semantics):
      - failure beats success beats unknown (fail-closed).
      - Within the same outcome class, pick the latest timestamp.
    """
    _RANK = {"failure": 0, "unknown": 1, "success": 2}

    best: Dict[str, Dict[str, Any]] = {}
    for rec in receipts:
        did = str(rec.get("dispatch_id") or "").strip()
        outcome = _normalize_status(rec.get("status"))
        ts = str(rec.get("timestamp") or "")

        if did not in best:
            best[did] = rec
        else:
            prev = best[did]
            prev_outcome = _normalize_status(prev.get("status"))
            # Lower rank = worse = wins (fail-closed).
            if _RANK[outcome] < _RANK[prev_outcome]:
                best[did] = rec
            elif _RANK[outcome] == _RANK[prev_outcome]:
                # Same outcome class — pick later timestamp.
                if ts > str(prev.get("timestamp") or ""):
                    best[did] = rec

    return best


def analyse(
    conn: sqlite3.Connection,
    receipts: list[Dict[str, Any]],
    project_id: str,
) -> Dict[str, Any]:
    """Analyse what the backfill would do without touching the database.

    Returns a summary dict with the following keys:
      - total_completion_receipts: int
      - dispatches_in_receipts: int  (unique dispatch_ids)
      - dispatches_already_in_db: int
      - dispatches_to_insert: int    (new rows to INSERT)
      - dispatches_to_update_outcome: int  (existing rows, outcome_status IS NULL)
      - rows: list of {'dispatch_id', 'action', 'status', 'timestamp'}
    """
    has_project_col = _has_project_id_column(conn)
    existing = _existing_dispatch_ids(conn, project_id, has_project_col)
    null_outcome = _dispatches_needing_outcome_update(conn, project_id, has_project_col)
    best = _best_receipt_per_dispatch(receipts)

    rows = []
    to_insert: list[str] = []
    to_update: list[str] = []

    for did, rec in best.items():
        status = _normalize_status(rec.get("status"))
        ts = str(rec.get("timestamp") or "")
        if did not in existing:
            action = "INSERT"
            to_insert.append(did)
        elif did in null_outcome:
            action = "UPDATE_OUTCOME"
            to_update.append(did)
        else:
            action = "SKIP"
        rows.append({"dispatch_id": did, "action": action, "status": status, "timestamp": ts})

    return {
        "total_completion_receipts": len(receipts),
        "dispatches_in_receipts": len(best),
        "dispatches_already_in_db": len([r for r in rows if r["action"] != "INSERT"]),
        "dispatches_to_insert": len(to_insert),
        "dispatches_to_update_outcome": len(to_update),
        "rows": rows,
    }


def apply_backfill(
    conn: sqlite3.Connection,
    receipts: list[Dict[str, Any]],
    project_id: str,
) -> Dict[str, int]:
    """Apply the backfill to the open connection.

    Inserts missing dispatch_metadata rows and updates outcome_status where NULL.
    Returns {'inserted': n, 'updated': n, 'skipped': n}.
    """
    has_project_col = _has_project_id_column(conn)
    existing = _existing_dispatch_ids(conn, project_id, has_project_col)
    null_outcome = _dispatches_needing_outcome_update(conn, project_id, has_project_col)
    best = _best_receipt_per_dispatch(receipts)

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    counts = {"inserted": 0, "updated": 0, "skipped": 0}

    for did, rec in best.items():
        raw_status = rec.get("status")
        outcome = _normalize_status(raw_status)
        report_path = rec.get("report_path") or None
        timestamp = rec.get("timestamp") or None
        terminal = str(rec.get("terminal") or "unknown")
        track = str(rec.get("track") or "unknown")

        if did not in existing:
            # INSERT new row — use INSERT OR IGNORE for idempotency under
            # concurrent execution (composite UNIQUE (project_id, dispatch_id)).
            if has_project_col:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO dispatch_metadata (
                        dispatch_id, project_id, terminal, track,
                        outcome_status, outcome_report_path,
                        dispatched_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        did,
                        project_id,
                        terminal,
                        track,
                        outcome if outcome != "unknown" else None,
                        report_path,
                        now_iso,
                        timestamp,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO dispatch_metadata (
                        dispatch_id, terminal, track,
                        outcome_status, outcome_report_path,
                        dispatched_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        did,
                        terminal,
                        track,
                        outcome if outcome != "unknown" else None,
                        report_path,
                        now_iso,
                        timestamp,
                    ),
                )
            counts["inserted"] += 1

        elif did in null_outcome:
            # Update outcome_status only where it was NULL — never overwrite
            # an existing value (same contract as link_receipts_to_dispatches).
            if has_project_col:
                conn.execute(
                    """
                    UPDATE dispatch_metadata
                    SET outcome_status = ?, outcome_report_path = ?, completed_at = ?
                    WHERE project_id = ? AND dispatch_id = ? AND outcome_status IS NULL
                    """,
                    (
                        outcome if outcome != "unknown" else None,
                        report_path,
                        timestamp,
                        project_id,
                        did,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE dispatch_metadata
                    SET outcome_status = ?, outcome_report_path = ?, completed_at = ?
                    WHERE dispatch_id = ? AND outcome_status IS NULL
                    """,
                    (
                        outcome if outcome != "unknown" else None,
                        report_path,
                        timestamp,
                        did,
                    ),
                )
            counts["updated"] += 1

        else:
            counts["skipped"] += 1

    conn.commit()
    return counts


def _print_dry_run_report(summary: Dict[str, Any], project_id: str) -> None:
    print(f"=== backfill_dispatch_metadata DRY-RUN (project_id={project_id!r}) ===")
    print(f"  completion receipts read    : {summary['total_completion_receipts']}")
    print(f"  unique dispatch_ids in file : {summary['dispatches_in_receipts']}")
    print(f"  already in dispatch_metadata: {summary['dispatches_already_in_db']}")
    print(f"  would INSERT (new rows)     : {summary['dispatches_to_insert']}")
    print(f"  would UPDATE (null outcome) : {summary['dispatches_to_update_outcome']}")
    print()
    action_counts: Dict[str, int] = {}
    for row in summary["rows"]:
        action_counts[row["action"]] = action_counts.get(row["action"], 0) + 1
    for action, count in sorted(action_counts.items()):
        print(f"  {action:<25}: {count}")
    print()
    print("Pass --apply to execute. Pass --backup to snapshot the DB first.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill dispatch_metadata from t0_receipts.ndjson. "
            "Dry-run by default — pass --apply to mutate the database."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute the backfill (default: dry-run only).",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        default=False,
        help="Create a timestamped DB backup before applying changes.",
    )
    parser.add_argument(
        "--receipts-file",
        default=None,
        help="Path to t0_receipts.ndjson (default: $VNX_STATE_DIR/t0_receipts.ndjson).",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to quality_intelligence.db (default: $VNX_STATE_DIR/quality_intelligence.db).",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Override project_id (default: from VNX_PROJECT_ID env or 'vnx-dev').",
    )
    args = parser.parse_args(argv)

    # Resolve paths.
    paths = ensure_env()
    state_dir = Path(paths["VNX_STATE_DIR"])

    receipts_file = Path(args.receipts_file) if args.receipts_file else state_dir / "t0_receipts.ndjson"
    db_path = Path(args.db_path) if args.db_path else state_dir / "quality_intelligence.db"
    project_id = args.project_id or current_project_id()

    if not receipts_file.exists():
        print(f"ERROR: receipts file not found: {receipts_file}", file=sys.stderr)
        return 1
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 1

    print(f"receipts file : {receipts_file}")
    print(f"database      : {db_path}")
    print(f"project_id    : {project_id}")
    print()

    receipts = _load_completion_receipts(receipts_file)
    conn = sqlite3.connect(str(db_path))
    summary = analyse(conn, receipts, project_id)

    if not args.apply:
        _print_dry_run_report(summary, project_id)
        conn.close()
        return 0

    # Apply path.
    if args.backup:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = db_path.with_name(f"{db_path.name}.backup_{ts}")
        shutil.copy2(db_path, backup_path)
        print(f"Backup written to: {backup_path}")

    counts = apply_backfill(conn, receipts, project_id)
    conn.close()

    print("=== backfill_dispatch_metadata APPLIED ===")
    print(f"  project_id : {project_id}")
    print(f"  inserted   : {counts['inserted']}")
    print(f"  updated    : {counts['updated']}")
    print(f"  skipped    : {counts['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
