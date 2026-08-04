"""test_dispatch_identity.py — tests for the dispatch_identity role resolver.

Receipt-quality track PR-1: role propagation from dispatch_metadata
(quality_intelligence.db) into the v2 receipt emit. FAIL-OPEN contract:
resolver errors return None, never raise.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import dispatch_identity
from dispatch_identity import (
    extract_role_from_instruction,
    normalize_role,
    resolve_dispatch_role,
)


def _make_db(state_dir: Path, rows):
    """Create a minimal quality_intelligence.db with dispatch_metadata rows."""
    db_path = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
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
    return db_path


def test_resolve_real_role_from_db(tmp_path):
    _make_db(tmp_path, [("disp-1", "vnx-dev", "debugger")])
    assert resolve_dispatch_role("disp-1", "vnx-dev", state_dir=tmp_path) == "debugger"


def test_resolve_latest_row_wins(tmp_path):
    # Composite key per ADR-007, ORDER BY id DESC LIMIT 1.
    _make_db(tmp_path, [("disp-1", "vnx-dev", "reviewer"), ("disp-1", "vnx-dev", "debugger")])
    assert resolve_dispatch_role("disp-1", "vnx-dev", state_dir=tmp_path) == "debugger"


def test_resolve_respects_project_id_composite_key(tmp_path):
    _make_db(tmp_path, [("disp-1", "other-project", "debugger")])
    assert resolve_dispatch_role("disp-1", "vnx-dev", state_dir=tmp_path) is None


def test_resolve_empty_sentinel_returns_none(tmp_path):
    # OI-981: the fake sentinel "" must not be propagated into the receipt trail.
    _make_db(tmp_path, [("disp-1", "vnx-dev", "")])
    assert resolve_dispatch_role("disp-1", "vnx-dev", state_dir=tmp_path) is None


def test_resolve_backend_developer_is_real_role(tmp_path):
    # OI-981: backend-developer is now a REAL role, not a sentinel. A deliberately
    # chosen backend-developer role must resolve normally — the key assertion of
    # OI-981: a chosen role is distinguishable from a failed resolution.
    _make_db(tmp_path, [("disp-1", "vnx-dev", "backend-developer")])
    assert resolve_dispatch_role("disp-1", "vnx-dev", state_dir=tmp_path) == "backend-developer"


def test_resolve_null_or_empty_role_returns_none(tmp_path):
    _make_db(tmp_path, [("disp-1", "vnx-dev", None), ("disp-2", "vnx-dev", "  ")])
    assert resolve_dispatch_role("disp-1", "vnx-dev", state_dir=tmp_path) is None
    assert resolve_dispatch_role("disp-2", "vnx-dev", state_dir=tmp_path) is None


def test_resolve_missing_row_returns_none(tmp_path):
    _make_db(tmp_path, [("disp-1", "vnx-dev", "debugger")])
    assert resolve_dispatch_role("disp-absent", "vnx-dev", state_dir=tmp_path) is None


def test_resolve_table_missing_fails_open(tmp_path):
    # DB exists but dispatch_metadata table does not → None, no raise.
    conn = sqlite3.connect(str(tmp_path / "quality_intelligence.db"))
    conn.execute("CREATE TABLE something_else (id INTEGER)")
    conn.commit()
    conn.close()
    assert resolve_dispatch_role("disp-1", "vnx-dev", state_dir=tmp_path) is None


def test_resolve_db_missing_fails_open(tmp_path, monkeypatch):
    # No resolvable DB anywhere → None, no raise.
    monkeypatch.setattr(dispatch_identity, "_resolve_db_path", lambda state_dir=None: None)
    assert resolve_dispatch_role("disp-1", "vnx-dev", state_dir=tmp_path) is None


def test_resolve_query_error_fails_open(tmp_path, monkeypatch):
    # A connect/query blow-up must degrade to None, never propagate.
    monkeypatch.setattr(
        dispatch_identity.sqlite3, "connect",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.Error("boom")),
    )
    assert resolve_dispatch_role("disp-1", "vnx-dev", state_dir=tmp_path) is None


def test_resolve_empty_identifiers_return_none(tmp_path):
    assert resolve_dispatch_role("", "vnx-dev", state_dir=tmp_path) is None
    assert resolve_dispatch_role("disp-1", "", state_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# PR-4 write-time helpers: normalize_role / extract_role_from_instruction
# ---------------------------------------------------------------------------

def test_normalize_role_strips_and_keeps_real_role():
    assert normalize_role("quality-engineer") == "quality-engineer"
    assert normalize_role("  debugger  ") == "debugger"


def test_normalize_role_fake_sentinel_returns_none():
    # OI-981: the fake sentinel "" must never be persisted.
    assert normalize_role("") is None


def test_normalize_role_backend_developer_is_real_role():
    # OI-981: backend-developer is now a REAL role, not a sentinel. A
    # deliberately chosen backend-developer role must be preserved.
    assert normalize_role("backend-developer") == "backend-developer"


def test_normalize_role_empty_or_none_returns_none():
    assert normalize_role(None) is None
    assert normalize_role("") is None
    assert normalize_role("   ") is None


def test_extract_role_from_instruction_header():
    assert extract_role_from_instruction("Role: debugger\n\nDo the thing") == "debugger"
    assert extract_role_from_instruction("# Dispatch\nRole: quality-engineer\nBody") == "quality-engineer"


def test_extract_role_from_instruction_absent_returns_none():
    assert extract_role_from_instruction("no header here") is None
    assert extract_role_from_instruction("") is None
    assert extract_role_from_instruction(None) is None


# ---------------------------------------------------------------------------
# OI-981: sentinel "" vs deliberately-chosen backend-developer are distinguishable
# ---------------------------------------------------------------------------


def test_oi981_backend_developer_is_real_role_via_resolve_effective(tmp_path):
    """OI-981 key assertion: a deliberately chosen backend-developer role is
    distinguishable from a failed role resolution (sentinel "").

    Before OI-981: backend-developer WAS the sentinel — both the chosen role
    and the "no role resolved" case shared the same string. After the fix, the
    sentinel is "" (empty), and backend-developer is a normal real role.
    """
    _make_db(tmp_path, [("disp-oi981", "vnx-dev", "backend-developer")])

    # Case 1: role="" (sentinel) → falls back to dispatch_metadata or identity_unresolved.
    # With no DB role for this dispatch: identity_unresolved.
    from dispatch_identity import resolve_effective_role
    assert resolve_effective_role("", "disp-oi981-missing", "vnx-dev", state_dir=tmp_path) == "identity_unresolved"

    # Case 2: role="backend-developer" (real chosen role) → returns backend-developer.
    assert resolve_effective_role("backend-developer", "disp-oi981-any", "vnx-dev", state_dir=tmp_path) == "backend-developer"

    # The two outcomes are different — the sentinel and real role are now structurally distinct.
    # Before OI-981, both would have returned "backend-developer" (indistinguishable).


def test_oi981_sentinel_distinct_in_normalize_role():
    """OI-981: normalize_role distinguishes the sentinel "" from the real role
    backend-developer."""
    # Sentinel "" → None (no real role to persist)
    assert normalize_role("") is None
    # Real role "backend-developer" → preserved as-is
    assert normalize_role("backend-developer") == "backend-developer"
    # Also test through strip: whitespace-sentinel
    assert normalize_role("  ") is None
