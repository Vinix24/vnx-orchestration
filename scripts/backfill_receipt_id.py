#!/usr/bin/env python3
"""Backfill receipt_id in provenance_registry from t0_receipts.ndjson.

Seam 3 (provenance backfill, OI-832): historical provenance_registry rows with
receipt_id IS NULL are never backfilled. The append-time enrichment path
(enrichment.py:_register_provenance_link) writes receipt_id for new receipts via
resolve_receipt_id(), but the git-scan reconcile path
(reconcile_commit_provenance) never supplies one — and no backfill script ever
closes the gap retroactively.

This script scans the receipts NDJSON ledger, resolves a stable receipt_id for
every dispatch via resolve_receipt_id() (real run_id/task_id when present,
deterministic synthetic:<dispatch_id>:<event_type> fallback otherwise), and
updates any provenance_registry row that still has receipt_id IS NULL.

Idempotent: running it twice is a no-op (only NULL receipt_id rows are matched,
and the destination value is derived deterministically from stable receipt
fields so a second run sees no NULL rows left to update).

Usage:
    python3 scripts/backfill_receipt_id.py                 # dry-run (default)
    python3 scripts/backfill_receipt_id.py --apply         # execute
    python3 scripts/backfill_receipt_id.py --project-id mission-control
    python3 scripts/backfill_receipt_id.py --json          # structured output

BILLING SAFETY: No Anthropic SDK. No LLM calls.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from project_root import (  # noqa: E402
    resolve_central_data_dir,
    resolve_project_id,
    resolve_project_root,
)
from receipt_provenance import (  # noqa: E402
    _calculate_chain_status,
    resolve_receipt_id,
)

EXIT_OK = 0
EXIT_ERROR = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _null_receipt_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Return provenance_registry rows with receipt_id IS NULL."""
    cur = conn.execute(
        "SELECT dispatch_id, receipt_id, commit_sha, chain_status "
        "FROM provenance_registry WHERE receipt_id IS NULL"
    )
    rows = cur.fetchall()
    return [
        {
            "dispatch_id": r[0],
            "receipt_id": r[1],
            "commit_sha": r[2],
            "chain_status": r[3],
        }
        for r in rows
    ]


def _load_dispatch_receipt_map(receipts_file: Path) -> Dict[str, str]:
    """Scan receipts NDJSON and return {dispatch_id: receipt_id}.

    resolve_receipt_id() is applied to every receipt so real run_id/task_id
    always takes priority over the synthetic fallback. When multiple receipts
    share a dispatch_id the LAST real id wins over the first synthetic one.
    """
    if not receipts_file.exists():
        return {}

    result: Dict[str, str] = {}
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

            dispatch_id = str(rec.get("dispatch_id") or "").strip()
            if not dispatch_id or dispatch_id.lower() == "unknown":
                continue

            receipt_id = resolve_receipt_id(rec)
            if not receipt_id:
                continue

            existing = result.get(dispatch_id)
            if existing is None:
                result[dispatch_id] = receipt_id
            else:
                # Prefer a real id over a synthetic one, but keep the first
                # real id that was seen (first writer wins for stability).
                existing_is_synthetic = existing.startswith("synthetic:")
                new_is_synthetic = receipt_id.startswith("synthetic:")
                if existing_is_synthetic and not new_is_synthetic:
                    result[dispatch_id] = receipt_id

    return result


def _recalculate_and_update_chain_status(
    conn: sqlite3.Connection, dispatch_id: str
) -> None:
    """Recalculate chain_status for *dispatch_id* after receipt_id was filled."""
    row = conn.execute(
        "SELECT receipt_id, commit_sha, pr_number, feature_plan_pr "
        "FROM provenance_registry WHERE dispatch_id = ?",
        (dispatch_id,),
    ).fetchone()
    if not row:
        return
    fields = {
        "receipt_id": row[0],
        "commit_sha": row[1],
        "pr_number": row[2],
        "feature_plan_pr": row[3],
    }
    new_status = _calculate_chain_status(fields)
    conn.execute(
        "UPDATE provenance_registry SET chain_status = ? WHERE dispatch_id = ?",
        (new_status, dispatch_id),
    )


# ---------------------------------------------------------------------------
# Core backfill
# ---------------------------------------------------------------------------


def analyse(
    conn: sqlite3.Connection,
    receipts_map: Dict[str, str],
) -> Dict[str, Any]:
    """Analyse what the backfill would do without touching the database.

    Returns a summary dict with:
      - null_rows_total: int
      - matchable: int (rows that have a receipt in the NDJSON)
      - unmatched: int (rows with no matching receipt)
      - rows: list of {'dispatch_id', 'current_receipt_id', 'new_receipt_id',
                        'current_chain_status', 'action'}
    """
    null_rows = _null_receipt_rows(conn)
    rows = []
    matchable = 0
    unmatched = 0

    for row in null_rows:
        dispatch_id = row["dispatch_id"]
        if dispatch_id in receipts_map:
            action = "UPDATE"
            matchable += 1
        else:
            action = "NO_MATCH"
            unmatched += 1
        rows.append(
            {
                "dispatch_id": dispatch_id,
                "current_receipt_id": row["receipt_id"],
                "new_receipt_id": receipts_map.get(dispatch_id),
                "current_chain_status": row["chain_status"],
                "action": action,
            }
        )

    return {
        "null_rows_total": len(null_rows),
        "matchable": matchable,
        "unmatched": unmatched,
        "rows": rows,
    }


