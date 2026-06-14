#!/usr/bin/env python3
"""migrate_future_system.py — apply track layer migrations (schema only).

run() ordering (R2.2 — repair → version-reconcile → numbered walk):
  A. ADR-007 dispatches repair (`_run_adr007_dispatches_repair`, PR-A1) — the ad-hoc
     in-place composite-UNIQUE rebuild, a PRE-walk step. NOT a numbered migration.
  B. Numbered version reconciliation (`_run_version_reconciliation`, PR-A2) — validate
     the DB's CLAIMED `user_version` against the declarative invariant manifest
     (scripts/lib/schema_manifest.py). A DB that LIES about its version (claims v30 but
     is physically v27) is DOWNGRADED to the exact highest version whose invariants
     fully hold, so the numbered walk re-applies whatever the downgrade exposes.
  C. Numbered migration walk (0022 → 0030), each idempotent via user_version.
  D. Convergence guard (`_assert_manifest_converged`) — after the walk the terminal
     version's manifest MUST hold, else a downgrade+re-walk did not converge → abort
     loudly rather than loop (oscillation guard).

The reconciliation (B) runs BEFORE the walk (C) so the walk re-applies whatever the
downgrade exposed. This matches the operator ordering in PRD §6 (migrate first) and
PRD §7.2 (Run: migrate → backfill → bridge → reconcile). The numbered walk itself:

  1. PRAGMA pre-flight: assert dispatches schema and UNIQUE constraint are intact
  2. Apply schemas/migrations/0022_track_layer.sql (idempotent via user_version)
  3. PRAGMA pre-flight: assert tracks v22 schema intact before composite-key rebuild
  4. Apply schemas/migrations/0024_tracks_tenant_scoping.sql (idempotent via user_version)
  5. PRAGMA pre-flight: assert tracks composite-key schema intact before adding horizon
  6. Apply schemas/migrations/0027_planning_horizon_and_deliverable_view.sql (idempotent)
  7. PRAGMA pre-flight: assert tracks has horizon (v27) before adding derived_status
  8. Apply schemas/migrations/0028_tracks_derived_status.sql (idempotent)
  9. PRAGMA pre-flight: assert tracks has derived_status (v28) before adding track_type
  10. Apply schemas/migrations/0029_track_type_discriminator.sql (idempotent)
  11. PRAGMA pre-flight: assert track_type present (v29) before adding resolved_at
  12. Apply schemas/migrations/0030_track_oi_resolved_at.sql (idempotent)

The per-version preflights (steps 3-11) are now MANIFEST-BACKED (schema_manifest):
the name-based column/table assertions are sourced from the declarative manifest, not
hand-typed name sets (ADR-009 schema-first). Cite ADR-007 (composite tenant keys).
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import tempfile
import time
import warnings
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap sys.path so lib modules resolve regardless of cwd
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
_SCHEMAS = _HERE.parent / "schemas"
_MIGRATIONS = _SCHEMAS / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from project_root import resolve_project_root
import schema_migration
import schema_manifest
from atomic_io import audit_event_append


# ---------------------------------------------------------------------------
# Test isolation guard (R8.6 / PR-0) — active only under pytest
# ---------------------------------------------------------------------------

def _pytest_db_isolation_guard(project_root: Path) -> None:
    """Refuse to open any DB when running under pytest without explicit isolation.

    Active only when PYTEST_CURRENT_TEST is set (i.e. inside a pytest process).
    Two conditions must hold:
    1. VNX_DATA_DIR_EXPLICIT=1 must be set.
    2. The resolved .vnx-data root derived from project_root must be under
       tempfile.gettempdir() and NOT under ~/.vnx-data.

    The second check prevents tests from passing the flag while still resolving
    to the real canonical data location — a false sense of isolation.

    Production code is never affected: PYTEST_CURRENT_TEST is only set by pytest.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") is None and "pytest" not in sys.modules:
        return
    if os.environ.get("VNX_DATA_DIR_EXPLICIT") != "1":
        raise RuntimeError(
            "[TEST ISOLATION GUARD] migrate_future_system.run() called under pytest "
            "without VNX_DATA_DIR_EXPLICIT=1. This would open the live database. "
            "Ensure the _fsr_migration_module_isolation fixture is active (tests/conftest.py), "
            "or set VNX_DATA_DIR_EXPLICIT=1 and VNX_DATA_DIR=<tmp_path> in your test."
        )
    # Flag is set — validate the resolved data root is actually temp-owned.
    data_root = (project_root / ".vnx-data").resolve()
    tmp_root = Path(tempfile.gettempdir()).resolve()
    canonical = (Path.home() / ".vnx-data").resolve()
    _sep = os.sep
    under_tmp = (
        str(data_root) == str(tmp_root)
        or str(data_root).startswith(str(tmp_root) + _sep)
    )
    under_canonical = (
        str(data_root) == str(canonical)
        or str(data_root).startswith(str(canonical) + _sep)
    )
    if under_canonical or not under_tmp:
        raise RuntimeError(
            f"[TEST ISOLATION GUARD] VNX_DATA_DIR_EXPLICIT=1 is set but the resolved "
            f"data root '{data_root}' is NOT under the system temp directory ('{tmp_root}'). "
            "Setting the flag while pointing at the canonical ~/.vnx-data location is unsafe. "
            "Pass a pytest tmp_path-based project_root to migrate_future_system.run()."
        )


# ===========================================================================
# ADR-007 dispatches repair (PR-A1) — general-purpose, idempotent, lossless
# in-place rebuild that converts ANY single-column uniqueness on dispatch_id
# into the composite UNIQUE(dispatch_id, project_id).
#
# Position in run() (R2.2): runs as a PRE-MIGRATION step — after the
# _pytest_db_isolation_guard, BEFORE the numbered version walk (0022→0030).
# It is a no-op when the schema is already composite (detected by parsing
# sqlite_master / PRAGMA, NOT a bare column check), so the operator ordering in
# PRD §6 ("migrate first") is preserved. This is the general robust repair; it
# does NOT replace the narrow _strip_stale_dispatches_track_fk (v24 FK path).
#
# ADR-007 binding: composite UNIQUE/PK over project_id for every central-DB
# table; NEVER default project_id to 'vnx-dev' as a sentinel for unknown
# identity (R3.1 fail-closed). See
# docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md
# ===========================================================================

_IDENT_PATTERN = r'"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*'
_IDENT_RE = re.compile(_IDENT_PATTERN)
_CONSTRAINT_KW = ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT")


def _mask_quoted_sql(sql: str) -> str:
    """Mask quoted content while preserving length and quote delimiters.

    Handles SQL strings plus double-quoted, backtick-quoted, and bracket-quoted
    identifiers, including doubled closing delimiters. This is intentionally a
    bounded scanner for the existing CREATE TABLE/INDEX helpers, not a SQL parser.
    """
    out, i = list(sql), 0
    while i < len(sql):
        opener = sql[i]
        if opener not in ("'", '"', "`", "["):
            i += 1
            continue
        closer = "]" if opener == "[" else opener
        i += 1
        while i < len(sql):
            if sql[i] != closer:
                out[i] = "\x00"
                i += 1
                continue
            if i + 1 < len(sql) and sql[i + 1] == closer:
                out[i] = out[i + 1] = "\x00"
                i += 2
                continue
            i += 1
            break
    return "".join(out)


def _matching_paren(sql: str, open_pos: int) -> int:
    """Index of the ``)`` matching the ``(`` at *open_pos*, skipping quoted text."""
    depth = 0
    masked = _mask_quoted_sql(sql)
    for i in range(open_pos, len(masked)):
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses in SQL")


def _paren_group(sql: str, open_pos: int) -> str:
    """Return the substring inside the parentheses that open at *open_pos* (G)."""
    return sql[open_pos + 1:_matching_paren(sql, open_pos)]


def _referenced_columns(spec: str, dispatch_cols) -> set[str]:
    """Identifiers in *spec* that name a real dispatches column (lower-cased).

    Strips quoting; SQL keywords, collation names and function names are ignored
    because they never coincide with a dispatches column name.
    """
    cols = {c.lower() for c in dispatch_cols}
    return {tok.strip('"`[]').lower() for tok in _IDENT_RE.findall(spec)
            if tok.strip('"`[]').lower() in cols}


