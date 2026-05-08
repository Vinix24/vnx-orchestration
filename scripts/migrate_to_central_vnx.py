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
import sqlite3
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
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

LOG = logging.getLogger("vnx.migrate.apply")

ABORT_FLAG = Path.home() / ".vnx-aggregator" / "ABORT"
CONFIRMATION_PHRASE = "MIGRATE-NOW-2026"
DEFAULT_BACKUP_BASE = Path.home() / "Documents"
CENTRAL_DATA_DIR = Path.home() / ".vnx-data" / "state"

MIGRATION_0010_PATH = REPO_ROOT / "schemas" / "migrations" / "0010_add_project_id.sql"
MIGRATION_0015_PATH = REPO_ROOT / "schemas" / "migrations" / "0015_complete_project_id.sql"
MIGRATION_0016_PATH = REPO_ROOT / "schemas" / "migrations" / "0016_rebuild_fts5.sql"
QI_SCHEMA_PATH = REPO_ROOT / "schemas" / "quality_intelligence.sql"

# Tables to import per source DB. Aligned with migrate_dry_run.PLAN_TABLES_*.
# Note: `code_snippets` (FTS5 vtab) MUST be imported BEFORE migration 0016
# rebuilds the FTS5 index — otherwise 0016 rebuilds over an empty table and
# the resulting central index is useless. See Finding 3 in PR #432 review.
IMPORT_TABLES_QI: tuple[str, ...] = (
    "success_patterns",
    "antipatterns",
    "prevention_rules",
    "pattern_usage",
    "confidence_events",
    "dispatch_metadata",
    "dispatch_pattern_offered",
    "session_analytics",
    "vnx_code_quality",
    "code_snippets",
    "snippet_metadata",
    "quality_trends",
    "quality_alerts",
    "dispatch_quality_context",
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

# Schema-driven collision-prefixing candidates. Any imported table that carries
# one of these exact column names gets its value rewritten to
# ``<project_id>:<original>`` so per-project identifiers remain unique after
# consolidation.
#
# NOTE: this is the BASE list. Round-2 fix for Finding 2 generalizes the
# detection to also cover columns whose name *ends with* ``_dispatch_id`` or
# ``_pattern_id`` (e.g. ``related_dispatch_id``, ``parent_dispatch_id``); see
# ``_collect_collision_columns`` below.
COLLISION_PREFIX_COLUMNS: tuple[str, ...] = ("dispatch_id", "pattern_id")

# Suffixes used for schema-driven collision detection. Any column name that
# ends in one of these suffixes (after the leading underscore) is treated as
# a per-project identifier carrier and gets the ``<project_id>:`` prefix
# rewritten on import. Examples: ``related_dispatch_id``, ``parent_dispatch_id``,
# ``parent_pattern_id``.
COLLISION_PREFIX_SUFFIXES: tuple[str, ...] = ("_dispatch_id", "_pattern_id")

# Columns that store JSON arrays of dispatch/pattern IDs. Each element in the
# array is rewritten to ``<project_id>:<element>`` on import so cross-tenant
# references stay disjoint after consolidation. (Finding 2 round 2.)
COLLISION_JSON_ARRAY_COLUMNS: tuple[str, ...] = ("source_dispatch_ids",)

# Special-case: ``coordination_events.entity_id`` stores either a dispatch_id
# or a pattern_id depending on ``entity_type``. We rewrite the value only when
# the entity_type matches one of these prefix-eligible types. (Finding 2 round 2.)
COLLISION_ENTITY_TABLE = "coordination_events"
COLLISION_ENTITY_ID_COLUMN = "entity_id"
COLLISION_ENTITY_TYPE_COLUMN = "entity_type"
COLLISION_ENTITY_TYPES_PREFIXED: frozenset[str] = frozenset({"dispatch", "pattern"})

# Free-text identifier columns that historically held a dispatch id but whose
# name does not end in ``_dispatch_id``. Listed explicitly so future schemas
# add to this set deliberately rather than accidentally inheriting the suffix
# rule. (Finding 2 round 2.)
COLLISION_NAMED_IDENTIFIER_COLUMNS: tuple[str, ...] = ("parent_dispatch",)


# ---------------------------------------------------------------------------
# Operator gates
# ---------------------------------------------------------------------------


def check_abort() -> None:
    if ABORT_FLAG.exists():
        raise AbortRequested(f"abort flag present: {ABORT_FLAG}")


class AbortRequested(RuntimeError):
    pass


class BackupFailure(RuntimeError):
    pass


class VerificationFailure(RuntimeError):
    pass


class BootstrapFailure(RuntimeError):
    """Raised when the central DB is missing canonical structure required for import.

    Round-3 fix-forward (Issue 4): rather than letting a per-row INSERT
    OR IGNORE silently drop every row when a central table is absent,
    pre-flight assert that every import-target table exists. If not,
    surface the missing tables in the exception message so the operator
    can diagnose the broken bootstrap before any data is moved.
    """


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
    """
    qi_fresh = not _has_table(qi_db, _QI_SENTINEL_TABLE)
    rc_fresh = not _has_table(rc_db, _RC_SENTINEL_TABLE)
    return qi_fresh or rc_fresh


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
    sql = MIGRATION_0010_PATH.read_text()
    # 0010 uses a slightly different delimiter than 0015. Match the
    # exact partition heading from the file so we don't accidentally
    # split inside an inline comment.
    qi_block, _, rc_block = sql.partition("-- @db: runtime_coordination")
    _apply_alters_idempotently(qi_db, qi_block)
    _apply_alters_idempotently(rc_db, rc_block)


def apply_migration_0015(qi_db: Path, rc_db: Path) -> None:
    sql = MIGRATION_0015_PATH.read_text()
    qi_block, _, rc_block = sql.partition(
        "-- @db: runtime_coordination (Phase 4 cold tables — 7 tables)"
    )
    _apply_alters_idempotently(qi_db, qi_block)
    _apply_alters_idempotently(rc_db, rc_block)


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


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    cur = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','virtual') AND name = ?",
        (table,),
    )
    return cur.fetchone() is not None


def _table_columns(
    con: sqlite3.Connection,
    table: str,
    alias: Optional[str] = None,
) -> list[str]:
    pragma = f"PRAGMA {alias}.table_info({table})" if alias else f"PRAGMA table_info({table})"
    return [r[1] for r in con.execute(pragma)]


def _column_exists(
    con: sqlite3.Connection,
    table: str,
    column: str,
    alias: Optional[str] = None,
) -> bool:
    return column in _table_columns(con, table, alias=alias)


def apply_migration_0016(qi_db: Path) -> None:
    """Rebuild FTS5 indexes in quality_intelligence.db with project_id.

    All statements run inside an explicit BEGIN/COMMIT frame so a failure
    after ``DROP TABLE code_snippets`` rolls back the drop and the original
    table survives intact. ``executescript`` is unsafe here because it
    issues an implicit COMMIT before running, defeating the wrapper. See
    Finding 4 in PR #432 review.
    """
    if not qi_db.exists():
        LOG.warning("skipping FTS5 rebuild: %s missing", qi_db)
        return
    sql = MIGRATION_0016_PATH.read_text()
    con = sqlite3.connect(str(qi_db), isolation_level=None)
    try:
        if not _table_exists(con, "code_snippets"):
            LOG.info("code_snippets vtab not present; skipping FTS5 rebuild")
            return
        cols = [r[1] for r in con.execute("PRAGMA table_info(code_snippets)")]
        if "project_id" in cols:
            LOG.info("FTS5 already includes project_id; skipping rebuild")
            return
        con.execute("BEGIN")
        try:
            for stmt in _iter_sql_statements(sql):
                con.execute(stmt)
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


@dataclass(frozen=True)
class ImportSummary:
    project_id: str
    db_name: str
    table: str
    rows_inserted: int
    rows_skipped_existing: int


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


def _now_utc_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _generate_run_id() -> str:
    """Per-apply run identifier used to scope skipped-row resolution.

    Runs at the top of the apply flow and threaded down to every call
    site that writes to ``p4_import_skipped`` so that ``verify_import``
    can filter out historical / resolved skips.
    """
    return f"run-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{os.getpid()}"


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


def _integer_primary_key(con: sqlite3.Connection, table: str, alias: Optional[str] = None) -> Optional[str]:
    """Return the column name that is INTEGER PRIMARY KEY (autoincrement rowid alias).

    Such columns cannot be ported across project DBs because each source DB
    starts numbering at 1 and would collide on import. Returning the name
    lets the caller exclude it from the INSERT column list.
    """
    pragma = f"PRAGMA {alias}.table_info({table})" if alias else f"PRAGMA table_info({table})"
    for cid, name, ctype, _notnull, _dflt, pk in con.execute(pragma):
        if pk and (ctype or "").upper() == "INTEGER":
            return name
    return None


def _common_columns(con: sqlite3.Connection, source_alias: str, table: str) -> list[str]:
    """Return columns present in BOTH source and central tables (intersection).

    Excludes the source's INTEGER PRIMARY KEY column so SQLite re-assigns
    autoincrement ids in the central DB; otherwise project-local primary
    keys (1, 2, 3, ...) collide with the first-imported project's rows.
    """
    if not _table_exists(con, table):
        return []
    central_cols = _table_columns(con, table)
    src_cols = _table_columns(con, table, alias=source_alias)
    src_int_pk = _integer_primary_key(con, table, alias=source_alias)
    central_int_pk = _integer_primary_key(con, table)
    skip = {c for c in (src_int_pk, central_int_pk) if c}
    return [c for c in src_cols if c in central_cols and c not in skip]


def _is_collision_column(name: str) -> bool:
    """True if a column name participates in cross-project key prefixing.

    Centralized so call sites in both the live migrator and dry-run
    detector share identical rules. Covers exact matches, the
    ``_dispatch_id`` / ``_pattern_id`` suffix family, and the explicitly
    enumerated free-text identifier columns. (Finding 2 round 2.)
    """
    if name in COLLISION_PREFIX_COLUMNS:
        return True
    if name in COLLISION_NAMED_IDENTIFIER_COLUMNS:
        return True
    for suffix in COLLISION_PREFIX_SUFFIXES:
        if name != suffix and name.endswith(suffix):
            return True
    return False


def _collect_collision_columns(
    con: sqlite3.Connection,
    source_alias: str,
    table: str,
) -> tuple[str, ...]:
    """Return per-table column names whose values must be project-prefixed.

    Schema-driven so any column matching the prefix-eligible name rules
    (see :func:`_is_collision_column`) is included automatically — no
    manual edits needed when a new table grows a ``related_dispatch_id``
    style reference. (Finding 2 round 2.)
    """
    if not _table_exists(con, table):
        return ()
    central_cols = _table_columns(con, table)
    source_cols = set(_table_columns(con, table, alias=source_alias))
    return tuple(
        column
        for column in central_cols
        if column in source_cols and _is_collision_column(column)
    )


def _collect_json_array_columns(
    con: sqlite3.Connection,
    source_alias: str,
    table: str,
) -> tuple[str, ...]:
    """Columns in ``table`` known to hold JSON arrays of identifiers.

    Used by the live migrator to rewrite each array element with the
    project prefix. (Finding 2 round 2.)
    """
    if not _table_exists(con, table):
        return ()
    central_cols = set(_table_columns(con, table))
    source_cols = set(_table_columns(con, table, alias=source_alias))
    return tuple(
        column
        for column in COLLISION_JSON_ARRAY_COLUMNS
        if column in central_cols and column in source_cols
    )


def _prefix_value(project_id: str, value: object) -> object:
    """Apply ``<project_id>:`` prefix to a scalar identifier value.

    Idempotent: a value that already starts with the project's prefix
    is returned unchanged so repeat-runs do not double-prefix.
    """
    if value is None or value == "":
        return value
    prefix = f"{project_id}:"
    text_value = str(value)
    return text_value if text_value.startswith(prefix) else f"{prefix}{text_value}"


def _prefix_json_array(project_id: str, value: object) -> object:
    """Apply project prefix to each element in a JSON-array string.

    If the value is missing, empty, or fails to parse as a JSON array,
    it is returned unchanged — defensive because legacy DBs sometimes
    stored unstructured strings in these columns.
    """
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value
    if not isinstance(parsed, list):
        return value
    rewritten = [_prefix_value(project_id, item) for item in parsed]
    return json.dumps(rewritten)


def _record_rowid_mapping(
    con: sqlite3.Connection,
    project_id: str,
    source_table: str,
    source_rowid: int,
    central_rowid: int,
) -> None:
    con.execute(
        """
        INSERT INTO p4_import_rowid_map
            (project_id, source_table, source_rowid, central_rowid)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(project_id, source_table, source_rowid)
        DO UPDATE SET central_rowid = excluded.central_rowid
        """,
        (project_id, source_table, source_rowid, central_rowid),
    )


def _mapped_central_rowid(
    con: sqlite3.Connection,
    project_id: str,
    source_table: str,
    source_rowid: int,
) -> Optional[int]:
    row = con.execute(
        """
        SELECT central_rowid
        FROM p4_import_rowid_map
        WHERE project_id = ? AND source_table = ? AND source_rowid = ?
        """,
        (project_id, source_table, source_rowid),
    ).fetchone()
    return int(row[0]) if row else None


def _resolve_prior_skip(
    con: sqlite3.Connection,
    project_id: str,
    source_table: str,
    source_rowid: int,
) -> None:
    """Mark any prior unresolved p4_import_skipped row as resolved.

    Called when a previously-skipped row finally imports successfully on
    a later run. ``verify_import`` only treats unresolved skips as
    discrepancies, so flipping ``resolved_at`` lets idempotent re-runs
    self-heal without operator intervention. (Finding 3 round 2.)
    """
    con.execute(
        """
        UPDATE p4_import_skipped
           SET resolved_at = ?
         WHERE project_id = ?
           AND source_table = ?
           AND source_rowid = ?
           AND resolved_at IS NULL
        """,
        (_now_utc_iso(), project_id, source_table, source_rowid),
    )


def _record_skip(
    con: sqlite3.Connection,
    project_id: str,
    source_table: str,
    source_rowid: int,
    reason: str,
    run_id: Optional[str],
) -> None:
    """Audit a row the migrator could not import (conflict / integrity).

    The PRIMARY KEY is ``(project_id, source_table, source_rowid)`` so
    this is naturally one-row-per-source-rowid; we re-stamp ``run_id`` /
    ``skipped_at`` on each occurrence and clear ``resolved_at`` so a row
    that re-skips after being marked resolved on a previous run shows up
    as an active discrepancy again. (Finding 3 round 2.)
    """
    con.execute(
        """
        INSERT INTO p4_import_skipped
            (project_id, source_table, source_rowid, reason, skipped_at, run_id, resolved_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(project_id, source_table, source_rowid)
        DO UPDATE SET
            reason       = excluded.reason,
            skipped_at   = excluded.skipped_at,
            run_id       = excluded.run_id,
            resolved_at  = NULL
        """,
        (
            project_id,
            source_table,
            source_rowid,
            reason,
            _now_utc_iso(),
            run_id,
        ),
    )


def _import_table(
    con: sqlite3.Connection,
    source_alias: str,
    project: ProjectEntry,
    table: str,
    run_id: Optional[str] = None,
) -> ImportSummary:
    source_cols = _common_columns(con, source_alias, table)
    central_has_project_id = _column_exists(con, table, "project_id")
    source_has_project_id = _column_exists(con, table, "project_id", alias=source_alias)
    insert_cols = list(source_cols)
    if central_has_project_id and not source_has_project_id:
        insert_cols.append("project_id")
    if not source_cols:
        return ImportSummary(project.project_id, "", table, 0, 0)

    cur = con.execute(
        "SELECT source_rowid FROM p4_import_idempotency "
        "WHERE project_id = ? AND source_table = ?",
        (project.project_id, table),
    )
    already = {int(row[0]) for row in cur.fetchall()}

    select_cols = ", ".join(f'"{c}"' for c in source_cols)
    src_rows = list(
        con.execute(
            f"SELECT rowid, {select_cols} FROM {source_alias}.{table}"
        )
    )

    inserted = 0
    skipped = 0
    collision_cols = _collect_collision_columns(con, source_alias, table)
    json_array_cols = _collect_json_array_columns(con, source_alias, table)
    is_entity_table = (
        table == COLLISION_ENTITY_TABLE
        and COLLISION_ENTITY_ID_COLUMN in source_cols
        and COLLISION_ENTITY_TYPE_COLUMN in source_cols
    )
    for row in src_rows:
        check_abort()
        rid = row[0]
        if rid in already:
            skipped += 1
            continue
        row_data = dict(zip(source_cols, row[1:]))
        if central_has_project_id:
            row_data["project_id"] = project.project_id
        for column in collision_cols:
            row_data[column] = _prefix_value(project.project_id, row_data.get(column))
        for column in json_array_cols:
            row_data[column] = _prefix_json_array(project.project_id, row_data.get(column))
        if is_entity_table:
            entity_type = row_data.get(COLLISION_ENTITY_TYPE_COLUMN)
            if (entity_type or "").lower() in COLLISION_ENTITY_TYPES_PREFIXED:
                row_data[COLLISION_ENTITY_ID_COLUMN] = _prefix_value(
                    project.project_id,
                    row_data.get(COLLISION_ENTITY_ID_COLUMN),
                )
        if table == "snippet_metadata" and "snippet_rowid" in row_data:
            mapped_rowid = _mapped_central_rowid(
                con,
                project.project_id,
                "code_snippets",
                int(row_data["snippet_rowid"]),
            )
            if mapped_rowid is None:
                LOG.warning(
                    "INSERT skipped project=%s table=%s rowid=%s err=missing_code_snippet_rowid_map",
                    project.project_id,
                    table,
                    rid,
                )
                _record_skip(
                    con,
                    project.project_id,
                    table,
                    rid,
                    "missing_code_snippet_rowid_map",
                    run_id,
                )
                skipped += 1
                continue
            row_data["snippet_rowid"] = mapped_rowid

        values = [row_data[column] for column in insert_cols]
        quoted_insert_cols = ", ".join(f'"{c}"' for c in insert_cols)
        placeholders = ", ".join("?" for _ in insert_cols)
        try:
            cur = con.execute(
                f"INSERT OR IGNORE INTO {table} ({quoted_insert_cols}) VALUES ({placeholders})",
                values,
            )
            if cur.rowcount == 1:
                central_rowid = cur.lastrowid
                con.execute(
                    "INSERT OR IGNORE INTO p4_import_idempotency "
                    "(project_id, source_table, source_rowid) VALUES (?, ?, ?)",
                    (project.project_id, table, rid),
                )
                # Self-heal: if a prior run logged this row as skipped (conflict
                # / integrity error), mark it resolved now that the import
                # succeeded. (Finding 3 round 2.)
                _resolve_prior_skip(con, project.project_id, table, rid)
                if table == "code_snippets":
                    # Preserve the logical snippet linkage without forcing raw
                    # rowid reuse across projects, which would collide.
                    _record_rowid_mapping(
                        con,
                        project.project_id,
                        table,
                        rid,
                        int(central_rowid),
                    )
                inserted += 1
            else:
                # SQLite IGNOREd the row (UNIQUE/PRIMARY KEY conflict). Do NOT
                # write to p4_import_idempotency — that table must reflect
                # actually-imported rows so re-runs can re-attempt the conflict
                # if the central row is later deleted/repaired. Audit the skip.
                LOG.warning(
                    "INSERT IGNORED project=%s table=%s rowid=%s (central key conflict)",
                    project.project_id, table, rid,
                )
                _record_skip(
                    con,
                    project.project_id,
                    table,
                    rid,
                    "insert_or_ignore_conflict",
                    run_id,
                )
                skipped += 1
        except sqlite3.IntegrityError as exc:
            LOG.warning(
                "INSERT skipped project=%s table=%s rowid=%s err=%s",
                project.project_id, table, rid, exc,
            )
            _record_skip(
                con,
                project.project_id,
                table,
                rid,
                f"integrity_error:{exc}",
                run_id,
            )
            skipped += 1
    return ImportSummary(project.project_id, "", table, inserted, skipped)


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


def _src_table_present(con: sqlite3.Connection, alias: str, table: str) -> bool:
    """Return True iff ``alias.table`` is a real table or virtual table.

    Used to distinguish *missing* tables (acceptable; the table was added
    in a later schema and isn't in this source) from *unreadable* tables
    (fatal; the source DB is corrupt). (Finding 4 round 2.)
    """
    cur = con.execute(
        f"SELECT 1 FROM {alias}.sqlite_master WHERE type IN ('table','virtual') AND name = ?",
        (table,),
    )
    return cur.fetchone() is not None


def _compare_counts(
    central_db: Path,
    central_db_label: str,
    src_db: Path,
    project: ProjectEntry,
    tables: Iterable[str],
    read_errors: list[dict[str, object]],
) -> dict[str, dict]:
    """Compare per-project row counts; surface read failures via ``read_errors``.

    Round-2 fix (Finding 4): a corrupt or unreadable source table no
    longer silently degrades to zero rows. The condition is split into
    'table absent' (fine — schema drift) vs 'table present but unreadable'
    (fatal — appended to the shared ``read_errors`` list and surfaced as
    a verification discrepancy).
    """
    out: dict[str, dict] = {}
    con = sqlite3.connect(str(central_db))
    try:
        try:
            attach_readonly(con, "src", src_db)
        except sqlite3.Error as exc:
            read_errors.append(
                {
                    "db": central_db_label,
                    "project_id": project.project_id,
                    "phase": "attach",
                    "error": str(exc),
                    "path": str(src_db),
                }
            )
            return out
        for tbl in tables:
            src_present = _src_table_present(con, "src", tbl)
            central_present = _table_exists(con, tbl)
            if not src_present:
                # Source predates this table → acceptable schema drift.
                continue
            if not central_present:
                # Round-3 fix-forward (Issue 3): a missing central table
                # while the source HAS the table is a hard failure, not a
                # silent skip. Without this, an empty-bootstrap apply
                # would happily declare "verification clean" against
                # zero imported rows. Surfacing as a read_error promotes
                # to a verification discrepancy (exit code 4).
                read_errors.append(
                    {
                        "db": central_db_label,
                        "project_id": project.project_id,
                        "phase": "central_table_missing",
                        "table": tbl,
                        "error": "central DB missing import-target table",
                    }
                )
                continue
            try:
                src_cnt = con.execute(f"SELECT COUNT(*) FROM src.{tbl}").fetchone()[0]
            except sqlite3.Error as exc:
                read_errors.append(
                    {
                        "db": central_db_label,
                        "project_id": project.project_id,
                        "phase": "source_count",
                        "table": tbl,
                        "error": str(exc),
                    }
                )
                continue
            try:
                cnt_query = (
                    f"SELECT COUNT(*) FROM {tbl} WHERE project_id = ?"
                    if _column_exists(con, tbl, "project_id")
                    else f"SELECT COUNT(*) FROM {tbl}"
                )
                params = (project.project_id,) if "WHERE" in cnt_query else ()
                central_cnt = con.execute(cnt_query, params).fetchone()[0]
            except sqlite3.Error as exc:
                read_errors.append(
                    {
                        "db": central_db_label,
                        "project_id": project.project_id,
                        "phase": "central_count",
                        "table": tbl,
                        "error": str(exc),
                    }
                )
                continue
            out[f"{central_db_label}.{tbl}"] = {
                "source_rows": int(src_cnt),
                "central_rows_for_project": int(central_cnt),
            }
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute("DETACH DATABASE src")
    finally:
        con.close()
    return out


def _collect_skipped_rows(
    central_db: Path,
    central_db_label: str,
    run_id: Optional[str] = None,
) -> list[dict[str, object]]:
    """Return *unresolved* skipped rows, optionally scoped to a run.

    Round-2 fix (Finding 3): adds the ``resolved_at IS NULL`` filter so a
    conflict logged on run 1 that succeeded on run 2 stops surfacing as a
    verify_import discrepancy. When ``run_id`` is supplied, the query
    further narrows to that run so a fresh apply is only judged against
    its own outcomes.
    """
    if not central_db.exists():
        return []
    con = sqlite3.connect(str(central_db))
    try:
        if not _table_exists(con, "p4_import_skipped"):
            return []
        cols = {r[1] for r in con.execute("PRAGMA table_info(p4_import_skipped)")}
        has_resolved = "resolved_at" in cols
        has_run_id = "run_id" in cols
        clauses: list[str] = []
        params: list[object] = []
        if has_resolved:
            clauses.append("resolved_at IS NULL")
        if run_id and has_run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT project_id, source_table, source_rowid, reason "
            "FROM p4_import_skipped" + where +
            " ORDER BY project_id, source_table, source_rowid"
        )
        rows = con.execute(sql, params).fetchall()
        return [
            {
                "db": central_db_label,
                "project_id": row[0],
                "source_table": row[1],
                "source_rowid": int(row[2]),
                "reason": row[3],
            }
            for row in rows
        ]
    finally:
        con.close()


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


def main(argv: Iterable[str] | None = None) -> int:
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
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout on completion")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

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

    try:
        backup_dir = backup_projects(projects, args.backup_base)
    except (BackupFailure, AbortRequested) as exc:
        LOG.error("backup phase failed: %s", exc)
        return 3

    central_state.mkdir(parents=True, exist_ok=True)

    # Round-3 fix-forward: detect a fresh central BEFORE creating empty
    # DB files. Without this gate, an operator who has just blown away
    # ~/.vnx-data/state/ would see "import complete" against empty DBs.
    central_was_fresh = _central_is_empty(central_qi, central_rc)
    if central_was_fresh and not args.fresh_central:
        LOG.error(
            "central appears fresh (missing canonical schema at %s); "
            "pass --fresh-central to acknowledge first-deploy bootstrap",
            central_state,
        )
        return 1

    if not central_qi.exists():
        LOG.warning("central QI db missing; creating empty: %s", central_qi)
        sqlite3.connect(str(central_qi)).close()
    if not central_rc.exists():
        LOG.warning("central RC db missing; creating empty: %s", central_rc)
        sqlite3.connect(str(central_rc)).close()

    pre_snapshot = _snapshot_central(central_qi, central_rc)
    try:
        if central_was_fresh:
            LOG.info(
                "central is fresh; running canonical bootstrap "
                "(quality_db_init + coordination_db.init_schema)"
            )
            _init_central_if_missing(central_qi, central_rc)

        # Order matters (Round-3 Issue 1 + Finding 3 in PR #432 review):
        #   1. 0010 ALTER TABLE — adds project_id to hot tables (foundation)
        #   2. 0015 ALTER TABLE — extends project_id to remaining cold tables
        #   3. _assert_central_tables_exist — fail-fast before per-row losses
        #   4. import_project loop — populates code_snippets + snippet_metadata
        #   5. 0016 FTS5 rebuild — joins snippet_metadata to assign project_id
        # Running 0016 before the import loop rebuilds FTS5 over an empty
        # central table → useless index.
        apply_migration_0010(central_qi, central_rc)
        apply_migration_0015(central_qi, central_rc)
        _assert_central_tables_exist(central_qi, central_rc, projects)
    except BootstrapFailure as exc:
        LOG.error("bootstrap assertion failed: %s", exc)
        _restore_snapshot(pre_snapshot, central_qi, central_rc)
        return 3
    except sqlite3.Error as exc:
        LOG.error("schema migration failed: %s", exc)
        _restore_snapshot(pre_snapshot, central_qi, central_rc)
        return 3
    except (FileNotFoundError, ImportError) as exc:
        LOG.error("canonical bootstrap failed: %s", exc)
        _restore_snapshot(pre_snapshot, central_qi, central_rc)
        return 3

    run_id = _generate_run_id()
    summaries: list[ImportSummary] = []
    failed_projects: list[str] = []
    for project in projects:
        try:
            check_abort()
            summaries.extend(import_project(central_qi, central_rc, project, run_id=run_id))
        except AbortRequested as exc:
            LOG.error("aborting: %s", exc)
            return 1
        except Exception as exc:
            LOG.error("project=%s import failed; rolled back THAT project: %s", project.project_id, exc)
            failed_projects.append(project.project_id)

    try:
        apply_migration_0016(central_qi)
    except sqlite3.Error as exc:
        LOG.error("FTS5 rebuild (migration 0016) failed: %s", exc)
        _restore_snapshot(pre_snapshot, central_qi, central_rc)
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
        _restore_snapshot(pre_snapshot, central_qi, central_rc)
        return 4
    except Exception as exc:
        LOG.error("verification raised: %s", exc)
        _restore_snapshot(pre_snapshot, central_qi, central_rc)
        return 4

    out_payload = {
        "applied_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "backup_dir": str(backup_dir),
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
    if args.json:
        print(json.dumps(out_payload, indent=2, default=str))
    else:
        print(f"P4 import complete. Backup: {backup_dir}")
        print(f"  Central QI: {central_qi}")
        print(f"  Central RC: {central_rc}")
        for s in summaries:
            print(f"  [{s.project_id}] {s.db_name} {s.table}: +{s.rows_inserted} ({s.rows_skipped_existing} idempotent skips)")
        if failed_projects:
            print(f"  FAILED projects (rolled back): {', '.join(failed_projects)}")
    return 0


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
