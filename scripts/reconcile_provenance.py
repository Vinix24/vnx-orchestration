#!/usr/bin/env python3
"""Reconcile the dispatch->commit half of the provenance chain.

The receipt path already writes ``dispatch_id`` + ``receipt_id`` into the provenance_registry
at append time (B1). The commit half lands later: a governed worker commits with a
``Dispatch-ID:`` trace token (stamped by the prepare-commit-msg hook, which reads
``VNX_CURRENT_DISPATCH_ID`` set by the dispatch lane). This script scans recent git commits for
those tokens and writes each commit's SHA back into the registry so ``chain_status`` can reach
``complete`` and the dashboard Observability panel shows the closed chain.

Usage:
    python3 scripts/reconcile_provenance.py                 # reconcile central store for this project
    python3 scripts/reconcile_provenance.py --max-commits 500
    python3 scripts/reconcile_provenance.py --project-id mission-control
    python3 scripts/reconcile_provenance.py --json          # structured output

Idempotent: register_provenance_link upserts per dispatch_id. Read-git + write-registry only.

BILLING SAFETY: No Anthropic SDK. No LLM calls.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from project_root import (  # noqa: E402
    resolve_central_data_dir,
    resolve_project_id,
    resolve_project_root,
)
from receipt_provenance import reconcile_commit_provenance  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-commits", type=int, default=300,
                        help="how many recent commits to scan (default: 300)")
    parser.add_argument("--project-id", default=None,
                        help="central-store project_id (default: resolved for this repo)")
    parser.add_argument("--json", action="store_true", help="emit a JSON result line")
    args = parser.parse_args(argv)

    repo_root = resolve_project_root(__file__)
    try:
        project_id = args.project_id or resolve_project_id(repo_root)
    except RuntimeError as exc:
        print(f"reconcile_provenance: cannot resolve project_id: {exc}", file=sys.stderr)
        return EXIT_ERROR

    db_path = resolve_central_data_dir(project_id) / "state" / "runtime_coordination.db"
    if not db_path.exists():
        result = {"scanned": 0, "linked": 0, "degraded": f"no registry db at {db_path}"}
        print(json.dumps(result) if args.json else
              f"reconcile_provenance: no registry db ({db_path}) — nothing to do")
        return EXIT_OK

    conn = sqlite3.connect(str(db_path))
    try:
        if not _table_exists(conn, "provenance_registry"):
            result = {"scanned": 0, "linked": 0, "degraded": "provenance_registry table missing"}
            print(json.dumps(result) if args.json else
                  "reconcile_provenance: provenance_registry table missing — nothing to do")
            return EXIT_OK
        result = reconcile_commit_provenance(repo_root, conn, max_commits=args.max_commits)
        conn.commit()
    finally:
        conn.close()

    result["project_id"] = project_id
    if args.json:
        print(json.dumps(result))
    else:
        print(f"reconcile_provenance[{project_id}]: scanned {result['scanned']} commit(s), "
              f"linked {result['linked']} dispatch->commit edge(s)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
