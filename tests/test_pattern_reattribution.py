#!/usr/bin/env python3
"""Tests for pattern_reattribution.py (D3b, learning-loop-sluiten).

(a) proves the BEHAVIOUR the existing injection selector has today: a pattern
row mis-stamped with the wrong project_id is offered to that wrong project and
withheld from the project that actually produced it — RED against the
untouched selector, GREEN after an --apply run of the reattribution.
(b)-(d) cover the other ADR-068 categories (ambiguous, absent, unattributed)
and idempotency of --apply.

All DB and receipt-store state lives under pytest's tmp_path — never touches
a real ~/.vnx-data store.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import pattern_reattribution as pr  # noqa: E402
from pattern_reattribution import apply_decisions, main, scan_database  # noqa: E402
from intelligence_sources.proven_pattern import _query_central as _query_central_sp  # noqa: E402

_SCHEMA_SQL = """
CREATE TABLE success_patterns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pattern_type TEXT NOT NULL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  pattern_data TEXT NOT NULL,
  usage_count INTEGER DEFAULT 0,
  confidence_score REAL DEFAULT 0.0,
  source_dispatch_ids TEXT,
  first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_used DATETIME,
  valid_from DATETIME DEFAULT NULL,
  valid_until DATETIME DEFAULT NULL,
  project_id TEXT NOT NULL,
  tags TEXT
);
CREATE UNIQUE INDEX ux_success_patterns_pid ON success_patterns (project_id, id);

CREATE TABLE antipatterns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pattern_type TEXT NOT NULL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  pattern_data TEXT NOT NULL,
  why_problematic TEXT NOT NULL,
  occurrence_count INTEGER DEFAULT 0,
  severity TEXT DEFAULT 'medium',
  source_dispatch_ids TEXT,
  first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_seen DATETIME,
  valid_from DATETIME DEFAULT NULL,
  valid_until DATETIME DEFAULT NULL,
  project_id TEXT NOT NULL,
  tags TEXT
);
CREATE UNIQUE INDEX ux_antipatterns_pid ON antipatterns (project_id, id);

CREATE TABLE dispatch_pattern_offered (
  dispatch_id   TEXT NOT NULL,
  pattern_id    TEXT NOT NULL,
  pattern_title TEXT NOT NULL,
  offered_at    TEXT NOT NULL,
  project_id    TEXT NOT NULL,
  ab_arm TEXT,
  PRIMARY KEY (dispatch_id, pattern_id, project_id)
);

CREATE TABLE pattern_usage (
  pattern_id TEXT,
  pattern_title TEXT NOT NULL,
  pattern_hash TEXT NOT NULL,
  used_count INTEGER DEFAULT 0,
  ignored_count INTEGER DEFAULT 0,
  success_count INTEGER DEFAULT 0,
  failure_count INTEGER DEFAULT 0,
  last_used TIMESTAMP,
  last_offered TIMESTAMP,
  confidence REAL DEFAULT 1.0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  dispatch_id TEXT DEFAULT NULL,
  project_id TEXT NOT NULL,
  PRIMARY KEY (pattern_id, project_id)
);

CREATE TABLE pattern_injection_outcome (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dispatch_id TEXT NOT NULL,
  pattern_id TEXT NOT NULL,
  pattern_hash TEXT,
  used INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  evidence TEXT,
  project_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ab_arm TEXT,
  UNIQUE (project_id, dispatch_id, pattern_id)
);
"""


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def _insert_success_pattern(
    db_path: Path, *, project_id: str, source_dispatch_ids, title="Some pattern",
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """INSERT INTO success_patterns
                (pattern_type, category, title, description, pattern_data,
                 confidence_score, usage_count, source_dispatch_ids, project_id)
               VALUES ('code', 'architect', ?, 'desc', '{}', 0.8, 3, ?, ?)""",
            (
                title,
                json.dumps(source_dispatch_ids) if isinstance(source_dispatch_ids, list) else source_dispatch_ids,
                project_id,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _write_receipt_store(vnx_data_root: Path, project_id: str, dispatch_ids) -> None:
    state_dir = vnx_data_root / project_id / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "t0_receipts.ndjson"
    with path.open("a", encoding="utf-8") as fh:
        for dispatch_id in dispatch_ids:
            fh.write(json.dumps({
                "dispatch_id": dispatch_id,
                "project_id": project_id,
                "terminal": "T1",
                "event_type": "task_complete",
                "schema_version": 2,
            }) + "\n")


def _receipt(vnx_data_root: Path, store: str, dispatch_id, **fields) -> None:
    """One receipt line in ``store``'s ledger carrying exactly ``fields``."""
    state_dir = vnx_data_root / store / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"dispatch_id": dispatch_id, "schema_version": 2, **fields}) + "\n")


