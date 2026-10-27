"""live_store_fixture.py — a tracks store in the shape of the live stores (ADR-049).

``build_tracks_db`` walks 0022, which gives ``dispatches`` a ``CHECK (state IN
(...))``. None of the six live stores carries that CHECK (measured read-only
2026-10-27), so this rebuilds the table without it and puts the deliverables
view of the terminal manifest version on top, stamped at that version: the
store `vnx deliverable withdraw` runs on in production.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import schema_manifest

from fixtures.closure_track_fixture import build_tracks_db

_STATE_CHECK = re.compile(r"\s*CHECK\s*\(\s*state\s+IN\s*\([^)]*\)\s*\)", re.IGNORECASE)


def drop_state_check(db_path: Path) -> None:
    """Rebuild dispatches without the 0022 state CHECK; rows and ids stay."""
    conn = sqlite3.connect(str(db_path))
    try:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'dispatches'").fetchone()[0]
        live = _STATE_CHECK.sub("", sql).replace("CREATE TABLE dispatches", "CREATE TABLE dispatches_live", 1)
        conn.execute("DROP VIEW IF EXISTS deliverables")
        conn.execute(live)
        conn.execute("INSERT INTO dispatches_live SELECT * FROM dispatches")
        conn.execute("DROP TABLE dispatches")
        conn.execute("ALTER TABLE dispatches_live RENAME TO dispatches")
        conn.commit()
    finally:
        conn.close()


def terminal_deliverables_view(db_path: Path) -> None:
    """The deliverables view as the terminal manifest version defines it, stamped at that version."""
    version = schema_manifest.TERMINAL_VERSION
    [view] = [v for v in schema_manifest.SCHEMA_MANIFEST[version].views if v.name == "deliverables"]
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP VIEW IF EXISTS deliverables")
        conn.execute(view.sql)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def build_live_store(state_dir: Path) -> Path:
    """``build_tracks_db`` without the state CHECK, with the terminal deliverables view."""
    build_tracks_db(state_dir)
    db_path = state_dir / "runtime_coordination.db"
    drop_state_check(db_path)
    terminal_deliverables_view(db_path)
    return db_path
