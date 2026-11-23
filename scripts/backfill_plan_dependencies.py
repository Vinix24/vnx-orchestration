#!/usr/bin/env python3
"""One-time fill of deliverable dependencies and paths from the plan tables.

Dry run by default. ``--apply`` writes exactly what the dry run showed: a
dependency through ``deliverable_dependencies.depend`` (an active row is skipped,
a removed row is NEVER restored) and ``dispatch_paths`` through the write path of
``vnx deliverable set --dispatch-paths``, only where no paths exist. A second
``--apply`` writes nothing. Nothing is guessed; no raw write SQL.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE / "lib"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import deliverable_dependencies as dlv_deps  # noqa: E402
import git_common  # noqa: E402
import plan_table  # noqa: E402
import planning_cli  # noqa: E402
import project_root as project_root_lib  # noqa: E402

CREATED_BY = "backfill_plan_dependencies"
OPEN_STATES = ("proposed", "ready")
DEPENDENCY_STATUSES = ("new", "exists", "removed", "cycle", "unknown_predecessor", "unmapped")
PATH_STATUSES = ("proposed", "has_paths", "error")
_NO_PATHS = (None, [])

def _default_plans_root() -> Path:
    """The main checkout's gitignored ``claudedocs/``; a worktree has no copy."""
    root = project_root_lib.resolve_project_root(__file__)
    common = git_common.git_common_dir(root)
    return (common.parent if common.name == ".git" else root) / "claudedocs"

def _metadata(raw: Any) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):  # vnx-silent-except: a malformed blob is not evidence of a deliverable; the row is left out
        return {}
    return value if isinstance(value, dict) else {}

def _connect(state_dir: Path) -> sqlite3.Connection:
    database = state_dir / "runtime_coordination.db"
    if not database.is_file():
        raise FileNotFoundError(f"no store at {database}")
    conn = sqlite3.connect(str(database), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def _open_tracks(conn: sqlite3.Connection, project_id: str) -> List[str]:
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT d.track FROM dispatches d JOIN tracks t ON t.track_id = d.track "
        "AND t.project_id = d.project_id WHERE d.project_id = ? AND d.state IN ('proposed', 'ready') "
        "AND t.phase != 'done' AND d.track IS NOT NULL AND d.track != '' AND json_valid(d.metadata_json) "
        "AND json_extract(d.metadata_json, '$.deliverable') = 1 ORDER BY d.track", (project_id,))]

def _deliverables(conn: sqlite3.Connection, project_id: str, track_id: str) -> List[Dict[str, Any]]:
    records = []  # any state: a completed predecessor still maps
    for row in conn.execute("SELECT dispatch_id, state, metadata_json FROM dispatches "
                            "WHERE project_id = ? AND track = ?", (project_id, track_id)):
        meta = _metadata(row["metadata_json"])
        if meta.get("deliverable") is not True:
            continue
        title = meta.get("title")
        records.append({"id": row["dispatch_id"], "state": row["state"],
                        "title": title if isinstance(title, str) else "",
                        "paths": meta.get("dispatch_paths")})
    return records

def _dependency_state(conn: sqlite3.Connection, project_id: str):
    """{(dlv, on): row} for every row, and the active-only adjacency."""
    rows = {(r["deliverable_id"], r["depends_on_id"]): r for r in conn.execute(
        "SELECT deliverable_id, depends_on_id, kind, removed_at, removed_reason "
        "FROM deliverable_dependencies WHERE project_id = ?", (project_id,))}
    edges: Dict[str, set] = {}
    for (deliverable, on_id), row in rows.items():
        if row["removed_at"] is None:
            edges.setdefault(deliverable, set()).add(on_id)
    return rows, edges

def _cycle(conn, project_id: str, edges: Dict[str, set], deliverable: str, on_id: str) -> Optional[List[str]]:
    canonical = dlv_deps.find_cycle(conn, project_id, deliverable, on_id)
    if canonical:
        return canonical
    stack, seen = [[deliverable, on_id]], {on_id}
    while stack:
        path = stack.pop()
        for next_id in sorted(edges.get(path[-1], ())):
            if next_id == deliverable:
                return [*path, deliverable]
            if next_id not in seen:
                seen.add(next_id)
                stack.append([*path, next_id])
    return None

