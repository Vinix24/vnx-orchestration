#!/usr/bin/env python3
"""qi_transfer_apply.py: execute a cross-database pattern move plan (reattr L3b: apply, resume).

ADR-068 besluit 13-17. Per move: (1) one ``BEGIN IMMEDIATE`` on the destination
re-reads the source row; ``drift`` (its content_key is not the plan's),
``user_version_changed``, ``schema_gap`` and ``evidence_changed`` (L2 no longer
moves it to the same project) change nothing. Otherwise it journals the
pre-image, inserts row and junction rows under the new ``pattern_id`` (or finds
the content ``already_present``) and journals ``copied`` with ``created`` per
row BEFORE the COMMIT, so a copy that never committed reads back absent and is
copied again. (2) The destination is re-read: ``verified``. (3) One ``BEGIN
IMMEDIATE`` on the source deletes row and junction rows only while they equal
the journaled pre-image: ``source_removed``, ``done``. A crash leaves a row in
both files, never in neither. No atomicity across files is claimed.

CLI: qi_transfer_apply.py {apply --plan PLAN.json --reason TEXT | resume --plan-id ID} [--vnx-data-root DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from atomic_io import atomic_write_text, slot_lock
from pattern_reattribution import STATUS_MOVES, TABLES, _same_file, build_attribution_index, decide_row
from qi_transfer_plan import (
    JOURNAL_STATES, JUNCTION_TABLES, PLAN_VERSION, _columns, _fetch_rows, _json_default, _user_version,
    canonical_json, content_key, journal_path, pattern_id_for, read_pre_image, schema_gaps,
)

_MOVE_KEYS = ("table", "source_id", "source_db", "destination_db", "destination_project_id", "content_key",
              "source_user_version", "destination_user_version")


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


def find_plan(root: Path, plan_id: str) -> Dict[str, Any]:
    """The copy ``apply`` published beside a journal of this plan."""
    if not re.fullmatch(r"[0-9a-f]{16}", plan_id):
        raise PlanError(f"not a plan id: {plan_id!r}")
    copies = sorted(Path(root).glob(f"*/state/reattribution/{plan_id}/plan.json"))
    if not copies:
        raise PlanError(f"no journal of plan {plan_id} under {root}")
    plan = load_plan(copies[0])
    if plan["plan_id"] != plan_id:
        raise PlanError(f"{copies[0]} holds plan {plan['plan_id']}, not {plan_id}")
    return plan


def _journal(path: Path, line: Dict[str, Any]) -> None:
    """Append one line and fsync it before the caller takes its next step."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with open(path, "a", encoding="utf-8") as fh:  # vnx-atomic-write: append-only journal, fsync per line
        fh.write(canonical_json({**line, "ts": stamp}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_journal(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Lines per move key; a line that is no JSON object with a move and a state is a PlanError."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines() if path.is_file() else [], 1):
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


def _connect(db: Path) -> sqlite3.Connection:
    """Autocommit and ``mode=rw``: a missing file is an error, never a new empty database."""
    conn = sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


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
        self.key = f"{self.table}:{move['source_id']}"
        self.jpath = journal_path(self.src, plan_id)

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
    dst, src = _connect(mv.dst), _connect(mv.src)
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
        _rollback(dst)
        src.close()
        dst.close()


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
        # created false and none of this move's own junction rows: the transaction rolled back
        uncommitted = copied["created"] is False and not any(hit for fresh, hit in hits if fresh is True)
        return None if uncommitted and all(hit for fresh, hit in hits if fresh is False) else False
    finally:
        dst.close()


def _remove(mv: _Move, pre: Dict[str, Any]) -> str:
    """Step 3, one ``BEGIN IMMEDIATE`` on the source: delete only what equals the journaled pre-image."""
    src = _connect(mv.src)
    try:
        src.execute("BEGIN IMMEDIATE")
        live = read_pre_image(src, mv.table, mv.m["source_id"])
        if live is None and not any(_fetch_rows(src, t, "pattern_id = ?", pre["pattern_id"]) for t in JUNCTION_TABLES):
            return "source_removed"  # this step committed, then a crash took its journal line
        if live is None or canonical_json(live) != canonical_json(pre):
            return "drift"
        src.execute(f"DELETE FROM {mv.table} WHERE id = ?", (mv.m["source_id"],))
        for t in JUNCTION_TABLES:
            if pre["junction"][t]:
                src.execute(f"DELETE FROM {t} WHERE pattern_id = ?", (pre["pattern_id"],))
        src.execute("COMMIT")
        return "source_removed"
    finally:
        _rollback(src)
        src.close()


def _advance(mv: _Move, index, reason: str) -> str:
    """Run one move from its last journal state to ``done``."""
    for _ in range(len(JOURNAL_STATES) * 2):
        lines = mv.lines()
        state = (_latest(lines) or {}).get("state")  # None: the move has not started
        if state == "done":
            return "already_present" if _verify(mv, _latest(lines, "copied")) else "destination_missing"
        if state == "source_removed":
            created = (_latest(lines, "copied") or {}).get("created")
            if not isinstance(created, bool):  # missing or malformed: refused, never read as either
                raise PlanError(f"{mv.key}: the copied line has no boolean created")
            mv.log("done")
            return "done" if created else "already_present"
        if state == "verified":
            outcome = _remove(mv, _latest(lines, "planned")["pre_image"])
            mv.log(outcome)
            if outcome == "drift":
                return outcome
            continue
        ok = _verify(mv, _latest(lines, "copied")) if state == "copied" else None
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


def run(plan: Dict[str, Any], command: str, reason: Optional[str], root: Path) -> Dict[str, str]:
    """Every planned move from its journal state, under the lock of each journal involved."""
    moves = [_Move(m, plan["plan_id"]) for m in plan["planned_moves"]]
    journals = sorted({mv.jpath for mv in moves})
    with ExitStack() as stack:
        for path in journals:
            stack.enter_context(slot_lock(path))
            if command == "apply" and not (path.parent / "plan.json").exists():
                text = json.dumps(plan, indent=2, sort_keys=True, default=_json_default) + "\n"
                atomic_write_text(path.parent / "plan.json", text)
        if command == "resume":
            runs = [x for p in journals for x in read_journal(p).get("*", []) if x.get("command") == "apply"]
            reason = runs[-1].get("reason") if runs else None
            if not isinstance(reason, str) or not reason.strip():
                raise PlanError("the journal holds no apply run with a reason to resume")
        for path in journals:
            _journal(path, {"move": "*", "state": "run", "command": command, "reason": reason})
        index, out = build_attribution_index(root), {}
        for mv in moves:
            try:
                out[f"{mv.src}#{mv.key}"] = _advance(mv, index, reason)
            except (PlanError, sqlite3.Error, KeyError, TypeError) as exc:  # this move refused, reported, exit 1
                out[f"{mv.src}#{mv.key}"] = f"refused: {exc}"
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or resume a cross-database pattern move plan.")
    sub = parser.add_subparsers(dest="command", required=True)
    apply_p, resume_p = sub.add_parser("apply"), sub.add_parser("resume")
    apply_p.add_argument("--plan", type=Path, required=True, help="plan from qi_transfer_plan.py --out")
    apply_p.add_argument("--reason", required=True)
    resume_p.add_argument("--plan-id", required=True)
    for p in (apply_p, resume_p):
        p.add_argument("--vnx-data-root", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.vnx_data_root or (Path.home() / ".vnx-data")
    try:
        if args.command == "apply" and not args.reason.strip():
            raise PlanError("--reason is empty")
        plan = load_plan(args.plan) if args.command == "apply" else find_plan(root, args.plan_id)
        outcomes = run(plan, args.command, getattr(args, "reason", None), root)
    except PlanError as exc:
        print(f"[qi_transfer_apply] {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"plan_id": plan["plan_id"], "command": args.command, "outcomes": outcomes}, indent=2))
    return 0 if all(o in ("done", "already_present") for o in outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
