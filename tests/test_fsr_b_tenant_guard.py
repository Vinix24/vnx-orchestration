"""Behavioral tests for PR-B: multi-tenant guard in build_t0_state (R3.2).

ADR-007: the canonical `tracks` / `track_open_items` tables are multi-tenant,
keyed by composite PRIMARY KEY (track_id, project_id). build_t0_state must NEVER
read canonical track rows without a `WHERE project_id = ?` predicate. On an
unavailable identity it must emit a documented DEGRADED fallback (no rows +
flag) rather than merge rows across tenants (codex F11 / opus #11).

Acceptance (dispatch 20260614-fsr-b-tenant-guard):
  (a) a DB with two tenants' tracks → only the resolved tenant's rows return,
      including the same-track_id-no-overwrite case;
  (b) identity unavailable → degraded flag set AND no canonical rows returned.

Discipline: temp-DB ONLY. Every test pins VNX_DATA_DIR_EXPLICIT=1 + a tmp
VNX_DATA_DIR; the live ~/.vnx-data is never touched.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"
_MIGRATIONS = _REPO_ROOT / "schemas" / "migrations"

for _p in (str(_SCRIPTS_DIR), str(_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_t0_state as bts  # noqa: E402
import schema_migration  # noqa: E402

_TENANT_A = "vnx-dev"
_TENANT_B = "seocrawler-v2"


# ---------------------------------------------------------------------------
# Isolation + fixtures
# ---------------------------------------------------------------------------

def _pin_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin VNX_DATA_DIR_EXPLICIT=1 + a tmp VNX_DATA_DIR; return the state dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    # Block the central-store fallback so the builder resolves the tmp store, not the live
    # ~/.vnx-data/<project>/state (audit #13 — the canary otherwise reads production tracks).
    monkeypatch.setattr(bts, "resolve_central_data_dir", None, raising=False)
    return state_dir


def _seed_two_tenants(conn: sqlite3.Connection) -> None:
    """Insert two tenants' tracks + open-items, sharing track_id 'shared-1'.

    Each tenant's 'shared-1' carries a distinct title so a cross-tenant merge /
    overwrite is observable: the resolved tenant must keep its own title.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    rows = [
        ("shared-1", _TENANT_A, "tenant-A shared", 0),
        ("a-only", _TENANT_A, "tenant-A only", 1),
        ("shared-1", _TENANT_B, "tenant-B shared", 0),
        ("b-only", _TENANT_B, "tenant-B only", 1),
    ]
    for track_id, project_id, title, sort_order in rows:
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, phase, sort_order) "
            "VALUES (?, ?, ?, 'queued', ?)",
            (track_id, project_id, title, sort_order),
        )
    oi_rows = [
        ("shared-1", _TENANT_A, "oi-A", "blocks", "manual"),
        ("shared-1", _TENANT_B, "oi-B", "blocks", "manual"),
    ]
    for track_id, project_id, oi_id, link_type, link_source in oi_rows:
        conn.execute(
            "INSERT INTO track_open_items "
            "(track_id, project_id, oi_id, link_type, link_source) VALUES (?, ?, ?, ?, ?)",
            (track_id, project_id, oi_id, link_type, link_source),
        )
    conn.commit()


