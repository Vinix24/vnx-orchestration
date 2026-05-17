#!/usr/bin/env python3
"""Shared helper for idempotent SQLite schema migrations via PRAGMA user_version.

Usage
-----
    from schema_migration import apply_if_below

    # conn.isolation_level = None is recommended for explicit control
    apply_if_below(conn, 2, _mig_v2_add_column)
    apply_if_below(conn, 3, _mig_v3_add_table)

Migration functions passed to apply_if_below MUST use conn.execute() for
individual statements. Never call conn.executescript() inside migration_fn —
executescript() commits any active transaction and breaks SAVEPOINT atomicity.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Callable

logger = logging.getLogger(__name__)


def get_user_version(conn: sqlite3.Connection) -> int:
    """Return PRAGMA user_version for the connection's database."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def apply_if_below(
    conn: sqlite3.Connection,
    target_version: int,
    migration_fn: Callable[[sqlite3.Connection], None],
) -> bool:
    """Apply *migration_fn* only when PRAGMA user_version < *target_version*.

    Uses a SAVEPOINT so mid-migration failures roll back cleanly without
    corrupting the database. Sets PRAGMA user_version = target_version on
    success. Returns True if the migration was applied, False if skipped.

    migration_fn must use conn.execute() only — no conn.executescript().
    """
    if get_user_version(conn) >= target_version:
        return False

    sp = f'"vnx_mig_{target_version}"'
    conn.execute(f"SAVEPOINT {sp}")
    try:
        migration_fn(conn)
        conn.execute(f"PRAGMA user_version = {target_version}")
        conn.execute(f"RELEASE SAVEPOINT {sp}")
        logger.debug("schema migration applied: user_version → %d", target_version)
    except Exception:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:
            pass
        raise
    return True


def apply_script_if_below(
    conn: sqlite3.Connection,
    target_version: int,
    sql: str,
) -> bool:
    """Apply a SQL script via executescript() if user_version < target_version.

    Because executescript() commits automatically, the schema changes and the
    user_version stamp occur in separate transactions. This is safe when all
    SQL uses CREATE TABLE IF NOT EXISTS and ALTER TABLE is preceded by
    column-existence checks — a crash between the two commits causes a harmless
    re-run of idempotent SQL on next start.

    Returns True if the script was applied, False if skipped.
    """
    if get_user_version(conn) >= target_version:
        return False

    conn.executescript(sql)
    # executescript() committed above; stamp the version in a new transaction
    sp = f'"vnx_ver_{target_version}"'
    conn.execute(f"SAVEPOINT {sp}")
    conn.execute(f"PRAGMA user_version = {target_version}")
    conn.execute(f"RELEASE SAVEPOINT {sp}")
    logger.debug("schema script applied: user_version → %d", target_version)
    return True
