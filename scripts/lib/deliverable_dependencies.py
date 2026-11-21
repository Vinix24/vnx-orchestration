"""deliverable_dependencies.py — order between deliverables (ADR-096 §1-2, track B1).

One row per (project_id, deliverable_id, depends_on_id) in ``deliverable_dependencies``
(migration 0039, ADR-007 key). ``depend`` refuses an unknown id on either side and a
cycle over the active hard and soft rows (a self-dependency is a cycle) and names the
cycle; ``undepend`` keeps the row with ``removed_at`` and ``removed_reason``. Every
depend and undepend writes a ``coordination_events`` row on the same connection.

The caller owns the transaction: ``depend`` and ``undepend`` refuse to run outside one,
so the checks and the write sit under the caller's ``BEGIN IMMEDIATE`` (the same
transaction as the new deliverable when called from ``add``), and the caller commits or
rolls back. ``DependencyRefused`` is the one refusal type; nothing is written when it is
raised before the write. No receipts are read here: whether a predecessor is satisfied
is B2.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

TABLE = "deliverable_dependencies"
EVENTS = "coordination_events"
KINDS = ("hard", "soft")
EVENT_ADDED = "deliverable_dependency_added"
EVENT_RESTORED = "deliverable_dependency_restored"
EVENT_REMOVED = "deliverable_dependency_removed"
_COLS = ("deliverable_id", "depends_on_id", "kind", "created_at", "created_by", "removed_at", "removed_reason")


class DependencyRefused(ValueError):
    """The dependency change or read is refused."""


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _require_table(conn: sqlite3.Connection) -> None:
    if not _columns(conn, TABLE):
        raise DependencyRefused(f"the store has no {TABLE} table: run `vnx migrate` (migration 0039) first")


def _require_write(conn: sqlite3.Connection) -> None:
    _require_table(conn)
    if "project_id" not in _columns(conn, EVENTS):
        raise DependencyRefused(f"the store has no {EVENTS} table with project_id, so the change cannot be recorded")
    if not conn.in_transaction:
        raise DependencyRefused("a dependency change must run inside the caller's BEGIN IMMEDIATE transaction")


def _require_text(**values: str) -> None:
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise DependencyRefused(f"{name} must be a non-empty string, got {value!r}")


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


def _require_deliverable(conn: sqlite3.Connection, project_id: str, dlv_id: str) -> None:
    if not is_deliverable(conn, project_id, dlv_id):
        raise DependencyRefused(f"unknown deliverable {dlv_id!r} in project {project_id!r}")


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
        ).fetchall():
            if nxt == dlv_id:
                return [*path, dlv_id]
            if nxt not in seen:
                seen.add(nxt)
                stack.append([*path, nxt])
    return None


def _event(conn: sqlite3.Connection, project_id: str, event_type: str, dlv_id: str, *, actor: str,
           now: str, reason: Optional[str], metadata: Dict[str, Any]) -> None:
    conn.execute(
        f"INSERT INTO {EVENTS} (event_id, event_type, entity_type, entity_id, from_state, to_state, actor, "
        "reason, metadata_json, occurred_at, project_id) VALUES (?, ?, 'deliverable', ?, NULL, NULL, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex[:16], event_type, dlv_id, actor, reason, json.dumps(metadata), now, project_id),
    )


def depend(conn: sqlite3.Connection, project_id: str, dlv_id: str, on_id: str, *,
           kind: str = "hard", created_by: str, now: str) -> str:
    """Record ``dlv_id`` depends on ``on_id``; ``added``, or ``restored`` for a removed row."""
    _require_write(conn)
    _require_text(project_id=project_id, created_by=created_by, now=now)
    if kind not in KINDS:
        raise DependencyRefused(f"kind {kind!r} is not one of {', '.join(KINDS)}")
    for side in (dlv_id, on_id):
        _require_deliverable(conn, project_id, side)
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
        outcome, event_type = "added", EVENT_ADDED
    else:
        conn.execute(
            f"UPDATE {TABLE} SET kind = ?, created_at = ?, created_by = ?, removed_at = NULL, "
            "removed_reason = NULL WHERE project_id = ? AND deliverable_id = ? AND depends_on_id = ?",
            (kind, now, created_by, project_id, dlv_id, on_id),
        )
        outcome, event_type = "restored", EVENT_RESTORED
    _event(conn, project_id, event_type, dlv_id, actor=created_by, now=now, reason=None,
           metadata={"depends_on_id": on_id, "kind": kind})
    return outcome


def undepend(conn: sqlite3.Connection, project_id: str, dlv_id: str, on_id: str, *,
             reason: str, removed_by: str, now: str) -> None:
    """Mark the active dependency removed; the row stays. An empty reason is refused."""
    _require_write(conn)
    if not isinstance(reason, str) or not reason.strip():
        raise DependencyRefused("--reason is required and may not be empty")
    _require_text(project_id=project_id, removed_by=removed_by, now=now)
    row = conn.execute(
        f"SELECT kind FROM {TABLE} WHERE project_id = ? AND deliverable_id = ? AND depends_on_id = ? "
        "AND removed_at IS NULL", (project_id, dlv_id, on_id),
    ).fetchone()
    if row is None:
        raise DependencyRefused(f"{dlv_id} has no active dependency on {on_id} in project {project_id!r}")
    conn.execute(
        f"UPDATE {TABLE} SET removed_at = ?, removed_reason = ? WHERE project_id = ? AND deliverable_id = ? "
        "AND depends_on_id = ? AND removed_at IS NULL", (now, reason.strip(), project_id, dlv_id, on_id),
    )
    _event(conn, project_id, EVENT_REMOVED, dlv_id, actor=removed_by, now=now, reason=reason.strip(),
           metadata={"depends_on_id": on_id, "kind": row[0]})


def dependencies(conn: sqlite3.Connection, project_id: str, dlv_id: str) -> Dict[str, Any]:
    """Both directions, active and removed rows, for one deliverable of one project."""
    _require_table(conn)
    _require_deliverable(conn, project_id, dlv_id)

    def rows(where: str) -> List[Dict[str, Any]]:
        cur = conn.execute(f"SELECT {', '.join(_COLS)} FROM {TABLE} WHERE project_id = ? AND {where} = ? "
                           "ORDER BY created_at, deliverable_id, depends_on_id", (project_id, dlv_id))
        return [{**dict(zip(_COLS, r)), "active": r[5] is None} for r in cur.fetchall()]

    return {"deliverable": dlv_id, "project_id": project_id,
            "depends_on": rows("deliverable_id"), "depended_on_by": rows("depends_on_id")}