def _make_v24_db(state_dir: Path) -> None:
    """Build a clean v24 runtime_coordination.db (composite-PK tracks) + seed it."""
    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2',
            pr_ref TEXT, gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
            bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, event_type TEXT, entity_type TEXT,
            entity_id TEXT, from_state TEXT, to_state TEXT,
            actor TEXT, reason TEXT, metadata_json TEXT,
            occurred_at TEXT, project_id TEXT
        )
        """
    )
    conn.commit()
    for version, filename in [(22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    _seed_two_tenants(conn)
    conn.close()


# ---------------------------------------------------------------------------
# (a) Direct reader: tenant isolation — only the resolved tenant's rows
# ---------------------------------------------------------------------------

def test_reader_returns_only_resolved_tenant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _make_v24_db(state_dir)

    result = bts._build_tracks_from_db(state_dir, _TENANT_A)

    assert result["available"] is True
    assert result["tenant_unavailable"] is False
    assert result["health"] == "healthy"
    assert result["project_id"] == _TENANT_A

    ids = {t["track_id"] for t in result["tracks"]}
    assert ids == {"shared-1", "a-only"}
    assert "b-only" not in ids

    shared = next(t for t in result["tracks"] if t["track_id"] == "shared-1")
    assert shared["title"] == "tenant-A shared"  # no cross-tenant overwrite

    oi_ids = {oi["oi_id"] for oi in result["open_items"]}
    assert oi_ids == {"oi-A"}  # track_open_items also tenant-scoped


def test_reader_scopes_to_other_tenant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _make_v24_db(state_dir)

    result = bts._build_tracks_from_db(state_dir, _TENANT_B)

    ids = {t["track_id"] for t in result["tracks"]}
    assert ids == {"shared-1", "b-only"}
    assert "a-only" not in ids
    shared = next(t for t in result["tracks"] if t["track_id"] == "shared-1")
    assert shared["title"] == "tenant-B shared"
    assert {oi["oi_id"] for oi in result["open_items"]} == {"oi-B"}


# ---------------------------------------------------------------------------
# (b) Direct reader: unavailable identity → degraded, no rows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_pid", ["", "   ", None])
def test_reader_unavailable_identity_is_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_pid
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _make_v24_db(state_dir)  # DB HAS two tenants' rows — none may leak

    result = bts._build_tracks_from_db(state_dir, bad_pid)

    assert result["available"] is False
    assert result["tenant_unavailable"] is True
    assert result["health"] == "degraded"
    assert result["tracks"] == []
    assert result["open_items"] == []
    assert result["project_id"] is None


def test_reader_premigration_db_is_healthy_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB without the tracks table is a healthy empty result, not a tenant fault."""
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    db_path = state_dir / "runtime_coordination.db"
    sqlite3.connect(str(db_path)).close()  # empty DB, no tracks table

    result = bts._build_tracks_from_db(state_dir, _TENANT_A)

    assert result["available"] is True
    assert result["tenant_unavailable"] is False
    assert result["reason"] == "premigration"
    assert result["tracks"] == []


def test_reader_absent_db_is_healthy_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    result = bts._build_tracks_from_db(state_dir, _TENANT_A)
    assert result["available"] is True
    assert result["tracks"] == []


# ---------------------------------------------------------------------------
# Through build_t0_state: wiring + output flag (acceptance a & b)
# ---------------------------------------------------------------------------

def test_build_t0_state_canonical_tracks_tenant_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _make_v24_db(state_dir)
    # Marker resolves project identity to tenant A (ancestor of state_dir).
    (tmp_path / ".vnx-project-id").write_text(_TENANT_A + "\n", encoding="utf-8")
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)
    ct = state["canonical_tracks"]

    assert ct["available"] is True
    assert ct["tenant_unavailable"] is False
    ids = {t["track_id"] for t in ct["tracks"]}
    assert "a-only" in ids
    assert "b-only" not in ids  # no cross-tenant leak through the full builder
    shared = next(t for t in ct["tracks"] if t["track_id"] == "shared-1")
    assert shared["title"] == "tenant-A shared"


def test_build_t0_state_unavailable_identity_degraded_no_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _make_v24_db(state_dir)  # two tenants present; none may surface
    # No .vnx-project-id marker anywhere up-tree, VNX_PROJECT_ID unset → unresolved.
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)
    ct = state["canonical_tracks"]

    assert ct["tenant_unavailable"] is True
    assert ct["available"] is False
    assert ct["health"] == "degraded"
    assert ct["tracks"] == []
    assert ct["open_items"] == []
