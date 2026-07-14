#!/usr/bin/env python3
"""Tests for the human_gate_queue surfacing in build_t0_state.py.

Track `kickoff-human-gate-queue`: deliverables/proposals in state=proposed are
un-promoted and NOT dispatchable until an operator runs `vnx deliverable
promote`. They were invisible at kickoff. `_build_human_gate_queue` and the
`human_gate_queue` key in `build_t0_state()`'s output surface them so the
operator sees the human-gate queue on startup.

Read-only, additive: this only surfaces the queue and must never touch the
deliverable/promote flow itself.

Discipline: temp-DB ONLY. Every test pins VNX_DATA_DIR_EXPLICIT=1 + a tmp
VNX_DATA_DIR, and blocks the central-store fallback so the builder resolves
the tmp store rather than a live ~/.vnx-data/<project>/state.
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

for _p in (str(_SCRIPTS_DIR), str(_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_t0_state as bts  # noqa: E402

_PROJECT_ID = "vnx-dev"


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
    # Block the central-store fallback so the builder resolves the tmp store,
    # not the live ~/.vnx-data/<project>/state.
    monkeypatch.setattr(bts, "resolve_central_data_dir", None, raising=False)
    return state_dir


def _make_dispatches_db(state_dir: Path) -> Path:
    """Minimal runtime_coordination.db with just the dispatches table."""
    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
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
    conn.commit()
    conn.close()
    return db_path


def _insert_dispatch(
    db_path: Path,
    *,
    dispatch_id: str,
    project_id: str,
    state: str,
    track: str | None = None,
    title: str | None = None,
    created_at: str = "2026-07-14T09:00:00.000000Z",
) -> None:
    metadata = json.dumps({"title": title, "deliverable": True}) if title else "{}"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO dispatches "
            "(dispatch_id, project_id, state, track, metadata_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dispatch_id, project_id, state, track, metadata, created_at, created_at),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Direct reader: _build_human_gate_queue
# ---------------------------------------------------------------------------

def test_reader_lists_proposed_deliverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    db_path = _make_dispatches_db(state_dir)
    _insert_dispatch(
        db_path,
        dispatch_id="dlv-bead16844a0a",
        project_id=_PROJECT_ID,
        state="proposed",
        track="kickoff-human-gate-queue",
        title="Ship the human gate queue",
    )

    queue = bts._build_human_gate_queue(state_dir, _PROJECT_ID)

    assert len(queue) == 1
    item = queue[0]
    assert item["id"] == "dlv-bead16844a0a"
    assert item["title"] == "Ship the human gate queue"
    assert item["track"] == "kickoff-human-gate-queue"
    assert item["created_at"] == "2026-07-14T09:00:00.000000Z"


def test_reader_excludes_non_proposed_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    db_path = _make_dispatches_db(state_dir)
    _insert_dispatch(
        db_path, dispatch_id="dlv-ready-1", project_id=_PROJECT_ID,
        state="ready", track="t", title="Already promoted",
    )
    _insert_dispatch(
        db_path, dispatch_id="d-queued-1", project_id=_PROJECT_ID,
        state="queued", track="t", title=None,
    )

    queue = bts._build_human_gate_queue(state_dir, _PROJECT_ID)
    assert queue == []


def test_reader_scopes_to_project_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    db_path = _make_dispatches_db(state_dir)
    _insert_dispatch(
        db_path, dispatch_id="dlv-a", project_id="tenant-a",
        state="proposed", track="t", title="Tenant A deliverable",
    )
    _insert_dispatch(
        db_path, dispatch_id="dlv-b", project_id="tenant-b",
        state="proposed", track="t", title="Tenant B deliverable",
    )

    queue_a = bts._build_human_gate_queue(state_dir, "tenant-a")
    assert {item["id"] for item in queue_a} == {"dlv-a"}

    queue_b = bts._build_human_gate_queue(state_dir, "tenant-b")
    assert {item["id"] for item in queue_b} == {"dlv-b"}


def test_reader_no_proposed_items_is_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _make_dispatches_db(state_dir)  # empty table, no rows at all

    queue = bts._build_human_gate_queue(state_dir, _PROJECT_ID)
    assert queue == []


def test_reader_absent_db_is_empty_list_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    # No runtime_coordination.db created at all.
    queue = bts._build_human_gate_queue(state_dir, _PROJECT_ID)
    assert queue == []


def test_reader_premigration_db_is_empty_list_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB without the dispatches table degrades to [], not an exception."""
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    db_path = state_dir / "runtime_coordination.db"
    sqlite3.connect(str(db_path)).close()  # empty DB, no dispatches table

    queue = bts._build_human_gate_queue(state_dir, _PROJECT_ID)
    assert queue == []


@pytest.mark.parametrize("bad_pid", ["", "   ", None])
def test_reader_unavailable_identity_is_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_pid
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    db_path = _make_dispatches_db(state_dir)
    _insert_dispatch(
        db_path, dispatch_id="dlv-x", project_id=_PROJECT_ID,
        state="proposed", track="t", title="x",
    )

    queue = bts._build_human_gate_queue(state_dir, bad_pid)
    assert queue == []


# ---------------------------------------------------------------------------
# Through build_t0_state: wiring + additive key
# ---------------------------------------------------------------------------

def test_build_t0_state_surfaces_proposed_deliverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    db_path = _make_dispatches_db(state_dir)
    _insert_dispatch(
        db_path,
        dispatch_id="dlv-bead16844a0a",
        project_id=_PROJECT_ID,
        state="proposed",
        track="kickoff-human-gate-queue",
        title="Ship the human gate queue",
    )
    # Marker resolves project identity to _PROJECT_ID (ancestor of state_dir).
    (tmp_path / ".vnx-project-id").write_text(_PROJECT_ID + "\n", encoding="utf-8")
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert "human_gate_queue" in state
    ids = {item["id"] for item in state["human_gate_queue"]}
    assert "dlv-bead16844a0a" in ids
    item = next(i for i in state["human_gate_queue"] if i["id"] == "dlv-bead16844a0a")
    assert item["title"] == "Ship the human gate queue"
    assert item["track"] == "kickoff-human-gate-queue"


def test_build_t0_state_empty_human_gate_queue_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _make_dispatches_db(state_dir)  # no proposed rows
    (tmp_path / ".vnx-project-id").write_text(_PROJECT_ID + "\n", encoding="utf-8")
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert state["human_gate_queue"] == []


def test_build_t0_state_preserves_existing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Additive-only: human_gate_queue must not disturb canonical_tracks/etc."""
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _make_dispatches_db(state_dir)
    (tmp_path / ".vnx-project-id").write_text(_PROJECT_ID + "\n", encoding="utf-8")
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert "canonical_tracks" in state
    assert "tracks" in state
    assert "human_gate_queue" in state
