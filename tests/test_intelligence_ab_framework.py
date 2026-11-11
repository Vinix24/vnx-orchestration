"""Tests for the A/B random-skip framework (V5).

Coverage:
- record_injection_audit stores ab_arm correctly when column exists
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
# InjectionResult ab_arm field
# ---------------------------------------------------------------------------

def test_injection_result_default_ab_arm_is_not_assigned():
    from intelligence_sources._models import InjectionResult
    result = InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-01-01T00:00:00Z",
        items=[],
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id="test-dispatch-001",
    )
    assert result.ab_arm is None and result.ab_window is None


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
    def _fake_get_conn(state_dir, *, timeout=10.0):
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