def _dispatch_table_columns(conn: sqlite3.Connection):
    """Return [(name, is_generated)] for dispatches via PRAGMA table_xinfo.

    hidden in (2, 3) marks a GENERATED column (VIRTUAL/STORED), which must be
    excluded from any INSERT…SELECT copy — its value is recomputed (R1.5).
    """
    rows = conn.execute("PRAGMA table_xinfo('dispatches')").fetchall()
    return [(r[1], r[6] in (2, 3)) for r in rows]


def _index_is_solo_dispatch_id(conn: sqlite3.Connection, index_name: str,
                               dispatch_cols) -> bool:
    """True iff the unique index *index_name* is keyed SOLELY on dispatch_id.

    PRAGMA index_xinfo classifies plain/decorated key columns (DESC, COLLATE);
    an expression key (cid == -2) falls back to parsing the CREATE INDEX SQL
    (e.g. UNIQUE(lower(dispatch_id))).
    """
    xinfo = conn.execute(f"PRAGMA index_xinfo('{index_name}')").fetchall()
    key_rows = [r for r in xinfo if r[5] == 1]  # r[5] == key flag
    if not key_rows:
        return False
    if all(r[1] >= 0 for r in key_rows):        # r[1] == cid; >=0 → real column
        names = {r[2].lower() for r in key_rows if r[2] is not None}
        return names == {"dispatch_id"}
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
    ).fetchone()
    if not row or not row[0]:
        return False
    spec = _paren_group(row[0], row[0].index("("))
    return _referenced_columns(spec, dispatch_cols) == {"dispatch_id"}


def _unique_index_rows(conn: sqlite3.Connection):
    """PRAGMA index_list rows for dispatches that enforce uniqueness (r[2]==1)."""
    return [r for r in conn.execute("PRAGMA index_list('dispatches')") if r[2] == 1]


def _index_sql(conn: sqlite3.Connection, index_name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
    ).fetchone()
    return row[0] if row and row[0] else None


def _index_is_partial(conn: sqlite3.Connection, index_row) -> bool:
    """True when an index is partial by PRAGMA metadata or CREATE INDEX SQL."""
    if len(index_row) > 4 and bool(index_row[4]):
        return True
    sql = _index_sql(conn, index_row[1])
    return bool(sql and re.search(r"(?i)\bWHERE\b", _mask_quoted_sql(sql)))


def _index_key_column_names(conn: sqlite3.Connection, index_name: str) -> list[str]:
    """Return real key-column names; expressions make the result invalid/empty."""
    xinfo = conn.execute(f"PRAGMA index_xinfo('{index_name}')").fetchall()
    key_rows = [r for r in xinfo if len(r) > 5 and r[5] == 1]
    if not key_rows:
        key_rows = conn.execute(f"PRAGMA index_info('{index_name}')").fetchall()
    if any(r[1] < 0 or r[2] is None for r in key_rows):
        return []
    return [r[2].lower() for r in key_rows]


def _has_solo_dispatch_id_unique(conn: sqlite3.Connection, dispatch_cols) -> bool:
    return any(_index_is_solo_dispatch_id(conn, r[1], dispatch_cols)
               for r in _unique_index_rows(conn))


def _has_composite_unique(conn: sqlite3.Connection, dispatch_cols=None) -> bool:
    """True for a full UNIQUE keyed exactly on dispatch_id + project_id.

    ADR-007 requires uniqueness for every row. A partial unique index cannot
    satisfy the contract because duplicate pairs remain possible outside its
    WHERE predicate.
    """
    for r in _unique_index_rows(conn):
        if _index_is_partial(conn, r):
            continue
        names = _index_key_column_names(conn, r[1])
        if len(names) == 2 and set(names) == {"dispatch_id", "project_id"}:
            return True
    return False


def _dispatches_needs_adr007_repair(conn: sqlite3.Connection) -> bool:
    """True when dispatches lacks the ADR-007 composite UNIQUE(dispatch_id, project_id).

    Detection is sqlite_master/PRAGMA based (R1.1). Repair is needed when EITHER a
    single-column uniqueness on dispatch_id still exists OR the composite is absent
    (N1: a table with NO solo uniqueness AND no composite was previously treated as
    "no repair needed", so the composite was never added). Semantics:
      already-composite (no solo)      → False  (no-op)
      solo dispatch_id uniqueness      → True
      neither solo nor composite       → True   (N1: composite now added)
      composite + a stray solo unique  → True
    A missing dispatches table is a no-op. See ADR-007
    (docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md).
    """
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatches'"
    ).fetchone()
    if not present:
        return False
    cols = [c for c, _ in _dispatch_table_columns(conn)]
    return (_has_solo_dispatch_id_unique(conn, cols)
            or not _has_composite_unique(conn, cols))


# --- R3.1: DB-path-anchored, fail-closed project_id resolver/validator -------

def _project_id_from_db_path(db_path) -> str | None:
    """Anchor project_id on the DB's physical location (R3.1).

    Canonical layout: <root>/.vnx-data/<project_id>/state/runtime_coordination.db
    Returns <project_id> when that shape matches, else None.
    """
    p = Path(db_path).resolve()
    state_dir = p.parent
    if state_dir.name != "state":
        return None
    pid_dir = state_dir.parent
    if pid_dir.parent.name != ".vnx-data":
        return None
    return pid_dir.name or None


def _marker_project_id(db_path) -> str | None:
    """Read the nearest .vnx-project-id marker walking UP from the DB path.

    Anchored on the DB path (NOT cwd) so a stray marker in the operator's
    working tree cannot override the tenant of the database being repaired
    (the codex-F1 leak class).
    """
    start = Path(db_path).resolve().parent
    for ancestor in [start, *start.parents]:
        marker = ancestor / ".vnx-project-id"
        if marker.is_file():
            try:
                first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
            except (OSError, IndexError):
                return None
            return first or None
    return None


