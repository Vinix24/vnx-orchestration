#!/usr/bin/env python3
"""learning_misfile_report.py: measure misfiled learning rows, and verify a transfer (reattr L6).

Measures, never writes: every database is opened ``mode=ro`` (ADR-068).

``report``: per database the L2 category counts, the L3a ``schema_gap`` and ``collision`` counts
(a collision is no longer a move) and the rows of tables without ``project_id`` (``no_project_column``).
An unreadable database is named under ``errors`` and the exit code is 1.

``verify --plan PLAN.json``: exit 0 only when every planned row stands once in the
destination (``stable_key``, the content key without the columns a writer updates in place, and
``project_id``), zero times in the source under the source project, and passes the consumer's selector predicate
(``valid_until IS NULL OR valid_until > now``). Exit 1: a row failed or a database
cannot be read. A found row whose mutable columns differ from the pre-image is listed under
``mutated_since_move`` (old and new value per column); it is proven, not a failure. Exit 2: the plan is malformed, empty or missing.

CLI:
  python3 scripts/learning_misfile_report.py report (--all | --db PATH) [--vnx-data-root DIR] --json
  python3 scripts/learning_misfile_report.py verify --plan PLAN.json --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from pattern_reattribution import (  # noqa: E402
    STATUS_ABSENT, STATUS_AMBIGUOUS, STATUS_MOVES, STATUS_POSITIVE_OWN,
    STATUS_STORE_ONLY, STATUS_UNATTRIBUTED, TABLES, build_attribution_index,
)
from qi_transfer_plan import (  # noqa: E402
    IDENTITY_EXCLUDED, MUTABLE_COLUMNS, _scan_rows, content_key, open_readonly, plan_database, stable_key,
)

# report key -> L2 status
CATEGORIES = {
    "moves": STATUS_MOVES, "unattributed": STATUS_UNATTRIBUTED, "ambiguous": STATUS_AMBIGUOUS,
    "store_only": STATUS_STORE_ONLY, "unchanged": STATUS_POSITIVE_OWN, "absent": STATUS_ABSENT,
}
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


class MalformedPlan(Exception):
    """The plan cannot be read as a plan: never a pass."""


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def report_database(db: Path, index: Any) -> Dict[str, int]:
    conn = open_readonly(db)
    try:
        counts = Counter(d.status for d in _scan_rows(conn, db, index))
    finally:
        conn.close()
    planned, _ = plan_database(db, index)
    out: Dict[str, Any] = {key: counts.get(status, 0) for key, status in CATEGORIES.items()}
    out["schema_gap"] = len(planned["schema_gap"])
    # A collision is a row L2 decided as a move that the destination already holds: not a move.
    out["collision"] = len(planned["collision"])
    out["moves"] -= out["collision"]
    out["no_project_column"] = sum(planned["no_project_column"].values())
    out["no_project_column_tables"] = sorted(planned["no_project_column"])
    return out


def build_report(dbs: List[Path], index: Any) -> Dict[str, Any]:
    databases: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for db in dbs:
        try:
            databases[str(db)] = report_database(db, index)
        except sqlite3.Error as exc:  # an unreadable database is an error, never a zero
            errors[str(db)] = str(exc)
    return {"databases": databases, "errors": errors}


def _run_report(args: argparse.Namespace) -> int:
    root = args.vnx_data_root or (Path.home() / ".vnx-data")
    index = build_attribution_index(root)
    dbs = {args.db.resolve()} if args.db else set()
    if args.all:
        dbs |= {p.resolve() for p in index.qi_databases.values()}
    if not dbs:
        print("[learning_misfile_report] report needs --all or --db", file=sys.stderr)
        return EXIT_USAGE
    report = build_report(sorted(dbs), index)
    print(json.dumps(report, indent=2, sort_keys=True))
    return EXIT_FAILED if report["errors"] or not report["databases"] else EXIT_OK


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def load_plan(path: Path) -> List[Dict[str, Any]]:
    """The plan's moves, or MalformedPlan. A plan that proves nothing is malformed."""
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MalformedPlan(f"plan unreadable: {exc}") from exc
    moves = plan.get("planned_moves") if isinstance(plan, dict) else None
    if not isinstance(moves, list) or not moves:
        raise MalformedPlan("planned_moves is missing, not a list or empty")
    if plan.get("moves") != len(moves):
        raise MalformedPlan("moves does not equal the number of planned_moves")
    for i, move in enumerate(moves):
        _check_move(i, move)
    return moves


