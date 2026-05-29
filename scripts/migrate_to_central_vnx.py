#!/usr/bin/env python3
"""Phase 6 P4 — One-shot data import: live migrator.

Attaches all 4 source DBs (`vnx-dev`, `mc`, `sales-copilot`, `seocrawler-v2`)
in `?mode=ro` and copies their `quality_intelligence.db` and
`runtime_coordination.db` rows into the central
``~/.vnx-data/state/quality_intelligence.db`` and
``~/.vnx-data/state/runtime_coordination.db``, stamping ``project_id`` per
source. Applies migrations 0015 (extend project_id columns) and 0016
(rebuild FTS5) before the import. Each project's INSERTs commit in a
single transaction; failure mid-project rolls back THAT project, others
remain applied.

SAFETY CONTRACT (non-negotiable):
- Default mode is ``--dry-run``. ``--apply`` requires the operator to
  also pass ``--confirm MIGRATE-NOW-2026`` AND respond ``yes`` to a TTY
  prompt. CI invocations must redirect stdin from the literal string
  ``yes\\n`` and supply the confirmation phrase.
- Source DBs are attached read-only via ``file:<path>?mode=ro``. The
  migrator NEVER writes to a source DB.
- Backup before apply: every project's ``.vnx-data/`` is tar.gz'd into
  ``~/Documents/vnx-pre-p4-auto-backup-<ts>/<project_id>.tar.gz`` with a
  SHA256 manifest BEFORE any write to the central DB. If any backup is
  missing/empty, the migrator aborts before opening the central DB for
  writes.
- Idempotent: every INSERT is INSERT OR IGNORE keyed on
  ``(project_id, source_rowid)`` so re-runs are no-ops once successful.
- Abort flag: ``~/.vnx-aggregator/ABORT`` is checked at every loop
  iteration; presence aborts cleanly with exit code 1.

Exit codes:
    0 — dry-run or apply succeeded
    1 — operator-requested abort (ABORT flag present, or confirmation declined)
    2 — registry / config error
    3 — backup or schema-migration failure (central DB untouched)
    4 — verification failure (central DB rolled back to pre-attempt snapshot)

Companion plan: ``claudedocs/2026-04-30-single-vnx-migration-plan.md`` §6 Phase 4
and ``roadmap/features/phase-06-single-system-migration/FEATURE_PLAN.md``
§w6-p4 Risk-Mitigation Steps.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.aggregator.build_central_view import (  # noqa: E402
    ProjectEntry,
    attach_readonly,
    load_registry,
    _default_registry_path,
)
from schema_versioning import (  # noqa: E402
    ensure_schema_meta,
    get_schema_version,
    set_schema_version,
    check_schema_version,
)
from scripts.lib.migrate_import import (  # noqa: E402
    ABORT_FLAG,
    AbortRequested,
    COLLISION_ENTITY_ID_COLUMN,
    COLLISION_ENTITY_TABLE,
    COLLISION_ENTITY_TYPE_COLUMN,
    COLLISION_ENTITY_TYPES_PREFIXED,
    COLLISION_JSON_ARRAY_COLUMNS,
    COLLISION_NAMED_IDENTIFIER_COLUMNS,
    COLLISION_PREFIX_COLUMNS,
    COLLISION_PREFIX_SUFFIXES,
    ImportSummary,
    _IMPORT_BATCH_SIZE,
    _collect_collision_columns,
    _collect_json_array_columns,
    _collect_skipped_rows,
    _column_exists,
    _common_columns,
    _compare_counts,
    _import_table,
    _integer_primary_key,
    _is_collision_column,
    _mapped_central_rowid,
    _now_utc_iso,
    _prefix_json_array,
    _prefix_value,
    _record_rowid_mapping,
    _record_skip,
    _resolve_prior_skip,
    _src_table_present,
    _table_columns,
    _table_exists,
    check_abort,
)
from scripts.lib.migrate_schema import (  # noqa: E402
    BootstrapFailure,
    MigrationOrphanError,
    _rebuild_fts5_code_snippets,
    _rebuild_one_table_dynamic,
)

LOG = logging.getLogger("vnx.migrate.apply")

CONFIRMATION_PHRASE = "MIGRATE-NOW-2026"
DEFAULT_BACKUP_BASE = Path.home() / "Documents"
CENTRAL_DATA_DIR = Path.home() / ".vnx-data" / "state"

MIGRATION_0010_PATH = REPO_ROOT / "schemas" / "migrations" / "0010_add_project_id.sql"
MIGRATION_0015_PATH = REPO_ROOT / "schemas" / "migrations" / "0015_complete_project_id.sql"
MIGRATION_0016_PATH = REPO_ROOT / "schemas" / "migrations" / "0016_rebuild_fts5.sql"
QI_SCHEMA_PATH = REPO_ROOT / "schemas" / "quality_intelligence.sql"

IMPORT_TABLES_QI: tuple[str, ...] = (
    "success_patterns",
    "antipatterns",
    "prevention_rules",
    "pattern_usage",
    "confidence_events",
    "dispatch_metadata",
    "dispatch_experiments",
    "dispatch_pattern_offered",
    "session_analytics",
    "vnx_code_quality",
    "code_snippets",
    "snippet_metadata",
    "quality_trends",
    "quality_alerts",
    "dispatch_quality_context",
    "quality_system_metrics",
    "scan_history",
    "tag_combinations",
    "improvement_suggestions",
    "nightly_digests",
    "governance_metrics",
)

IMPORT_TABLES_RC: tuple[str, ...] = (
    "dispatches",
    "dispatch_attempts",
    "terminal_leases",
    "coordination_events",
    "incident_log",
    "intelligence_injections",
    "retry_budgets",
    "retry_state",
    "escalation_log",
    "execution_targets",
    "inbound_inbox",
    "recommendations",
    "recommendation_outcomes",
)

# ---------------------------------------------------------------------------
# Operator gates
# ---------------------------------------------------------------------------
# check_abort, AbortRequested, ABORT_FLAG, and collision-prefix constants are
# imported from scripts.lib.migrate_import at the top of this module.


class BackupFailure(RuntimeError):
    pass


class VerificationFailure(RuntimeError):
    pass


# BootstrapFailure and MigrationOrphanError are imported from
# scripts.lib.migrate_schema at the top of this module.


def confirm_apply(confirmation: Optional[str], no_prompt: bool = False) -> bool:
    """Enforce the two-factor apply gate: phrase + TTY confirmation."""
    if confirmation != CONFIRMATION_PHRASE:
        LOG.error("--apply requires --confirm %s", CONFIRMATION_PHRASE)
        return False
    if no_prompt:
        return True
    try:
        sys.stdout.write(
            f"About to MIGRATE 4 source projects into {CENTRAL_DATA_DIR}.\n"
            "Type 'yes' to proceed, anything else to abort: "
        )
        sys.stdout.flush()
        ans = sys.stdin.readline().strip().lower()
    except (OSError, EOFError):
        LOG.error("apply confirmation requires interactive stdin")
        return False
    return ans == "yes"


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def _check_backup_access(
    projects: list[ProjectEntry],
) -> list[tuple[str, str, str]]:
    """Probe read access to each project's .vnx-data directory before backup.

    Iterates every project and calls os.listdir() on its .vnx-data dir.
    Returns a list of (project_id, path, error_msg) tuples for directories
    that are inaccessible due to permission or OS errors.  An empty list
    means all accessible directories can be read.

    Missing .vnx-data dirs are skipped — BackupFailure in backup_projects()
    handles the absent-dir case.  This probe targets the macOS TCC
    PermissionError that occurs when the Python binary lacks Full Disk Access,
    so operators get an actionable message before any tarfile work begins.
    """
    failures: list[tuple[str, str, str]] = []
    for project in projects:
        src_dir = project.path / ".vnx-data"
        if not src_dir.is_dir():
            continue  # missing dir caught later by BackupFailure
        try:
            os.listdir(str(src_dir))
        except (PermissionError, OSError) as exc:
            failures.append((project.project_id, str(src_dir), str(exc)))
    return failures


def backup_projects(projects: list[ProjectEntry], backup_base: Path) -> Path:
    """Tar-gz each project's ``.vnx-data/`` to ``backup_base/<ts>/<project_id>.tar.gz``.

    Writes a SHA256 manifest at ``manifest.sha256`` next to the tarballs.
    Raises BackupFailure if any tarball is missing or empty.
    """
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir = backup_base / f"vnx-pre-p4-auto-backup-{ts}"
    out_dir.mkdir(parents=True, exist_ok=False)

    manifest_lines: list[str] = []
    for project in projects:
        check_abort()
        src_dir = project.path / ".vnx-data"
        archive = out_dir / f"{project.project_id}.tar.gz"
        if not src_dir.is_dir():
            LOG.warning(
                "project=%s missing .vnx-data dir at %s; recording empty placeholder",
                project.project_id,
                src_dir,
            )
            archive.write_text("")  # zero-byte sentinel; will fail size check below
        else:
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(src_dir, arcname=f"{project.project_id}/.vnx-data")
        size = archive.stat().st_size if archive.exists() else 0
        if size == 0:
            raise BackupFailure(
                f"backup tar empty/missing for project={project.project_id} at {archive}"
            )
        sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        manifest_lines.append(f"{sha}  {archive.name}  size={size}")

    manifest = out_dir / "manifest.sha256"
    _atomic_write_text(manifest, "\n".join(manifest_lines) + "\n")
    LOG.info("backup complete: %s (manifest=%s)", out_dir, manifest)
    return out_dir


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Backup Retention Policy (PR-WAVE2A-3)
# ---------------------------------------------------------------------------

_BACKUP_DIR_PATTERN = re.compile(r"^vnx-pre-p4-auto-backup-")


def cleanup_old_backups(backup_base: Path, keep_n: int = 3) -> list[Path]:
    """Remove excess backup directories beyond the ``keep_n`` most-recent ones.

    Only directories whose names match ``vnx-pre-p4-auto-backup-*`` are
    considered.  All other directories under ``backup_base`` are left
    untouched regardless of their names or ages.

    Sorting is by ``mtime`` (newest first) so the ``keep_n`` survivors are
    always the most recently written backups.  Ties in mtime are broken
    alphabetically (descending) to produce a deterministic order.

    Args:
        backup_base: Directory that contains the ``vnx-pre-p4-auto-backup-*``
            directories (e.g. ``~/Documents``).
        keep_n: Number of most-recent backup directories to retain.
            Must be >= 1.  Defaults to 3.

    Returns:
        List of :class:`~pathlib.Path` objects that were removed (empty when
        nothing needed cleaning up — i.e. the call is idempotent).

    Raises:
        ValueError: If ``keep_n < 1``.
    """
    if keep_n < 1:
        raise ValueError(f"keep_n must be >= 1, got {keep_n!r}")

    backup_base = backup_base.expanduser()
    if not backup_base.is_dir():
        LOG.debug("cleanup_old_backups: backup_base does not exist: %s", backup_base)
        return []

    # Collect only dirs matching the strict pattern — no unrelated dirs.
    candidates: list[Path] = [
        p
        for p in backup_base.iterdir()
        if p.is_dir() and _BACKUP_DIR_PATTERN.match(p.name)
    ]

    if len(candidates) <= keep_n:
        LOG.info(
            "cleanup_old_backups: %d backup dir(s) found, keep_n=%d — nothing to remove",
            len(candidates),
            keep_n,
        )
        return []

    # Sort newest first: primary key = mtime descending, secondary = name descending.
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)

    to_keep = candidates[:keep_n]
    to_remove = candidates[keep_n:]

    LOG.info(
        "cleanup_old_backups: %d dir(s) found, keeping %d most-recent, removing %d",
        len(candidates),
        len(to_keep),
        len(to_remove),
    )
    for kept in to_keep:
        LOG.debug("  keeping:  %s", kept.name)
    for old in to_remove:
        LOG.debug("  removing: %s", old.name)

    removed: list[Path] = []
    for old_dir in to_remove:
        try:
            import shutil
            shutil.rmtree(old_dir)
            LOG.info("cleanup_old_backups: removed %s", old_dir)
            removed.append(old_dir)
        except OSError as exc:
            LOG.error(
                "cleanup_old_backups: failed to remove %s: %s", old_dir, exc
            )

    return removed


# ---------------------------------------------------------------------------
# Central DB freshness + canonical bootstrap (round-3 fix-forward)
# ---------------------------------------------------------------------------


# Sentinel tables: a populated central DB always has these. When either is
# absent the central is considered "fresh" and requires --fresh-central
# acknowledgement plus a canonical bootstrap before any import can run.
_QI_SENTINEL_TABLE = "success_patterns"
_RC_SENTINEL_TABLE = "dispatches"


def _has_table(db_path: Path, table: str) -> bool:
    """Return True iff ``db_path`` exists and contains ``table``."""
    if not db_path.exists() or db_path.stat().st_size == 0:
        return False
    try:
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type IN ('table','virtual') AND name = ?",
                (table,),
            ).fetchone()
            return row is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _central_is_empty(qi_db: Path, rc_db: Path) -> bool:
    """True if the central DBs lack canonical structure (fresh-deploy state).

    Round-3 fix-forward (Bonus): used to gate the ``--fresh-central``
    operator acknowledgement and to decide whether canonical bootstrap
    must run. Bookkeeping tables (``p4_import_*``) created by a previous
    failed apply do not count as "populated" — only the sentinel
    business tables matter.

    Note: this function returns True for *both* a completely empty DB
    (no tables) and one with only bookkeeping / partial-init tables (e.g.
    ``dispatch_experiments`` from ``retroactive_backfill``). The
    ``_qi_is_partial`` helper distinguishes these two sub-cases so the
    apply flow can handle each correctly.
    """
    qi_fresh = not _has_table(qi_db, _QI_SENTINEL_TABLE)
    rc_fresh = not _has_table(rc_db, _RC_SENTINEL_TABLE)
    return qi_fresh or rc_fresh


def _qi_is_partial(qi_db: Path) -> bool:
    """True if the QI DB has tables but is missing the canonical sentinel.

    OI-011 fix: ``retroactive_backfill._open_tracker()`` creates
    ``dispatch_experiments`` in the central QI DB but skips all other
    tables.  The result is a DB that ``_central_is_empty`` considers
    "fresh" (missing ``success_patterns``) yet already contains data —
    a partial-init limbo where ``bootstrap_qi_db`` should complete the
    schema without requiring the ``--fresh-central`` operator gate
    (which is reserved for truly empty, first-deploy DBs).

    Returns True when ALL of:
    - DB file exists and is non-empty.
    - At least one table is present (not a truly-empty file).
    - The sentinel table (``success_patterns``) is absent.

    ``user_version`` is intentionally NOT checked here: DBs created
    outside ``bootstrap_qi_db`` (e.g. test fixtures, legacy snapshots)
    may have ``user_version=0`` while still containing the sentinel and
    valid data.  Using ``user_version=0`` alone as an indicator would
    incorrectly trigger bootstrap on those DBs and corrupt them.

    ``bootstrap_qi_db`` is idempotent — existing tables and rows
    (including ``dispatch_experiments`` data) are preserved via
    ``CREATE TABLE IF NOT EXISTS`` and column-presence guards.
    """
    if not qi_db.exists() or qi_db.stat().st_size == 0:
        return False
    try:
        con = sqlite3.connect(str(qi_db))
        try:
            table_count = con.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type IN ('table','virtual')"
            ).fetchone()[0]
            if table_count == 0:
                return False  # truly empty file — not partial, treat as fresh
            has_sentinel = (
                con.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type IN ('table','virtual') AND name = ?",
                    (_QI_SENTINEL_TABLE,),
                ).fetchone()
                is not None
            )
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return not has_sentinel


def _init_central_if_missing(qi_db: Path, rc_db: Path) -> None:
    """Bootstrap canonical QI + RC schemas at the given central paths.

    Round-3 fix-forward (Issue 2): the previous apply path created empty
    SQLite files via ``sqlite3.connect(...).close()`` and then expected
    migration 0015 alone to extend "remaining tables" — but with no base
    schema in place, every ALTER TABLE skipped and zero rows landed.

    This helper invokes the canonical init paths used at install time:

    * :func:`scripts.quality_db_init.bootstrap_qi_db` — applies
      ``schemas/quality_intelligence.sql`` plus the 14 imperative
      migrations (``confidence_events``, ``dispatch_pattern_offered``,
      governance/SPC tables, etc.) that are NOT in the base SQL.
    * :func:`scripts.lib.coordination_db.init_schema` — applies
      ``schemas/runtime_coordination.sql`` plus every
      ``runtime_coordination_v{N}.sql`` delta in numeric order.

    Idempotent on subsequent calls: both init paths use
    ``CREATE TABLE IF NOT EXISTS`` and ``ALTER TABLE`` guards. The
    helper only refuses to run when the parent directory cannot be
    created.
    """
    qi_db = Path(qi_db).expanduser()
    rc_db = Path(rc_db).expanduser()
    qi_db.parent.mkdir(parents=True, exist_ok=True)
    rc_db.parent.mkdir(parents=True, exist_ok=True)

    # ``coordination_db.init_schema(state_dir)`` writes to a hardcoded
    # ``state_dir / "runtime_coordination.db"`` filename. If a caller
    # passed a non-canonical RC path the bootstrap would silently write
    # to the wrong file. Fail-fast so the contract is explicit.
    if rc_db.name != "runtime_coordination.db":
        raise BootstrapFailure(
            f"runtime_coordination DB must be named 'runtime_coordination.db'; "
            f"got {rc_db.name!r}"
        )

    # Local imports keep `migrate_to_central_vnx` importable in test
    # contexts where vnx_paths' ensure_env() depends on env state that
    # tests have not yet set up. Both paths are also independent of
    # each other so a partial bootstrap is recoverable on retry.
    import importlib

    qdb = importlib.import_module("scripts.quality_db_init")
    if not qdb.bootstrap_qi_db(qi_db, QI_SCHEMA_PATH):
        raise BootstrapFailure(
            f"quality_db_init.bootstrap_qi_db returned False for {qi_db}"
        )

    cdb = importlib.import_module("coordination_db")
    cdb.init_schema(rc_db.parent)


def _assert_central_tables_exist(
    qi_db: Path,
    rc_db: Path,
    projects: list[ProjectEntry],
) -> None:
    """Pre-import: every IMPORT_TABLES_* table any source has must exist in central.

    Round-3 fix-forward (Issue 4): without this guard, a missing central
    table caused ``_common_columns`` to return ``[]`` and
    ``_import_table`` to silently early-return zero rows imported per
    source — exactly the failure mode the empty-central apply hit.

    Lenient w.r.t. schema drift: a table that exists in NO source is
    treated as acceptably absent (tests use minimal source fixtures).
    Strict against silent skip: if any source has a row of data we'd
    try to import, the central must have the table.

    Raises :class:`BootstrapFailure` with a human-readable list of every
    missing target.
    """
    missing: list[str] = []
    _check_assert_db(qi_db, IMPORT_TABLES_QI, "quality_intelligence",
                     [p.state_dir / "quality_intelligence.db" for p in projects],
                     missing)
    _check_assert_db(rc_db, IMPORT_TABLES_RC, "runtime_coordination",
                     [p.state_dir / "runtime_coordination.db" for p in projects],
                     missing)
    if missing:
        raise BootstrapFailure(
            "central DB(s) missing required import-target tables: "
            + ", ".join(missing)
            + ". Run --fresh-central or repair the central state before retrying."
        )


def _check_assert_db(
    central_db: Path,
    tables: tuple[str, ...],
    label: str,
    source_dbs: list[Path],
    missing_acc: list[str],
) -> None:
    """Append ``label.<table>`` entries to ``missing_acc`` for tables that
    a source has but ``central_db`` is missing. Helper for
    :func:`_assert_central_tables_exist`.
    """
    if not central_db.exists():
        # Whole-DB absence is fatal; record every potentially-relevant table.
        for tbl in tables:
            if any(_has_table(src, tbl) for src in source_dbs):
                missing_acc.append(f"{label}.{tbl}")
        return
    con = sqlite3.connect(str(central_db))
    try:
        for tbl in tables:
            if _table_exists(con, tbl):
                continue
            if any(_has_table(src, tbl) for src in source_dbs):
                missing_acc.append(f"{label}.{tbl}")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Migration application (schemas/migrations/0010 + 0015 + 0016)
# ---------------------------------------------------------------------------


def apply_migration_0010(qi_db: Path, rc_db: Path) -> None:
    """Apply the Phase 0 hot-table ``project_id`` ALTERs to central.

    Round-3 fix-forward (Issue 1): the prior apply flow only invoked
    0015, which extends to "remaining 18 tables" — but the foundational
    0010 (``dispatches``, ``success_patterns``, ``pattern_usage``,
    ``coordination_events``, …) was never applied against a freshly
    bootstrapped central, leaving hot tables without ``project_id``.

    The migration file is partitioned by ``-- @db: runtime_coordination``
    so the QI half runs against ``qi_db`` and the RC half runs against
    ``rc_db``. Each statement is filtered through
    :func:`_apply_alters_idempotently`, which:

    * skips ALTERs for tables not present in the target DB,
    * skips ADD COLUMN for columns that already exist,
    * tolerates ``duplicate column`` errors from concurrent writers, and
    * applies CREATE INDEX / INSERT OR IGNORE clauses if present.

    The companion 0015 then extends to cold tables; the order
    ``0010 → 0015`` matches FEATURE_PLAN §w6-p4.
    """
    if rc_db.exists():
        with sqlite3.connect(str(rc_db)) as _conn:
            ensure_schema_meta(_conn)
            _v = get_schema_version(_conn)
            if _v > 0 and not check_schema_version(_conn, 0, "0010_add_project_id"):
                LOG.info("0010 already applied (schema_version=%d); skipping", _v)
                return

    sql = MIGRATION_0010_PATH.read_text()
    # 0010 uses a slightly different delimiter than 0015. Match the
    # exact partition heading from the file so we don't accidentally
    # split inside an inline comment.
    qi_block, _, rc_block = sql.partition("-- @db: runtime_coordination")
    _apply_alters_idempotently(qi_db, qi_block)
    _apply_alters_idempotently(rc_db, rc_block)

    if rc_db.exists():
        with sqlite3.connect(str(rc_db)) as _conn:
            set_schema_version(_conn, 10)


def apply_migration_0015(qi_db: Path, rc_db: Path) -> None:
    if rc_db.exists():
        with sqlite3.connect(str(rc_db)) as _conn:
            ensure_schema_meta(_conn)
            # Always check prerequisite: v10 required before v15 can run.
            # No bypass for fresh DB (schema_version=0) — 0010 must be applied first.
            if not check_schema_version(_conn, 10, "0015_complete_project_id"):
                _v = get_schema_version(_conn)
                LOG.info("0015 already applied (schema_version=%d); skipping", _v)
                return

    sql = MIGRATION_0015_PATH.read_text()
    qi_block, _, rc_block = sql.partition(
        "-- @db: runtime_coordination (Phase 4 cold tables — 7 tables)"
    )
    _apply_alters_idempotently(qi_db, qi_block)
    _apply_alters_idempotently(rc_db, rc_block)

    if rc_db.exists():
        with sqlite3.connect(str(rc_db)) as _conn:
            set_schema_version(_conn, 15)
    # QI must also reach v15 so apply_migration_0016 can verify the prerequisite
    # on its own connection (QI schema_meta is independent of RC schema_meta).
    if qi_db.exists():
        with sqlite3.connect(str(qi_db)) as _conn:
            ensure_schema_meta(_conn)
            set_schema_version(_conn, 15)


# ---------------------------------------------------------------------------
# Round-5 fix: composite UNIQUE rebuild for cross-tenant collision tables
# ---------------------------------------------------------------------------
#
# Bugs 1 + 2 from P4 round-5: terminal_leases, execution_targets, and
# tag_combinations have a single-column UNIQUE constraint that is NOT
# scoped by project_id. Source DBs across projects share the same
# business keys (T1/T2/T3, target IDs, tag tuples) so INSERT OR IGNORE
# silently drops cross-tenant rows. Migration 0010's
# ``DEFAULT 'vnx-dev'`` ALTER TABLE further compounds this on partial-
# failure recovery: pre-existing rows stamped 'vnx-dev' block the new
# project's correctly-stamped INSERT.
#
# The fix rebuilds each affected table with composite UNIQUE
# (project_id, key_col). After rebuild, cross-tenant rows coexist as
# distinct composite keys; legacy 'vnx-dev'-stamped rows do not block
# new imports for other projects.

# Map: table → business-key column whose old single-column UNIQUE must
# become composite ``UNIQUE(project_id, key)``.
#
# Round-6: extended with the 5 QI tables surfaced by the v4 verify failure
# (session_analytics) and the audit pass over the central schema. Each
# table has a single-column UNIQUE on a tenant-suspect column that would
# silently drop cross-project rows on import.
COMPOSITE_UNIQUE_TABLES_QI: dict[str, str] = {
    # Round-5
    "tag_combinations": "tag_tuple",
    # Round-6
    "session_analytics": "session_id",
    "vnx_code_quality": "file_path",
    "dispatch_quality_context": "dispatch_id",
    "dispatch_metadata": "dispatch_id",
    "dispatch_experiments": "dispatch_id",
}
COMPOSITE_UNIQUE_TABLES_RC: dict[str, str] = {
    "terminal_leases": "terminal_id",
    "execution_targets": "target_id",
}

# Round-6 audit: tenant-suspect column-name patterns. Any single-column
# UNIQUE on a column matching one of these patterns must either be
# rebuilt to composite UNIQUE(project_id, col) (added to the maps above)
# or be explicitly listed as an exception. Documented exceptions cover
# columns whose value is already globally unique by construction (e.g.
# random UUIDs prefixed with project_id, or schema_version pkeys).
_T3_SUSPECT_COLUMN_PATTERN = re.compile(
    r"^(.*_id|.*_path|.*_key|.*_hash|.*_tuple|tag_tuple|session_id|file_path|dispatch_id)$",
    re.IGNORECASE,
)

# Documented exceptions: tables/columns where single-column UNIQUE is
# correct because the column carries a globally-unique value. These are
# accepted by ``_audit_unique_constraints`` without rebuild. Each entry
# must be justified — the audit's purpose is to force every multi-tenant-
# suspect column into an explicit decision (rebuild OR exception).
_T3_AUDIT_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset({
    # ``schema_version.version`` / ``runtime_schema_version.version``:
    # PK-or-UNIQUE on a hand-curated migration tag (e.g. "8.0.4-…").
    # One row per migration step, project-agnostic; cannot collide.
    ("schema_version", "version"),
    ("runtime_schema_version", "version"),
    # Application-generated UUID columns in runtime_coordination.
    # Values are produced by the dispatcher / event log writer with
    # ``uuid.uuid4()``; collision probability across projects is
    # ~2^-122. Listed explicitly so future-you knows the *reason* a
    # single-column UNIQUE is OK here, vs. a real T3 regression.
    ("dispatch_attempts", "attempt_id"),
    ("coordination_events", "event_id"),
    ("incident_log", "incident_id"),
    ("escalation_log", "escalation_id"),
    ("intelligence_injections", "injection_id"),
    ("inbound_inbox", "event_id"),
    ("recommendations", "recommendation_id"),
    # ``retry_budgets.budget_key`` is a structured string
    # ``"{entity_type}:{entity_id}:{incident_class}"`` whose entity_id
    # component already namespaces values per-tenant (terminals/dispatches
    # are scoped to a project). Single-column UNIQUE is correct.
    ("retry_budgets", "budget_key"),
})


def _is_prefix_rewritten_column(table: str, col: str) -> bool:
    """True if the importer rewrites ``table.col`` values to
    ``"<project_id>:<original>"`` on import. Such columns are globally
    unique by construction — single-column UNIQUE is safe.

    Mirrors the prefix-rewrite logic in :func:`_import_table` /
    :func:`_collect_collision_columns`. Kept as an audit-side helper to
    avoid pulling the importer's runtime mapping into a structural check.
    """
    if col in COLLISION_PREFIX_COLUMNS:
        return True
    if col in COLLISION_NAMED_IDENTIFIER_COLUMNS:
        return True
    for suffix in COLLISION_PREFIX_SUFFIXES:
        if col.endswith(suffix):
            return True
    # ``coordination_events.entity_id`` is conditionally rewritten based
    # on entity_type. Treat it as rewritten for audit purposes — the
    # value is namespaced for the prefix-eligible types and is otherwise
    # an unstructured FK that doesn't carry a UNIQUE constraint.
    if table == COLLISION_ENTITY_TABLE and col == COLLISION_ENTITY_ID_COLUMN:
        return True
    return False

# Hardcoded rebuild SQL per table. Keeping the SQL canonical (no
# post-ALTER ", project_id ..." cruft) means EXPLAIN QUERY PLAN and
# downstream tooling sees the same shape as a fresh-bootstrap DB.
_REBUILD_SQL: dict[str, str] = {
    "terminal_leases": """
        CREATE TABLE terminal_leases_new (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            terminal_id         TEXT    NOT NULL,
            state               TEXT    NOT NULL DEFAULT 'idle',
            dispatch_id         TEXT    REFERENCES dispatches (dispatch_id),
            generation          INTEGER NOT NULL DEFAULT 1,
            leased_at           TEXT,
            expires_at          TEXT,
            last_heartbeat_at   TEXT,
            released_at         TEXT,
            metadata_json       TEXT    DEFAULT '{}',
            project_id          TEXT    NOT NULL DEFAULT 'vnx-dev',
            UNIQUE (project_id, terminal_id)
        );
        INSERT INTO terminal_leases_new (
            id, terminal_id, state, dispatch_id, generation,
            leased_at, expires_at, last_heartbeat_at, released_at,
            metadata_json, project_id
        )
        SELECT
            id, terminal_id, state, dispatch_id, generation,
            leased_at, expires_at, last_heartbeat_at, released_at,
            metadata_json, project_id
        FROM terminal_leases;
        DROP TABLE terminal_leases;
        ALTER TABLE terminal_leases_new RENAME TO terminal_leases;
        CREATE INDEX IF NOT EXISTS idx_lease_state ON terminal_leases (state);
        CREATE INDEX IF NOT EXISTS idx_lease_dispatch ON terminal_leases (dispatch_id);
        CREATE INDEX IF NOT EXISTS idx_terminal_leases_project ON terminal_leases (project_id);
    """,
    "execution_targets": """
        CREATE TABLE execution_targets_new (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id           TEXT    NOT NULL,
            target_type         TEXT    NOT NULL,
            terminal_id         TEXT,
            capabilities_json   TEXT    NOT NULL DEFAULT '[]',
            health              TEXT    NOT NULL DEFAULT 'offline',
            health_checked_at   TEXT,
            model               TEXT,
            registered_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            metadata_json       TEXT    DEFAULT '{}',
            project_id          TEXT    NOT NULL DEFAULT 'vnx-dev',
            UNIQUE (project_id, target_id)
        );
        INSERT INTO execution_targets_new (
            id, target_id, target_type, terminal_id, capabilities_json,
            health, health_checked_at, model, registered_at, updated_at,
            metadata_json, project_id
        )
        SELECT
            id, target_id, target_type, terminal_id, capabilities_json,
            health, health_checked_at, model, registered_at, updated_at,
            metadata_json, project_id
        FROM execution_targets;
        DROP TABLE execution_targets;
        ALTER TABLE execution_targets_new RENAME TO execution_targets;
        CREATE INDEX IF NOT EXISTS idx_target_type ON execution_targets (target_type);
        CREATE INDEX IF NOT EXISTS idx_target_terminal ON execution_targets (terminal_id);
        CREATE INDEX IF NOT EXISTS idx_target_health ON execution_targets (health);
        CREATE INDEX IF NOT EXISTS idx_execution_targets_project ON execution_targets (project_id);
    """,
    "tag_combinations": """
        CREATE TABLE tag_combinations_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_tuple TEXT NOT NULL,
            occurrence_count INTEGER DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            phases TEXT,
            terminals TEXT,
            outcomes TEXT,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            UNIQUE (project_id, tag_tuple)
        );
        INSERT INTO tag_combinations_new (
            id, tag_tuple, occurrence_count, first_seen, last_seen,
            phases, terminals, outcomes, project_id
        )
        SELECT
            id, tag_tuple, occurrence_count, first_seen, last_seen,
            phases, terminals, outcomes, project_id
        FROM tag_combinations;
        DROP TABLE tag_combinations;
        ALTER TABLE tag_combinations_new RENAME TO tag_combinations;
        CREATE INDEX IF NOT EXISTS idx_tag_tuple ON tag_combinations (tag_tuple);
        CREATE INDEX IF NOT EXISTS idx_tag_combinations_project ON tag_combinations (project_id);
    """,
}


# Tables whose FK declarations would break when terminal_leases drops its
# single-column UNIQUE on terminal_id. SQLite parses the FK reference at
# every schema validation, and a reference to a non-UNIQUE/PK column is a
# "foreign key mismatch" error. We rebuild these tables WITHOUT the FK
# to terminal_leases. This is safe in central context because:
#   - worker_states is a runtime-state-tracking table; it doesn't need a
#     hard FK constraint to enforce referential integrity at the central
#     consolidation layer (each project's worker_states is single-tenant
#     and unrelated to the central composite-keyed terminal_leases).
#   - the canonical per-project schema still has the FK; only the central
#     DB drops it.
_REBUILD_DEPENDENT_FK_SQL: dict[str, str] = {
    "worker_states": """
        CREATE TABLE worker_states_new (
            terminal_id      TEXT    NOT NULL,
            dispatch_id      TEXT    NOT NULL,
            state            TEXT    NOT NULL DEFAULT 'initializing',
            last_output_at   TEXT,
            state_entered_at TEXT    NOT NULL,
            stall_count      INTEGER NOT NULL DEFAULT 0,
            blocked_reason   TEXT,
            metadata_json    TEXT,
            created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (terminal_id)
        );
        INSERT INTO worker_states_new
            SELECT terminal_id, dispatch_id, state, last_output_at,
                   state_entered_at, stall_count, blocked_reason,
                   metadata_json, created_at, updated_at
            FROM worker_states;
        DROP TABLE worker_states;
        ALTER TABLE worker_states_new RENAME TO worker_states;
        CREATE INDEX IF NOT EXISTS idx_worker_state ON worker_states (state);
        CREATE INDEX IF NOT EXISTS idx_worker_dispatch ON worker_states (dispatch_id);
    """,
}


def _has_composite_project_unique(
    con: sqlite3.Connection,
    table: str,
    key_col: str,
) -> bool:
    """Return True iff a UNIQUE index on ``(project_id, key_col)`` (in either
    order) is already present.

    Used to make :func:`apply_composite_unique_constraints` idempotent
    so a re-run of ``--apply`` against an already-rebuilt central is a
    no-op rather than an attempted second rebuild that would fail
    because the column-level ``UNIQUE`` is already gone.
    """
    if not _table_exists(con, table):
        return False
    target = {"project_id", key_col}
    for row in con.execute(f"PRAGMA index_list({table})"):
        idx_name = row[1]
        is_unique = bool(row[2])
        if not is_unique:
            continue
        cols = [r[2] for r in con.execute(f"PRAGMA index_info({idx_name})")]
        if len(cols) == 2 and set(cols) == target:
            return True
    return False


def _rebuild_one_table(
    con: sqlite3.Connection,
    table: str,
    key_col: str,
) -> None:
    """Rebuild a single table to swap single-column UNIQUE → composite
    ``UNIQUE(project_id, key_col)``.

    Idempotent: returns early if the composite UNIQUE is already present
    or if the table is missing in this DB. The rebuild runs inside the
    caller's transaction frame; failure raises and rolls back the entire
    composite-unique pass.
    """
    if not _table_exists(con, table):
        LOG.info("composite-unique skip: %s not present in this DB", table)
        return
    if _has_composite_project_unique(con, table, key_col):
        LOG.info("composite-unique already applied: %s(%s,project_id)", table, key_col)
        return
    if not _column_exists(con, table, "project_id"):
        # 0010/0015 must have run first. Defensive: refuse to rebuild
        # a pre-Phase-0 table or we will lose the project_id semantics.
        raise BootstrapFailure(
            f"composite-unique pre-condition failed: {table} has no "
            "project_id column. Apply migrations 0010+0015 first."
        )

    LOG.info("composite-unique rebuild: %s → UNIQUE(project_id,%s)", table, key_col)
    if table in _REBUILD_SQL:
        # Round-5 path: hardcoded canonical SQL for the 3 RC/QI tables
        # whose schemas are pinned to the source-of-truth .sql files.
        sql = _REBUILD_SQL[table]
        for stmt in _iter_sql_statements(sql):
            con.execute(stmt)
    else:
        # Round-6 path: schema-introspection rebuild for tables whose
        # live schema is the product of bootstrap + imperative migrations
        # (e.g. ``dispatch_metadata`` accumulates cqs/normalized_status/
        # cqs_components/target_open_items/... columns across releases).
        # Hardcoding their SQL would drift; introspection stays correct.
        _rebuild_one_table_dynamic(con, table, key_col)


# _rebuild_one_table_dynamic is imported from scripts.lib.migrate_schema
# at the top of this module (OI-1533, part 2/3).


def _audit_unique_constraints(qi_db: Path, rc_db: Path) -> None:
    """Round-6 regression guard: scan central schema for unhandled T3 patterns.

    Runs AFTER ``apply_composite_unique_constraints``. By that point, every
    table listed in :data:`COMPOSITE_UNIQUE_TABLES_QI` /
    :data:`COMPOSITE_UNIQUE_TABLES_RC` should have its single-column
    UNIQUE swapped for ``UNIQUE(project_id, key)``. Any remaining
    single-column UNIQUE on a tenant-suspect column means a NEW table
    was added without composite-key handling — fail-fast so the operator
    decides explicitly.

    Suspect columns are those matching :data:`_T3_SUSPECT_COLUMN_PATTERN`
    (``*_id``, ``*_path``, ``*_key``, ``*_hash``, plus the literal
    ``session_id`` / ``tag_tuple`` / ``file_path`` / ``dispatch_id``).
    Documented exceptions live in :data:`_T3_AUDIT_EXCEPTIONS`.

    Raises :class:`BootstrapFailure` listing every offending
    ``<table>.<column>`` pair. Tables without a ``project_id`` column
    are skipped (they are pre-multi-tenant or singleton tables and are
    not exposed to cross-project import collisions).
    """
    findings: list[str] = []
    for db_path in (qi_db, rc_db):
        if not db_path.exists():
            continue
        con = sqlite3.connect(str(db_path))
        try:
            tables = [
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' "
                    "AND name NOT LIKE 'p4_import_%'"
                )
            ]
            for table in tables:
                if not _column_exists(con, table, "project_id"):
                    # Singleton or pre-multi-tenant table — not exposed
                    # to cross-project collisions.
                    continue
                for idx_row in con.execute(f"PRAGMA index_list({table})"):
                    is_unique = bool(idx_row[2])
                    if not is_unique:
                        continue
                    # PRAGMA index_list columns: (seq, name, unique, origin, partial).
                    # ``origin`` is ``'pk'`` for the auto-index that backs a PRIMARY
                    # KEY, ``'u'`` for a UNIQUE constraint, ``'c'`` for an explicit
                    # CREATE [UNIQUE] INDEX. PK-backed indexes are the table's
                    # identity and are handled by collision-prefix-rewrite for
                    # cross-tenant scoping (``dispatches.dispatch_id``,
                    # ``pattern_usage.pattern_id``); they are NOT a T3 regression
                    # signal.
                    origin = idx_row[3] if len(idx_row) > 3 else None
                    if origin == "pk":
                        continue
                    idx_name = idx_row[1]
                    cols = [r[2] for r in con.execute(
                        f"PRAGMA index_info({idx_name})"
                    )]
                    if len(cols) != 1:
                        continue
                    col = cols[0]
                    if col == "project_id":
                        continue
                    if (table, col) in _T3_AUDIT_EXCEPTIONS:
                        continue
                    # Prefix-rewritten columns are globally unique by
                    # construction (the importer rewrites their value
                    # to ``<project_id>:<orig>``), so single-column
                    # UNIQUE is safe regardless of the column name
                    # matching the suspect pattern.
                    if _is_prefix_rewritten_column(table, col):
                        continue
                    if not _T3_SUSPECT_COLUMN_PATTERN.match(col):
                        continue
                    findings.append(f"{table}.{col}")
        finally:
            con.close()

    if findings:
        raise BootstrapFailure(
            "Multi-tenant T3 pattern detected: "
            + ", ".join(sorted(findings))
            + " has single-column UNIQUE on a tenant-suspect column. "
            "Either add to COMPOSITE_UNIQUE_REBUILDS or document as exception."
        )


def _rebuild_dependent_fk_holders(con: sqlite3.Connection) -> None:
    """Rebuild tables whose FK declarations would dangle after a
    composite-unique rebuild drops a referenced single-column UNIQUE.

    Currently only ``worker_states`` (FK on
    ``terminal_leases(terminal_id)``). Idempotent: detects rebuild
    completion via the absence of FK in PRAGMA foreign_key_list.
    """
    for dep_table in _REBUILD_DEPENDENT_FK_SQL.keys():
        if not _table_exists(con, dep_table):
            continue
        fk_rows = list(con.execute(f"PRAGMA foreign_key_list({dep_table})"))
        # Skip if no FK to terminal_leases survives (already rebuilt).
        has_lease_fk = any((row[2] or "") == "terminal_leases" for row in fk_rows)
        if not has_lease_fk:
            LOG.info("dependent-fk rebuild skip: %s has no terminal_leases FK", dep_table)
            continue
        LOG.info("dependent-fk rebuild: %s (drop FK to terminal_leases)", dep_table)
        for stmt in _iter_sql_statements(_REBUILD_DEPENDENT_FK_SQL[dep_table]):
            con.execute(stmt)


def apply_composite_unique_constraints(qi_db: Path, rc_db: Path) -> None:
    """Round-5 fix: rebuild collision-prone tables with composite UNIQUE.

    Runs after migrations 0010 + 0015 (so the project_id column exists)
    and BEFORE the per-project import (so the import sees the new
    constraints). Wrapped in a single transaction per DB; any failure
    rolls back the rebuild and propagates so the outer pre-snapshot
    restore engages.

    Idempotent: re-running against an already-rebuilt central is a
    no-op via :func:`_has_composite_project_unique`.

    Note on FK handling: terminal_leases.dispatch_id has a FOREIGN KEY
    to dispatches. SQLite does not enforce FKs unless
    ``PRAGMA foreign_keys = ON`` is set, and the migrator does NOT set
    it during this rebuild because the dispatches data may not yet be
    imported (rebuild happens BEFORE per-project import). We run a
    ``foreign_key_check`` after rebuild for visibility, but only log
    findings rather than fail — the import phase will populate
    dispatches and the FK becomes consistent at that point.
    """
    for db_path, mapping, is_rc in (
        (qi_db, COMPOSITE_UNIQUE_TABLES_QI, False),
        (rc_db, COMPOSITE_UNIQUE_TABLES_RC, True),
    ):
        if not db_path.exists():
            LOG.warning("skipping composite-unique on missing DB: %s", db_path)
            continue
        con = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            # FKs OFF during rebuild so DROP TABLE on a referenced table
            # does not trip the check. We restore at the end.
            con.execute("PRAGMA foreign_keys = OFF")
            # Round-6: ``legacy_alter_table = ON`` disables SQLite 3.25+'s
            # automatic rewriting of view/trigger references during
            # ALTER TABLE RENAME. Without this, the canonical QI schema's
            # ``cost_per_dispatch`` view (which joins dispatch_metadata to
            # session_analytics) trips during the rename step: the view
            # body references both tables, and SQLite's reference walker
            # sees a transient missing-table state mid-rebuild and aborts
            # with ``error in view cost_per_dispatch: no such table``.
            # Since we always rename ``<table>_p4r6_new`` back to its
            # original name within the same transaction, view references
            # are restored before any view is queried — the legacy
            # behavior is correct for our pattern.
            con.execute("PRAGMA legacy_alter_table = ON")
            con.execute("BEGIN")
            try:
                # On RC: rebuild FK-holders FIRST so terminal_leases'
                # subsequent UNIQUE drop doesn't dangle worker_states' FK.
                if is_rc:
                    _rebuild_dependent_fk_holders(con)
                for table, key_col in mapping.items():
                    _rebuild_one_table(con, table, key_col)
                # Visibility-only FK check; warn rather than fail because
                # the dispatches table may legitimately be empty at this
                # point in the apply flow (rebuild precedes import).
                fk_violations = list(con.execute("PRAGMA foreign_key_check"))
                if fk_violations:
                    LOG.info(
                        "composite-unique post-rebuild FK check: %d findings (informational; "
                        "import phase will populate referenced tables)",
                        len(fk_violations),
                    )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            finally:
                con.execute("PRAGMA foreign_keys = ON")
                con.execute("PRAGMA legacy_alter_table = OFF")
        finally:
            con.close()


def _strip_leading_sql_comments(stmt: str) -> str:
    """Drop leading whitespace + ``--`` comment lines from a SQL chunk.

    A naive ``split(";")`` over a SQL file bundles the leading comment block
    with the first SQL statement after it. Without this helper, that whole
    chunk would be matched by ``stmt.startswith("--")`` and silently dropped,
    causing the first ALTER after each comment block to never execute. See
    Finding 1 in PR #432 review.
    """
    lines = stmt.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or line.startswith("--"):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip()


def _iter_sql_statements(sql: str) -> Iterable[str]:
    """Yield non-empty, comment-stripped SQL statements split on ``;``.

    Used by both ``_apply_alters_idempotently`` and ``apply_migration_0016``
    so a single comment-handling rule applies to every migration. Note: this
    is a deliberately simple split — none of our migrations contain ``;``
    inside string literals, so we don't pay the cost of a full SQL tokenizer.
    """
    uncommented_sql = "\n".join(
        line
        for line in sql.splitlines()
        if not line.lstrip().startswith("--")
    )
    for raw_stmt in uncommented_sql.split(";"):
        stmt = _strip_leading_sql_comments(raw_stmt)
        if stmt:
            yield stmt


def _apply_alters_idempotently(db_path: Path, sql_block: str) -> None:
    """Apply ALTER TABLE / CREATE INDEX statements, skipping duplicates and missing tables.

    SQLite does not support ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so we
    parse the SQL block and per-statement check existence via PRAGMA.
    """
    if not db_path.exists():
        LOG.warning("skipping migration on missing DB: %s", db_path)
        return
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA foreign_keys = ON")
        for stmt in _iter_sql_statements(sql_block):
            stmt_upper = stmt.upper()
            if stmt_upper.startswith("ALTER TABLE"):
                _try_alter(con, stmt)
            elif stmt_upper.startswith("CREATE INDEX") or stmt_upper.startswith("INSERT OR IGNORE"):
                with contextlib.suppress(sqlite3.OperationalError):
                    con.execute(stmt)
        con.commit()
    finally:
        con.close()


def _try_alter(con: sqlite3.Connection, stmt: str) -> None:
    """Best-effort ALTER TABLE ADD COLUMN that skips duplicates and missing tables."""
    parts = stmt.split()
    try:
        table_idx = parts.index("TABLE") + 1
        table = parts[table_idx]
    except (ValueError, IndexError):
        return
    if not _table_exists(con, table):
        LOG.info("alter skipped: table not present: %s", table)
        return
    if "ADD COLUMN" in stmt.upper() and "PROJECT_ID" in stmt.upper():
        if _column_exists(con, table, "project_id"):
            return
    try:
        con.execute(stmt)
    except sqlite3.OperationalError as exc:
        # Tolerate "duplicate column name" if a parallel writer already added it.
        if "duplicate column" in str(exc).lower():
            return
        raise


# _rebuild_fts5_code_snippets is imported from scripts.lib.migrate_schema
# at the top of this module (OI-1536, part 2/3).


def apply_migration_0016(qi_db: Path) -> None:
    """Rebuild FTS5 indexes in quality_intelligence.db with project_id.

    Column list is derived from ``PRAGMA table_info`` at apply time per
    ADR-009 (schema-first migrations).  Project-id attribution uses
    ``snippet_metadata`` with ``p4_import_rowid_map`` as fallback; orphan
    rows with no recoverable project_id raise ``MigrationOrphanError``
    before the DROP so the database is left intact.

    All statements run inside an explicit BEGIN/COMMIT frame so a failure
    after ``DROP TABLE code_snippets`` rolls back the drop and the original
    table survives intact.  ``executescript`` is unsafe here because it
    issues an implicit COMMIT before running, defeating the wrapper.  See
    Finding 4 in PR #432 review.
    """
    if not qi_db.exists():
        LOG.warning("skipping FTS5 rebuild: %s missing", qi_db)
        return
    con = sqlite3.connect(str(qi_db), isolation_level=None)
    try:
        ensure_schema_meta(con)
        # Always check prerequisite: v15 required before v16 can run.
        # No bypass for fresh DB (schema_version=0) — 0015 must be applied first.
        if not check_schema_version(con, 15, "0016_rebuild_fts5"):
            _qi_v = get_schema_version(con)
            LOG.info("0016 already applied (schema_version=%d); skipping", _qi_v)
            return

        if not _table_exists(con, "code_snippets"):
            LOG.info("code_snippets vtab not present; skipping FTS5 rebuild")
            return
        cols = [r[1] for r in con.execute("PRAGMA table_info(code_snippets)")]
        if "project_id" in cols:
            LOG.info("FTS5 already includes project_id; skipping rebuild")
            return
        con.execute("BEGIN")
        try:
            # Perf index (round-4 fix): must exist before the correlated
            # subquery in _rebuild_fts5_code_snippets to avoid O(N×M) scans.
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_snippet_metadata_rowid "
                "ON snippet_metadata(snippet_rowid)"
            )
            _rebuild_fts5_code_snippets(con)
            con.execute(
                "INSERT OR IGNORE INTO schema_version (version, description) "
                "VALUES ('8.4.0-fts5-project-id', "
                "'Phase 6 P4: rebuild FTS5 virtual tables with project_id column')"
            )
            set_schema_version(con, 16)
            con.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.OperationalError):
                con.execute("ROLLBACK")
            raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Import: per-project, per-table, single transaction
# ---------------------------------------------------------------------------
# ImportSummary, _import_table, and their helpers are imported from
# scripts.lib.migrate_import at the top of this module.


def _ensure_idempotency_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS p4_import_idempotency (
            project_id TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_rowid INTEGER NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (project_id, source_table, source_rowid)
        )
        """
    )


