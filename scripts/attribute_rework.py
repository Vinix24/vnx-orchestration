#!/usr/bin/env python3
"""Attribute rework to the originating dispatch + role, and persist the origin link.

Slice 1 of the rework->skill loop. Traverses git same-line churn over commits carrying a Dispatch-ID
trace token (post-#969), blames the replaced lines back to the origin token-commit, and writes the
dominant origin into ``dispatch_metadata.parent_dispatch`` so "rework -> original dispatch -> role" is a
persistent, auditable edge. Also prints per-role first-pass success (already-populated data) so the
pattern is visible immediately.

Usage:
    python3 scripts/attribute_rework.py                 # attribute + persist for this project
    python3 scripts/attribute_rework.py --no-persist    # dry: compute + print, write nothing
    python3 scripts/attribute_rework.py --json
    python3 scripts/attribute_rework.py --project-id mission-control --max-commits 500

Read-git + a single fill-once UPDATE per rework edge. Idempotent. No LLM, no Anthropic SDK.
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
from rework_attribution import (  # noqa: E402
    compute_rework_edges,
    rework_by_origin_role,
    success_by_role,
)

EXIT_OK = 0
EXIT_ERROR = 1


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-commits", type=int, default=300)
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--no-persist", action="store_true", help="compute only; do not write parent_dispatch")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    repo_root = resolve_project_root(__file__)
    try:
        project_id = args.project_id or resolve_project_id(repo_root)
    except RuntimeError as exc:
        print(f"attribute_rework: cannot resolve project_id: {exc}", file=sys.stderr)
        return EXIT_ERROR

    state = resolve_central_data_dir(project_id) / "state"
    rc_path = state / "runtime_coordination.db"
    qi_path = state / "quality_intelligence.db"
    if not qi_path.exists():
        msg = {"degraded": f"no quality_intelligence.db at {qi_path}", "edges": [], "persisted": 0}
        print(json.dumps(msg) if args.json else f"attribute_rework: {msg['degraded']} — nothing to do")
        return EXIT_OK

    # Open inside try: a corrupted DB file must fail-open like a missing one, not exit with an error.
    try:
        rc_conn = sqlite3.connect(f"file:{rc_path}?mode=ro", uri=True) if rc_path.exists() else None
        qi_conn = sqlite3.connect(str(qi_path))
    except sqlite3.Error as exc:
        msg = {"degraded": f"cannot open store: {exc}", "edges": [], "persisted": 0}
        print(json.dumps(msg) if args.json else f"attribute_rework: {msg['degraded']} — nothing to do")
        return EXIT_OK
    try:
        if rc_conn is None:
            result = {"scanned": 0, "edges": [], "persisted": 0}
        else:
            result = compute_rework_edges(
                repo_root, rc_conn, qi_conn, project_id,
                max_commits=args.max_commits, persist=not args.no_persist,
            )
            qi_conn.commit()
        roles = success_by_role(qi_conn)
        rework_roles = rework_by_origin_role(qi_conn, project_id)
    finally:
        qi_conn.close()
        if rc_conn is not None:
            rc_conn.close()

    payload = {
        "project_id": project_id,
        "scanned": result["scanned"],
        "edges": result["edges"],
        "persisted": result["persisted"],
        "success_by_role": roles,
        "rework_by_origin_role": rework_roles,
    }
    if args.json:
        print(json.dumps(payload))
        return EXIT_OK

    print(f"attribute_rework[{project_id}]: scanned {result['scanned']} token-commit(s), "
          f"found {len(result['edges'])} rework edge(s), persisted {result['persisted']}")
    for e in result["edges"]:
        print(f"  rework {e['rework_dispatch']} ({e['rework_role']}) "
              f"<- origin {e['origin_dispatch']} ({e['origin_role']}) [{e['lines']} line(s)]")
    if rework_roles:
        print("rework by origin role:")
        for r in rework_roles:
            print(f"  {r['origin_role']}: {r['reworked']} reworked")
    print("first-pass success by role (top 8):")
    for r in roles[:8]:
        print(f"  {r['role']}: {r['successes']}/{r['total']} ({r['success_rate']})")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