def _map_row(deliverables, by_id, row: plan_table.Row) -> Tuple[Optional[str], str, Optional[str]]:
    if row.dlv_id:
        target = by_id.get(row.dlv_id)
        if target is None:
            return None, f"id {row.dlv_id} is not a deliverable of this track", None
        if plan_table.title_matches_label(row.label, target["title"]):
            return row.dlv_id, "", None
        return row.dlv_id, "", f"label_mismatch: title {target['title']!r} does not start with {row.label!r}"
    target, reason = plan_table.map_label(
        row.label, [(record["id"], record["title"]) for record in deliverables])
    return target, reason, None

def _dependency_proposal(conn, project_id, dependency_rows, edges, proposed_edges, titles,
                         track_id, row, target, kind, token, unmapped_reason, mismatch):
    base = {"type": "dependency", "track": track_id, "deliverable": target, "label": row.label,
            "line": row.line, "on": token, "kind": kind, "label_mismatch": mismatch}
    if target is None:
        return {**base, "on_id": None, "status": "unmapped", "detail": unmapped_reason}
    unknown = False
    if plan_table.is_dlv_id(token):
        if dlv_deps.is_deliverable(conn, project_id, token):
            predecessor, reason = token, ""
        else:
            predecessor, reason, unknown = None, f"unknown deliverable {token}", True
    else:
        predecessor, reason = plan_table.map_label(token, titles)
    if predecessor is None:
        return {**base, "on_id": None, "status": "unknown_predecessor" if unknown else "unmapped",
                "detail": reason}
    existing = dependency_rows.get((target, predecessor))
    if existing is not None and existing["removed_at"] is None:
        status, detail = "exists", "an active dependency is already recorded"
    elif existing is not None:
        status = "removed"
        detail = f"removed on {existing['removed_at']}: {existing['removed_reason']}, not restored"
    elif (target, predecessor) in proposed_edges:
        status, detail = "exists", "already proposed earlier in this run"
    else:
        cycle = _cycle(conn, project_id, edges, target, predecessor)
        if cycle:
            status, detail = "cycle", "would close a cycle: " + " -> ".join(cycle)
        else:
            status, detail = "new", f"new {kind} dependency"
            edges.setdefault(target, set()).add(predecessor)
            proposed_edges.add((target, predecessor))
    return {**base, "on_id": predecessor, "status": status, "detail": detail}

def _paths_proposal(track_id, row, target, by_id, unmapped_reason):
    if row.paths_error:
        return {"type": "paths", "track": track_id, "deliverable": target, "label": row.label,
                "line": row.line, "paths": None, "status": "error", "detail": row.paths_error}
    if not row.paths:
        return None
    if target is None:
        status, detail = "error", f"row is unmapped: {unmapped_reason}"
    elif by_id[target]["paths"] not in _NO_PATHS:
        status, detail = "has_paths", "existing dispatch_paths are not overwritten"
    else:
        status, detail = "proposed", "no dispatch_paths yet"
    return {"type": "paths", "track": track_id, "deliverable": target, "label": row.label,
            "line": row.line, "paths": row.paths, "status": status, "detail": detail}

