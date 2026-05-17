"""central_install_db.py — read/write helpers for central-install bookkeeping.

Tables:
- central_install_pins       (project_id, project_root) → pin_version
- central_install_events     append-only event log

Connection pattern mirrors coordination_db.py.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from coordination_db import get_connection_for_db


# ---------------------------------------------------------------------------
# Row dataclasses (lightweight)
# ---------------------------------------------------------------------------

class PinRecord:
    """Represents a row from central_install_pins."""

    def __init__(self, row: sqlite3.Row) -> None:
        self.project_id: str = row["project_id"]
        self.project_root: str = row["project_root"]
        self.pin_version: str = row["pin_version"]
        self.pinned_at: str = row["pinned_at"]
        self.pinned_by: Optional[str] = row["pinned_by"]
        self.pin_source: Optional[str] = row["pin_source"]
        self.notes: Optional[str] = row["notes"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_root": self.project_root,
            "pin_version": self.pin_version,
            "pinned_at": self.pinned_at,
            "pinned_by": self.pinned_by,
            "pin_source": self.pin_source,
            "notes": self.notes,
        }


class InstallEvent:
    """Represents a row from central_install_events."""

    def __init__(self, row: sqlite3.Row) -> None:
        self.id: int = row["id"]
        self.project_id: str = row["project_id"]
        self.event_type: str = row["event_type"]
        self.from_version: Optional[str] = row["from_version"]
        self.to_version: Optional[str] = row["to_version"]
        self.occurred_at: str = row["occurred_at"]
        self.success: bool = bool(row["success"])
        self.error_message: Optional[str] = row["error_message"]
        self.actor: Optional[str] = row["actor"]
        self.details_json: Optional[str] = row["details_json"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "event_type": self.event_type,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "occurred_at": self.occurred_at,
            "success": self.success,
            "error_message": self.error_message,
            "actor": self.actor,
            "details": json.loads(self.details_json) if self.details_json else None,
        }


# ---------------------------------------------------------------------------
# Schema init (idempotent)
# ---------------------------------------------------------------------------

_SCHEMA_SQL_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "schemas"
    / "migrations"
    / "0021_central_install_metadata.sql"
)


def init_central_install_schema(db_path: str | Path) -> None:
    """Create central-install tables if missing. Idempotent."""
    if not _SCHEMA_SQL_PATH.exists():
        raise FileNotFoundError(f"Schema migration not found: {_SCHEMA_SQL_PATH}")
    sql = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    with get_connection_for_db(db_path) as conn:
        conn.executescript(sql)
        conn.commit()


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------

def get_project_pin(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str,
) -> Optional[PinRecord]:
    """Return the active pin for a project+root pair, or None."""
    row = conn.execute(
        "SELECT * FROM central_install_pins WHERE project_id = ? AND project_root = ?",
        (project_id, project_root),
    ).fetchone()
    return PinRecord(row) if row else None


def set_project_pin(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str,
    version: str,
    pinned_by: Optional[str] = None,
    pin_source: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """Upsert a pin row. Composite PK (project_id, project_root) overwrites on conflict."""
    conn.execute(
        """
        INSERT INTO central_install_pins
            (project_id, project_root, pin_version, pinned_by, pin_source, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, project_root) DO UPDATE SET
            pin_version = excluded.pin_version,
            pinned_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            pinned_by = excluded.pinned_by,
            pin_source = excluded.pin_source,
            notes = excluded.notes
        """,
        (project_id, project_root, version, pinned_by, pin_source, notes),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def record_install_event(
    conn: sqlite3.Connection,
    project_id: str,
    event_type: str,
    from_version: Optional[str] = None,
    to_version: Optional[str] = None,
    success: bool = True,
    error_message: Optional[str] = None,
    actor: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> int:
    """Append an install event. Returns the auto-generated row id."""
    cur = conn.execute(
        """
        INSERT INTO central_install_events
            (project_id, event_type, from_version, to_version,
             success, error_message, actor, details_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            event_type,
            from_version,
            to_version,
            1 if success else 0,
            error_message,
            actor,
            json.dumps(details) if details else None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def list_install_history(
    conn: sqlite3.Connection,
    project_id: str,
    limit: int = 10,
) -> List[InstallEvent]:
    """Return recent install events for a project, newest first."""
    rows = conn.execute(
        """
        SELECT * FROM central_install_events
        WHERE project_id = ?
        ORDER BY occurred_at DESC, id DESC
        LIMIT ?
        """,
        (project_id, limit),
    ).fetchall()
    return [InstallEvent(r) for r in rows]
