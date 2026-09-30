"""auto_apply.py — Wave 6 PR-6.5d N-worker enablement migration hook.

Bridges the gap between the install-time schema (runtime_coordination.sql +
the runtime_coordination_vN.sql files, which bring a fresh DB to
``PRAGMA user_version`` 10) and every newer numbered migration under
schemas/migrations/. Wired into T0 state bootstrap so the
runtime_coordination.db acquires new tables (e.g. pool_config from 0020)
on the first session after a code update — without requiring a separate
``vnx db migrate`` step. For a brand-new project this is the ONLY path that
takes runtime_coordination.db past v10 (``vnx init --step init-db`` creates
quality_intelligence.db alone).

Mechanism:
- Tracks position with ``PRAGMA user_version`` on runtime_coordination.db
  (orthogonal to the legacy ``runtime_schema_version`` row tracker; the
  PRAGMA is owned by this auto-applier).
- Discovers ``NNNN_<name>.sql`` files in schemas/migrations/, sorted
  ascending. Date-named files (``YYYY_MM_<name>.sql``) are a separate family
  that is not ordered by user_version and is never discovered here.
- Every NNNN strictly greater than the current user_version resolves to
  exactly one handler, in this order:
    1. its paired runner ``scripts/lib/migrations/apply_NNNN.py`` (Python
       logic: column-existence guards, adaptive rebuilds, tenant resolution);
    2. the generic pure-SQL runner, when NNNN is in ``PURE_SQL_MIGRATIONS``
       (the ``.sql`` is applied via ``schema_migration.apply_script_if_below``,
       atomic and idempotent on user_version);
    3. a declared no-op, when NNNN is in ``APPLIED_ELSEWHERE`` (the
       migration targets another database, or its runtime half is owned by
       another step; the entry names who applies it).
  A number with none of the three raises ``UnhandledMigrationError``
  (fail-closed). Before this, such a number was silently skipped, and a fresh
  store lost 0015 and then died on 0022 with "no such column: project_id".
- Logs ``migration NNNN auto-applied`` at INFO when the handler reports the
  migration was actually applied (vs. an idempotent skip because the
  legacy version tracker already reflected the change).
- Errors from handlers propagate; the rolled-back transaction is the
  handler's responsibility. The PRAGMA is only advanced when every handler
  returns cleanly.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sqlite3
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_MIGRATIONS_DIR = _REPO_ROOT / "schemas" / "migrations"
_RUNNERS_DIR = Path(__file__).resolve().parent
# A numbered migration is four digits, an underscore, then a name that starts
# with a letter. ``2026_05_intelligence_hygiene.sql`` is date-named: its
# "number" would be 2026, a user_version no store can ever reach.
_MIGRATION_NUM_RE = re.compile(r"^(\d{4})_[A-Za-z][^/]*\.sql$")

#: Migrations whose ``.sql`` is pure, runtime_coordination.db-only SQL that
#: ``schema_migration.apply_script_if_below`` applies as-is. Verified per file:
#: 0028 (tracks.derived_status), 0029 (tracks.track_type + next_action_owner)
#: and 0030 (track_open_items.resolved_at + resolution_reason) are additive
#: ALTERs + indexes on tables 0022/0024 created. Their ALTERs are not
#: idempotent on their own; the user_version gate is what makes them safe on
#: a store that already got them through ``migrate_future_system``'s walk,
#: because that walk stamps the same PRAGMA.
PURE_SQL_MIGRATIONS: FrozenSet[int] = frozenset({28, 29, 30})

#: Migrations under schemas/migrations/ that auto_apply does not apply to
#: runtime_coordination.db, each with who does. A no-op by declaration, not by
#: omission. 0010 sits here because a fresh store reaches user_version 10
#: through init_schema's runtime_coordination_v10.sql, which is a different
#: "10" than migration 0010: its runtime columns are ensured by apply_0015.
APPLIED_ELSEWHERE: Dict[int, str] = {
    10: "runtime + quality_intelligence.db; runtime half via "
        "project_id_migration.run_runtime_coordination_migration and apply_0015",
    11: "quality_intelligence.db via quality_db_init.py",
    12: "quality_intelligence.db via quality_db_init.py",
    13: "quality_intelligence.db via quality_db_init.py",
    14: "quality_intelligence.db via quality_db_init.py",
    16: "quality_intelligence.db via migrate_to_central_vnx.apply_migration_0016",
    18: "global_intelligence.db via IntelligenceAggregator",
    21: "central install DB via central_install_db.init_central_install_schema",
    25: "quality_intelligence.db via quality_db_init.py",
}


class UnhandledMigrationError(RuntimeError):
    """A numbered migration has no runner, is not pure SQL and is not applied elsewhere."""

    def __init__(self, number: int, sql_path: Path) -> None:
        self.number = number
        self.sql_path = sql_path
        super().__init__(
            f"migration {number:04d} ({sql_path.name}) has no handler: no "
            f"apply_{number:04d}.py runner, not in PURE_SQL_MIGRATIONS and not in "
            "APPLIED_ELSEWHERE (scripts/lib/migrations/auto_apply.py). "
            "Refusing to skip it: a skipped migration leaves the store behind a "
            "user_version that claims it."
        )


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


def is_auto_applicable(number: int, runners_dir: Optional[Path] = None) -> bool:
    """True when auto_apply changes runtime_coordination.db for *number*.

    A migration in ``APPLIED_ELSEWHERE`` is handled (no error) but changes
    nothing here, so it does not count.
    """
    run_dir = runners_dir or _RUNNERS_DIR
    return (run_dir / f"apply_{number:04d}.py").exists() or number in PURE_SQL_MIGRATIONS


def highest_auto_applicable_migration(
    migrations_dir: Optional[Path] = None,
    runners_dir: Optional[Path] = None,
) -> Optional[int]:
    """Highest NNNN under *migrations_dir* that auto_apply applies to the runtime DB.

    None when there is none (unmeasurable, not zero).
    """
    mig_dir = migrations_dir or _DEFAULT_MIGRATIONS_DIR
    numbers = [n for n, _ in _discover_migrations(mig_dir) if is_auto_applicable(n, runners_dir)]
    return max(numbers) if numbers else None


def apply_pure_sql_migration(db_path: Path, number: int, sql_path: Path) -> bool:
    """Generic runner: apply *sql_path* atomically when user_version < *number*.

    Returns True if applied, False if the store is already at or past *number*.
    """
    # Lazy import: schema_migration lives in scripts/lib, which build_t0_state and
    # the runners put on sys.path before importing this module.
    from schema_migration import apply_script_if_below

    sql = sql_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit — required for SAVEPOINT semantics
    try:
        return apply_script_if_below(conn, number, sql)
    finally:
        conn.close()


def _apply_one(db_path: Path, number: int, sql_path: Path, run_dir: Path) -> bool:
    runner = _load_runner(run_dir, number)
    if runner is not None:
        return bool(runner.apply_migration(Path(db_path), sql_path))
    if number in PURE_SQL_MIGRATIONS:
        return apply_pure_sql_migration(Path(db_path), number, sql_path)
    if number in APPLIED_ELSEWHERE:
        log.debug(
            "migration %04d is applied elsewhere (%s); no-op on %s",
            number, APPLIED_ELSEWHERE[number], Path(db_path).name,
        )
        return False
    raise UnhandledMigrationError(number, sql_path)


def auto_apply(
    db_path: Path,
    migrations_dir: Optional[Path] = None,
    runners_dir: Optional[Path] = None,
) -> List[int]:
    """Apply all migrations newer than the DB's PRAGMA user_version.

    Returns the list of migration numbers that were applied (logged at INFO).
    A NNNN whose handler reports an idempotent skip is still considered
    "seen" and bumps user_version, but is not added to the returned list.

    Raises ``UnhandledMigrationError`` for a pending number that has no
    handler, and sqlite3.Error (or whatever the runner raises) on failure; the
    PRAGMA user_version is not advanced past a number that raised.
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
        if _apply_one(Path(db_path), number, sql_path, run_dir):
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
