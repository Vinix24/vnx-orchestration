#!/usr/bin/env python3
"""migrate_future_system.py — apply track layer migrations (schema only).

run() ordering (R2.2 — repair → version-reconcile → numbered walk):
  A. ADR-007 dispatches repair (`_run_adr007_dispatches_repair`, PR-A1) — the ad-hoc
     in-place composite-UNIQUE rebuild, a PRE-walk step. NOT a numbered migration.
  B. Numbered version reconciliation (`_run_version_reconciliation`, PR-A2) — validate
     the DB's CLAIMED `user_version` against the declarative invariant manifest
     (scripts/lib/schema_manifest.py). A DB that LIES about its version (claims v31 but
     is physically v27) is DOWNGRADED to the exact highest version whose invariants
     fully hold, so the numbered walk re-applies whatever the downgrade exposes.
  C. Numbered migration walk (0022 → 0031), each idempotent via user_version.
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
  13. Apply schemas/migrations/0031_runtime_tenant_fk_repair.sql (idempotent)

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
import tenant_stamping


# ---------------------------------------------------------------------------
# Test isolation guard (R8.6 / PR-0) — active only under pytest
# ---------------------------------------------------------------------------

def _resolve_data_dir(project_root: Path, *, project_root_provided: bool = False) -> Path:
    """Resolve the VNX data directory for migrate_future_system.run().

    Priority (mirrors dispatch_cli.py:69-74):
    1. Explicit project_root argument (project_root_provided=True): always use
       project_root / ".vnx-data". An explicit caller argument wins.
    2. VNX_DATA_DIR_EXPLICIT=1 + VNX_DATA_DIR set (and no explicit project_root):
       use VNX_DATA_DIR directly. This allows targeting ~/.vnx-data/<pid>/
       (central store) without changing project_root.
    3. Fallback: project_root / ".vnx-data" (legacy local layout).
    """
    if project_root_provided:
        return (project_root / ".vnx-data").resolve()
    explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    explicit_val = (os.environ.get("VNX_DATA_DIR") or "").strip()
    if explicit_flag and explicit_val:
        return Path(explicit_val).resolve()
    return (project_root / ".vnx-data").resolve()


def _pytest_db_isolation_guard(project_root: Path) -> None:
    """Refuse to open any DB when running under pytest without explicit isolation.

    Active only when PYTEST_CURRENT_TEST is set (i.e. inside a pytest process).
    Two conditions must hold:
    1. VNX_DATA_DIR_EXPLICIT=1 must be set.
    2. The resolved data_dir (via _resolve_data_dir) MUST be under
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
    # Flag is set — validate the resolved data dir is actually temp-owned.
    # The guard always checks the VNX_DATA_DIR path (not project_root) because
    # the guard's job is to prevent opening ~/.vnx-data regardless of how the
    # caller arrived here.
    explicit_val = (os.environ.get("VNX_DATA_DIR") or "").strip()
    if explicit_val:
        data_root = Path(explicit_val).resolve()
    else:
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
            "Set VNX_DATA_DIR to a pytest tmp_path-based path, or pass an explicit "
            "project_root under tempfile.gettempdir() to migrate_future_system.run()."
        )


# ===========================================================================
# ADR-007 dispatches repair (PR-A1) — general-purpose, idempotent, lossless
# in-place rebuild that converts ANY single-column uniqueness on dispatch_id
# into the composite UNIQUE(dispatch_id, project_id).
#
# Position in run() (R2.2): runs as a PRE-MIGRATION step — after the
# _pytest_db_isolation_guard, BEFORE the numbered version walk (0022→0031).
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

#: Safe tenant-slug grammar (codex injection fix). A resolved project_id gets inlined
#: into DDL DEFAULTs / row-copy where a bind param can't reach, so it must be a slug
#: with no quote/semicolon/paren/whitespace. Bounded length caps abuse. Matches every
#: legitimate VNX id (`^[a-z][a-z0-9-]{1,31}$` and `vnx-<hex>`), rejects hostile input
#: like `x'); DROP TABLE tracks;--`.
_SAFE_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _mask_quoted_sql(sql: str) -> str:
    """Mask quoted content AND SQL comments while preserving length.

    Handles SQL strings plus double-quoted, backtick-quoted, and bracket-quoted
    identifiers (including doubled closing delimiters), and additionally masks
    ``-- line`` comments and ``/* block */`` comments before any paren-balance,
    comma-split, or keyword scan runs against the masked copy. A ``)`` / ``(`` /
    ``,`` living inside a comment (or a doubled string delimiter) must never
    perturb `_matching_paren`, `_split_columns_and_constraints`, or the inline
    keyword scans — sqlite_master stores CREATE TABLE/INDEX text verbatim, so a
    legacy schema carrying a comment with an unbalanced paren was raising
    "unbalanced parentheses in SQL" (B.1). Comment bytes (delimiters included)
    become the mask byte; string/identifier quote delimiters are preserved so the
    downstream scanners still see the token boundaries. Length is preserved
    exactly (`zip(body, masked)` in `_split_columns_and_constraints` depends on
    it). This is a bounded scanner for the CREATE TABLE/INDEX helpers, not a full
    SQL parser.
    """
    out, i, n = list(sql), 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        # -- line comment: mask through (but not including) the newline. A `--`
        # inside a string is consumed by the quote inner-loop below, never here.
        if ch == "-" and nxt == "-":
            while i < n and sql[i] != "\n":
                out[i] = "\x00"
                i += 1
            continue
        # /* block comment */: mask through the closing delimiter, inclusive.
        if ch == "/" and nxt == "*":
            out[i] = out[i + 1] = "\x00"
            i += 2
            while i < n:
                if sql[i] == "*" and i + 1 < n and sql[i + 1] == "/":
                    out[i] = out[i + 1] = "\x00"
                    i += 2
                    break
                out[i] = "\x00"
                i += 1
            continue
        if ch not in ("'", '"', "`", "["):
            i += 1
            continue
        closer = "]" if ch == "[" else ch
        i += 1
        while i < n:
            if sql[i] != closer:
                out[i] = "\x00"
                i += 1
                continue
            if i + 1 < n and sql[i + 1] == closer:
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
    resolved = distinct.pop()
    # Charset validation (codex injection fix): the resolved tenant is inlined into
    # DDL DEFAULTs / index names / row-copy where a bind parameter cannot reach, so a
    # marker or env value carrying a quote/semicolon/paren/space could inject SQL. A
    # legitimate VNX project_id is a slug; reject anything outside the safe grammar
    # BEFORE it is used anywhere. This is the last line of defense — _sql_quote_literal
    # and bound parameters still apply at the use-sites.
    if not _SAFE_PROJECT_ID_RE.match(resolved):
        raise RuntimeError(
            f"ADR-007 fail-closed: resolved project_id {resolved!r} is not a valid "
            f"tenant slug (must match {_SAFE_PROJECT_ID_RE.pattern}). Refusing to use "
            "an unsafe/injectable identity."
        )
    return resolved


def _sql_quote_literal(value: str) -> str:
    """Return *value* as a safely single-quoted SQL string literal (R3.1/ADR-007).

    Used to inline a fail-closed-RESOLVED project_id into DDL where a bind
    parameter is impossible (a CREATE TABLE column DEFAULT, or the static-file
    row-copy substitution). Single quotes are doubled. Callers pass only an
    already-validated project_id (`^[a-z][a-z0-9-]{1,31}$`), so this is defense in
    depth, never the primary guard. NEVER used to inline the 'vnx-dev' sentinel.
    """
    return "'" + value.replace("'", "''") + "'"