def _resolve_validated_project_id(db_path) -> str:
    """Derive+validate project_id, FAIL CLOSED, never default to 'vnx-dev' (R3.1).

    Anchor/precedence: resolved DB path → .vnx-project-id marker → VNX_PROJECT_ID.
    Every present source MUST agree; any conflict aborts (the codex-F1 fix: env
    can never override the DB's real tenant). No source at all → abort.
    Cite ADR-007 (docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md).
    """
    sources = {
        "db-path": _project_id_from_db_path(db_path),
        "marker": _marker_project_id(db_path),
        "env:VNX_PROJECT_ID": (os.environ.get("VNX_PROJECT_ID") or "").strip() or None,
    }
    present = {k: v for k, v in sources.items() if v}
    distinct = set(present.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{k}={v!r}" for k, v in present.items())
        raise RuntimeError(
            "ADR-007 project_id conflict — cannot stamp dispatches with an "
            f"ambiguous tenant identity ({detail}). Resolve the conflict; "
            "refusing to guess (R3.1)."
        )
    if not distinct:
        raise RuntimeError(
            "ADR-007 fail-closed: cannot resolve project_id for the dispatches "
            "repair from the DB path, .vnx-project-id marker, or VNX_PROJECT_ID. "
            "No silent 'vnx-dev' default (R3.1). See docs/governance/decisions/"
            "ADR-007-multitenant-project-id-stamping.md"
        )
    return distinct.pop()


def _validate_existing_project_id_or_abort(conn: sqlite3.Connection,
                                           project_id: str) -> None:
    """ABORT (pre-mutation) on a bad existing project_id column (R1.4).

    NULL/empty value → abort (no COALESCE coercion). A value conflicting with the
    validated identity → abort. The DB is left byte-unchanged because this runs
    before any schema/data/version mutation. A MISSING column is fine — it is
    added and stamped from the validated identity later (R1.4a).
    """
    cols = [c for c, _ in _dispatch_table_columns(conn)]
    if "project_id" not in cols:
        return
    # SQLite's bare TRIM() strips only the ASCII space (0x20); pass the full
    # whitespace set so a tab/newline/CR/FF/VT-only project_id is treated as empty
    # and aborts like '' rather than passing as a valid tenant (I).
    bad = conn.execute(
        "SELECT COUNT(*) FROM dispatches WHERE project_id IS NULL OR "
        "TRIM(project_id, char(32)||char(9)||char(10)||char(13)||char(11)||char(12)) = ''"
    ).fetchone()[0]
    if bad:
        raise RuntimeError(
            f"ADR-007 abort: dispatches has {bad} row(s) with NULL/empty project_id. "
            "Refusing to coerce bad tenant data; fix the rows first (R1.4c)."
        )
    others = sorted({r[0] for r in conn.execute("SELECT DISTINCT project_id FROM dispatches")
                     if r[0] != project_id})
    if others:
        raise RuntimeError(
            f"ADR-007 abort: dispatches rows carry project_id {others!r} conflicting "
            f"with the resolved tenant {project_id!r} (R1.4d). DB unchanged."
        )


# --- SQL transform: drop solo dispatch_id uniqueness, add project_id + composite

def _split_columns_and_constraints(body: str):
    """Split a CREATE TABLE body into top-level items by depth-1 commas.

    Quote- and paren-aware so commas inside CHECK(...) / DEFAULT(...) / string
    literals never split an item.
    """
    items, depth, cur = [], 0, []
    masked = _mask_quoted_sql(body)
    for ch, visible in zip(body, masked):
        if visible == "(":
            depth += 1
            cur.append(ch)
        elif visible == ")":
            depth -= 1
            cur.append(ch)
        elif visible == "," and depth == 0:
            items.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        items.append(tail)
    return items


def _is_table_constraint(item: str) -> bool:
    head = item.lstrip().split("(", 1)[0].strip().upper().split()
    return bool(head) and head[0] in _CONSTRAINT_KW


def _is_solo_unique_constraint(item: str, dispatch_cols) -> bool:
    """True iff *item* is a table-level UNIQUE(...) or PRIMARY KEY(...) keyed solely
    on dispatch_id (B — a table-level PRIMARY KEY(dispatch_id) is stripped the same
    way as UNIQUE so only the composite remains)."""
    s = item.strip()
    m = re.match(rf"(?is)^CONSTRAINT\s+({_IDENT_PATTERN})\s+(.*)$", s)
    if m:
        s = m.group(2).strip()
    if not re.match(r'(?is)^(?:UNIQUE|PRIMARY\s+KEY)\s*\(', s):
        return False
    return _referenced_columns(_paren_group(s, s.index("(")), dispatch_cols) == {"dispatch_id"}


def _constraint_is_composite(item: str, dispatch_cols) -> bool:
    s = item.strip()
    m = re.match(rf"(?is)^CONSTRAINT\s+({_IDENT_PATTERN})\s+(.*)$", s)
    if m:
        s = m.group(1).strip()
    if not re.match(r'(?is)^UNIQUE\s*\(', s):
        return False
    return _referenced_columns(_paren_group(s, s.index("(")), dispatch_cols) == {
        "dispatch_id", "project_id"}


_INLINE_UNIQUE_RE = re.compile(r'(?is)\s+UNIQUE(\s+ON\s+CONFLICT\s+\w+)?(?=\s|$)')
_INLINE_PK_RE = re.compile(
    r'(?is)\s+PRIMARY\s+KEY(\s+(?:ASC|DESC))?'
    r'(\s+ON\s+CONFLICT\s+\w+)?(\s+AUTOINCREMENT)?(?=\s|$)')
_PK_TOKEN_RE = re.compile(r'(?is)\bPRIMARY\s+KEY\b')


def _mask_string_literals(sql: str) -> str:
    """Mask SQL strings and quoted identifiers for keyword-token scans (N3)."""
    return _mask_quoted_sql(sql)


def _strip_inline_unique(coldef: str) -> str:
    """Strip an inline column UNIQUE *and* PRIMARY KEY constraint from a column-def (B).

    Removes a column-level UNIQUE (+ optional ON CONFLICT) and a column-level
    PRIMARY KEY (+ optional ASC/DESC, ON CONFLICT, AUTOINCREMENT) so a solo
    dispatch_id key cannot survive the rebuild while the repair reports success.
    Called only on the dispatch_id column-def; the composite UNIQUE(dispatch_id,
    project_id) is added separately.

    N3: the keyword scan is string-literal aware — a UNIQUE / PRIMARY KEY token
    inside a quoted DEFAULT (e.g. ``DEFAULT 'a UNIQUE b'``) is preserved verbatim;
    only a real keyword token outside any quoted string is stripped. Match spans
    are computed on the masked copy and deleted from the original right-to-left so
    earlier indices stay valid (the two keyword tokens never overlap).
    """
    masked = _mask_string_literals(coldef)
    spans = [m.span() for m in _INLINE_UNIQUE_RE.finditer(masked)]
    spans += [m.span() for m in _INLINE_PK_RE.finditer(masked)]
    for start, end in sorted(spans, reverse=True):
        coldef = coldef[:start] + " " + coldef[end:]
    return coldef.rstrip()


def _coldef_has_primary_key(coldef: str) -> bool:
    """True if *coldef* declares an inline PRIMARY KEY (string-literal aware, N4)."""
    return bool(_PK_TOKEN_RE.search(_mask_string_literals(coldef)))


def _constraint_is_primary_key(item: str) -> bool:
    """True if table-constraint *item* is a PRIMARY KEY(...) (optionally CONSTRAINT-named, N4)."""
    s = item.strip()
    m = re.match(rf"(?is)^CONSTRAINT\s+({_IDENT_PATTERN})\s+(.*)$", s)
    if m:
        s = m.group(1).strip()
    return bool(re.match(r'(?is)^PRIMARY\s+KEY\s*\(', s))


def _column_def_index(items, dispatch_cols, target: str) -> int:
    """Index of the column-def in *items* whose leading identifier is *target*."""
    for idx, item in enumerate(items):
        if _is_table_constraint(item):
            continue
        toks = _IDENT_RE.findall(item)
        if toks and toks[0].strip('"`[]').lower() == target:
            return idx
    return -1


def _promote_project_id_not_null(coldef: str) -> str:
    """Ensure an existing project_id column-def is NOT NULL (J); add no DEFAULT (A).

    R1.4 guarantees zero NULL/empty project_id rows at repair time, so promoting a
    nullable column to NOT NULL is safe and closes the tenant-isolation hole (SQLite
    treats NULLs as distinct in a UNIQUE). An already-NOT NULL column is returned
    verbatim (the real dispatches table keeps its existing default).
    """
    if re.search(r'(?is)\bNOT\s+NULL\b', coldef):
        return coldef
    return coldef.rstrip() + " NOT NULL"


def _composite_constraint_clause(col_defs, constraints, removed_solo_pk: bool) -> str:
    """The composite (dispatch_id, project_id) clause for the rebuilt table (N4).

    Emit it as PRIMARY KEY when the removed solo dispatch_id uniqueness WAS a
    PRIMARY KEY and no other PRIMARY KEY survives the rebuild — otherwise the table
    would lose its PK. Emit UNIQUE in every other case (a separate PK such as
    ``id INTEGER PRIMARY KEY`` remains, or the removed solo key was a plain UNIQUE).
    """
    has_remaining_pk = (any(_coldef_has_primary_key(c) for c in col_defs)
                        or any(_constraint_is_primary_key(c) for c in constraints))
    if removed_solo_pk and not has_remaining_pk:
        return "PRIMARY KEY(dispatch_id, project_id)"
    return "UNIQUE(dispatch_id, project_id)"


def _transform_create_table_sql(orig_sql: str, dispatch_cols, has_project_id: bool) -> str:
    """Mutate dispatches CREATE SQL → dispatches_new: drop solo dispatch_id
    uniqueness (inline/table UNIQUE *and* PRIMARY KEY, B) and add/keep project_id
    as NOT NULL — added with no vnx-dev default (A), existing-nullable promoted (J),
    existing NOT NULL untouched — plus the composite key. FKs, CHECK, collations,
    generated cols and the trailing table-option suffix (STRICT / WITHOUT ROWID, C)
    are preserved verbatim (R1.5).

    N4: the composite is added as PRIMARY KEY(dispatch_id, project_id) when the
    stripped solo key was a PRIMARY KEY and no other PK survives (so the table is
    not left PK-less); otherwise as UNIQUE(dispatch_id, project_id).

    SQLite requires every column-def to precede every table constraint, so the
    new project_id column is appended to the column section and the composite
    to the constraint section (never interleaved).
    """
    open_pos = orig_sql.index("(")
    close_pos = _matching_paren(orig_sql, open_pos)
    body = orig_sql[open_pos + 1:close_pos]
    suffix = orig_sql[close_pos + 1:].strip().rstrip(";").strip()       # C: table options
    items = _split_columns_and_constraints(body)
    did_idx = _column_def_index(items, dispatch_cols, "dispatch_id")
    pid_idx = _column_def_index(items, dispatch_cols, "project_id")
    col_defs, constraints, has_composite, removed_solo_pk = [], [], False, False
    for idx, item in enumerate(items):
        if _is_table_constraint(item):
            if _is_solo_unique_constraint(item, dispatch_cols):
                removed_solo_pk = removed_solo_pk or _constraint_is_primary_key(item)
                continue
            has_composite = has_composite or _constraint_is_composite(item, dispatch_cols)
            constraints.append(item)
        elif idx == did_idx:
            removed_solo_pk = removed_solo_pk or _coldef_has_primary_key(item)  # N4
            col_defs.append(_strip_inline_unique(item))                 # B: strip UNIQUE + PK
        elif idx == pid_idx:
            col_defs.append(_promote_project_id_not_null(item))         # J: always NOT NULL
        else:
            col_defs.append(item)
    if not has_project_id:
        col_defs.append("project_id TEXT NOT NULL")                     # A: no vnx-dev default
    if not has_composite:
        constraints.append(_composite_constraint_clause(               # N4: PK vs UNIQUE
            col_defs, constraints, removed_solo_pk))
    body_items = ",\n    ".join(col_defs + constraints)
    new_sql = "CREATE TABLE dispatches_new (\n    " + body_items + "\n)"
    return new_sql + (" " + suffix if suffix else "")                   # C: re-append options


# --- R1.3: dependent view/trigger discovery (transitive, quoted-aware) -------

def _object_references(sql: str, name: str) -> bool:
    """True if *sql* references identifier *name* (bare/"quoted"/`quoted`/[quoted])."""
    esc = re.escape(name)
    pat = re.compile(
        r'(?i)(?<![A-Za-z0-9_])(?:"%s"|`%s`|\[%s\]|%s)(?![A-Za-z0-9_])' % (esc, esc, esc, esc)
    )
    return bool(pat.search(sql or ""))


def _discover_dependent_views(conn: sqlite3.Connection):
    """Transitively discover views depending on dispatches (views-on-views,
    quoted identifiers). Return [(name, sql)] in base-first RECREATE order.
    """
    views = [(r[0], r[1]) for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view'")]
    dep, targets, changed = {}, {"dispatches"}, True
    while changed:
        changed = False
        for name, sql in views:
            if name not in dep and any(_object_references(sql, t) for t in targets):
                dep[name], changed = sql, True
                targets.add(name)
    ordered, remaining = [], dict(dep)
    while remaining:
        progressed = False
        for name in list(remaining):
            if not any(o != name and _object_references(remaining[name], o) for o in remaining):
                ordered.append((name, remaining.pop(name)))
                progressed = True
        if not progressed:  # cycle guard (views cannot be cyclic in SQLite)
            ordered.extend(remaining.items())
            break
    return ordered


def _triggers_for(conn: sqlite3.Connection, table_names):
    """Return [(name, sql)] for triggers whose tbl_name is in *table_names*."""
    if not table_names:
        return []
    rows = conn.execute(
        "SELECT name, sql, tbl_name FROM sqlite_master WHERE type='trigger'").fetchall()
    return [(r[0], r[1]) for r in rows if r[2] in table_names and r[1]]


def _standalone_indexes_to_recreate(conn: sqlite3.Connection, dispatch_cols):
    """Standalone CREATE INDEX objects on dispatches that are NOT solo
    dispatch_id uniques. Solo uniques are intentionally dropped (R1.1).
    """
    unique_flags = {r[1]: r[2] for r in conn.execute("PRAGMA index_list('dispatches')")}
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='dispatches' "
        "AND sql IS NOT NULL").fetchall()
    keep = []
    for name, sql in rows:
        if unique_flags.get(name) == 1 and _index_is_solo_dispatch_id(conn, name, dispatch_cols):
            continue
        keep.append((name, sql))
    return keep


def _checksum_order_clause(conn: sqlite3.Connection, table: str, cols) -> str:
    """Deterministic ORDER BY for the content checksum (L).

    Prefer ``id`` when the column is present; else ``rowid`` for a rowid table; else
    order by all copy columns (WITHOUT ROWID / alt-PK shapes). Raise an explicit
    error when none is available rather than crashing opaquely on a hard-coded
    ``ORDER BY id``.
    """
    table_cols = {r[1].lower() for r in conn.execute(f'PRAGMA table_info("{table}")')}
    if "id" in table_cols:
        return "ORDER BY id"
    try:
        conn.execute(f'SELECT rowid FROM "{table}" LIMIT 0')
        return "ORDER BY rowid"
    except sqlite3.OperationalError:
        pass
    if cols:
        return "ORDER BY " + ", ".join(f'"{c}"' for c in cols)
    raise RuntimeError(
        f"ADR-007 checksum: table {table!r} exposes no id, no rowid, and no copy "
        "columns to order by; cannot compute a deterministic checksum (L).")


def _content_checksum(conn: sqlite3.Connection, table: str, cols):
    """Deterministic (row_count, sha256) over *cols* of *table* (L: id|rowid|cols)."""
    collist = ", ".join(f'"{c}"' for c in cols) or "1"
    order = _checksum_order_clause(conn, table, cols)
    rows = conn.execute(f'SELECT {collist} FROM "{table}" {order}').fetchall()
    h = hashlib.sha256()
    for row in rows:
        h.update(repr(row).encode("utf-8"))
        h.update(b"\x1e")
    return len(rows), h.hexdigest()


def _build_dispatches_rebuild_plan(conn: sqlite3.Connection) -> dict:
    """Capture (read-only) everything needed to rebuild dispatches (R1.5/R1.3)."""
    col_info = _dispatch_table_columns(conn)
    cols = [c for c, _ in col_info]
    has_pid = "project_id" in cols
    copy_cols = [c for c, gen in col_info if not gen]                # exclude generated
    checksum_cols = [c for c in copy_cols if c != "project_id"]
    orig_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dispatches'"
    ).fetchone()[0]
    seq_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='dispatches'"
    ).fetchone() if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone() else None
    views = _discover_dependent_views(conn)
    return {
        "has_project_id": has_pid,
        "copy_cols": copy_cols,
        "checksum_cols": checksum_cols,
        "is_autoincrement": "AUTOINCREMENT" in orig_sql.upper(),
        "old_seq": seq_row[0] if seq_row else None,
        "new_table_sql": _transform_create_table_sql(orig_sql, cols, has_pid),
        "views": views,
        "table_triggers": _triggers_for(conn, {"dispatches"}),
        "view_triggers": _triggers_for(conn, {n for n, _ in views}),
        "indexes": _standalone_indexes_to_recreate(conn, cols),
        "before": _content_checksum(conn, "dispatches", checksum_cols),
    }


