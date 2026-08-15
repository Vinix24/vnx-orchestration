"""tests/test_objective_set_goal.py — `vnx objective set-goal` / `vnx track set-goal`.

Punt 16 + OI-1230: a too-thin track goal was only settable at creation time
(`objective add` / `track new --goal`). The plan-first gate refuses a goal under
``goal_min_chars`` meaningful characters, and no verb could raise it afterwards
without an operator-attest (which books the track as done) or a forbidden direct
DB write.

Verifies the repair command end to end, against a temporary database:

- a too-thin goal is refused with the measured length and the threshold
- a sufficient goal is written and read back from DISK (fresh connection), with
  ``phase`` and ``derived_status`` untouched
- the ``track_goal_set`` audit event carries the old and the new goal value
- an unknown track_id fails loudly
- both surfaces (`objective set-goal`, `track set-goal`) share the same writer
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"
for p in (_LIB, _SCRIPTS, _ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import schema_migration  # noqa: E402
import tracks as tracks_lib  # noqa: E402
import planning_cli  # noqa: E402
import plan_gate_panel  # noqa: E402


THRESHOLD = 200


def _bootstrap(tmp_path: Path) -> Path:
    """Create a temporary state dir with the track schema (migrations 0022-0028)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
        "state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT, "
        "priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT, "
        "attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "expires_after TEXT, metadata_json TEXT DEFAULT '{}', "
        "UNIQUE(dispatch_id, project_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coordination_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT, event_type TEXT, entity_type TEXT, entity_id TEXT, from_state TEXT, "
        "to_state TEXT, actor TEXT, reason TEXT, metadata_json TEXT, occurred_at TEXT, project_id TEXT)"
    )
    conn.commit()
    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.close()
    return state_dir


def _read_row(state_dir: Path, track_id: str, project_id: str) -> dict:
    """Read a track row from DISK via a fresh connection (not the in-memory DAL)."""
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def _read_events(state_dir: Path) -> list[dict]:
    path = state_dir.parent / "events" / "track_events.ndjson"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _add_args(state_dir: Path, **over) -> argparse.Namespace:
    base = dict(
        track_id="feat-goal-001", goal="x" * 250,
        project_id="vnx-dev", state_dir=str(state_dir), json=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# DAL: tracks.set_goal
# ---------------------------------------------------------------------------

def test_set_goal_thin_goal_refused_with_length_and_threshold(tmp_path):
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(state_dir, "feat-goal-001", "vnx-dev", "T", "old goal")

    with pytest.raises(tracks_lib.GoalTooThinError) as exc_info:
        tracks_lib.set_goal(
            state_dir, "feat-goal-001", "vnx-dev", "too thin",
            min_goal_chars=THRESHOLD,
        )

    assert exc_info.value.length == len("too thin")
    assert exc_info.value.threshold == THRESHOLD
    msg = str(exc_info.value)
    assert "measured 8 meaningful chars" in msg
    assert f"threshold {THRESHOLD}" in msg
    # refused: nothing was written
    assert _read_row(state_dir, "feat-goal-001", "vnx-dev")["goal_state"] == "old goal"


def test_set_goal_measures_whitespace_stripped_length(tmp_path):
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(state_dir, "feat-goal-001", "vnx-dev", "T", "old goal")

    # 300 spaces -> 0 meaningful chars, refused.
    with pytest.raises(tracks_lib.GoalTooThinError) as exc_info:
        tracks_lib.set_goal(
            state_dir, "feat-goal-001", "vnx-dev", " " * 300,
            min_goal_chars=THRESHOLD,
        )
    assert exc_info.value.length == 0


def test_set_goal_sufficient_goal_written_and_phase_preserved(tmp_path):
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(
        state_dir, "feat-goal-001", "vnx-dev", "T", "old goal", phase="active",
    )
    # plant a non-default derived_status to prove set_goal does not touch it
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "UPDATE tracks SET derived_status = 'blocked' WHERE track_id = ? AND project_id = ?",
        ("feat-goal-001", "vnx-dev"),
    )
    conn.commit()
    before = _read_row(state_dir, "feat-goal-001", "vnx-dev")

    new_goal = "ship " + "x" * 240
    result = tracks_lib.set_goal(
        state_dir, "feat-goal-001", "vnx-dev", new_goal,
        min_goal_chars=THRESHOLD,
    )

    assert result["goal_state"] == new_goal
    # read back from disk (fresh connection), not the returned dict
    after = _read_row(state_dir, "feat-goal-001", "vnx-dev")
    assert after["goal_state"] == new_goal
    assert after["phase"] == "active"
    assert after["derived_status"] == "blocked"
    assert after["phase_changed_at"] == before["phase_changed_at"]


