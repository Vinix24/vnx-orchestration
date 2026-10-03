#!/usr/bin/env python3
"""Regression: intelligence import merges pattern rows by natural key.

``scripts/intelligence_import.py`` hydrates ``success_patterns`` /
``antipatterns`` from the git-tracked NDJSON export. It used to write every
exported row with ``INSERT OR REPLACE`` including the exported autoincrement
``id``. A pattern's identity is its natural key
``(project_id, pattern_type, title)``, so that write:

* deleted a local row (and its id's references) when an export row with the
  same natural key arrived under a different id (the natural-key unique index
  makes REPLACE pick the local row); without the index it double-booked the
  pattern as a second row;
* overwrote an unrelated local pattern through a colliding id.

These tests drive the real ``import_intelligence`` entry point against tmp DBs
only — with and without the natural-key unique index — and assert the local
row survives with its id and the merged counter, that the same export imported
twice is a no-op, that a row without ``project_id`` is skipped rather than
stamped, and that the result reports inserted / merged / skipped.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import intelligence_import  # noqa: E402

PID = "vnx-dev"

SUCCESS_DDL = """
CREATE TABLE success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    pattern_data TEXT NOT NULL,
    code_example TEXT,
    prerequisites TEXT,
    outcomes TEXT,
    usage_count INTEGER DEFAULT 0,
    avg_completion_time INTEGER,
    confidence_score REAL DEFAULT 0.0,
    source_dispatch_ids TEXT,
    source_receipts TEXT,
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used DATETIME,
    valid_from DATETIME,
    valid_until DATETIME,
    invalidation_reason TEXT,
    tags TEXT,
    project_id TEXT NOT NULL
);
"""

ANTI_DDL = """
CREATE TABLE antipatterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    pattern_data TEXT NOT NULL,
    problem_example TEXT,
    why_problematic TEXT NOT NULL,
    better_alternative TEXT,
    occurrence_count INTEGER DEFAULT 1,
    avg_resolution_time INTEGER,
    severity TEXT DEFAULT 'medium',
    source_dispatch_ids TEXT,
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen DATETIME,
    valid_from DATETIME,
    valid_until DATETIME,
    invalidation_reason TEXT,
    tags TEXT,
    project_id TEXT NOT NULL
);
"""

OTHER_DDL = """
CREATE TABLE other_table (
    id INTEGER PRIMARY KEY,
    project_id TEXT,
    value TEXT
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_env(tmp_path: Path, *, natural_key_index: bool):
    """A tmp VNX layout with an empty quality DB and an export dir."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    data = tmp_path / "data"
    intel = tmp_path / "intel"
    for directory in (home, state, data, intel / "db_export"):
        directory.mkdir(parents=True, exist_ok=True)

    db = state / "quality_intelligence.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SUCCESS_DDL + ANTI_DDL + OTHER_DDL)
    if natural_key_index:
        conn.execute(
            "CREATE UNIQUE INDEX ux_success_patterns_natural_key "
            "ON success_patterns (project_id, pattern_type, title)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX ux_antipatterns_natural_key "
            "ON antipatterns (project_id, pattern_type, title)"
        )
    conn.commit()
    conn.close()

    paths = {
        "VNX_HOME": str(home),
        "VNX_STATE_DIR": str(state),
        "VNX_DATA_DIR": str(data),
        "VNX_INTELLIGENCE_DIR": str(intel),
        "VNX_CANONICAL_ROOT": str(home),
    }
    return paths, intel, db


def _seed_success(db: Path, *, title, project_id=PID, pattern_type="approach",
                  usage=1, description="seed", **extra) -> int:
    cols = {
        "project_id": project_id,
        "pattern_type": pattern_type,
        "category": "c",
        "title": title,
        "description": description,
        "pattern_data": "{}",
        "usage_count": usage,
        "confidence_score": 0.5,
        "source_dispatch_ids": "[]",
        "first_seen": "2026-01-01T00:00:00",
        "last_used": "2026-01-01T00:00:00",
    }
    cols.update(extra)
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            f"INSERT INTO success_patterns ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            tuple(cols.values()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _seed_antipattern(db: Path, *, title, project_id=PID, pattern_type="approach",
                      occurrence=1, **extra) -> int:
    cols = {
        "project_id": project_id,
        "pattern_type": pattern_type,
        "category": "c",
        "title": title,
        "description": "seed",
        "pattern_data": "{}",
        "why_problematic": "why",
        "severity": "medium",
        "occurrence_count": occurrence,
        "source_dispatch_ids": "[]",
        "first_seen": "2026-01-01T00:00:00",
        "last_seen": "2026-01-01T00:00:00",
    }
    cols.update(extra)
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            f"INSERT INTO antipatterns ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            tuple(cols.values()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _write_export(intel: Path, table: str, rows) -> None:
    path = intel / "db_export" / f"{table}.ndjson"
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _all_rows(db: Path, table: str, order_by: str = "id"):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")]
    finally:
        conn.close()


def _success_row(*, id, project_id=PID, pattern_type="approach", category="c",
                 title, description="exported", pattern_data="{}", usage_count=1,
                 confidence_score=0.5, source_dispatch_ids="[]",
                 first_seen="2026-01-01T00:00:00", last_used="2026-02-01T00:00:00"):
    return {
        "id": id,
        "project_id": project_id,
        "pattern_type": pattern_type,
        "category": category,
        "title": title,
        "description": description,
        "pattern_data": pattern_data,
        "usage_count": usage_count,
        "confidence_score": confidence_score,
        "source_dispatch_ids": source_dispatch_ids,
        "first_seen": first_seen,
        "last_used": last_used,
    }


# ---------------------------------------------------------------------------
# (1)(2)(5) natural-key collision under a different id merges into the local row
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("natural_key_index", [True, False])
def test_same_natural_key_under_new_id_merges_and_keeps_local_id(
    tmp_path, natural_key_index
):
    paths, intel, db = _make_env(tmp_path, natural_key_index=natural_key_index)
    local_id = _seed_success(db, title="T", usage=5, description="local")
    _write_export(intel, "success_patterns", [
        _success_row(id=7, title="T", description="exported", usage_count=2),
    ])

    result = intelligence_import.import_intelligence(paths)

    rows = _all_rows(db, "success_patterns")
    assert len(rows) == 1, "the same natural key must not become two rows"
    row = rows[0]
    assert row["id"] == local_id, "the local row keeps its id"
    assert row["title"] == "T"
    assert row["usage_count"] == 5, "the higher (local) counter is not lost"
    stats = result["pattern_import"]["success_patterns"]
    assert stats["merged"] == 1
    assert stats["inserted"] == 0
    assert stats["skipped"] == 0


# ---------------------------------------------------------------------------
# (1)(5)(8) id collision must not overwrite a different pattern
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("natural_key_index", [True, False])
def test_colliding_id_does_not_overwrite_a_different_pattern(
    tmp_path, natural_key_index
):
    paths, intel, db = _make_env(tmp_path, natural_key_index=natural_key_index)
    local_id = _seed_success(db, title="T", usage=1, description="local")
    _write_export(intel, "success_patterns", [
        _success_row(id=local_id, title="OTHER", description="exported"),
    ])

    result = intelligence_import.import_intelligence(paths)

    rows = {r["title"]: r for r in _all_rows(db, "success_patterns")}
    assert set(rows) == {"T", "OTHER"}, "both patterns survive the id collision"
    assert rows["T"]["id"] == local_id
    assert rows["T"]["description"] == "local"
    assert rows["OTHER"]["id"] != local_id
    stats = result["pattern_import"]["success_patterns"]
    assert stats["inserted"] == 1
    assert stats["merged"] == 0


# ---------------------------------------------------------------------------
# (3) a new natural key is inserted, and (4) re-import is idempotent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("natural_key_index", [True, False])
def test_new_natural_key_is_inserted(tmp_path, natural_key_index):
    paths, intel, db = _make_env(tmp_path, natural_key_index=natural_key_index)
    _write_export(intel, "success_patterns", [
        _success_row(id=42, title="brand new", description="exported", usage_count=3),
    ])

    result = intelligence_import.import_intelligence(paths)

    rows = _all_rows(db, "success_patterns")
    assert len(rows) == 1
    assert rows[0]["title"] == "brand new"
    assert rows[0]["usage_count"] == 3
    assert result["pattern_import"]["success_patterns"]["inserted"] == 1


@pytest.mark.parametrize("natural_key_index", [True, False])
def test_reimport_leaves_pattern_tables_unchanged(tmp_path, natural_key_index):
    paths, intel, db = _make_env(tmp_path, natural_key_index=natural_key_index)
    _seed_success(db, title="local only", usage=5, description="local")
    _write_export(intel, "success_patterns", [
        # Overlaps the local natural key under another id.
        _success_row(id=7, title="local only", description="exported", usage_count=2),
        # A brand-new pattern, and an export row with the local row's id but a
        # different natural key.
        _success_row(id=999, title="brand new"),
        _success_row(id=1, title="OTHER"),
        # Timestamps omitted on purpose: the writer's "now" default must not
        # drift the stored values across imports.
        {"id": 12, "project_id": PID, "pattern_type": "approach", "category": "c",
         "title": "no timestamps", "description": "exported", "pattern_data": "{}",
         "usage_count": 4, "confidence_score": 0.5},
    ])

    intelligence_import.import_intelligence(paths)
    first = _all_rows(db, "success_patterns", order_by="project_id, pattern_type, title")
    intelligence_import.import_intelligence(paths)
    second = _all_rows(db, "success_patterns", order_by="project_id, pattern_type, title")

    assert first == second
    counts = {r["title"]: r["usage_count"] for r in second}
    assert counts["local only"] == 5
    assert counts["brand new"] == 1
    assert counts["no timestamps"] == 4


# ---------------------------------------------------------------------------
# (2) antipatterns follow the same natural-key merge
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("natural_key_index", [True, False])
def test_antipattern_same_natural_key_merges(tmp_path, natural_key_index):
    paths, intel, db = _make_env(tmp_path, natural_key_index=natural_key_index)
    local_id = _seed_antipattern(db, title="A", occurrence=4)
    _write_export(intel, "antipatterns", [
        {"id": 9, "project_id": PID, "pattern_type": "approach", "category": "c",
         "title": "A", "description": "exported", "pattern_data": "{}",
         "why_problematic": "exported why", "severity": "high",
         "occurrence_count": 1, "first_seen": "2026-01-01T00:00:00",
         "last_seen": "2026-02-01T00:00:00"},
    ])

    result = intelligence_import.import_intelligence(paths)

    rows = _all_rows(db, "antipatterns")
    assert len(rows) == 1
    assert rows[0]["id"] == local_id
    assert rows[0]["occurrence_count"] == 4
    assert result["pattern_import"]["antipatterns"]["merged"] == 1


# ---------------------------------------------------------------------------
# (6) a pattern without project_id is skipped, never guessed, and counted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("natural_key_index", [True, False])
def test_pattern_without_project_id_is_skipped_and_counted(
    tmp_path, natural_key_index
):
    paths, intel, db = _make_env(tmp_path, natural_key_index=natural_key_index)
    _write_export(intel, "success_patterns", [
        {"id": 2, "pattern_type": "approach", "category": "c", "title": "no project",
         "description": "exported", "pattern_data": "{}", "usage_count": 2},
        _success_row(id=3, title="has project", usage_count=1),
    ])

    result = intelligence_import.import_intelligence(paths)

    titles = {r["title"] for r in _all_rows(db, "success_patterns")}
    assert titles == {"has project"}, "the unscoped row must not be written"
    stats = result["pattern_import"]["success_patterns"]
    assert stats["inserted"] == 1
    assert stats["merged"] == 0
    assert stats["skipped"] == 1
    assert result["pattern_import"]["success_patterns"]["skipped"] == 1


# ---------------------------------------------------------------------------
# (7) other tables keep the current INSERT OR REPLACE import behaviour
# ---------------------------------------------------------------------------

def test_other_tables_keep_insert_or_replace(tmp_path):
    paths, intel, db = _make_env(tmp_path, natural_key_index=True)
    _write_export(intel, "other_table", [
        {"id": 1, "project_id": PID, "value": "first"},
    ])

    result = intelligence_import.import_intelligence(paths)
    assert result["tables_imported"]["other_table"] == 1
    assert _all_rows(db, "other_table")[0]["value"] == "first"

    # Replace is the documented behaviour for non-pattern tables.
    _write_export(intel, "other_table", [
        {"id": 1, "project_id": PID, "value": "replaced"},
    ])
    intelligence_import.import_intelligence(paths)
    assert _all_rows(db, "other_table")[0]["value"] == "replaced"
    assert "other_table" not in result["pattern_import"]


# ---------------------------------------------------------------------------
# The pattern import path merges on the natural key.
# ---------------------------------------------------------------------------

def test_import_pattern_table_merges_on_natural_key(tmp_path):
    paths, intel, db = _make_env(tmp_path, natural_key_index=True)
    local_id = _seed_success(db, title="T", usage=5, description="local")
    _write_export(intel, "success_patterns", [
        _success_row(id=7, title="T", description="exported", usage_count=2),
    ])

    ndjson = intel / "db_export" / "success_patterns.ndjson"
    conn = sqlite3.connect(str(db))
    try:
        counts = intelligence_import._import_pattern_table(conn, "success_patterns", ndjson)
        conn.commit()
    finally:
        conn.close()

    assert counts == {"inserted": 0, "merged": 1, "skipped": 0}
    rows = _all_rows(db, "success_patterns")
    assert [r["id"] for r in rows] == [local_id]
    assert rows[0]["usage_count"] == 5


def test_import_table_returns_plain_int_for_other_tables(tmp_path):
    paths, intel, db = _make_env(tmp_path, natural_key_index=True)
    _write_export(intel, "other_table", [
        {"id": 1, "project_id": PID, "value": "v"},
    ])

    ndjson = intel / "db_export" / "other_table.ndjson"
    conn = sqlite3.connect(str(db))
    try:
        count = intelligence_import._import_table(conn, "other_table", ndjson)
        conn.commit()
    finally:
        conn.close()

    assert type(count) is int
    assert count == 1


def test_import_table_refuses_pattern_tables(tmp_path):
    paths, intel, db = _make_env(tmp_path, natural_key_index=True)
    ndjson = intel / "db_export" / "success_patterns.ndjson"
    conn = sqlite3.connect(str(db))
    try:
        with pytest.raises(ValueError):
            intelligence_import._import_table(conn, "success_patterns", ndjson)
    finally:
        conn.close()


def test_pattern_table_with_nothing_written_is_not_listed_as_imported(tmp_path):
    paths, intel, db = _make_env(tmp_path, natural_key_index=True)
    _write_export(intel, "success_patterns", [
        {"id": 2, "pattern_type": "approach", "title": "no project"},
    ])

    result = intelligence_import.import_intelligence(paths)

    assert "success_patterns" not in result["tables_imported"]
    assert result["pattern_import"]["success_patterns"]["skipped"] == 1