# --- R7.2: bounded retry/backoff on a locked DB ------------------------------

def _is_busy_or_locked(exc: sqlite3.OperationalError) -> bool:
    """Classify a retryable lock error (M).

    Prefer the structured ``sqlite_errorcode`` (Python 3.11+) against SQLITE_BUSY /
    SQLITE_LOCKED — including extended codes via the low-byte primary mask — so a
    localized or otherwise non-standard error message cannot bypass retry. Fall back
    to the legacy substring check when no usable error code is available.
    """
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return True
        if (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return True
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _begin_immediate_with_retry(conn: sqlite3.Connection, max_attempts: int = 6,
                                base_delay: float = 0.05, max_delay: float = 1.0) -> None:
    """BEGIN IMMEDIATE with bounded exponential backoff on BUSY/LOCKED (R7.2)."""
    delay, last = base_delay, None
    for attempt in range(1, max_attempts + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy_or_locked(exc):
                raise
            last = exc
            if attempt < max_attempts:
                time.sleep(delay)
                delay = min(delay * 2, max_delay)
    raise RuntimeError(
        f"ADR-007 repair could not acquire a write lock after {max_attempts} "
        f"BEGIN IMMEDIATE attempts (last error: {last}). Aborting — no infinite wait (R7.2)."
    )


# --- 12-step transactional rebuild (R1.6 / PRD §7.1) -------------------------

def _copy_rows_into_new(conn: sqlite3.Connection, plan: dict, project_id: str) -> None:
    """Step 6: copy rows into dispatches_new with the validated project_id."""
    collist = ", ".join(f'"{c}"' for c in plan["copy_cols"])
    if plan["has_project_id"]:
        conn.execute(
            f"INSERT INTO dispatches_new ({collist}) SELECT {collist} FROM dispatches")
    else:
        conn.execute(
            f"INSERT INTO dispatches_new ({collist}, project_id) "
            f"SELECT {collist}, ? FROM dispatches", (project_id,))


def _restore_dispatches_sequence(conn: sqlite3.Connection, plan: dict) -> None:
    """R1.2: sqlite_sequence[dispatches] = max(old_seq, current_max(id)).

    Applied AFTER the rename (DROP TABLE removes the old high-water row, so it
    must be re-asserted on the final table — empirically verified).
    """
    if not plan["is_autoincrement"]:
        return
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM dispatches").fetchone()[0]
    target = max(plan["old_seq"] or 0, max_id or 0)
    conn.execute("DELETE FROM sqlite_sequence WHERE name='dispatches'")
    conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES('dispatches', ?)", (target,))


def _drop_dependent_objects(conn: sqlite3.Connection, plan: dict) -> None:
    """Step 8a: drop dependent view-triggers then views (leaf-first)."""
    for name, _ in plan["view_triggers"]:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    for name, _ in reversed(plan["views"]):
        conn.execute(f'DROP VIEW IF EXISTS "{name}"')


def _recreate_dependent_objects(conn: sqlite3.Connection, plan: dict) -> None:
    """Step 10: recreate dependent objects in dependency order (F):
    indexes → views → table triggers → view triggers, so every view exists before
    any trigger whose body may reference it. SQL is recreated verbatim; any failure
    propagates → whole repair rolls back (R1.3).
    """
    for _, sql in plan["indexes"]:
        conn.execute(sql)
    for _, sql in plan["views"]:          # base-first; views before any trigger (F)
        conn.execute(sql)
    for _, sql in plan["table_triggers"]:
        conn.execute(sql)
    for _, sql in plan["view_triggers"]:
        conn.execute(sql)


def _assert_integrity_or_raise(conn: sqlite3.Connection) -> None:
    """Step 11: foreign_key_check + integrity_check; raise on ANY violation."""
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        raise RuntimeError(f"ADR-007 repair foreign_key_check violations: {fk}")
    res = conn.execute("PRAGMA integrity_check").fetchall()
    if res != [("ok",)]:
        raise RuntimeError(f"ADR-007 repair integrity_check failed: {res}")


def _rename_new_to_dispatches(conn: sqlite3.Connection, orig_legacy) -> None:
    """Step 9: rename dispatches_new → dispatches with legacy_alter_table ON (so
    SQLite does not auto-rewrite the dependent triggers/views we recreate verbatim),
    restoring the PRAGMA in a finally even when the RENAME raises (E — the PRAGMA is
    non-transactional and would otherwise leak ON to later migrations).
    """
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("ALTER TABLE dispatches_new RENAME TO dispatches")
    finally:
        conn.execute(f"PRAGMA legacy_alter_table={orig_legacy}")


def _assert_no_solo_unique_or_raise(conn: sqlite3.Connection) -> None:
    """Post-rebuild guard (B): fail (→ rollback) if any solo dispatch_id uniqueness
    survived the rebuild; never return a false success."""
    cols = [c for c, _ in _dispatch_table_columns(conn)]
    if _has_solo_dispatch_id_unique(conn, cols):
        raise RuntimeError(
            "ADR-007 repair did not eliminate solo dispatch_id uniqueness "
            "(a column/table PRIMARY KEY or UNIQUE survived the rebuild); rolling "
            "back rather than reporting a false success (B).")


def _execute_dispatches_rebuild(conn: sqlite3.Connection, plan: dict, project_id: str) -> None:
    """Steps 4–12 inside one BEGIN IMMEDIATE txn. foreign_keys is already OFF
    (caller, steps 1–2). On any failure the explicit transaction is rolled back
    (guarded so a rollback error never masks the original, K), leaving the DB
    consistent; the caller restores foreign_keys.
    """
    orig_legacy = conn.execute("PRAGMA legacy_alter_table").fetchone()[0]
    _begin_immediate_with_retry(conn)                                   # step 4
    try:
        _validate_existing_project_id_or_abort(conn, project_id)        # H: re-validate in-txn
        conn.execute(plan["new_table_sql"])                             # step 5
        _copy_rows_into_new(conn, plan, project_id)                     # step 6
        after = _content_checksum(conn, "dispatches_new", plan["checksum_cols"])
        if after != plan["before"]:
            raise RuntimeError(
                f"ADR-007 repair content checksum mismatch (before={plan['before']} "
                f"after={after}); aborting to prevent data loss.")
        _drop_dependent_objects(conn, plan)                             # step 8a
        conn.execute("DROP TABLE dispatches")                           # step 8b
        _rename_new_to_dispatches(conn, orig_legacy)                    # step 9 (E)
        _restore_dispatches_sequence(conn, plan)                        # step 7 (post-rename)
        _recreate_dependent_objects(conn, plan)                         # step 10
        _assert_integrity_or_raise(conn)                                # step 11
        _assert_no_solo_unique_or_raise(conn)                           # B: post-rebuild guard
        conn.execute("COMMIT")                                          # step 12
    except Exception:
        try:
            conn.execute("ROLLBACK")                                    # K: guarded rollback
        except Exception as rb_exc:
            # N2/K: never silently swallow the rollback failure (silent-except CI
            # gate). Log it, then fall through to re-raise the ORIGINAL exception
            # so K's contract holds — the original error propagates, not this one.
            warnings.warn(
                f"ADR-007 repair: ROLLBACK after error failed: {rb_exc}",
                stacklevel=2,
            )
        raise


def _verify_foreign_keys_restored(conn: sqlite3.Connection, expected: int) -> None:
    got = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if got != expected:
        raise RuntimeError(
            f"ADR-007 repair could not restore PRAGMA foreign_keys "
            f"(expected {expected}, got {got}).")


def _repair_dispatches_adr007(conn: sqlite3.Connection, project_id: str) -> bool:
    """Idempotent ADR-007 in-place repair of the dispatches table (PR-A1).

    Converts ANY single-column uniqueness on dispatch_id (inline UNIQUE,
    table-level UNIQUE(dispatch_id [ASC/DESC/COLLATE]), standalone/partial/
    expression UNIQUE INDEX, auto-index) into composite UNIQUE(dispatch_id,
    project_id) with zero data loss and full schema preservation (FKs, CHECK,
    collations, generated cols, triggers, dependent views, non-target indexes).
    No-op when already composite.

    Position (R2.2): PRE-MIGRATION step in run() — after _pytest_db_isolation_guard,
    before the numbered version walk. Implements the canonical 12-step procedure
    (PRD §7.1 / R1.6). *project_id* is the DB-path-anchored validated tenant
    identity (R3.1); never the 'vnx-dev' sentinel. Returns True if a rebuild ran.
    Cite ADR-007.

    ADR-005 audit (N5): this primitive deliberately emits no ledger event. The
    governed caller records the mutation after a successful rebuild, preserving
    unit-testability and the operator runbook §7.2 caller boundary.
    """
    if not _dispatches_needs_adr007_repair(conn):
        return False
    if conn.in_transaction:
        raise RuntimeError(
            "ADR-007 repair requires a connection with no open transaction; it "
            "refuses to commit the caller's uncommitted work (D). Commit or roll "
            "back before invoking the repair.")
    _validate_existing_project_id_or_abort(conn, project_id)            # R1.4 pre-mutation abort
    plan = _build_dispatches_rebuild_plan(conn)                         # read-only capture
    orig_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]         # step 1
    prev_iso = conn.isolation_level
    conn.isolation_level = None                                         # explicit-txn control
    try:
        conn.execute("PRAGMA foreign_keys=OFF")                         # step 2 (before BEGIN)
        _execute_dispatches_rebuild(conn, plan, project_id)             # steps 4–12
    finally:
        conn.execute("PRAGMA foreign_keys=ON" if orig_fk else "PRAGMA foreign_keys=OFF")
        conn.isolation_level = prev_iso
    _verify_foreign_keys_restored(conn, orig_fk)                        # step 12 verify
    return True


def _adr007_events_dir(db_path) -> Path:
    """Resolve the ADR-005 events directory without falling back to live data.

    An explicit VNX_DATA_DIR wins, matching repo event-store behavior and keeping
    temp-DB tests isolated. Otherwise events live beside the resolved DB's state
    directory under the same project-specific .vnx-data tree.
    """
    explicit = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    data_dir = (os.environ.get("VNX_DATA_DIR") or "").strip()
    if explicit and data_dir:
        return Path(data_dir).expanduser().resolve() / "events"
    db = Path(db_path).expanduser().resolve()
    return db.parent.parent / "events" if db.parent.name == "state" else db.parent / "events"


def _emit_adr007_repair_event(db_path, project_id: str) -> None:
    """Record the successful dispatches rebuild under ADR-005 and ADR-007."""
    audit_event_append(
        _adr007_events_dir(db_path),
        "adr007_dispatches_repaired",
        {
            "dispatch_table": "dispatches",
            "project_id": project_id,
            "rebuild_occurred": True,
            "adr": "ADR-007",
            "db_path": str(Path(db_path).expanduser().resolve()),
        },
    )


def _run_adr007_dispatches_repair(conn: sqlite3.Connection, db_path) -> None:
    """run() wiring: detect → resolve tenant → repair. project_id is resolved
    ONLY when a rebuild is actually needed, so a fresh/composite DB never
    requires identity resolution. Cite ADR-007.

    ADR-005 audit (N5): this GOVERNED CALLER records a temp-safe event only after
    a successful rebuild. The pure primitive remains event-I/O free. This is the
    operator runbook §7.2 caller boundary. See ADR-007:
    docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md.
    """
    if not _dispatches_needs_adr007_repair(conn):
        return
    project_id = _resolve_validated_project_id(db_path)
    if _repair_dispatches_adr007(conn, project_id):
        _emit_adr007_repair_event(db_path, project_id)
        print(f"  [adr007] dispatches rebuilt → composite UNIQUE(dispatch_id, "
              f"project_id), tenant={project_id!r}")


# ===========================================================================
# PR-A2 — manifest-backed migration preflights + numbered version reconciliation
# (R2.1, R2.2). The version reconciler is the AUTHORITATIVE replacement for the
# old name-based version trust: it runs BEFORE the numbered walk and aligns a
# lying `user_version` with the DB's true shape (schema_manifest). The per-version
# preflights below remain as defense-in-depth migration-apply-time guards, but
# their column/PK identities are now sourced from the declarative manifest
# (ADR-009 schema-first), not hand-typed name sets. Cite ADR-007 (tenant keys).
# ===========================================================================

def _table_pk(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """Ordered PK column names for *table* (empty when none / table absent)."""
    rows = [r for r in conn.execute(f"PRAGMA table_info('{table}')") if r[5] > 0]
    return tuple(r[1] for r in sorted(rows, key=lambda r: r[5]))


def _assert_required_tables(conn: sqlite3.Connection, tables, before: str) -> None:
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for required in tables:
        if required not in present:
            raise RuntimeError(
                f"Required table '{required}' not found. Run prior migrations before {before}.")


def _assert_track_precondition(conn: sqlite3.Connection, *, prereq_table: str,
                               prereq_cols, forbidden_table: str, forbidden_cols) -> None:
    """Manifest-backed migration precondition guard (replaces the name-based
    `_assert_tracks_vNN_intact` body). Raises when a prerequisite column from the
    PRIOR migration is MISSING, or a column the TARGET migration adds is already
    PRESENT (double-apply). Column identities come from schema_manifest deltas, not
    hand-typed sets (ADR-009). Messages name the specific offending column."""
    have_prereq = {r[1] for r in conn.execute(f"PRAGMA table_info('{prereq_table}')")}
    for col in prereq_cols:
        if col not in have_prereq:
            raise RuntimeError(
                f"{prereq_table} missing '{col}' column (prior migration not applied). "
                "Run the prior migration before this one.")
    have_forbidden = {r[1] for r in conn.execute(f"PRAGMA table_info('{forbidden_table}')")}
    for col in forbidden_cols:
        if col in have_forbidden:
            raise RuntimeError(
                f"{forbidden_table} already has '{col}' column. Migration should be "
                "skipped (user_version should already be advanced).")


def _emit_version_reconcile_event(db_path, claimed: int, corrected: int, violations) -> None:
    """ADR-005 ledger: record a user_version downgrade (a state mutation) after it
    succeeds. Temp-safe via _adr007_events_dir (respects VNX_DATA_DIR_EXPLICIT)."""
    audit_event_append(
        _adr007_events_dir(db_path),
        "schema_version_reconciled",
        {
            "claimed_user_version": claimed,
            "corrected_user_version": corrected,
            "first_violations": list(violations)[:5],
            "adr": "ADR-009",
            "db_path": str(Path(db_path).expanduser().resolve()),
        },
    )


def _run_version_reconciliation(conn: sqlite3.Connection, db_path) -> None:
    """R2.1: validate the claimed user_version against the invariant manifest and
    DOWNGRADE to the highest fully-satisfied version on mismatch, so the numbered
    walk re-applies the exposed migrations. Runs AFTER the ADR-007 repair, BEFORE
    the walk (R2.2). Genuine corruption with no satisfiable lower version raises.

    A2-N2 (PRD D3, rollback-on-ledger-failure): the downgrade and its ADR-005 ledger
    event are ATOMIC. The reconcile (which writes `PRAGMA user_version`) runs inside an
    explicit BEGIN IMMEDIATE and the event is emitted BEFORE the COMMIT, so a ledger
    emit failure ROLLS BACK the downgrade — nothing is committed without its audit
    event. A bare conn.commit() after the write does NOT suffice: `PRAGMA user_version`
    auto-commits in autocommit mode, so the explicit transaction is what makes the
    mutation revertible. Cite ADR-005 (audit ledger) + ADR-009 (schema-first)."""
    prev_iso = conn.isolation_level
    conn.isolation_level = None                         # full manual transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = schema_manifest.reconcile_user_version(conn)
            if not result.reconciled:
                conn.execute("ROLLBACK")               # no mutation; release the lock
                return
            _emit_version_reconcile_event(             # D3: emit BEFORE the commit
                db_path, result.claimed, result.corrected, result.violations)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")                   # emit/reconcile failure → revert downgrade
            raise
    finally:
        conn.isolation_level = prev_iso
    print(f"  [reconcile] user_version {result.claimed} → {result.corrected}: claimed "
          f"version failed its manifest; re-walk will re-apply. first violations: "
          f"{list(result.violations)[:2]}")


def _assert_manifest_converged(conn: sqlite3.Connection) -> None:
    """Oscillation guard (R2.1): after the numbered walk, the terminal version's
    manifest MUST hold. If a downgrade+re-walk did not converge, fail loudly rather
    than leave a silently-broken DB (or loop on the next run). Cite ADR-009."""
    final = schema_migration.get_user_version(conn)
    if final not in schema_manifest.SCHEMA_MANIFEST:
        return
    violations = schema_manifest.validate_db_at_version(conn, final)
    if violations:
        raise schema_manifest.SchemaReconciliationError(
            f"version reconciliation did not converge: after the migration walk "
            f"user_version={final} but the v{final} invariant manifest still fails "
            f"({violations[:3]}). Aborting rather than looping (R2.1).")


# ---------------------------------------------------------------------------
# Step 0: PRAGMA pre-flight — guard against schema drift before rebuild
# ---------------------------------------------------------------------------

def _assert_dispatches_schema_intact(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}
    expected = {'id', 'dispatch_id', 'project_id', 'state', 'terminal_id', 'track', 'priority',
                'pr_ref', 'gate', 'attempt_count', 'bundle_path', 'created_at', 'updated_at',
                'expires_after', 'metadata_json'}
    missing = expected - cols
    extra = cols - expected
    if missing or extra:
        raise RuntimeError(
            f'dispatches schema drift: missing={missing} extra={extra}. '
            'Refusing rebuild — please add migration logic for the new columns first.'
        )
    if not _has_composite_unique(conn):
        raise RuntimeError(
            'dispatches missing UNIQUE(dispatch_id, project_id) — '
            'was added in migration 0017, must be preserved'
        )


# Register PRAGMA pre-flight for 0022: any call to apply_script_if_below(22, ...)
# triggers the column assertion, even when invoked outside of run().
schema_migration.register_preflight(22, _assert_dispatches_schema_intact)


# ---------------------------------------------------------------------------
# Step 1: apply 0022 migration
# ---------------------------------------------------------------------------

def apply_migration(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0022_track_layer.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 22:
        print(f"  [skip] migration 0022 already applied (user_version={current_version})")
        return

    _assert_dispatches_schema_intact(conn)
    print("  [apply] migration 0022_track_layer.sql ...")
    schema_migration.apply_script_if_below(conn, 22, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Step 2: PRAGMA pre-flight for 0024 — assert v22 tracks schema intact
# ---------------------------------------------------------------------------

def _assert_tracks_v22_intact(conn: sqlite3.Connection) -> None:
    """Manifest-backed preflight for 0024: tracks must be in the v22 single-column-PK
    state. A composite (track_id, project_id) PK means 0024 already applied. Required
    columns + PK shape come from schema_manifest (ADR-009), not a hand-typed set.
    Codex peer-review §3: check columns AND key shape, not just column names.
    """
    _assert_required_tables(
        conn, ('tracks', 'track_phase_history', 'track_dependencies', 'track_open_items'),
        before='0024')
    cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    missing = set(schema_manifest.table_at(22, 'tracks').columns) - cols
    if missing:
        raise RuntimeError(
            f"tracks schema drift before v24 migration: missing columns={missing}. "
            "Expected v22 state.")
    pk = _table_pk(conn, 'tracks')
    if pk == schema_manifest.table_pk_at(24, 'tracks'):
        raise RuntimeError(
            "tracks already has the v24 composite (track_id, project_id) PK. "
            "Migration 0024 should be skipped (user_version should be >= 24).")
    if pk != schema_manifest.table_pk_at(22, 'tracks'):
        raise RuntimeError(
            f"tracks PK {pk} is neither v22 single-column nor v24 composite — "
            "schema drift before 0024.")


schema_migration.register_preflight(24, _assert_tracks_v22_intact)


# ---------------------------------------------------------------------------
# Step 3: orphan warning check before v24 migration
# ---------------------------------------------------------------------------

def _warn_orphan_child_rows(conn: sqlite3.Connection) -> None:
    """Check for orphan child rows before v24 migration and warn. Does not block."""
    checks = [
        ("track_phase_history", "track_phase_history", "track_id"),
        ("track_dependencies (from_track_id)", "track_dependencies", "from_track_id"),
        ("track_dependencies (to_track_id)", "track_dependencies", "to_track_id"),
        ("track_open_items", "track_open_items", "track_id"),
    ]
    for label, table, col in checks:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} NOT IN (SELECT track_id FROM tracks)"
        ).fetchone()[0]
        if count:
            warnings.warn(
                f"v24 migration: {count} orphan row(s) in {label} "
                f"({col} not in tracks) will be skipped",
                UserWarning,
                stacklevel=3,
            )


# ---------------------------------------------------------------------------
# Stale-FK repair: strip dispatches.track -> tracks(track_id) FK if present
# ---------------------------------------------------------------------------

def _strip_stale_dispatches_track_fk(conn: sqlite3.Connection) -> None:
    """Remove the stale dispatches.track -> tracks(track_id) FK via table rebuild.

    The superseded 0023_dispatches_fk.sql added this FK before it was removed
    in FUT-1 Option B scope-shrink. If an operator applied that migration before
    upgrading, the tracks RENAME in 0024 breaks unless the FK is stripped first.
    This repair is safe: the FK existed only in the operator-side superseded
    0023 application and carries no semantic constraint we need to preserve.
    """
    col_names = [row[1] for row in conn.execute("PRAGMA table_info('dispatches')")]
    col_list = ", ".join(col_names)

    conn.execute("ALTER TABLE dispatches RENAME TO dispatches_pre_v24_strip")
    conn.execute("""
        CREATE TABLE dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
            state           TEXT    NOT NULL DEFAULT 'proposed'
                                    CHECK (state IN (
                                        'proposed', 'ready', 'active', 'completed', 'failed',
                                        'queued', 'claimed', 'delivering', 'accepted', 'running',
                                        'timed_out', 'failed_delivery', 'expired', 'recovered',
                                        'dead_letter'
                                    )),
            terminal_id     TEXT,
            track           TEXT,
            priority        TEXT    DEFAULT 'P2',
            pr_ref          TEXT,
            gate            TEXT,
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            bundle_path     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after   TEXT,
            metadata_json   TEXT    DEFAULT '{}',
            operator_approved_at TEXT,
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute(
        f"INSERT INTO dispatches ({col_list}) SELECT {col_list} FROM dispatches_pre_v24_strip"
    )
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'dispatches'")
    conn.execute("""
        INSERT INTO sqlite_sequence(name, seq)
        SELECT 'dispatches',
               COALESCE(
                   (SELECT seq FROM sqlite_sequence WHERE name = 'dispatches_pre_v24_strip'),
                   (SELECT MAX(id) FROM dispatches),
                   0
               )
    """)
    conn.execute("DROP TABLE dispatches_pre_v24_strip")


# ---------------------------------------------------------------------------
# v22 timestamp dedup: prevent UNIQUE(track_id, project_id, occurred_at) rejection
# ---------------------------------------------------------------------------

def _dedupe_v22_phase_history_timestamps(conn: sqlite3.Connection) -> None:
    """v22 occurred_at default is millisecond precision; bulk transitions
    can share timestamps. Composite UNIQUE in v24 would reject those.
    Append microsecond offset (.0001Z, .0002Z, ...) to make timestamps
    distinct while preserving chronological order via stable id ordering.

    KNOWN LIMITATIONS (tracked in OI-008 + GitHub roadmap):
    - Dedupe-suffix collision possible if pre-existing v22 data has
      timestamps matching the post-dedupe format (.NNN0001Z). Real-world
      probability near-zero for default v22 strftime '%f' timestamps.
    - Suffix '.0001Z' does not sort lex with '.NNNZ'. Chronological
      ordering preserved via id sequence, not timestamp string sort.
    """
    rows = conn.execute("""
        SELECT id, occurred_at,
               ROW_NUMBER() OVER (PARTITION BY track_id, occurred_at ORDER BY id) - 1 AS offset
        FROM track_phase_history
        ORDER BY id
    """).fetchall()
    for row_id, occurred_at, offset in rows:
        if offset > 0:
            base = occurred_at.rstrip("Z")
            new_ts = (
                f"{base}{offset:04d}Z"
                if "." in base.rsplit("T", 1)[-1]
                else f"{base}.{offset:04d}Z"
            )
            conn.execute(
                "UPDATE track_phase_history SET occurred_at = ? WHERE id = ?",
                (new_ts, row_id),
            )


# ---------------------------------------------------------------------------
# Step 4: apply 0024 migration
# ---------------------------------------------------------------------------

def apply_migration_v24(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0024_tracks_tenant_scoping.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 24:
        print(f"  [skip] migration 0024 already applied (user_version={current_version})")
        return

    _assert_tracks_v22_intact(conn)

    # Detect and strip stale FK from superseded 0023_dispatches_fk.sql.
    # If operator applied that migration, dispatches has a FK to tracks(track_id)
    # that would break the tracks RENAME in 0024.
    stale_fks = [
        row for row in conn.execute("PRAGMA foreign_key_list('dispatches')")
        if row[2] == "tracks" and row[4] == "track_id"
    ]
    if stale_fks:
        warnings.warn(
            "Detected stale dispatches.track -> tracks(track_id) FK from superseded "
            "0023_dispatches_fk.sql. Stripping FK before applying 0024.",
            UserWarning,
            stacklevel=2,
        )
        _strip_stale_dispatches_track_fk(conn)

    _dedupe_v22_phase_history_timestamps(conn)
    _warn_orphan_child_rows(conn)
    print("  [apply] migration 0024_tracks_tenant_scoping.sql ...")
    schema_migration.apply_script_if_below(conn, 24, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Step 5: PRAGMA pre-flight for 0027 — assert composite-key tracks intact
# ---------------------------------------------------------------------------

def _ensure_dispatches_output_columns(conn: sqlite3.Connection) -> None:
    """Idempotently ensure dispatches carries output_ref + output_kind columns.

    Migration 0027 creates the deliverables VIEW which reads dispatches.output_ref
    and dispatches.output_kind. On the live DB these columns were added by the
    structural-doctor repair step, but a fresh DB that arrives at v24 without the
    structural-doctor pass (or via tests) will not have them. The VIEW creation
    does not fail at DDL time (SQLite resolves view columns at query time), but
    any SELECT from deliverables would fail.

    This preflight adds the columns additively when they are absent, then back-
    fills output_ref=pr_ref, output_kind='pr' for rows where pr_ref is set.
    It is idempotent: column-existence checks guard the ALTER TABLE calls so
    they are never attempted twice, and the UPDATE is a no-op after the first run.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}

    if "output_ref" not in cols:
        conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    if "output_kind" not in cols:
        conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    if "operator_approved_at" not in cols:
        conn.execute("ALTER TABLE dispatches ADD COLUMN operator_approved_at TEXT")

    conn.execute(
        "UPDATE dispatches SET output_ref = pr_ref, output_kind = 'pr' "
        "WHERE pr_ref IS NOT NULL AND output_ref IS NULL"
    )


def _assert_tracks_v24_intact(conn: sqlite3.Connection) -> None:
    """Manifest-backed preflight for 0027: tracks must be at the composite-key (v24+)
    state before adding horizon. 0027 is additive (ALTER ADD COLUMN + a VIEW), so it
    needs the composite (track_id, project_id) PK and horizon ABSENT. The PK and the
    introduced column come from schema_manifest (ADR-009); it must NOT run on a
    pre-v24 single-column-PK tracks table.
    """
    _assert_required_tables(conn, ('tracks',), before='0027')
    if _table_pk(conn, 'tracks') != schema_manifest.table_pk_at(24, 'tracks'):
        raise RuntimeError(
            "tracks missing the v24 composite (track_id, project_id) PK. "
            "Run migration 0024 before 0027.")
    _assert_track_precondition(
        conn, prereq_table='tracks', prereq_cols=(),
        forbidden_table='tracks',
        forbidden_cols=schema_manifest.columns_introduced_at(27, 'tracks'))


schema_migration.register_preflight(27, _ensure_dispatches_output_columns)
schema_migration.register_preflight(27, _assert_tracks_v24_intact)


# ---------------------------------------------------------------------------
# Step 6: apply 0027 migration
# ---------------------------------------------------------------------------

def apply_migration_v27(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 27:
        print(f"  [skip] migration 0027 already applied (user_version={current_version})")
        return

    _assert_tracks_v24_intact(conn)
    print("  [apply] migration 0027_planning_horizon_and_deliverable_view.sql ...")
    schema_migration.apply_script_if_below(conn, 27, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Step 7: preflight + apply 0028 migration (tracks.derived_status)
# ---------------------------------------------------------------------------

def _assert_tracks_v27_intact(conn: sqlite3.Connection) -> None:
    """Manifest-backed preflight for 0028: tracks has horizon (v27) and not yet
    derived_status (double-apply guard). Column identities from schema_manifest
    (the v27 and v28 tracks deltas) per ADR-009."""
    _assert_track_precondition(
        conn, prereq_table='tracks',
        prereq_cols=schema_manifest.columns_introduced_at(27, 'tracks'),
        forbidden_table='tracks',
        forbidden_cols=schema_manifest.columns_introduced_at(28, 'tracks'))


schema_migration.register_preflight(28, _assert_tracks_v27_intact)


def apply_migration_v28(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0028_tracks_derived_status.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 28:
        print(f"  [skip] migration 0028 already applied (user_version={current_version})")
        return

    _assert_tracks_v27_intact(conn)
    print("  [apply] migration 0028_tracks_derived_status.sql ...")
    schema_migration.apply_script_if_below(conn, 28, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Step 8: preflight + apply 0029 migration (tracks.track_type + next_action_owner)
# ---------------------------------------------------------------------------

def _assert_tracks_v28_intact(conn: sqlite3.Connection) -> None:
    """Manifest-backed preflight for 0029: tracks has derived_status (v28) and not yet
    track_type (double-apply guard). Column identities come from schema_manifest (the
    v28 and v29 tracks deltas) per ADR-009 — a secondary idempotency guard beyond the
    user_version check in apply_script_if_below. Raises 'missing derived_status' /
    'already has track_type' for the specific offending column."""
    _assert_track_precondition(
        conn, prereq_table='tracks',
        prereq_cols=schema_manifest.columns_introduced_at(28, 'tracks'),
        forbidden_table='tracks',
        forbidden_cols=schema_manifest.columns_introduced_at(29, 'tracks'))


schema_migration.register_preflight(29, _assert_tracks_v28_intact)


def apply_migration_v29(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0029_track_type_discriminator.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 29:
        print(f"  [skip] migration 0029 already applied (user_version={current_version})")
        return

    _assert_tracks_v28_intact(conn)
    print("  [apply] migration 0029_track_type_discriminator.sql ...")
    schema_migration.apply_script_if_below(conn, 29, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Step 9: preflight + apply 0030 migration (track_open_items.resolved_at)
# ---------------------------------------------------------------------------

def _assert_tracks_v29_intact(conn: sqlite3.Connection) -> None:
    """Manifest-backed preflight for 0030: tracks has track_type (v29) and
    track_open_items has not yet gained resolved_at (double-apply guard). Column
    identities come from schema_manifest (the v29 tracks delta + v30 track_open_items
    delta) per ADR-009."""
    _assert_track_precondition(
        conn, prereq_table='tracks',
        prereq_cols=schema_manifest.columns_introduced_at(29, 'tracks'),
        forbidden_table='track_open_items',
        forbidden_cols=schema_manifest.columns_introduced_at(30, 'track_open_items'))


schema_migration.register_preflight(30, _assert_tracks_v29_intact)


def apply_migration_v30(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0030_track_oi_resolved_at.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 30:
        print(f"  [skip] migration 0030 already applied (user_version={current_version})")
        return

    _assert_tracks_v29_intact(conn)
    print("  [apply] migration 0030_track_oi_resolved_at.sql ...")
    schema_migration.apply_script_if_below(conn, 30, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _run_numbered_walk(conn: sqlite3.Connection, project_root: Path) -> None:
    """Step C of run(): apply the numbered migrations 0022 → 0030 in order, each
    idempotent via user_version, committing after each. Preflights (steps 3-11) are
    manifest-backed (schema_manifest). See the module docstring for the full ordering.
    """
    apply_migration(conn, project_root)       # 0022 — track tables; dispatches w/o track FK
    conn.commit()
    apply_migration_v24(conn, project_root)   # 0024 — composite (track_id, project_id) PK/FK
    conn.commit()
    apply_migration_v27(conn, project_root)   # 0027 — tracks.horizon + deliverables view
    conn.commit()
    apply_migration_v28(conn, project_root)   # 0028 — tracks.derived_status advisory column
    conn.commit()
    apply_migration_v29(conn, project_root)   # 0029 — tracks.track_type + next_action_owner
    conn.commit()
    apply_migration_v30(conn, project_root)   # 0030 — track_open_items.resolved_at + reason
    conn.commit()


def run(project_root: Path | None = None) -> None:
    """Apply track layer migrations: 0022, 0024, 0027, 0028, 0029, 0030."""
    if project_root is None:
        project_root = resolve_project_root(__file__)
    _pytest_db_isolation_guard(project_root)

    state_dir = project_root / ".vnx-data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"

    if not db_path.exists():
        raise FileNotFoundError(
            f"runtime_coordination.db not found at {db_path}\n"
            "Run `vnx init` or initialize the schema first."
        )

    print(f"\nVNX migrate_future_system — db: {db_path}")

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        # (A) ADR-007 pre-migration repair (R1/R2.2/R3.1): convert any single-column
        # dispatch_id uniqueness into composite UNIQUE(dispatch_id, project_id).
        # No-op when already composite; resolves the tenant only when needed.
        _run_adr007_dispatches_repair(conn, db_path)

        # (B) Numbered version reconciliation (R2.1/R2.2): validate the claimed
        # user_version against the invariant manifest; a DB that LIES about its
        # version is downgraded to its true version so the walk re-applies what the
        # downgrade exposed. Runs AFTER the repair, BEFORE the numbered walk (PRD §6).
        _run_version_reconciliation(conn, db_path)

        if schema_migration.get_user_version(conn) < 22:
            _assert_dispatches_schema_intact(conn)

        # (C) Numbered migration walk: 0022 → 0030 (each idempotent via user_version).
        _run_numbered_walk(conn, project_root)

        # (D) Oscillation guard (R2.1): the terminal version's manifest MUST hold;
        # a downgrade+re-walk that did not converge aborts here rather than looping.
        _assert_manifest_converged(conn)

        print(f"\n  Migration complete. Schema at user_version={schema_migration.get_user_version(conn)}.\n")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\n  [ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
