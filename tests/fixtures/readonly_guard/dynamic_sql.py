"""Fixture: the first argument is a variable, so the guard cannot read the SQL.

Outside the allowlist this must fail closed with reason ``unreadable_sql``.
"""
import sqlite3


def ingest_rules_unreadable(conn: sqlite3.Connection, statement: str) -> None:
    conn.execute(statement)
