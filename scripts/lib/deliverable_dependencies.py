"""deliverable_dependencies.py — order between deliverables (ADR-096 §1-2, track B1).

One row per (project_id, deliverable_id, depends_on_id) in ``deliverable_dependencies``
(migration 0039, ADR-007 key). ``depend`` refuses an unknown id on either side and a
cycle over the active hard and soft rows (a self-dependency is a cycle) and names the
cycle; ``undepend`` keeps the row with ``removed_at`` and ``removed_reason``. The
caller owns the transaction: checks and write run on one connection, under the
caller's ``BEGIN IMMEDIATE``, and the caller commits or rolls back. No receipts are
read here: whether a predecessor is satisfied is B2.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

TABLE = "deliverable_dependencies"
KINDS = ("hard", "soft")


class DependencyRefused(ValueError):
    """The dependency change is refused; nothing was written."""


def _require_table(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)).fetchone() is None:
        raise DependencyRefused(f"the store has no {TABLE} table: run `vnx migrate` (migration 0039) first")


def is_deliverable(conn: sqlite3.Connection, project_id: str, dlv_id: str) -> bool:
    row = conn.execute(
        "SELECT metadata_json FROM dispatches WHERE project_id = ? AND dispatch_id = ?", (project_id, dlv_id)
    ).fetchone()
    if row is None:
        return False
    try:
        meta = json.loads(row[0] or "")
    except (TypeError, ValueError):  # a malformed blob is no evidence of a deliverable
        return False
    return isinstance(meta, dict) and meta.get("deliverable") is True


def find_cycle(conn: sqlite3.Connection, project_id: str, dlv_id: str, on_id: str) -> Optional[List[str]]:
    """The cycle ``dlv_id -> on_id -> ... -> dlv_id`` the new edge would close, or None."""
    if dlv_id == on_id:
        return [dlv_id, dlv_id]
    stack, seen = [[dlv_id, on_id]], {on_id}
    while stack:
        path = stack.pop()
        for (nxt,) in conn.execute(
            f"SELECT depends_on_id FROM {TABLE} WHERE project_id = ? AND deliverable_id = ? "
            "AND removed_at IS NULL ORDER BY depends_on_id", (project_id, path[-1]),
        ):
            if nxt == dlv_id:
                return [*path, dlv_id]
            if nxt not in seen:
                seen.add(nxt)
                stack.append([*path, nxt])
    return None


def depend(conn: sqlite3.Connection, project_id: str, dlv_id: str, on_id: str, *,
           kind: str, created_by: str, now: str) -> str:
    """Record ``dlv_id`` depends on ``on_id``; ``added``, or ``restored`` for a removed row."""
    _require_table(conn)
    if kind not in KINDS:
        raise DependencyRefused(f"kind {kind!r} is not one of {', '.join(KINDS)}")
    for side in (dlv_id, on_id):
        if not is_deliverable(conn, project_id, side):
            raise DependencyRefused(f"unknown deliverable {side!r} in project {project_id!r}")
    cycle = find_cycle(conn, project_id, dlv_id, on_id)
    if cycle:
        raise DependencyRefused(f"refused, this would close a cycle: {' -> '.join(cycle)}")
    row = conn.execute(
        f"SELECT removed_at FROM {TABLE} WHERE project_id = ? AND deliverable_id = ? AND depends_on_id = ?",
        (project_id, dlv_id, on_id),
    ).fetchone()
    if row is not None and row[0] is None:
        raise DependencyRefused(f"{dlv_id} already depends on {on_id}")
    if row is None:
        conn.execute(
            f"INSERT INTO {TABLE} (project_id, deliverable_id, depends_on_id, kind, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)", (project_id, dlv_id, on_id, kind, now, created_by),
        )
        return "added"
    conn.execute(
        f"UPDATE {TABLE} SET kind = ?, created_at = ?, created_by = ?, removed_at = NULL, removed_reason = NULL "
        "WHERE project_id = ? AND deliverable_id = ? AND depends_on_id = ?",
        (kind, now, created_by, project_id, dlv_id, on_id),
    )
    return "restored"


def undepend(conn: sqlite3.Connection, project_id: str, dlv_id: str, on_id: str, *, reason: str, now: str) -> None:
    """Mark the active dependency removed; the row stays. An empty reason is refused."""
    _require_table(conn)
    if not (reason or "").strip():
        raise DependencyRefused("--reason is required and may not be empty")
    cur = conn.execute(
        f"UPDATE {TABLE} SET removed_at = ?, removed_reason = ? WHERE project_id = ? AND deliverable_id = ? "
        "AND depends_on_id = ? AND removed_at IS NULL", (now, reason.strip(), project_id, dlv_id, on_id),
    )
    if cur.rowcount != 1:
        raise DependencyRefused(f"{dlv_id} has no active dependency on {on_id} in project {project_id!r}")


def dependencies(conn: sqlite3.Connection, project_id: str, dlv_id: str) -> Dict[str, Any]:
    """Both directions, active and removed rows, for one deliverable of one project."""
    _require_table(conn)
    cols = "deliverable_id, depends_on_id, kind, created_at, created_by, removed_at, removed_reason"

    def rows(where: str) -> List[Dict[str, Any]]:
        cur = conn.execute(f"SELECT {cols} FROM {TABLE} WHERE project_id = ? AND {where} = ? "
                           "ORDER BY created_at, deliverable_id, depends_on_id", (project_id, dlv_id))
        out = [dict(zip(cols.split(", "), r)) for r in cur.fetchall()]
        return [{**r, "active": r["removed_at"] is None} for r in out]

    return {"deliverable": dlv_id, "project_id": project_id,
            "depends_on": rows("deliverable_id"), "depended_on_by": rows("depends_on_id")}
