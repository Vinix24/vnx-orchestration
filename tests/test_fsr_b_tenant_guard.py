"""Behavioral tests for PR-B: multi-tenant guard in build_t0_state (R3.2).

ADR-007: the canonical `tracks` / `track_open_items` tables are multi-tenant,
keyed by composite PRIMARY KEY (track_id, project_id). build_t0_state must NEVER
read canonical track rows without a `WHERE project_id = ?` predicate. On an
unavailable identity it must emit a documented DEGRADED fallback (no rows +
flag) rather than merge rows across tenants (codex F11 / opus #11).

D8 (fabric-state-herstel) removed the canonical_tracks projection: a T0 reads
tracks through planning_cli.py. The tenant-scoped reader is gone; what stays
here is the proof that neither tenant's rows reach the state document, and the
tracks-store isolation guard.

Discipline: temp-DB ONLY. Every test pins VNX_DATA_DIR_EXPLICIT=1 + a tmp
VNX_DATA_DIR; the live ~/.vnx-data is never touched.
"""

from __future__ import annotations

import json
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
# Builder-layer guard: _resolve_tracks_store must never escape to the real
# central store during a pinned-isolation test run (audit #13 regression).
# ---------------------------------------------------------------------------

def test_resolve_tracks_store_escape_is_neutralized_by_pinned_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """audit #13: prove BOTH halves so the test can actually fail.

    ``_resolve_tracks_store`` returns the central store only when
    ``resolve_central_data_dir`` is not None AND that store has a
    ``runtime_coordination.db``. A test that just calls ``_pin_isolation``
    (which sets ``resolve_central_data_dir`` to None) is a tautology — it passes
    regardless of the guard. So first demonstrate a *live* escape path against a
    fake central store, then assert the pinned isolation neutralizes it.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)

    # A fake central store WITH a runtime_coordination.db — the escape target.
    fake_central = tmp_path / "central"
    fake_central_state = fake_central / "state"
    fake_central_state.mkdir(parents=True)
    (fake_central_state / "runtime_coordination.db").write_text("")

    # 1) Escape path is REAL: when central resolves to a populated store,
    #    _resolve_tracks_store prefers it over state_dir. If this ever stops
    #    holding, the guard-half below would pass vacuously — hence assert it.
    monkeypatch.setattr(
        bts, "resolve_central_data_dir", lambda pid: fake_central, raising=False
    )
    assert bts._resolve_tracks_store(state_dir, _TENANT_A) == fake_central_state

    # 2) Pinned isolation NEUTRALIZES the escape: _pin_isolation sets
    #    resolve_central_data_dir to None, so the resolver stays on the tmp
    #    state_dir even though a populated central store exists on disk.
    pinned_state = _pin_isolation(tmp_path, monkeypatch)
    assert bts._resolve_tracks_store(pinned_state, _TENANT_A) == pinned_state


# ---------------------------------------------------------------------------
# Through build_t0_state: the canonical projection is gone (D8), and with it
# the only tenant-scoped row reader. Neither tenant's rows may reach the state.
# ---------------------------------------------------------------------------

def test_build_t0_state_carries_no_track_rows_of_either_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _make_v24_db(state_dir)
    (tmp_path / ".vnx-project-id").write_text(_TENANT_A + "\n", encoding="utf-8")
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)
    dumped = json.dumps(state, default=str)

    assert "canonical_tracks" not in state
    assert "tenant-B" not in dumped  # no cross-tenant leak through the full builder
    assert "tenant-A" not in dumped  # the projection itself is gone


def test_build_t0_state_unresolved_identity_carries_no_track_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _make_v24_db(state_dir)  # two tenants present; none may surface
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)
    dumped = json.dumps(state, default=str)

    assert "canonical_tracks" not in state
    assert "tenant-A" not in dumped
    assert "tenant-B" not in dumped
