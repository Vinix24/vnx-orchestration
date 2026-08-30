#!/usr/bin/env python3
"""qi_db_health.py — shared table-count classification for quality_intelligence.db.

Single source of truth for "is this SQLite file an actually-bootstrapped
quality_intelligence.db, or an empty decoy (0 tables)?" Used by vnx_doctor's
install-health check (``vnx_doctor.py::check_database``) and by the state
builder's query layer (``build_t0_state.py::_query_qi_db``) so both sides
count tables the same way instead of drifting into two counters that can
disagree (OI: absence-is-loud D5).

``sqlite3.connect(path)`` lazily creates a 0-byte, 0-table file at ``path``
the moment it is called, even if nothing is ever written or committed. A
0-table file and a genuinely empty result set look identical to a naive
``SELECT ... WHERE ...`` (both return zero rows once "no such table" is
swallowed) — but they mean opposite things: zero rows is "nothing here
yet", zero tables is "this is not the database you were looking for."
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

# Below this table count, a quality_intelligence.db is not a real bootstrap:
# either a decoy left behind by an interrupted create-then-populate sequence
# (0 tables), or a schema so partial it predates the current base schema.
# Mirrors the threshold vnx_doctor.py has enforced since the install-health
# check shipped.
MIN_HEALTHY_TABLE_COUNT = 10


def count_tables(db_path: Path) -> Optional[int]:
    """Return the table count in ``db_path``, or None if missing/unreadable.

    None is a distinct outcome from 0: it means the file does not exist (or
    could not be opened as SQLite at all), not that it exists with an empty
    schema.
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def is_empty_schema(db_path: Path) -> bool:
    """True when ``db_path`` exists but has zero tables — a decoy, not "no data yet".

    False both when the file is missing (that's "no data yet", the normal
    not-bootstrapped-here case readers already handle) and when it has
    tables (healthy or under-versioned, but a real database).
    """
    return count_tables(db_path) == 0
