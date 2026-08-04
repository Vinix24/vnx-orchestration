"""dispatches_schema_fixture.py — canonical dispatches schema from schema_manifest (OI-1021).

Two helpers:
  - ``dispatches_ddl()`` — full CREATE TABLE DDL for fresh databases.
  - ``ensure_dispatches_columns(conn)`` — idempotently add any columns declared
    in the canonical schema that are missing from an existing dispatches table.
    Use this in place of hand-written ``ALTER TABLE dispatches ADD COLUMN …``
    statements that must be kept in sync with every schema change.

Before: every new dispatches column (e.g. output_ref/output_kind in v27)
required ALTER TABLE patches in every test fixture that touched the table.
After: adding a column to the schema manifest updates every test automatically.
"""

import sqlite3
import sys
from pathlib import Path

_SCRIPTS_LIB = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from schema_manifest import TERMINAL_VERSION, table_at


def dispatches_ddl() -> str:
    """Generate the canonical dispatches CREATE TABLE DDL from schema_manifest.

    Indexes are omitted — they are performance artifacts the fixtures' queries
    do not depend on.
    """
    tbl = table_at(TERMINAL_VERSION, "dispatches")
    assert tbl is not None, "schema_manifest must declare the dispatches table"
    body: list[str] = []
    for col in tbl.columns.values():
        notnull = " NOT NULL" if col.notnull else ""
        body.append(f"    {col.name} {col.affinity}{notnull}")
    body.append(f"    PRIMARY KEY ({', '.join(tbl.pk)})")
    for uk in tbl.unique_keys:
        body.append(f"    UNIQUE ({', '.join(uk)})")
    return "CREATE TABLE dispatches (\n" + ",\n".join(body) + "\n);"


def ensure_dispatches_columns(conn: sqlite3.Connection) -> list[str]:
    """Idempotently add any columns the canonical schema declares that are
    missing from the dispatches table.

    Returns the list of column names that were added (empty on subsequent calls).
    Use this in place of hand-written ``ALTER TABLE dispatches ADD COLUMN …``
    statements — when a new column lands in the canonical manifest, every test
    that calls this function gets it automatically instead of silently breaking
    (OI-1021).
    """
    tbl = table_at(TERMINAL_VERSION, "dispatches")
    assert tbl is not None, "schema_manifest must declare the dispatches table"
    existing = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}
    added = []
    for col in tbl.columns.values():
        if col.name not in existing:
            conn.execute(f"ALTER TABLE dispatches ADD COLUMN {col.name} {col.affinity}")
            added.append(col.name)
    return added
