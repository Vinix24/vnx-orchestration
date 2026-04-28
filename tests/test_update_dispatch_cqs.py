#!/usr/bin/env python3
"""Tests for OI-1175 fix: update_dispatch_cqs.py must preserve quality_advisory,
target_open_items, open_items_created, and open_items_resolved from DB row."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))


def _init_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE dispatch_metadata (
            dispatch_id TEXT PRIMARY KEY,
            outcome_status TEXT,
            outcome_report_path TEXT,
            role TEXT,
            gate TEXT,
            pr_id TEXT,
            open_items_created INTEGER DEFAULT 0,
            open_items_resolved INTEGER DEFAULT 0,
            target_open_items TEXT,
            quality_advisory_json TEXT,
            cqs REAL,
            normalized_status TEXT,
            cqs_components TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE session_analytics (
            dispatch_id TEXT PRIMARY KEY,
            total_input_tokens INTEGER,
            total_output_tokens INTEGER,
            error_count INTEGER,
            total_messages INTEGER,
            tool_calls_total INTEGER
        )
    """)
    conn.commit()
    conn.close()


def _insert_row(db_path: Path, **kwargs) -> None:
    conn = sqlite3.connect(str(db_path))
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join(["?"] * len(kwargs))
    conn.execute(
        f"INSERT INTO dispatch_metadata ({cols}) VALUES ({placeholders})",
        list(kwargs.values()),
    )
    conn.commit()
    conn.close()


