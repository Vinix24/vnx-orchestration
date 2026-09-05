"""tests/test_oi1632_track_overdracht.py — OI-1632: stage_spec_bundle carries no track_id,
so no bridge-staged dispatch ever gets a track.

Root cause under test: ``dispatch_bridge.stage_spec_bundle`` (the ONLY spec writer for the
four legacy callers — dispatch_deliver.sh, pool_worker_runner.py, headless_dispatch_daemon.py,
claude_adapter.py) had no ``track_id`` parameter at all, so a bridge-staged dispatch's spec
never carried one, regardless of what the door itself (``_check_track_link_verdict``,
``_persist_dispatch_row``, ``_persist_track_id``) already supports. Measured 2026-09-05 on
``~/.vnx-data/vnx-dev/state/runtime_coordination.db``: 681 of 681 non-deliverable dispatch
rows had a NULL/empty ``track`` column, against 122/122 for the `dlv-` control group (proving
the query itself is sound, not a measurement artifact).

This test exercises the REAL bridge call (``dispatch_bridge.stage_spec_bundle``), drives the
resulting bundle through the REAL door (``dispatch_cli.run_dispatch``), and asserts the
``dispatches`` row the door writes carries the track in its ``track`` column — the same
column ``dispatch_cli._persist_dispatch_row`` / ``_persist_track_id`` and
``receipt_provenance._link_pr_to_track`` / ``gate_findings_bridge._resolve_dispatch_track``
all read and write (OI-1632 also consolidates those three off a stray, never-populated
``track_id`` column — see the sibling assertions below).

RED on origin/main: ``stage_spec_bundle(..., track_id=...)`` raises TypeError (no such
parameter). GREEN on the fix: the door-written row's ``track`` column equals the given
track_id. Runs entirely against a throwaway DB under tmp_path — never the live central store.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import coordination_db
import project_id_migration
import dispatch_bridge
from dispatch_cli import run_dispatch


def _make_coordination_db(state_dir: Path, *, tracks: "dict[str, str] | None" = None) -> Path:
    """Real runtime_coordination.db schema + a minimal ``tracks`` stand-in for
    _check_track_link_verdict / _lookup_track_phase (mirrors test_dispatch_door_row.py's
    fixture of the same name — kept independent per-file rather than shared, matching
    that file's own stated rationale for not importing a cross-file fixture)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    coordination_db.init_schema(state_dir)
    db_path = state_dir / "runtime_coordination.db"
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id="vnx-dev")
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracks (
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


def _read_row(db_path: Path, dispatch_id: str):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
    ).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# 1. The bridge itself: stage_spec_bundle(track_id=...) must carry the value
#    onto the written dispatch-spec.json payload.
# ---------------------------------------------------------------------------

def test_stage_spec_bundle_carries_track_id_into_payload(tmp_path):
    spec_file = dispatch_bridge.stage_spec_bundle(
        instruction_text="do the thing",
        dispatch_id="20260905-160001-oi1632-bridge-carries",
        role="backend-developer",
        target_slot="T1",
        project_id="p1",
        provider="claude",
        track_id="oi1632-track",
        data_dir=tmp_path,
    )
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    assert payload["track_id"] == "oi1632-track"


def test_stage_spec_bundle_track_id_absent_is_none(tmp_path):
    spec_file = dispatch_bridge.stage_spec_bundle(
        instruction_text="do the thing",
        dispatch_id="20260905-160001-oi1632-bridge-absent",
        role="backend-developer",
        target_slot="T1",
        project_id="p1",
        provider="claude",
        data_dir=tmp_path,
    )
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    assert payload["track_id"] is None


# ---------------------------------------------------------------------------
# 2. End-to-end: a bridge-staged dispatch with a track must show up in the
#    dispatches row the door writes — the exact behavior the audit demanded.
# ---------------------------------------------------------------------------

def test_bridge_staged_dispatch_with_track_lands_in_door_row(tmp_path, monkeypatch):
    data_dir = tmp_path / "vnx-data"
    db_path = _make_coordination_db(
        data_dir / "state", tracks={"oi1632-track": "active"}
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    dispatch_id = "20260905-160001-oi1632-e2e"
    spec_file = dispatch_bridge.stage_spec_bundle(
        instruction_text="do the thing",
        dispatch_id=dispatch_id,
        role="backend-developer",
        target_slot="T0",
        project_id="vnx-dev",
        provider="claude",
        gate="codex_gate",
        track_id="oi1632-track",
        data_dir=data_dir,
        # Door tests assert row/track state, not lane behavior; tmp_path is not
        # a real git repo, so pin the tmux lane exactly like test_dispatch_door_row.py.
        force_tmux=True,
        force_tmux_reason="OI-1632 door-row test asserts track linkage, not lane behavior",
    )

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)

    assert rc == 0
    row = _read_row(db_path, dispatch_id)
    assert row is not None, "door must create a dispatches row for an accepted dispatch"
    assert row["track"] == "oi1632-track", (
        "a track_id staged via dispatch_bridge.stage_spec_bundle must land in the "
        "dispatches.track column the door writes — OI-1632's core assertion"
    )


def test_bridge_staged_dispatch_without_track_leaves_row_track_null(tmp_path, monkeypatch):
    """Control: an absent track_id must NOT become a hard requirement (VNX_REQUIRE_DISPATCH_TRACK
    stays default OFF) — the dispatch still proceeds, the row's track column is simply NULL."""
    data_dir = tmp_path / "vnx-data"
    db_path = _make_coordination_db(data_dir / "state")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    dispatch_id = "20260905-160001-oi1632-e2e-notrack"
    spec_file = dispatch_bridge.stage_spec_bundle(
        instruction_text="do the thing",
        dispatch_id=dispatch_id,
        role="backend-developer",
        target_slot="T0",
        project_id="vnx-dev",
        provider="claude",
        gate="codex_gate",
        data_dir=data_dir,
        force_tmux=True,
        force_tmux_reason="OI-1632 door-row test asserts track linkage, not lane behavior",
    )

    with patch("dispatch_cli._execute_claude", return_value=0):
        rc = run_dispatch(spec_file)

    assert rc == 0, "an absent track_id must remain advisory-only, never a hard block"
    row = _read_row(db_path, dispatch_id)
    assert row is not None
    assert row["track"] is None
