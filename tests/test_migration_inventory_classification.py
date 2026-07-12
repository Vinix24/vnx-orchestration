#!/usr/bin/env python3
"""Tests for migration_inventory table classification (central_db vs per-project).

Dispatch-ID: 20260712-183939-cockpit-pr8

ADR-007 grounding: every table touched by schemas/migrations/*.sql lives in one
of the three central VNX state DBs, so every touched table must classify as a
real bool — never `unknown` (None).
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import migration_inventory as mi  # noqa: E402


def test_every_touched_table_has_a_classification():
    surfaces = mi.build_inventory(REPO)
    sql_surface = surfaces[0]
    assert sql_surface.tables_touched, "expected at least one touched table"
    unknown = [tt.table for tt in sql_surface.tables_touched if tt.central_db is None]
    assert unknown == [], f"tables with no central_db classification: {unknown}"


def test_every_touched_table_is_central_db_true():
    # Verified 2026-07-12: the entire schemas/migrations/ surface exclusively
    # evolves the three central stores (quality_intelligence.db,
    # runtime_coordination.db, dispatch_tracker.db) per ADR-007. There is
    # currently no per-project-only table in this surface.
    surfaces = mi.build_inventory(REPO)
    sql_surface = surfaces[0]
    non_central = [tt.table for tt in sql_surface.tables_touched if tt.central_db is not True]
    assert non_central == []


def test_every_sql_file_resolves_to_at_least_one_table():
    surfaces = mi.build_inventory(REPO)
    sql_surface = surfaces[0]
    empty_files = [fname for fname, tables in sql_surface.per_file_tables if not tables]
    assert empty_files == [], f"SQL files with zero resolved tables: {empty_files}"


def test_classification_lookup_returns_none_for_unclassified_table():
    assert mi.CENTRAL_DB_TABLES.get("some_future_unclassified_table") is None
