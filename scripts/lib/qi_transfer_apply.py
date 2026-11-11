#!/usr/bin/env python3
"""qi_transfer_apply.py: execute a cross-database pattern move plan (reattr L3b, split 2 of 3: apply, resume).

ADR-068 besluit 13-17, each step its own ``BEGIN IMMEDIATE``. (1) On the destination, the source opened read-only:
the source row is re-read; ``drift`` (content_key is not the plan's), ``user_version_changed``, ``schema_gap``,
``same_file`` and ``evidence_changed`` (L2 no longer moves it there) change nothing. Else it journals the pre-image,
inserts row and junction rows under the new ``pattern_id`` (or finds them ``already_present``) and journals
``copied`` with ``created`` BEFORE the COMMIT, so a copy that never committed reads back absent and is copied again.
(2) The destination is re-read: ``verified``. (3) Only from ``verified``, and only while the copy still verifies: on
the source, row and junction rows are re-read and deleted only while they equal the journaled pre-image (else
``drift``, nothing deleted): ``source_removed``, then ``done``. Every path checks the ``planned`` and the ``copied``
line field by field before it reads one. A crash leaves the row in both files, never in neither; no atomicity
across files is claimed. ``resume`` runs the same steps from each move's last journal state. Split ``-c`` adds
``undo``.

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
from typing import Any, Dict, List, Optional, Tuple

from atomic_io import atomic_write_text, slot_lock
from pattern_reattribution import STATUS_MOVES, TABLES, _same_file, build_attribution_index, decide_row
from qi_transfer_plan import (
    JOURNAL_STATES, JUNCTION_TABLES, PLAN_VERSION, _columns, _fetch_rows, _json_default, _user_version,
    canonical_json, content_key, journal_path, pattern_id_for, read_pre_image, schema_gaps,
)

_MOVE_KEYS = ("table", "source_id", "source_db", "destination_db", "destination_project_id", "content_key",
              "source_user_version", "destination_user_version")
_FINISHED = ("done", "already_present")
_STEP_3_REFUSALS = ("drift", "destination_missing")
_SOURCE_KEPT = "step 3 deleted nothing: the source keeps the row until it equals the pre-image and the copy verifies"


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
    """The copy ``apply`` published beside a journal of this plan, in a ``<root>/<project>/state/`` dir."""
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
    with open(path, "a", encoding="utf-8") as fh:  # vnx-atomic-write: append-only journal, fsync per line
        fh.write(canonical_json({**line, "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}) + "\n")
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
        _rollback(dst)
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


def _checked_planned(mv: _Move, p: Dict[str, Any]) -> Dict[str, Any]:
    """The ``planned`` line, once its reason and the pre-image step 3 compares are what _copy wrote; else PlanError."""
    pre = p.get("pre_image") if isinstance(p.get("pre_image"), dict) else {}
    row, junction, sid = pre.get("row"), pre.get("junction"), mv.m["source_id"]
    pid = pattern_id_for(mv.table, sid)
    ok = {"reason": isinstance(p.get("reason"), str) and bool(p["reason"].strip()),
          "pre_image.table": pre.get("table") == mv.table,
          "pre_image.row": isinstance(row, dict) and type(row.get("id")) is int and row["id"] == sid  # a bool is no id
          and content_key(row) == mv.m["content_key"],
          "pre_image.junction": isinstance(junction, dict) and set(junction) == set(JUNCTION_TABLES) and all(
              isinstance(rs, list) and all(isinstance(r, dict) and r.get("pattern_id") == pid for r in rs)
              for rs in junction.values()),
          "pre_image.pattern_id": pre.get("pattern_id") == pid}
    if not all(ok.values()):
        raise PlanError(f"{mv.key}: no planned line with a valid {', '.join(f for f, good in ok.items() if not good)}")
    return p


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


def _remove(mv: _Move, pre: Dict[str, Any]) -> str:
    """Step 3, one ``BEGIN IMMEDIATE`` on the source: delete row and junction rows only while they equal ``pre``."""
    src = _connect(mv.src)
    try:
        src.execute("BEGIN IMMEDIATE")
        live = read_pre_image(src, mv.table, mv.m["source_id"])
        if live is None and not any(_fetch_rows(src, t, "pattern_id = ?", pre["pattern_id"]) for t in JUNCTION_TABLES):
            return "source_removed"  # this step committed, then a crash took its journal line
        if live is None or canonical_json(live) != canonical_json(pre):
            return "drift"  # its content (content_key), id or project, or a junction row changed after the copy
        src.execute(f"DELETE FROM {mv.table} WHERE id = ?", (mv.m["source_id"],))
        for t in JUNCTION_TABLES:
            if pre["junction"][t]:  # a table the source lacks has no rows to delete
                src.execute(f"DELETE FROM {t} WHERE pattern_id = ?", (pre["pattern_id"],))
        src.execute("COMMIT")
        return "source_removed"
    finally:
        _rollback(src)
        src.close()


def _advance(mv: _Move, index, reason: str) -> str:
    """Run one move from its last journal state to ``done``; a no-op on a move an earlier run finished."""
    for _ in range(len(JOURNAL_STATES) * 2):
        lines = mv.lines()
        state = (_latest(lines) or {}).get("state")  # None: the move has not started
        # both lines before any field is read; a line that is not there ({}) misses every field
        copied = _checked(mv, _latest(lines, "copied") or {}) if state not in (None, "planned") else None
        pre = _checked_planned(mv, _latest(lines, "planned") or {})["pre_image"] if state else None
        if state == "done":  # read again, write nothing
            return "already_present" if _verify(mv, copied) is True else "destination_missing"
        if state == "source_removed":
            mv.log("done")
            return "done" if copied["created"] else "already_present"
        if state == "verified":  # step 3, only while the copy still stands in the destination
            outcome = _remove(mv, pre) if _verify(mv, copied) is True else "destination_missing"
            mv.log(outcome)
            if outcome in _STEP_3_REFUSALS:
                return outcome
            continue
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


def run(plan: Dict[str, Any], command: str, reason: Optional[str], root: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Every planned move from its journal state, under the lock of each journal involved: outcomes and notes."""
    moves = [_Move(m, plan["plan_id"]) for m in plan["planned_moves"]]
    journals = sorted({mv.jpath for mv in moves})
    with ExitStack() as stack:
        for path in journals:
            stack.enter_context(slot_lock(path))
        # every journal reads before one is written: one torn line refuses the command and nothing changes
        runs = [x for p in journals for x in read_journal(p).get("*", []) if x.get("command") == "apply"]
        if command == "resume":
            reason = runs[-1].get("reason") if runs else None
            if not isinstance(reason, str) or not reason.strip():
                raise PlanError("the journal holds no apply run with a reason to resume")
        for path in journals:  # the copy find_plan reads, published before the journal names this plan
            if not (path.parent / "plan.json").exists():
                text = json.dumps(plan, indent=2, sort_keys=True, default=_json_default) + "\n"
                atomic_write_text(path.parent / "plan.json", text)
            _journal(path, {"move": "*", "state": "run", "command": command, "reason": reason})
        index, out, notes = build_attribution_index(root), {}, {}
        for mv in moves:
            name = f"{mv.src}#{mv.key}"
            try:
                out[name] = _advance(mv, index, reason)
                if out[name] in _STEP_3_REFUSALS and (_latest(mv.lines()) or {}).get("state") == "verified":
                    notes[name] = _SOURCE_KEPT
            except (PlanError, sqlite3.Error, KeyError, TypeError) as exc:  # this move refused, reported, exit 1
                out[name] = f"refused: {exc}"
    return out, notes


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or resume a cross-database pattern move plan.")
    sub = parser.add_subparsers(dest="command", required=True)
    apply_p, resume_p = sub.add_parser("apply"), sub.add_parser("resume")
    apply_p.add_argument("--plan", type=Path, required=True, help="plan from qi_transfer_plan.py --out")
    apply_p.add_argument("--reason", required=True)
    resume_p.add_argument("--plan-id", required=True, help="the plan_id of an earlier apply")
    for p in (apply_p, resume_p):
        p.add_argument("--vnx-data-root", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.vnx_data_root or (Path.home() / ".vnx-data")
    try:
        if args.command == "apply" and not args.reason.strip():
            raise PlanError("--reason is empty")
        plan = load_plan(args.plan) if args.command == "apply" else find_plan(root, args.plan_id)
        outcomes, notes = run(plan, args.command, getattr(args, "reason", None), root)
    except PlanError as exc:
        print(f"[qi_transfer_apply] {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"plan_id": plan["plan_id"], "command": args.command, "outcomes": outcomes, "notes": notes},
                     indent=2))
    return 0 if all(o in _FINISHED for o in outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
