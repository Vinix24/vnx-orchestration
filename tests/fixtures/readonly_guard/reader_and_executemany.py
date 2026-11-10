"""Fixture: a literal SELECT is a read, but every executemany is a write.

The guard must report only ``write_anyway``, whatever executemany's argument is.
"""
import sqlite3


def read_only(conn: sqlite3.Connection) -> None:
    conn.execute("SELECT 1")


def write_anyway(conn: sqlite3.Connection) -> None:
    conn.executemany("SELECT 1", [])
