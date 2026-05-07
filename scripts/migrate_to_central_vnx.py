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
import shutil
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

MIGRATION_0015_PATH = REPO_ROOT / "schemas" / "migrations" / "0015_complete_project_id.sql"
MIGRATION_0016_PATH = REPO_ROOT / "schemas" / "migrations" / "0016_rebuild_fts5.sql"

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
COLLISION_PREFIX_COLUMNS: tuple[str, ...] = ("dispatch_id", "pattern_id")


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
# Migration application (schemas/migrations/0015 + 0016)
# ---------------------------------------------------------------------------


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
    """
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS p4_import_skipped (
            project_id TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_rowid INTEGER NOT NULL,
            reason TEXT NOT NULL,
            skipped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (project_id, source_table, source_rowid)
        )
        """
    )


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


def _collect_collision_columns(
    con: sqlite3.Connection,
    source_alias: str,
    table: str,
) -> tuple[str, ...]:
    if not _table_exists(con, table):
        return ()
    central_cols = set(_table_columns(con, table))
    source_cols = set(_table_columns(con, table, alias=source_alias))
    return tuple(
        column
        for column in COLLISION_PREFIX_COLUMNS
        if column in central_cols and column in source_cols
    )


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


def _import_table(
    con: sqlite3.Connection,
    source_alias: str,
    project: ProjectEntry,
    table: str,
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
            value = row_data.get(column)
            if value is None or value == "":
                continue
            prefix = f"{project.project_id}:"
            text_value = str(value)
            row_data[column] = text_value if text_value.startswith(prefix) else f"{prefix}{text_value}"
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
                con.execute(
                    "INSERT OR IGNORE INTO p4_import_skipped "
                    "(project_id, source_table, source_rowid, reason) VALUES (?, ?, ?, ?)",
                    (project.project_id, table, rid, "missing_code_snippet_rowid_map"),
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
                con.execute(
                    "INSERT OR IGNORE INTO p4_import_skipped "
                    "(project_id, source_table, source_rowid, reason) VALUES (?, ?, ?, ?)",
                    (project.project_id, table, rid, "insert_or_ignore_conflict"),
                )
                skipped += 1
        except sqlite3.IntegrityError as exc:
            LOG.warning(
                "INSERT skipped project=%s table=%s rowid=%s err=%s",
                project.project_id, table, rid, exc,
            )
            con.execute(
                "INSERT OR IGNORE INTO p4_import_skipped "
                "(project_id, source_table, source_rowid, reason) VALUES (?, ?, ?, ?)",
                (project.project_id, table, rid, f"integrity_error:{exc}"),
            )
            skipped += 1
    return ImportSummary(project.project_id, "", table, inserted, skipped)


def import_project(
    central_qi: Path,
    central_rc: Path,
    project: ProjectEntry,
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
                    summary = _import_table(con, "src", project, tbl)
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
                    summary = _import_table(con, "src", project, tbl)
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
) -> dict:
    """Recompute per-project row counts + simple column checksums; raises on drift."""
    report: dict = {
        "per_project": {},
        "checksums": {},
        "skipped_rows": [],
        "discrepancies": [],
    }
    for project in projects:
        check_abort()
        qi_src = project.state_dir / "quality_intelligence.db"
        rc_src = project.state_dir / "runtime_coordination.db"
        per_table: dict[str, dict] = {}
        if qi_src.is_file() and central_qi.exists():
            per_table.update(_compare_counts(central_qi, "quality_intelligence.db", qi_src, project, IMPORT_TABLES_QI))
        if rc_src.is_file() and central_rc.exists():
            per_table.update(_compare_counts(central_rc, "runtime_coordination.db", rc_src, project, IMPORT_TABLES_RC))
        report["per_project"][project.project_id] = per_table
    report["skipped_rows"].extend(_collect_skipped_rows(central_qi, "quality_intelligence.db"))
    report["skipped_rows"].extend(_collect_skipped_rows(central_rc, "runtime_coordination.db"))
    report["discrepancies"] = _verification_discrepancies(report)
    return report


def _compare_counts(
    central_db: Path,
    central_db_label: str,
    src_db: Path,
    project: ProjectEntry,
    tables: Iterable[str],
) -> dict[str, dict]:
    out: dict[str, dict] = {}
    con = sqlite3.connect(str(central_db))
    try:
        attach_readonly(con, "src", src_db)
        for tbl in tables:
            if not _table_exists(con, tbl):
                continue
            try:
                src_cnt = con.execute(f"SELECT COUNT(*) FROM src.{tbl}").fetchone()[0]
            except sqlite3.OperationalError:
                continue
            try:
                cnt_query = (
                    f"SELECT COUNT(*) FROM {tbl} WHERE project_id = ?"
                    if _column_exists(con, tbl, "project_id")
                    else f"SELECT COUNT(*) FROM {tbl}"
                )
                params = (project.project_id,) if "WHERE" in cnt_query else ()
                central_cnt = con.execute(cnt_query, params).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            out[f"{central_db_label}.{tbl}"] = {
                "source_rows": int(src_cnt),
                "central_rows_for_project": int(central_cnt),
            }
        con.execute("DETACH DATABASE src")
    finally:
        con.close()
    return out


def _collect_skipped_rows(central_db: Path, central_db_label: str) -> list[dict[str, object]]:
    if not central_db.exists():
        return []
    con = sqlite3.connect(str(central_db))
    try:
        if not _table_exists(con, "p4_import_skipped"):
            return []
        rows = con.execute(
            """
            SELECT project_id, source_table, source_rowid, reason
            FROM p4_import_skipped
            ORDER BY project_id, source_table, source_rowid
            """
        ).fetchall()
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
    if not central_qi.exists():
        LOG.warning("central QI db missing; creating empty: %s", central_qi)
        sqlite3.connect(str(central_qi)).close()
    if not central_rc.exists():
        LOG.warning("central RC db missing; creating empty: %s", central_rc)
        sqlite3.connect(str(central_rc)).close()

    pre_snapshot = _snapshot_central(central_qi, central_rc)
    try:
        # Order matters (Finding 3 in PR #432 review):
        #   1. 0015 ALTER TABLE — adds project_id column to non-FTS tables
        #   2. import_project loop — populates code_snippets + snippet_metadata
        #   3. 0016 FTS5 rebuild — joins snippet_metadata to assign project_id
        # Running 0016 before the import loop rebuilds FTS5 over an empty
        # central table → useless index.
        apply_migration_0015(central_qi, central_rc)
    except sqlite3.Error as exc:
        LOG.error("schema migration failed: %s", exc)
        _restore_snapshot(pre_snapshot, central_qi, central_rc)
        return 3

    summaries: list[ImportSummary] = []
    failed_projects: list[str] = []
    for project in projects:
        try:
            check_abort()
            summaries.extend(import_project(central_qi, central_rc, project))
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
        verify_report = verify_import(central_qi, central_rc, projects)
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
    snapshots: dict[str, Path] = {}
    for label, db in (("qi", qi), ("rc", rc)):
        if db.exists():
            tmp = db.with_suffix(db.suffix + f".presnap.{os.getpid()}")
            shutil.copy2(db, tmp)
            snapshots[label] = tmp
    return snapshots


def _restore_snapshot(snapshots: dict[str, Path], qi: Path, rc: Path) -> None:
    for label, tmp in snapshots.items():
        target = qi if label == "qi" else rc
        try:
            shutil.copy2(tmp, target)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
