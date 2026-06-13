#!/usr/bin/env python3
"""migrate_future_system.py — apply track layer migrations (schema only).

Resolves to the CANONICAL data root (vnx_paths.resolve_data_root: explicit
override > central ~/.vnx-data/<project_id> > project-local .vnx-data > XDG) so
migrations land on the SAME DB the live system reads — fixing the split-brain
where this script previously hard-coded the worktree-local path.

Steps:
  -1. introspection-driven ADR-007 repair: if dispatches is missing project_id
      or the composite UNIQUE(dispatch_id, project_id), rebuild it to conform.
      Decided purely from PRAGMA introspection — NEVER from the (possibly lying)
      runtime_schema_version / user_version. Cites ADR-007.
  0a. user_version reconciliation: lower a falsely-high user_version to the
      level the real schema actually supports, so half-applied migrations re-run.
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
"""

from __future__ import annotations

import re
import sqlite3
import sys
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
from project_scope import current_project_id
import schema_migration


# ---------------------------------------------------------------------------
# CANONICAL DATA-ROOT RESOLUTION (future-state reconciliation, A)
# ---------------------------------------------------------------------------
# Historically this script hard-coded ``project_root / ".vnx-data" / "state"``
# (the worktree-local DEV root), which is the split-brain: build_t0_state.py
# and the receipt processor resolve through vnx_paths to the CENTRAL
# ``~/.vnx-data/<project_id>`` once it exists, so migrations applied here landed
# on a different DB than the one the live system reads — the root cause of
# "migrations 0029/0030 never applied" while the worktree DB sat at v28.
#
# Resolve the SAME canonical root vnx_paths uses (explicit override > central >
# project-local > XDG). Worktree/dev usage stays intact because VNX_DATA_DIR_
# EXPLICIT=1 + VNX_DATA_DIR still wins (precedence rule 1).

def resolve_canonical_state_dir(project_root: Path) -> Path:
    """Resolve the canonical ``<data_root>/state`` dir for migrations.

    Uses ``vnx_paths.resolve_data_root`` (the production-canonical resolver)
    when available, falling back to the legacy worktree-local layout only when
    that import fails. Honors VNX_DATA_DIR_EXPLICIT for tests/worktrees.
    """
    try:
        from vnx_paths import resolve_data_root
    except ImportError:
        return project_root / ".vnx-data" / "state"
    return Path(resolve_data_root(project_root)) / "state"


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
    indexes = list(conn.execute("PRAGMA index_list('dispatches')"))
    composite_unique_exists = False
    for idx in indexes:
        if idx[2]:  # unique flag
            idx_cols = [c[2] for c in conn.execute(f"PRAGMA index_info('{idx[1]}')")]
            if set(idx_cols) == {'dispatch_id', 'project_id'}:
                composite_unique_exists = True
                break
    if not composite_unique_exists:
        raise RuntimeError(
            'dispatches missing UNIQUE(dispatch_id, project_id) — '
            'was added in migration 0017, must be preserved'
        )


# ---------------------------------------------------------------------------
# ADR-007 dispatches composite-UNIQUE repair (introspection-driven, version-blind)
# ---------------------------------------------------------------------------

def _dispatches_has_composite_unique(conn: sqlite3.Connection) -> bool:
    """True iff dispatches carries a UNIQUE index over exactly {dispatch_id, project_id}."""
    for idx in conn.execute("PRAGMA index_list('dispatches')"):
        if idx[2]:  # unique flag
            idx_cols = {c[2] for c in conn.execute(f"PRAGMA index_info('{idx[1]}')")}
            if idx_cols == {"dispatch_id", "project_id"}:
                return True
    return False


def _dispatches_has_single_unique(conn: sqlite3.Connection) -> bool:
    """True iff dispatches still enforces UNIQUE(dispatch_id) by itself."""
    for idx in conn.execute("PRAGMA index_list('dispatches')"):
        if idx[2]:
            idx_cols = [c[2] for c in conn.execute(f"PRAGMA index_info('{idx[1]}')")]
            if idx_cols == ["dispatch_id"]:
                return True
    return False