def _plan_track(conn, project_id: str, track_id: str, plans_root: Path, dependency_rows,
                edges, proposed_edges) -> Dict[str, Any]:
    plan_path, plan_note = plan_table.find_plan(track_id, plans_root)
    table, plan_status = plan_table.Table(Path(""), found=False), "none"
    if plan_path is not None:
        plan_status, table = "one", plan_table.parse(plan_path)
    elif plan_note.startswith("several plans:"):
        plan_status = "several"
    deliverables = _deliverables(conn, project_id, track_id)
    by_id = {record["id"]: record for record in deliverables}
    titles = [(record["id"], record["title"]) for record in deliverables]
    proposals: List[Dict[str, Any]] = []
    order_errors: List[Dict[str, Any]] = []
    mapped_targets: set = set()
    mapped_rows = 0
    for row in table.rows:
        target, unmapped_reason, mismatch = _map_row(deliverables, by_id, row)
        if target is not None:
            mapped_rows += 1
            mapped_targets.add(target)
        if row.order_error:
            order_errors.append({"line": row.line, "label": row.label, "error": row.order_error})
        else:
            proposals.extend(_dependency_proposal(
                conn, project_id, dependency_rows, edges, proposed_edges, titles,
                track_id, row, target, kind, token, unmapped_reason, mismatch)
                for token, kind in row.depends)
        path_proposal = _paths_proposal(track_id, row, target, by_id, unmapped_reason)
        if path_proposal is not None:
            proposals.append(path_proposal)
    dependency_counts = Counter(p["status"] for p in proposals if p["type"] == "dependency")
    path_counts = Counter(p["status"] for p in proposals if p["type"] == "paths")
    open_ids = [record["id"] for record in deliverables if record["state"] in OPEN_STATES]
    return {"track": track_id, "plan": plan_status,
            "plan_path": str(plan_path) if plan_path is not None else None, "plan_note": plan_note,
            "rows": len(table.rows), "rows_mapped": mapped_rows,
            "dependencies": {s: dependency_counts.get(s, 0) for s in DEPENDENCY_STATUSES},
            "paths": {s: path_counts.get(s, 0) for s in PATH_STATUSES},
            "order_errors": order_errors, "open_deliverables": open_ids,
            "uncovered_open": [d for d in open_ids if d not in mapped_targets], "proposals": proposals}

