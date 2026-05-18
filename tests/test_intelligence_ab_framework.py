"""Tests for the A/B random-skip framework (V5).

Coverage:
- Default (VNX_INTEL_AB_TEST=0): always 'treatment', no injection skip
- Enabled (VNX_INTEL_AB_TEST=1): ~10% control arm via mocked random
- record_injection_audit stores ab_arm correctly when column exists
- weekly_ab_lift returns matched-pairs structure
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_LIB_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# _ab_arm tests
# ---------------------------------------------------------------------------

def test_ab_arm_default_returns_treatment(monkeypatch):
    monkeypatch.delenv("VNX_INTEL_AB_TEST", raising=False)
    from intelligence_selector import _ab_arm
    for _ in range(50):
        assert _ab_arm() == "treatment"


def test_ab_arm_disabled_explicit_zero_returns_treatment(monkeypatch):
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "0")
    from intelligence_selector import _ab_arm
    for _ in range(20):
        assert _ab_arm() == "treatment"


def test_ab_arm_enabled_returns_control_when_rng_below_threshold(monkeypatch):
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    from intelligence_selector import _ab_arm
    with patch("intelligence_selector.random.random", return_value=0.05):
        assert _ab_arm() == "control"


def test_ab_arm_enabled_returns_treatment_when_rng_above_threshold(monkeypatch):
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    from intelligence_selector import _ab_arm
    with patch("intelligence_selector.random.random", return_value=0.50):
        assert _ab_arm() == "treatment"


def test_ab_arm_enabled_statistical_distribution(monkeypatch):
    """10% control arm is approximate; verify 0-25% range over 500 trials."""
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    from intelligence_selector import _ab_arm
    control_count = sum(1 for _ in range(500) if _ab_arm() == "control")
    assert 0 <= control_count <= 125, f"Control arm out of expected range: {control_count}/500"


# ---------------------------------------------------------------------------
# InjectionResult ab_arm field
# ---------------------------------------------------------------------------

def test_injection_result_default_ab_arm():
    from intelligence_sources._models import InjectionResult
    result = InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-01-01T00:00:00Z",
        items=[],
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id="test-dispatch-001",
    )
    assert result.ab_arm == "treatment"


def test_injection_result_control_ab_arm():
    from intelligence_sources._models import InjectionResult
    result = InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-01-01T00:00:00Z",
        items=[],
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id="test-dispatch-002",
        ab_arm="control",
    )
    assert result.ab_arm == "control"


# ---------------------------------------------------------------------------
# record_injection_audit stores ab_arm
# ---------------------------------------------------------------------------

def _make_injection_db(tmp_path: Path, with_ab_arm: bool = True) -> Path:
    db_path = tmp_path / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    cols = """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        injection_id TEXT NOT NULL UNIQUE,
        dispatch_id TEXT NOT NULL,
        injection_point TEXT NOT NULL,
        task_class TEXT NOT NULL,
        items_injected INTEGER NOT NULL DEFAULT 0,
        items_suppressed INTEGER NOT NULL DEFAULT 0,
        payload_chars INTEGER NOT NULL DEFAULT 0,
        items_json TEXT NOT NULL DEFAULT '[]',
        suppressed_json TEXT NOT NULL DEFAULT '[]',
        injected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    """
    if with_ab_arm:
        cols += ", ab_arm TEXT DEFAULT 'treatment'"
    conn.execute(f"CREATE TABLE intelligence_injections ({cols})")
    conn.commit()
    conn.close()
    return db_path


def _make_fake_get_conn(db_path: Path):
    """Return a contextmanager that opens db_path instead of the real coord DB."""
    import contextlib

    @contextlib.contextmanager
    def _fake_get_conn(state_dir):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    return _fake_get_conn


def test_record_injection_audit_stores_ab_arm(tmp_path):
    db_path = _make_injection_db(tmp_path, with_ab_arm=True)

    from intelligence_sources._models import InjectionResult
    result = InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-01-01T00:00:00Z",
        items=[],
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id="ab-test-dispatch-003",
        ab_arm="control",
    )

    # runtime_coordination.get_connection is lazily imported inside the function;
    # patch it at the source module to redirect to the test DB.
    with patch("runtime_coordination.get_connection", _make_fake_get_conn(db_path)):
        from intelligence_sources._recording import record_injection_audit
        record_injection_audit(result, state_dir=tmp_path, project_id=None)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT ab_arm FROM intelligence_injections WHERE dispatch_id = ?",
        ("ab-test-dispatch-003",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["ab_arm"] == "control"


def test_record_injection_audit_without_ab_arm_column(tmp_path):
    """Degrades gracefully when ab_arm column does not yet exist."""
    db_path = _make_injection_db(tmp_path, with_ab_arm=False)

    from intelligence_sources._models import InjectionResult
    result = InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-01-01T00:00:00Z",
        items=[],
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id="ab-test-dispatch-004",
        ab_arm="control",
    )

    with patch("runtime_coordination.get_connection", _make_fake_get_conn(db_path)):
        from intelligence_sources._recording import record_injection_audit
        record_injection_audit(result, state_dir=tmp_path, project_id=None)

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT dispatch_id FROM intelligence_injections WHERE dispatch_id = ?",
        ("ab-test-dispatch-004",),
    ).fetchone()
    conn.close()
    assert row is not None  # row written without ab_arm column


# ---------------------------------------------------------------------------
# weekly_ab_lift matched-pairs structure
# ---------------------------------------------------------------------------

def _make_report_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE intelligence_injections (
            id INTEGER PRIMARY KEY,
            dispatch_id TEXT NOT NULL,
            injection_point TEXT NOT NULL DEFAULT 'dispatch_create',
            task_class TEXT NOT NULL DEFAULT 'coding_interactive',
            ab_arm TEXT DEFAULT 'treatment',
            injected_at TEXT NOT NULL
        );
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY,
            dispatch_id TEXT NOT NULL UNIQUE,
            role TEXT,
            outcome_status TEXT
        );
    """)
    # Insert treatment rows (success)
    for i in range(10):
        did = f"treat-{i}"
        conn.execute(
            "INSERT INTO intelligence_injections (dispatch_id, task_class, ab_arm, injected_at) VALUES (?, ?, 'treatment', datetime('now', '-1 days'))",
            (did, "coding_interactive"),
        )
        conn.execute(
            "INSERT INTO dispatch_metadata (dispatch_id, role, outcome_status) VALUES (?, 'backend-developer', 'success')",
            (did,),
        )
    # Insert control rows (lower success rate)
    for i in range(10):
        did = f"ctrl-{i}"
        status = "success" if i < 5 else "failure"
        conn.execute(
            "INSERT INTO intelligence_injections (dispatch_id, task_class, ab_arm, injected_at) VALUES (?, ?, 'control', datetime('now', '-1 days'))",
            (did, "coding_interactive"),
        )
        conn.execute(
            "INSERT INTO dispatch_metadata (dispatch_id, role, outcome_status) VALUES (?, 'backend-developer', ?)",
            (did, status),
        )
    conn.commit()
    conn.close()
    return db_path


def test_weekly_ab_lift_returns_matched_pairs(tmp_path):
    db_path = _make_report_db(tmp_path)
    from intelligence_ab_report import weekly_ab_lift, _compute_lift

    raw = weekly_ab_lift(db_path, days=7)
    assert len(raw) >= 2  # at least treatment + control rows

    results = _compute_lift(raw)
    assert len(results) >= 1

    matched = [r for r in results if r["lift"] is not None]
    assert len(matched) >= 1, "Expected at least one matched pair"
    # Treatment (100%) vs control (50%) → lift = +0.5
    assert matched[0]["lift"] is not None
    assert matched[0]["treatment_rate"] > matched[0]["control_rate"]


def test_weekly_ab_lift_empty_db(tmp_path):
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE intelligence_injections (
            id INTEGER PRIMARY KEY, dispatch_id TEXT, injection_point TEXT,
            task_class TEXT, ab_arm TEXT, injected_at TEXT
        );
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY, dispatch_id TEXT, role TEXT, outcome_status TEXT
        );
    """)
    conn.close()

    from intelligence_ab_report import weekly_ab_lift, _compute_lift
    raw = weekly_ab_lift(db_path, days=30)
    assert raw == []
    results = _compute_lift(raw)
    assert results == []