def _dispatches_repair_needed(conn: sqlite3.Connection) -> bool:
    """Decide — purely from PRAGMA introspection — whether the ADR-007 repair runs.

    NEVER trusts runtime_schema_version / user_version: the central DB's version
    row falsely claimed a composite UNIQUE the table lacks. The actual schema
    (PRAGMA table_info + index_list) is the only source of truth.

    Repair is needed when dispatches exists but is missing the project_id column
    OR the composite UNIQUE(dispatch_id, project_id), or still carries the
    superseded single-column UNIQUE(dispatch_id).
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatches'"
    ).fetchone()
    if not has_table:
        return False
    cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}
    if "project_id" not in cols:
        return True
    return (
        not _dispatches_has_composite_unique(conn)
        or _dispatches_has_single_unique(conn)
    )


_IDENTIFIER = r'(?:"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_$]*)'


def _unquote_identifier(identifier: str) -> str:
    identifier = identifier.strip()
    if identifier.startswith('"') and identifier.endswith('"'):
        return identifier[1:-1].replace('""', '"')
    if identifier.startswith("`") and identifier.endswith("`"):
        return identifier[1:-1].replace("``", "`")
    if identifier.startswith("[") and identifier.endswith("]"):
        return identifier[1:-1]
    return identifier


def _find_table_body(sql: str) -> tuple[int, int]:
    """Return the outer CREATE TABLE body parentheses, respecting SQL quoting."""
    quote: str | None = None
    depth = 0
    start = -1
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote == "'":
            if ch == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
                i += 2
                continue
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == '"' and i + 1 < len(sql) and sql[i + 1] == '"':
                i += 2
                continue
            if ch == '"':
                quote = None
        elif quote == "`":
            if ch == "`" and i + 1 < len(sql) and sql[i + 1] == "`":
                i += 2
                continue
            if ch == "`":
                quote = None
        elif quote == "]":
            if ch == "]":
                quote = None
        elif ch in {"'", '"', "`"}:
            quote = ch
        elif ch == "[":
            quote = "]"
        elif ch == "(":
            if start < 0:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start >= 0:
                return start, i
        i += 1
    raise RuntimeError("dispatches CREATE TABLE SQL has no balanced table body")


def _split_sql_list(sql: str) -> list[str]:
    """Split a comma-delimited SQL list without splitting nested expressions."""
    parts: list[str] = []
    quote: str | None = None
    depth = 0
    start = 0
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote == "'":
            if ch == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
                i += 2
                continue
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == '"' and i + 1 < len(sql) and sql[i + 1] == '"':
                i += 2
                continue
            if ch == '"':
                quote = None
        elif quote == "`":
            if ch == "`" and i + 1 < len(sql) and sql[i + 1] == "`":
                i += 2
                continue
            if ch == "`":
                quote = None
        elif quote == "]":
            if ch == "]":
                quote = None
        elif ch in {"'", '"', "`"}:
            quote = ch
        elif ch == "[":
            quote = "]"
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(sql[start:i].strip())
            start = i + 1
        i += 1
    parts.append(sql[start:].strip())
    return [part for part in parts if part]


def _column_name(component: str) -> str | None:
    match = re.match(rf"\s*({_IDENTIFIER})", component)
    if not match:
        return None
    name = _unquote_identifier(match.group(1))
    if name.upper() in {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"}:
        return None
    return name


def _single_dispatch_unique_constraint(component: str) -> bool:
    rest = component.strip()
    constraint = re.match(rf"(?is)^CONSTRAINT\s+{_IDENTIFIER}\s+(.*)$", rest)
    if constraint:
        rest = constraint.group(1).strip()
    unique = re.match(r"(?is)^UNIQUE\s*\((.*)\)\s*(?:ON\s+CONFLICT\s+\w+)?$", rest)
    if not unique:
        return False
    columns = _split_sql_list(unique.group(1))
    return (
        len(columns) == 1
        and _unquote_identifier(columns[0]).lower() == "dispatch_id"
    )


def _remove_inline_dispatch_unique(component: str) -> str:
    if (_column_name(component) or "").lower() != "dispatch_id":
        return component
    return re.sub(
        r"(?is)\bUNIQUE\b(?:\s+ON\s+CONFLICT\s+(?:ROLLBACK|ABORT|FAIL|IGNORE|REPLACE))?",
        "",
        component,
        count=1,
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _repaired_dispatches_create_sql(original_sql: str, project_id: str) -> str:
    """Transform sqlite_master CREATE SQL while preserving all unrelated DDL."""
    body_start, body_end = _find_table_body(original_sql)
    components = _split_sql_list(original_sql[body_start + 1:body_end])
    repaired: list[str] = []
    has_project_id = False
    for component in components:
        column = _column_name(component)
        if column and column.lower() == "project_id":
            has_project_id = True
        if _single_dispatch_unique_constraint(component):
            continue
        repaired.append(_remove_inline_dispatch_unique(component))

    if not has_project_id:
        repaired.append(
            f"project_id TEXT NOT NULL DEFAULT {_sql_literal(project_id)}"
        )
    repaired.append("UNIQUE(dispatch_id, project_id)")
    suffix = original_sql[body_end + 1:].strip()
    suffix = f" {suffix}" if suffix else ""
    body = ",\n    ".join(repaired)
    return f'CREATE TABLE "dispatches_adr007_new" (\n    {body}\n){suffix}'


def _single_dispatch_unique_index(sql: str) -> bool:
    if not re.match(r"(?is)^\s*CREATE\s+UNIQUE\s+INDEX\b", sql):
        return False
    try:
        start, end = _find_table_body(sql)
    except RuntimeError:
        return False
    columns = _split_sql_list(sql[start + 1:end])
    return (
        len(columns) == 1
        and _unquote_identifier(columns[0]).lower() == "dispatch_id"
    )


def _repair_dispatches_adr007(
    conn: sqlite3.Connection,
    project_id: str | None = None,
) -> bool:
    """Repair dispatches via SQLite's schema-preserving 12-step rebuild.

    The CREATE TABLE, index, and trigger SQL comes from sqlite_master. The repair
    changes only tenant scoping: it adds project_id when absent, removes
    single-column UNIQUE(dispatch_id), and adds UNIQUE(dispatch_id, project_id).

    Idempotent: returns False (no-op) when repair is not needed.

    ADR-007: docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md
    — composite UNIQUE over project_id is mandatory for tenant-scoped natural keys.
    """
    if not _dispatches_repair_needed(conn):
        return False

    if conn.in_transaction:
        raise RuntimeError("ADR-007 dispatches repair requires no active transaction")

    effective_project_id = project_id or current_project_id()
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dispatches'"
    ).fetchone()
    if not table_row or not table_row[0]:
        raise RuntimeError("dispatches CREATE TABLE SQL missing from sqlite_master")
    schema_objects = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE tbl_name='dispatches' AND type IN ('index', 'trigger') "
        "AND sql IS NOT NULL ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END, name"
    ).fetchall()
    recreate_sql = [
        sql for obj_type, _name, sql in schema_objects
        if not (obj_type == "index" and _single_dispatch_unique_index(sql))
    ]
    create_sql = _repaired_dispatches_create_sql(table_row[0], effective_project_id)

    table_xinfo = conn.execute("PRAGMA table_xinfo('dispatches')").fetchall()
    copy_columns = [row[1] for row in table_xinfo if row[6] == 0]
    has_project_id = "project_id" in copy_columns
    insert_columns = list(copy_columns)
    select_exprs = [f'"{column}"' for column in copy_columns]
    params: tuple[str, ...] = ()
    if not has_project_id:
        insert_columns.append("project_id")
        select_exprs.append("?")
        params = (effective_project_id,)
    quoted_insert = ", ".join(f'"{column}"' for column in insert_columns)
    select_sql = ", ".join(select_exprs)

    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if foreign_keys_enabled:
        conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(create_sql)
        conn.execute(
            f'INSERT INTO "dispatches_adr007_new" ({quoted_insert}) '
            f'SELECT {select_sql} FROM "dispatches"',
            params,
        )
        conn.execute('DROP TABLE "dispatches"')
        conn.execute('ALTER TABLE "dispatches_adr007_new" RENAME TO "dispatches"')
        for sql in recreate_sql:
            conn.execute(sql)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"ADR-007 dispatches repair introduced foreign-key violations: {violations[:5]}"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys = ON")
    return True


def _reconcile_lying_user_version(conn: sqlite3.Connection) -> int | None:
    """Lower a falsely-high user_version to the level the real schema supports.

    The half-applied-state hazard: a DB stamped user_version=N (e.g. via an
    import that copied the version row) whose tables do not actually carry the
    columns/constraints migration N installs. Trusting the stamp would skip the
    migration that the schema genuinely still needs.

    We probe the ACTUAL schema (PRAGMA introspection) for the per-version
    invariant each migration establishes, walking down from the claimed version
    until the schema matches. The version is then reset to that true level so
    the run() pipeline re-applies the missing migrations.

    Returns the new user_version when it was lowered, else None.

    Conservative: only ever LOWERS the version (never raises it — raising is the
    migrations' job). Probes are cheap and read-only until the final stamp.
    """
    claimed = schema_migration.get_user_version(conn)
    if claimed <= 0:
        return None

    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    track_cols: set[str] = set()
    if "tracks" in tables:
        track_cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    oi_cols: set[str] = set()
    if "track_open_items" in tables:
        oi_cols = {row[1] for row in conn.execute("PRAGMA table_info('track_open_items')")}

    # Per-version "is this migration actually applied?" invariants. Each entry:
    # version N is satisfied iff the predicate(schema) is True.
    def _v22() -> bool:
        return "tracks" in tables and "track_open_items" in tables
    def _v24() -> bool:
        if "tracks" not in tables:
            return False
        idxs = {row[1] for row in conn.execute("PRAGMA index_list('tracks')")}
        return "ux_tracks_next_up_per_project" in idxs and "project_id" in oi_cols
    def _v27() -> bool:
        return "horizon" in track_cols
    def _v28() -> bool:
        return "derived_status" in track_cols
    def _v29() -> bool:
        return "track_type" in track_cols
    def _v30() -> bool:
        return "resolved_at" in oi_cols

    invariants = [(30, _v30), (29, _v29), (28, _v28), (27, _v27), (24, _v24), (22, _v22)]

    true_version = claimed
    for version, predicate in invariants:
        if claimed >= version and not predicate():
            # Migration `version` is claimed but NOT actually applied. The true
            # version is at most version-1; keep walking down.
            true_version = min(true_version, version - 1)

    if true_version < claimed:
        conn.execute(f"PRAGMA user_version = {true_version}")
        print(
            f"  [reconcile] user_version {claimed} -> {true_version} "
            "(schema did not match the claimed version; re-applying missing migrations)"
        )
        return true_version
    return None


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

_EXPECTED_TRACKS_V22_COLS = frozenset({
    'track_id', 'title', 'goal_state', 'phase', 'next_up', 'sort_order',
    'priority', 'requires_operator_promotion', 'instruction_template',
    'context_composer_rules', 'pr_ref', 'trigger_condition', 'project_id',
    'created_at', 'phase_changed_at', 'completed_at', 'metadata_json',
})


def _assert_tracks_v22_intact(conn: sqlite3.Connection) -> None:
    """Assert tracks table is in v22 state: single-column PK, no composite indexes.

    Codex peer-review §3: preflight must check columns AND unique indexes,
    not just column names.
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for required in ('tracks', 'track_phase_history', 'track_dependencies', 'track_open_items'):
        if required not in tables:
            raise RuntimeError(
                f"Required table '{required}' not found. "
                "Run migration 0022 before 0024."
            )

    cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    missing = _EXPECTED_TRACKS_V22_COLS - cols
    if missing:
        raise RuntimeError(
            f"tracks schema drift before v24 migration: missing columns={missing}. "
            "Expected v22 state."
        )

    # Guard: if composite PK already present (ux_tracks_next_up_per_project from v24),
    # skip — migration was already applied to this tracks table.
    indexes = [row[1] for row in conn.execute("PRAGMA index_list('tracks')")]
    if 'ux_tracks_next_up_per_project' in indexes:
        raise RuntimeError(
            "tracks already has v24 composite index 'ux_tracks_next_up_per_project'. "
            "Migration 0024 should be skipped (user_version should be >= 24)."
        )


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
    """Assert tracks is in the composite-key (v24+) state before adding horizon.

    0027 is purely additive (ALTER TABLE ADD COLUMN + a VIEW), so it only needs
    the tracks table to exist with its composite-key index. It must NOT run on a
    pre-v24 single-column-PK tracks table.
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if 'tracks' not in tables:
        raise RuntimeError(
            "Required table 'tracks' not found. Run migrations 0022 + 0024 before 0027."
        )
    indexes = [row[1] for row in conn.execute("PRAGMA index_list('tracks')")]
    if 'ux_tracks_next_up_per_project' not in indexes:
        raise RuntimeError(
            "tracks missing composite-key index 'ux_tracks_next_up_per_project' "
            "(from 0024). Run migration 0024 before 0027."
        )
    cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    if 'horizon' in cols:
        raise RuntimeError(
            "tracks already has 'horizon' column. Migration 0027 should be "
            "skipped (user_version should be >= 27)."
        )


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
    """Assert tracks has horizon column (v27 state) before adding derived_status."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    if "horizon" not in cols:
        raise RuntimeError(
            "tracks missing 'horizon' column (from 0027). "
            "Run migration 0027 before 0028."
        )
    if "derived_status" in cols:
        raise RuntimeError(
            "tracks already has 'derived_status' column. Migration 0028 should be "
            "skipped (user_version should be >= 28)."
        )


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
    """Assert tracks has derived_status (v28 state) before adding track_type.

    Also guards against double-apply by rejecting if track_type already exists.
    Column-presence check via PRAGMA table_info provides a secondary idempotency
    guard beyond the user_version check in apply_script_if_below.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    if "derived_status" not in cols:
        raise RuntimeError(
            "tracks missing 'derived_status' column (from 0028). "
            "Run migration 0028 before 0029."
        )
    if "track_type" in cols:
        raise RuntimeError(
            "tracks already has 'track_type' column. Migration 0029 should be "
            "skipped (user_version should be >= 29)."
        )


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
    """Assert tracks has track_type (v29 state) before adding resolved_at to track_open_items.

    Also guards against double-apply by rejecting if resolved_at already exists.
    """
    track_cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    if "track_type" not in track_cols:
        raise RuntimeError(
            "tracks missing 'track_type' column (from 0029). "
            "Run migration 0029 before 0030."
        )
    oi_cols = {row[1] for row in conn.execute("PRAGMA table_info('track_open_items')")}
    if "resolved_at" in oi_cols:
        raise RuntimeError(
            "track_open_items already has 'resolved_at' column. Migration 0030 should be "
            "skipped (user_version should be >= 30)."
        )


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

def run(project_root: Path | None = None) -> None:
    """Apply track layer migrations: 0022, 0024, 0027, 0028, 0029, 0030."""
    if project_root is None:
        project_root = resolve_project_root(__file__)

    state_dir = resolve_canonical_state_dir(project_root)
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
        # Step -1: introspection-driven repair of half-applied state. The
        # central DB's runtime_schema_version / user_version may FALSELY claim a
        # composite UNIQUE on dispatches that the table does not have (symptom 4).
        # Repair from the actual PRAGMA schema BEFORE any version-gated step, so
        # the downstream preflight (_assert_dispatches_schema_intact) can pass.
        if _repair_dispatches_adr007(conn):
            print("  [repair] dispatches: added project_id + UNIQUE(dispatch_id, project_id) per ADR-007")
            conn.commit()

        # Step 0: reconcile a lying user_version against the real schema. If the
        # version claims tracks-layer state (>= 22) but the tracks table is
        # absent, the version is half-applied/false — reset it to the highest
        # version the actual schema supports so the migrations below re-run.
        _reconcile_lying_user_version(conn)
        conn.commit()

        current_ver = schema_migration.get_user_version(conn)

        if current_ver < 22:
            _assert_dispatches_schema_intact(conn)

        # Apply 0022 — creates track tables; dispatches rebuilt WITHOUT track FK
        apply_migration(conn, project_root)
        conn.commit()

        # Apply 0024 — rebuilds track tables with composite (track_id, project_id) PKs
        apply_migration_v24(conn, project_root)
        conn.commit()

        # Apply 0027 — additive: tracks.horizon column + deliverables derived view
        apply_migration_v27(conn, project_root)
        conn.commit()

        # Apply 0028 — additive: tracks.derived_status advisory column
        apply_migration_v28(conn, project_root)
        conn.commit()

        # Apply 0029 — additive: tracks.track_type + tracks.next_action_owner
        apply_migration_v29(conn, project_root)
        conn.commit()

        # Apply 0030 — additive: track_open_items.resolved_at + resolution_reason
        apply_migration_v30(conn, project_root)
        conn.commit()

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
