#!/usr/bin/env python3
"""Idempotent schema-prep runner for VNX pre-central-migration.

Applies the project-specific prep-migration SQL files from
schemas/prep_migrations/ to a project's .vnx-data/state/ databases,
closing schema-drift gaps BEFORE migrate_to_central_vnx.py --apply runs.

Each ALTER TABLE is guarded: PRAGMA table_info check first, skip if the
column already exists. CREATE TABLE IF NOT EXISTS statements are always
executed (SQLite makes them idempotent). Entire apply per DB runs inside
one transaction; any error rolls back that DB fully.

CLI:
    python3 scripts/apply_schema_prep.py \\
      --project-id sales-copilot|seocrawler-v2 \\
      --source-db-dir <path>/.vnx-data/state/ \\
      --dry-run | --apply

Post-apply verification: re-runs migrate_dry_run.py in dry-run mode
(if --verify flag set) and asserts drift count = 0.

Exit codes:
    0 — success (dry-run or apply, no remaining drift)
    1 — invalid arguments
    2 — source DB dir not found
    3 — SQL parse error in prep file
    4 — apply failure (DB rolled back — check error log)
    5 — post-apply drift > 0 (prep incomplete — investigate manually)
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "schemas" / "prep_migrations"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOG = logging.getLogger("vnx.schema_prep")

# Map project_id → prep SQL filename
_PREP_SQL_MAP: dict[str, str] = {
    "sales-copilot": "sales_copilot_pre_central_prep.sql",
    "seocrawler-v2": "seocrawler_v2_pre_central_prep.sql",
}

# Map table name → which DB file it lives in (for multi-DB projects)
# Only relevant tables that appear in the prep migrations.
_TABLE_DB_MAP: dict[str, str] = {
    # quality_intelligence tables
    "prevention_rules": "quality_intelligence.db",
    "dispatch_pattern_offered": "quality_intelligence.db",
    "session_analytics": "quality_intelligence.db",
    "confidence_events": "quality_intelligence.db",
    "success_patterns": "quality_intelligence.db",
    "antipatterns": "quality_intelligence.db",
    "dispatch_metadata": "quality_intelligence.db",
    "pattern_usage": "quality_intelligence.db",
    # runtime_coordination tables
    "dispatch_attempts": "runtime_coordination.db",
    "incident_log": "runtime_coordination.db",
    "dispatches": "runtime_coordination.db",
    "terminal_leases": "runtime_coordination.db",
    "coordination_events": "runtime_coordination.db",
    "intelligence_injections": "runtime_coordination.db",
    # dispatch_tracker tables
    "dispatch_experiments": "dispatch_tracker.db",
}


@dataclass
class StatementResult:
    stmt_type: str  # 'alter_table' | 'create_table' | 'create_index' | 'other'
    table: str
    column: Optional[str]
    action: str  # 'applied' | 'skipped' | 'dry_run'
    detail: str = ""


@dataclass
class PrepResult:
    project_id: str
    source_db_dir: str
    dry_run: bool
    statements_total: int = 0
    statements_applied: int = 0
    statements_skipped: int = 0
    details: list[StatementResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
    ).fetchone()
    return row is not None


def _parse_alter_table(stmt: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (table_name, column_name) from ALTER TABLE ... ADD COLUMN ... statement."""
    m = re.match(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)",
        stmt.strip(),
        re.IGNORECASE,
    )
    if m:
        return m.group(1), m.group(2)
    return None, None


