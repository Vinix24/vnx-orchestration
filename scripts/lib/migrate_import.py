#!/usr/bin/env python3
"""Per-table import engine extracted from migrate_to_central_vnx.

Contains:
  - _import_table  (OI-1537): streaming, idempotent, per-row collision-prefix
  - _compare_counts (OI-1539): verification row-count comparison
  - Direct helpers: _collect_skipped_rows, _record_*, _prefix_*, SQLite utils

Part 1/3 of the migrate_to_central_vnx split (OI-1537 / OI-1539).
Do NOT import from scripts.migrate_to_central_vnx here — this module must
remain standalone so the parent can import from it without circular dependency.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.aggregator.build_central_view import (  # noqa: E402
    ProjectEntry,
    attach_readonly,
)

LOG = logging.getLogger("vnx.migrate.apply")

# ---------------------------------------------------------------------------
# Abort-flag support
# ---------------------------------------------------------------------------

ABORT_FLAG = Path.home() / ".vnx-aggregator" / "ABORT"


class AbortRequested(RuntimeError):
    pass


def check_abort() -> None:
    if ABORT_FLAG.exists():
        raise AbortRequested(f"abort flag present: {ABORT_FLAG}")


# ---------------------------------------------------------------------------
# Collision-prefix constants
# ---------------------------------------------------------------------------

# Streaming batch size for _import_table — bounds Python memory during import
# of large FTS5 tables (code_snippets: 855k rows). OI-1377 / ADR-009.
_IMPORT_BATCH_SIZE = 500

# Schema-driven collision-prefixing candidates. Any imported table that carries
# one of these exact column names gets its value rewritten to
# ``<project_id>:<original>`` so per-project identifiers remain unique after
# consolidation.
#
# Exact-name collision prefix columns. Suffix matches are collected separately
# in ``_collect_collision_columns`` below.
COLLISION_PREFIX_COLUMNS: tuple[str, ...] = ("dispatch_id", "pattern_id")

# Suffixes used for schema-driven collision detection. Any column name that
# ends in one of these suffixes (after the leading underscore) is treated as
# a per-project identifier carrier and gets the ``<project_id>:`` prefix
# rewritten on import. Examples: ``related_dispatch_id``, ``parent_dispatch_id``,
# ``parent_pattern_id``.
COLLISION_PREFIX_SUFFIXES: tuple[str, ...] = ("_dispatch_id", "_pattern_id")

# Columns that store JSON arrays of dispatch/pattern IDs. Each element in the
# array is rewritten to ``<project_id>:<element>`` on import so cross-tenant
# references stay disjoint after consolidation. (Finding 2 round 2.)
COLLISION_JSON_ARRAY_COLUMNS: tuple[str, ...] = ("source_dispatch_ids",)

# Special-case: ``coordination_events.entity_id`` stores either a dispatch_id
# or a pattern_id depending on ``entity_type``. We rewrite the value only when
# the entity_type matches one of these prefix-eligible types. (Finding 2 round 2.)
COLLISION_ENTITY_TABLE = "coordination_events"
COLLISION_ENTITY_ID_COLUMN = "entity_id"
COLLISION_ENTITY_TYPE_COLUMN = "entity_type"
COLLISION_ENTITY_TYPES_PREFIXED: frozenset[str] = frozenset({"dispatch", "pattern"})

# Free-text identifier columns that historically held a dispatch id but whose
# name does not end in ``_dispatch_id``. Listed explicitly so future schemas
# add to this set deliberately rather than accidentally inheriting the suffix
# rule. (Finding 2 round 2.)
COLLISION_NAMED_IDENTIFIER_COLUMNS: tuple[str, ...] = ("parent_dispatch",)


# ---------------------------------------------------------------------------
# SQLite schema-introspection utilities
# ---------------------------------------------------------------------------


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    cur = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','virtual') AND name = ?",
        (table,),
    )
    return cur.fetchone() is not None


def _table_columns(
    con: sqlite3.Connection,
    table: str,
    alias: Optional[str] = None,
) -> list[str]:
    pragma = f"PRAGMA {alias}.table_info({table})" if alias else f"PRAGMA table_info({table})"
    return [r[1] for r in con.execute(pragma)]


def _column_exists(
    con: sqlite3.Connection,
    table: str,
    column: str,
    alias: Optional[str] = None,
) -> bool:
    return column in _table_columns(con, table, alias=alias)


def _integer_primary_key(con: sqlite3.Connection, table: str, alias: Optional[str] = None) -> Optional[str]:
    """Return the column name that is INTEGER PRIMARY KEY (autoincrement rowid alias).

    Such columns cannot be ported across project DBs because each source DB
    starts numbering at 1 and would collide on import. Returning the name
    lets the caller exclude it from the INSERT column list.
    """
    pragma = f"PRAGMA {alias}.table_info({table})" if alias else f"PRAGMA table_info({table})"
    for cid, name, ctype, _notnull, _dflt, pk in con.execute(pragma):
        if pk and (ctype or "").upper() == "INTEGER":
            return name
    return None


def _common_columns(con: sqlite3.Connection, source_alias: str, table: str) -> list[str]:
    """Return columns present in BOTH source and central tables (intersection).

    Excludes the source's INTEGER PRIMARY KEY column so SQLite re-assigns
    autoincrement ids in the central DB; otherwise project-local primary
    keys (1, 2, 3, ...) collide with the first-imported project's rows.
    """
    if not _table_exists(con, table):
        return []
    central_cols = _table_columns(con, table)
    src_cols = _table_columns(con, table, alias=source_alias)
    src_int_pk = _integer_primary_key(con, table, alias=source_alias)
    central_int_pk = _integer_primary_key(con, table)
    skip = {c for c in (src_int_pk, central_int_pk) if c}
    return [c for c in src_cols if c in central_cols and c not in skip]


# ---------------------------------------------------------------------------
# Import data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportSummary:
    project_id: str
    db_name: str
    table: str
    rows_inserted: int
    rows_skipped_existing: int


# ---------------------------------------------------------------------------
# Time utility
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Collision-prefix helpers
# ---------------------------------------------------------------------------


def _is_collision_column(name: str) -> bool:
    """True if a column name participates in cross-project key prefixing.

    Centralized so call sites in both the live migrator and dry-run
    detector share identical rules. Covers exact matches, the
    ``_dispatch_id`` / ``_pattern_id`` suffix family, and the explicitly
    enumerated free-text identifier columns. (Finding 2 round 2.)
    """
    if name in COLLISION_PREFIX_COLUMNS:
        return True
    if name in COLLISION_NAMED_IDENTIFIER_COLUMNS:
        return True
    for suffix in COLLISION_PREFIX_SUFFIXES:
        if name != suffix and name.endswith(suffix):
            return True
    return False


def _collect_collision_columns(
    con: sqlite3.Connection,
    source_alias: str,
    table: str,
) -> tuple[str, ...]:
    """Return per-table column names whose values must be project-prefixed.

    Schema-driven so any column matching the prefix-eligible name rules
    (see :func:`_is_collision_column`) is included automatically — no
    manual edits needed when a new table grows a ``related_dispatch_id``
    style reference. (Finding 2 round 2.)
    """
    if not _table_exists(con, table):
        return ()
    central_cols = _table_columns(con, table)
    source_cols = set(_table_columns(con, table, alias=source_alias))
    return tuple(
        column
        for column in central_cols
        if column in source_cols and _is_collision_column(column)
    )


def _collect_json_array_columns(
    con: sqlite3.Connection,
    source_alias: str,
    table: str,
) -> tuple[str, ...]:
    """Columns in ``table`` known to hold JSON arrays of identifiers.

    Used by the live migrator to rewrite each array element with the
    project prefix. (Finding 2 round 2.)
    """
    if not _table_exists(con, table):
        return ()
    central_cols = set(_table_columns(con, table))
    source_cols = set(_table_columns(con, table, alias=source_alias))
    return tuple(
        column
        for column in COLLISION_JSON_ARRAY_COLUMNS
        if column in central_cols and column in source_cols
    )


def _prefix_value(project_id: str, value: object) -> object:
    """Apply ``<project_id>:`` prefix to a scalar identifier value.

    Idempotent: a value that already starts with the project's prefix
    is returned unchanged so repeat-runs do not double-prefix.
    """
    if value is None or value == "":
        return value
    prefix = f"{project_id}:"
    text_value = str(value)
    return text_value if text_value.startswith(prefix) else f"{prefix}{text_value}"


def _prefix_json_array(project_id: str, value: object) -> object:
    """Apply project prefix to each element in a JSON-array string.

    If the value is missing, empty, or fails to parse as a JSON array,
    it is returned unchanged — defensive because legacy DBs sometimes
    stored unstructured strings in these columns.
    """
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value
    if not isinstance(parsed, list):
        return value
    rewritten = [_prefix_value(project_id, item) for item in parsed]
    return json.dumps(rewritten)


# ---------------------------------------------------------------------------
# Rowid-map and skip-tracking helpers
# ---------------------------------------------------------------------------


def _record_rowid_mapping(
    con: sqlite3.Connection,
    project_id: str,
    source_table: str,
    source_rowid: int,
    central_rowid: int,
) -> None:
    con.execute(
        """
        INSERT INTO p4_import_rowid_map
            (project_id, source_table, source_rowid, central_rowid)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(project_id, source_table, source_rowid)
        DO UPDATE SET central_rowid = excluded.central_rowid
        """,
        (project_id, source_table, source_rowid, central_rowid),
    )


def _mapped_central_rowid(
    con: sqlite3.Connection,
    project_id: str,
    source_table: str,
    source_rowid: int,
) -> Optional[int]:
    row = con.execute(
        """
        SELECT central_rowid
        FROM p4_import_rowid_map
        WHERE project_id = ? AND source_table = ? AND source_rowid = ?
        """,
        (project_id, source_table, source_rowid),
    ).fetchone()
    return int(row[0]) if row else None


def _resolve_prior_skip(
    con: sqlite3.Connection,
    project_id: str,
    source_table: str,
    source_rowid: int,
) -> None:
    """Mark any prior unresolved p4_import_skipped row as resolved.

    Called when a previously-skipped row finally imports successfully on
    a later run. ``verify_import`` only treats unresolved skips as
    discrepancies, so flipping ``resolved_at`` lets idempotent re-runs
    self-heal without operator intervention. (Finding 3 round 2.)
    """
    con.execute(
        """
        UPDATE p4_import_skipped
           SET resolved_at = ?
         WHERE project_id = ?
           AND source_table = ?
           AND source_rowid = ?
           AND resolved_at IS NULL
        """,
        (_now_utc_iso(), project_id, source_table, source_rowid),
    )


def _record_skip(
    con: sqlite3.Connection,
    project_id: str,
    source_table: str,
    source_rowid: int,
    reason: str,
    run_id: Optional[str],
) -> None:
    """Audit a row the migrator could not import (conflict / integrity).

    The PRIMARY KEY is ``(project_id, source_table, source_rowid)`` so
    this is naturally one-row-per-source-rowid; we re-stamp ``run_id`` /
    ``skipped_at`` on each occurrence and clear ``resolved_at`` so a row
    that re-skips after being marked resolved on a previous run shows up
    as an active discrepancy again. (Finding 3 round 2.)
    """
    con.execute(
        """
        INSERT INTO p4_import_skipped
            (project_id, source_table, source_rowid, reason, skipped_at, run_id, resolved_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(project_id, source_table, source_rowid)
        DO UPDATE SET
            reason       = excluded.reason,
            skipped_at   = excluded.skipped_at,
            run_id       = excluded.run_id,
            resolved_at  = NULL
        """,
        (
            project_id,
            source_table,
            source_rowid,
            reason,
            _now_utc_iso(),
            run_id,
        ),
    )


# ---------------------------------------------------------------------------
# Core import function (OI-1537)
# ---------------------------------------------------------------------------


def _import_table(
    con: sqlite3.Connection,
    source_alias: str,
    project: ProjectEntry,
    table: str,
    run_id: Optional[str] = None,
) -> ImportSummary:
    source_cols = _common_columns(con, source_alias, table)
    central_has_project_id = _column_exists(con, table, "project_id")
    source_has_project_id = _column_exists(con, table, "project_id", alias=source_alias)
    insert_cols = list(source_cols)
    if central_has_project_id and not source_has_project_id:
        insert_cols.append("project_id")
    if not source_cols:
        return ImportSummary(project.project_id, "", table, 0, 0)

    cur = con.execute(
        "SELECT source_rowid FROM p4_import_idempotency "
        "WHERE project_id = ? AND source_table = ?",
        (project.project_id, table),
    )
    already = {int(row[0]) for row in cur.fetchall()}

    select_cols = ", ".join(f'"{c}"' for c in source_cols)
    # Stream rows in batches instead of materializing the full result set.
    # code_snippets can have 855k rows × full text payload; list() would load
    # all snippet bodies into Python memory at once.  OI-1377 / ADR-009.
    src_cursor = con.execute(
        f"SELECT rowid, {select_cols} FROM {source_alias}.{table}"
    )

    inserted = 0
    skipped = 0
    collision_cols = _collect_collision_columns(con, source_alias, table)
    json_array_cols = _collect_json_array_columns(con, source_alias, table)
    is_entity_table = (
        table == COLLISION_ENTITY_TABLE
        and COLLISION_ENTITY_ID_COLUMN in source_cols
        and COLLISION_ENTITY_TYPE_COLUMN in source_cols
    )
    while True:
        batch = src_cursor.fetchmany(_IMPORT_BATCH_SIZE)
        if not batch:
            break
        for row in batch:
            check_abort()
            rid = row[0]
            if rid in already:
                skipped += 1
                continue
            row_data = dict(zip(source_cols, row[1:]))
            if central_has_project_id:
                row_data["project_id"] = project.project_id
            for column in collision_cols:
                row_data[column] = _prefix_value(project.project_id, row_data.get(column))
            for column in json_array_cols:
                row_data[column] = _prefix_json_array(project.project_id, row_data.get(column))
            if is_entity_table:
                entity_type = row_data.get(COLLISION_ENTITY_TYPE_COLUMN)
                if (entity_type or "").lower() in COLLISION_ENTITY_TYPES_PREFIXED:
                    row_data[COLLISION_ENTITY_ID_COLUMN] = _prefix_value(
                        project.project_id,
                        row_data.get(COLLISION_ENTITY_ID_COLUMN),
                    )
            if table == "snippet_metadata" and "snippet_rowid" in row_data:
                mapped_rowid = _mapped_central_rowid(
                    con,
                    project.project_id,
                    "code_snippets",
                    int(row_data["snippet_rowid"]),
                )
                if mapped_rowid is None:
                    LOG.warning(
                        "INSERT skipped project=%s table=%s rowid=%s err=missing_code_snippet_rowid_map",
                        project.project_id,
                        table,
                        rid,
                    )
                    _record_skip(
                        con,
                        project.project_id,
                        table,
                        rid,
                        "missing_code_snippet_rowid_map",
                        run_id,
                    )
                    skipped += 1
                    continue
                row_data["snippet_rowid"] = mapped_rowid

            values = [row_data[column] for column in insert_cols]
            quoted_insert_cols = ", ".join(f'"{c}"' for c in insert_cols)
            placeholders = ", ".join("?" for _ in insert_cols)
            try:
                cur = con.execute(
                    f"INSERT OR IGNORE INTO {table} ({quoted_insert_cols}) VALUES ({placeholders})",
                    values,
                )
                if cur.rowcount == 1:
                    central_rowid = cur.lastrowid
                    con.execute(
                        "INSERT OR IGNORE INTO p4_import_idempotency "
                        "(project_id, source_table, source_rowid) VALUES (?, ?, ?)",
                        (project.project_id, table, rid),
                    )
                    # Self-heal: if a prior run logged this row as skipped (conflict
                    # / integrity error), mark it resolved now that the import
                    # succeeded. (Finding 3 round 2.)
                    _resolve_prior_skip(con, project.project_id, table, rid)
                    if table == "code_snippets":
                        # Preserve the logical snippet linkage without forcing raw
                        # rowid reuse across projects, which would collide.
                        _record_rowid_mapping(
                            con,
                            project.project_id,
                            table,
                            rid,
                            int(central_rowid),
                        )
                    inserted += 1
                else:
                    # SQLite IGNOREd the row (UNIQUE/PRIMARY KEY conflict). Do NOT
                    # write to p4_import_idempotency — that table must reflect
                    # actually-imported rows so re-runs can re-attempt the conflict
                    # if the central row is later deleted/repaired. Audit the skip.
                    LOG.warning(
                        "INSERT IGNORED project=%s table=%s rowid=%s (central key conflict)",
                        project.project_id, table, rid,
                    )
                    _record_skip(
                        con,
                        project.project_id,
                        table,
                        rid,
                        "insert_or_ignore_conflict",
                        run_id,
                    )
                    skipped += 1
            except sqlite3.IntegrityError as exc:
                LOG.warning(
                    "INSERT skipped project=%s table=%s rowid=%s err=%s",
                    project.project_id, table, rid, exc,
                )
                _record_skip(
                    con,
                    project.project_id,
                    table,
                    rid,
                    f"integrity_error:{exc}",
                    run_id,
                )
                skipped += 1
    return ImportSummary(project.project_id, "", table, inserted, skipped)


# ---------------------------------------------------------------------------
# Verification helpers (OI-1539)
# ---------------------------------------------------------------------------


def _src_table_present(con: sqlite3.Connection, alias: str, table: str) -> bool:
    """Return True iff ``alias.table`` is a real table or virtual table.

    Used to distinguish *missing* tables (acceptable; the table was added
    in a later schema and isn't in this source) from *unreadable* tables
    (fatal; the source DB is corrupt). (Finding 4 round 2.)
    """
    cur = con.execute(
        f"SELECT 1 FROM {alias}.sqlite_master WHERE type IN ('table','virtual') AND name = ?",
        (table,),
    )
    return cur.fetchone() is not None


def _compare_counts(
    central_db: Path,
    central_db_label: str,
    src_db: Path,
    project: ProjectEntry,
    tables: Iterable[str],
    read_errors: list[dict[str, object]],
) -> dict[str, dict]:
    """Compare per-project row counts; surface read failures via ``read_errors``.

    Unreadable source tables and central tables missing ``project_id`` are
    recorded as verification discrepancies instead of being counted as zero
    rows or unfiltered central totals.
    """
    out: dict[str, dict] = {}
    tables_list = list(tables)
    con = sqlite3.connect(str(central_db))
    try:
        try:
            attach_readonly(con, "src", src_db)
        except sqlite3.Error as exc:
            read_errors.append(
                {
                    "db": central_db_label,
                    "project_id": project.project_id,
                    "phase": "attach",
                    "error": str(exc),
                    "path": str(src_db),
                }
            )
            return out
        for tbl in tables_list:
            src_present = _src_table_present(con, "src", tbl)
            central_present = _table_exists(con, tbl)
            if not src_present:
                # Source predates this table → acceptable schema drift.
                continue
            if not central_present:
                # A missing central table while the source has data is a
                # verification discrepancy, not acceptable schema drift.
                read_errors.append(
                    {
                        "db": central_db_label,
                        "project_id": project.project_id,
                        "phase": "central_table_missing",
                        "table": tbl,
                        "error": "central DB missing import-target table",
                    }
                )
                continue
            try:
                src_cnt = con.execute(f"SELECT COUNT(*) FROM src.{tbl}").fetchone()[0]
            except sqlite3.Error as exc:
                read_errors.append(
                    {
                        "db": central_db_label,
                        "project_id": project.project_id,
                        "phase": "source_count",
                        "table": tbl,
                        "error": str(exc),
                    }
                )
                continue
            central_has_pid = _column_exists(con, tbl, "project_id")
            if not central_has_pid:
                # Import-target tables require project_id; otherwise an
                # unfiltered central count would produce a false match.
                read_errors.append(
                    {
                        "db": central_db_label,
                        "project_id": project.project_id,
                        "phase": "central_missing_project_id",
                        "table": tbl,
                        "error": (
                            "import-target table is missing project_id "
                            "column post-migration; per-project verification "
                            "cannot be performed without it"
                        ),
                    }
                )
                continue
            try:
                central_cnt = con.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE project_id = ?",
                    (project.project_id,),
                ).fetchone()[0]
            except sqlite3.Error as exc:
                read_errors.append(
                    {
                        "db": central_db_label,
                        "project_id": project.project_id,
                        "phase": "central_count",
                        "table": tbl,
                        "error": str(exc),
                    }
                )
                continue
            out[f"{central_db_label}.{tbl}"] = {
                "source_rows": int(src_cnt),
                "central_rows_for_project": int(central_cnt),
            }
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute("DETACH DATABASE src")
    finally:
        con.close()
    return out


def _collect_skipped_rows(
    central_db: Path,
    central_db_label: str,
    run_id: Optional[str] = None,
) -> list[dict[str, object]]:
    """Return *unresolved* skipped rows, optionally scoped to a run.

    Resolved conflicts are ignored, and ``run_id`` narrows verification to the
    current apply run when supplied.
    """
    if not central_db.exists():
        return []
    con = sqlite3.connect(str(central_db))
    try:
        if not _table_exists(con, "p4_import_skipped"):
            return []
        cols = {r[1] for r in con.execute("PRAGMA table_info(p4_import_skipped)")}
        has_resolved = "resolved_at" in cols
        has_run_id = "run_id" in cols
        clauses: list[str] = []
        params: list[object] = []
        if has_resolved:
            clauses.append("resolved_at IS NULL")
        if run_id and has_run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT project_id, source_table, source_rowid, reason "
            "FROM p4_import_skipped" + where +
            " ORDER BY project_id, source_table, source_rowid"
        )
        rows = con.execute(sql, params).fetchall()
        return [
            {
                "db": central_db_label,
                "project_id": row[0],
                "source_table": row[1],
                "source_rowid": int(row[2]),
                "reason": row[3],
            }
            for row in rows
        ]
    finally:
        con.close()
