"""test_dispatch_door_row.py — OI-847: the door writes a dispatches row.

Root cause under test: the single-entry dispatch door (dispatch_cli.run_dispatch)
never INSERTed a row into dispatches (runtime_coordination.db) — only the
deliverable layer (planning_cli.py, `dlv-` ids) did. That one missing write
starved three consumers:

1. ``_persist_track_id`` (UPDATE-only) — its UPDATE hit zero rows (symptom 1);
2. ``reconcile_all_dispatch_outcomes`` — empty dispatch-id population, so
   "nothing to reconcile" looked identical to "everything reconciled";
3. TL-D2 ``receipt_provenance._link_pr_to_track`` — no track_id row to read,
   so tracks.pr_ref auto-propagation on merge never fired.

Every test in this file is RED on origin/main (no row is ever created there)
and GREEN on the fix branch. Tests run against a throwaway DB under tmp_path —
never the live central store.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_cli import _persist_track_id, load_spec, run_dispatch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_coordination_db(state_dir: Path, *, tracks: "dict[str, str] | None" = None) -> Path:
    """Minimal runtime_coordination.db mirroring the v10 dispatches schema.

    Composite UNIQUE(dispatch_id, project_id) per ADR-007; created_at /
    updated_at present so the door's row carries timestamps the outcome
    classifier's age computation can parse. No track_id column on purpose:
    the door must add it additively (_has_col-guarded), as _persist_track_id
    already does.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            track TEXT,
            priority TEXT DEFAULT 'P2',
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(dispatch_id, project_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE tracks (
            track_id TEXT NOT NULL PRIMARY KEY,
            phase TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        )
        """
    )
    for tid, phase in (tracks or {}).items():
        conn.execute(
            "INSERT INTO tracks (track_id, phase, project_id) VALUES (?, ?, 'vnx-dev')",
            (tid, phase),
        )
    conn.commit()
    conn.close()
    return db_path


def _make_bundle(
    tmp_path: Path,
    *,
    staging_id: str,
    dispatch_id: str,
    track_id: "str | None" = "oi-847-track",
    schema_version: int = 1,
) -> "tuple[Path, Path]":
    """A promoted-style staged bundle (spec + instruction inside the bundle dir).

    Returns (data_dir, spec_file).
    """
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec = {
        "schema_version": schema_version,
        "project_id": "vnx-dev",
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(instruction),
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    if track_id is not None:
        spec["track_id"] = track_id
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _read_row(db_path: Path, dispatch_id: str):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()
    conn.close()
    return row


def _row_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    conn.close()
    return count


# ---------------------------------------------------------------------------
# 1. A dispatch through the door yields a row with the columns the three
#    consumers read (dispatch_id, project_id, state, track_id, created_at).
# ---------------------------------------------------------------------------

def test_door_creates_dispatch_row(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi847-row",
        dispatch_id="20260731-oi847-row-created",
    )
    db_path = _make_coordination_db(
        data_dir / "state", tracks={"oi-847-track": "active"}
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)

    assert rc == 0
    row = _read_row(db_path, "20260731-oi847-row-created")
    assert row is not None, "door must create a dispatches row for an accepted dispatch"
    assert row["project_id"] == "vnx-dev"
    # 'proposed': visible to live queries, invisible to claim/stuck/ghost sweeps
    assert row["state"] == "proposed"
    # symptom 1 consumer: the track_id column _persist_track_id / D2 read
    assert row["track_id"] == "oi-847-track"
    # symptom 2 consumer: created_at feeds the classifier's age computation
    assert row["created_at"]


# ---------------------------------------------------------------------------
# 2. Symptom 1 DoD: after the door ran, _persist_track_id's UPDATE actually
#    hits a row (on main it updated zero rows — a silent no-op).
# ---------------------------------------------------------------------------

def test_persist_track_id_hits_row_after_door(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi847-tl",
        dispatch_id="20260731-oi847-track-link",
    )
    db_path = _make_coordination_db(
        data_dir / "state", tracks={"oi-847-track": "active"}
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)
    assert rc == 0

    # Blank the column, then prove _persist_track_id's UPDATE lands on a row.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE dispatches SET track_id = NULL WHERE dispatch_id = ?",
        ("20260731-oi847-track-link",),
    )
    conn.commit()
    conn.close()

    spec = load_spec(spec_file)
    _persist_track_id(spec, state_dir=data_dir / "state")

    row = _read_row(db_path, "20260731-oi847-track-link")
    assert row is not None, "no dispatches row — _persist_track_id updated zero rows"
    assert row["track_id"] == "oi-847-track", (
        "_persist_track_id must stamp track_id onto the door-created row"
    )


# ---------------------------------------------------------------------------
# 3. Idempotency: the same dispatch through the door twice (retry /
#    fix-forward) creates no second row and leaves the first untouched.
# ---------------------------------------------------------------------------

def test_retry_is_idempotent(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi847-retry",
        dispatch_id="20260731-oi847-retry",
    )
    db_path = _make_coordination_db(
        data_dir / "state", tracks={"oi-847-track": "active"}
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0):
        assert run_dispatch(spec_file) == 0
    first = _read_row(db_path, "20260731-oi847-retry")
    assert first is not None

    with patch("dispatch_cli._execute_claude", return_value=0):
        assert run_dispatch(spec_file) == 0

    assert _row_count(db_path) == 1, "retry must not create a second dispatches row"
    second = _read_row(db_path, "20260731-oi847-retry")
    assert dict(second) == dict(first), "retry must leave the first row untouched"


# ---------------------------------------------------------------------------
# 4. A rejected dispatch yields NO row; an accepted one yields exactly its own.
# ---------------------------------------------------------------------------

def test_rejected_dispatch_creates_no_row(tmp_path, monkeypatch):
    db_path = _make_coordination_db(
        tmp_path / "vnx-data" / "state", tracks={"oi-847-track": "active"}
    )

    # Rejected: schema_version=99 fails validate() before any acceptance.
    data_dir, bad_spec = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi847-reject",
        dispatch_id="20260731-oi847-rejected",
        schema_version=99,
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(bad_spec)
    assert rc == 1
    mock_execute.assert_not_called()

    # Accepted: same store, valid spec.
    _, good_spec = _make_bundle(
        tmp_path,
        staging_id="20260731-staging-oi847-accept",
        dispatch_id="20260731-oi847-accepted",
    )
    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(good_spec)
    assert rc == 0

    assert _row_count(db_path) == 1, (
        "exactly one row: the accepted dispatch's; the rejected one must add none"
    )
    assert _read_row(db_path, "20260731-oi847-rejected") is None
    assert _read_row(db_path, "20260731-oi847-accepted") is not None
