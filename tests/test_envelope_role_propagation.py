"""test_envelope_role_propagation.py — spec.role must reach the envelope receipt.

Dispatch-20260801-w7: the envelope/provider-lane GOVERN path resolved the receipt
``role`` from ``dispatch_metadata`` ONLY and ignored ``spec.role``, so a dispatch
staged with a genuine role (e.g. ``system-architect``) still landed as
``identity_unresolved``. These tests pin the propagation contract:

1. A spec with ``role=X`` produces a receipt with ``role=X`` (two different roles).
2. A spec role wins over a ``dispatch_metadata`` row (the plan's own staged role
   is the authoritative source; the DB join is a fallback for writers that never
   staged a role).
3. Sentinel protection: an absent role (None) or the fake ``backend-developer``
   default NEVER lands verbatim in the receipt — it resolves to
   ``identity_unresolved`` or the DB row, exactly like ``dispatch_govern``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_envelope
from dispatch_envelope import EnvelopeSpec, _AdapterResult


def _make_spec(tmp_path: Path, *, role, dispatch_id="role-prop-001") -> EnvelopeSpec:
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "unified_reports").mkdir(parents=True)
    return EnvelopeSpec(
        dispatch_id=dispatch_id,
        terminal_id="T1",
        provider="codex",
        model="gpt-test",
        instruction="implement the feature",
        role=role,
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
    )


def _make_metadata_db(state_dir: Path, rows):
    """Create a minimal quality_intelligence.db with dispatch_metadata rows."""
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "quality_intelligence.db"))
    conn.execute(
        "CREATE TABLE dispatch_metadata ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " dispatch_id TEXT NOT NULL,"
        " project_id TEXT NOT NULL,"
        " role TEXT"
        ")"
    )
    for dispatch_id, project_id, role in rows:
        conn.execute(
            "INSERT INTO dispatch_metadata (dispatch_id, project_id, role) VALUES (?, ?, ?)",
            (dispatch_id, project_id, role),
        )
    conn.commit()
    conn.close()


def _run_govern(spec: EnvelopeSpec) -> dict:
    """Run the envelope GOVERN and return the emitted receipt line as a dict."""
    result = _AdapterResult(returncode=0, completion_text="all good", status="success")
    start = end = datetime.now(timezone.utc)
    _report_path, receipt_path = dispatch_envelope._govern(spec, result, start, end)
    assert receipt_path is not None and receipt_path.exists(), "receipt must be emitted"
    lines = [l for l in receipt_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines, "receipt ledger must not be empty"
    # The last line for this dispatch is the one GOVERN just wrote.
    receipt = json.loads(lines[-1])
    assert receipt["dispatch_id"] == spec.dispatch_id, "unexpected receipt line"
    return receipt


def test_envelope_spec_role_system_architect_propagates_to_receipt(
    tmp_path, monkeypatch,
):
    """A spec staged with role='system-architect' must land in the receipt verbatim."""
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)

    spec = _make_spec(tmp_path, role="system-architect")
    receipt = _run_govern(spec)

    assert receipt.get("role") == "system-architect", (
        f"spec.role did not reach the receipt; got role={receipt.get('role')!r}"
    )


def test_envelope_spec_role_second_role_propagates_to_receipt(
    tmp_path, monkeypatch,
):
    """Generality: a second distinct role also propagates (not a one-value fluke)."""
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)

    spec = _make_spec(tmp_path, role="security-reviewer", dispatch_id="role-prop-002")
    receipt = _run_govern(spec)

    assert receipt.get("role") == "security-reviewer", (
        f"spec.role did not reach the receipt; got role={receipt.get('role')!r}"
    )


def test_envelope_spec_role_wins_over_db_metadata(tmp_path, monkeypatch):
    """The plan's staged role is authoritative; a DB row must not override it."""
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    _make_metadata_db(tmp_path / "state", [("role-prop-003", "vnx-dev", "debugger")])

    spec = _make_spec(tmp_path, role="plan-reviewer", dispatch_id="role-prop-003")
    receipt = _run_govern(spec)

    assert receipt.get("role") == "plan-reviewer", (
        f"spec.role should win over dispatch_metadata; got role={receipt.get('role')!r}"
    )


def test_envelope_db_fallback_when_no_spec_role(tmp_path, monkeypatch):
    """No spec role -> the dispatch_metadata DB join supplies the role."""
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    _make_metadata_db(tmp_path / "state", [("role-prop-004", "vnx-dev", "debugger")])

    spec = _make_spec(tmp_path, role=None, dispatch_id="role-prop-004")
    receipt = _run_govern(spec)

    assert receipt.get("role") == "debugger", (
        f"DB fallback role did not propagate; got role={receipt.get('role')!r}"
    )


def test_envelope_no_role_never_stamps_backend_developer(tmp_path, monkeypatch):
    """Sentinel protection: role=None must NOT become the fake backend-developer."""
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)

    spec = _make_spec(tmp_path, role=None, dispatch_id="role-prop-005")
    receipt = _run_govern(spec)

    assert receipt.get("role") != "backend-developer", (
        "absent role silently stamped the fake backend-developer default"
    )
    assert receipt.get("role") == "identity_unresolved", (
        f"expected identity_unresolved, got role={receipt.get('role')!r}"
    )


def test_envelope_sentinel_spec_role_falls_through_to_db(tmp_path, monkeypatch):
    """A spec role of the literal fake default must fall through to the DB, not propagate."""
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    _make_metadata_db(tmp_path / "state", [("role-prop-006", "vnx-dev", "reviewer")])

    spec = _make_spec(tmp_path, role="backend-developer", dispatch_id="role-prop-006")
    receipt = _run_govern(spec)

    assert receipt.get("role") == "reviewer", (
        f"fake default should fall through to the DB row; got role={receipt.get('role')!r}"
    )