def _check_move(i: int, move: Any) -> None:
    if not isinstance(move, dict):
        raise MalformedPlan(f"planned_moves[{i}] is not an object")
    for field in ("source_db", "destination_db", "project_id", "destination_project_id", "content_key"):
        if not isinstance(move.get(field), str) or not move[field]:
            raise MalformedPlan(f"planned_moves[{i}].{field} is missing or not a string")
    image = move.get("pre_image")
    row = image.get("row") if isinstance(image, dict) else None
    if move.get("table") not in TABLES or not isinstance(row, dict):
        raise MalformedPlan(f"planned_moves[{i}] names no known table or has no pre_image row")
    if content_key(row) != move["content_key"]:
        raise MalformedPlan(f"planned_moves[{i}].content_key does not match its pre_image")


def _matching_rows(db: str, table: str, project_id: str, key: str, columns: List[str]) -> List[Dict[str, Any]]:
    """Rows of ``project_id`` in ``db`` whose stable_key is ``key``; each carries ``_selectable``.

    ``_selectable`` is the consumer's selector predicate evaluated by SQLite
    itself. A missing ``valid_until`` column raises: the consumer could not
    read the table either, so it is never a pass.
    """
    conn = open_readonly(Path(db))
    try:
        cur = conn.execute(
            f"SELECT *, (valid_until IS NULL OR valid_until > datetime('now')) AS _selectable "
            f"FROM {table} WHERE project_id = ?", (project_id,))
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return [r for r in rows if all(c in r for c in columns) and stable_key(table, {c: r[c] for c in columns}) == key]


def _mutations(move: Dict[str, Any], dest_row: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Per mutable column the pre-image and destination value, where they differ."""
    pre = move["pre_image"]["row"]
    cols = MUTABLE_COLUMNS.get(move["table"], frozenset())
    return {c: {"old": pre[c], "new": dest_row[c]}
            for c in sorted(cols) if c in pre and c in dest_row and pre[c] != dest_row[c]}


def verify_move(move: Dict[str, Any], mutated: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """None when the move is proven done, else the failing row with its reason.

    A proven row whose mutable columns changed since the move is appended to ``mutated``."""
    named = {k: move.get(k) for k in ("table", "source_id", "content_key")}
    key = stable_key(move["table"], move["pre_image"]["row"])
    columns = [c for c in move["pre_image"]["row"] if c not in IDENTITY_EXCLUDED]
    try:
        dest = _matching_rows(move["destination_db"], move["table"], move["destination_project_id"],
                              key, columns)
    except sqlite3.Error as exc:
        return {**named, "reason": "destination_unreadable", "detail": str(exc)}
    if not dest:
        return {**named, "reason": "missing_in_destination"}
    if len(dest) > 1:
        return {**named, "reason": "duplicate_in_destination", "count": len(dest)}
    if dest[0]["_selectable"] != 1:  # NULL or 0 both mean the consumer cannot see it
        return {**named, "reason": "not_selectable_by_consumer"}
    try:
        left = _matching_rows(move["source_db"], move["table"], move["project_id"], key, columns)
    except sqlite3.Error as exc:
        return {**named, "reason": "source_unreadable", "detail": str(exc)}
    if left:
        return {**named, "reason": "still_in_source", "count": len(left)}
    changed = _mutations(move, dest[0])
    if changed and mutated is not None:
        mutated.append({"table": move["table"], "source_id": move.get("source_id"), "columns": changed})
    return None


def verify_plan(moves: List[Dict[str, Any]]) -> Dict[str, Any]:
    mutated: List[Dict[str, Any]] = []
    failures = [f for f in (verify_move(m, mutated) for m in moves) if f is not None]
    planned, present = len(moves), len(moves) - len(failures)
    return {"ok": not failures and present == planned, "planned": planned, "present": present,
            "moved_total": planned, "failures": failures, "mutated_since_move": mutated}


def _run_verify(args: argparse.Namespace) -> int:
    try:
        moves = load_plan(args.plan)
    except MalformedPlan as exc:
        print(json.dumps({"ok": False, "reason": "plan_malformed", "detail": str(exc)}, indent=2))
        return EXIT_USAGE
    result = verify_plan(moves)
    print(json.dumps(result, indent=2, sort_keys=True))
    return EXIT_OK if result["ok"] else EXIT_FAILED


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Report misfiled learning rows and verify a transfer.")
    sub = p.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="category counts per database")
    rep.add_argument("--all", action="store_true", help="every project database under --vnx-data-root")
    rep.add_argument("--db", type=Path, default=None, help="one quality_intelligence.db")
    rep.add_argument("--vnx-data-root", type=Path, default=None)
    rep.add_argument("--json", action="store_true", help="JSON output (the only format)")
    ver = sub.add_parser("verify", help="check a plan after it ran")
    ver.add_argument("--plan", type=Path, required=True)
    ver.add_argument("--json", action="store_true", help="JSON output (the only format)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    return _run_report(args) if args.command == "report" else _run_verify(args)


if __name__ == "__main__":
    sys.exit(main())
