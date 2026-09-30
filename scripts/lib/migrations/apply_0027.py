"""apply_0027.py — tracks.horizon + deliverables view.

Why Python and not the generic pure-SQL runner:
  0027 creates the ``deliverables`` VIEW over dispatches.output_ref and
  dispatches.output_kind. The base schema declares both columns, but the static
  0022 rebuild (apply_0022.py) recreates dispatches without them. SQLite accepts
  the VIEW at DDL time and fails only when it is read ("no such column:
  output_ref"). The columns therefore have to be ensured first, which a .sql file
  cannot do conditionally. ``ensure_dispatches_output_columns`` is that step; it
  is also the v27 preflight migrate_future_system registers for its own walk.

Idempotent: PRAGMA user_version >= 27 → skip entirely.
Atomicity: the column step runs before the SAVEPOINT, exactly like a registered
preflight; its ALTERs are additive and nullable, so a failed 0027 leaves nothing
a rerun trips on. The 0027 statements and the version stamp are atomic.
Applied by: scripts/lib/migrations/auto_apply.py
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from schema_migration import apply_script_if_below, get_user_version

log = logging.getLogger(__name__)

_TARGET_VERSION = 27


def ensure_dispatches_output_columns(conn: sqlite3.Connection) -> None:
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


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """Returns True if applied, False if skipped (already at target version)."""
    sql = migration_sql_path.read_text(encoding="utf-8")

    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit — required for SAVEPOINT semantics
    try:
        if get_user_version(conn) >= _TARGET_VERSION:
            log.debug("apply_0027: already at user_version >= 27; skipped")
            return False
        ensure_dispatches_output_columns(conn)
        applied = apply_script_if_below(conn, _TARGET_VERSION, sql)
    finally:
        conn.close()

    if applied:
        log.info("apply_0027: tracks.horizon + deliverables view applied (user_version → 27)")
    return applied
