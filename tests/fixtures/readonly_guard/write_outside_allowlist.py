"""Fixture: one INSERT in a function the allowlist does not name.

The guard must fail on this module and report the function name and line.
"""
import sqlite3


def persist_without_approval(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO success_patterns (title) VALUES ('unapproved')")
