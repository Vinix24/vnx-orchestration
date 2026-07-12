#!/usr/bin/env python3
"""Tests for migration_inventory — the six-surface migration inventory-lock (PR-8).

Dispatch-ID: 20260712-183939-cockpit-pr8

Read-only cataloguing only: these tests assert enumeration counts and shape, not
that anything is removed or migrated.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import migration_inventory as mi  # noqa: E402


def test_six_surfaces_present():
    surfaces = mi.build_inventory(REPO)
    assert [s.surface_id for s in surfaces] == [1, 2, 3, 4, 5, 6]
    names = {s.name for s in surfaces}
    assert names == {
        "sql-migrations",
        "python-appliers",
        "schema-manifest",
        "schema-migration-helpers",
        "project-id-migration",
        "migrate-cli",
    }


def test_sql_surface_has_at_least_42_files():
    surfaces = mi.build_inventory(REPO)
    sql_surface = surfaces[0]
    assert sql_surface.file_count >= 42
    assert len(sql_surface.paths) == sql_surface.file_count
    assert all(p.endswith(".sql") for p in sql_surface.paths)


def test_applier_surface_has_six_runners():
    surfaces = mi.build_inventory(REPO)
    applier_surface = surfaces[1]
    assert applier_surface.file_count == 6
    names = {Path(p).stem for p in applier_surface.paths}
    assert names == {
        "apply_0017", "apply_0019", "apply_0020",
        "apply_0022", "apply_0024", "apply_0026",
    }


def test_single_file_surfaces_point_at_real_paths():
    surfaces = mi.build_inventory(REPO)
    for surface in surfaces[2:]:
        assert surface.file_count == len(surface.paths)
        for p in surface.paths:
            assert (REPO / p).is_file(), f"{p} missing on disk for surface {surface.name}"


def test_no_file_is_deleted_or_modified_by_import():
    # This module is read-only cataloguing: importing/building the inventory
    # must not touch the working tree.
    import subprocess

    before = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "schemas/migrations"],
        capture_output=True, text=True, check=True,
    ).stdout
    mi.build_inventory(REPO)
    after = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "schemas/migrations"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert before == after