def _totals(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    plans = Counter(report["plan"] for report in reports)
    return {"tracks": len(reports), "plan": {s: plans.get(s, 0) for s in ("one", "none", "several")},
            "rows": sum(r["rows"] for r in reports), "rows_mapped": sum(r["rows_mapped"] for r in reports),
            "dependencies": {s: sum(r["dependencies"][s] for r in reports) for s in DEPENDENCY_STATUSES},
            "paths": {s: sum(r["paths"][s] for r in reports) for s in PATH_STATUSES},
            "open_deliverables": sum(len(r["open_deliverables"]) for r in reports),
            "uncovered_open": sorted({d for r in reports for d in r["uncovered_open"]})}

def _apply(conn: sqlite3.Connection, project_id: str, reports, now: str) -> Tuple[int, int]:
    pending = [(report, p) for report in reports for p in report["proposals"]
               if (p["type"] == "dependency" and p["status"] == "new")
               or (p["type"] == "paths" and p["status"] == "proposed")]
    if not pending:
        return 0, 0
    dependencies = paths = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for report, proposal in pending:
            if proposal["type"] == "dependency":
                dlv_deps.depend(conn, project_id, proposal["deliverable"], proposal["on_id"],
                                kind=proposal["kind"], created_by=CREATED_BY, now=now)
                dependencies += 1
                continue
            current = conn.execute("SELECT metadata_json FROM dispatches WHERE project_id = ? "
                                   "AND dispatch_id = ?", (project_id, proposal["deliverable"])).fetchone()
            if current is None:
                raise RuntimeError(f"deliverable {proposal['deliverable']!r} disappeared")
            if _metadata(current[0]).get("dispatch_paths") not in _NO_PATHS:
                proposal["status"] = "has_paths"  # another writer got there first: never overwrite
                proposal["detail"] = "existing dispatch_paths appeared before the write; not overwritten"
                report["paths"]["proposed"] -= 1
                report["paths"]["has_paths"] += 1
                continue
            if planning_cli.apply_deliverable_metadata_fields(
                conn, project_id, proposal["deliverable"], {"dispatch_paths": proposal["paths"]},
                actor=CREATED_BY, reason="backfill_plan_dependencies: dispatch_paths") is None:
                raise RuntimeError(f"deliverable {proposal['deliverable']!r} disappeared")
            paths += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return dependencies, paths

def _counts(counts: Dict[str, int], names) -> str:
    return ", ".join(f"{name}={counts[name]}" for name in names)

def _print_text(payload: Dict[str, Any]) -> None:
    for report in payload["tracks"]:
        print(f"Track {report['track']}: plan {report['plan']} ({report['plan_path'] or report['plan_note']})")
        print(f"  rows {report['rows']}, mapped {report['rows_mapped']}; "
              f"deps {_counts(report['dependencies'], DEPENDENCY_STATUSES)}; "
              f"paths {_counts(report['paths'], PATH_STATUSES)}")
        for error in report["order_errors"]:
            print(f"  ! {error['error']} (row label {error['label']!r})")
        for p in report["proposals"]:
            target = p.get("on") if p["type"] == "dependency" else p.get("paths")
            print(f"  {p['deliverable'] or p['label']} <- {target}: {p['status']} ({p['detail']})")
        if report["uncovered_open"]:
            print("  uncovered open: " + ", ".join(report["uncovered_open"]))
    total = payload["total"]
    print(f"Total: {total['tracks']} tracks, {total['rows']} rows, {total['rows_mapped']} mapped, "
          f"deps {_counts(total['dependencies'], DEPENDENCY_STATUSES)}; "
          f"paths {_counts(total['paths'], PATH_STATUSES)}; "
          f"open {total['open_deliverables']}, uncovered {len(total['uncovered_open'])}")
    if payload["apply"]:
        print(f"Applied: {payload['applied']['dependencies']} dependencies, {payload['applied']['paths']} path sets.")
    else:
        print("Dry run: nothing written. Re-run with --apply to write these proposals.")

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Propose (and with --apply write) deliverable dependencies and paths from plan tables.")
    parser.add_argument("--project-id", required=True, help="project id (ADR-007 key)")
    parser.add_argument("--track", action="append", default=None, metavar="ID",
                        help="track to read (repeatable; default: every open track with open deliverables)")
    parser.add_argument("--plans-root", default=None, metavar="DIR",
                        help="plans tree (default: the main checkout's claudedocs/)")
    parser.add_argument("--state-dir", default="", help="store directory (default: the resolved VNX state dir)")
    parser.add_argument("--apply", action="store_true", help="write the proposals (default: dry run)")
    parser.add_argument("--json", action="store_true", help="emit the whole report as one JSON object")
    args = parser.parse_args(argv)
    state_dir = planning_cli._resolve_state_dir(args.state_dir)
    plans_root = Path(args.plans_root) if args.plans_root else _default_plans_root()
    if not plans_root.is_dir():
        print(f"backfill: plans root {plans_root} is not a directory. No change made.", file=sys.stderr)
        return 2
    try:
        conn = _connect(state_dir)
    except (OSError, sqlite3.Error) as exc:
        print(f"backfill: cannot open the store: {exc}. No change made.", file=sys.stderr)
        return 1
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'deliverable_dependencies'").fetchone() is None:
            print("backfill: the store has no deliverable_dependencies table: run `vnx migrate` "
                  "(migration 0039) first. No change made.", file=sys.stderr)
            return 2
        tracks = list(args.track) if args.track else _open_tracks(conn, args.project_id)
        for track in tracks if args.track else []:
            if conn.execute("SELECT 1 FROM tracks WHERE project_id = ? AND track_id = ?",
                            (args.project_id, track)).fetchone() is None:
                print(f"backfill: unknown track {track!r} in project {args.project_id!r}. "
                      "No change made.", file=sys.stderr)
                return 2
        dependency_rows, edges = _dependency_state(conn, args.project_id)
        proposed_edges: set = set()
        reports = [_plan_track(conn, args.project_id, track, plans_root, dependency_rows,
                               edges, proposed_edges) for track in tracks]
        payload: Dict[str, Any] = {"project_id": args.project_id, "plans_root": str(plans_root),
                                   "apply": bool(args.apply), "tracks": reports}
        if args.apply:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
            dependencies, paths = _apply(conn, args.project_id, reports, now)
            payload["applied"] = {"dependencies": dependencies, "paths": paths}
        payload["total"] = _totals(reports)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            _print_text(payload)
    except dlv_deps.DependencyRefused as exc:
        print(f"backfill: {exc}. No change made.", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError, sqlite3.Error, RuntimeError) as exc:
        print(f"backfill: {exc}. No change made.", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
