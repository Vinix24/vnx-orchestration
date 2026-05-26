#!/usr/bin/env python3
"""Schema-rebuild helpers extracted from migrate_to_central_vnx.

Contains:
  - BootstrapFailure / MigrationOrphanError exception classes
  - _rebuild_one_table_dynamic (OI-1533): composite-UNIQUE rebuild via introspection
  - _rebuild_fts5_code_snippets  (OI-1536): FTS5 schema-first rebuild

Part 2/3 of the migrate_to_central_vnx split (OI-1536 / OI-1533).
Do NOT import from scripts.migrate_to_central_vnx here — this module must
remain standalone so the parent can import from it without circular dependency.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.migrate_import import _table_exists  # noqa: E402


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


class BootstrapFailure(RuntimeError):
    """Raised when the central DB is missing canonical structure required for import.

    Round-3 fix-forward (Issue 4): rather than letting a per-row INSERT
    OR IGNORE silently drop every row when a central table is absent,
    pre-flight assert that every import-target table exists. If not,
    surface the missing tables in the exception message so the operator
    can diagnose the broken bootstrap before any data is moved.
    """


class MigrationOrphanError(RuntimeError):
    """Raised when FTS5 rebuild finds code_snippets rows with no recoverable project_id.

    Indicates orphan rows: no snippet_metadata match and no p4_import_rowid_map
    entry exists for the snippet. The operator must either fix the source data
    (ensure snippet_metadata covers all snippets) or re-run the full migration
    so that p4_import_rowid_map is populated before migration 0016 fires.
    OI-1376 / ADR-009.
    """


# ---------------------------------------------------------------------------
# OI-1533: composite-UNIQUE rebuild via schema introspection
# ---------------------------------------------------------------------------


def _rebuild_one_table_dynamic(
    con: sqlite3.Connection,
    table: str,
    key_col: str,
) -> None:
    """Round-6: schema-introspection rebuild for composite UNIQUE.

    Reads the live table schema via ``PRAGMA table_info`` plus
    ``sqlite_master.sql`` and reconstructs an equivalent ``CREATE TABLE``
    statement that:

    * preserves every column (name, type, NOT NULL, DEFAULT) in order;
    * preserves the integer-PK ``AUTOINCREMENT`` modifier when the
      original schema had it;
    * **drops** the column-level ``UNIQUE`` modifier on ``key_col``
      (effectively, by not re-emitting it) — the dynamic builder never
      writes a column-level UNIQUE, so the only UNIQUE the rebuilt table
      carries is the composite one we add;
    * adds a table-level ``UNIQUE(project_id, key_col)`` constraint.

    All non-UNIQUE indexes are captured before ``DROP TABLE`` and
    re-created from their original SQL after rename. UNIQUE auto-indexes
    backing the dropped column-level UNIQUE go away with the old table
    and are not recreated (the composite UNIQUE provides the new index).

    The rebuild runs inside the caller's transaction frame, so failure
    raises and rolls back the entire composite-unique pass.
    """
    cols_info = list(con.execute(f"PRAGMA table_info({table})"))
    if not cols_info:
        raise BootstrapFailure(
            f"dynamic rebuild: {table} has no columns (schema empty?)"
        )

    # Validate that key_col actually exists; otherwise the composite UNIQUE
    # we'd produce would reference a phantom column.
    col_names = [c[1] for c in cols_info]
    if key_col not in col_names:
        raise BootstrapFailure(
            f"dynamic rebuild: {table} has no column {key_col!r}; "
            "COMPOSITE_UNIQUE_TABLES_QI/RC entry must match the live schema."
        )

    # Pull the original CREATE TABLE so we can detect AUTOINCREMENT, which
    # PRAGMA table_info does not surface directly.
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or not row[0]:
        raise BootstrapFailure(
            f"dynamic rebuild: sqlite_master has no CREATE TABLE for {table}"
        )
    original_sql = row[0]

    # Capture non-auto indexes BEFORE drop. ``sql`` is NULL for SQLite-
    # auto-generated indexes (UNIQUE backings, internal sqlite_autoindex_*),
    # which we explicitly do NOT want to recreate.
    saved_index_sql = [
        sql for (sql,) in con.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name = ? AND sql IS NOT NULL",
            (table,),
        )
    ]

    # Build column definitions from PRAGMA introspection. Columns are
    # emitted in cid order (matches original definition order).
    col_defs: list[str] = []
    for cid, name, ctype, notnull, dflt, pk in cols_info:
        parts = [name, (ctype or "TEXT")]
        if pk:
            # SQLite reports the PK status per-column; only one column
            # can carry an integer ROWID PK, so we emit PRIMARY KEY here
            # rather than as a table constraint.
            parts.append("PRIMARY KEY")
            if (ctype or "").upper() == "INTEGER":
                # AUTOINCREMENT only valid on INTEGER PRIMARY KEY. Detect
                # via a regex on the original CREATE TABLE — normalize
                # whitespace so multi-space definitions still match.
                normalized = re.sub(r"\s+", " ", original_sql).upper()
                pat = (
                    rf"\b{re.escape(name.upper())}\s+INTEGER\s+PRIMARY\s+KEY"
                    r"\s+AUTOINCREMENT\b"
                )
                if re.search(pat, normalized):
                    parts.append("AUTOINCREMENT")
        if notnull and not pk:
            parts.append("NOT NULL")
        if dflt is not None:
            # PRAGMA returns dflt as the literal SQL fragment used in the
            # original DEFAULT clause (e.g. ``'vnx-dev'``, ``0``,
            # ``CURRENT_TIMESTAMP``, ``(strftime('%Y-...', 'now'))``).
            # Re-emit as-is.
            parts.append(f"DEFAULT {dflt}")
        col_defs.append(" ".join(parts))

    # Composite UNIQUE replaces the dropped single-column UNIQUE.
    col_defs.append(f"UNIQUE (project_id, {key_col})")

    # Build the rebuild SQL. ``<table>_p4r6_new`` is a transient name
    # scoped to the rebuild transaction; renamed before any other code
    # observes the database.
    new_table = f"{table}_p4r6_new"
    create_new = (
        f"CREATE TABLE {new_table} (\n  " + ",\n  ".join(col_defs) + "\n)"
    )
    con.execute(create_new)

    quoted_cols = ", ".join(col_names)
    con.execute(
        f"INSERT INTO {new_table} ({quoted_cols}) "
        f"SELECT {quoted_cols} FROM {table}"
    )
    con.execute(f"DROP TABLE {table}")
    con.execute(f"ALTER TABLE {new_table} RENAME TO {table}")

    # Recreate user-defined indexes captured pre-drop. ``IF NOT EXISTS``
    # guards on those statements would have been included in their
    # original SQL when present.
    for idx_sql in saved_index_sql:
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute(idx_sql)


# ---------------------------------------------------------------------------
# OI-1536: FTS5 schema-first rebuild
# ---------------------------------------------------------------------------


def _rebuild_fts5_code_snippets(con: sqlite3.Connection) -> None:
    """Schema-first FTS5 rebuild for code_snippets per ADR-009.

    Derives column list from ``PRAGMA table_info`` at apply time so that
    deployed DBs with extra columns beyond the canonical 12 are preserved
    (OI-1375).  Attributes each row's ``project_id`` via ``snippet_metadata``
    first, then ``p4_import_rowid_map`` as fallback; rows with no recoverable
    project_id raise ``MigrationOrphanError`` BEFORE the DROP so the database
    is left intact (OI-1376).

    Must be called inside an active transaction.  Caller is responsible for
    the ``BEGIN`` / ``ROLLBACK`` / ``COMMIT`` frame.
    """
    # --- schema-first column discovery (ADR-009) ----------------------------
    cols_info = list(con.execute("PRAGMA table_info(code_snippets)"))
    if not cols_info:
        raise BootstrapFailure(
            "code_snippets has no columns in PRAGMA table_info — schema empty?"
        )
    current_cols = [row[1] for row in cols_info]

    # Preserve the original FTS5 tokenize options so the rebuilt table is
    # semantically identical except for the added project_id column.
    fts5_row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='code_snippets'"
    ).fetchone()
    tokenize_clause = "tokenize = 'porter unicode61'"
    if fts5_row and fts5_row[0]:
        m = re.search(r"tokenize\s*=\s*'[^']*'", fts5_row[0], re.IGNORECASE)
        if m:
            tokenize_clause = m.group(0)

    # --- materialize existing rows into a plain temp table ------------------
    # Using a regular (non-virtual) table preserves rowid values which are
    # required both for the project_id correlated lookups below and for
    # FTS5 rowid-preservation on re-insert.
    quoted_cols = ", ".join(f'"{c}"' for c in current_cols)
    con.execute(
        f"CREATE TABLE IF NOT EXISTS code_snippets_rebuild_tmp AS "
        f"SELECT rowid, {quoted_cols} FROM code_snippets"
    )

    # --- orphan check BEFORE DROP (fail fast, leave DB intact) --------------
    has_rowid_map = _table_exists(con, "p4_import_rowid_map")
    if has_rowid_map:
        orphan_sql = (
            "SELECT t.rowid FROM code_snippets_rebuild_tmp t "
            "WHERE (SELECT m.project_id FROM snippet_metadata m "
            "       WHERE m.snippet_rowid = t.rowid) IS NULL "
            "AND NOT EXISTS ("
            "    SELECT 1 FROM p4_import_rowid_map r "
            "    WHERE r.source_table = 'code_snippets' AND r.central_rowid = t.rowid"
            ")"
        )
    else:
        orphan_sql = (
            "SELECT t.rowid FROM code_snippets_rebuild_tmp t "
            "WHERE (SELECT m.project_id FROM snippet_metadata m "
            "       WHERE m.snippet_rowid = t.rowid) IS NULL"
        )
    orphan_rowids = [r[0] for r in con.execute(orphan_sql)]
    if orphan_rowids:
        # Drop the temp table we just created before raising so the caller's
        # ROLLBACK has nothing extra to undo.
        con.execute("DROP TABLE IF EXISTS code_snippets_rebuild_tmp")
        raise MigrationOrphanError(
            f"FTS5 rebuild: {len(orphan_rowids)} code_snippets row(s) have no "
            f"recoverable project_id (no snippet_metadata match and no "
            f"p4_import_rowid_map entry). "
            f"Orphan rowids: {orphan_rowids[:20]}"
            + ("..." if len(orphan_rowids) > 20 else "")
            + ". Fix source data or re-run migration to regenerate rowid map."
        )

    # --- drop FTS5, recreate with dynamic column list + project_id ----------
    con.execute("DROP TABLE IF EXISTS code_snippets")
    new_col_list = ", ".join(current_cols) + f", project_id, {tokenize_clause}"
    con.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS code_snippets USING fts5({new_col_list})"
    )

    # --- re-populate with SQL-level project_id attribution ------------------
    # 1st choice: snippet_metadata.project_id via snippet_rowid → rowid join
    #             (canonical cross-reference populated by import step)
    # 2nd choice: p4_import_rowid_map.project_id via central_rowid lookup
    #             (records which project imported each snippet — covers orphan
    #             snippets that exist in the DB but have no metadata row)
    if has_rowid_map:
        pid_expr = (
            "COALESCE("
            "(SELECT m.project_id FROM snippet_metadata m WHERE m.snippet_rowid = t.rowid), "
            "(SELECT r.project_id FROM p4_import_rowid_map r "
            " WHERE r.source_table = 'code_snippets' AND r.central_rowid = t.rowid)"
            ")"
        )
    else:
        pid_expr = (
            "(SELECT m.project_id FROM snippet_metadata m WHERE m.snippet_rowid = t.rowid)"
        )

    con.execute(
        f"INSERT INTO code_snippets (rowid, {quoted_cols}, project_id) "
        f"SELECT t.rowid, {quoted_cols}, {pid_expr} "
        f"FROM code_snippets_rebuild_tmp t"
    )
    con.execute("DROP TABLE IF EXISTS code_snippets_rebuild_tmp")