def _ensure_skipped_table(con: sqlite3.Connection) -> None:
    """Audit table for rows the migrator could NOT insert (conflicts/IGNOREs).

    Created alongside ``p4_import_idempotency`` so every dry-run / apply
    keeps a durable record of rows that were dropped by ``INSERT OR
    IGNORE`` and would otherwise be silently lost. See Finding 2 in PR
    #432 review.

    Round-2 fix (Finding 3): added ``run_id`` and ``resolved_at``
    columns so verify_import only treats *unresolved* skips from the
    *current* run as discrepancies. Without this, a conflict logged on
    run 1 that succeeded on run 2 would still flag verify_import as
    failed forever — breaking idempotent re-runs.

    The schema is migrated in-place when an older p4_import_skipped is
    encountered (best-effort ALTER TABLE ADD COLUMN, tolerating
    duplicates from concurrent migrators).
    """
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS p4_import_skipped (
            project_id TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_rowid INTEGER NOT NULL,
            reason TEXT NOT NULL,
            skipped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            run_id TEXT,
            resolved_at TEXT,
            PRIMARY KEY (project_id, source_table, source_rowid)
        )
        """
    )
    # Best-effort migration for pre-round-2 deployments where the table
    # already exists without the new columns. ALTER TABLE ADD COLUMN is
    # idempotent under the duplicate-column tolerance pattern used elsewhere
    # in this module.
    existing = {r[1] for r in con.execute("PRAGMA table_info(p4_import_skipped)")}
    if "run_id" not in existing:
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute("ALTER TABLE p4_import_skipped ADD COLUMN run_id TEXT")
    if "resolved_at" not in existing:
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute("ALTER TABLE p4_import_skipped ADD COLUMN resolved_at TEXT")


def _generate_run_id() -> str:
    """Per-apply run identifier used to scope skipped-row resolution.

    Runs at the top of the apply flow and threaded down to every call
    site that writes to ``p4_import_skipped`` so that ``verify_import``
    can filter out historical / resolved skips.
    """
    return f"run-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{os.getpid()}"


def reset_idempotency_state(qi_db: Path, rc_db: Path) -> dict[str, int]:
    """Round-5: wipe p4 bookkeeping tables so next --apply re-evaluates every row.

    Returns a per-DB count of rows deleted across the three tables for
    operator visibility. Targets only the migrator's own bookkeeping —
    never touches imported business data. Idempotent: tables that don't
    yet exist are silently skipped (returns 0 for that DB).
    """
    counts: dict[str, int] = {}
    for label, db_path in (("qi", qi_db), ("rc", rc_db)):
        if not db_path.exists():
            counts[label] = 0
            continue
        con = sqlite3.connect(str(db_path))
        try:
            total = 0
            for tbl in ("p4_import_idempotency", "p4_import_skipped", "p4_import_rowid_map"):
                if not _table_exists(con, tbl):
                    continue
                cur = con.execute(f"DELETE FROM {tbl}")
                total += cur.rowcount or 0
            con.commit()
            counts[label] = total
        finally:
            con.close()
    return counts


def _ensure_rowid_map_table(con: sqlite3.Connection) -> None:
    """Map source rowids to imported central rowids for link-table rewrites."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS p4_import_rowid_map (
            project_id TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_rowid INTEGER NOT NULL,
            central_rowid INTEGER NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (project_id, source_table, source_rowid)
        )
        """
    )


def import_project(
    central_qi: Path,
    central_rc: Path,
    project: ProjectEntry,
    run_id: Optional[str] = None,
) -> list[ImportSummary]:
    """Import one project's QI + RC tables in a single transaction per DB."""
    out: list[ImportSummary] = []
    qi_src = project.state_dir / "quality_intelligence.db"
    rc_src = project.state_dir / "runtime_coordination.db"

    if qi_src.is_file() and central_qi.exists():
        con = sqlite3.connect(str(central_qi), isolation_level=None)
        try:
            _ensure_idempotency_table(con)
            _ensure_skipped_table(con)
            _ensure_rowid_map_table(con)
            attach_readonly(con, "src", qi_src)
            con.execute("BEGIN")
            try:
                for tbl in IMPORT_TABLES_QI:
                    summary = _import_table(con, "src", project, tbl, run_id=run_id)
                    if summary.rows_inserted or summary.rows_skipped_existing:
                        out.append(
                            ImportSummary(
                                project_id=summary.project_id,
                                db_name="quality_intelligence.db",
                                table=summary.table,
                                rows_inserted=summary.rows_inserted,
                                rows_skipped_existing=summary.rows_skipped_existing,
                            )
                        )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            finally:
                with contextlib.suppress(sqlite3.OperationalError):
                    con.execute("DETACH DATABASE src")
        finally:
            con.close()

    if rc_src.is_file() and central_rc.exists():
        con = sqlite3.connect(str(central_rc), isolation_level=None)
        try:
            _ensure_idempotency_table(con)
            _ensure_skipped_table(con)
            _ensure_rowid_map_table(con)
            attach_readonly(con, "src", rc_src)
            con.execute("BEGIN")
            try:
                for tbl in IMPORT_TABLES_RC:
                    summary = _import_table(con, "src", project, tbl, run_id=run_id)
                    if summary.rows_inserted or summary.rows_skipped_existing:
                        out.append(
                            ImportSummary(
                                project_id=summary.project_id,
                                db_name="runtime_coordination.db",
                                table=summary.table,
                                rows_inserted=summary.rows_inserted,
                                rows_skipped_existing=summary.rows_skipped_existing,
                            )
                        )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            finally:
                with contextlib.suppress(sqlite3.OperationalError):
                    con.execute("DETACH DATABASE src")
        finally:
            con.close()
    return out


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_import(
    central_qi: Path,
    central_rc: Path,
    projects: list[ProjectEntry],
    run_id: Optional[str] = None,
) -> dict:
    """Recompute per-project row counts + simple column checksums; raises on drift.

    ``run_id`` (Finding 3 round 2): when supplied, only unresolved skips
    *from this run* contribute to discrepancies. ``--verify-only``
    invocations leave it ``None`` and only filter on
    ``resolved_at IS NULL`` so historical-but-now-imported rows do not
    re-flag the verification step. Read-only failure paths
    (Finding 4 round 2) surface as a list under ``read_errors`` and as
    discrepancies of type ``read_error``.
    """
    report: dict = {
        "per_project": {},
        "checksums": {},
        "skipped_rows": [],
        "read_errors": [],
        "discrepancies": [],
    }
    for project in projects:
        check_abort()
        qi_src = project.state_dir / "quality_intelligence.db"
        rc_src = project.state_dir / "runtime_coordination.db"
        per_table: dict[str, dict] = {}
        if qi_src.is_file() and central_qi.exists():
            per_table.update(
                _compare_counts(
                    central_qi,
                    "quality_intelligence.db",
                    qi_src,
                    project,
                    IMPORT_TABLES_QI,
                    report["read_errors"],
                )
            )
        if rc_src.is_file() and central_rc.exists():
            per_table.update(
                _compare_counts(
                    central_rc,
                    "runtime_coordination.db",
                    rc_src,
                    project,
                    IMPORT_TABLES_RC,
                    report["read_errors"],
                )
            )
        report["per_project"][project.project_id] = per_table
    report["skipped_rows"].extend(
        _collect_skipped_rows(central_qi, "quality_intelligence.db", run_id=run_id)
    )
    report["skipped_rows"].extend(
        _collect_skipped_rows(central_rc, "runtime_coordination.db", run_id=run_id)
    )
    report["discrepancies"] = _verification_discrepancies(report)
    return report


