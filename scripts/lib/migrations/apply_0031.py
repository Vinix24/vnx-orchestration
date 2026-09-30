"""apply_0031.py — ADR-007 runtime tenant + FK repair.

Why Python and not the generic pure-SQL runner:
  0031_runtime_tenant_fk_repair.sql carries the literal 'vnx-dev' in its column
  DEFAULTs and row-copy SELECTs, and it is only correct on the exact clean-v30
  legacy shape. migrate_future_system.apply_migration_v31 owns the real logic:
  it resolves the tenant fail-closed from the DB path and renders it into the
  SQL, picks the adaptive FK-repair for a mixed store, runs the whole rebuild
  FK-off inside BEGIN IMMEDIATE, and checks foreign_key_check + integrity_check
  before commit. This runner delegates to it instead of copying it.

  On a fresh store the repair is required: after 0022 rebuilds dispatches with
  UNIQUE(dispatch_id, project_id), terminal_leases / dispatch_attempts /
  headless_runs / worker_states still reference dispatches(dispatch_id) alone,
  which SQLite reports as "foreign key mismatch" on the next FK check.

Importing migrate_future_system registers its numbered-walk preflights in
schema_migration (v22, v24, v27-v30). By the time 0031 runs in auto_apply every
one of those versions is already applied on this store. Those registrations
are process-global; auto_apply walks with an empty registry and restores the
caller's afterwards, so they never reach the next store in the same process
(vnx migrate, doctor per project), whose 0022 the v22 hook would refuse.

Idempotent: user_version >= 31 → apply_migration_v31 returns without writing.
Applied by: scripts/lib/migrations/auto_apply.py
"""

from __future__ import annotations

import contextlib
import io
import logging
import sqlite3
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _LIB_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (_LIB_DIR, _SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from schema_migration import get_user_version

log = logging.getLogger(__name__)

_TARGET_VERSION = 31


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """Returns True if applied, False if skipped (already at target version).

    *migration_sql_path* is accepted for the auto_apply runner contract;
    apply_migration_v31 reads the same file from schemas/migrations/ itself.
    """
    import migrate_future_system

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        before = get_user_version(conn)
        if before >= _TARGET_VERSION:
            log.debug("apply_0031: already at user_version >= 31; skipped")
            return False
        # apply_migration_v31 reports progress with print(); auto_apply runs inside
        # SessionStart (build_t0_state), where stdout is not a log. Route it to the log.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            migrate_future_system.apply_migration_v31(conn, _REPO_ROOT)
        conn.commit()
        for line in out.getvalue().splitlines():
            if line.strip():
                log.info("apply_0031: %s", line.strip())
        after = get_user_version(conn)
    finally:
        conn.close()
    return after >= _TARGET_VERSION > before