def apply_backfill(
    conn: sqlite3.Connection,
    receipts_map: Dict[str, str],
) -> Dict[str, int]:
    """Backfill receipt_id for provenance_registry rows where it is NULL.

    Only updates rows whose dispatch_id has a matching receipt in the map.
    Chain status is recalculated after the update so rows that now have both
    receipt_id and commit_sha reach ``complete``.

    Returns {'updated': n, 'skipped': n, 'no_match': n}.
    """
    null_rows = _null_receipt_rows(conn)
    counts = {"updated": 0, "skipped": 0, "no_match": 0}

    for row in null_rows:
        dispatch_id = row["dispatch_id"]
        new_receipt_id = receipts_map.get(dispatch_id)
        if new_receipt_id is None:
            counts["no_match"] += 1
            continue

        cur = conn.execute(
            "UPDATE provenance_registry SET receipt_id = ? "
            "WHERE dispatch_id = ? AND receipt_id IS NULL",
            (new_receipt_id, dispatch_id),
        )
        if cur.rowcount > 0:
            _recalculate_and_update_chain_status(conn, dispatch_id)
            counts["updated"] += 1
        else:
            counts["skipped"] += 1

    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_dry_run(analysis: Dict[str, Any], project_id: str, receipts_scanned: int) -> None:
    print(f"=== backfill_receipt_id DRY-RUN (project_id={project_id!r}) ===")
    print(f"  receipts scanned (unique dispatch_ids): {receipts_scanned}")
    print(f"  provenance_registry NULL receipt_id   : {analysis['null_rows_total']}")
    print(f"  matchable (receipt exists in NDJSON)  : {analysis['matchable']}")
    print(f"  unmatched (no receipt for dispatch)   : {analysis['unmatched']}")
    print()
    action_counts: Dict[str, int] = {}
    for r in analysis["rows"]:
        action_counts[r["action"]] = action_counts.get(r["action"], 0) + 1
    for action, count in sorted(action_counts.items()):
        print(f"  {action:<15}: {count}")
    print()
    print("Pass --apply to execute.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", default=False,
        help="Execute the backfill (default: dry-run only).",
    )
    parser.add_argument(
        "--receipts-file", default=None,
        help="Path to t0_receipts.ndjson (default: central state dir).",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Path to runtime_coordination.db (default: central state dir).",
    )
    parser.add_argument(
        "--project-id", default=None,
        help="Override project_id (default: resolved for this repo).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a JSON result line.",
    )
    args = parser.parse_args(argv)

    repo_root = resolve_project_root(__file__)
    try:
        project_id = args.project_id or resolve_project_id(repo_root)
    except RuntimeError as exc:
        print(f"backfill_receipt_id: cannot resolve project_id: {exc}", file=sys.stderr)
        return EXIT_ERROR

    # Resolve paths.
    if args.db_path:
        db_path = Path(args.db_path)
    else:
        db_path = resolve_central_data_dir(project_id) / "state" / "runtime_coordination.db"

    if args.receipts_file:
        receipts_file = Path(args.receipts_file)
    else:
        receipts_file = resolve_central_data_dir(project_id) / "state" / "t0_receipts.ndjson"

    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return EXIT_ERROR

    if not receipts_file.exists():
        print(f"ERROR: receipts file not found: {receipts_file}", file=sys.stderr)
        return EXIT_ERROR

    conn = sqlite3.connect(str(db_path))
    try:
        if not _table_exists(conn, "provenance_registry"):
            result = {
                "updated": 0, "skipped": 0, "no_match": 0,
                "degraded": "provenance_registry table missing",
            }
            print(json.dumps(result) if args.json else
                  "backfill_receipt_id: provenance_registry table missing — nothing to do")
            return EXIT_OK

        receipts_map = _load_dispatch_receipt_map(receipts_file)
        analysis = analyse(conn, receipts_map)

        if not args.apply:
            _print_dry_run(analysis, project_id, len(receipts_map))
            return EXIT_OK

        counts = apply_backfill(conn, receipts_map)
        conn.commit()

        result = {
            **counts,
            "project_id": project_id,
            "receipts_scanned": len(receipts_map),
            "null_rows_total": analysis["null_rows_total"],
        }
        if args.json:
            print(json.dumps(result))
        else:
            print(
                f"backfill_receipt_id[{project_id}]: updated {counts['updated']}, "
                f"skipped {counts['skipped']}, no_match {counts['no_match']}"
            )
        return EXIT_OK
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
