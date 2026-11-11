#!/usr/bin/env python3
"""qi_transfer_apply.py: execute a cross-database pattern move plan (reattr L3b: apply, resume, undo).

ADR-068 besluit 13-17, each step its own ``BEGIN IMMEDIATE``. (1) On the destination, the source opened read-only:
the source row is re-read; ``drift`` (content_key is not the plan's), ``user_version_changed``, ``schema_gap``,
``same_file`` and ``evidence_changed`` (L2 no longer moves it there) change nothing. Else it journals the pre-image,
inserts row and junction rows under the new ``pattern_id`` (or finds them ``already_present``) and journals
``copied`` with ``created`` BEFORE the COMMIT, so a copy that never committed reads back absent and is copied again.
(2) The destination is re-read: ``verified``. (3) Only from ``verified``: the copy verifies under a destination lock
held until the source deletes row and junction rows, only while they equal the journaled pre-image (else ``drift``, a
held lock ``*_locked``; nothing deleted): ``source_removed``, ``done`` once the copy verifies again. Every path checks
the ``planned`` and the ``copied`` line field by field before it reads one. A crash leaves the row in both files,
never in neither; no atomicity across files is claimed. ``resume`` runs the same steps from each move's last journal
state. ``undo`` touches only moves with a ``copied`` line: the source gets the pre-image back first (``restored``),
then the destination loses only what this move created and still holds what it wrote; a changed copy stays
(``undone_kept_changed_copy``), and so does every copy while the source row is not the pre-image by content
(``source_missing``, ``source_not_restored``). Apply and resume never move a move again once an undo touched it.

CLI: qi_transfer_apply.py {apply --plan PLAN.json --reason TEXT | resume --plan-id ID | undo --plan-id ID --reason TEXT}
     [--vnx-data-root DIR]
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
_UNDONE = ("undone", "undone_kept_changed_copy")  # a finished undo: a second one answers from the journal
_UNDO_STATES = ("restored", "source_missing", "source_not_restored") + _UNDONE
_UNDO_OK = ("undone", "not_copied")  # source_missing, source_not_restored, undone_kept_changed_copy: exit 1
_STEP_3_REFUSALS = ("drift", "destination_missing", "destination_locked", "source_locked")
_SOURCE_KEPT = "step 3 deleted nothing: the source keeps the row; the journal line after verified names why"
_SOURCE_GONE = "the source row is removed and the copy does not verify: its content is the journal's planned pre-image"
_NOTES = {"verified": _SOURCE_KEPT, "source_removed": _SOURCE_GONE, "done": _SOURCE_GONE}
_WAIT = 30.0  # seconds a statement waits on another connection's lock, then "database is locked"


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
    conn = sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode={mode}", uri=True, isolation_level=None, timeout=_WAIT)
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


def _verify(mv: _Move, copied: Dict[str, Any], held: Optional[sqlite3.Connection] = None) -> Optional[bool]:
    """Step 2: True when row and every junction row are there by content; None when the copy never committed."""
    cols, dst = copied["cols"], held if held is not None else _connect(mv.dst)  # held: step 3's locked connection
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
        if held is None:
            dst.close()


def _remove(mv: _Move, pre: Dict[str, Any], copied: Dict[str, Any]) -> str:
    """Step 3, locks in step 1's order (destination, then source), held from the copy's check to the source COMMIT."""
    dst, src, locked = _connect(mv.dst), _connect(mv.src), "destination_locked"
    try:
        dst.execute("BEGIN IMMEDIATE")
        if _verify(mv, copied, dst) is not True:
            return "destination_missing"
        locked = "source_locked"
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
    except sqlite3.OperationalError as exc:
        if str(exc) != "database is locked":  # only SQLite's busy answer past _WAIT is a lock; any other error raises
            raise
        return locked  # refused once, not retried; nothing deleted
    finally:
        for conn in (src, dst):  # the destination wrote nothing; its lock ends only after the source COMMIT
            _rollback(conn)
            conn.close()


def _advance(mv: _Move, index, reason: str) -> str:
    """Run one move from its last journal state to ``done``; a no-op on a move an earlier run finished."""
    if any(x["state"] in _UNDO_STATES for x in mv.lines()):
        return "undo_started"  # an undo touched this move: this plan never moves it again
    for _ in range(len(JOURNAL_STATES) * 2):
        lines = mv.lines()
        state = (_latest(lines) or {}).get("state")  # None: the move has not started
        # both lines before any field is read; a line that is not there ({}) misses every field
        copied = _checked(mv, _latest(lines, "copied") or {}) if state not in (None, "planned") else None
        pre = _checked_planned(mv, _latest(lines, "planned") or {})["pre_image"] if state else None
        if state == "done":  # read again, write nothing
            return "already_present" if _verify(mv, copied) is True else "destination_missing"
        if state == "source_removed":  # the copy read again before done, as the done branch does
            if _verify(mv, copied) is not True:
                mv.log("destination_missing")
                return "destination_missing"
            mv.log("done")
            return "done" if copied["created"] else "already_present"
        if state == "verified":  # step 3 checks the copy under the destination lock it holds until the delete commits
            outcome = _remove(mv, pre, copied)
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


def _decode(row: Dict[str, Any], cols: List[str]) -> Dict[str, Any]:
    """A journaled row as SQLite values in columns the table has; the hex object L3a writes for bytes is bytes again."""
    out = {}
    for k, v in row.items():
        hexed = v.get("__bytes_hex__") if isinstance(v, dict) and len(v) == 1 else None
        if isinstance(hexed, str) and re.fullmatch(r"(?:[0-9a-f]{2})*", hexed):
            v = bytes.fromhex(hexed)
        if k not in cols or not (v is None or type(v) in (int, float, str, bytes)):  # a bool is no SQLite value
            raise PlanError(f"journaled {k}={v!r} is no value of a column the source has")
        out[k] = v
    return out


def _put_back(conn: sqlite3.Connection, table: str, row: Dict[str, Any]) -> int:
    """One journaled row into the source: under its old id when that id is free, else under a new one."""
    if "id" in row and conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row["id"],)).fetchone():
        row = {k: v for k, v in row.items() if k != "id"}
    sql = f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})"
    return conn.execute(sql, list(row.values())).lastrowid


def _holds_pre_image(src: sqlite3.Connection, mv: _Move, pre: Dict[str, Any], rid: int) -> bool:
    """True when the source row at ``rid`` exists with the plan's content_key and the pre-image's project_id."""
    rows = _fetch_rows(src, mv.table, "id = ?", rid)  # no row: never the pre-image
    return bool(rows) and (content_key(rows[0]), rows[0]["project_id"]) == (
        mv.m["content_key"], pre["row"]["project_id"])


def _restore(mv: _Move, lines: List[Dict[str, Any]], pre: Dict[str, Any]) -> Optional[int]:
    """Undo step 1, one ``BEGIN IMMEDIATE`` on the source: the id under which the source holds this move's pre-image.

    The pre-image goes back unless an earlier undo's restore committed, the source still holds it, or the source holds
    other content on a row step 3 may not have removed (None: left alone). An id is only accepted with its content: a
    ``restored`` line is journaled before the COMMIT, as ``copied`` is, so its id alone proves nothing."""
    earlier = [x.get("restored_id") for x in lines if x["state"] == "restored"]
    if not all(type(rid) is int for rid in earlier):  # a bool is no id
        raise PlanError(f"{mv.key}: no restored line with a valid restored_id")
    src, sid = _connect(mv.src), mv.m["source_id"]
    try:
        src.execute("BEGIN IMMEDIATE")
        for rid in earlier:  # an id holding other content (or none): that restore never committed
            if _holds_pre_image(src, mv, pre, rid):
                return rid  # that restore committed, then a crash took the rest of the undo
        live = read_pre_image(src, mv.table, sid)
        removed = (_latest(lines) or {}).get("state") in ("source_removed", "done")
        if canonical_json(live) == canonical_json(pre) or (live is not None and not removed):
            # step 3 never deleted it, or the source changed it since, or another row took the id after step 3
            # committed and crashed before its journal line: only the pre-image's content is its own row
            return sid if _holds_pre_image(src, mv, pre, sid) else None
        cols = {t: _columns(src, t) or [] for t in (mv.table,) + JUNCTION_TABLES}
        rid = _put_back(src, mv.table, _decode(pre["row"], cols[mv.table]))
        for t in JUNCTION_TABLES:  # a junction row the source still holds with this content is not put back twice
            held = {content_key(r) for r in _fetch_rows(src, t, "pattern_id = ?", pattern_id_for(mv.table, rid))}
            for j in pre["junction"][t]:
                jrow = _decode({**j, "pattern_id": pattern_id_for(mv.table, rid)}, cols[t])
                if content_key(jrow) not in held:
                    _put_back(src, t, jrow)
        if not _holds_pre_image(src, mv, pre, rid):
            raise PlanError(f"{mv.key}: the row put back is not the journaled pre-image")  # rolled back below
        mv.log("restored", restored_id=rid, pattern_id=pattern_id_for(mv.table, rid))
        src.execute("COMMIT")
        return rid
    finally:
        _rollback(src)
        src.close()


def _drop_copy(mv: _Move, copied: Dict[str, Any], rid: int, pre: Dict[str, Any]) -> str:
    """Undo step 2, locks in step 3's order, the source held until the destination COMMIT: only rows this move created
    that still hold what it wrote go; a copy changed since stays whole. An id is only accepted with its content: while
    the source row at ``rid`` is not the pre-image under the source lock, nothing in the destination goes."""
    dst, src = _connect(mv.dst), _connect(mv.src)
    try:
        dst.execute("BEGIN IMMEDIATE")
        src.execute("BEGIN IMMEDIATE")
        if not _holds_pre_image(src, mv, pre, rid):  # another writer after step 1: the copy stays
            return "source_not_restored" if _fetch_rows(src, mv.table, "id = ?", rid) else "source_missing"
        cols, intact = copied["cols"], _verify(mv, copied, dst)
        own = [(t, _find(dst, t, mv.pid, k, cols[t], pattern_id=copied["pattern_id"]))
               for t, k, created in copied["junction"] if created]
        if intact is False or (intact is None and any(rowid is not None for _, rowid in own)):
            return "undone_kept_changed_copy"  # None with a junction row of its own: the copy's row went since
        for t, rowid in own if intact else ():  # None: the copy never committed, nothing of it to delete
            dst.execute(f"DELETE FROM {t} WHERE rowid = ?", (rowid,))
        if intact and copied["created"]:
            dst.execute(f"DELETE FROM {mv.table} WHERE id = ?", (copied["dest_id"],))
        dst.execute("COMMIT")
        return "undone"
    finally:
        for conn in (src, dst):  # the source wrote nothing; its lock ends only after the destination COMMIT
            _rollback(conn)
            conn.close()


def _undo(mv: _Move) -> str:
    """Put the source back first, then drop only what this move created; a finished undo writes nothing."""
    lines = mv.lines()
    finished = [x["state"] for x in lines if x["state"] in _UNDONE]
    if finished:
        return finished[-1]
    if (_latest(lines) or {}).get("state") in (None, "planned") and _latest(lines, "copied") is None:
        return "not_copied"  # no copy ever committed and step 3 never ran: nothing to put back or drop
    copied = _checked(mv, _latest(lines, "copied") or {})  # both lines before any field is read
    pre = _checked_planned(mv, _latest(lines, "planned") or {})["pre_image"]
    rid = _restore(mv, lines, pre)  # None: the source holds other content on the old id, the copy stays
    outcome = "source_not_restored" if rid is None else _drop_copy(mv, copied, rid, pre)
    mv.log(outcome)  # after the destination COMMIT: a drop that never committed is found and done again
    return outcome


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
        index, out, notes = build_attribution_index(root) if command != "undo" else None, {}, {}
        for mv in moves:
            name = f"{mv.src}#{mv.key}"
            try:
                out[name] = _undo(mv) if command == "undo" else _advance(mv, index, reason)
                if out[name] in _STEP_3_REFUSALS and (state := (_latest(mv.lines()) or {}).get("state")) in _NOTES:
                    notes[name] = _NOTES[state]  # the source kept the row, or only the journal holds it
            except (PlanError, sqlite3.Error, KeyError, TypeError) as exc:  # this move refused, reported, exit 1
                out[name] = f"refused: {exc}"
    return out, notes


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Apply, resume or undo a cross-database pattern move plan.")
    sub = parser.add_subparsers(dest="command", required=True)
    apply_p, resume_p, undo_p = (sub.add_parser(n) for n in ("apply", "resume", "undo"))
    apply_p.add_argument("--plan", type=Path, required=True, help="plan from qi_transfer_plan.py --out")
    for p in (apply_p, undo_p):
        p.add_argument("--reason", required=True)
    for p in (resume_p, undo_p):
        p.add_argument("--plan-id", required=True, help="the plan_id of an earlier apply")
    for p in (apply_p, resume_p, undo_p):
        p.add_argument("--vnx-data-root", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.vnx_data_root or (Path.home() / ".vnx-data")
    try:
        if args.command != "resume" and not args.reason.strip():
            raise PlanError("--reason is empty")
        plan = load_plan(args.plan) if args.command == "apply" else find_plan(root, args.plan_id)
        outcomes, notes = run(plan, args.command, getattr(args, "reason", None), root)
    except PlanError as exc:
        print(f"[qi_transfer_apply] {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"plan_id": plan["plan_id"], "command": args.command, "outcomes": outcomes, "notes": notes},
                     indent=2))
    finished = _UNDO_OK if args.command == "undo" else _FINISHED
    return 0 if all(o in finished for o in outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