def _render_static_0031_sql_with_pid(sql: str, project_id: str) -> str:
    """Substitute the tenant placeholder in the static 0031 DDL with the RESOLVED
    project_id (D3, ADR-007 damage-causer fix).

    `schemas/migrations/0031_runtime_tenant_fk_repair.sql` uses ``'vnx-dev'`` at
    rest as a placeholder for BOTH the column DEFAULT and the four row-copy
    ``INSERT ... SELECT`` literals. Applying that verbatim on a non-vnx-dev store
    (e.g. sales-copilot) would stamp every rebuilt runtime row with the wrong
    tenant — corruption. The clean-v30-legacy path therefore renders the file with
    the DB-path-anchored, fail-closed-resolved tenant identity before executing
    it. `project_id` is never the sentinel: it comes from
    `_resolve_validated_project_id`. Idempotent for a store whose real tenant IS
    'vnx-dev' (the substitution is then a no-op replacement of like-for-like).
    """
    return sql.replace("'vnx-dev'", _sql_quote_literal(project_id))


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
    than leave a silently-broken DB (or loop on the next run). The independent
    foreign_key_check closes any gap in the declarative manifest. Cite ADR-009."""
    try:
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError as exc:
        raise schema_manifest.SchemaReconciliationError(
            f"terminal convergence foreign_key_check failed structurally: {exc}"
        ) from exc
    if fk_violations:
        raise schema_manifest.SchemaReconciliationError(
            f"terminal convergence foreign_key_check violations: {fk_violations[:5]}")

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
    """Assert that dispatches carries all columns required by the 0022 rebuild.

    Only raises on MISSING required columns. Extra columns (e.g. VNX-canonical v4
    forward-scaffolding: task_class, target_type, target_id, channel_origin,
    intelligence_payload) are explicitly allowed — they are preserved, not dropped.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}
    expected = {'id', 'dispatch_id', 'project_id', 'state', 'terminal_id', 'track', 'priority',
                'pr_ref', 'gate', 'attempt_count', 'bundle_path', 'created_at', 'updated_at',
                'expires_after', 'metadata_json'}
    missing = expected - cols
    if missing:
        raise RuntimeError(
            f'dispatches schema drift: missing required columns={missing}. '
            'Refusing rebuild — the 0022 INSERT...SELECT depends on these columns.'
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
# 0022 shape-detection: is dispatches ALREADY in post-v22 form?
# ---------------------------------------------------------------------------

#: Columns that 0022 adds to dispatches (operator_approved_at).
#: When this column is present AND the composite UNIQUE holds, the dispatches
#: table is already in v22-shape — the rebuild is a no-op and must be skipped.
_V22_DISPATCHES_NEW_COLS = frozenset({"operator_approved_at"})

#: Required 15-column base that 0022's INSERT…SELECT depends on.
_V22_DISPATCHES_REQUIRED = frozenset({
    'id', 'dispatch_id', 'project_id', 'state', 'terminal_id', 'track', 'priority',
    'pr_ref', 'gate', 'attempt_count', 'bundle_path', 'created_at', 'updated_at',
    'expires_after', 'metadata_json',
})


def _dispatches_already_v22_shape(conn: sqlite3.Connection) -> bool:
    """True when dispatches is already in post-v22 shape: composite UNIQUE + v22 new cols.

    MC's runtime_coordination.db is a concrete case: it was hand-migrated to v22-shape
    (added operator_approved_at + UNIQUE(dispatch_id, project_id)) before the schema walk
    ran.  The 0022 dispatches rebuild is therefore a no-op for that store — and must be
    skipped to avoid dropping the 5 VNX-canonical v4 forward-scaffolding columns
    (task_class, target_type, target_id, channel_origin, intelligence_payload).

    The detection uses two orthogonal signals:
      1. Column presence: operator_approved_at (the column 0022 introduces).
      2. Constraint: UNIQUE(dispatch_id, project_id) already composite.

    Both must hold.  A store that has the composite but NOT operator_approved_at
    has a partial state that we should let the full rebuild handle.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}
    return (
        _V22_DISPATCHES_NEW_COLS.issubset(cols)
        and _has_composite_unique(conn)
    )


# ---------------------------------------------------------------------------
# 0022 DDL fragments: applied conditionally by the skip-rebuild and dynamic paths.
# All statements use CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
# so they are safe to run on any store regardless of prior state.
#
# Splitting into two parts is required because idx_dispatches_ready references
# operator_approved_at, which only exists on dispatches AFTER the v22 rebuild.
# The dynamic rebuild path applies _0022_TRACK_TABLES_ONLY_DDL first, rebuilds
# dispatches, then applies _0022_INDEXES_DDL.  The skip-rebuild path can apply
# both in sequence because dispatches already has operator_approved_at.
# ---------------------------------------------------------------------------

_0022_TRACK_TABLES_ONLY_DDL = """\
CREATE TABLE IF NOT EXISTS tracks (
    track_id                TEXT    NOT NULL PRIMARY KEY,
    title                   TEXT    NOT NULL,
    goal_state              TEXT    NOT NULL,
    phase                   TEXT    NOT NULL DEFAULT 'queued'
                                    CHECK (phase IN ('queued', 'active', 'parked', 'done')),
    next_up                 INTEGER NOT NULL DEFAULT 0,
    sort_order              INTEGER NOT NULL DEFAULT 0,
    priority                TEXT,
    requires_operator_promotion INTEGER NOT NULL DEFAULT 1,
    instruction_template    TEXT,
    context_composer_rules  TEXT,
    pr_ref                  TEXT,
    trigger_condition       TEXT,
    project_id              TEXT    NOT NULL DEFAULT 'vnx-dev',
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    phase_changed_at        TEXT,
    completed_at            TEXT,
    metadata_json           TEXT    DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS track_phase_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    TEXT    NOT NULL REFERENCES tracks(track_id),
    from_phase  TEXT,
    to_phase    TEXT    NOT NULL,
    actor       TEXT    NOT NULL CHECK (actor IN ('operator', 'T0', 'system')),
    reason      TEXT,
    approval_id TEXT,
    occurred_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS track_dependencies (
    from_track_id       TEXT    NOT NULL REFERENCES tracks(track_id),
    to_track_id         TEXT    NOT NULL REFERENCES tracks(track_id),
    kind                TEXT    NOT NULL CHECK (kind IN ('hard', 'soft', 'overlap')),
    derivation_source   TEXT    NOT NULL
                                CHECK (derivation_source IN (
                                    'manual', 'git_ancestry', 'path_overlap', 'oi_ref', 'pr_ref'
                                )),
    confidence          REAL    NOT NULL DEFAULT 1.0,
    evidence_json       TEXT,
    derived_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (from_track_id, to_track_id)
);
CREATE TABLE IF NOT EXISTS track_open_items (
    track_id    TEXT    NOT NULL REFERENCES tracks(track_id),
    oi_id       TEXT    NOT NULL,
    link_type   TEXT    NOT NULL CHECK (link_type IN ('blocks', 'warns', 'related')),
    link_source TEXT    NOT NULL CHECK (link_source IN ('file_path', 'mention', 'manual')),
    linked_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (track_id, oi_id, link_type)
);
"""

# All 0022 indexes — applied AFTER dispatches is in v22-shape (operator_approved_at present).
_0022_INDEXES_DDL = """\
CREATE INDEX IF NOT EXISTS idx_dispatch_state
    ON dispatches (state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispatch_terminal
    ON dispatches (terminal_id, state);
CREATE INDEX IF NOT EXISTS idx_dispatch_created
    ON dispatches (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tracks_phase_nextup
    ON tracks(project_id, phase, next_up DESC, sort_order);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tracks_next_up
    ON tracks(project_id) WHERE next_up = 1 AND phase = 'queued';
CREATE INDEX IF NOT EXISTS idx_dispatches_ready
    ON dispatches(state, operator_approved_at)
    WHERE state = 'proposed' OR state = 'ready';
CREATE INDEX IF NOT EXISTS idx_track_deps_from
    ON track_dependencies(from_track_id);
CREATE INDEX IF NOT EXISTS idx_track_phase_history
    ON track_phase_history(track_id, occurred_at DESC);
"""


def _apply_0022_skip_dispatches_rebuild(conn: sqlite3.Connection) -> None:
    """Apply the track-layer portion of 0022 WITHOUT rebuilding dispatches.

    Called when dispatches is already in v22-shape (composite UNIQUE + operator_approved_at
    present).  Creates all track tables and indexes idempotently, then bumps user_version
    to 22.  The dispatches table is left untouched — its v4 forward-scaffolding columns
    (task_class, target_type, target_id, channel_origin, intelligence_payload) are preserved.

    Applies _0022_TRACK_TABLES_ONLY_DDL first (tables only), then _0022_INDEXES_DDL
    (which references operator_approved_at — safe because dispatches already has it).
    """
    sp = '"vnx_ver_22_skip_rebuild"'
    conn.execute(f"SAVEPOINT {sp}")
    try:
        for stmt in schema_migration._split_sql_statements(_0022_TRACK_TABLES_ONLY_DDL):
            conn.execute(stmt)
        for stmt in schema_migration._split_sql_statements(_0022_INDEXES_DDL):
            conn.execute(stmt)
        conn.execute("PRAGMA user_version = 22")
        conn.execute(f"RELEASE SAVEPOINT {sp}")
    except Exception:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception as rb_exc:
            warnings.warn(
                f"0022 skip-rebuild ROLLBACK failed: {rb_exc}",
                stacklevel=2,
            )
        raise


def _create_v22_dispatches_table(conn: sqlite3.Connection) -> None:
    """Create the v22-shape dispatches table (composite UNIQUE(dispatch_id, project_id))."""
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


def _copy_into_v22_dispatches(
    conn: sqlite3.Connection,
    col_info: list,
    col_list: str,
) -> None:
    """Re-add extra columns, copy rows, preserve the sequence high-water mark, drop the old table.

    Extracted from _apply_0022_dynamic_rebuild; the caller wraps this in the
    foreign_keys=OFF + SAVEPOINT transaction.
    """
    # Add any extra columns (beyond the 0022 schema) back via ALTER TABLE.
    v22_cols = {
        'id', 'dispatch_id', 'project_id', 'state', 'terminal_id', 'track', 'priority',
        'pr_ref', 'gate', 'attempt_count', 'bundle_path', 'created_at', 'updated_at',
        'expires_after', 'metadata_json', 'operator_approved_at',
    }
    extra_cols = [c for c, gen in col_info if not gen and c not in v22_cols]
    for extra_col in extra_cols:
        conn.execute(f"ALTER TABLE dispatches ADD COLUMN \"{extra_col}\" TEXT")

    conn.execute(
        f"INSERT INTO dispatches ({col_list}) "
        f"SELECT {col_list} FROM dispatches_pre_v22"
    )

    # Sequence preservation (mirrors 0022 SQL DELETE+INSERT pattern, PR #685).
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'dispatches'")
    conn.execute("""
        INSERT INTO sqlite_sequence(name, seq)
        SELECT 'dispatches',
               COALESCE(
                   (SELECT seq FROM sqlite_sequence WHERE name = 'dispatches_pre_v22'),
                   (SELECT MAX(id) FROM dispatches),
                   0
               )
    """)

    # DROP succeeds because foreign_keys=OFF (set by the caller, outside the savepoint).
    # Ghost FKs on child tables referencing dispatches_pre_v22 are tolerated here;
    # the adaptive 0031-branch rebuilds those child tables with correct composite FKs.
    conn.execute("DROP TABLE dispatches_pre_v22")


def _apply_0022_dynamic_rebuild(conn: sqlite3.Connection) -> None:
    """Apply the full 0022 migration using a dynamic column copy.

    Used for genuine pre-v22 stores that carry extra columns beyond the 15-column
    baseline.  Rather than the hardcoded INSERT list in 0022_track_layer.sql, this
    path copies ALL non-generated columns dynamically via _dispatch_table_columns,
    so no extra column is ever silently dropped.

    The sequence-preservation logic mirrors the 0022 SQL DELETE+INSERT pattern.

    FK guard: the rename→drop sequence triggers FOREIGN KEY constraint failures on
    stores (e.g. Mission Control) where a child table carries a ghost FK referencing
    dispatches_pre_v22.  PRAGMA foreign_keys is a no-op inside a transaction, so it
    MUST be set to OFF before the SAVEPOINT opens.  The caller pattern mirrors
    _run_runtime_v31_transaction: capture original state → set OFF outside the
    transaction → do work → restore in finally.  The dangling child FK is fixed later
    by the adaptive 0031-branch (_ADAPTIVE_RUNTIME_TABLES rebuild).
    """
    col_info = _dispatch_table_columns(conn)
    copy_cols = [c for c, gen in col_info if not gen]
    col_list = ", ".join(f'"{c}"' for c in copy_cols)

    # Capture FK state and disable BEFORE opening any transaction/savepoint.
    # PRAGMA foreign_keys is silently ignored inside a transaction (SQLite docs).
    original_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # switch to explicit-transaction control
    try:
        conn.execute("PRAGMA foreign_keys=OFF")  # must precede SAVEPOINT

        sp = '"vnx_ver_22_dynamic_rebuild"'
        conn.execute(f"SAVEPOINT {sp}")
        try:
            # Track-layer tables only (no indexes yet — idx_dispatches_ready references
            # operator_approved_at which does not exist until AFTER the dispatches rebuild).
            for stmt in schema_migration._split_sql_statements(_0022_TRACK_TABLES_ONLY_DDL):
                conn.execute(stmt)

            # Dispatches rebuild: rename → create v22 shape → dynamic copy → seq fix → drop.
            conn.execute("ALTER TABLE dispatches RENAME TO dispatches_pre_v22")
            _create_v22_dispatches_table(conn)
            _copy_into_v22_dispatches(conn, col_info, col_list)

            # Indexes — applied AFTER dispatches rebuild so operator_approved_at exists.
            for stmt in schema_migration._split_sql_statements(_0022_INDEXES_DDL):
                conn.execute(stmt)

            conn.execute("PRAGMA user_version = 22")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                conn.execute(f"RELEASE SAVEPOINT {sp}")
            except Exception as rb_exc:
                warnings.warn(
                    f"0022 dynamic-rebuild ROLLBACK failed: {rb_exc}",
                    stacklevel=2,
                )
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON" if original_fk else "PRAGMA foreign_keys=OFF")
        conn.isolation_level = previous_isolation
    _verify_foreign_keys_restored(conn, original_fk)


# ---------------------------------------------------------------------------
# Step 1: apply 0022 migration
# ---------------------------------------------------------------------------

def apply_migration(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0022_track_layer.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 22:
        print(f"  [skip] migration 0022 already applied (user_version={current_version})")
        return

    _assert_dispatches_schema_intact(conn)

    if _dispatches_already_v22_shape(conn):
        # Dispatches is already in post-v22 form (composite UNIQUE + operator_approved_at).
        # This happens when a store (e.g. Mission Control's runtime_coordination.db) was
        # hand-migrated ahead of the schema walk.  Rebuilding dispatches here would drop
        # any extra columns (e.g. v4 forward-scaffolding).  Skip the rebuild; create only
        # the track-layer tables and bump user_version to 22.
        print("  [skip-rebuild] 0022 dispatches already in v22-shape; "
              "creating track tables only ...")
        _apply_0022_skip_dispatches_rebuild(conn)
        print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}
    extra_cols = cols - _V22_DISPATCHES_REQUIRED - {'operator_approved_at'}
    if extra_cols:
        # Genuine pre-v22 store with extra columns beyond the required baseline.
        # Use the dynamic rebuild path to preserve them rather than the hardcoded
        # INSERT list in 0022_track_layer.sql.
        print(f"  [dynamic] 0022 dispatches has extra columns {extra_cols!r}; "
              "using dynamic column copy to preserve them ...")
        _apply_0022_dynamic_rebuild(conn)
        print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")
        return

    # Standard path: run the SQL file as-is.
    sql = migration_path.read_text(encoding="utf-8")
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
# Step 10: apply 0031 migration (ADR-007 runtime tenant + FK repair)
# ---------------------------------------------------------------------------

_RUNTIME_V31_TABLES = (
    "terminal_leases",
    "dispatch_attempts",
    "headless_runs",
    "worker_states",
    "worker_pool_membership",
)

_RUNTIME_V30_LEGACY_COLUMNS = {
    "terminal_leases": {
        "id", "terminal_id", "state", "dispatch_id", "generation", "leased_at",
        "expires_at", "last_heartbeat_at", "released_at", "worker_pid",
        "metadata_json", "lease_token",
    },
    "dispatch_attempts": {
        "id", "attempt_id", "dispatch_id", "attempt_number", "terminal_id",
        "state", "started_at", "ended_at", "failure_reason", "metadata_json",
    },
    "headless_runs": {
        "id", "run_id", "dispatch_id", "attempt_id", "target_id", "target_type",
        "task_class", "terminal_id", "pid", "pgid", "state", "failure_class",
        "exit_code", "started_at", "subprocess_started_at", "heartbeat_at",
        "last_output_at", "completed_at", "duration_seconds", "log_artifact_path",
        "output_artifact_path", "receipt_id", "metadata_json",
    },
    "worker_states": {
        "terminal_id", "dispatch_id", "state", "last_output_at", "state_entered_at",
        "stall_count", "blocked_reason", "metadata_json", "created_at", "updated_at",
    },
}

_RUNTIME_V30_LEGACY_INDEX_SQL = {
    "terminal_leases": {
        "idx_lease_state": "CREATE INDEX idx_lease_state ON terminal_leases(state)",
        "idx_lease_dispatch": "CREATE INDEX idx_lease_dispatch ON terminal_leases(dispatch_id)",
        "idx_terminal_leases_token": (
            "CREATE UNIQUE INDEX idx_terminal_leases_token ON terminal_leases(lease_token) "
            "WHERE lease_token != ''"
        ),
    },
    "dispatch_attempts": {
        "idx_attempt_dispatch": (
            "CREATE INDEX idx_attempt_dispatch "
            "ON dispatch_attempts(dispatch_id, attempt_number)"
        ),
        "idx_attempt_state": (
            "CREATE INDEX idx_attempt_state ON dispatch_attempts(state, started_at DESC)"
        ),
        "idx_attempt_terminal": (
            "CREATE INDEX idx_attempt_terminal ON dispatch_attempts(terminal_id, started_at DESC)"
        ),
    },
    "headless_runs": {
        "idx_headless_run_state": (
            "CREATE INDEX idx_headless_run_state ON headless_runs(state, started_at DESC)"
        ),
        "idx_headless_run_dispatch": (
            "CREATE INDEX idx_headless_run_dispatch ON headless_runs(dispatch_id)"
        ),
        "idx_headless_run_target": (
            "CREATE INDEX idx_headless_run_target ON headless_runs(target_id, state)"
        ),
        "idx_headless_run_heartbeat": (
            "CREATE INDEX idx_headless_run_heartbeat ON headless_runs(state, heartbeat_at) "
            "WHERE state = 'running'"
        ),
    },
    "worker_states": {
        "idx_worker_state": "CREATE INDEX idx_worker_state ON worker_states(state)",
        "idx_worker_dispatch": "CREATE INDEX idx_worker_dispatch ON worker_states(dispatch_id)",
    },
}

_RUNTIME_V31_PARENT_TABLES = ("pool_config",)
_RUNTIME_V31_AUTOINCREMENT_TABLES = (
    "terminal_leases",
    "dispatch_attempts",
    "headless_runs",
)


def _normalize_create_index_sql(sql: str | None) -> str:
    """Normalize formatting while preserving every index semantic."""
    normalized = re.sub(r"\s+", " ", sql or "").strip().rstrip(";").strip().lower()
    normalized = re.sub(r"\bif\s+not\s+exists\s+", "", normalized)
    normalized = re.sub(r"\s*([(),])\s*", r"\1", normalized)
    normalized = re.sub(r"\s*(<=|>=|<>|!=|=|<|>)\s*", r"\1", normalized)
    return normalized


def _runtime_v31_violations(conn: sqlite3.Connection, skip_absent: bool = False) -> list[str]:
    """Validate the runtime family and its required parent invariants.

    When *skip_absent* is True, tables that do not exist in the DB are silently
    skipped.  This is used by the adaptive repair path to validate only the subset
    of runtime tables that were actually rebuilt (e.g. MC's headless_runs-only shape).
    """
    violations: list[str] = []
    present = None  # lazy — only computed when skip_absent=True
    for table in (*_RUNTIME_V31_TABLES, *_RUNTIME_V31_PARENT_TABLES):
        if skip_absent:
            if present is None:
                present = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            if table not in present:
                continue
        invariant = schema_manifest.table_at(31, table)
        if invariant is None:
            violations.append(f"v31 manifest missing runtime table invariant '{table}'")
        else:
            violations.extend(schema_manifest.validate_table(conn, invariant))
    return violations


def _runtime_v31_complete(conn: sqlite3.Connection) -> bool:
    return not _runtime_v31_violations(conn)


def _runtime_v31_tables_absent(conn: sqlite3.Connection) -> bool:
    """True only when the separate runtime schema has not been initialized at all."""
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return not any(table in present for table in _RUNTIME_V31_TABLES)


def _assert_runtime_v30_legacy_shape(conn: sqlite3.Connection) -> None:
    """Fail before mutation if static 0031 DDL would lose unknown schema objects."""
    _assert_required_tables(
        conn, (*_RUNTIME_V31_TABLES, "pool_config", "dispatches"), before="0031")
    if not _has_composite_unique(conn):
        raise RuntimeError(
            "0031 requires dispatches UNIQUE(dispatch_id, project_id) from PR-A1.")

    for table, expected_cols in _RUNTIME_V30_LEGACY_COLUMNS.items():
        actual_cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}
        if actual_cols != expected_cols:
            raise RuntimeError(
                f"0031 legacy-shape drift in {table}: expected columns={sorted(expected_cols)} "
                f"actual={sorted(actual_cols)}. Refusing static rebuild to prevent data loss.")

        actual_indexes = {
            r[0]: r[1] for r in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))
        }
        expected_indexes = _RUNTIME_V30_LEGACY_INDEX_SQL[table]
        if set(actual_indexes) != set(expected_indexes):
            raise RuntimeError(
                f"0031 legacy-shape drift in {table}: expected indexes="
                f"{sorted(expected_indexes)} actual={sorted(actual_indexes)}. "
                "Refusing rebuild because every index must be recreated.")
        for index_name, expected_sql in expected_indexes.items():
            actual_sql = actual_indexes[index_name]
            if _normalize_create_index_sql(actual_sql) != _normalize_create_index_sql(
                expected_sql
            ):
                raise RuntimeError(
                    f"0031 legacy-shape drift in {table}: index {index_name!r} "
                    f"definition differs from canonical legacy definition. "
                    f"expected={expected_sql!r} actual={actual_sql!r}. "
                    "Refusing rebuild because index semantics would be lost.")

        triggers = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))]
        if triggers:
            raise RuntimeError(
                f"0031 found unexpected triggers on {table}: {triggers}. "
                "Refusing rebuild rather than dropping them.")


def _assert_runtime_v31_clean(
    conn: sqlite3.Connection, skip_absent: bool = False
) -> None:
    """Assert that every present runtime table satisfies the v31 manifest invariants.

    When *skip_absent* is True, tables that are not present in the DB are skipped
    in the manifest check.  The FK + integrity checks always run over the full DB.
    Used by the adaptive repair path so a partial-runtime store (e.g. MC's
    headless_runs-only shape) does not fail on tables that were never created.
    """
    violations = _runtime_v31_violations(conn, skip_absent=skip_absent)
    if violations:
        raise RuntimeError(f"0031 runtime manifest violations: {violations[:5]}")
    try:
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"0031 foreign_key_check failed structurally: {exc}") from exc
    if fk_violations:
        raise RuntimeError(f"0031 foreign_key_check violations: {fk_violations}")
    integrity = conn.execute("PRAGMA integrity_check").fetchall()
    if integrity != [("ok",)]:
        raise RuntimeError(f"0031 integrity_check failed: {integrity}")


def _rollback_runtime_v31(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except Exception as rollback_exc:
        warnings.warn(
            f"0031 runtime repair: ROLLBACK after error failed: {rollback_exc}",
            stacklevel=2,
        )


def _capture_runtime_v31_sequences(conn: sqlite3.Connection) -> dict[str, int | None]:
    """Capture AUTOINCREMENT high-water marks before the tables are dropped."""
    sequences: dict[str, int | None] = {}
    for table in _RUNTIME_V31_AUTOINCREMENT_TABLES:
        row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)
        ).fetchone()
        sequences[table] = row[0] if row else None
    return sequences


def _restore_runtime_v31_sequences(
    conn: sqlite3.Connection, old_sequences: dict[str, int | None]
) -> None:
    """Restore max(pre-rebuild sequence, copied max id) after final-table rename.

    Skips tables that are not present in the DB (partial-runtime stores such as
    MC's headless_runs-only shape may have only a subset of the AUTOINCREMENT tables).
    """
    present_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for table, old_seq in old_sequences.items():
        if table not in present_tables:
            continue
        max_id = conn.execute(
            f'SELECT COALESCE(MAX(id), 0) FROM "{table}"'
        ).fetchone()[0]
        target = max(old_seq or 0, max_id or 0)
        conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        if old_seq is not None or target:
            conn.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)", (table, target)
            )


def _run_runtime_v31_transaction(
    conn: sqlite3.Connection, statements, *, preserve_runtime_sequences: bool = False
) -> None:
    """Execute 0031 under one validated transaction with FK enforcement suspended."""
    if conn.in_transaction:
        raise RuntimeError(
            "0031 requires a connection with no open transaction; commit or roll back first.")
    original_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        _begin_immediate_with_retry(conn)
        try:
            old_sequences = (
                _capture_runtime_v31_sequences(conn) if preserve_runtime_sequences else {}
            )
            for statement in statements:
                conn.execute(statement)
            if old_sequences:
                _restore_runtime_v31_sequences(conn, old_sequences)
            _assert_runtime_v31_clean(conn)
            conn.execute("COMMIT")
        except Exception:
            _rollback_runtime_v31(conn)
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON" if original_fk else "PRAGMA foreign_keys=OFF")
        conn.isolation_level = previous_isolation
    _verify_foreign_keys_restored(conn, original_fk)


# ---------------------------------------------------------------------------
# 0031 adaptive branch — repair stores that already carry an out-of-band
# project_id but lack composite FKs (e.g. seocrawler-v2 "mixed" state).
# Called when the cluster is NEITHER v31-complete (early exit above) NOR
# clean-v30-legacy (the static-DDL guard). Follows the W1B spec exactly.
# ---------------------------------------------------------------------------

# The 4 tables the adaptive branch rebuilds to the v31 manifest shape.
_ADAPTIVE_RUNTIME_TABLES = (
    "dispatch_attempts",
    "headless_runs",
    "terminal_leases",
    "worker_states",
)

# The exact v31 DDL for each of the 4 runtime tables (matches 0031 SQL + manifest).
# project_id column is always TEXT NOT NULL — NULL/''/absent cells are
# deterministic-filled to the resolved pid by the row-copy logic.
_V31_TABLE_DDL: dict[str, str] = {
    "dispatch_attempts": """\
CREATE TABLE {staging} (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id      TEXT    NOT NULL,
    dispatch_id     TEXT    NOT NULL,
    project_id      TEXT    NOT NULL DEFAULT {pid},
    attempt_number  INTEGER NOT NULL DEFAULT 1,
    terminal_id     TEXT    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'pending',
    started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at        TEXT,
    failure_reason  TEXT,
    metadata_json   TEXT    DEFAULT '{{}}',
    UNIQUE(attempt_id, project_id),
    FOREIGN KEY (dispatch_id, project_id)
        REFERENCES dispatches(dispatch_id, project_id)
)""",
    "headless_runs": """\
CREATE TABLE {staging} (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT    NOT NULL,
    dispatch_id             TEXT    NOT NULL,
    project_id              TEXT    NOT NULL DEFAULT {pid},
    attempt_id              TEXT    NOT NULL,
    target_id               TEXT    NOT NULL,
    target_type             TEXT    NOT NULL,
    task_class              TEXT    NOT NULL,
    terminal_id             TEXT,
    pid                     INTEGER,
    pgid                    INTEGER,
    state                   TEXT    NOT NULL DEFAULT 'init',
    failure_class           TEXT,
    exit_code               INTEGER,
    started_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    subprocess_started_at   TEXT,
    heartbeat_at            TEXT,
    last_output_at          TEXT,
    completed_at            TEXT,
    duration_seconds        REAL,
    log_artifact_path       TEXT,
    output_artifact_path    TEXT,
    receipt_id              TEXT,
    metadata_json           TEXT    DEFAULT '{{}}',
    UNIQUE(run_id, project_id),
    FOREIGN KEY (dispatch_id, project_id)
        REFERENCES dispatches(dispatch_id, project_id),
    FOREIGN KEY (attempt_id, project_id)
        REFERENCES dispatch_attempts(attempt_id, project_id)
)""",
    "terminal_leases": """\
CREATE TABLE {staging} (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id         TEXT    NOT NULL,
    project_id          TEXT    NOT NULL DEFAULT {pid},
    state               TEXT    NOT NULL DEFAULT 'idle',
    dispatch_id         TEXT,
    generation          INTEGER NOT NULL DEFAULT 1,
    leased_at           TEXT,
    expires_at          TEXT,
    last_heartbeat_at   TEXT,
    released_at         TEXT,
    worker_pid          INTEGER,
    metadata_json       TEXT    DEFAULT '{{}}',
    lease_token         TEXT    NOT NULL DEFAULT '',
    UNIQUE(terminal_id, project_id),
    FOREIGN KEY (dispatch_id, project_id)
        REFERENCES dispatches(dispatch_id, project_id)
)""",
    "worker_states": """\
CREATE TABLE {staging} (
    terminal_id      TEXT    NOT NULL,
    project_id       TEXT    NOT NULL DEFAULT {pid},
    dispatch_id      TEXT    NOT NULL,
    state            TEXT    NOT NULL DEFAULT 'initializing',
    last_output_at   TEXT,
    state_entered_at TEXT    NOT NULL,
    stall_count      INTEGER NOT NULL DEFAULT 0,
    blocked_reason   TEXT,
    metadata_json    TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (terminal_id, project_id),
    FOREIGN KEY (terminal_id, project_id)
        REFERENCES terminal_leases(terminal_id, project_id),
    FOREIGN KEY (dispatch_id, project_id)
        REFERENCES dispatches(dispatch_id, project_id)
)""",
}

# Per-table secondary indexes to recreate after rebuild (matches 0031 SQL).
_V31_TABLE_INDEXES: dict[str, list[str]] = {
    "dispatch_attempts": [
        "CREATE INDEX idx_attempt_dispatch ON dispatch_attempts(dispatch_id, attempt_number)",
        "CREATE INDEX idx_attempt_state ON dispatch_attempts(state, started_at DESC)",
        "CREATE INDEX idx_attempt_terminal ON dispatch_attempts(terminal_id, started_at DESC)",
        "CREATE INDEX idx_attempt_project ON dispatch_attempts(project_id)",
    ],
    "headless_runs": [
        "CREATE INDEX idx_headless_run_state ON headless_runs(state, started_at DESC)",
        "CREATE INDEX idx_headless_run_dispatch ON headless_runs(dispatch_id)",
        "CREATE INDEX idx_headless_run_target ON headless_runs(target_id, state)",
        "CREATE INDEX idx_headless_run_heartbeat ON headless_runs(state, heartbeat_at) "
        "WHERE state = 'running'",
        "CREATE INDEX idx_headless_run_project ON headless_runs(project_id)",
    ],
    "terminal_leases": [
        "CREATE INDEX idx_lease_state ON terminal_leases(state)",
        "CREATE INDEX idx_lease_dispatch ON terminal_leases(dispatch_id)",
        "CREATE INDEX idx_lease_project ON terminal_leases(project_id)",
        "CREATE INDEX idx_lease_terminal_project ON terminal_leases(terminal_id, project_id)",
        "CREATE UNIQUE INDEX idx_terminal_leases_token ON terminal_leases(lease_token) "
        "WHERE lease_token != ''",
    ],
    "worker_states": [
        "CREATE INDEX idx_worker_state ON worker_states(state)",
        "CREATE INDEX idx_worker_dispatch ON worker_states(dispatch_id)",
        "CREATE INDEX idx_worker_states_project ON worker_states(project_id)",
    ],
}

# Columns present in v30-legacy tables (those WITHOUT project_id yet) and
# columns present in the "mixed" shape (those WITH project_id already).
# The copy logic detects which shape each table is in at runtime.
_V31_COPY_COLS: dict[str, list[str]] = {
    "dispatch_attempts": [
        "id", "attempt_id", "dispatch_id", "attempt_number", "terminal_id",
        "state", "started_at", "ended_at", "failure_reason", "metadata_json",
    ],
    "headless_runs": [
        "id", "run_id", "dispatch_id", "attempt_id", "target_id", "target_type",
        "task_class", "terminal_id", "pid", "pgid", "state", "failure_class",
        "exit_code", "started_at", "subprocess_started_at", "heartbeat_at",
        "last_output_at", "completed_at", "duration_seconds", "log_artifact_path",
        "output_artifact_path", "receipt_id", "metadata_json",
    ],
    "terminal_leases": [
        "id", "terminal_id", "state", "dispatch_id", "generation", "leased_at",
        "expires_at", "last_heartbeat_at", "released_at", "worker_pid",
        "metadata_json", "lease_token",
    ],
    "worker_states": [
        "terminal_id", "dispatch_id", "state", "last_output_at", "state_entered_at",
        "stall_count", "blocked_reason", "metadata_json", "created_at", "updated_at",
    ],
}


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if ``table`` has a column named ``column``."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}
    return column in cols


def _adaptive_foreign_tenant_preflight(
    conn: sqlite3.Connection,
    tables: list[str],
    resolved_pid: str,
) -> None:
    """D1: Assert that every runtime table with project_id holds only
    {resolved_pid, 'vnx-dev'}. Skip tables that lack the column entirely.
    Abort on any third tenant value — this is a fail-closed gate.
    """
    for table in tables:
        if not _table_has_column(conn, table, "project_id"):
            continue  # table lacks project_id — nothing to assert
        rows = conn.execute(
            f"SELECT DISTINCT project_id FROM \"{table}\" "
            "WHERE project_id IS NOT NULL AND project_id != ''"
        ).fetchall()
        for (val,) in rows:
            if val != resolved_pid and val != "vnx-dev":
                raise RuntimeError(
                    f"0031 adaptive pre-flight: table '{table}' contains a third "
                    f"tenant '{val}' (resolved_pid='{resolved_pid}'). "
                    "Refusing to mutate a multi-tenant store. "
                    "Resolve the foreign tenant manually before retrying."
                )


def _adaptive_orphan_preflight(conn: sqlite3.Connection) -> None:
    """D1 + orphan policy (CONSERVATIVE): count headless_runs rows whose
    dispatch_id or attempt_id have no matching parent row. If ANY orphans exist
    → ABORT with a clear report (incl. tables referencing headless_runs). We
    NEVER silently exclude governance data.
    """
    if not _table_has_column(conn, "headless_runs", "dispatch_id"):
        # headless_runs has no dispatch_id column at all — cannot have orphans
        return

    # Count orphans via LEFT JOIN to dispatches
    dispatch_orphans_count = conn.execute(
        "SELECT COUNT(*) FROM headless_runs hr "
        "LEFT JOIN dispatches d ON hr.dispatch_id = d.dispatch_id "
        "WHERE d.dispatch_id IS NULL"
    ).fetchone()[0]

    attempt_orphans_count = 0
    if _table_has_column(conn, "headless_runs", "attempt_id") and (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatch_attempts'"
        ).fetchone()
    ):
        attempt_orphans_count = conn.execute(
            "SELECT COUNT(*) FROM headless_runs hr "
            "LEFT JOIN dispatch_attempts da ON hr.attempt_id = da.attempt_id "
            "WHERE da.attempt_id IS NULL"
        ).fetchone()[0]

    if dispatch_orphans_count == 0 and attempt_orphans_count == 0:
        return  # clean — proceed

    # Gather sample rows for the abort report
    sample_dispatch_orphans = conn.execute(
        "SELECT hr.id, hr.run_id, hr.dispatch_id FROM headless_runs hr "
        "LEFT JOIN dispatches d ON hr.dispatch_id = d.dispatch_id "
        "WHERE d.dispatch_id IS NULL LIMIT 5"
    ).fetchall()
    sample_attempt_orphans: list = []
    if attempt_orphans_count > 0:
        sample_attempt_orphans = conn.execute(
            "SELECT hr.id, hr.run_id, hr.attempt_id FROM headless_runs hr "
            "LEFT JOIN dispatch_attempts da ON hr.attempt_id = da.attempt_id "
            "WHERE da.attempt_id IS NULL LIMIT 5"
        ).fetchall()

    # Check for any tables referencing headless_runs (deepseek finding)
    referencing_tables = []
    all_tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    for tbl in all_tables:
        fks = conn.execute(f"PRAGMA foreign_key_list('{tbl}')").fetchall()
        if any(r[2] == "headless_runs" for r in fks):
            referencing_tables.append(tbl)

    msg = (
        f"0031 adaptive orphan pre-flight ABORT: headless_runs contains orphan rows "
        f"that would violate the composite FK after repair.\n"
        f"  dispatch orphans: {dispatch_orphans_count} "
        f"(sample: {sample_dispatch_orphans})\n"
        f"  attempt orphans: {attempt_orphans_count} "
        f"(sample: {sample_attempt_orphans})\n"
        f"  tables referencing headless_runs: {referencing_tables}\n"
        "Operator action required: inspect and resolve orphan rows before retrying. "
        "No governance data will be silently deleted."
    )
    raise RuntimeError(msg)


def _validate_col_references(sql: str, valid_cols: set[str], object_name: str) -> None:
    """Warn (not abort) if a dependent-object SQL references a column name that
    no longer exists in the rebuilt table. The check is conservative (name-based,
    no full SQL parse), so it only catches obviously dangling references.
    """
    # Extract bare identifiers from the SQL — simple tokenization
    # by removing quoted strings and splitting on non-identifier chars.
    stripped = re.sub(r'"[^"]*"', " ", sql)
    stripped = re.sub(r"'[^']*'", " ", stripped)
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", stripped.lower()))
    # SQL keywords that look like column names but are not
    _SQL_KEYWORDS = {
        "create", "index", "on", "where", "table", "unique", "not", "null",
        "primary", "key", "foreign", "references", "default", "and", "or",
        "is", "in", "like", "select", "from", "insert", "update", "delete",
        "trigger", "before", "after", "instead", "of", "for", "each", "row",
        "begin", "end", "when", "if", "exists", "view", "as", "asc", "desc",
        "int", "integer", "text", "real", "blob", "numeric", "boolean",
        "running", "idle", "pending", "initializing",  # state values
    }
    col_tokens = tokens - _SQL_KEYWORDS
    dangling = col_tokens - valid_cols
    if dangling:
        warnings.warn(
            f"0031 adaptive: dependent object '{object_name}' may reference "
            f"columns not in the rebuilt table: {dangling}. "
            "Recreating verbatim — verify manually if this causes an error.",
            stacklevel=3,
        )


def _prepare_v31_dependents(
    conn: sqlite3.Connection,
    table: str,
    base_copy_cols: list,
) -> tuple:
    """Capture views/triggers on `table`, validate their column refs against the
    v31 shape, and drop views before the rebuild. Returns (views, triggers) for
    verbatim recreation after the rename.

    Secondary indexes are intentionally NOT captured here: the rebuild replaces
    them with authoritative v31 DDL from _V31_TABLE_INDEXES.
    """
    views = tenant_stamping._get_views_referencing(conn, table)
    triggers = tenant_stamping._get_triggers_for_table(conn, table)
    v31_cols_for_table = set(base_copy_cols) | {"project_id"}
    for view_name, view_sql in views:
        _validate_col_references(view_sql, v31_cols_for_table, f"view:{view_name}")
        conn.execute(f'DROP VIEW IF EXISTS "{view_name}"')
    for trig_name, trig_sql in triggers:
        _validate_col_references(trig_sql, v31_cols_for_table, f"trigger:{trig_name}")
    return views, triggers


def _copy_rows_into_v31_staging(
    conn: sqlite3.Connection,
    table: str,
    staging: str,
    base_copy_cols: list,
    resolved_pid: str,
    has_pid: bool,
) -> None:
    """Copy rows from `table` into the v31 staging table, filling (has_pid) or
    adding (not has_pid) project_id = resolved_pid. Fail-loud on a composite-key
    collision — never silent data loss.
    """
    src_col_list = ", ".join(f'"{c}"' for c in base_copy_cols)
    # resolved_pid is passed as a BOUND PARAMETER (codex injection fix) — never
    # string-interpolated into the SQL, even though _resolve_validated_project_id
    # already charset-validates it (defense in depth).
    if has_pid:
        # project_id column exists: copy it, filling NULL/'' with resolved_pid.
        dst_col_list = ", ".join(f'"{c}"' for c in base_copy_cols + ["project_id"])
        pid_expr = (
            "CASE WHEN \"project_id\" IS NULL OR \"project_id\" = '' "
            "THEN ? ELSE \"project_id\" END"
        )
        select_sql = f'SELECT {src_col_list}, {pid_expr} FROM "{table}"'
    else:
        # project_id column absent: add it set to resolved_pid for all rows.
        dst_col_list = src_col_list + ', "project_id"'
        select_sql = f'SELECT {src_col_list}, ? FROM "{table}"'
    try:
        conn.execute(
            f'INSERT INTO "{staging}" ({dst_col_list}) {select_sql}', (resolved_pid,))
    except sqlite3.IntegrityError as exc:
        raise RuntimeError(
            f"0031 adaptive row-copy composite-key collision in '{table}': {exc}. "
            "Two or more rows resolve to the same (key, project_id) after NULL/'' fill. "
            "Investigate duplicates before retrying. ROLLBACK."
        ) from exc


def _adaptive_rebuild_table(
    conn: sqlite3.Connection,
    table: str,
    resolved_pid: str,
    staging_suffix: str = "_v31_new",
) -> None:
    """D2: Rebuild one runtime table to its exact v31 manifest shape.

    project_id: if present, preserve non-NULL/'' values and fill NULL/'' with
    resolved_pid; if absent, add it set to resolved_pid. Dependent views/triggers
    are captured + validated + recreated verbatim. Row-copy uses an exact
    count-assert (rows_copied == source_rowcount) and fails loud on a
    composite-key collision — no silent data loss. FK-off must already be active
    on the connection (set by _run_runtime_v31_transaction).
    """
    ddl_template = _V31_TABLE_DDL[table]
    base_copy_cols = _V31_COPY_COLS[table]
    staging = f"{table}{staging_suffix}"

    has_pid = _table_has_column(conn, table, "project_id")

    # Capture dependent views/triggers, validate their column refs against the
    # v31 shape, and drop views before the rebuild (see helper). Returned for
    # verbatim recreation after the rename.
    views, triggers = _prepare_v31_dependents(conn, table, base_copy_cols)

    # Count source rows for the count-assert
    source_rowcount = conn.execute(
        f'SELECT COUNT(*) FROM "{table}"'
    ).fetchone()[0]

    # Build + populate the staging table. The project_id column DEFAULT is the
    # RESOLVED tenant (D3), never the 'vnx-dev' sentinel — inlined because a
    # CREATE TABLE DEFAULT cannot be a bind parameter. Row values are still set
    # explicitly by _copy_rows_into_v31_staging; the DEFAULT only governs any
    # future INSERT that omits project_id, and must not be a wrong tenant.
    conn.execute(f'DROP TABLE IF EXISTS "{staging}"')
    conn.execute(ddl_template.format(
        staging=f'"{staging}"', pid=_sql_quote_literal(resolved_pid)))

    _copy_rows_into_v31_staging(conn, table, staging, base_copy_cols, resolved_pid, has_pid)

    # Count-assert: every source row must be copied (no composite-key collision allowed).
    # This fires when the INSERT succeeded but produced fewer rows than the source
    # (e.g. if future INSERT OR IGNORE semantics are ever used). Belt-and-suspenders.
    rows_copied = conn.execute(f'SELECT COUNT(*) FROM "{staging}"').fetchone()[0]
    if rows_copied != source_rowcount:
        raise RuntimeError(
            f"0031 adaptive row-copy mismatch for '{table}': "
            f"{source_rowcount} source rows but {rows_copied} copied. "
            "Composite-key collision or data loss detected — investigate before retrying. "
            "ROLLBACK."
        )

    # Drop source table and rename staging into place
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{staging}" RENAME TO "{table}"')

    # Recreate secondary indexes from the v31 manifest (authoritative shape)
    for idx_sql in _V31_TABLE_INDEXES[table]:
        conn.execute(idx_sql)

    # Recreate any captured views and triggers verbatim
    for _, view_sql in views:
        conn.execute(view_sql)
    for _, trigger_sql in triggers:
        conn.execute(trigger_sql)


def _wpm_has_composite_fk_to_terminal_leases(conn: sqlite3.Connection) -> bool:
    """D5: Return True if worker_pool_membership's FK to terminal_leases is
    composite (terminal_id, project_id). Return False if single-column or absent.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_pool_membership'"
    ).fetchone():
        return True  # table absent — nothing to fix

    fk_rows = conn.execute(
        "PRAGMA foreign_key_list('worker_pool_membership')"
    ).fetchall()
    # Find FK group(s) referencing terminal_leases
    for fk_id in {r[0] for r in fk_rows if r[2] == "terminal_leases"}:
        fk_cols = [r[3] for r in fk_rows if r[0] == fk_id and r[2] == "terminal_leases"]
        if set(fk_cols) == {"terminal_id", "project_id"}:
            return True
    return False


def _create_wpm_v31_staging(conn: sqlite3.Connection, staging: str) -> None:
    """Create the v31 worker_pool_membership staging table (composite FKs to
    terminal_leases and pool_config)."""
    conn.execute(f"""
CREATE TABLE "{staging}" (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id         TEXT    NOT NULL,
    project_id          TEXT    NOT NULL,
    pool_id             TEXT    NOT NULL DEFAULT 'default',
    provider            TEXT    NOT NULL
                            CHECK (provider IN ('claude', 'codex', 'gemini', 'litellm')),
    role                TEXT    NOT NULL,
    joined_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    released_at         TEXT,
    release_reason      TEXT,
    spawn_generation    INTEGER NOT NULL DEFAULT 1,
    metadata_json       TEXT    DEFAULT '{{}}',
    FOREIGN KEY (terminal_id, project_id)
        REFERENCES terminal_leases(terminal_id, project_id),
    FOREIGN KEY (project_id, pool_id)
        REFERENCES pool_config(project_id, pool_id)
)""")


def _adaptive_rebuild_worker_pool_membership(
    conn: sqlite3.Connection,
    staging_suffix: str = "_v31_new",
) -> None:
    """D5: Rebuild worker_pool_membership to give it a composite FK
    (terminal_id, project_id) → terminal_leases if it currently has a
    single-column FK. Captures and restores dependent objects verbatim.
    No-op if already composite (caller checks first).
    """
    table = "worker_pool_membership"
    staging = f"{table}{staging_suffix}"

    views = tenant_stamping._get_views_referencing(conn, table)
    triggers = tenant_stamping._get_triggers_for_table(conn, table)
    secondary_indexes = tenant_stamping._get_secondary_indexes(conn, table)

    for view_name, _ in views:
        conn.execute(f'DROP VIEW IF EXISTS "{view_name}"')

    source_rowcount = conn.execute(
        f'SELECT COUNT(*) FROM "{table}"'
    ).fetchone()[0]

    conn.execute(f'DROP TABLE IF EXISTS "{staging}"')
    _create_wpm_v31_staging(conn, staging)

    conn.execute(
        f'INSERT INTO "{staging}" '
        '(id, terminal_id, project_id, pool_id, provider, role, joined_at, '
        'released_at, release_reason, spawn_generation, metadata_json) '
        f'SELECT id, terminal_id, project_id, pool_id, provider, role, joined_at, '
        f'released_at, release_reason, spawn_generation, metadata_json FROM "{table}"'
    )

    rows_copied = conn.execute(f'SELECT COUNT(*) FROM "{staging}"').fetchone()[0]
    if rows_copied != source_rowcount:
        raise RuntimeError(
            f"0031 adaptive WPM row-copy mismatch: "
            f"{source_rowcount} source rows but {rows_copied} copied. ROLLBACK."
        )

    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{staging}" RENAME TO "{table}"')

    # Recreate the authoritative v31 indexes for worker_pool_membership
    conn.execute(
        "CREATE UNIQUE INDEX idx_pool_membership_active "
        f"ON {table}(terminal_id, project_id) WHERE released_at IS NULL"
    )
    conn.execute(
        f"CREATE INDEX idx_pool_membership_pool ON {table}(project_id, pool_id)"
    )

    for _, view_sql in views:
        conn.execute(view_sql)
    for _, trigger_sql in triggers:
        conn.execute(trigger_sql)

    # Recreate any non-standard secondary indexes verbatim (exclude the above two)
    authoritative_names = {"idx_pool_membership_active", "idx_pool_membership_pool"}
    for idx_name, idx_sql in secondary_indexes:
        if idx_name not in authoritative_names:
            conn.execute(idx_sql)


def _build_adaptive_repair_statements(
    conn: sqlite3.Connection,
    resolved_pid: str,
) -> list:
    """D4: Build the list of callables that implement the adaptive repair.

    Returns a list of zero-argument callables to be executed inside
    _run_runtime_v31_transaction. Each callable executes one logical step
    (table rebuild or WPM rebuild) using the already-open connection.

    The ordering respects FK dependency:
      terminal_leases (no runtime parent among the 4)
      dispatch_attempts (FK → dispatches, but dispatch_id already there)
      headless_runs (FK → dispatches + dispatch_attempts — must come after DA)
      worker_states (FK → terminal_leases + dispatches — must come after TL)
      worker_pool_membership (FK → terminal_leases — must come after TL)
    """
    # Ordered to satisfy FK dependency (parent-before-child within the runtime cluster).
    # Only include tables that ACTUALLY EXIST in the DB — a store that has a partial
    # runtime setup (e.g. MC's headless_runs-only shape) must not fail on missing tables.
    ordered_tables = [
        "terminal_leases",
        "dispatch_attempts",
        "headless_runs",
        "worker_states",
    ]
    present_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}

    stmts = []
    for table in ordered_tables:
        if table not in present_tables:
            continue
        t = table  # capture for closure
        p = resolved_pid
        stmts.append(lambda _conn=conn, _t=t, _p=p: _adaptive_rebuild_table(_conn, _t, _p))

    # D5: worker_pool_membership — rebuild if FK is not already composite
    if not _wpm_has_composite_fk_to_terminal_leases(conn):
        stmts.append(
            lambda _conn=conn: _adaptive_rebuild_worker_pool_membership(_conn)
        )

    # Stamp user_version=31 inside the same transaction
    stmts.append(lambda _conn=conn: _conn.execute("PRAGMA user_version = 31"))

    return stmts


def _run_adaptive_v31_repair(
    conn: sqlite3.Connection,
    resolved_pid: str,
) -> None:
    """D4 branch orchestration: run the full adaptive repair inside one
    FK-off + BEGIN IMMEDIATE transaction, then _assert_runtime_v31_clean
    (FK+integrity check) before COMMIT, then _assert_manifest_converged.

    Reuses _run_runtime_v31_transaction for the transaction envelope and
    _capture/_restore_runtime_v31_sequences for AUTOINCREMENT preservation.
    """
    if conn.in_transaction:
        raise RuntimeError(
            "0031 adaptive repair requires no open transaction; commit or roll back first."
        )

    original_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    previous_isolation = conn.isolation_level
    conn.isolation_level = None

    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        _begin_immediate_with_retry(conn)
        try:
            # Capture AUTOINCREMENT sequences after BEGIN (inside transaction, matches
            # the pattern in _run_runtime_v31_transaction).
            old_sequences = _capture_runtime_v31_sequences(conn)

            stmts = _build_adaptive_repair_statements(conn, resolved_pid)
            for stmt in stmts:
                stmt()  # each callable operates on conn

            _restore_runtime_v31_sequences(conn, old_sequences)
            # skip_absent=True: a partial-runtime store (e.g. MC's headless_runs-only
            # shape) must not fail manifest validation for runtime tables that simply
            # were never created.  Only tables that exist in the DB are validated.
            _assert_runtime_v31_clean(conn, skip_absent=True)
            conn.execute("COMMIT")
        except Exception:
            _rollback_runtime_v31(conn)
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON" if original_fk else "PRAGMA foreign_keys=OFF")
        conn.isolation_level = previous_isolation

    _verify_foreign_keys_restored(conn, original_fk)


_CREATE_INDEX_NAME_RE = re.compile(
    r'(?is)^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?("?)([A-Za-z0-9_]+)\1')


def _canonical_runtime_index_defs() -> dict[str, set[str]]:
    """Map index name → {acceptable normalized CREATE INDEX definitions}, over the
    legacy (_RUNTIME_V30_LEGACY_INDEX_SQL) and v31 (_V31_TABLE_INDEXES) canonical sets.
    An index of a canonical name whose definition matches none of these has drifted."""
    acc: dict[str, set[str]] = {}
    for sqls in _V31_TABLE_INDEXES.values():
        for sql in sqls:
            m = _CREATE_INDEX_NAME_RE.match(sql)
            if m:
                acc.setdefault(m.group(2), set()).add(_normalize_create_index_sql(sql))
    for by_name in _RUNTIME_V30_LEGACY_INDEX_SQL.values():
        for name, sql in by_name.items():
            acc.setdefault(name, set()).add(_normalize_create_index_sql(sql))
    return acc


def _assert_no_conflicting_runtime_index_redefinition(conn: sqlite3.Connection) -> None:
    """Refuse (do NOT silently coerce/drop) a runtime index that REUSES a canonical
    index name with a DIFFERENT definition (codex safety fix). The adaptive rebuild
    recreates indexes from the authoritative v31 set, so a same-name index carrying
    different semantics (e.g. a rogue UNIQUE(idx_lease_state) on other columns) would
    be silently replaced — a real, possibly load-bearing constraint change. A drifted
    canonical-named index whose normalized definition matches neither the legacy nor
    the v31 canonical def aborts the migration for operator review. Custom-named
    (non-canonical) indexes are out of scope here."""
    acc = _canonical_runtime_index_defs()
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table in _RUNTIME_V31_TABLES:
        if table not in present:
            continue
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL", (table,)).fetchall():
            if name in acc and _normalize_create_index_sql(sql) not in acc[name]:
                raise RuntimeError(
                    f"0031 refuses to proceed: index {name!r} on {table!r} definition "
                    f"differs from every canonical/legacy runtime definition "
                    f"(found {sql!r}). Refusing to silently coerce or drop conflicting "
                    "index semantics — resolve the index manually first.")


def apply_migration_v31(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0031_runtime_tenant_fk_repair.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 31:
        print(f"  [skip] migration 0031 already applied (user_version={current_version})")
        return
    if current_version != 30:
        raise RuntimeError(
            f"0031 requires user_version=30 after the prior numbered walk; got {current_version}.")

    # Refuse conflicting canonical-named index redefinitions FIRST — before the
    # v31-complete / tables-absent fast paths (codex round-2 fast-path-bypass fix). The
    # manifest's v31-complete check compares key columns/unique/partial only, NOT
    # ASC/DESC direction or collation, so a same-name index that differs ONLY in
    # direction/collation would slip through the fast path and be silently accepted.
    # The guard skips absent tables, so it is a no-op on the tables-absent path.
    _assert_no_conflicting_runtime_index_redefinition(conn)

    if _runtime_v31_tables_absent(conn):
        print("  [skip] migration 0031 runtime tables absent; user_version → 31")
        conn.execute("PRAGMA user_version = 31")
        return

    if _runtime_v31_complete(conn):
        print("  [stamp] runtime tenant/FK repair already complete; user_version → 31")
        _run_runtime_v31_transaction(conn, ("PRAGMA user_version = 31",))
        return

    # Adaptive branch (W1B spec): when the cluster is NEITHER v31-complete (above)
    # NOR clean-v30-legacy, run the adaptive FK-repair (mixed state, e.g. seocrawler-v2
    # / sales-copilot at v30). Otherwise apply the static 0031 DDL. Both resolve the
    # tenant identity and stamp the RESOLVED project_id, never the 'vnx-dev' sentinel.
    try:
        _assert_runtime_v30_legacy_shape(conn)
        is_legacy_clean = True
    except RuntimeError:
        is_legacy_clean = False

    if not is_legacy_clean:
        _apply_adaptive_0031(conn)
    else:
        _apply_static_0031(conn, migration_path)


def _resolve_0031_pid_from_conn(conn: sqlite3.Connection, label: str) -> str:
    """Resolve+validate the tenant identity for 0031 from the connection's DB file."""
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    if not db_file:
        raise RuntimeError(
            f"0031 {label}: cannot resolve project_id — connection has no database "
            "file (in-memory or unnamed). Pass a real DB path.")
    return _resolve_validated_project_id(db_file)


def _apply_adaptive_0031(conn: sqlite3.Connection) -> None:
    """Mixed/contaminated store — adaptive FK-repair with the RESOLVED project_id."""
    resolved_pid = _resolve_0031_pid_from_conn(conn, "adaptive")
    print(
        f"  [adapt] 0031 mixed store detected (project_id already present but FKs not "
        f"composite). Running adaptive FK-repair for pid='{resolved_pid}' ...")
    # D1: foreign-tenant + orphan pre-flights (fail-closed) before any mutation.
    _adaptive_foreign_tenant_preflight(conn, list(_ADAPTIVE_RUNTIME_TABLES), resolved_pid)
    _adaptive_orphan_preflight(conn)
    # D2–D5 + stamp user_version=31 in one FK-off BEGIN IMMEDIATE transaction.
    _run_adaptive_v31_repair(conn, resolved_pid)
    print(f"  [ok]    adaptive repair complete; user_version → "
          f"{schema_migration.get_user_version(conn)}")


def _apply_static_0031(conn: sqlite3.Connection, migration_path: Path) -> None:
    """Clean v30-legacy shape — static 0031 DDL, tenant-stamped with the RESOLVED
    project_id (D3). The file's 'vnx-dev' placeholders (column DEFAULT + the four
    row-copy INSERT...SELECT literals) are substituted for the DB-path-anchored,
    fail-closed tenant identity before execution so a non-vnx-dev store is never
    corrupted. The FK-off-before-BEGIN-IMMEDIATE envelope lives in
    _run_runtime_v31_transaction (the in-file `PRAGMA foreign_keys=OFF` is a no-op
    inside the open transaction — D6)."""
    resolved_pid = _resolve_0031_pid_from_conn(conn, "static")
    sql = _render_static_0031_sql_with_pid(
        migration_path.read_text(encoding="utf-8"), resolved_pid)
    print(f"  [apply] migration 0031_runtime_tenant_fk_repair.sql (tenant={resolved_pid!r}) ...")
    _run_runtime_v31_transaction(
        conn,
        schema_migration._split_sql_statements(sql),
        preserve_runtime_sequences=True,
    )
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

#: The numbered-walk preflight hooks, as (version, fn) — the single source for both
#: the import-time registrations above and the defensive re-assert in _run_pipeline.
_PREFLIGHT_SPECS = (
    (22, _assert_dispatches_schema_intact),
    (24, _assert_tracks_v22_intact),
    (27, _ensure_dispatches_output_columns),
    (27, _assert_tracks_v24_intact),
    (28, _assert_tracks_v27_intact),
    (29, _assert_tracks_v28_intact),
    (30, _assert_tracks_v29_intact),
)


def _register_preflights() -> None:
    """(Re)register the numbered-walk preflight hooks idempotently.

    The hooks are registered at import, but a caller/test may have cleared
    schema_migration._PREFLIGHT_HOOKS (bootstrap-isolation fixtures do this to keep
    the v22 pre-hook from firing during a fresh bootstrap). The future-system walk
    DEPENDS on them — notably v27's _ensure_dispatches_output_columns, which adds
    dispatches.output_ref before the 0027 deliverables view (else: "no such column:
    output_ref"). run() re-asserts them defensively, registering each only when
    absent so import-time registration is never duplicated. The v22 pre-hook stays a
    no-op here because run() executes AFTER the bootstrap has already reached ≥ v22
    (0022 is then a user_version skip that never invokes its preflight)."""
    for version, fn in _PREFLIGHT_SPECS:
        if fn not in schema_migration._PREFLIGHT_HOOKS.get(version, []):
            schema_migration.register_preflight(version, fn)


def _run_numbered_walk(conn: sqlite3.Connection, project_root: Path) -> None:
    """Step C of run(): apply the numbered migrations 0022 → 0031 in order, each
    idempotent via user_version, committing after each. Preflights (steps 3-12) are
    manifest-backed (schema_manifest). See the module docstring for the full ordering.
    """
    _register_preflights()  # defensive: the walk depends on these hooks (v27 output_ref)
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
    apply_migration_v31(conn, project_root)   # 0031 — runtime tenant/FK repair
    conn.commit()


def _run_w1_coupled_migration(rc_db_path: Path) -> None:
    """B1 fix: Run the W1 3-phase tenant-stamping migration on BOTH RC and QI.

    The previous implementation only migrated RC (run_three_phase_migration_on_db
    called with db_label="RC"). The QI half (quality_intelligence.db) was dead
    code: quality_db_init.run_qi_three_phase_migration had zero production callers,
    and the coupled two-DB orchestrator (tenant_stamping.run_three_phase_migration)
    that runs assert_phase2_postcondition also had zero production callers.

    This function:
    1. Resolves pid ONCE (fail-closed) from the RC DB path.
    2. Resolves the QI DB path as the sibling of RC in the same state dir.
    3. If the QI DB does not exist, migrates RC alone and logs the skip.
    4. If the QI DB exists, invokes the COUPLED two-DB orchestrator
       (tenant_stamping.run_three_phase_migration) which runs:
         RC Phase 1+2, QI Phase 1+2,
         coupled post-condition (assert BOTH DBs hold zero legacy rows),
         RC Phase 3, QI Phase 3.
    5. Is idempotent — reruns find nothing to do on already-clean DBs.
    """
    pid = _resolve_validated_project_id(rc_db_path)
    qi_db_path = rc_db_path.parent / "quality_intelligence.db"

    if not qi_db_path.exists():
        print(f"  [W1] QI DB not found at {qi_db_path} — skipping QI (RC only).")
        print("  [W1] Running tenant-stamping Phase 1+2+3 on RC ...")
        ts_result = tenant_stamping.run_three_phase_migration_on_db(
            rc_db_path, pid, db_label="RC"
        )
        rebuilt1 = ts_result.get("phase1_rebuilt", [])
        updated2 = ts_result.get("phase2_updated", {})
        rebuilt3 = ts_result.get("phase3_rebuilt", [])
        if rebuilt1 or any(n for n in updated2.values()) or rebuilt3:
            print(f"  [W1] Phase 1 rebuilt: {rebuilt1}")
            print(f"  [W1] Phase 2 updated: { {t: n for t, n in updated2.items() if n} }")
            print(f"  [W1] Phase 3 rebuilt: {rebuilt3}")
        else:
            print("  [W1] No tenant-stamping changes needed on RC (already clean).")
        return

    print(f"  [W1] Running coupled tenant-stamping Phase 1+2+3 on RC + QI ...")
    print(f"  [W1] RC: {rc_db_path}")
    print(f"  [W1] QI: {qi_db_path}")
    combined = tenant_stamping.run_three_phase_migration(rc_db_path, qi_db_path, pid)

    rc_p1 = combined.get("rc_phase1", [])
    rc_p2 = combined.get("rc_phase2", {})
    rc_p3 = combined.get("rc_phase3", [])
    qi_p1 = combined.get("qi_phase1", [])
    qi_p2 = combined.get("qi_phase2", {})
    qi_p3 = combined.get("qi_phase3", [])

    any_rc = rc_p1 or any(n for n in rc_p2.values()) or rc_p3
    any_qi = qi_p1 or any(n for n in qi_p2.values()) or qi_p3
    if any_rc or any_qi:
        if any_rc:
            print(f"  [W1] RC Phase 1 rebuilt: {rc_p1}")
            print(f"  [W1] RC Phase 2 updated: { {t: n for t, n in rc_p2.items() if n} }")
            print(f"  [W1] RC Phase 3 rebuilt: {rc_p3}")
        if any_qi:
            print(f"  [W1] QI Phase 1 rebuilt: {qi_p1}")
            print(f"  [W1] QI Phase 2 updated: { {t: n for t, n in qi_p2.items() if n} }")
            print(f"  [W1] QI Phase 3 rebuilt: {qi_p3}")
    else:
        print("  [W1] No tenant-stamping changes needed (RC + QI already clean).")


def backup_store_vacuum_into(db_path: Path) -> Path:
    """D6 data-safety: consistent pre-migration backup via ``VACUUM INTO``.

    The store runs in WAL mode, so a plain file copy can miss committed data
    still in the -wal (not a valid safety proof). ``VACUUM INTO`` writes a single
    self-consistent snapshot file using a short-lived read connection (no
    migration lock held). Writes ``<db>.premigrate-<UTC>.bak`` beside the store
    and returns it. Raises if the snapshot is not produced — a store is never
    migrated without a provable backup. The rebuild is NOT purely additive
    (0022/0024/0031 rebuild tables), so this backup is the abort-and-restore path.
    """
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_path = db_path.with_name(f"{db_path.name}.premigrate-{ts}.bak")
    if backup_path.exists():
        backup_path = db_path.with_name(
            f"{db_path.name}.premigrate-{ts}-{os.getpid()}.bak")
    src = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        src.execute("VACUUM INTO ?", (str(backup_path),))
    finally:
        src.close()
    if not backup_path.exists():
        raise RuntimeError(
            f"pre-migration VACUUM INTO backup was not created at {backup_path}; "
            "refusing to migrate without a provable backup (D6).")
    print(f"  [backup] pre-migration snapshot → {backup_path}")
    return backup_path


def run(
    project_root: Path | None = None,
    *,
    data_dir: Path | None = None,
    tenant_stamp_fatal: bool = True,
    run_tenant_stamp: bool = True,
    backup: bool = False,
) -> None:
    """Apply future-system migrations through 0031 (+ optional W1 tenant-stamping).

    DB path resolution (mirrors dispatch_cli.py:69-74):
    - Explicit ``data_dir`` argument wins (D4 threading trap): the CLI has already
      resolved the canonical data root, so it threads the EXACT directory here
      instead of letting the runner re-derive ``<project_root>/.vnx-data``.
    - VNX_DATA_DIR_EXPLICIT=1 + VNX_DATA_DIR set: use the explicit data dir's
      state directory (allows targeting a central per-project store for migrations).
    - Fallback: the project-local state directory under project_root.

    ``run_tenant_stamp`` (D-W1): when True (default, direct/strict callers) the W1
    3-phase tenant-stamping runs after the schema walk. The ``vnx migrate`` CLI passes
    FALSE: W1 is DISABLED there because it is currently broken for the store shapes
    vnx migrate targets — a normally-bootstrapped store carries BOTH a ('vnx-dev',
    'default') pool_config row (migration 0020 seed) and a (<pid>, 'default') row
    (init bootstrap seed), so Phase 2's 'vnx-dev'→<pid> restamp always collides on
    pool_config's UNIQUE(project_id, pool_id); legacy stores additionally trip a
    child-FK-widening gap (recommendation_outcomes → recommendations). W1 self-restores
    on failure, so it achieves nothing but heavy checkpoint churn here. The SCHEMA fix
    (tracks.horizon, the actual gap) is delivered by the walk regardless. Re-enable W1
    once the tenant_stamping follow-up ships. ``tenant_stamp_fatal`` governs whether a
    W1 failure aborts (True) or downgrades to a WARNING after the committed walk (False)
    — only consulted when ``run_tenant_stamp`` is True.

    ``backup`` (D6): when True, take a ``VACUUM INTO`` snapshot of the store before
    any mutation.
    """
    _project_root_provided = project_root is not None
    if project_root is None:
        project_root = resolve_project_root(__file__)
    _pytest_db_isolation_guard(project_root)

    if data_dir is not None:
        resolved_data_dir = Path(data_dir).resolve()
    else:
        resolved_data_dir = _resolve_data_dir(
            project_root, project_root_provided=_project_root_provided)
    state_dir = resolved_data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"

    if not db_path.exists():
        raise FileNotFoundError(
            f"runtime_coordination.db not found at {db_path}\n"
            "Run `vnx init` or initialize the schema first."
        )

    print(f"\nVNX migrate_future_system — db: {db_path}")

    if backup:
        backup_store_vacuum_into(db_path)

    _run_pipeline(db_path, project_root,
                  tenant_stamp_fatal=tenant_stamp_fatal,
                  run_tenant_stamp=run_tenant_stamp)


def _run_pipeline(
    db_path: Path, project_root: Path, *,
    tenant_stamp_fatal: bool, run_tenant_stamp: bool = True,
) -> None:
    """Steps A–E of run(): ADR-007 repair → version reconcile → 0022→0031 walk →
    convergence guard → W1 tenant-stamping. Extracted from run() so each stays within
    the 70-line function-size gate; behaviour is identical."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # (A) ADR-007 pre-migration repair: convert any single-column dispatch_id
        # uniqueness into composite UNIQUE(dispatch_id, project_id). No-op when already
        # composite; resolves the tenant only when needed.
        _run_adr007_dispatches_repair(conn, db_path)
        # (B) Numbered version reconciliation: a DB that LIES about its user_version is
        # downgraded to its true version so the walk re-applies what the downgrade
        # exposed. Runs AFTER the repair, BEFORE the numbered walk (PRD §6).
        _run_version_reconciliation(conn, db_path)
        if schema_migration.get_user_version(conn) < 22:
            _assert_dispatches_schema_intact(conn)
        # (C) Numbered migration walk 0022 → 0031 (each idempotent via user_version).
        _run_numbered_walk(conn, project_root)
        # (D) Oscillation guard: the terminal version's manifest MUST hold or abort.
        _assert_manifest_converged(conn)
        # (E) W1 tenant-stamping: close conn first (the 3-phase runner opens its own,
        # avoiding WAL conflicts). The walk (C) + convergence (D) are already COMMITTED
        # per-step, so a non-fatal W1 failure keeps the converged terminal schema. The
        # vnx migrate CLI disables W1 (run_tenant_stamp=False) — see run()'s docstring.
        conn.close()
        conn = None
        if run_tenant_stamp:
            _run_w1_step(db_path, tenant_stamp_fatal=tenant_stamp_fatal)
        else:
            print("  [W1][skip] tenant-stamping disabled by caller (schema-only migrate).")
        print("\n  Migration complete. Schema at user_version (RC verified by manifest converge).\n")
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # vnx-silent-except: rollback best-effort
                pass
        raise
    finally:
        if conn is not None:
            conn.close()


def _run_w1_step(db_path: Path, *, tenant_stamp_fatal: bool) -> None:
    """Run W1 coupled tenant-stamping. On a non-fatal failure (the vnx migrate CLI)
    W1 self-checkpoints + restores its own partial work, so the converged terminal
    schema (tracks.horizon) is preserved and the operator is warned (D-W1)."""
    try:
        _run_w1_coupled_migration(db_path)
    except Exception as w1_exc:
        if tenant_stamp_fatal:
            raise
        print(
            "\n  [W1][WARN] deep tenant-stamping did not converge and was rolled back "
            f"to its pre-W1 checkpoint: {w1_exc}\n"
            "  The schema migration (through 0031, tracks.horizon) DID land and is "
            "committed. Any residual legacy ('vnx-dev'/NULL) tenant rows in runtime/"
            "intelligence tables remain unstamped — re-run once the tenant-stamping "
            "follow-up ships. No data was lost.\n")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\n  [ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
