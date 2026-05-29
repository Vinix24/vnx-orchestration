#!/usr/bin/env python3
"""
Rebuild-preservation CI harness.

Scans schemas/migrations/*.sql for migrations containing ALTER TABLE ... RENAME TO.
For each such migration at position N, diffs the schema state (columns, UNIQUE indexes,
foreign keys) of the affected table before and after the migration is applied.
Any undeclared drift causes a non-zero exit code.

Allowlist format (in the migration SQL header):
    -- preservation-allowlist: table.col_name, table.idx_name
    -- preservation-rationale: reason the item was intentionally dropped

Output: structured JSON to stdout.
Exit code: 0 = all clear, 1 = drift detected.

Institutional rationale: FUT-1 incident (2026-05-28). ALTER TABLE dispatches RENAME TO
silently dropped project_id (ADR-007 composite key). Six review rounds missed it.
This harness catches the pattern mechanically before merge.
Ref: claudedocs/FUT-1-ARCHITECT-REFLECTION-2026-05-28.md
"""

import re
import sqlite3
import json
import sys
import tempfile
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

RENAME_PATTERN = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+RENAME\s+TO", re.IGNORECASE)
ALLOWLIST_PATTERN = re.compile(r"--\s*preservation-allowlist:\s*(.+)", re.IGNORECASE)


def _find_project_root() -> Path:
    # Walk up from this file until we find schemas/migrations/
    p = Path(__file__).resolve().parent
    for _ in range(6):
        if (p / "schemas" / "migrations").is_dir():
            return p
        p = p.parent
    raise RuntimeError("Could not locate project root (schemas/migrations not found)")


def _migrations_dir() -> Path:
    return _find_project_root() / "schemas" / "migrations"


def _sorted_migrations(directory: Path):
    # Exclude _down.sql rollback scripts — applying them in sequence after their
    # corresponding up migrations undoes the schema state and breaks later migrations.
    # The linter checks forward-migration schema drift only.
    return sorted(
        (f for f in directory.glob("*.sql") if not f.name.endswith("_down.sql")),
        key=lambda f: f.name,
    )