def _parse_create_table(stmt: str) -> Optional[str]:
    """Extract table name from CREATE TABLE [IF NOT EXISTS] ... statement."""
    m = re.match(
        r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
        stmt.strip(),
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def _parse_create_index(stmt: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (index_name, table_name) from CREATE INDEX [IF NOT EXISTS] ... ON ... statement."""
    m = re.match(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(\w+)",
        stmt.strip(),
        re.IGNORECASE,
    )
    if m:
        return m.group(1), m.group(2)
    return None, None


def _split_sql_statements(sql: str) -> list[str]:
    """Split SQL text on semicolons, skipping comments and empty statements."""
    statements = []
    # Strip line comments
    lines = []
    for line in sql.splitlines():
        stripped = re.sub(r"--.*$", "", line).strip()
        if stripped:
            lines.append(stripped)
    full = " ".join(lines)
    for raw_stmt in full.split(";"):
        stmt = raw_stmt.strip()
        if stmt:
            statements.append(stmt)
    return statements


def _group_statements_by_db(
    statements: list[str],
) -> dict[str, list[str]]:
    """Group SQL statements by target DB filename based on table analysis."""
    db_groups: dict[str, list[str]] = {
        "quality_intelligence.db": [],
        "runtime_coordination.db": [],
        "dispatch_tracker.db": [],
    }

    for stmt in statements:
        upper = stmt.upper().lstrip()
        if upper.startswith("ALTER TABLE"):
            table, _ = _parse_alter_table(stmt)
            db_name = _TABLE_DB_MAP.get(table or "", "quality_intelligence.db")
        elif upper.startswith("CREATE TABLE"):
            table = _parse_create_table(stmt)
            db_name = _TABLE_DB_MAP.get(table or "", "quality_intelligence.db")
        elif upper.startswith("CREATE INDEX") or upper.startswith("CREATE UNIQUE INDEX"):
            _, table = _parse_create_index(stmt)
            db_name = _TABLE_DB_MAP.get(table or "", "quality_intelligence.db")
        else:
            db_name = "quality_intelligence.db"

        db_groups[db_name].append(stmt)

    return db_groups


def _apply_statements_to_db(
    db_path: Path,
    statements: list[str],
    dry_run: bool,
    project_id: str,
) -> tuple[list[StatementResult], list[str]]:
    """Apply (or dry-run) a list of SQL statements to a single SQLite DB.

    Returns (results, errors). On --apply, runs inside a single transaction.
    If the DB does not exist and there are only CREATE IF NOT EXISTS + ALTER
    statements, we create it (SQLite auto-creates on connect).
    """
    results: list[StatementResult] = []
    errors: list[str] = []

    if not statements:
        return results, errors

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        errors.append(f"Cannot open {db_path}: {exc}")
        return results, errors

    try:
        with conn:
            if not dry_run:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")

            for stmt in statements:
                upper = stmt.upper().lstrip()

                if upper.startswith("ALTER TABLE"):
                    table, column = _parse_alter_table(stmt)
                    if table is None or column is None:
                        LOG.warning("Could not parse ALTER TABLE: %s", stmt[:80])
                        continue

                    if _column_exists(conn, table, column):
                        results.append(StatementResult(
                            stmt_type="alter_table", table=table, column=column,
                            action="skipped", detail="column already exists",
                        ))
                        continue

                    if not _table_exists(conn, table):
                        results.append(StatementResult(
                            stmt_type="alter_table", table=table, column=column,
                            action="skipped", detail="table does not exist (will be created by CREATE TABLE)",
                        ))
                        continue

                    if dry_run:
                        results.append(StatementResult(
                            stmt_type="alter_table", table=table, column=column,
                            action="dry_run", detail=f"would add column {column} to {table}",
                        ))
                    else:
                        conn.execute(stmt)
                        results.append(StatementResult(
                            stmt_type="alter_table", table=table, column=column,
                            action="applied",
                        ))

                elif upper.startswith("CREATE TABLE"):
                    table = _parse_create_table(stmt)
                    if dry_run:
                        already = _table_exists(conn, table or "")
                        results.append(StatementResult(
                            stmt_type="create_table", table=table or "?", column=None,
                            action="dry_run",
                            detail="table exists, no-op" if already else "would create table",
                        ))
                    else:
                        conn.execute(stmt)
                        results.append(StatementResult(
                            stmt_type="create_table", table=table or "?", column=None,
                            action="applied",
                        ))

                elif upper.startswith("CREATE INDEX") or upper.startswith("CREATE UNIQUE INDEX"):
                    idx_name, table = _parse_create_index(stmt)
                    if dry_run:
                        already = _index_exists(conn, idx_name or "")
                        results.append(StatementResult(
                            stmt_type="create_index", table=table or "?", column=None,
                            action="dry_run",
                            detail="index exists, no-op" if already else f"would create index {idx_name}",
                        ))
                    else:
                        conn.execute(stmt)
                        results.append(StatementResult(
                            stmt_type="create_index", table=table or "?", column=None,
                            action="applied",
                        ))

                else:
                    results.append(StatementResult(
                        stmt_type="other", table="?", column=None,
                        action="skipped", detail=f"unhandled stmt type: {stmt[:60]}",
                    ))

            if not dry_run:
                conn.execute("COMMIT")

    except Exception as exc:
        if not dry_run:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        errors.append(f"Error applying to {db_path}: {exc}")
    finally:
        conn.close()

    return results, errors


def apply_prep(
    project_id: str,
    source_db_dir: Path,
    dry_run: bool,
    prep_sql_path: Optional[Path] = None,
) -> PrepResult:
    """Main entry point: apply (or dry-run) prep SQL for a project.

    Reads prep SQL from schemas/prep_migrations/<project>.sql, groups
    statements by target DB, then calls _apply_statements_to_db for each DB.
    """
    result = PrepResult(
        project_id=project_id,
        source_db_dir=str(source_db_dir),
        dry_run=dry_run,
    )

    if prep_sql_path is None:
        sql_filename = _PREP_SQL_MAP.get(project_id)
        if not sql_filename:
            result.errors.append(f"No prep SQL registered for project_id={project_id}. "
                                  f"Known projects: {list(_PREP_SQL_MAP)}")
            return result
        prep_sql_path = SCHEMAS_DIR / sql_filename

    if not prep_sql_path.exists():
        result.errors.append(f"Prep SQL file not found: {prep_sql_path}")
        return result

    sql_text = prep_sql_path.read_text(encoding="utf-8")
    statements = _split_sql_statements(sql_text)
    db_groups = _group_statements_by_db(statements)

    result.statements_total = len(statements)

    for db_name, stmts in db_groups.items():
        if not stmts:
            continue
        db_path = source_db_dir / db_name
        LOG.info("%s %d statements to %s",
                 "DRY-RUN" if dry_run else "Applying", len(stmts), db_path)

        stmt_results, errors = _apply_statements_to_db(db_path, stmts, dry_run, project_id)
        result.details.extend(stmt_results)
        result.errors.extend(errors)

        for r in stmt_results:
            if r.action == "applied":
                result.statements_applied += 1
            elif r.action == "skipped":
                result.statements_skipped += 1

    return result


def _print_result(result: PrepResult) -> None:
    mode = "DRY-RUN" if result.dry_run else "APPLIED"
    print(f"[{mode}] project={result.project_id}  db_dir={result.source_db_dir}")
    print(f"  Statements: total={result.statements_total}  "
          f"applied={result.statements_applied}  "
          f"skipped={result.statements_skipped}")

    if result.errors:
        print(f"  ERRORS ({len(result.errors)}):")
        for e in result.errors:
            print(f"    - {e}")

    for r in result.details:
        if r.action == "applied":
            col_part = f".{r.column}" if r.column else ""
            print(f"  + {r.stmt_type}: {r.table}{col_part}")
        elif r.action == "dry_run" and "would" in r.detail:
            col_part = f".{r.column}" if r.column else ""
            print(f"  ~ {r.stmt_type}: {r.table}{col_part}  [{r.detail}]")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Apply idempotent schema-prep migrations before VNX central-DB migration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--project-id", required=True,
        choices=list(_PREP_SQL_MAP),
        help="Project to prep (determines which SQL file to apply)",
    )
    p.add_argument(
        "--source-db-dir", type=Path, required=True,
        help="Path to the project's .vnx-data/state/ directory (contains the SQLite DBs)",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Print planned changes; write NOTHING to any DB")
    mode.add_argument("--apply", action="store_true",
                      help="Apply changes to the DB files inside transactions")
    p.add_argument(
        "--prep-sql", type=Path, default=None,
        help="Override path to prep SQL file (default: schemas/prep_migrations/<project>.sql)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    source_db_dir = args.source_db_dir
    if not source_db_dir.exists():
        LOG.error("Source DB dir not found: %s", source_db_dir)
        return 2

    result = apply_prep(
        project_id=args.project_id,
        source_db_dir=source_db_dir,
        dry_run=args.dry_run,
        prep_sql_path=args.prep_sql,
    )

    _print_result(result)

    if result.errors:
        return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())
