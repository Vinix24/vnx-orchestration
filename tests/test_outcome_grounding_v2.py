#!/usr/bin/env python3
"""Tests for outcome-grounding v2: junction-grounded confidence updates.

Dispatch-ID: 20260626-outcome-grounding-v2

Step 4 collapses the universal receipt-append confidence path onto the
``dispatch_pattern_offered`` junction (the precise per-dispatch offered↔pattern
linkage), gated by ``VNX_OUTCOME_GROUNDING_V2`` (default OFF → legacy
``source_dispatch_ids`` substring join, byte-identical).

Covers:
- junction grounds a pattern even when source_dispatch_ids is empty (the gap the
  legacy join could not close)
- flag OFF keeps the legacy substring behaviour exactly
- flag ON falls back to the legacy join when the junction table is absent
- grounding_source is recorded on the confidence_events audit row
- failure decays via the junction
- tenant isolation: a junction join never crosses project_id
- non-pattern junction rows (code_anchor etc.) are ignored
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from intelligence_persist import update_confidence_from_outcome  # noqa: E402

# ---------------------------------------------------------------------------
# Schema fixtures
# ---------------------------------------------------------------------------

_SCHEMA_BASE = """
CREATE TABLE IF NOT EXISTS success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    pattern_data TEXT NOT NULL,
    confidence_score REAL DEFAULT 0.0,
    usage_count INTEGER DEFAULT 0,
    source_dispatch_ids TEXT,
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used DATETIME{sp_project}
);

