#!/usr/bin/env python3
"""Idempotent wrapper for migrate.sql — applies ADR-007 tenant-scoping to scan_quota.

Usage:
    python3 scripts/apply_migration.py [db_path] [sql_path]

Defaults:
    db_path  = scan_quota.db
    sql_path = scripts/migrate.sql

Idempotency: checks whether project_id column already exists in scan_quota
before executing the SQL. Safe to run multiple times.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def apply(db_path: str, sql_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(scan_quota)")}
        if "project_id" in cols:
            print(f"[skip] project_id column already present in {db_path}. Migration already applied.")
            return

        sql = Path(sql_path).read_text()
        conn.executescript(sql)
        conn.commit()
        print(f"[ok] Migration applied to {db_path}.")
    finally:
        conn.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "scan_quota.db"
    sql = sys.argv[2] if len(sys.argv) > 2 else str(Path(__file__).parent / "migrate.sql")
    apply(db, sql)