def _decide(tmp_path: Path, db: Path, mode: str = "--dry-run"):
    """The CLI, which old and new code share: (exit, {row id: (status, new project)})."""
    out = tmp_path / "report.json"
    code = main([mode, "--db", str(db), "--vnx-data-root", str(tmp_path / "vnx-data"), "--report", str(out)])
    rows = json.loads(out.read_text())["rows"] if out.exists() else []
    return code, {r["id"]: (r["status"], r["new_project_id"]) for r in rows}


def _build_index(vnx_data_root: Path):
    index = pr.build_attribution_index(vnx_data_root)
    return index.stores, index


def _db_in(tmp_path: Path, project: str, make=_make_db) -> Path:
    """A QI DB that is ``project``'s own file: ``<root>/<project>/state/``."""
    path = tmp_path / "vnx-data" / project / "state" / "quality_intelligence.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    make(path)
    return path


def _offered_to(db_path: Path, project_id: str):
    """Run the REAL selector (_query_central) scoped to project_id, central-DB style."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return _query_central_sp(
            "coding_interactive", [], central_conn_fn=lambda: conn,
            project_id_fn=lambda: project_id,
        )
    finally:
        pass  # caller-owned conn is closed by _query_central itself


# ---------------------------------------------------------------------------
# (a) red -> green against the real injection selector
# ---------------------------------------------------------------------------

def test_selector_offers_to_wrong_project_until_reattributed(tmp_path):
    db_path = _db_in(tmp_path, "project-b")
    vnx_data_root = tmp_path / "vnx-data"

    # Row stamped project-a, but its ONLY source dispatch actually ran in project-b.
    row_id = _insert_success_pattern(
        db_path, project_id="project-a", source_dispatch_ids=["D-100"],
        title="Fixed the flaky lock retry",
    )
    _write_receipt_store(vnx_data_root, "project-b", ["D-100"])
    # project-a's own store never saw D-100 (it didn't run there).
    (vnx_data_root / "project-a" / "state").mkdir(parents=True)
    (vnx_data_root / "project-a" / "state" / "t0_receipts.ndjson").write_text("")

    # RED: today's selector reflects the WRONG attribution.
    offered_to_a = _offered_to(db_path, "project-a")
    offered_to_b = _offered_to(db_path, "project-b")
    assert any(i.title == "Fixed the flaky lock retry" for i in offered_to_a), (
        "red-run premise: the mis-attributed row must still be offered to "
        "project-a before reattribution"
    )
    assert not any(i.title == "Fixed the flaky lock retry" for i in offered_to_b), (
        "red-run premise: project-b must NOT see it yet — that's the bug"
    )

    # Apply reattribution.
    stores, index = _build_index(vnx_data_root)
    decisions = scan_database(db_path, index)
    moved = [d for d in decisions if d.status == "moves"]
    assert len(moved) == 1
    assert moved[0].row_id == row_id
    assert moved[0].old_project_id == "project-a"
    assert moved[0].new_project_id == "project-b"
    result = apply_decisions(db_path, decisions)
    assert result == {"rows_moved": 1}

    # GREEN: selector now agrees with reality.
    offered_to_a_after = _offered_to(db_path, "project-a")
    offered_to_b_after = _offered_to(db_path, "project-b")
    assert not any(i.title == "Fixed the flaky lock retry" for i in offered_to_a_after)
    assert any(i.title == "Fixed the flaky lock retry" for i in offered_to_b_after)

    row = sqlite3.connect(str(db_path)).execute(
        "SELECT project_id FROM success_patterns WHERE id = ?", (row_id,)
    ).fetchone()
    assert row[0] == "project-b"


def test_apply_is_idempotent(tmp_path):
    db_path = _db_in(tmp_path, "project-b")
    vnx_data_root = tmp_path / "vnx-data"
    _insert_success_pattern(db_path, project_id="project-a", source_dispatch_ids=["D-1"])
    _write_receipt_store(vnx_data_root, "project-b", ["D-1"])

    _, index = _build_index(vnx_data_root)
    first = apply_decisions(db_path, scan_database(db_path, index))
    assert first == {"rows_moved": 1}

    second = apply_decisions(db_path, scan_database(db_path, index))
    assert second == {"rows_moved": 0}


# ---------------------------------------------------------------------------
# (b) mixed sources -> row stays, reason reported
# ---------------------------------------------------------------------------

def test_mixed_sources_stays_and_is_reported(tmp_path):
    db_path = tmp_path / "quality_intelligence.db"
    _make_db(db_path)
    vnx_data_root = tmp_path / "vnx-data"

    row_id = _insert_success_pattern(
        db_path, project_id="project-a", source_dispatch_ids=["D-1", "D-2"],
    )
    _write_receipt_store(vnx_data_root, "project-b", ["D-1"])
    _write_receipt_store(vnx_data_root, "project-c", ["D-2"])

    _, index = _build_index(vnx_data_root)
    decisions = scan_database(db_path, index)
    row_decision = next(d for d in decisions if d.row_id == row_id)
    assert row_decision.status == "ambiguous"
    assert row_decision.resolved_projects == ["project-b", "project-c"]

    apply_decisions(db_path, decisions)
    row = sqlite3.connect(str(db_path)).execute(
        "SELECT project_id FROM success_patterns WHERE id = ?", (row_id,)
    ).fetchone()
    assert row[0] == "project-a"  # unchanged


# ---------------------------------------------------------------------------
# (c) no match -> row stays, reason reported
# ---------------------------------------------------------------------------

def test_no_match_stays_and_is_reported(tmp_path):
    db_path = tmp_path / "quality_intelligence.db"
    _make_db(db_path)
    vnx_data_root = tmp_path / "vnx-data"
    row_id = _insert_success_pattern(
        db_path, project_id="project-a", source_dispatch_ids=["D-does-not-exist"],
    )
    _write_receipt_store(vnx_data_root, "project-b", ["D-1"])  # unrelated store exists

    _, index = _build_index(vnx_data_root)
    decisions = scan_database(db_path, index)
    row_decision = next(d for d in decisions if d.row_id == row_id)
    assert row_decision.status == "absent"
    assert row_decision.resolved_projects == []

    apply_decisions(db_path, decisions)
    row = sqlite3.connect(str(db_path)).execute(
        "SELECT project_id FROM success_patterns WHERE id = ?", (row_id,)
    ).fetchone()
    assert row[0] == "project-a"


def test_partial_match_stays_and_is_reported(tmp_path):
    db_path = tmp_path / "quality_intelligence.db"
    _make_db(db_path)
    vnx_data_root = tmp_path / "vnx-data"
    row_id = _insert_success_pattern(
        db_path, project_id="project-a", source_dispatch_ids=["D-1", "D-ghost"],
    )
    _receipt(vnx_data_root, "project-b", "D-1", project_id="unknown")  # one vote: no winner

    _, index = _build_index(vnx_data_root)
    decisions = scan_database(db_path, index)
    row_decision = next(d for d in decisions if d.row_id == row_id)
    assert row_decision.status == "store_only"
    assert row_decision.resolved_projects == []


def test_no_sources_stays_and_is_reported(tmp_path):
    db_path = tmp_path / "quality_intelligence.db"
    _make_db(db_path)
    vnx_data_root = tmp_path / "vnx-data"
    row_id = _insert_success_pattern(
        db_path, project_id="project-a", source_dispatch_ids=None,
    )

    _, index = _build_index(vnx_data_root)
    decisions = scan_database(db_path, index)
    row_decision = next(d for d in decisions if d.row_id == row_id)
    assert row_decision.status == "unattributed"


def test_all_sources_agree_with_own_project_is_unchanged(tmp_path):
    db_path = _db_in(tmp_path, "project-a")
    vnx_data_root = tmp_path / "vnx-data"
    row_id = _insert_success_pattern(
        db_path, project_id="project-a", source_dispatch_ids=["D-1"],
    )
    _write_receipt_store(vnx_data_root, "project-a", ["D-1"])

    _, index = _build_index(vnx_data_root)
    decisions = scan_database(db_path, index)
    row_decision = next(d for d in decisions if d.row_id == row_id)
    assert row_decision.status == "positive_own"

    result = apply_decisions(db_path, decisions)
    assert result == {"rows_moved": 0}


# ---------------------------------------------------------------------------
# antipatterns table gets the same treatment (prefix intel_ap_, own unique index)
# ---------------------------------------------------------------------------

def test_antipatterns_row_reattributed_and_junction_rows_follow(tmp_path):
    db_path = _db_in(tmp_path, "project-z")
    vnx_data_root = tmp_path / "vnx-data"

    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        """INSERT INTO antipatterns
            (pattern_type, category, title, description, pattern_data,
             why_problematic, occurrence_count, source_dispatch_ids, project_id)
           VALUES ('code', 'security', 'Bad retry', 'desc', '{}',
                   'because', 2, ?, 'project-a')""",
        (json.dumps(["D-9"]),),
    )
    row_id = cur.lastrowid
    pattern_id = f"intel_ap_{row_id}"
    now = "2026-09-01T00:00:00Z"
    conn.execute(
        "INSERT INTO dispatch_pattern_offered (dispatch_id, pattern_id, pattern_title, offered_at, project_id) "
        "VALUES ('D-9', ?, 'Bad retry', ?, 'project-a')",
        (pattern_id, now),
    )
    conn.execute(
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count, project_id) "
        "VALUES (?, 'Bad retry', 'hash123', 4, 'project-a')",
        (pattern_id,),
    )
    conn.execute(
        "INSERT INTO pattern_injection_outcome (dispatch_id, pattern_id, used, project_id) "
        "VALUES ('D-9', ?, 1, 'project-a')",
        (pattern_id,),
    )
    conn.commit()
    conn.close()

    _write_receipt_store(vnx_data_root, "project-z", ["D-9"])

    _, index = _build_index(vnx_data_root)
    decisions = scan_database(db_path, index)
    row_decision = next(d for d in decisions if d.table == "antipatterns" and d.row_id == row_id)
    assert row_decision.status == "moves"
    assert row_decision.new_project_id == "project-z"

    apply_decisions(db_path, decisions)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ap_row = conn.execute("SELECT project_id FROM antipatterns WHERE id = ?", (row_id,)).fetchone()
    assert ap_row["project_id"] == "project-z"

    dpo_row = conn.execute(
        "SELECT project_id FROM dispatch_pattern_offered WHERE pattern_id = ?", (pattern_id,)
    ).fetchone()
    assert dpo_row["project_id"] == "project-z"

    pu_row = conn.execute(
        "SELECT project_id, used_count FROM pattern_usage WHERE pattern_id = ?", (pattern_id,)
    ).fetchone()
    assert pu_row["project_id"] == "project-z"
    assert pu_row["used_count"] == 4

    pio_row = conn.execute(
        "SELECT project_id FROM pattern_injection_outcome WHERE pattern_id = ?", (pattern_id,)
    ).fetchone()
    assert pio_row["project_id"] == "project-z"
    conn.close()


# ---------------------------------------------------------------------------
# codex_gate ebf308f9: junction-row moves must survive a PRIMARY KEY / UNIQUE
# constraint that predates ADR-007 and does not include project_id.
#
# ``dispatch_pattern_offered``'s real bootstrap PRIMARY KEY (_migrate_v17 in
# scripts/quality_db_init.py) is ``(dispatch_id, pattern_id)`` — no
# project_id. ADR-007 later adds ``ux_dispatch_pattern_offered_pid`` as a
# SEPARATE unique INDEX over (project_id, dispatch_id, pattern_id); the real
# table PRIMARY KEY is untouched (SQLite ALTER TABLE cannot rewrite a PK), so
# a store bootstrapped before ``intelligence_sources/_recording.py``'s own
# fresh ``CREATE TABLE ... PRIMARY KEY (dispatch_id, pattern_id, project_id)``
# path existed still carries the 2-column PK today. Fixture below reproduces
# that real 2-column PK exactly (not the idealized 3-column PK the main
# ``_SCHEMA_SQL`` above uses, which matches the _recording.py fresh-bootstrap
# path instead).
# ---------------------------------------------------------------------------

_LEGACY_JUNCTION_SCHEMA_SQL = """
CREATE TABLE success_patterns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pattern_type TEXT NOT NULL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  pattern_data TEXT NOT NULL,
  usage_count INTEGER DEFAULT 0,
  confidence_score REAL DEFAULT 0.0,
  source_dispatch_ids TEXT,
  first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_used DATETIME,
  valid_from DATETIME DEFAULT NULL,
  valid_until DATETIME DEFAULT NULL,
  project_id TEXT NOT NULL,
  tags TEXT
);
CREATE UNIQUE INDEX ux_success_patterns_pid ON success_patterns (project_id, id);

