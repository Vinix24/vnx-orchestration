#!/usr/bin/env python3
"""qi_transfer_plan.py: the plan of a cross-database pattern move (reattr L3a).

Plans, never runs: every database is opened ``mode=ro``, the plan goes to
stdout only; ``--out`` follows in dispatch ``dlv-2df4000f3899-b``. L3b executes
it, L6 verifies it (ADR-068 besluit 13-17).
Only L2 ``moves`` rows become moves (13: full row plus junction rows keyed on
``pattern_id``); ``content_key`` is sha256 over all columns but ``id`` and
``project_id`` (14); a non-NULL value the destination has no column for is
``schema_gap`` (15); the journal path and states are defined, not written (17).

CLI: python3 scripts/lib/qi_transfer_plan.py --db PATH [--all --vnx-data-root DIR] --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pattern_reattribution import (
    _PATTERN_ID_PREFIX,
    STATUS_MOVES,
    TABLES,
    AttributionIndex,
    ReattributionDecision,
    build_attribution_index,
    decide_row,
)

JUNCTION_TABLES: Tuple[str, ...] = (
    "pattern_usage",
    "dispatch_pattern_offered",
    "pattern_injection_outcome",
)
# Columns that never take part in identity (besluit 14).
IDENTITY_EXCLUDED = ("id", "project_id")

# Journal states (besluit 17): ``copied`` carries the destination id as ``dest_id``.
JOURNAL_STATES: Tuple[str, ...] = ("planned", "copied", "verified", "source_removed", "done")

PLAN_VERSION = 1


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """A connection that cannot write: ``mode=ro`` URI, rows as ``sqlite3.Row``."""
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace; ``json`` emits UTF-8-safe text, bytes become hex."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes_hex__": bytes(value).hex()}
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def content_key(row: Dict[str, Any]) -> str:
    """sha256 hex over every column except ``id`` and ``project_id``."""
    body = {k: v for k, v in row.items() if k not in IDENTITY_EXCLUDED}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def journal_path(source_db: Path, plan_id: str) -> Path:
    """``<state dir of the source project>/reattribution/<plan-id>/journal.ndjson``."""
    return Path(source_db).resolve().parent / "reattribution" / plan_id / "journal.ndjson"


def pattern_id_for(table: str, row_id: int) -> str:
    return f"{_PATTERN_ID_PREFIX[table]}{row_id}"


def _columns(conn: sqlite3.Connection, table: str) -> Optional[List[str]]:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    return cols or None  # None: the table does not exist


def _fetch_rows(conn: sqlite3.Connection, table: str, where: str, arg: Any) -> List[Dict[str, Any]]:
    if _columns(conn, table) is None:
        return []
    cur = conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY rowid", (arg,))
    return [dict(r) for r in cur.fetchall()]


def read_pre_image(conn: sqlite3.Connection, table: str, row_id: int) -> Optional[Dict[str, Any]]:
    """The full row and its junction rows, or ``None`` when the row is gone."""
    rows = _fetch_rows(conn, table, "id = ?", row_id)
    if not rows:
        return None
    pattern_id = pattern_id_for(table, row_id)
    junction = {t: _fetch_rows(conn, t, "pattern_id = ?", pattern_id) for t in JUNCTION_TABLES}
    return {"table": table, "row": rows[0], "junction": junction, "pattern_id": pattern_id}


def schema_gaps(dest: sqlite3.Connection, pre_image: Dict[str, Any]) -> List[str]:
    """Every ``table.column`` holding a non-NULL source value the destination lacks."""
    gaps: List[str] = []
    tables = [(pre_image["table"], [pre_image["row"]])]
    tables += [(t, rows) for t, rows in pre_image["junction"].items() if rows]
    for table, rows in tables:
        dest_cols = set(_columns(dest, table) or [])
        for col in rows[0]:
            if col == "id" or col in dest_cols:
                continue
            if any(r.get(col) is not None for r in rows):
                gaps.append(f"{table}.{col}")
    return sorted(gaps)


def _entry(d: ReattributionDecision) -> Dict[str, Any]:
    return {
        "table": d.table, "source_id": d.row_id, "title": d.title,
        "status": d.status, "project_id": d.old_project_id,
        "resolved_projects": d.resolved_projects,
    }


def _scan_rows(conn: sqlite3.Connection, db_path: Path, index: AttributionIndex) -> List[ReattributionDecision]:
    """L2's decision for every row of one database, read on the caller's snapshot."""
    decisions: List[ReattributionDecision] = []
    for table in TABLES:
        if _columns(conn, table) is None:
            continue
        for r in conn.execute(f"SELECT id, title, project_id, source_dispatch_ids FROM {table} ORDER BY id"):
            decisions.append(decide_row(
                table=table, row_id=r["id"], title=r["title"] or "", project_id=r["project_id"],
                source_dispatch_ids_raw=r["source_dispatch_ids"], dispatch_index=index, db_path=db_path,
            ))
    return decisions


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _plan_move(src: sqlite3.Connection, src_db: Path, src_version: int, d: ReattributionDecision) -> Dict[str, Any]:
    """One decided move: a planned move, a schema_gap or an unplannable entry."""
    pre = read_pre_image(src, d.table, d.row_id)
    if pre is None:
        return {"kind": "unplanned", "reason": "source_row_missing", **_entry(d)}
    try:
        dest = open_readonly(Path(d.destination_db or ""))
        try:
            gaps = schema_gaps(dest, pre)
            dest_version = _user_version(dest)
        finally:
            dest.close()
    except sqlite3.Error as exc:  # an unreadable destination is never a plan
        return {"kind": "unplanned", "reason": f"destination_unreadable: {exc}", **_entry(d)}
    base = {
        **_entry(d), "pattern_id": pre["pattern_id"], "source_db": str(src_db),
        "destination_db": d.destination_db, "destination_project_id": d.new_project_id,
        "source_user_version": src_version, "destination_user_version": dest_version,
    }
    if gaps:
        return {"kind": "schema_gap", "columns": gaps, **base}
    return {
        "kind": "move", **base, "content_key": content_key(pre["row"]),
        "pre_image": {"row": pre["row"], "junction": pre["junction"]},
    }


def plan_database(db_path: Path, index: AttributionIndex) -> Tuple[Dict[str, List[Dict[str, Any]]], int]:
    """Moves, schema gaps, unplannable moves and listed rows for one source database,
    plus its ``user_version``: all read inside ONE read transaction (one snapshot)."""
    out: Dict[str, List[Dict[str, Any]]] = {"moves": [], "schema_gap": [], "unplanned": [], "listed": []}
    src = open_readonly(db_path)
    try:
        src.execute("BEGIN")
        version = _user_version(src)  # the first read pins the snapshot
        for d in _scan_rows(src, db_path, index):
            if d.status != STATUS_MOVES:
                if d.status != "positive_own":
                    out["listed"].append({**_entry(d), "source_db": str(db_path)})
                continue
            item = _plan_move(src, db_path, version, d)
            key = {"move": "moves", "schema_gap": "schema_gap"}.get(item["kind"], "unplanned")
            out[key].append(item)
        src.rollback()
    finally:
        src.close()
    return out, version


def build_plan(db_paths: List[Path], index: AttributionIndex) -> Dict[str, Any]:
    """The whole plan; ``plan_id`` is the sha256 of its own content, so it is reproducible."""
    parts: Dict[str, List[Dict[str, Any]]] = {"moves": [], "schema_gap": [], "unplanned": [], "listed": []}
    versions: Dict[str, int] = {}
    for db in db_paths:
        planned, versions[str(db)] = plan_database(db, index)
        for key, items in planned.items():
            parts[key].extend(items)
    body = {
        "plan_version": PLAN_VERSION, "user_versions": versions,
        "journal_states": list(JOURNAL_STATES),
        "planned_moves": parts["moves"], "schema_gap_rows": parts["schema_gap"],
        "unplanned_rows": parts["unplanned"], "listed_rows": parts["listed"],
    }
    plan_id = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()[:16]
    for move in body["planned_moves"]:
        move["journal_path"] = str(journal_path(Path(move["source_db"]), plan_id))
    return {
        "plan_id": plan_id, "moves": len(parts["moves"]), "schema_gap": len(parts["schema_gap"]),
        "unplanned": len(parts["unplanned"]), "listed": len(parts["listed"]), **body,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Plan (never run) a cross-database pattern move.")
    p.add_argument("--db", type=Path, required=True, help="source quality_intelligence.db")
    p.add_argument("--all", action="store_true", help="plan every project database under --vnx-data-root")
    p.add_argument("--vnx-data-root", type=Path, default=None)
    p.add_argument("--json", action="store_true", help="JSON output (the only format)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if not args.db.is_file():
        print(f"[qi_transfer_plan] DB not found: {args.db}", file=sys.stderr)
        return 2
    root = args.vnx_data_root or (Path.home() / ".vnx-data")
    index = build_attribution_index(root)
    dbs = sorted(set(index.qi_databases.values()) | {args.db.resolve()}) if args.all else [args.db]
    plan = build_plan(dbs, index)
    text = json.dumps(plan, indent=2, sort_keys=True, default=_json_default)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