def _fetch_row(db_path: Path, dispatch_id: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatch_metadata WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def _run_updater(db_path: Path, dispatch_id: str, monkeypatch) -> int:
    """Run main() from update_dispatch_cqs with the given DB."""
    monkeypatch.setenv("VNX_STATE_DIR", str(db_path.parent))
    import importlib
    import sys as _sys

    # Patch ensure_env to return our test state dir
    import vnx_paths
    monkeypatch.setattr(
        vnx_paths, "ensure_env", lambda: {"VNX_STATE_DIR": str(db_path.parent)}
    )

    # Reload module so patched paths take effect
    if "update_dispatch_cqs" in _sys.modules:
        del _sys.modules["update_dispatch_cqs"]

    _sys.argv = ["update_dispatch_cqs.py", "--dispatch-id", dispatch_id]
    spec = importlib.util.spec_from_file_location(
        "update_dispatch_cqs", SCRIPTS_DIR / "update_dispatch_cqs.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main()


# ── Tests ──────────────────────────────────────────────────────────────────


def test_open_items_fields_used_in_cqs(tmp_path, monkeypatch):
    """CQS should reflect open_items_resolved bonus when field is present in DB."""
    db_path = tmp_path / "quality_intelligence.db"
    _init_db(db_path)

    dispatch_id = "test-oi-1175-a"
    _insert_row(
        db_path,
        dispatch_id=dispatch_id,
        outcome_status="task_complete",
        outcome_report_path="/report.md",
        role="backend-developer",
        gate="f99",
        pr_id=None,
        open_items_created=0,
        open_items_resolved=2,
        target_open_items=None,
        quality_advisory_json=None,
    )

    _run_updater(db_path, dispatch_id, monkeypatch)

    row = _fetch_row(db_path, dispatch_id)
    assert row["cqs"] is not None

    components = json.loads(row["cqs_components"])
    # Resolved 2, created 0 → oi_delta = 50 + 30 = 80
    assert components["oi_delta"] == pytest.approx(80.0)


def test_quality_advisory_used_in_cqs(tmp_path, monkeypatch):
    """CQS t0_advisory component should reflect stored quality_advisory_json."""
    db_path = tmp_path / "quality_intelligence.db"
    _init_db(db_path)

    advisory = {
        "t0_recommendation": {"decision": "approve"},
        "summary": {"risk_score": 0},
    }

    dispatch_id = "test-oi-1175-b"
    _insert_row(
        db_path,
        dispatch_id=dispatch_id,
        outcome_status="task_complete",
        outcome_report_path="/report.md",
        role="backend-developer",
        gate="f99",
        pr_id=None,
        open_items_created=0,
        open_items_resolved=0,
        target_open_items=None,
        quality_advisory_json=json.dumps(advisory),
    )

    _run_updater(db_path, dispatch_id, monkeypatch)

    row = _fetch_row(db_path, dispatch_id)
    assert row["cqs"] is not None

    components = json.loads(row["cqs_components"])
    # approve + risk_score=0 → t0_advisory = 100*0.7 + 100*0.3 = 100
    assert components["t0_advisory"] == pytest.approx(100.0)


def test_stripped_payload_previously_gave_neutral_advisory(tmp_path, monkeypatch):
    """Without the fix, quality_advisory=None gives neutral 50.0 for t0_advisory.
    With the fix, a stored advisory of 'hold' should give 0.0."""
    db_path = tmp_path / "quality_intelligence.db"
    _init_db(db_path)

    hold_advisory = {
        "t0_recommendation": {"decision": "hold"},
        "summary": {"risk_score": 100},
    }

    dispatch_id = "test-oi-1175-c"
    _insert_row(
        db_path,
        dispatch_id=dispatch_id,
        outcome_status="task_complete",
        outcome_report_path="/report.md",
        role="backend-developer",
        gate="f99",
        pr_id=None,
        open_items_created=0,
        open_items_resolved=0,
        target_open_items=None,
        quality_advisory_json=json.dumps(hold_advisory),
    )

    _run_updater(db_path, dispatch_id, monkeypatch)

    row = _fetch_row(db_path, dispatch_id)
    components = json.loads(row["cqs_components"])
    # hold + risk_score=100 → t0_advisory = 0*0.7 + 0*0.3 = 0.0
    assert components["t0_advisory"] == pytest.approx(0.0)


def test_target_open_items_penalty_preserved(tmp_path, monkeypatch):
    """Unresolved target_open_items should penalise oi_delta."""
    db_path = tmp_path / "quality_intelligence.db"
    _init_db(db_path)

    dispatch_id = "test-oi-1175-d"
    _insert_row(
        db_path,
        dispatch_id=dispatch_id,
        outcome_status="task_complete",
        outcome_report_path="/report.md",
        role="backend-developer",
        gate="f99",
        pr_id=None,
        open_items_created=0,
        open_items_resolved=0,
        target_open_items=json.dumps(["OI-001", "OI-002"]),
        quality_advisory_json=None,
    )

    _run_updater(db_path, dispatch_id, monkeypatch)

    row = _fetch_row(db_path, dispatch_id)
    components = json.loads(row["cqs_components"])
    # targeted=2, resolved=0 → score=50 - min(20,2*20)=50-20=30
    assert components["oi_delta"] == pytest.approx(30.0)


def test_missing_columns_dont_crash(tmp_path, monkeypatch):
    """Older DB without quality_advisory_json column must not raise IndexError."""
    db_path = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE dispatch_metadata (
            dispatch_id TEXT PRIMARY KEY,
            outcome_status TEXT,
            outcome_report_path TEXT,
            role TEXT,
            gate TEXT,
            pr_id TEXT,
            cqs REAL,
            normalized_status TEXT,
            cqs_components TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE session_analytics (
            dispatch_id TEXT PRIMARY KEY,
            total_input_tokens INTEGER,
            total_output_tokens INTEGER,
            error_count INTEGER,
            total_messages INTEGER,
            tool_calls_total INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO dispatch_metadata (dispatch_id, outcome_status, role) VALUES (?, ?, ?)",
        ("test-legacy", "task_complete", "backend-developer"),
    )
    conn.commit()
    conn.close()

    rc = _run_updater(db_path, "test-legacy", monkeypatch)
    assert rc == 0
    row = _fetch_row(db_path, "test-legacy")
    assert row["cqs"] is not None
