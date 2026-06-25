#!/usr/bin/env python3
"""tenant_stamping.py — 3-phase tenant-isolation migration (W1, 1.0-blocker).

Implements the W1 spec exactly:
  Phase 1 (DDL)    — for every project-scoped table whose UNIQUE/PK EXCLUDES
                     project_id, rebuild to a composite UNIQUE/PK including
                     project_id (project_id NULLABLE here). Idempotent.
                     Checkpoint before.
  Phase 2 (data)   — resolve pid once (fail-closed). Re-stamp legacy
                     (NULL / '' / 'vnx-dev') -> pid across ALL schema-enumerated
                     tables, per-DB in its own foreign_keys=OFF + BEGIN EXCLUSIVE
                     transaction. Guard: abort on a third genuine tenant.
                     foreign_key_check + integrity_check before COMMIT.
                     Any failure -> ROLLBACK + restore checkpoint.
                     Post-condition: BOTH DBs hold zero legacy rows for pid != 'vnx-dev'.
  Phase 3 (DDL)    — rebuild tenant tables to project_id TEXT NOT NULL (no
                     DEFAULT 'vnx-dev'). Checkpoint before.

Pre-flight (before Phase 1):
  - Assert NO foreign key spans RC <-> QI.
  - Produce parent-before-child order via real FK-dependency topological sort.

See claudedocs/W1-TENANT-STAMPING-FIX-SPEC.md for the full specification.
ADR-007: docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LEGACY_PROJECT_IDS = {"vnx-dev", "", None}

# ---------------------------------------------------------------------------
# Helpers: identifier safety
# ---------------------------------------------------------------------------

import re as _re

_IDENT_RE_SAFE = _re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _safe_ident(name: str) -> str:
    """Validate and double-quote an SQL identifier for safe interpolation."""
    if not _IDENT_RE_SAFE.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return f'"{name}"'


# ---------------------------------------------------------------------------
# Schema-driven table enumeration
# ---------------------------------------------------------------------------

def _fts_shadow_table_names(conn: sqlite3.Connection) -> set[str]:
    """Return the set of FTS shadow table names for all FTS virtual tables in this DB.

    FTS5 (and FTS3/FTS4) virtual tables each produce a family of hidden shadow
    tables named <ftsname>_data, <ftsname>_idx, <ftsname>_content, <ftsname>_docsize,
    <ftsname>_config (FTS5), and for FTS3/4: <ftsname>_segments, <ftsname>_segdir.
    These are implementation internals — they never carry a project_id column and
    must not be treated as tenant tables.

    Detection: find every virtual table in sqlite_master whose sql contains
    'USING fts5', 'USING fts4', or 'USING fts3' (case-insensitive), then derive
    the shadow names from the virtual table name.

    This is the correct approach: shadow tables are defined by the *existence of a
    parent FTS virtual table*, not by their name suffix.  A real table named
    pool_config (which has a project_id column) has no parent FTS virtual table
    named 'pool', so it is NOT in the shadow set.
    """
    fts_suffixes = ("_data", "_idx", "_content", "_docsize", "_config",
                    "_segments", "_segdir")
    shadow_names: set[str] = set()

    fts_rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND ("
        "  sql LIKE '%USING fts5%' OR sql LIKE '%using fts5%' OR "
        "  sql LIKE '%USING fts4%' OR sql LIKE '%using fts4%' OR "
        "  sql LIKE '%USING fts3%' OR sql LIKE '%using fts3%'"
        ")"
    ).fetchall()

    for (fts_name,) in fts_rows:
        for suffix in fts_suffixes:
            shadow_names.add(fts_name + suffix)

    return shadow_names


def enumerate_project_id_tables(conn: sqlite3.Connection) -> list[str]:
    """Return every table carrying a project_id column in this DB.

    Uses sqlite_master + pragma_table_info — no hardcoded table list.
    Excludes FTS5/FTS4/FTS3 shadow tables and virtual tables (they have no
    direct UNIQUE constraints we can rebuild).

    The correct exclusion rule:
      1. A table that HAS a project_id column is, by definition, a real tenant
         table and must be included — regardless of its name suffix.  This means
         pool_config (ends in _config, but carries project_id) IS included.
      2. Real FTS shadow tables (xxx_data, xxx_idx, etc.) never carry project_id
         so they are naturally excluded by the EXISTS check alone.  We additionally
         build the explicit FTS shadow set (belt-and-suspenders) and exclude any
         table in that set.  This guards against edge cases where an FTS shadow
         table might somehow surface a project_id column in future SQLite versions.

    Replaces the previous brittle name-suffix NOT LIKE filters (%_config,
    %_data, %_idx, etc.) which incorrectly excluded legitimate tenant tables
    whose names happen to end in those suffixes (C4 concern, now manifested
    as a FK violation: worker_pools FK to pool_config broken by Phase 2).
    """
    fts_shadows = _fts_shadow_table_names(conn)

    # Collect FTS virtual table names so we can exclude the parent too.
    # FTS virtual tables report their content columns (e.g. 'project_id') via
    # PRAGMA table_info, even though the virtual table itself has no real schema.
    # Treating an FTS virtual table as a tenant table would break Phase 1 DDL
    # (which attempts to rebuild it via DROP+CREATE — not valid for virtual tables).
    fts_virtual_names: set[str] = {name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND ("
        "  sql LIKE '%USING fts5%' OR sql LIKE '%using fts5%' OR "
        "  sql LIKE '%USING fts4%' OR sql LIKE '%using fts4%' OR "
        "  sql LIKE '%USING fts3%' OR sql LIKE '%using fts3%'"
        ")"
    ).fetchall()}

    excluded = fts_shadows | fts_virtual_names

    rows = conn.execute(
        "SELECT m.name FROM sqlite_master m "
        "WHERE m.type='table' "
        "AND EXISTS ("
        "  SELECT 1 FROM pragma_table_info(m.name) WHERE name='project_id'"
        ") "
        "ORDER BY m.name"
    ).fetchall()

    return [r[0] for r in rows if r[0] not in excluded]


# ---------------------------------------------------------------------------
# Topological sort — parent-before-child via FK dependency
# ---------------------------------------------------------------------------

def topological_sort_tables(conn: sqlite3.Connection, tables: list[str]) -> list[str]:
    """Order tables parent-before-child using their FK dependencies.

    Algorithm (Kahn's BFS topological sort):
    1. For every table in ``tables``, query PRAGMA foreign_key_list to
       collect all FK edges (child -> parent).
    2. Build an adjacency graph restricted to the provided table set
       (FKs that reference tables outside the set are ignored — they
       form no ordering dependency within this set).
    3. Compute in-degrees for all nodes.
    4. Enqueue all zero-in-degree nodes (sorted for determinism).
    5. BFS: dequeue a node, emit it, decrement its dependents' in-degrees,
       enqueue any newly-zero nodes.
    6. If the emitted list is shorter than the input, a cycle exists —
       raise RuntimeError (SQLite FK cycles are forbidden anyway).

    This guarantees that for any FK (child -> parent), parent appears
    before child in the output, so the Phase-2 re-stamp touches a parent
    before its dependents (satisfying FK constraints even under FK-off).
    """
    table_set = set(tables)
    # Build child -> {parents} and parent -> {children} within the set
    parents: dict[str, set[str]] = {t: set() for t in tables}
    children: dict[str, set[str]] = {t: set() for t in tables}

    for table in tables:
        fk_rows = conn.execute(f"PRAGMA foreign_key_list({_safe_ident(table)[1:-1]})").fetchall()
        for row in fk_rows:
            # row: (id, seq, table, from, to, on_update, on_delete, match)
            ref_table = row[2]
            if ref_table in table_set and ref_table != table:
                parents[table].add(ref_table)
                children[ref_table].add(table)

    # Kahn's BFS
    in_degree = {t: len(parents[t]) for t in tables}
    queue = sorted(t for t in tables if in_degree[t] == 0)
    result: list[str] = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        for child in sorted(children[node]):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(result) != len(tables):
        cycle_nodes = [t for t in tables if t not in result]
        raise RuntimeError(
            f"FK dependency cycle detected among tables: {cycle_nodes}. "
            "SQLite does not allow FK cycles; this indicates a schema defect."
        )
    return result


# ---------------------------------------------------------------------------
# Pre-flight: no cross-DB FK
# ---------------------------------------------------------------------------

def assert_no_cross_db_fk(
    rc_conn: sqlite3.Connection,
    qi_conn: sqlite3.Connection,
    rc_tables: list[str],
    qi_tables: list[str],
) -> None:
    """Assert that no FK in RC references a QI table name, and vice versa.

    The per-DB-transaction design requires that no FK crosses the RC/QI
    boundary. This pre-flight catches any future schema addition that would
    invalidate that assumption.

    We compare FK references against the OTHER DB's table set — a FK to a
    table that exists in the other DB but not in the current one is the
    cross-DB case we are guarding against.
    """
    rc_table_set = set(rc_tables)
    qi_table_set = set(qi_tables)

    for table in rc_tables:
        fk_rows = rc_conn.execute(f"PRAGMA foreign_key_list({_safe_ident(table)[1:-1]})").fetchall()
        for row in fk_rows:
            ref = row[2]
            if ref in qi_table_set and ref not in rc_table_set:
                raise RuntimeError(
                    f"Cross-DB FK detected: RC.{table} -> QI.{ref}. "
                    "The per-DB-transaction design forbids cross-DB FKs. "
                    "This is a schema defect — fix before running W1."
                )

    for table in qi_tables:
        fk_rows = qi_conn.execute(f"PRAGMA foreign_key_list({_safe_ident(table)[1:-1]})").fetchall()
        for row in fk_rows:
            ref = row[2]
            if ref in rc_table_set and ref not in qi_table_set:
                raise RuntimeError(
                    f"Cross-DB FK detected: QI.{table} -> RC.{ref}. "
                    "The per-DB-transaction design forbids cross-DB FKs. "
                    "This is a schema defect — fix before running W1."
                )


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def checkpoint_db(db_path: Path, label: str) -> Path:
    """Copy the DB file to <db_path>.w1_checkpoint_<label> before mutation.

    B4 fix: before copying, run PRAGMA wal_checkpoint(TRUNCATE) so any
    committed-but-not-checkpointed WAL data is folded into the main file.
    This ensures the checkpoint copy is self-consistent (no orphaned WAL).
    The sha256 manifest is written alongside for integrity verification.

    Returns the checkpoint path. Existing checkpoint is overwritten (idempotent rerun).
    """
    checkpoint = Path(str(db_path) + f".w1_checkpoint_{label}")
    # Fold WAL into the main file before copying (B4).
    try:
        tmp_conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            tmp_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            tmp_conn.close()
    except Exception:
        pass  # If DB is not WAL-mode or doesn't exist yet, skip silently.
    shutil.copy2(str(db_path), str(checkpoint))
    # Write sha256 manifest for integrity verification.
    manifest = Path(str(checkpoint) + ".sha256")
    manifest.write_text(_sha256(checkpoint), encoding="ascii")
    return checkpoint


def restore_checkpoint(checkpoint: Path, db_path: Path) -> None:
    """Restore a DB from its checkpoint after a failed phase.

    B4 fix: verify the checkpoint's sha256 before restoring (integrity check).
    After copying, delete any live -wal/-shm files so a stale WAL cannot
    replay over the restored file and silently corrupt it.
    """
    manifest = Path(str(checkpoint) + ".sha256")
    if manifest.exists():
        expected = manifest.read_text(encoding="ascii").strip()
        actual = _sha256(checkpoint)
        if actual != expected:
            raise RuntimeError(
                f"Checkpoint integrity failure: sha256 mismatch for {checkpoint}. "
                f"Expected {expected}, got {actual}. "
                "The checkpoint copy is corrupt — cannot restore safely."
            )
    shutil.copy2(str(checkpoint), str(db_path))
    # Remove any stale WAL/SHM so they cannot replay over the restored file (B4).
    for suffix in ("-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Phase 1: DDL — add project_id to composite UNIQUE/PK (NULLABLE)
# ---------------------------------------------------------------------------

def _get_unique_indexes(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Return UNIQUE indexes on ``table`` that do NOT include project_id."""
    indexes = conn.execute(f"PRAGMA index_list({_safe_ident(table)[1:-1]})").fetchall()
    result = []
    for idx in indexes:
        idx_name = idx[1]
        unique = idx[2]  # 1 if UNIQUE
        origin = idx[3] if len(idx) > 3 else "c"  # 'u'=unique, 'pk'=primary key, 'c'=create
        if not unique:
            continue
        cols = [
            r[2]
            for r in conn.execute(f"PRAGMA index_info({_safe_ident(idx_name)[1:-1]})").fetchall()
        ]
        if "project_id" in cols:
            continue  # already composite with project_id
        result.append({
            "name": idx_name,
            "cols": cols,
            "origin": origin,
        })
    return result


def _format_default(dflt_value: str | None) -> str | None:
    """Format a PRAGMA table_info dflt_value for use in a CREATE TABLE statement.

    PRAGMA returns the default expression WITHOUT the outer parentheses that the
    original DDL may have used (e.g., 'strftime(...)' not '(strftime(...))').
    SQLite requires expressions in DEFAULT to be in parentheses when they contain
    function calls. Simple quoted strings and numeric literals also work inside
    parens: DEFAULT ('vnx-dev') and DEFAULT (0) are both valid.

    Strategy: always wrap in (...) to cover all cases uniformly. SQLite accepts
    DEFAULT ('literal'), DEFAULT (0), and DEFAULT (expr()) without complaint.
    """
    if dflt_value is None:
        return None
    # Already wrapped — don't double-wrap
    if dflt_value.startswith("(") and dflt_value.endswith(")"):
        return f"DEFAULT {dflt_value}"
    return f"DEFAULT ({dflt_value})"


def _get_secondary_indexes(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """Return (name, sql) for all explicitly-created indexes on ``table``.

    Queries sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL.
    The ``sql IS NOT NULL`` filter excludes auto-indexes created by SQLite for
    declared UNIQUE / PRIMARY KEY constraints (those have sql=NULL and origin='u'
    or origin='pk' in PRAGMA index_list). Only indexes from CREATE [UNIQUE] INDEX
    statements have non-NULL sql — these are origin='c' indexes in PRAGMA index_list.

    This captures the verbatim CREATE INDEX DDL including any partial WHERE clause
    and expression columns, so they can be recreated exactly after DROP+RENAME.
    """
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL "
        "ORDER BY name",
        (table,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _get_views_referencing(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """Return (name, sql) for all views that reference ``table`` by name.

    Used to drop views before a table rename (SQLite validates views at rename
    time) and recreate them after. SQLite will raise 'error in view X: no such
    table: main.T' if a view referencing T is present when T is dropped+renamed.

    We detect references by searching the view SQL for the table name (case-insensitive).
    This is a conservative heuristic — it may include views that reference the
    name in a comment, but dropping+recreating them is harmless.
    """
    views = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY name"
    ).fetchall()
    result = []
    for name, sql in views:
        if sql and table.lower() in sql.lower():
            result.append((name, sql))
    return result


def _get_triggers_for_table(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """Return (name, sql) for all triggers whose tbl_name is ``table``.

    Mirrors the approach in migrate_future_system._triggers_for().
    Used to capture triggers before DROP TABLE (cascade-drops them) and
    recreate them verbatim after the rename, preserving FTS-sync triggers
    (e.g. adrs_ai/adrs_ad/adrs_au) and any other table-level triggers.
    Only rows with non-NULL sql are returned — auto-generated system triggers
    have sql=NULL and cannot be recreated from DDL.
    """
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='trigger' AND tbl_name=? AND sql IS NOT NULL "
        "ORDER BY name",
        (table,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _get_table_columns(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Return full column info for a table."""
    rows = conn.execute(f"PRAGMA table_info({_safe_ident(table)[1:-1]})").fetchall()
    return [
        {
            "cid": r[0],
            "name": r[1],
            "type": r[2],
            "notnull": bool(r[3]),
            "dflt_value": r[4],
            "pk": r[5],
        }
        for r in rows
    ]


def _get_all_declared_unique_indexes(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Return ALL declared (origin='u') UNIQUE indexes on ``table``, regardless of whether
    they include project_id or not.

    origin='u' means the constraint was declared inline in CREATE TABLE as UNIQUE(...).
    origin='c' means a standalone CREATE [UNIQUE] INDEX statement — those are captured
    verbatim via _get_secondary_indexes and must never drive a rebuild decision.
    origin='pk' means the PRIMARY KEY auto-index — handled separately.

    Used by _rebuild_table_phase1 to ensure no declared UNIQUE is lost during a rebuild:
    composites are re-emitted verbatim; non-composites are widened by appending project_id.
    """
    indexes = conn.execute(f"PRAGMA index_list({_safe_ident(table)[1:-1]})").fetchall()
    result = []
    for idx in indexes:
        idx_name = idx[1]
        unique = idx[2]
        origin = idx[3] if len(idx) > 3 else "c"
        if not unique:
            continue
        if origin != "u":
            continue  # only declared UNIQUE constraints (not CREATE INDEX or PK auto-index)
        cols = [
            r[2]
            for r in conn.execute(f"PRAGMA index_info({_safe_ident(idx_name)[1:-1]})").fetchall()
        ]
        result.append({
            "name": idx_name,
            "cols": cols,
            "origin": origin,
        })
    return result


def _rebuild_table_phase1(
    conn: sqlite3.Connection,
    table: str,
    non_composite_uniques: list[dict],
) -> None:
    """Rebuild ``table`` to add project_id to each non-composite UNIQUE/PK.

    Phase 1 keeps project_id NULLABLE — we only widen the uniqueness key.
    Uses the copy-and-rename pattern (SQLite cannot ALTER CONSTRAINT).

    FK-off must already be active on this connection (caller's responsibility).
    """
    cols = _get_table_columns(conn, table)
    col_defs = []
    pk_cols = [c for c in cols if c["pk"] > 0]
    pk_col_names = {c["name"] for c in pk_cols}

    for col in cols:
        cname = col["name"]
        ctype = col["type"] or "TEXT"
        notnull = "NOT NULL" if col["notnull"] else ""
        dflt = _format_default(col["dflt_value"]) or ""

        # Remove NOT NULL from project_id for Phase 1 (we'll make it NOT NULL in Phase 3)
        if cname == "project_id":
            notnull = ""
            dflt = ""

        pk_ord = col["pk"]
        if pk_ord > 0:
            # Single-column INTEGER PK with AUTOINCREMENT is a special case
            if len(pk_cols) == 1 and ctype.upper() == "INTEGER":
                col_defs.append(f"  {_safe_ident(cname)} {ctype} PRIMARY KEY AUTOINCREMENT")
                continue
            # B2 fix: single-column NON-integer PK (e.g. TEXT PRIMARY KEY).
            # We MUST NOT emit `PRIMARY KEY` inline on the column — that would keep
            # the old single-column PK.  Instead emit the column as a plain column
            # and fall through to the table-constraint block below, which emits a
            # composite PRIMARY KEY (original_pk_col, project_id) per ADR-007.
            # (Multi-column PK is handled in the table-constraint block below.)
            if len(pk_cols) == 1 and ctype.upper() != "INTEGER":
                # Emit the column without PRIMARY KEY; the composite table constraint
                # will be added in the block below.
                parts = [_safe_ident(cname), ctype]
                if notnull:
                    parts.append(notnull)
                if dflt:
                    parts.append(dflt)
                col_defs.append("  " + " ".join(p for p in parts if p))
                continue

        parts = [_safe_ident(cname), ctype]
        if notnull:
            parts.append(notnull)
        if dflt:
            parts.append(dflt)
        col_defs.append("  " + " ".join(p for p in parts if p))

    # Composite PK table constraint.
    # B2 fix: emit for BOTH multi-col PK AND single-col non-INTEGER PK so the
    # PK is never silently dropped.  For single-col non-INTEGER PK, expand to
    # (original_pk_col, project_id) — the ADR-007 shape.
    single_nonint_pk = (
        len(pk_cols) == 1 and pk_cols[0]["type"].upper() != "INTEGER"
    )
    if len(pk_cols) > 1 or single_nonint_pk:
        pk_names = sorted(pk_cols, key=lambda c: c["pk"])
        pk_col_list = pk_names
        # If project_id is not already in PK columns, add it
        pk_name_set = {c["name"] for c in pk_col_list}
        if "project_id" not in pk_name_set:
            # Find original pk col names without project_id:
            extra_pk_cols = [c["name"] for c in pk_col_list]
            composite_pk = ", ".join(_safe_ident(n) for n in extra_pk_cols + ["project_id"])
            col_defs.append(f"  PRIMARY KEY ({composite_pk})")
        else:
            existing_pk = ", ".join(_safe_ident(c["name"]) for c in pk_col_list)
            col_defs.append(f"  PRIMARY KEY ({existing_pk})")

    # Rebuild UNIQUE constraints: re-emit ALL declared (origin='u') UNIQUE constraints.
    # - Already-composite ones (contain project_id) are re-emitted verbatim to avoid loss.
    # - Non-composite ones (lack project_id) are widened by appending project_id.
    # origin='c' indexes come from CREATE [UNIQUE] INDEX statements and carry verbatim
    # DDL including any partial WHERE clause — they are recreated verbatim after the
    # rename via _get_secondary_indexes. Never fold them into table-level UNIQUE constraints
    # as that would lose any WHERE clause and silently change a partial index into a full one.
    #
    # BUG 2 fix: enumerate the full set of origin='u' UNIQUEs from the live schema, not
    # just the non_composite_uniques subset passed by the caller. The caller only passes
    # the uniques that LACK project_id (the ones W1 actually widens), but a rebuild must
    # also preserve any already-composite declared UNIQUE that was filtered out of that
    # subset — otherwise the rebuilt table silently loses it, breaking FK constraints that
    # reference the composite unique key.
    all_declared_uniques = _get_all_declared_unique_indexes(conn, table)
    for uidx in all_declared_uniques:
        existing_cols = uidx["cols"]
        if "project_id" not in existing_cols:
            extended = existing_cols + ["project_id"]
        else:
            extended = existing_cols  # already composite — re-emit verbatim
        uc_list = ", ".join(_safe_ident(c) for c in extended)
        col_defs.append(f"  UNIQUE ({uc_list})")

    # Get FK list for this table
    fk_rows = conn.execute(f"PRAGMA foreign_key_list({_safe_ident(table)[1:-1]})").fetchall()
    fk_by_id: dict[int, list] = {}
    for row in fk_rows:
        fk_id = row[0]
        if fk_id not in fk_by_id:
            fk_by_id[fk_id] = []
        fk_by_id[fk_id].append(row)
    for fk_id, fk_group in sorted(fk_by_id.items()):
        from_cols = ", ".join(_safe_ident(r[3]) for r in sorted(fk_group, key=lambda r: r[1]))
        ref_table = fk_group[0][2]
        to_cols = ", ".join(_safe_ident(r[4]) for r in sorted(fk_group, key=lambda r: r[1]))
        on_update = fk_group[0][5]
        on_delete = fk_group[0][6]
        col_defs.append(
            f"  FOREIGN KEY ({from_cols}) REFERENCES {_safe_ident(ref_table)} ({to_cols})"
            f" ON UPDATE {on_update} ON DELETE {on_delete}"
        )

    # Capture secondary indexes (CREATE [UNIQUE] INDEX) BEFORE DROP+RENAME.
    # DROP TABLE cascade-removes all associated indexes. The origin='c' indexes
    # captured here include verbatim DDL with any partial WHERE clause and
    # expression columns. They are recreated verbatim after the rename so no
    # WHERE clause is lost and non-unique indexes are not permanently dropped.
    secondary_indexes = _get_secondary_indexes(conn, table)

    # Capture triggers BEFORE the DROP+RENAME sequence.
    # DROP TABLE cascade-drops all triggers on the table; they are never
    # automatically recreated after a rename. We must capture them here and
    # recreate them verbatim after the rename — mirroring migrate_future_system
    # _triggers_for / _recreate_dependent_objects. This preserves FTS-sync
    # triggers (e.g. adrs_ai/adrs_ad/adrs_au) so adrs_fts does not go stale.
    dependent_triggers = _get_triggers_for_table(conn, table)

    # Drop views that reference this table before the DROP+RENAME sequence.
    # SQLite validates views at rename time and will raise if a view references
    # the old name while it's temporarily absent. We recreate them after.
    dependent_views = _get_views_referencing(conn, table)
    for view_name, _ in dependent_views:
        conn.execute(f"DROP VIEW IF EXISTS {_safe_ident(view_name)}")

    staging = f"{table}_w1_p1"
    col_defs_sql = ",\n".join(col_defs)
    conn.execute(f"DROP TABLE IF EXISTS {_safe_ident(staging)}")
    conn.execute(f"CREATE TABLE {_safe_ident(staging)} (\n{col_defs_sql}\n)")

    # Count source rows before copy so we can assert no rows were silently dropped.
    source_rowcount = conn.execute(
        f"SELECT COUNT(*) FROM {_safe_ident(table)}"
    ).fetchone()[0]

    # Copy all columns (project_id is kept nullable in Phase 1).
    all_col_names = [c["name"] for c in cols]
    col_list = ", ".join(_safe_ident(n) for n in all_col_names)
    conn.execute(
        f"INSERT OR IGNORE INTO {_safe_ident(staging)} ({col_list}) "
        f"SELECT {col_list} FROM {_safe_ident(table)}"
    )

    # Assert every source row was copied.  INSERT OR IGNORE silently discards
    # rows that collide on the widened composite key — that must never happen
    # (data loss masked as success).  If a collision occurs the widened key is
    # not self-consistent with the existing data; fail loud so the operator can
    # investigate before any data is committed.
    rows_copied = conn.execute(
        f"SELECT COUNT(*) FROM {_safe_ident(staging)}"
    ).fetchone()[0]
    if rows_copied != source_rowcount:
        raise RuntimeError(
            f"Phase 1 row-copy mismatch for table '{table}': "
            f"{source_rowcount} source rows but only {rows_copied} copied. "
            "The widened composite key produced collisions — this indicates "
            "duplicate rows that would silently be dropped. "
            "Investigate the data before rerunning W1. ROLLBACK."
        )

    conn.execute(f"DROP TABLE {_safe_ident(table)}")
    conn.execute(f"ALTER TABLE {_safe_ident(staging)} RENAME TO {_safe_ident(table)}")

    # Recreate dependent views after the rename.
    for _, view_sql in dependent_views:
        conn.execute(view_sql)

    # Recreate triggers after the rename.  Triggers are recreated AFTER views
    # so any trigger body that references a view finds it already present.
    for _, trigger_sql in dependent_triggers:
        conn.execute(trigger_sql)

    # Recreate secondary indexes verbatim (CREATE [UNIQUE] INDEX).
    # DROP TABLE removed them; origin='c' indexes are not re-emitted as table-level
    # UNIQUE constraints (to preserve partial WHERE clauses). Execute the original
    # sql exactly — this preserves WHERE clauses, expression columns, and collations.
    for _, idx_sql in secondary_indexes:
        conn.execute(idx_sql)


def run_phase1_ddl(
    conn: sqlite3.Connection,
    tables: list[str],
    *,
    fk_already_off: bool = False,
) -> list[str]:
    """Phase 1: add project_id to UNIQUE/PK constraints for tables that need it.

    ``tables`` must already be in topological (parent-before-child) order.
    Returns the list of tables that were actually rebuilt.

    This function manages its own FK-off + BEGIN EXCLUSIVE transaction.
    """
    rebuilt: list[str] = []
    tables_needing_rebuild: list[tuple[str, list[dict]]] = []

    for table in tables:
        non_composite = _get_unique_indexes(conn, table)
        # BUG 1 fix: the rebuild trigger must consider ONLY origin='u' declared UNIQUE
        # constraints that lack project_id — those are the ones W1 actually widens.
        # origin='c' CREATE-INDEX uniques (e.g. partial indexes with WHERE clauses) are
        # preserved verbatim by _get_secondary_indexes and must NOT on their own force a
        # table rebuild. A table that is already in correct v31 shape (composite declared
        # UNIQUE present) but also carries an origin='c' partial unique index would
        # otherwise be spuriously rebuilt, losing the composite declared UNIQUE (BUG 2)
        # and breaking FK constraints that reference it.
        non_composite_declared = [u for u in non_composite if u.get("origin") == "u"]
        # Also check if a PK exists without project_id (multi-col OR single-col non-INTEGER).
        # B2 fix: include single-col non-INTEGER PK tables so we never drop a PK.
        pk_cols = [
            c for c in _get_table_columns(conn, table) if c["pk"] > 0
        ]
        pk_name_set = {c["name"] for c in pk_cols}
        pk_lacks_pid = (
            len(pk_cols) > 0
            and "project_id" not in pk_name_set
            and not (len(pk_cols) == 1 and pk_cols[0]["type"].upper() == "INTEGER")
        )
        if non_composite_declared or pk_lacks_pid:
            tables_needing_rebuild.append((table, non_composite))

    if not tables_needing_rebuild:
        return []

    original_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN EXCLUSIVE")
        try:
            for table, non_composite in tables_needing_rebuild:
                _rebuild_table_phase1(conn, table, non_composite)
                rebuilt.append(table)
            # B3(a) fix: run integrity checks before committing Phase 1 DDL.
            # A rebuild that silently damages the schema is caught here and
            # rolled back, never committed.
            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_violations:
                raise RuntimeError(
                    f"Phase 1 foreign_key_check failed after DDL rebuild: "
                    f"{fk_violations[:5]} (showing up to 5 violations). ROLLBACK."
                )
            ic_result = conn.execute("PRAGMA integrity_check").fetchall()
            if ic_result != [("ok",)]:
                raise RuntimeError(
                    f"Phase 1 integrity_check failed after DDL rebuild: "
                    f"{ic_result[:5]}. ROLLBACK."
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # vnx-silent-except: rollback best-effort
                pass
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON" if original_fk else "PRAGMA foreign_keys=OFF")
        conn.isolation_level = prev_isolation

    return rebuilt


# ---------------------------------------------------------------------------
# Phase 2: data re-stamp
# ---------------------------------------------------------------------------

def _resolve_legacy_guard(
    conn: sqlite3.Connection,
    tables: list[str],
    pid: str,
) -> None:
    """Guard: abort if any table contains a THIRD genuine tenant value.

    Legitimate state: rows with project_id IN (pid, 'vnx-dev', '', NULL).
    Abort state: any distinct non-NULL, non-empty value outside {pid, 'vnx-dev'}.
    """
    for table in tables:
        rows = conn.execute(
            f"SELECT DISTINCT project_id FROM {_safe_ident(table)} "
            f"WHERE project_id IS NOT NULL AND project_id != ''"
        ).fetchall()
        for (val,) in rows:
            if val != pid and val != "vnx-dev":
                raise RuntimeError(
                    f"Tenant isolation guard: table '{table}' contains a third "
                    f"genuine tenant '{val}' (expected only '{pid}' and 'vnx-dev'). "
                    "This is a real multi-tenant store — refusing to coerce. "
                    "Resolve the third tenant manually before running W1."
                )


def _restamp_table(conn: sqlite3.Connection, table: str, pid: str) -> int:
    """Re-stamp legacy project_id values in ``table`` to ``pid``.

    Legacy = NULL, '', or 'vnx-dev' (when pid != 'vnx-dev').
    Returns the number of rows updated.
    """
    if pid == "vnx-dev":
        # vnx-dev store: only NULL and '' need re-stamping
        cur = conn.execute(
            f"UPDATE {_safe_ident(table)} "
            f"SET project_id = ? "
            f"WHERE project_id IS NULL OR project_id = ''",
            (pid,),
        )
    else:
        cur = conn.execute(
            f"UPDATE {_safe_ident(table)} "
            f"SET project_id = ? "
            f"WHERE project_id IS NULL OR project_id IN ('vnx-dev', '')",
            (pid,),
        )
    return cur.rowcount


def run_phase2_restamp(
    conn: sqlite3.Connection,
    tables: list[str],
    pid: str,
    *,
    db_label: str = "DB",
) -> dict[str, int]:
    """Phase 2: re-stamp legacy rows in all enumerated tables.

    Runs in a single foreign_keys=OFF + BEGIN EXCLUSIVE transaction.
    Calls foreign_key_check + integrity_check before COMMIT.
    Any failure -> ROLLBACK (caller restores from checkpoint).

    ``tables`` must be in topological order (parent-before-child).
    Returns {table: rows_updated}.
    """
    # Guard first (before acquiring write lock)
    _resolve_legacy_guard(conn, tables, pid)

    updated: dict[str, int] = {}
    original_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN EXCLUSIVE")
        try:
            for table in tables:
                n = _restamp_table(conn, table, pid)
                updated[table] = n

            # Integrity checks before committing
            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_violations:
                raise RuntimeError(
                    f"[{db_label}] Phase 2 foreign_key_check failed after re-stamp: "
                    f"{fk_violations[:5]} (showing up to 5 violations). ROLLBACK."
                )
            ic_result = conn.execute("PRAGMA integrity_check").fetchall()
            if ic_result != [("ok",)]:
                raise RuntimeError(
                    f"[{db_label}] Phase 2 integrity_check failed after re-stamp: "
                    f"{ic_result[:5]}. ROLLBACK."
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # vnx-silent-except: rollback best-effort
                pass
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON" if original_fk else "PRAGMA foreign_keys=OFF")
        conn.isolation_level = prev_isolation

    return updated


def assert_phase2_postcondition(
    rc_conn: sqlite3.Connection,
    qi_conn: sqlite3.Connection,
    rc_tables: list[str],
    qi_tables: list[str],
    pid: str,
) -> None:
    """Post-condition: both DBs must hold zero legacy rows for pid != 'vnx-dev'.

    For the vnx-dev store itself: zero NULL/empty rows (legacy is only NULL/'').
    """
    for conn, tables, label in [(rc_conn, rc_tables, "RC"), (qi_conn, qi_tables, "QI")]:
        for table in tables:
            if pid == "vnx-dev":
                bad = conn.execute(
                    f"SELECT COUNT(*) FROM {_safe_ident(table)} "
                    f"WHERE project_id IS NULL OR project_id = ''"
                ).fetchone()[0]
            else:
                bad = conn.execute(
                    f"SELECT COUNT(*) FROM {_safe_ident(table)} "
                    f"WHERE project_id IS NULL OR project_id IN ('vnx-dev', '')"
                ).fetchone()[0]
            if bad:
                raise RuntimeError(
                    f"Phase 2 post-condition failed: [{label}] table '{table}' "
                    f"still has {bad} legacy row(s) after re-stamp for pid='{pid}'. "
                    "This indicates a split-brain or partial failure — rerun to converge."
                )


# ---------------------------------------------------------------------------
# Phase 3: DDL — enforce NOT NULL, drop DEFAULT 'vnx-dev'
# ---------------------------------------------------------------------------

def _rebuild_table_phase3(conn: sqlite3.Connection, table: str) -> None:
    """Rebuild ``table`` so project_id is TEXT NOT NULL with no DEFAULT.

    After Phase 2 no legacy rows remain, so NOT NULL is safe to enforce.
    Preserves all other columns, constraints, and FKs.
    Uses copy-and-rename pattern.
    """
    cols = _get_table_columns(conn, table)
    col_defs = []
    pk_cols = [c for c in cols if c["pk"] > 0]

    for col in cols:
        cname = col["name"]
        ctype = col["type"] or "TEXT"
        pk_ord = col["pk"]

        if cname == "project_id":
            # Enforce NOT NULL, remove any DEFAULT
            col_defs.append(f"  {_safe_ident(cname)} TEXT NOT NULL")
            continue

        if pk_ord > 0 and len(pk_cols) == 1 and ctype.upper() == "INTEGER":
            col_defs.append(f"  {_safe_ident(cname)} {ctype} PRIMARY KEY AUTOINCREMENT")
            continue

        parts = [_safe_ident(cname), ctype]
        # Preserve NOT NULL for ALL columns including composite-PK members (e.g.
        # tracks.track_id, track_open_items.oi_id/link_type). The single-INTEGER-PK
        # case already `continue`d above; project_id is handled above. A prior
        # `and pk_ord == 0` here dropped NOT NULL from non-project_id PK columns,
        # leaving the rebuilt table violating the v31 manifest (nullability drift)
        # on the idempotent re-run.
        if col["notnull"]:
            parts.append("NOT NULL")
        dflt_clause = _format_default(col["dflt_value"])
        if dflt_clause:
            parts.append(dflt_clause)
        col_defs.append("  " + " ".join(p for p in parts if p))

    # Multi-col PK constraint
    if len(pk_cols) > 1:
        pk_ordered = sorted(pk_cols, key=lambda c: c["pk"])
        pk_col_list = ", ".join(_safe_ident(c["name"]) for c in pk_ordered)
        col_defs.append(f"  PRIMARY KEY ({pk_col_list})")

    # Existing UNIQUE constraints (post-Phase 1 they already include project_id).
    # Only emit table-level UNIQUE for origin='u' (declared UNIQUE constraints with
    # sql=NULL / SQLite auto-index). origin='c' indexes come from CREATE [UNIQUE] INDEX
    # statements and carry verbatim DDL (including any partial WHERE clause) — they are
    # recreated verbatim after the rename via secondary_indexes below, NOT emitted here.
    indexes = conn.execute(f"PRAGMA index_list({_safe_ident(table)[1:-1]})").fetchall()
    for idx in indexes:
        idx_name = idx[1]
        unique = idx[2]
        origin = idx[3] if len(idx) > 3 else "c"
        if not unique:
            continue
        if origin == "pk":
            continue  # already handled as PRIMARY KEY
        if origin == "c":
            continue  # recreated verbatim via secondary_indexes after rename
        idx_cols = [
            r[2]
            for r in conn.execute(f"PRAGMA index_info({_safe_ident(idx_name)[1:-1]})").fetchall()
        ]
        col_list_str = ", ".join(_safe_ident(c) for c in idx_cols)
        col_defs.append(f"  UNIQUE ({col_list_str})")

    # FK constraints
    fk_rows = conn.execute(f"PRAGMA foreign_key_list({_safe_ident(table)[1:-1]})").fetchall()
    fk_by_id: dict[int, list] = {}
    for row in fk_rows:
        fk_id = row[0]
        if fk_id not in fk_by_id:
            fk_by_id[fk_id] = []
        fk_by_id[fk_id].append(row)
    for fk_id, fk_group in sorted(fk_by_id.items()):
        from_cols = ", ".join(_safe_ident(r[3]) for r in sorted(fk_group, key=lambda r: r[1]))
        ref_table = fk_group[0][2]
        to_cols = ", ".join(_safe_ident(r[4]) for r in sorted(fk_group, key=lambda r: r[1]))
        on_update = fk_group[0][5]
        on_delete = fk_group[0][6]
        col_defs.append(
            f"  FOREIGN KEY ({from_cols}) REFERENCES {_safe_ident(ref_table)} ({to_cols})"
            f" ON UPDATE {on_update} ON DELETE {on_delete}"
        )

    # Capture secondary indexes (CREATE [UNIQUE] INDEX) BEFORE DROP+RENAME.
    # DROP TABLE cascade-removes them. Verbatim DDL is preserved here so any
    # partial WHERE clause, expression columns, and collations survive Phase 3.
    secondary_indexes = _get_secondary_indexes(conn, table)

    # Capture triggers BEFORE the DROP+RENAME sequence.
    # Phase 3 uses the same copy-and-rename pattern as Phase 1 and has the same
    # trigger-loss risk: DROP TABLE cascade-drops all triggers. Capture them now
    # and recreate verbatim after the rename.
    dependent_triggers = _get_triggers_for_table(conn, table)

    # Drop views referencing this table before DROP+RENAME (SQLite validates at rename).
    dependent_views = _get_views_referencing(conn, table)
    for view_name, _ in dependent_views:
        conn.execute(f"DROP VIEW IF EXISTS {_safe_ident(view_name)}")

    staging = f"{table}_w1_p3"
    col_defs_sql = ",\n".join(col_defs)
    conn.execute(f"DROP TABLE IF EXISTS {_safe_ident(staging)}")
    conn.execute(f"CREATE TABLE {_safe_ident(staging)} (\n{col_defs_sql}\n)")

    all_col_names = [c["name"] for c in cols]
    col_list = ", ".join(_safe_ident(n) for n in all_col_names)
    conn.execute(
        f"INSERT INTO {_safe_ident(staging)} ({col_list}) "
        f"SELECT {col_list} FROM {_safe_ident(table)}"
    )
    conn.execute(f"DROP TABLE {_safe_ident(table)}")
    conn.execute(f"ALTER TABLE {_safe_ident(staging)} RENAME TO {_safe_ident(table)}")

    # Recreate dependent views after the rename.
    for _, view_sql in dependent_views:
        conn.execute(view_sql)

    # Recreate triggers after the rename (after views, so trigger bodies that
    # reference views find them already present).
    for _, trigger_sql in dependent_triggers:
        conn.execute(trigger_sql)

    # Recreate secondary indexes verbatim (CREATE [UNIQUE] INDEX).
    # DROP TABLE removed them; they are not emitted as table-level UNIQUE constraints
    # to preserve any partial WHERE clause and expression columns.
    for _, idx_sql in secondary_indexes:
        conn.execute(idx_sql)


def run_phase3_enforce(
    conn: sqlite3.Connection,
    tables: list[str],
    *,
    db_label: str = "DB",
) -> list[str]:
    """Phase 3: rebuild tables so project_id is TEXT NOT NULL (no DEFAULT 'vnx-dev').

    ``tables`` must be in topological order.
    Returns the list of tables rebuilt.
    """
    # Idempotent guard: only rebuild tables where project_id is NULLABLE or still has a DEFAULT.
    # Both conditions must be absent after Phase 3: NOT NULL AND no DEFAULT 'vnx-dev'.
    tables_needing_rebuild: list[str] = []
    for table in tables:
        cols = _get_table_columns(conn, table)
        for col in cols:
            if col["name"] == "project_id":
                is_nullable = not col["notnull"]
                has_default = col["dflt_value"] is not None
                if is_nullable or has_default:
                    tables_needing_rebuild.append(table)
                break

    if not tables_needing_rebuild:
        return []

    # Pre-condition: no legacy rows should remain
    for table in tables_needing_rebuild:
        bad = conn.execute(
            f"SELECT COUNT(*) FROM {_safe_ident(table)} "
            f"WHERE project_id IS NULL OR project_id = ''"
        ).fetchone()[0]
        if bad:
            raise RuntimeError(
                f"[{db_label}] Phase 3 pre-condition failed: table '{table}' still "
                f"has {bad} NULL/empty project_id row(s). Run Phase 2 first."
            )

    original_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN EXCLUSIVE")
        try:
            for table in tables_needing_rebuild:
                _rebuild_table_phase3(conn, table)
            # Verify after rebuild
            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_violations:
                raise RuntimeError(
                    f"[{db_label}] Phase 3 foreign_key_check failed: "
                    f"{fk_violations[:5]}. ROLLBACK."
                )
            ic_result = conn.execute("PRAGMA integrity_check").fetchall()
            if ic_result != [("ok",)]:
                raise RuntimeError(
                    f"[{db_label}] Phase 3 integrity_check failed: {ic_result[:5]}. ROLLBACK."
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # vnx-silent-except: rollback best-effort
                pass
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON" if original_fk else "PRAGMA foreign_keys=OFF")
        conn.isolation_level = prev_isolation

    return tables_needing_rebuild


# ---------------------------------------------------------------------------
# Orchestrator: run all 3 phases on a single DB
# ---------------------------------------------------------------------------

def _cleanup_checkpoint(ckpt: Path) -> None:
    """Delete a checkpoint file and its sha256 manifest if they exist.

    Called on successful migration to reclaim disk space.  The 1.65 GB
    seocrawler-v2 QI DB produces ~1.65 GB per checkpoint; four copies
    per DB run exhausted the disk on a 92%-full volume.  We keep only
    the premigration checkpoint (taken before any mutation) and delete it
    on success.  On failure the premigration checkpoint is intentionally
    left in place for operator recovery; only its path is logged.
    """
    for path in (ckpt, Path(str(ckpt) + ".sha256")):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass  # best-effort; do not mask the primary result


def run_three_phase_migration_on_db(
    db_path: Path,
    pid: str,
    *,
    db_label: str = "DB",
    skip_phase3: bool = False,
) -> dict:
    """Run Phases 1, 2, 3 on a single DB file.

    B3(b) fix: keep ONE single pre-migration checkpoint taken BEFORE Phase 1.
    On ANY phase failure, restore from this pre-migration checkpoint — not from
    a per-phase checkpoint taken after earlier phases already mutated the DB.
    This ensures 'Restored from checkpoint' always means the DB is back to its
    original pre-migration state, never a partially-migrated intermediate.

    Per-phase checkpoints are NOT taken (disk blowup fix): the 1.65 GB QI DB
    would produce ~6.6 GB of checkpoint files; the premigration copy suffices.
    On success, the premigration checkpoint is deleted to reclaim disk space.
    On failure, it is left in place for operator recovery and its path is logged.

    This is the single-DB runner. The two-DB orchestrator
    (run_three_phase_migration) calls this for RC and QI separately
    and checks the coupled post-condition after both Phase 2s.
    Returns a result dict with per-phase outcomes.
    """
    result: dict = {"db": str(db_path), "pid": pid}

    # Take the pre-migration checkpoint BEFORE any mutation.
    # All phase failures restore from this single safe copy.
    ckpt_premigration = checkpoint_db(db_path, "premigration")
    result["checkpoint_premigration"] = str(ckpt_premigration)

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        tables_raw = enumerate_project_id_tables(conn)
        tables = topological_sort_tables(conn, tables_raw)
        result["tables"] = tables

        # --- Phase 1 ---
        try:
            rebuilt1 = run_phase1_ddl(conn, tables)
            result["phase1_rebuilt"] = rebuilt1
        except Exception as exc:
            conn.close()
            conn = None
            restore_checkpoint(ckpt_premigration, db_path)
            _verify_restore(db_path, ckpt_premigration, db_label)
            # Leave premigration checkpoint on disk for operator recovery.
            raise RuntimeError(
                f"[{db_label}] Phase 1 (DDL constraint repair) failed: {exc}. "
                f"Restored from pre-migration checkpoint: {ckpt_premigration}"
            ) from exc

        # Re-enumerate after Phase 1 DDL rebuild (table shapes changed)
        tables_raw = enumerate_project_id_tables(conn)
        tables = topological_sort_tables(conn, tables_raw)

        # --- Phase 2 ---
        try:
            updated = run_phase2_restamp(conn, tables, pid, db_label=db_label)
            result["phase2_updated"] = updated
        except Exception as exc:
            conn.close()
            conn = None
            restore_checkpoint(ckpt_premigration, db_path)
            _verify_restore(db_path, ckpt_premigration, db_label)
            raise RuntimeError(
                f"[{db_label}] Phase 2 (data re-stamp) failed: {exc}. "
                f"Restored from pre-migration checkpoint: {ckpt_premigration}"
            ) from exc

        # --- Phase 3 ---
        if not skip_phase3:
            try:
                rebuilt3 = run_phase3_enforce(conn, tables, db_label=db_label)
                result["phase3_rebuilt"] = rebuilt3
            except Exception as exc:
                conn.close()
                conn = None
                restore_checkpoint(ckpt_premigration, db_path)
                _verify_restore(db_path, ckpt_premigration, db_label)
                raise RuntimeError(
                    f"[{db_label}] Phase 3 (NOT NULL enforcement) failed: {exc}. "
                    f"Restored from pre-migration checkpoint: {ckpt_premigration}"
                ) from exc

        result["ok"] = True
    finally:
        if conn is not None:
            conn.close()

    # On success: delete the premigration checkpoint to reclaim disk space.
    _cleanup_checkpoint(ckpt_premigration)

    return result


def _verify_restore(db_path: Path, ckpt: Path, db_label: str) -> None:
    """Verify that a restored DB matches its checkpoint by sha256.

    Called after restore_checkpoint to confirm the DB is genuinely restored.
    Logs a warning if verification fails (the restore still happened; this
    is a defence-in-depth check, not a second failure path).
    """
    manifest = Path(str(ckpt) + ".sha256")
    if not manifest.exists():
        return
    expected = manifest.read_text(encoding="ascii").strip()
    actual = _sha256(db_path)
    if actual != expected:
        import warnings as _warnings
        _warnings.warn(
            f"[{db_label}] Post-restore sha256 mismatch for {db_path}. "
            f"Expected {expected}, got {actual}. "
            "The DB may not have been fully restored.",
            RuntimeWarning,
            stacklevel=2,
        )


# ---------------------------------------------------------------------------
# Two-DB coupled orchestrator
# ---------------------------------------------------------------------------

def run_three_phase_migration(
    rc_db_path: Path,
    qi_db_path: Path,
    pid: str,
) -> dict:
    """Run the 3-phase migration across both RC and QI DBs.

    Non-atomic by design (two separate DB files). Coupling strategy:
    - Run Phases 1+2 on RC, then Phases 1+2 on QI.
    - Assert the post-condition on BOTH (zero legacy rows) before Phase 3.
    - Run Phase 3 on RC, then Phase 3 on QI.

    Idempotent rerun: if RC committed Phase 2 but QI aborted, a rerun of
    Phase 2 will find RC already clean (no legacy rows to update) and
    will succeed silently; QI will be re-stamped to convergence.

    Returns a combined result dict.
    """
    combined: dict = {"pid": pid, "rc": str(rc_db_path), "qi": str(qi_db_path)}

    # --- Pre-flight: cross-DB FK check + ordering ---
    rc_conn = sqlite3.connect(str(rc_db_path), timeout=30.0)
    qi_conn = sqlite3.connect(str(qi_db_path), timeout=30.0)
    try:
        rc_tables_raw = enumerate_project_id_tables(rc_conn)
        qi_tables_raw = enumerate_project_id_tables(qi_conn)
        assert_no_cross_db_fk(rc_conn, qi_conn, rc_tables_raw, qi_tables_raw)
    finally:
        rc_conn.close()
        qi_conn.close()

    # --- Phase 1 + 2 on RC ---
    rc_result = run_three_phase_migration_on_db(
        rc_db_path, pid, db_label="RC", skip_phase3=True
    )
    combined["rc_phase1"] = rc_result.get("phase1_rebuilt", [])
    combined["rc_phase2"] = rc_result.get("phase2_updated", {})

    # --- Phase 1 + 2 on QI ---
    qi_result = run_three_phase_migration_on_db(
        qi_db_path, pid, db_label="QI", skip_phase3=True
    )
    combined["qi_phase1"] = qi_result.get("phase1_rebuilt", [])
    combined["qi_phase2"] = qi_result.get("phase2_updated", {})

    # --- Coupled post-condition (Phase 2) ---
    rc_conn2 = sqlite3.connect(str(rc_db_path), timeout=30.0)
    qi_conn2 = sqlite3.connect(str(qi_db_path), timeout=30.0)
    try:
        rc_tables_after = topological_sort_tables(
            rc_conn2, enumerate_project_id_tables(rc_conn2)
        )
        qi_tables_after = topological_sort_tables(
            qi_conn2, enumerate_project_id_tables(qi_conn2)
        )
        assert_phase2_postcondition(rc_conn2, qi_conn2, rc_tables_after, qi_tables_after, pid)
    finally:
        rc_conn2.close()
        qi_conn2.close()

    # --- Phase 3 on RC ---
    # Take a premigration-for-phase3 checkpoint (state after Phases 1+2 succeeded).
    # Delete it on success; leave it for operator recovery on failure.
    rc_ckpt_p3 = checkpoint_db(rc_db_path, "premigration_phase3")
    rc_conn3 = sqlite3.connect(str(rc_db_path), timeout=30.0)
    try:
        tables_raw = enumerate_project_id_tables(rc_conn3)
        tables = topological_sort_tables(rc_conn3, tables_raw)
        try:
            rebuilt = run_phase3_enforce(rc_conn3, tables, db_label="RC")
            combined["rc_phase3"] = rebuilt
        except Exception as exc:
            rc_conn3.close()
            rc_conn3 = None
            restore_checkpoint(rc_ckpt_p3, rc_db_path)
            raise RuntimeError(
                f"RC Phase 3 failed: {exc}. "
                f"Restored from checkpoint: {rc_ckpt_p3}"
            ) from exc
    finally:
        if rc_conn3 is not None:
            rc_conn3.close()
    _cleanup_checkpoint(rc_ckpt_p3)

    # --- Phase 3 on QI ---
    qi_ckpt_p3 = checkpoint_db(qi_db_path, "premigration_phase3")
    qi_conn3 = sqlite3.connect(str(qi_db_path), timeout=30.0)
    try:
        tables_raw = enumerate_project_id_tables(qi_conn3)
        tables = topological_sort_tables(qi_conn3, tables_raw)
        try:
            rebuilt = run_phase3_enforce(qi_conn3, tables, db_label="QI")
            combined["qi_phase3"] = rebuilt
        except Exception as exc:
            qi_conn3.close()
            qi_conn3 = None
            restore_checkpoint(qi_ckpt_p3, qi_db_path)
            raise RuntimeError(
                f"QI Phase 3 failed: {exc}. "
                f"Restored from checkpoint: {qi_ckpt_p3}"
            ) from exc
    finally:
        if qi_conn3 is not None:
            qi_conn3.close()
    _cleanup_checkpoint(qi_ckpt_p3)

    combined["ok"] = True
    return combined