CREATE TABLE antipatterns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pattern_type TEXT NOT NULL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  pattern_data TEXT NOT NULL,
  why_problematic TEXT NOT NULL,
  occurrence_count INTEGER DEFAULT 0,
  severity TEXT DEFAULT 'medium',
  source_dispatch_ids TEXT,
  first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_seen DATETIME,
  valid_from DATETIME DEFAULT NULL,
  valid_until DATETIME DEFAULT NULL,
  project_id TEXT NOT NULL,
  tags TEXT
);
CREATE UNIQUE INDEX ux_antipatterns_pid ON antipatterns (project_id, id);

-- Real _migrate_v17 bootstrap PK: (dispatch_id, pattern_id) — no project_id.
CREATE TABLE dispatch_pattern_offered (
  dispatch_id   TEXT NOT NULL,
  pattern_id    TEXT NOT NULL,
  pattern_title TEXT NOT NULL,
  offered_at    TEXT NOT NULL,
  project_id    TEXT NOT NULL DEFAULT 'vnx-dev',
  ab_arm TEXT,
  PRIMARY KEY (dispatch_id, pattern_id)
);

-- Real pre-migration-0010 PK: pattern_id alone — no project_id.
CREATE TABLE pattern_usage (
  pattern_id TEXT PRIMARY KEY,
  pattern_title TEXT NOT NULL,
  pattern_hash TEXT NOT NULL,
  used_count INTEGER DEFAULT 0,
  ignored_count INTEGER DEFAULT 0,
  success_count INTEGER DEFAULT 0,
  failure_count INTEGER DEFAULT 0,
  last_used TIMESTAMP,
  last_offered TIMESTAMP,
  confidence REAL DEFAULT 1.0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  dispatch_id TEXT DEFAULT NULL,
  project_id TEXT NOT NULL DEFAULT 'vnx-dev'
);

