#!/usr/bin/env python3
"""qi_transfer_apply.py: execute a cross-database pattern move plan (reattr L3b, split 1 of 3: copy, verify).

ADR-068 besluit 13-17. Steps 1 and 2 of every move; the source is opened read-only. (1) One ``BEGIN IMMEDIATE`` on
the destination re-reads the source row: ``drift`` (content_key is not the plan's), ``user_version_changed``,
``schema_gap``, ``same_file`` and ``evidence_changed`` (L2 no longer moves it there) change nothing. Else it
journals the pre-image, inserts row and junction rows under the new ``pattern_id`` (or finds them
``already_present``) and journals ``copied`` with ``created`` BEFORE the COMMIT, so a copy that never committed
reads back absent and is copied again. (2) The destination is re-read: ``verified``. Every path checks a ``copied``
line field by field before it reads one. A move ends there and the source still holds the row. Split ``-b`` adds
step 3 (``source_removed``, ``done``) and ``resume``, split ``-c`` adds ``undo``.

CLI: qi_transfer_apply.py apply --plan PLAN.json --reason TEXT [--vnx-data-root DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from atomic_io import slot_lock
from pattern_reattribution import STATUS_MOVES, TABLES, _same_file, build_attribution_index, decide_row
from qi_transfer_plan import (
    JOURNAL_STATES, JUNCTION_TABLES, PLAN_VERSION, _columns, _user_version, canonical_json, content_key,
    journal_path, pattern_id_for, read_pre_image, schema_gaps,
)

_MOVE_KEYS = ("table", "source_id", "source_db", "destination_db", "destination_project_id", "content_key",
              "source_user_version", "destination_user_version")
_FINISHED = ("verified", "already_present")
_SOURCE_KEPT = "the source still holds the row: step 3 (remove it from the source) follows in a later version"


class PlanError(ValueError):
    """A plan or journal line that cannot be trusted: nothing is decided from it."""


def load_plan(path: Path) -> Dict[str, Any]:
    """The plan, only when its ``plan_id`` is the hash of its own content (L3a ``build_plan``)."""
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError(f"plan unreadable: {exc}") from exc
    moves = plan.get("planned_moves") if isinstance(plan, dict) else None
    if not isinstance(moves, list) or not all(
        isinstance(m, dict) and m.get("kind") == "move" and m.get("table") in TABLES
        and all(k in m for k in _MOVE_KEYS) for m in moves
    ):
        raise PlanError("planned_moves is not a list of complete moves on a pattern table")
    body = {k: plan.get(k) for k in ("plan_version", "user_versions", "journal_states", "schema_gap_rows",
                                     "unplanned_rows", "listed_rows")}
    body["planned_moves"] = [{k: v for k, v in m.items() if k != "journal_path"} for m in moves]
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()[:16]
    if body["plan_version"] != PLAN_VERSION or digest != plan.get("plan_id"):
        raise PlanError(f"not a version {PLAN_VERSION} plan whose plan_id is the hash of its content")
    return plan


def _journal(path: Path, line: Dict[str, Any]) -> None:
    """Append one line and fsync it before the caller takes its next step."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with open(path, "a", encoding="utf-8") as fh:  # vnx-atomic-write: append-only journal, fsync per line
        fh.write(canonical_json({**line, "ts": stamp}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_journal(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Lines per move key, cut on bytes (a str also breaks at U+2028); no JSON object with move and state: PlanError."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for n, raw in enumerate(path.read_bytes().splitlines() if path.is_file() else [], 1):
        try:
            line = json.loads(raw)
        except ValueError as exc:
            raise PlanError(f"{path}:{n} is not JSON") from exc
        if not isinstance(line, dict) or not isinstance(line.get("move"), str) or not isinstance(line.get("state"), str):
            raise PlanError(f"{path}:{n} names no move or state")
        out.setdefault(line["move"], []).append(line)
    return out


def _latest(lines: List[Dict[str, Any]], state: Optional[str] = None) -> Optional[Dict[str, Any]]:
    hits = [x for x in lines if (x["state"] == state if state else x["state"] in JOURNAL_STATES)]
    return hits[-1] if hits else None


def _connect(db: Path, mode: str = "rw") -> sqlite3.Connection:
    """Autocommit; ``rw`` or ``ro``, never ``rwc``: a missing file is an error, never a new empty database."""
    conn = sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode={mode}", uri=True, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _key(row: Dict[str, Any], cols: List[str]) -> str:
    return content_key({c: row[c] for c in cols if c in row})


def _find(conn, table: str, project_id: str, key: str, cols: List[str], pattern_id: Optional[str] = None):
    """The rowid of a row of ``project_id`` (and ``pattern_id``) whose content is ``key``, else None."""
    where, args = "project_id = ?", [project_id]
    if pattern_id is not None:
        where, args = where + " AND pattern_id = ?", args + [pattern_id]
    for r in conn.execute(f"SELECT rowid AS rowid_, * FROM {table} WHERE {where}", args):
        if _key(dict(r), cols) == key:
            return r["rowid_"]
    return None


def _insert(conn, table: str, row: Dict[str, Any], cols: List[str], project_id: str) -> int:
    values = {**{c: row[c] for c in cols if c != "id"}, "project_id": project_id}
    sql = f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})"
    return conn.execute(sql, list(values.values())).lastrowid


class _Move:
    def __init__(self, move: Dict[str, Any], plan_id: str):
        self.m, self.table, self.pid = move, move["table"], move["destination_project_id"]
        self.src, self.dst = Path(move["source_db"]), Path(move["destination_db"])
        self.key, self.jpath = f"{self.table}:{move['source_id']}", journal_path(self.src, plan_id)

    def lines(self) -> List[Dict[str, Any]]:
        return read_journal(self.jpath).get(self.key, [])

    def log(self, state: str, **extra: Any) -> None:
        _journal(self.jpath, {"move": self.key, "state": state, **extra})


def _refusal(mv: _Move, pre, src, dst, index) -> Optional[str]:
    m, row = mv.m, (pre or {}).get("row")
    if row is None or content_key(row) != m["content_key"]:
        return "drift"
    if (_user_version(src), _user_version(dst)) != (m["source_user_version"], m["destination_user_version"]):
        return "user_version_changed"
    if schema_gaps(dst, pre):
        return "schema_gap"
    d = decide_row(table=mv.table, row_id=row["id"], title=row.get("title") or "", project_id=row["project_id"],
                   source_dispatch_ids_raw=row.get("source_dispatch_ids"), dispatch_index=index, db_path=mv.src)
    if d.status != STATUS_MOVES or d.new_project_id != mv.pid or not _same_file(Path(d.destination_db or ""), mv.dst):
        return "evidence_changed"
    return None


def _insert_copy(dst, mv: _Move, pre, cols) -> Dict[str, Any]:
    """Row and junction rows into the destination; returns what ``copied`` journals."""
    row_key = _key(pre["row"], cols[mv.table])
    dest_id = _find(dst, mv.table, mv.pid, row_key, cols[mv.table])
    created = dest_id is None
    if created:
        dest_id = _insert(dst, mv.table, pre["row"], cols[mv.table], mv.pid)
    new_pid, junction = pattern_id_for(mv.table, dest_id), []
    for t in JUNCTION_TABLES:
        for j in pre["junction"][t]:
            jrow = {**j, "pattern_id": new_pid}
            jkey = _key(jrow, cols[t])
            fresh = _find(dst, t, mv.pid, jkey, cols[t], pattern_id=new_pid) is None
            if fresh:
                _insert(dst, t, jrow, cols[t], mv.pid)
            junction.append([t, jkey, fresh])
    return {"dest_id": dest_id, "created": created, "row_key": row_key, "pattern_id": new_pid,
            "cols": cols, "junction": junction}


def _copy(mv: _Move, index, reason: str) -> str:
    """Step 1, one ``BEGIN IMMEDIATE`` on the destination: ``copied`` or a refusal."""
    dst, src = _connect(mv.dst), _connect(mv.src, "ro")
    try:
        dst.execute("BEGIN IMMEDIATE")
        src.execute("BEGIN")  # one read snapshot of the source
        pre = read_pre_image(src, mv.table, mv.m["source_id"])
        refusal = "same_file" if _same_file(mv.src, mv.dst) else _refusal(mv, pre, src, dst, index)
        if refusal:
            mv.log(refusal)
            return refusal
        cols = {}  # besluit 15: the columns source and destination both have
        for t in (mv.table,) + JUNCTION_TABLES:
            dest_cols = set(_columns(dst, t) or [])
            cols[t] = [c for c in _columns(src, t) or [] if c in dest_cols]
        mv.log("planned", pre_image=pre, reason=reason)
        copied = _insert_copy(dst, mv, pre, cols)  # a key held with other content raises: rolled back below
        mv.log("copied", **copied)  # before COMMIT: a copy that never committed reads back absent
        dst.execute("COMMIT")
        return "copied"
    finally:
        if dst.in_transaction:
            dst.execute("ROLLBACK")
        src.close()
        dst.close()


def _checked(mv: _Move, c: Dict[str, Any]) -> Dict[str, Any]:
    """The ``copied`` line, once every field this module reads holds what _insert_copy wrote; else a PlanError."""
    cols, junction = c.get("cols"), c.get("junction")
    ok = {"created": isinstance(c.get("created"), bool), "dest_id": type(c.get("dest_id")) is int,  # a bool is no id
          "row_key": isinstance(c.get("row_key"), str),
          "pattern_id": c.get("pattern_id") == pattern_id_for(mv.table, c.get("dest_id")),
          "cols": isinstance(cols, dict) and set(cols) == {mv.table, *JUNCTION_TABLES}
          and all(isinstance(cs, list) and all(isinstance(x, str) for x in cs) for cs in cols.values()),
          "junction": isinstance(junction, list) and all(
              isinstance(j, list) and len(j) == 3 and j[0] in JUNCTION_TABLES and isinstance(j[1], str)
              and isinstance(j[2], bool) for j in junction)}
    if not all(ok.values()):
        raise PlanError(f"{mv.key}: no copied line with a valid {', '.join(f for f, good in ok.items() if not good)}")
    return c


def _verify(mv: _Move, copied: Dict[str, Any]) -> Optional[bool]:
    """Step 2: True when row and every junction row are there by content; None when the copy never committed."""
    cols, dst = copied["cols"], _connect(mv.dst)
    try:
        row = dst.execute(f"SELECT * FROM {mv.table} WHERE id = ?", (copied["dest_id"],)).fetchone()
        if row is None:
            return None
        if row["project_id"] != mv.pid or _key(dict(row), cols[mv.table]) != copied["row_key"]:
            return False
        hits = [(fresh, _find(dst, t, mv.pid, k, cols[t], pattern_id=copied["pattern_id"]) is not None)
                for t, k, fresh in copied["junction"]]
        if all(hit for _, hit in hits):
            return True
        # created false, no junction row of its own and every row it found before: the copy never committed
        return None if not copied["created"] and all(hit != fresh for fresh, hit in hits) else False
    finally:
        dst.close()


def _advance(mv: _Move, index, reason: str) -> str:
    """Run one move from its last journal state to ``verified``; a no-op on a move an earlier run verified."""
    for step in range(3):
        lines = mv.lines()
        state = (_latest(lines) or {}).get("state")
        if state not in (None, "planned", "copied", "verified"):
            raise PlanError(f"{mv.key}: journal state {state} needs step 3, which this version does not run")
        # before any field is read; no copied line at all ({}) misses every field
        copied = _checked(mv, _latest(lines, "copied") or {}) if state in ("copied", "verified") else None
        if state == "verified" and step == 0:  # read again, write nothing
            return "already_present" if _verify(mv, copied) is True else "destination_missing"
        if state == "verified":
            return "verified" if copied["created"] else "already_present"
        ok = _verify(mv, copied) if state == "copied" else None
        if ok is True:
            mv.log("verified")
        elif ok is False:
            mv.log("verify_failed")
            return "verify_failed"
        else:  # not started, planned only, or a copy that never committed
            outcome = _copy(mv, index, reason)
            if outcome != "copied":
                return outcome
    raise PlanError(f"{mv.key}: no progress from the journal")


def run(plan: Dict[str, Any], reason: str, root: Path) -> Dict[str, str]:
    """Steps 1 and 2 of every planned move from its journal state, under the lock of each journal involved."""
    moves = [_Move(m, plan["plan_id"]) for m in plan["planned_moves"]]
    journals = sorted({mv.jpath for mv in moves})
    with ExitStack() as stack:
        for path in journals:
            stack.enter_context(slot_lock(path))
        for path in journals:
            _journal(path, {"move": "*", "state": "run", "command": "apply", "reason": reason})
        index, out = build_attribution_index(root), {}
        for mv in moves:
            try:
                out[f"{mv.src}#{mv.key}"] = _advance(mv, index, reason)
            except (PlanError, sqlite3.Error, KeyError, TypeError) as exc:  # this move refused, reported, exit 1
                out[f"{mv.src}#{mv.key}"] = f"refused: {exc}"
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Copy and verify the moves of a cross-database pattern plan.")
    apply_p = parser.add_subparsers(dest="command", required=True).add_parser("apply")
    apply_p.add_argument("--plan", type=Path, required=True, help="plan from qi_transfer_plan.py --out")
    apply_p.add_argument("--reason", required=True)
    apply_p.add_argument("--vnx-data-root", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if not args.reason.strip():
            raise PlanError("--reason is empty")
        plan = load_plan(args.plan)
        outcomes = run(plan, args.reason, args.vnx_data_root or (Path.home() / ".vnx-data"))
    except PlanError as exc:
        print(f"[qi_transfer_apply] {exc}", file=sys.stderr)
        return 2
    notes = {k: _SOURCE_KEPT for k, o in outcomes.items() if o in _FINISHED}
    print(json.dumps({"plan_id": plan["plan_id"], "command": "apply", "outcomes": outcomes, "notes": notes}, indent=2))
    return 0 if all(o in _FINISHED for o in outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
