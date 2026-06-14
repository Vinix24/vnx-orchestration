"""apply_0022.py — track layer migration (ADR-019 zero-to-tracks).

Creates: tracks, track_phase_history, track_dependencies, track_open_items.
Rebuilds dispatches with state CHECK + operator_approved_at column.

Idempotent: PRAGMA user_version >= 22 → skip entirely.
Atomicity: the legacy ``dispatches.project_id`` self-heal AND the v22 rebuild run
inside ONE SAVEPOINT, so a mid-way failure rolls back BOTH (no half-healed schema
is left committed). See ``_apply_22_with_self_heal``.
Applied by: scripts/lib/migrations/auto_apply.py

ADR-007 (docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md):
``dispatches`` is a central-DB table; its tenant key is ``project_id`` and the v22
rebuild adds the composite ``UNIQUE(dispatch_id, project_id)``. On a legacy /
pre-project_id DB the self-heal stamps the VALIDATED tenant — fail-closed, NEVER the
'vnx-dev' sentinel — so the rebuild's INSERT…SELECT carries the CORRECT tenant into
the composite. A wrong-tenant default (the prior bug) is exactly the corruption
PR-A1 fixed in scripts/migrate_future_system.py.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from schema_migration import apply_script_if_below, get_user_version

log = logging.getLogger(__name__)

_V22 = 22


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if *table* exists (PRAGMA table_info returns [] for absent tables)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _col_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if *column* exists in *table* (checked via PRAGMA table_info)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


# ---------------------------------------------------------------------------
# B-N1: DB-path-anchored, fail-closed tenant resolver.
#
# This mirrors PR-A1's ``_resolve_validated_project_id`` in
# scripts/migrate_future_system.py (R3.1) and is reproduced here ON PURPOSE
# rather than imported: importing migrate_future_system runs its module-level
# ``register_preflight(22, _assert_dispatches_schema_intact)``, which installs a
# composite-UNIQUE precondition that the auto_apply legacy lane (this runner)
# intentionally does NOT satisfy until the 0022 rebuild itself builds the
# composite — so the import would break the very path the self-heal exists for.
# Same contract: DB-path → .vnx-project-id marker → VNX_PROJECT_ID env; every
# present source MUST agree; NO source resolves to a silent 'vnx-dev' default.
# Cite ADR-007.
# ---------------------------------------------------------------------------

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
    """Read the nearest ``.vnx-project-id`` marker walking UP from the DB path.

    Anchored on the DB path (NOT cwd) so a stray marker in the operator's working
    tree cannot override the tenant of the database being healed (codex-F1 class).
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

    Precedence/anchor: resolved DB path → .vnx-project-id marker → VNX_PROJECT_ID.
    Every present source MUST agree; any conflict aborts (env can never override the
    DB's real tenant). No source at all → abort. Cite ADR-007.
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
            "ADR-007 project_id conflict — cannot self-heal dispatches with an "
            f"ambiguous tenant identity ({detail}). Resolve the conflict; refusing "
            "to guess (R3.1)."
        )
    if not distinct:
        raise RuntimeError(
            "ADR-007 fail-closed: cannot resolve project_id for the dispatches "
            "self-heal from the DB path, .vnx-project-id marker, or VNX_PROJECT_ID. "
            "No silent 'vnx-dev' default (R3.1). See docs/governance/decisions/"
            "ADR-007-multitenant-project-id-stamping.md"
        )
    return distinct.pop()


def _stamp_legacy_dispatches_project_id(conn: sqlite3.Connection, db_path) -> bool:
    """Add + stamp dispatches.project_id when a legacy table lacks it (B-N1).

    Returns True if a stamp occurred. Resolves the VALIDATED tenant fail-closed
    (never 'vnx-dev') BEFORE any mutation, then writes it to every existing row so
    the v22 rebuild's ``INSERT … SELECT project_id …`` carries the correct tenant
    into the composite ``UNIQUE(dispatch_id, project_id)``. No-op when dispatches is
    absent or already has project_id. Cite ADR-007.

    MUST be called inside the migration savepoint (see ``_apply_22_with_self_heal``)
    so a later rebuild failure rolls this stamp back atomically (B-N3).
    """
    if not _table_exists(conn, "dispatches"):
        return False
    if _col_exists(conn, "dispatches", "project_id"):
        return False
    project_id = _resolve_validated_project_id(db_path)  # fail-closed; may raise (B-N1)
    conn.execute("ALTER TABLE dispatches ADD COLUMN project_id TEXT")
    conn.execute("UPDATE dispatches SET project_id = ?", (project_id,))
    log.info("apply_0022: self-healed legacy dispatches.project_id → %r (ADR-007)", project_id)
    return True


def _apply_22_with_self_heal(conn: sqlite3.Connection, db_path, sql: str) -> bool:
    """Run the legacy project_id self-heal and the v22 migration as ONE atomic unit.

    B-N2 (ordering): gated on ``user_version < 22`` — the SAME check the migration
    uses — so an already-current / partially-migrated DB is never mutated outside the
    version gate (the self-heal no longer runs unconditionally ahead of the gate).
    B-N3 (atomicity): the self-heal stamp and the v22 rebuild share one SAVEPOINT, so
    ANY failure rolls back BOTH; an unresolvable tenant raises before any mutation,
    leaving the DB byte-unchanged.
    """
    if get_user_version(conn) >= _V22:
        return False
    sp = '"vnx_selfheal_22"'
    conn.execute(f"SAVEPOINT {sp}")
    try:
        _stamp_legacy_dispatches_project_id(conn, db_path)
        applied = apply_script_if_below(conn, _V22, sql)
        conn.execute(f"RELEASE SAVEPOINT {sp}")
    except Exception:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
        except sqlite3.Error as rb_err:
            log.warning("apply_0022 self-heal savepoint rollback failed: %s", rb_err)
        raise
    return applied


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """Returns True if applied, False if skipped (already at target version)."""
    sql = migration_sql_path.read_text()

    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit — required for SAVEPOINT semantics
    try:
        applied = _apply_22_with_self_heal(conn, db_path, sql)
    finally:
        conn.close()

    if applied:
        log.info("apply_0022: track layer applied (user_version → 22)")
    else:
        log.debug("apply_0022: already at user_version >= 22; skipped")
    return applied
