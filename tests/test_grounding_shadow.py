#!/usr/bin/env python3
"""Tests for outcome-grounding shadow mode: V1 vs V2 divergence report.

Dispatch-ID: D-selfimprove-d5-grounding

Named fixture: ``receipt_trail_gap_5x3``

Corpus design:
- 5 patterns total
- Patterns 1-3: offered to FAILED dispatches; source_dispatch_ids=[] (the receipt-trail gap)
- Patterns 4-5: offered to SUCCESSFUL dispatches; source_dispatch_ids=[dispatch_id]

V1 (legacy substring join) cannot ground patterns 1-3 for their failed dispatches
because the source_dispatch_ids list is empty — the LIKE match fails on "[]".
V2 (junction) finds them via dispatch_pattern_offered and correctly decays confidence.

Acceptance check (deterministic, no judge):
  For each failure dispatch, v2_new_conf < current_conf AND v1_new_conf == current_conf.
  The V2 projected confidence is strictly lower than V1 for the 3 failure patterns.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from intelligence_persist import shadow_grounding_compare, update_confidence_from_outcome  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture name — the named corpus referenced in the acceptance check
# ---------------------------------------------------------------------------

FIXTURE_NAME = "receipt_trail_gap_5x3"

# ---------------------------------------------------------------------------
# Schema / helpers (self-contained; no cross-test imports)
# ---------------------------------------------------------------------------

_SCHEMA = """
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
    last_used DATETIME
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

CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
    dispatch_id   TEXT NOT NULL,
    pattern_id    TEXT NOT NULL,
    pattern_title TEXT NOT NULL,
    offered_at    TEXT NOT NULL,
    PRIMARY KEY (dispatch_id, pattern_id)
);
"""


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db


def _insert_pattern(db: Path, *, source_dispatch_ids, confidence: float = 0.5, title: str = "Test pattern") -> int:
    conn = sqlite3.connect(str(db))
    src = json.dumps(source_dispatch_ids) if source_dispatch_ids is not None else None
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


def _offer(db: Path, dispatch_id: str, pattern_id: str, title: str = "Test pattern") -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO dispatch_pattern_offered "
        "(dispatch_id, pattern_id, pattern_title, offered_at) "
        "VALUES (?, ?, ?, ?)",
        (dispatch_id, pattern_id, title, "2026-07-04T00:00:00+00:00"),
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


# ---------------------------------------------------------------------------
# Named fixture builder: receipt_trail_gap_5x3
# ---------------------------------------------------------------------------

_FAILURE_DISPATCHES = ["D-shadow-F1", "D-shadow-F2", "D-shadow-F3"]
_SUCCESS_DISPATCHES = ["D-shadow-S1", "D-shadow-S2"]


def _build_fixture_db(tmp_path: Path):
    """Build the ``receipt_trail_gap_5x3`` corpus.

    Returns (db_path, dispatches_list) where dispatches_list has the 5 entries
    in the order [F1, F2, F3, S1, S2].
    """
    db = _make_db(tmp_path)

    # Patterns 1-3: offered to failed dispatches; source_dispatch_ids intentionally EMPTY
    sp_ids_failure = []
    for i, did in enumerate(_FAILURE_DISPATCHES, start=1):
        sp_id = _insert_pattern(db, source_dispatch_ids=[], confidence=0.5, title=f"Failure pattern {i}")
        _offer(db, did, f"intel_sp_{sp_id}", title=f"Failure pattern {i}")
        sp_ids_failure.append(sp_id)

    # Patterns 4-5: offered to successful dispatches; source_dispatch_ids populated
    sp_ids_success = []
    for i, did in enumerate(_SUCCESS_DISPATCHES, start=1):
        sp_id = _insert_pattern(db, source_dispatch_ids=[did], confidence=0.5, title=f"Success pattern {i}")
        _offer(db, did, f"intel_sp_{sp_id}", title=f"Success pattern {i}")
        sp_ids_success.append(sp_id)

    dispatches = (
        [{"dispatch_id": d, "status": "failure"} for d in _FAILURE_DISPATCHES]
        + [{"dispatch_id": d, "status": "success"} for d in _SUCCESS_DISPATCHES]
    )
    return db, dispatches, sp_ids_failure, sp_ids_success


# ---------------------------------------------------------------------------
# Acceptance check: receipt_trail_gap_5x3 — directional difference assertion
# ---------------------------------------------------------------------------

def test_receipt_trail_gap_5x3_directional_check(tmp_path, monkeypatch):
    """Fixture receipt_trail_gap_5x3: V2 shows lower confidence for failure patterns.

    This is the deterministic scripted acceptance check for D5.  No judge;
    the assertions are numeric comparisons on projected confidence values.
    """
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db, dispatches, sp_ids_failure, sp_ids_success = _build_fixture_db(tmp_path)

    report = shadow_grounding_compare(db, dispatches)
    summary = report["summary"]

    assert summary["junction_available"], "junction table must exist in fixture"
    assert summary["total_dispatches"] == 5

    failure_entries = [e for e in report["dispatches"] if e["status"] == "failure"]
    success_entries = [e for e in report["dispatches"] if e["status"] == "success"]
    assert len(failure_entries) == 3
    assert len(success_entries) == 2

    # Core directional check: for each failure dispatch
    for entry in failure_entries:
        assert entry["has_divergence"], (
            f"fixture {FIXTURE_NAME}: expected divergence for {entry['dispatch_id']}"
        )
        assert not entry["v1_only"], (
            f"{entry['dispatch_id']}: V1 must not find patterns with empty source_dispatch_ids"
        )
        assert entry["v2_only"], (
            f"{entry['dispatch_id']}: V2 must find the pattern via junction"
        )

        for sp_id in entry["v2_only"]:
            detail = entry["pattern_details"][sp_id]
            # V2 correctly decays the confidence because the dispatch failed
            assert detail["v2_new_conf"] < detail["current_conf"], (
                f"fixture {FIXTURE_NAME}: V2 must decay for failure "
                f"(sp_id={sp_id}, v2={detail['v2_new_conf']}, current={detail['current_conf']})"
            )
            # V1 is blind to this failure (empty source_dispatch_ids = no LIKE match)
            assert detail["v1_new_conf"] == detail["current_conf"], (
                f"fixture {FIXTURE_NAME}: V1 must leave confidence unchanged "
                f"(sp_id={sp_id}, v1={detail['v1_new_conf']}, current={detail['current_conf']})"
            )
            # The key directional assertion: V2 < V1
            assert detail["v2_new_conf"] < detail["v1_new_conf"], (
                f"fixture {FIXTURE_NAME}: V2 must be strictly lower than V1 for failure patterns "
                f"(sp_id={sp_id}, v2={detail['v2_new_conf']}, v1={detail['v1_new_conf']})"
            )

    # Summary aggregates
    assert summary["diverged_dispatches"] == 3
    assert summary["v2_only_grounded"] == 3
    assert summary["v1_only_grounded"] == 0

    # Success dispatches: both paths agree (source_dispatch_ids is populated)
    for entry in success_entries:
        assert not entry["v1_only"], (
            f"fixture {FIXTURE_NAME}: no V1-only for success dispatch {entry['dispatch_id']}"
        )
        assert not entry["v2_only"], (
            f"fixture {FIXTURE_NAME}: no V2-only for success dispatch {entry['dispatch_id']}"
        )
        for sp_id in entry["agreement"]:
            detail = entry["pattern_details"][sp_id]
            assert detail["v1_new_conf"] == detail["v2_new_conf"], (
                f"fixture {FIXTURE_NAME}: V1 and V2 must agree for success dispatch "
                f"(sp_id={sp_id})"
            )


# ---------------------------------------------------------------------------
# Guard: shadow must not change the live default or write to DB
# ---------------------------------------------------------------------------

def test_shadow_does_not_mutate_db(tmp_path, monkeypatch):
    """shadow_grounding_compare must not alter any confidence_score rows."""
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db, dispatches, sp_ids_failure, _ = _build_fixture_db(tmp_path)

    before = {sp_id: _confidence(db, sp_id) for sp_id in sp_ids_failure}
    shadow_grounding_compare(db, dispatches)
    after = {sp_id: _confidence(db, sp_id) for sp_id in sp_ids_failure}

    assert before == after, "shadow_grounding_compare must not write to success_patterns"


def test_shadow_does_not_change_env_flag(tmp_path, monkeypatch):
    """Calling shadow_grounding_compare must not set VNX_OUTCOME_GROUNDING_V2."""
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db, dispatches, _, _ = _build_fixture_db(tmp_path)

    shadow_grounding_compare(db, dispatches)

    assert os.environ.get("VNX_OUTCOME_GROUNDING_V2") is None, (
        "shadow_grounding_compare must not set VNX_OUTCOME_GROUNDING_V2"
    )


# ---------------------------------------------------------------------------
# V1 default preservation: flag unset → live update_confidence_from_outcome
# does NOT ground via junction (receipt-trail gap stays open until V2 is flipped)
# ---------------------------------------------------------------------------

def test_v1_default_preserved_for_failure_with_empty_source_ids(tmp_path, monkeypatch):
    """V1 default: live update_confidence_from_outcome does not decay via junction."""
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db, dispatches, sp_ids_failure, _ = _build_fixture_db(tmp_path)

    failure_dispatch = dispatches[0]  # D-shadow-F1, status='failure'
    result = update_confidence_from_outcome(
        db, failure_dispatch["dispatch_id"], "T1", "failure"
    )

    # V1 flag off → LIKE on "[]" finds nothing → 0 grounded
    assert result == {"boosted": 0, "decayed": 0}, (
        "V1 default must not decay patterns with empty source_dispatch_ids"
    )
    # Confidence unchanged
    assert _confidence(db, sp_ids_failure[0]) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Shadow with no junction table — falls back gracefully
# ---------------------------------------------------------------------------

def test_shadow_no_junction_reports_unavailable(tmp_path, monkeypatch):
    """When dispatch_pattern_offered is absent, shadow reports junction_available=False."""
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)

    db = tmp_path / "qi.db"
    conn = sqlite3.connect(str(db))
    # Schema without the junction table
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            pattern_data TEXT NOT NULL,
            confidence_score REAL DEFAULT 0.5,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT,
            first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_used DATETIME
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
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

    dispatches = [{"dispatch_id": "D-test", "status": "failure"}]
    report = shadow_grounding_compare(db, dispatches)

    assert not report["summary"]["junction_available"]
    assert report["summary"]["total_dispatches"] == 1
    assert report["summary"]["v2_only_grounded"] == 0


# ---------------------------------------------------------------------------
# Shadow with nonexistent DB — returns empty report, no exception
# ---------------------------------------------------------------------------

def test_shadow_missing_db(tmp_path):
    """shadow_grounding_compare returns an empty report when DB does not exist."""
    db = tmp_path / "missing.db"
    dispatches = [{"dispatch_id": "D-x", "status": "failure"}]
    report = shadow_grounding_compare(db, dispatches)

    assert report["summary"]["total_dispatches"] == 0
    assert report["dispatches"] == []