def test_set_goal_audit_event_contains_old_and_new(tmp_path):
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(state_dir, "feat-goal-001", "vnx-dev", "T", "old goal")

    new_goal = "ship " + "x" * 240
    tracks_lib.set_goal(
        state_dir, "feat-goal-001", "vnx-dev", new_goal,
        min_goal_chars=THRESHOLD, actor="operator",
    )

    events = [e for e in _read_events(state_dir) if e["event_type"] == "track_goal_set"]
    assert len(events) == 1
    ev = events[0]
    assert ev["details"]["old_goal"] == "old goal"
    assert ev["details"]["new_goal"] == new_goal
    assert ev["track_id"] == "feat-goal-001"
    assert ev["project_id"] == "vnx-dev"
    assert ev["actor"] == "operator"
    assert ev["timestamp"]


def test_set_goal_unknown_track_raises(tmp_path):
    state_dir = _bootstrap(tmp_path)
    with pytest.raises(tracks_lib.TrackNotFoundError):
        tracks_lib.set_goal(
            state_dir, "does-not-exist", "vnx-dev", "x" * 250,
            min_goal_chars=THRESHOLD,
        )


# ---------------------------------------------------------------------------
# CLI: objective set-goal (planning_cli)
# ---------------------------------------------------------------------------

def test_objective_set_goal_cmd_thin_refused(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(state_dir, "feat-goal-001", "vnx-dev", "T", "old goal")

    rc = planning_cli.cmd_objective_set_goal(_add_args(state_dir, goal="too thin"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "measured 8 meaningful chars" in err
    assert f"threshold {plan_gate_panel.load_goal_min_chars()}" in err
    # refused: nothing written
    assert _read_row(state_dir, "feat-goal-001", "vnx-dev")["goal_state"] == "old goal"


def test_objective_set_goal_cmd_sufficient_written(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(state_dir, "feat-goal-001", "vnx-dev", "T", "old goal", phase="active")

    new_goal = "ship " + "x" * 240
    rc = planning_cli.cmd_objective_set_goal(_add_args(state_dir, goal=new_goal))
    assert rc == 0
    assert "Set goal_state" in capsys.readouterr().out

    after = _read_row(state_dir, "feat-goal-001", "vnx-dev")
    assert after["goal_state"] == new_goal
    assert after["phase"] == "active"


def test_objective_set_goal_cmd_unknown_track_fails(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path)
    rc = planning_cli.cmd_objective_set_goal(
        _add_args(state_dir, track_id="does-not-exist", goal="x" * 250)
    )
    assert rc == 2
    assert "track not found" in capsys.readouterr().err


def test_objective_set_goal_cli_end_to_end(tmp_path):
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(state_dir, "feat-goal-001", "vnx-dev", "T", "old goal")

    new_goal = "ship " + "x" * 240
    rc = planning_cli.main([
        "objective", "set-goal", "feat-goal-001", new_goal,
        "--project-id", "vnx-dev", "--state-dir", str(state_dir),
    ])
    assert rc == 0
    assert _read_row(state_dir, "feat-goal-001", "vnx-dev")["goal_state"] == new_goal


# ---------------------------------------------------------------------------
# CLI: vnx track set-goal alias
# ---------------------------------------------------------------------------

def test_track_set_goal_alias_writes(tmp_path, monkeypatch, capsys):
    # Pin the data root to the temp dir so the alias's _resolve_state_dir never
    # falls through to the real central store (~/.vnx-data/<id>).
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(state_dir, "feat-goal-001", "vnx-dev", "T", "old goal")

    from vnx_cli.commands import track as track_cmd

    args = argparse.Namespace(
        track_id="feat-goal-001", goal="ship " + "x" * 240,
        project_id="vnx-dev", project_dir=str(tmp_path),
    )
    rc = track_cmd._cmd_set_goal(args)
    assert rc == 0
    assert "Set goal_state" in capsys.readouterr().out

    assert _read_row(state_dir, "feat-goal-001", "vnx-dev")["goal_state"] == "ship " + "x" * 240


def test_track_set_goal_alias_thin_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(state_dir, "feat-goal-001", "vnx-dev", "T", "old goal")

    from vnx_cli.commands import track as track_cmd

    args = argparse.Namespace(
        track_id="feat-goal-001", goal="too thin",
        project_id="vnx-dev", project_dir=str(tmp_path),
    )
    rc = track_cmd._cmd_set_goal(args)
    assert rc == 2
    assert "measured 8 meaningful chars" in capsys.readouterr().err
