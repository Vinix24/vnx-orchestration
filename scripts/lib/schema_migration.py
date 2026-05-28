#!/usr/bin/env python3
"""Shared helper for idempotent SQLite schema migrations via PRAGMA user_version.

Usage
-----
    from schema_migration import apply_if_below, apply_script_if_below

    # conn.isolation_level = None is recommended for explicit control
    apply_if_below(conn, 2, _mig_v2_add_column)        # Python migration fn
    apply_script_if_below(conn, 3, sql_text)            # SQL-file string

Both helpers run under a SAVEPOINT — mid-migration failure rolls back ALL
statements + the version stamp atomically.

For ``apply_if_below``: migration_fn MUST use ``conn.execute()`` for individual
statements. Never call ``conn.executescript()`` inside migration_fn —
executescript() commits any active transaction and breaks SAVEPOINT atomicity.

For ``apply_script_if_below``: pass the raw SQL text; the helper splits it
into individual statements (quote- and comment-aware) and executes them
inside the SAVEPOINT.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Callable

logger = logging.getLogger(__name__)

# Pre-flight hook registry: migration version → list of fn(conn) callables.
# Hooks run inside apply_script_if_below before any SQL executes.
# Each fn must raise (e.g. RuntimeError) to abort; returning None means pass.
_PREFLIGHT_HOOKS: dict[int, list] = {}


def register_preflight(version: int, fn) -> None:
    """Register fn(conn) as a pre-flight guard for migration *version*.

    Called inside apply_script_if_below before SQL execution, so direct callers
    cannot bypass the check by skipping the wrapper function.
    """
    _PREFLIGHT_HOOKS.setdefault(version, []).append(fn)


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
        except Exception as rollback_err:
            logger.warning(
                "rollback/release failed (continuing): %s: %s",
                type(rollback_err).__name__, rollback_err,
            )
            # not re-raising: already in error-recovery path; logging ensures
            # observability without masking the primary failure
        raise
    return True


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements by `;` boundaries.

    Handles single/double-quoted strings (no semicolon-split inside literals).
    Ignores SQL line comments (`-- …`) and block comments (`/* … */`).
    Returns non-empty, stripped statements ready for ``conn.execute()``.
    """
    statements: list[str] = []
    current: list[str] = []
    i = 0
    n = len(sql)
    in_string = False
    quote_char: str | None = None
    in_block_comment = False
    in_line_comment = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            current.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            current.append(ch)
            if ch == "*" and nxt == "/":
                current.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            current.append(ch)
            if ch == quote_char:
                in_string = False
                quote_char = None
            i += 1
            continue
        # not in string/comment
        if ch == "-" and nxt == "-":
            in_line_comment = True
            current.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            current.append(ch)
            current.append(nxt)
            i += 2
            continue
        if ch in ('"', "'"):
            in_string = True
            quote_char = ch
            current.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def apply_script_if_below(
    conn: sqlite3.Connection,
    target_version: int,
    sql: str,
) -> bool:
    """Apply a SQL script atomically if user_version < target_version.

    Splits the script into individual statements and executes them inside a
    SAVEPOINT together with the ``PRAGMA user_version`` stamp. Mid-script
    failure rolls back ALL statements + the version stamp atomically — no
    partial schema state is left behind (codex round-2 atomicity fix).

    Returns True if the script was applied, False if skipped.
    """
    if get_user_version(conn) >= target_version:
        return False

    for hook in _PREFLIGHT_HOOKS.get(target_version, []):
        hook(conn)  # raises on failure; prevents any SQL from executing

    statements = _split_sql_statements(sql)
    sp = f'"vnx_ver_{target_version}"'
    conn.execute(f"SAVEPOINT {sp}")
    try:
        for stmt in statements:
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {target_version}")
        conn.execute(f"RELEASE SAVEPOINT {sp}")
        logger.debug("schema script applied atomically: user_version → %d", target_version)
    except Exception:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception as rollback_err:
            logger.warning(
                "rollback/release failed (continuing): %s: %s",
                type(rollback_err).__name__, rollback_err,
            )
        raise
    return True
