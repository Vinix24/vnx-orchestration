"""Fixture: the write sits inside a function the scoped allowlist does name.

With that scoped allowlist the guard must pass and report no stale entry.
"""
import sqlite3


def save_pattern_metrics(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR REPLACE INTO pattern_usage (pattern_id) VALUES (?)", ("p1",))