def _verification_discrepancies(report: dict) -> list[dict[str, object]]:
    """Build the unified discrepancy list used by ``raise_for_verification_failures``.

    Round-2 fix (Finding 4): unreadable source DBs / tables now surface
    as ``read_error`` discrepancies instead of being absorbed into a
    silent zero-count. Operators see them prominently in the verify
    payload and ``--verify-only`` exits 4.
    """
    discrepancies: list[dict[str, object]] = []
    for project_id, per_table in report.get("per_project", {}).items():
        for table_label, counts in per_table.items():
            source_rows = int(counts.get("source_rows", 0))
            central_rows = int(counts.get("central_rows_for_project", 0))
            if source_rows != central_rows:
                discrepancies.append(
                    {
                        "type": "count_mismatch",
                        "project_id": project_id,
                        "table": table_label,
                        "source_rows": source_rows,
                        "central_rows_for_project": central_rows,
                    }
                )
    for skipped in report.get("skipped_rows", []):
        discrepancies.append(
            {
                "type": "skipped_row",
                **skipped,
            }
        )
    for err in report.get("read_errors", []):
        discrepancies.append({"type": "read_error", **err})
    return discrepancies


def raise_for_verification_failures(report: dict) -> None:
    discrepancies = report.get("discrepancies") or _verification_discrepancies(report)
    if not discrepancies:
        return
    sample = discrepancies[0]
    raise VerificationFailure(
        f"{len(discrepancies)} verification discrepancy(s); first={sample}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for migrate_to_central_vnx."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="ACTUALLY perform the import")
    parser.add_argument(
        "--confirm",
        type=str,
        default=None,
        help=f"Required confirmation phrase ({CONFIRMATION_PHRASE}) when --apply is set",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Skip TTY prompt (for CI / fixture tests; still requires --confirm)",
    )
    parser.add_argument("--dry-run-manifest", type=Path, default=None,
                        help="Path to dry-run JSON manifest (must exist and be <24h old in --apply)")
    parser.add_argument("--backup-base", type=Path, default=DEFAULT_BACKUP_BASE)
    parser.add_argument("--central-state", type=Path, default=CENTRAL_DATA_DIR,
                        help="Override central state dir (used by tests)")
    parser.add_argument("--verify-only", action="store_true",
                        help="Run verification suite against the central DBs and exit")
    parser.add_argument(
        "--fresh-central",
        action="store_true",
        help=(
            "Operator acknowledgement that the central DB is fresh (missing or "
            "empty). Required when --apply targets a central state dir without "
            "canonical schemas — guards against accidental first-deploy runs."
        ),
    )
    parser.add_argument(
        "--reset-idempotency",
        action="store_true",
        help=(
            "Round-5: clear p4_import_idempotency, p4_import_skipped, "
            "p4_import_rowid_map before importing. Use after schema rebuilds "
            "(e.g. composite-UNIQUE migration) when prior bookkeeping is no "
            "longer accurate. Only effective with --apply."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout on completion")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--cleanup-backups",
        action="store_true",
        default=False,
        help=(
            "OPT-IN: after a successful --apply, remove backup directories "
            "in --backup-base beyond the --keep-backups most-recent ones. "
            "Only directories matching 'vnx-pre-p4-auto-backup-*' are touched. "
            "No-op without --apply."
        ),
    )
    parser.add_argument(
        "--keep-backups",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Number of most-recent 'vnx-pre-p4-auto-backup-*' directories to "
            "retain when --cleanup-backups is set (default: 3)."
        ),
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        metavar="PROJECT_ID",
        action="append",
        dest="projects_filter",
        help=(
            "Migrate only this project_id (from registry). Enables targeted "
            "re-run after partial failure without re-applying already-succeeded "
            "projects. Repeat to include multiple projects: --project a --project b. "
            "Default (omitted): all registry projects."
        ),
    )
    parser.add_argument(
        "--test-apply",
        action="store_true",
        default=False,
        help=(
            "Run the full bootstrap + migration chain against a TEMP central "
            "directory (in /tmp), using real source DBs read-only. Verifies "
            "the apply sequence succeeds without touching the live central DB. "
            "Mutually exclusive with --verify-only. Combine with --project to "
            "test one project at a time. The temp dir is auto-cleaned after "
            "completion (success or failure)."
        ),
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _run_apply(args: argparse.Namespace, projects: list[ProjectEntry]) -> int:
    """Execute the apply / verify / backup flow.

    Receives a fully-parsed ``args`` namespace (from :func:`_parse_args`) and
    a filtered ``projects`` list (from :func:`main`).  Handles the
    ``--verify-only``, dry-run, ``--test-apply``, and ``--apply`` code paths.
    """
    # Env isolation pre-flight: warn if VNX_DATA_DIR is set and does not
    # match the --central-state argument.  This detects cross-repo env
    # contamination (e.g. VNX_DATA_DIR inherited from a different tmux pane).
    # WARN only — not abort — because the flag value always takes precedence
    # over env vars in the migrator's own code paths.
    _env_vnx_data_dir = os.environ.get("VNX_DATA_DIR", "")
    if _env_vnx_data_dir:
        _central_arg = str(args.central_state.expanduser().resolve())
        _env_resolved = str(Path(_env_vnx_data_dir).expanduser().resolve()) if _env_vnx_data_dir else ""
        if _env_resolved and _env_resolved != _central_arg:
            LOG.warning(
                "env leak detected: VNX_DATA_DIR=%s vs --central-state=%s. "
                "Run scripts/check_env_isolation.sh for details.",
                _env_vnx_data_dir,
                args.central_state,
            )

    # --test-apply: redirect central_state to a temp dir and run the full
    # bootstrap + migration chain without touching the live central DB.
    # Source DBs remain read-only (unchanged from normal apply flow).
    # The temp dir is auto-cleaned after completion regardless of exit code.
    if args.test_apply:
        import shutil
        import tempfile

        real_central = args.central_state.expanduser()
        tmp_dir = tempfile.mkdtemp(prefix="vnx-test-apply-")
        try:
            print(
                f"TEST MODE -- no writes to live central DB at {real_central}",
                flush=True,
            )
            LOG.info(
                "test-apply: redirecting central_state from %s to temp dir %s",
                real_central,
                tmp_dir,
            )
            # Override central_state and implicit flags for the test run.
            args.central_state = Path(tmp_dir)
            args.fresh_central = True
            args.no_prompt = True
            # --test-apply implies --apply so the real apply path runs.
            args.apply = True
            # Confirmation phrase is set implicitly for test-apply.
            args.confirm = CONFIRMATION_PHRASE
            # Run the apply through the normal code path.  We CANNOT call
            # main() recursively here because that re-parses argv and would
            # infinite-loop on args.test_apply.  Instead, fall through to the
            # existing apply logic below with the modified args namespace.
        except Exception as exc:
            LOG.error("test-apply setup failed: %s", exc)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return 2

        # Register cleanup to run after the apply block regardless of outcome.
        _test_apply_tmp_dir: str | None = tmp_dir
    else:
        _test_apply_tmp_dir = None

    central_state: Path = args.central_state.expanduser()
    central_qi = central_state / "quality_intelligence.db"
    central_rc = central_state / "runtime_coordination.db"

    if args.verify_only:
        report = verify_import(central_qi, central_rc, projects)
        print(json.dumps(report, indent=2, default=str))
        try:
            raise_for_verification_failures(report)
        except VerificationFailure as exc:
            LOG.error("verification failed: %s", exc)
            return 4
        return 0

    if not args.apply:
        # Default DRY-RUN: delegate to migrate_dry_run for canonical behavior.
        cmd = [sys.executable, str(REPO_ROOT / "scripts" / "migrate_dry_run.py")]
        if args.registry:
            cmd.extend(["--registry", str(args.registry)])
        if args.json:
            cmd.append("--json")
        LOG.info("default mode: invoking dry-run preflight (no writes)")
        return subprocess.call(cmd)

    if not confirm_apply(args.confirm, no_prompt=args.no_prompt):
        return 1

    if args.dry_run_manifest is not None:
        if not args.dry_run_manifest.is_file():
            LOG.error("dry-run manifest missing: %s", args.dry_run_manifest)
            return 2
        age_s = time.time() - args.dry_run_manifest.stat().st_mtime
        if age_s > 24 * 3600:
            LOG.error(
                "dry-run manifest is %s hours old (> 24); regenerate before applying",
                int(age_s // 3600),
            )
            return 2

    try:
        check_abort()
    except AbortRequested as exc:
        LOG.error("aborting: %s", exc)
        return 1

    # In test-apply mode, source DBs are opened read-only (unchanged from
    # normal flow) but no backup of source data is taken — there is nothing
    # to roll back because the central write target is a temp dir that is
    # auto-cleaned at the end of the run.
    if _test_apply_tmp_dir is not None:
        LOG.info("test-apply: skipping backup phase (writes target temp dir only)")
        backup_dir: Path | None = None
    else:
        inaccessible = _check_backup_access(projects)
        if inaccessible:
            for pid, path, err in inaccessible:
                LOG.error(
                    "Cannot access source directory for project_id=%s: %s. "
                    "macOS TCC blocking: enable Full Disk Access for %s in "
                    "System Settings → Privacy & Security → Full Disk Access, "
                    "then re-run.",
                    pid,
                    err,
                    sys.executable,
                )
            return 3

        try:
            backup_dir = backup_projects(projects, args.backup_base)
        except (BackupFailure, AbortRequested) as exc:
            LOG.error("backup phase failed: %s", exc)
            return 3

    # All paths below may return early (exit codes 1/3/4).  When in
    # test-apply mode the temp dir must be cleaned up on every exit path.
    # _test_apply_cleanup() is defined here as a closure so it can access
    # _test_apply_tmp_dir without threading it through every helper.
    def _test_apply_cleanup() -> None:
        if _test_apply_tmp_dir is not None:
            import shutil as _shutil
            LOG.info("test-apply: cleaning up temp central dir %s", _test_apply_tmp_dir)
            _shutil.rmtree(_test_apply_tmp_dir, ignore_errors=True)

    central_state.mkdir(parents=True, exist_ok=True)

    # OI-011 fix: detect partial-init QI DB BEFORE the --fresh-central gate.
    # A partial DB (e.g. dispatch_experiments only from retroactive_backfill)
    # has tables but user_version=0 — it is NOT fresh and must NOT require
    # --fresh-central, since existing data would be incorrectly abandoned.
    # _init_central_if_missing is safe to call on a partial DB (idempotent).
    central_qi_partial = _qi_is_partial(central_qi)

    # Round-3 fix-forward: detect a fresh central BEFORE creating empty
    # DB files. Without this gate, an operator who has just blown away
    # ~/.vnx-data/state/ would see "import complete" against empty DBs.
    # OI-011: partial-init QI (data present, not truly empty) bypasses this
    # gate — _init_central_if_missing handles the bootstrap idempotently.
    central_was_fresh = _central_is_empty(central_qi, central_rc)
    if central_was_fresh and not args.fresh_central and not central_qi_partial:
        LOG.error(
            "central appears fresh (missing canonical schema at %s); "
            "pass --fresh-central to acknowledge first-deploy bootstrap",
            central_state,
        )
        _test_apply_cleanup()
        return 1

    if not central_qi.exists():
        LOG.warning("central QI db missing; creating empty: %s", central_qi)
        sqlite3.connect(str(central_qi)).close()
    if not central_rc.exists():
        LOG.warning("central RC db missing; creating empty: %s", central_rc)
        sqlite3.connect(str(central_rc)).close()

    pre_snapshot = _snapshot_central(central_qi, central_rc)
    try:
        if central_was_fresh and not central_qi_partial:
            LOG.info(
                "central is fresh; running canonical bootstrap "
                "(quality_db_init + coordination_db.init_schema)"
            )
            _init_central_if_missing(central_qi, central_rc)
        elif central_qi_partial:
            # OI-011 fix: partial-init QI DB (e.g. only dispatch_experiments
            # created by retroactive_backfill._open_tracker()) — complete the
            # bootstrap via _init_central_if_missing which handles both QI
            # (idempotent: dispatch_experiments data preserved) and RC init.
            # ADR-007: bootstrap_qi_db wires composite PKs over project_id.
            LOG.info(
                "partial-init central QI DB detected (tables present but "
                "user_version=0 or missing sentinel '%s'); running canonical "
                "bootstrap to complete schema — existing data preserved",
                _QI_SENTINEL_TABLE,
            )
            _init_central_if_missing(central_qi, central_rc)

        # Order matters (Round-3 Issue 1 + Finding 3 in PR #432 review,
        # extended with Round-5 step 2.5 for composite UNIQUE):
        #   1. 0010 ALTER TABLE — adds project_id to hot tables (foundation)
        #   2. 0015 ALTER TABLE — extends project_id to remaining cold tables
        #   2.5 Composite UNIQUE rebuild — terminal_leases, execution_targets,
        #       tag_combinations swap single-col UNIQUE for
        #       UNIQUE(project_id, key) so cross-tenant rows coexist (round-5).
        #   3. _assert_central_tables_exist — fail-fast before per-row losses
        #   4. import_project loop — populates code_snippets + snippet_metadata
        #   5. 0016 FTS5 rebuild — joins snippet_metadata to assign project_id
        # Running 0016 before the import loop rebuilds FTS5 over an empty
        # central table → useless index. Running composite UNIQUE AFTER the
        # import would either (a) lose data when the rebuild discovers a
        # conflict, or (b) require us to reapply the import; running it
        # BEFORE the import is the only safe order.
        apply_migration_0010(central_qi, central_rc)
        apply_migration_0015(central_qi, central_rc)
        apply_composite_unique_constraints(central_qi, central_rc)
        # Round-6 regression guard: every tenant-suspect single-column
        # UNIQUE in central must either be rebuilt to composite UNIQUE
        # (handled above) or be in the documented exceptions list. This
        # makes adding a new T3-pattern table without scoping a hard
        # failure rather than a silent post-import discrepancy.
        _audit_unique_constraints(central_qi, central_rc)
        _assert_central_tables_exist(central_qi, central_rc, projects)
    except BootstrapFailure as exc:
        LOG.error("bootstrap assertion failed: %s", exc)
        _restore_snapshot_safe(pre_snapshot, central_qi, central_rc)
        _test_apply_cleanup()
        return 3
    except sqlite3.Error as exc:
        LOG.error("schema migration failed: %s", exc)
        _restore_snapshot_safe(pre_snapshot, central_qi, central_rc)
        _test_apply_cleanup()
        return 3
    except (FileNotFoundError, ImportError) as exc:
        LOG.error("canonical bootstrap failed: %s", exc)
        _restore_snapshot_safe(pre_snapshot, central_qi, central_rc)
        _test_apply_cleanup()
        return 3

    if args.reset_idempotency:
        cleared = reset_idempotency_state(central_qi, central_rc)
        LOG.info(
            "round-5: cleared p4_import bookkeeping (qi=%s rows, rc=%s rows)",
            cleared.get("qi", 0),
            cleared.get("rc", 0),
        )

    run_id = _generate_run_id()
    summaries: list[ImportSummary] = []
    failed_projects: list[str] = []
    for project in projects:
        try:
            check_abort()
            summaries.extend(import_project(central_qi, central_rc, project, run_id=run_id))
        except AbortRequested as exc:
            LOG.error("aborting: %s", exc)
            _test_apply_cleanup()
            return 1
        except Exception as exc:
            LOG.error("project=%s import failed; rolled back THAT project: %s", project.project_id, exc)
            failed_projects.append(project.project_id)

    try:
        apply_migration_0016(central_qi)
    except sqlite3.Error as exc:
        LOG.error("FTS5 rebuild (migration 0016) failed: %s", exc)
        _restore_snapshot_safe(pre_snapshot, central_qi, central_rc)
        _test_apply_cleanup()
        return 3

    try:
        verify_report = verify_import(central_qi, central_rc, projects, run_id=run_id)
        raise_for_verification_failures(verify_report)
        if failed_projects:
            raise VerificationFailure(
                f"project import failures recorded for: {', '.join(failed_projects)}"
            )
    except VerificationFailure as exc:
        LOG.error("verification failed: %s", exc)
        _restore_snapshot_safe(pre_snapshot, central_qi, central_rc)
        _test_apply_cleanup()
        return 4
    except Exception as exc:
        LOG.error("verification raised: %s", exc)
        _restore_snapshot_safe(pre_snapshot, central_qi, central_rc)
        _test_apply_cleanup()
        return 4

    out_payload = {
        "applied_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "backup_dir": str(backup_dir) if backup_dir is not None else None,
        "central_qi": str(central_qi),
        "central_rc": str(central_rc),
        "imported_summary": [
            {
                "project_id": s.project_id,
                "db": s.db_name,
                "table": s.table,
                "rows_inserted": s.rows_inserted,
                "rows_skipped_existing": s.rows_skipped_existing,
            }
            for s in summaries
        ],
        "failed_projects": failed_projects,
        "verification": verify_report,
    }
    if _test_apply_tmp_dir is not None:
        out_payload["test_apply"] = True
        out_payload["test_apply_temp_dir"] = _test_apply_tmp_dir

    if args.json:
        print(json.dumps(out_payload, indent=2, default=str))
    else:
        if _test_apply_tmp_dir is not None:
            print("TEST MODE complete. Temp central dir will be cleaned up.")
        else:
            print(f"P4 import complete. Backup: {backup_dir}")
        print(f"  Central QI: {central_qi}")
        print(f"  Central RC: {central_rc}")
        for s in summaries:
            print(f"  [{s.project_id}] {s.db_name} {s.table}: +{s.rows_inserted} ({s.rows_skipped_existing} idempotent skips)")
        if failed_projects:
            print(f"  FAILED projects (rolled back): {', '.join(failed_projects)}")

    if args.cleanup_backups and _test_apply_tmp_dir is None:
        removed = cleanup_old_backups(args.backup_base, keep_n=args.keep_backups)
        if removed:
            msg = f"  Backup cleanup: removed {len(removed)} old backup dir(s) (keep_n={args.keep_backups})"
        else:
            msg = f"  Backup cleanup: nothing to remove (keep_n={args.keep_backups})"
        if args.json:
            out_payload["backup_cleanup_removed"] = [str(p) for p in removed]
        else:
            print(msg)

    # Cleanup must run last on the success path.
    _test_apply_cleanup()
    if _test_apply_tmp_dir is not None:
        print(f"TEST MODE -- temp central dir cleaned up: {_test_apply_tmp_dir}")

    return 0


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entry-point: parse args, load registry, filter projects, delegate to _run_apply."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    registry_path = args.registry or _default_registry_path()
    try:
        projects = load_registry(registry_path)
    except FileNotFoundError:
        print(f"ERROR: registry not found at {registry_path}", file=sys.stderr)
        return 2

    if args.projects_filter:
        valid_ids = {p.project_id for p in projects}
        unknown = set(args.projects_filter) - valid_ids
        if unknown:
            LOG.error(
                "--project filter contains unknown project_id(s): %s; "
                "valid project_ids from registry: %s",
                sorted(unknown),
                sorted(valid_ids),
            )
            return 2
        filter_set = set(args.projects_filter)
        selected: list = []
        for p in projects:
            if p.project_id in filter_set:
                selected.append(p)
            else:
                LOG.info("skipping project %s — not in --project filter", p.project_id)
        projects = selected
        LOG.info(
            "--project filter active: migrating subset %s",
            [p.project_id for p in projects],
        )

    # --test-apply and --verify-only are mutually exclusive.
    if args.test_apply and args.verify_only:
        LOG.error("--test-apply and --verify-only are mutually exclusive")
        return 2

    return _run_apply(args, projects)


def _snapshot_central(qi: Path, rc: Path) -> dict[str, Path]:
    """WAL-safe snapshot via SQLite backup API.

    A plain ``shutil.copy2`` of just the ``.db`` file is unsafe under
    ``journal_mode = WAL``: committed state may live in the ``-wal``
    sidecar and metadata in ``-shm``. Copying only the base file produces
    a torn snapshot that cannot be reliably restored. The online backup
    API instead emits a transactionally consistent single-file copy
    regardless of the source journal mode (Finding 1 round 2).
    """
    snapshots: dict[str, Path] = {}
    for label, db in (("qi", qi), ("rc", rc)):
        if db.exists():
            # Label is part of the filename so two different live DBs that
            # happen to share a path (e.g. in fixture tests) don't end up
            # writing the same tmp file twice and clobbering each other.
            tmp = db.with_suffix(db.suffix + f".presnap.{label}.{os.getpid()}")
            if tmp.exists():
                tmp.unlink()
            src = sqlite3.connect(str(db))
            try:
                dest = sqlite3.connect(str(tmp))
                try:
                    src.backup(dest)
                finally:
                    dest.close()
            finally:
                src.close()
            snapshots[label] = tmp
    return snapshots


def _restore_snapshot_safe(snapshots: dict[str, Path], qi: Path, rc: Path) -> None:
    """Call :func:`_restore_snapshot`, absorbing any exception into a logged warning.

    Callers already hold the primary exception on the call stack (inside an
    ``except`` handler). If the snapshot restore itself fails, that secondary
    exception would replace the primary one in the traceback the operator sees,
    hiding the real root cause. This wrapper ensures:

    1. The rollback is always *attempted*.
    2. A rollback failure is logged at ERROR level with full details.
    3. The primary exception remains the one that ultimately propagates (the
       caller's ``except`` block returns a non-zero exit code, so the primary
       exception's message is what reaches the operator's terminal).
    """
    try:
        _restore_snapshot(snapshots, qi, rc)
    except Exception as rollback_exc:
        LOG.error(
            "snapshot restore (rollback) failed: %r — "
            "central DB may be in a partially-modified state; "
            "restore manually from backup if needed.",
            rollback_exc,
        )


def _restore_snapshot(snapshots: dict[str, Path], qi: Path, rc: Path) -> None:
    """Restore each snapshot by replaying the transactionally consistent copy
    over the live DB through the SQLite backup API.

    Using the backup API (rather than ``shutil.copy2`` of a single ``.db``
    file) leaves the live DB's journal-mode and any open handles in a
    coherent state; it also tolerates concurrent ``-wal``/``-shm`` files
    on the target path that would otherwise survive a raw file copy and
    re-corrupt the just-restored state (Finding 1 round 2).
    """
    for label, tmp in snapshots.items():
        target = qi if label == "qi" else rc
        try:
            # Drop sidecars before restoring so we don't replay stale WAL
            # frames against the freshly copied base file.
            for suffix in ("-wal", "-shm"):
                sidecar = target.with_name(target.name + suffix)
                if sidecar.exists():
                    with contextlib.suppress(OSError):
                        sidecar.unlink()
            src = sqlite3.connect(str(tmp))
            try:
                dest = sqlite3.connect(str(target))
                try:
                    src.backup(dest)
                finally:
                    dest.close()
            finally:
                src.close()
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
            for suffix in ("-wal", "-shm"):
                sidecar = tmp.with_name(tmp.name + suffix)
                if sidecar.exists():
                    with contextlib.suppress(OSError):
                        sidecar.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