-- Hypothetical legacy variant: UNIQUE without project_id (real v28 bootstrap
-- always includes project_id in its UNIQUE — this fixture exists purely to
-- prove the reattribution code does not assume that).
CREATE TABLE pattern_injection_outcome (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dispatch_id TEXT NOT NULL,
  pattern_id TEXT NOT NULL,
  pattern_hash TEXT,
  used INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  evidence TEXT,
  project_id TEXT NOT NULL DEFAULT 'vnx-dev',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ab_arm TEXT,
  UNIQUE (dispatch_id, pattern_id)
);
"""


def _make_legacy_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_LEGACY_JUNCTION_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def _seed_antipattern_with_junction_rows(
    db_path: Path, *, project_id: str, source_dispatch_id: str,
) -> tuple[int, str]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """INSERT INTO antipatterns
                (pattern_type, category, title, description, pattern_data,
                 why_problematic, occurrence_count, source_dispatch_ids, project_id)
               VALUES ('code', 'security', 'Bad retry', 'desc', '{}',
                       'because', 2, ?, ?)""",
            (json.dumps([source_dispatch_id]), project_id),
        )
        row_id = cur.lastrowid
        pattern_id = f"intel_ap_{row_id}"
        now = "2026-09-01T00:00:00Z"
        conn.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at, project_id) "
            "VALUES (?, ?, 'Bad retry', ?, ?)",
            (source_dispatch_id, pattern_id, now, project_id),
        )
        conn.execute(
            "INSERT INTO pattern_usage "
            "(pattern_id, pattern_title, pattern_hash, used_count, project_id) "
            "VALUES (?, 'Bad retry', 'hash123', 4, ?)",
            (pattern_id, project_id),
        )
        conn.execute(
            "INSERT INTO pattern_injection_outcome "
            "(dispatch_id, pattern_id, used, project_id) VALUES (?, ?, 1, ?)",
            (source_dispatch_id, pattern_id, project_id),
        )
        conn.commit()
        return row_id, pattern_id
    finally:
        conn.close()


def test_legacy_pk_junction_rows_survive_reattribution_not_lost(tmp_path):
    """RED before the fix: INSERT OR IGNORE + DELETE silently drops the only
    ``dispatch_pattern_offered``/``pattern_usage``/``pattern_injection_outcome``
    row when the real PRIMARY KEY/UNIQUE predates ADR-007 and excludes
    project_id — the INSERT collides with the row it is meant to replace,
    is ignored, and the DELETE that follows erases it. GREEN after the fix:
    every junction row is moved (count 1, new project_id), never lost
    (count 0)."""
    db_path = _db_in(tmp_path, "project-z", _make_legacy_db)
    vnx_data_root = tmp_path / "vnx-data"

    row_id, pattern_id = _seed_antipattern_with_junction_rows(
        db_path, project_id="project-a", source_dispatch_id="D-9",
    )
    _write_receipt_store(vnx_data_root, "project-z", ["D-9"])

    _, index = _build_index(vnx_data_root)
    decisions = scan_database(db_path, index)
    row_decision = next(
        d for d in decisions if d.table == "antipatterns" and d.row_id == row_id
    )
    assert row_decision.status == "moves"
    assert row_decision.new_project_id == "project-z"

    apply_decisions(db_path, decisions)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ap_row = conn.execute(
            "SELECT project_id FROM antipatterns WHERE id = ?", (row_id,)
        ).fetchone()
        assert ap_row["project_id"] == "project-z"

        dpo_rows = conn.execute(
            "SELECT project_id FROM dispatch_pattern_offered WHERE pattern_id = ?",
            (pattern_id,),
        ).fetchall()
        assert len(dpo_rows) == 1, "the only offer row must survive the move, not vanish"
        assert dpo_rows[0]["project_id"] == "project-z"

        pu_rows = conn.execute(
            "SELECT project_id, used_count FROM pattern_usage WHERE pattern_id = ?",
            (pattern_id,),
        ).fetchall()
        assert len(pu_rows) == 1, "the only usage row must survive the move, not vanish"
        assert pu_rows[0]["project_id"] == "project-z"
        assert pu_rows[0]["used_count"] == 4

        pio_rows = conn.execute(
            "SELECT project_id FROM pattern_injection_outcome WHERE pattern_id = ?",
            (pattern_id,),
        ).fetchall()
        assert len(pio_rows) == 1, "the only outcome row must survive the move, not vanish"
        assert pio_rows[0]["project_id"] == "project-z"
    finally:
        conn.close()


def test_legacy_pk_reattribution_is_idempotent(tmp_path):
    """A second --apply over the same legacy-PK store moves nothing further
    and leaves exactly one junction row per table (no duplication either)."""
    db_path = _db_in(tmp_path, "project-z", _make_legacy_db)
    vnx_data_root = tmp_path / "vnx-data"
    row_id, pattern_id = _seed_antipattern_with_junction_rows(
        db_path, project_id="project-a", source_dispatch_id="D-9",
    )
    _write_receipt_store(vnx_data_root, "project-z", ["D-9"])

    _, index = _build_index(vnx_data_root)
    first = apply_decisions(db_path, scan_database(db_path, index))
    assert first == {"rows_moved": 1}
    second = apply_decisions(db_path, scan_database(db_path, index))
    assert second == {"rows_moved": 0}

    conn = sqlite3.connect(str(db_path))
    try:
        for table in ("dispatch_pattern_offered", "pattern_usage", "pattern_injection_outcome"):
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE pattern_id = ?", (pattern_id,)
            ).fetchone()[0]
            assert count == 1, f"{table} must carry exactly one row after two applies"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Genuine collision: BOTH the old and the new project already independently
# recorded a junction row for the same (dispatch_id, pattern_id) — only
# reachable on a schema whose key DOES include project_id (the idealized
# _SCHEMA_SQL fixture at the top of this file, matching the fresh-bootstrap
# path in intelligence_sources/_recording.py). Must merge, never silently
# drop either side.
# ---------------------------------------------------------------------------

def test_dispatch_pattern_offered_real_collision_is_merged_not_dropped(tmp_path):
    db_path = _db_in(tmp_path, "project-z")
    vnx_data_root = tmp_path / "vnx-data"

    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        """INSERT INTO antipatterns
            (pattern_type, category, title, description, pattern_data,
             why_problematic, occurrence_count, source_dispatch_ids, project_id)
           VALUES ('code', 'security', 'Bad retry', 'desc', '{}',
                   'because', 2, ?, 'project-a')""",
        (json.dumps(["D-9"]),),
    )
    row_id = cur.lastrowid
    pattern_id = f"intel_ap_{row_id}"
    # Both projects already have an "offered" row for the same dispatch_id +
    # pattern_id — a genuine duplicate under the 3-column PK.
    conn.execute(
        "INSERT INTO dispatch_pattern_offered (dispatch_id, pattern_id, pattern_title, offered_at, project_id) "
        "VALUES ('D-9', ?, 'Bad retry', '2026-01-01T00:00:00Z', 'project-a')",
        (pattern_id,),
    )
    conn.execute(
        "INSERT INTO dispatch_pattern_offered (dispatch_id, pattern_id, pattern_title, offered_at, project_id) "
        "VALUES ('D-9', ?, 'Bad retry', '2026-06-01T00:00:00Z', 'project-z')",
        (pattern_id,),
    )
    conn.commit()
    conn.close()

    _write_receipt_store(vnx_data_root, "project-z", ["D-9"])

    _, index = _build_index(vnx_data_root)
    decisions = scan_database(db_path, index)
    apply_decisions(db_path, decisions)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT project_id, offered_at FROM dispatch_pattern_offered WHERE pattern_id = ?",
            (pattern_id,),
        ).fetchall()
        assert len(rows) == 1, "collision must merge to one row, not two, not zero"
        assert rows[0]["project_id"] == "project-z"
        assert rows[0]["offered_at"] == "2026-06-01T00:00:00Z", (
            "merge must keep the later offered_at, not silently prefer either side"
        )
    finally:
        conn.close()


def test_pattern_injection_outcome_real_collision_is_merged_not_dropped(tmp_path):
    db_path = _db_in(tmp_path, "project-z")
    vnx_data_root = tmp_path / "vnx-data"

    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        """INSERT INTO antipatterns
            (pattern_type, category, title, description, pattern_data,
             why_problematic, occurrence_count, source_dispatch_ids, project_id)
           VALUES ('code', 'security', 'Bad retry', 'desc', '{}',
                   'because', 2, ?, 'project-a')""",
        (json.dumps(["D-9"]),),
    )
    row_id = cur.lastrowid
    pattern_id = f"intel_ap_{row_id}"
    conn.execute(
        "INSERT INTO pattern_injection_outcome (dispatch_id, pattern_id, used, project_id, created_at) "
        "VALUES ('D-9', ?, 1, 'project-a', '2026-01-01T00:00:00Z')",
        (pattern_id,),
    )
    conn.execute(
        "INSERT INTO pattern_injection_outcome (dispatch_id, pattern_id, used, project_id, created_at) "
        "VALUES ('D-9', ?, 0, 'project-z', '2026-06-01T00:00:00Z')",
        (pattern_id,),
    )
    conn.commit()
    conn.close()

    _write_receipt_store(vnx_data_root, "project-z", ["D-9"])

    _, index = _build_index(vnx_data_root)
    decisions = scan_database(db_path, index)
    apply_decisions(db_path, decisions)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT project_id, used, created_at FROM pattern_injection_outcome WHERE pattern_id = ?",
            (pattern_id,),
        ).fetchall()
        assert len(rows) == 1, "collision must merge to one row, not two, not zero"
        assert rows[0]["project_id"] == "project-z"
        assert rows[0]["created_at"] == "2026-06-01T00:00:00Z", (
            "merge must keep the later created_at row, not silently prefer either side"
        )
    finally:
        conn.close()


# ADR-068 besluit 2, 3, 11 through main(), so red1-red5 fail on behaviour on origin/main.
# The row sits in seocrawler-v2's own DB; vnx-dev has a DB, sales-copilot only receipts.

_A, _B, _C = "vnx-dev", "seocrawler-v2", "sales-copilot"
_PATH_A, _PATH_B = (f"/Users/x/.vnx-data/{p}/unified_reports/d-1.md" for p in (_A, _B))
_WIN_A = [(_A, "d-1", {"project_id": _A})]  # store and body agree: vnx-dev wins d-1

_CASES = {  # raw source_dispatch_ids, receipts (store, dispatch_id, fields), (status, new project)
    "red1_absent_id_does_not_block": (
        ["d-1", "d-2", "d-3"], _WIN_A + [(_A, "d-2", {"project_id": _A})], ("moves", _A)),
    "red2_body_and_path_beat_store": (
        ["d-1"], [(_B, "d-1", {"project_id": _A, "report_path": _PATH_A})], ("moves", _A)),
    "red3_known_prefix_is_stripped": (["sales-copilot:d-1"], [(_C, "d-1", {})], ("store_only", _B)),
    "red4_sentinel_is_no_id": (
        ["unknown"], [(s, "unknown", {"project_id": s}) for s in (_A, _B, _C)], ("unattributed", _B)),
    "null_column_is_unattributed": (None, _WIN_A, ("unattributed", _B)),
    "empty_list_is_unattributed": ("[]", _WIN_A, ("unattributed", _B)),
    "malformed_json_is_unattributed": ('["d-1"', _WIN_A, ("unattributed", _B)),
    "non_list_is_unattributed": ('{"d-1": 1}', _WIN_A, ("unattributed", _B)),
    "null_element_is_unattributed": ('["d-1", null]', _WIN_A, ("unattributed", _B)),
    "only_sentinels_are_unattributed": ('["unknown", "null", " "]', _WIN_A, ("unattributed", _B)),
    "unknown_prefix_is_kept": (["headless-review:d-1"], _WIN_A, ("absent", _B)),
    "receipt_without_dispatch_id_is_absent": (["d-1"], [(_A, None, {"project_id": _A})], ("absent", _B)),
    "unknown_project_is_no_vote": (["d-1"], [(_A, "d-1", {
        "project_id": "ghost", "report_path": "/x/.vnx-data/ghost/r.md"})], ("store_only", _B)),
    "sentinel_body_is_no_vote": (["d-1"], [(_A, "d-1", {"project_id": "unknown"})], ("store_only", _B)),
    "missing_body_is_no_vote": (["d-1"], [(_A, "d-1", {})], ("store_only", _B)),
    "tie_is_store_only": (["d-1"], _WIN_A + [(_B, "d-1", {"project_id": _B})], ("store_only", _B)),
    "two_winners_are_ambiguous": (["d-1", "d-2"], _WIN_A + [(_B, "d-2", {"project_id": _B})], ("ambiguous", _B)),
    "winner_without_own_db_is_ambiguous": (["d-1"], [(_C, "d-1", {"project_id": _C})], ("ambiguous", _B)),
}


def _seocrawler_world(tmp_path: Path, raw, receipts):
    db = _db_in(tmp_path, _B)
    _db_in(tmp_path, _A)
    row_id = _insert_success_pattern(db, project_id=_B, source_dispatch_ids=raw)
    for store, dispatch_id, fields in receipts:
        _receipt(tmp_path / "vnx-data", store, dispatch_id, **fields)
    return db, row_id


@pytest.mark.parametrize("raw,receipts,expected", list(_CASES.values()), ids=list(_CASES))
def test_vote_categories_fail_closed(tmp_path, raw, receipts, expected):
    db, row_id = _seocrawler_world(tmp_path, raw, receipts)
    assert _decide(tmp_path, db) == (0, {row_id: expected})


def test_red5_apply_refuses_a_move_into_another_db_file(tmp_path, capsys):
    """Red on origin/main: --apply relabels the row inside the seocrawler file."""
    db, _ = _seocrawler_world(tmp_path, ["d-1"], _WIN_A)
    _insert_success_pattern(db, project_id=_A, source_dispatch_ids=["d-2"])  # an in-file relabel
    _receipt(tmp_path / "vnx-data", _B, "d-2", project_id=_B)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    code, _ = _decide(tmp_path, db, "--apply")
    assert code != 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert "qi_transfer_apply.py" in capsys.readouterr().err


def test_red6_defaulted_body_never_moves_a_seocrawler_row(tmp_path):
    """Characterisation, green on origin/main too: body vnx-dev, store and path seocrawler-v2."""
    db, row_id = _seocrawler_world(
        tmp_path, ["d-1"], [(_B, "d-1", {"project_id": _A, "report_path": _PATH_B})])
    code, rows = _decide(tmp_path, db, "--apply")
    assert code == 0 and rows[row_id][1] == _B
    label = sqlite3.connect(str(db)).execute(
        "SELECT project_id FROM success_patterns WHERE id = ?", (row_id,)).fetchone()
    assert label == (_B,)


@pytest.mark.parametrize("destination", [None, "missing.db", "other.db"])
def test_apply_refuses_a_destination_that_is_not_provably_this_file(tmp_path, destination):
    db = _db_in(tmp_path, _B)
    _make_db(tmp_path / "other.db")
    row_id = _insert_success_pattern(db, project_id=_A, source_dispatch_ids=["d-1"])
    move = pr.ReattributionDecision(
        "success_patterns", row_id, "t", _A, "moves", _B,
        destination_db=str(tmp_path / destination) if destination else None)
    with pytest.raises(pr.CrossDatabaseMove, match="qi_transfer_apply.py"):
        apply_decisions(db, [move])
    assert sqlite3.connect(str(db)).execute("SELECT project_id FROM success_patterns").fetchone() == (_A,)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
