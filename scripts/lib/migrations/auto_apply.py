"""auto_apply.py — Wave 6 PR-6.5d N-worker enablement migration hook.

Bridges the gap between the install-time schema (runtime_coordination.sql +
project_id_migration.py bringing the DB to v13) and any newer numbered
migrations under schemas/migrations/. Wired into T0 state bootstrap so the
runtime_coordination.db acquires new tables (e.g. pool_config from 0020)
on the first session after a code update — without requiring a separate
``vnx db migrate`` step.

Mechanism:
- Tracks position with ``PRAGMA user_version`` on runtime_coordination.db
  (orthogonal to the legacy ``runtime_schema_version`` row tracker; the
  PRAGMA is owned by this auto-applier).
- Discovers ``NNNN_*.sql`` files in schemas/migrations/, sorted ascending.
- For each NNNN strictly greater than the current user_version, locates the
  paired runner ``scripts/lib/migrations/apply_NNNN.py`` and invokes its
  ``apply_migration(db_path, sql_path)``. Migrations without a Python
  runner are out-of-scope (they target other databases such as
  quality_intelligence.db, handled by project_id_migration.py and
  quality_db_init.py) and silently skipped.
- Logs ``migration NNNN auto-applied`` at INFO when the runner reports the
  migration was actually applied (vs. an idempotent skip because the
  legacy version tracker already reflected the change).
- Errors from runners propagate; the rolled-back transaction is the
  runner's responsibility. The PRAGMA is only advanced when the runner
  returns cleanly.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_MIGRATIONS_DIR = _REPO_ROOT / "schemas" / "migrations"
_RUNNERS_DIR = Path(__file__).resolve().parent
_MIGRATION_NUM_RE = re.compile(r"^(\d{4})_.*\.sql$")


def _discover_migrations(migrations_dir: Path) -> List[Tuple[int, Path]]:
    """Return ``[(NNNN, sql_path)]`` sorted ascending. Excludes ``*_down.sql``."""
    found: List[Tuple[int, Path]] = []
    if not migrations_dir.is_dir():
        return found
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name.endswith("_down.sql"):
            continue
        m = _MIGRATION_NUM_RE.match(path.name)
        if not m:
            continue
        found.append((int(m.group(1)), path))
    return found


def _load_runner(runners_dir: Path, number: int):
    """Import scripts/lib/migrations/apply_NNNN.py from disk; None if absent."""
    runner_path = runners_dir / f"apply_{number:04d}.py"
    if not runner_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        f"_vnx_migration_runner_{number:04d}", runner_path
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def auto_apply(
    db_path: Path,
    migrations_dir: Optional[Path] = None,
    runners_dir: Optional[Path] = None,
) -> List[int]:
    """Apply all migrations newer than the DB's PRAGMA user_version.

    Returns the list of migration numbers that were applied (logged at INFO).
    A NNNN whose runner reports an idempotent skip is still considered
    "seen" and bumps user_version, but is not added to the returned list.

    Raises sqlite3.Error (or whatever the runner raises) on failure; the
    PRAGMA user_version is not advanced when a runner raises.
    """
    mig_dir = migrations_dir or _DEFAULT_MIGRATIONS_DIR
    run_dir = runners_dir or _RUNNERS_DIR
    applied: List[int] = []

    conn = sqlite3.connect(str(db_path))
    try:
        current = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    finally:
        conn.close()

    highest_seen = current
    for number, sql_path in _discover_migrations(mig_dir):
        if number <= current:
            continue
        runner = _load_runner(run_dir, number)
        if runner is None:
            continue
        was_applied = runner.apply_migration(Path(db_path), sql_path)
        if was_applied:
            log.info("migration %04d auto-applied", number)
            applied.append(number)
        highest_seen = number

    if highest_seen > current:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"PRAGMA user_version = {int(highest_seen)}")
            conn.commit()
        finally:
            conn.close()

    return applied