CREATE TABLE IF NOT EXISTS pattern_usage (
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
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS confidence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    terminal TEXT,
    outcome TEXT NOT NULL,
    patterns_boosted INTEGER DEFAULT 0,
    patterns_decayed INTEGER DEFAULT 0,
    confidence_change REAL NOT NULL,
    occurred_at TEXT NOT NULL,
    grounding_source TEXT
);
"""

_JUNCTION_DDL = """
CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
    dispatch_id   TEXT NOT NULL,
    pattern_id    TEXT NOT NULL,
    pattern_title TEXT NOT NULL,
    offered_at    TEXT NOT NULL{dpo_project},
    PRIMARY KEY (dispatch_id, pattern_id)
);
"""


def _make_db(tmp_path: Path, *, junction: bool = True, project_id: bool = False) -> Path:
    db = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        _SCHEMA_BASE.format(
            sp_project=",\n    project_id TEXT NOT NULL DEFAULT 'vnx-dev'" if project_id else ""
        )
    )
    if junction:
        conn.executescript(
            _JUNCTION_DDL.format(
                dpo_project=",\n    project_id TEXT NOT NULL DEFAULT 'vnx-dev'" if project_id else ""
            )
        )
    conn.commit()
    conn.close()
    return db


def _insert_pattern(
    db: Path,
    *,
    source_dispatch_ids,
    confidence: float = 0.5,
    title: str = "Test pattern",
    project_id: str = None,
) -> int:
    conn = sqlite3.connect(str(db))
    src = json.dumps(source_dispatch_ids) if source_dispatch_ids is not None else None
    if project_id is not None:
        cur = conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, description, pattern_data, "
            " confidence_score, usage_count, source_dispatch_ids, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "governance", title, "desc", "{}", confidence, 1, src, project_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, description, pattern_data, "
            " confidence_score, usage_count, source_dispatch_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "governance", title, "desc", "{}", confidence, 1, src),
        )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def _offer(db: Path, dispatch_id: str, pattern_id: str, title: str = "Test pattern", project_id: str = None) -> None:
    conn = sqlite3.connect(str(db))
    if project_id is not None:
        conn.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at, project_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (dispatch_id, pattern_id, title, "2026-06-26T00:00:00+00:00", project_id),
        )
    else:
        conn.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at) "
            "VALUES (?, ?, ?, ?)",
            (dispatch_id, pattern_id, title, "2026-06-26T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()


def _confidence(db: Path, sp_id: int) -> float:
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT confidence_score FROM success_patterns WHERE id = ?", (sp_id,)
    ).fetchone()
    conn.close()
    return float(row[0])


def _event_grounding(db: Path, dispatch_id: str) -> str:
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT grounding_source FROM confidence_events WHERE dispatch_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (dispatch_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


@pytest.fixture
def grounding_on(monkeypatch):
    monkeypatch.setenv("VNX_OUTCOME_GROUNDING_V2", "1")


@pytest.fixture
def grounding_off(monkeypatch):
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)


# ---------------------------------------------------------------------------
# 1. Junction grounds even when source_dispatch_ids is empty (the core gap)
# ---------------------------------------------------------------------------

def test_junction_grounds_with_empty_source_dispatch_ids(tmp_path, grounding_on):
    db = _make_db(tmp_path)
    sp_id = _insert_pattern(db, source_dispatch_ids=[], confidence=0.5)
    _offer(db, "D-1", f"intel_sp_{sp_id}")

    result = update_confidence_from_outcome(db, "D-1", "T1", "success")

    assert result == {"boosted": 1, "decayed": 0}
    # Beta first success → (1+1)/(1+0+2) = 2/3
    assert abs(_confidence(db, sp_id) - 2 / 3) < 1e-4
    assert _event_grounding(db, "D-1") == "junction"


# ---------------------------------------------------------------------------
# 2. Flag OFF keeps the legacy substring behaviour (junction ignored)
# ---------------------------------------------------------------------------

def test_flag_off_ignores_junction(tmp_path, grounding_off):
    db = _make_db(tmp_path)
    # Pattern is OFFERED in the junction but NOT stamped into source_dispatch_ids.
    sp_id = _insert_pattern(db, source_dispatch_ids=[], confidence=0.5)
    _offer(db, "D-1", f"intel_sp_{sp_id}")

    result = update_confidence_from_outcome(db, "D-1", "T1", "success")

    # Legacy LIKE on empty source_dispatch_ids finds nothing → no grounding.
    assert result == {"boosted": 0, "decayed": 0}
    assert abs(_confidence(db, sp_id) - 0.5) < 1e-9
    assert _event_grounding(db, "D-1") == "source_dispatch_ids"


# ---------------------------------------------------------------------------
# 3. Flag ON falls back to legacy when the junction table is absent
# ---------------------------------------------------------------------------

def test_flag_on_falls_back_to_legacy_without_junction(tmp_path, grounding_on):
    db = _make_db(tmp_path, junction=False)
    sp_id = _insert_pattern(db, source_dispatch_ids=["D-1"], confidence=0.5)

    result = update_confidence_from_outcome(db, "D-1", "T1", "success")

    assert result == {"boosted": 1, "decayed": 0}
    assert abs(_confidence(db, sp_id) - 2 / 3) < 1e-4
    assert _event_grounding(db, "D-1") == "source_dispatch_ids"


# ---------------------------------------------------------------------------
# 4. Failure decays via the junction
# ---------------------------------------------------------------------------

def test_junction_failure_decays(tmp_path, grounding_on):
    db = _make_db(tmp_path)
    sp_id = _insert_pattern(db, source_dispatch_ids=[], confidence=0.7)
    _offer(db, "D-9", f"intel_sp_{sp_id}")

    result = update_confidence_from_outcome(db, "D-9", "T1", "failure")

    assert result == {"boosted": 0, "decayed": 1}
    # Beta first failure → (0+1)/(0+1+2) = 1/3
    assert abs(_confidence(db, sp_id) - 1 / 3) < 1e-4
    assert _event_grounding(db, "D-9") == "junction"


# ---------------------------------------------------------------------------
# 5. Non-pattern junction rows (code_anchor etc.) are ignored
# ---------------------------------------------------------------------------

def test_non_pattern_junction_rows_ignored(tmp_path, grounding_on):
    db = _make_db(tmp_path)
    sp_id = _insert_pattern(db, source_dispatch_ids=[], confidence=0.5)
    _offer(db, "D-2", f"intel_sp_{sp_id}")
    # A code-anchor / doc pointer offered to the same dispatch — not a pattern.
    _offer(db, "D-2", "code_anchor_foo_bar", title="some/file.py:10")
    _offer(db, "D-2", "intel_ap_99", title="an antipattern, not a success pattern")

    result = update_confidence_from_outcome(db, "D-2", "T1", "success")

    # Only the single success-pattern grounds; the others do not resolve to a row.
    assert result == {"boosted": 1, "decayed": 0}


# ---------------------------------------------------------------------------
# 6. Junction offered once → updated exactly once (DISTINCT)
# ---------------------------------------------------------------------------

def test_junction_offered_once_updates_once(tmp_path, grounding_on):
    db = _make_db(tmp_path)
    sp_id = _insert_pattern(db, source_dispatch_ids=[], confidence=0.5)
    _offer(db, "D-3", f"intel_sp_{sp_id}")

    result = update_confidence_from_outcome(db, "D-3", "T1", "success")

    assert result["boosted"] == 1  # not 2, even though source_dispatch_ids also empty
    conn = sqlite3.connect(str(db))
    n_events = conn.execute(
        "SELECT COUNT(*) FROM confidence_events WHERE dispatch_id = ?", ("D-3",)
    ).fetchone()[0]
    conn.close()
    assert n_events == 1


# ---------------------------------------------------------------------------
# 7. Tenant isolation: the junction join never crosses project_id
# ---------------------------------------------------------------------------

def test_junction_tenant_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_OUTCOME_GROUNDING_V2", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", "proj-a")
    db = _make_db(tmp_path, project_id=True)

    sp_a = _insert_pattern(db, source_dispatch_ids=[], confidence=0.5, project_id="proj-a")
    sp_b = _insert_pattern(db, source_dispatch_ids=[], confidence=0.5, project_id="proj-b")
    # Same dispatch_id, both projects stamp a junction row (id-collision scenario).
    _offer(db, "D-T", f"intel_sp_{sp_a}", project_id="proj-a")
    _offer(db, "D-T", f"intel_sp_{sp_b}", project_id="proj-b")

    result = update_confidence_from_outcome(db, "D-T", "T1", "success")

    # Only proj-a's pattern is grounded; proj-b is untouched.
    assert result == {"boosted": 1, "decayed": 0}
    assert abs(_confidence(db, sp_a) - 2 / 3) < 1e-4
    assert abs(_confidence(db, sp_b) - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# 8. grounding_source omitted gracefully when the column is absent (old DB)
# ---------------------------------------------------------------------------

def test_grounding_source_column_absent_is_graceful(tmp_path, grounding_on):
    db = _make_db(tmp_path)
    # Drop the grounding_source column scenario: rebuild confidence_events without it.
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE confidence_events")
    conn.execute(
        "CREATE TABLE confidence_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL, terminal TEXT,"
        " outcome TEXT NOT NULL, patterns_boosted INTEGER DEFAULT 0,"
        " patterns_decayed INTEGER DEFAULT 0, confidence_change REAL NOT NULL,"
        " occurred_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    sp_id = _insert_pattern(db, source_dispatch_ids=[], confidence=0.5)
    _offer(db, "D-4", f"intel_sp_{sp_id}")

    # Must not raise even though grounding_source has nowhere to go.
    result = update_confidence_from_outcome(db, "D-4", "T1", "success")
    assert result == {"boosted": 1, "decayed": 0}

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT outcome, patterns_boosted FROM confidence_events WHERE dispatch_id = ?",
        ("D-4",),
    ).fetchone()
    conn.close()
    assert row == ("success", 1)