def _build_fresh_db() -> sqlite3.Connection:
    """Create an in-memory DB bootstrapped with all base schemas.

    Applies runtime_coordination.sql (v1) through v9 and quality_intelligence.sql
    so that numbered migrations (0010+) have all prerequisite tables in place.
    v10 is intentionally excluded — it documents the post-0017 state and would
    conflict with migration replay.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    root = _find_project_root()
    schemas_dir = root / "schemas"
    base_files = [
        schemas_dir / "runtime_coordination.sql",
        schemas_dir / "runtime_coordination_v2.sql",
        schemas_dir / "runtime_coordination_v3.sql",
        schemas_dir / "runtime_coordination_v4.sql",
        schemas_dir / "runtime_coordination_v5.sql",
        schemas_dir / "runtime_coordination_v6.sql",
        schemas_dir / "runtime_coordination_v7.sql",
        schemas_dir / "runtime_coordination_v8.sql",
        schemas_dir / "runtime_coordination_v9.sql",
        schemas_dir / "quality_intelligence.sql",
    ]
    for schema_file in base_files:
        conn.executescript(schema_file.read_text())
    return conn


def _apply_migrations(migrations: list[Path]) -> sqlite3.Connection:
    conn = _build_fresh_db()
    for mf in migrations:
        sql = mf.read_text()
        try:
            conn.executescript(sql)
        except sqlite3.Error as exc:
            print(f"  [error] migration apply failed for {mf.name}: {exc}", file=sys.stderr)
            raise RuntimeError(f"Migration apply failed for {mf.name}: {exc}") from exc
    return conn


def _capture_table_schema(conn: sqlite3.Connection, table: str) -> dict:
    """Capture columns, unique indexes, and foreign keys for a table."""
    # columns: list of column names (lowercased)
    try:
        cols = [
            row[1].lower()
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]
    except sqlite3.Error:
        raise

    # unique indexes: set of frozensets of column names
    unique_indexes = set()
    try:
        for idx_row in conn.execute(f"PRAGMA index_list({table})").fetchall():
            idx_name = idx_row[1]
            is_unique = idx_row[2]
            if is_unique:
                idx_cols = frozenset(
                    r[2].lower()
                    for r in conn.execute(f"PRAGMA index_info({idx_name})").fetchall()
                )
                unique_indexes.add((idx_name, idx_cols))
    except sqlite3.Error:
        raise

    # foreign keys: set of (from_col, to_table, to_col)
    fks = set()
    try:
        for fk_row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            fks.add((fk_row[3].lower(), fk_row[2].lower(), fk_row[4].lower()))
    except sqlite3.Error:
        raise

    return {"columns": cols, "unique_indexes": list(unique_indexes), "foreign_keys": list(fks)}


def _parse_allowlist(sql_text: str) -> set[str]:
    """Parse preservation-allowlist entries from SQL comment header."""
    allowlist = set()
    for line in sql_text.splitlines():
        m = ALLOWLIST_PATTERN.match(line.strip())
        if m:
            for item in m.group(1).split(","):
                allowlist.add(item.strip().lower())
    return allowlist


def _check_migration(migration_file: Path, all_migrations: list[Path]) -> dict:
    idx = all_migrations.index(migration_file)
    sql_text = migration_file.read_text()

    # Find all table names involved in RENAME TO in this migration
    matches = RENAME_PATTERN.findall(sql_text)
    if not matches:
        return {"file": migration_file.name, "status": "skip", "drifts": []}

    allowlist = _parse_allowlist(sql_text)

    drifts = []

    for old_table in matches:
        old_table = old_table.lower()

        # Pre-state: apply migrations 0..idx-1
        # Legacy migrations may depend on base schemas not present in schemas/migrations/.
        # If context setup fails, skip this migration with a warning rather than
        # treating it as a gate failure — the linter goal is NEW migration drift, not
        # replaying an irresolvable history.
        try:
            pre_conn = _apply_migrations(all_migrations[:idx])
        except RuntimeError as exc:
            print(
                f"  [warn] {migration_file.name}: pre-state unavailable for '{old_table}'"
                f" (base schema dependency): {exc}",
                file=sys.stderr,
            )
            return {
                "file": migration_file.name,
                "status": "skip",
                "drifts": [],
                "skip_reason": f"pre-state unavailable: {exc}",
            }
        pre_schema = _capture_table_schema(pre_conn, old_table)
        pre_conn.close()

        # Post-state: apply migration idx as well
        try:
            post_conn = _apply_migrations(all_migrations[: idx + 1])
        except RuntimeError as exc:
            print(
                f"  [warn] {migration_file.name}: post-state unavailable for '{old_table}'"
                f" (base schema dependency): {exc}",
                file=sys.stderr,
            )
            return {
                "file": migration_file.name,
                "status": "skip",
                "drifts": [],
                "skip_reason": f"post-state unavailable: {exc}",
            }

        # After rename, the table has a new name — try to determine it
        new_name_match = re.search(
            rf"ALTER\s+TABLE\s+{re.escape(old_table)}\s+RENAME\s+TO\s+(\w+)",
            sql_text,
            re.IGNORECASE,
        )
        post_table = new_name_match.group(1).lower() if new_name_match else old_table
        post_schema = _capture_table_schema(post_conn, post_table)

        # Also check the old name in case the rename kept it in place
        if not post_schema["columns"]:
            post_schema = _capture_table_schema(post_conn, old_table)
            post_table = old_table

        post_conn.close()

        pre_cols = set(pre_schema["columns"])
        post_cols = set(post_schema["columns"])

        # Column drift
        for missing_col in pre_cols - post_cols:
            key = f"{old_table}.{missing_col}"
            if key not in allowlist:
                drifts.append({"type": "column_dropped", "table": old_table, "item": missing_col, "allowlisted": False})

        # Unique index drift
        pre_uniq = {frozenset(cols) for _, cols in pre_schema["unique_indexes"]}
        post_uniq = {frozenset(cols) for _, cols in post_schema["unique_indexes"]}
        pre_idx_names = {name: frozenset(cols) for name, cols in pre_schema["unique_indexes"]}

        for idx_name, idx_cols in pre_schema["unique_indexes"]:
            if frozenset(idx_cols) not in post_uniq:
                key = f"{old_table}.{idx_name}"
                if key not in allowlist:
                    drifts.append({"type": "unique_dropped", "table": old_table, "item": idx_name, "allowlisted": False})

        # FK drift
        pre_fks = set(tuple(fk) for fk in pre_schema["foreign_keys"])
        post_fks = set(tuple(fk) for fk in post_schema["foreign_keys"])
        for fk in pre_fks - post_fks:
            key = f"{old_table}.{fk[0]}"
            if key not in allowlist:
                drifts.append({"type": "fk_dropped", "table": old_table, "item": str(fk), "allowlisted": False})

    status = "fail" if drifts else "pass"
    return {"file": migration_file.name, "status": status, "drifts": drifts}


def main(migrations_directory: Optional[Path] = None) -> int:
    directory = migrations_directory or _migrations_dir()
    all_migrations = _sorted_migrations(directory)
    results = []

    for mf in all_migrations:
        sql_text = mf.read_text()
        if RENAME_PATTERN.search(sql_text):
            try:
                result = _check_migration(mf, all_migrations)
            except (RuntimeError, Exception) as exc:
                print(f"  [fatal] gate error for {mf.name}: {exc}", file=sys.stderr)
                results.append({"file": mf.name, "status": "error", "drifts": [], "error": str(exc)})
                continue
            results.append(result)

    failures = [r for r in results if r["status"] in ("fail", "error", "skip")]
    output = {
        "scanned": results,
        "total_files": len(all_migrations),
        "files_with_rename": len(results),
        "failures": len(failures),
    }
    print(json.dumps(output, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
