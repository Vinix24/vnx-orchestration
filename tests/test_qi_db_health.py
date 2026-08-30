#!/usr/bin/env python3
"""Tests for scripts/lib/qi_db_health.py — shared table_count classification
for quality_intelligence.db (sqlite_master-backed).

A 0-table file is a distinct third state from "missing" and "healthy" — it
must never be conflated with either (OI: absence-is-loud D5).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = REPO_ROOT / "scripts" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import qi_db_health as qh  # noqa: E402


def test_count_tables_missing_file_returns_none(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.db"
    assert qh.count_tables(missing) is None


def test_count_tables_zero_table_decoy_returns_zero(tmp_path: Path) -> None:
    decoy = tmp_path / "quality_intelligence.db"
    # The exact lazy-create side effect that produces the real-world decoy:
    # connect + close with no schema ever applied.
    sqlite3.connect(str(decoy)).close()
    assert decoy.exists()
    assert qh.count_tables(decoy) == 0


def test_count_tables_healthy_db_returns_positive(tmp_path: Path) -> None:
    healthy = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(healthy))
    try:
        conn.execute("CREATE TABLE success_patterns (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE antipatterns (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    assert qh.count_tables(healthy) == 2


def test_is_empty_schema_true_only_for_zero_table_existing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    decoy = tmp_path / "decoy.db"
    healthy = tmp_path / "healthy.db"

    sqlite3.connect(str(decoy)).close()
    conn = sqlite3.connect(str(healthy))
    try:
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
    finally:
        conn.close()

    # Three-branch check: missing != empty-schema != healthy. A resolver that
    # collapses "missing" and "0 tables" into the same branch would silently
    # treat a decoy as "nothing here yet" instead of refusing it.
    assert qh.is_empty_schema(missing) is False
    assert qh.is_empty_schema(decoy) is True
    assert qh.is_empty_schema(healthy) is False
