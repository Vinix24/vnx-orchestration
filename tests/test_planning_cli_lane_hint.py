"""tests/test_planning_cli_lane_hint.py — per-track `lane_hint` (governed|direct|unset).

A track carries a descriptive lane_hint so a cold-start T0 knows how a track is
MEANT to be dispatched without relying on judgment carried over from a prior
session. Stored in tracks.metadata_json (the existing extensibility slot for
per-track attributes — see seed_tracks_from_roadmap.py's metadata.pr_queue),
not a dedicated column, so no migration is required and pre-existing tracks
read back as 'unset' with no error.

Verifies:
- default 'unset' for a track created with no lane_hint
- `objective add --lane-hint <value>` sets it at creation time
- `objective set-lane-hint <id> <value>` sets/updates it on an existing track
- it round-trips through `objective show` (text + --json)
- a pre-existing track with no lane_hint key (or malformed metadata_json)
  reads as 'unset' — no error, no migration break
- an invalid value is rejected by argparse (small controlled set)
- `objective sync --apply` (the ROADMAP seeder) does not silently wipe a
  manually-set lane_hint, even when the seeder legitimately rewrites the rest
  of metadata_json for an authored-field change
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
for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import schema_migration  # noqa: E402
import tracks as tracks_lib  # noqa: E402
import seed_tracks_from_roadmap as seeder  # noqa: E402
import planning_cli  # noqa: E402


def _bootstrap(tmp_path: Path) -> Path:
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


def _add_args(state_dir: Path, **over) -> argparse.Namespace:
    base = dict(
        track_id="feat-lane-001", title="Lane hint track", goal_state="shipped",
        horizon="now", priority=None, lane_hint=None,
        project_id="vnx-dev", state_dir=str(state_dir), json=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _show_json(state_dir: Path, track_id: str, project_id: str = "vnx-dev") -> dict:
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = planning_cli.main([
            "objective", "show", track_id,
            "--project-id", project_id, "--state-dir", str(state_dir), "--json",
        ])
    assert rc == 0
    return json.loads(buf.getvalue())


# --- default / round-trip ---

def test_new_track_defaults_to_unset(tmp_path):
    state_dir = _bootstrap(tmp_path)
    rc = planning_cli.cmd_objective_add(_add_args(state_dir))
    assert rc == 0
    data = _show_json(state_dir, "feat-lane-001")
    assert data["lane_hint"] == "unset"


def test_objective_add_sets_lane_hint(tmp_path):
    state_dir = _bootstrap(tmp_path)
    rc = planning_cli.cmd_objective_add(_add_args(state_dir, lane_hint="governed"))
    assert rc == 0
    data = _show_json(state_dir, "feat-lane-001")
    assert data["lane_hint"] == "governed"
    # round-trips through the store, not just the in-memory return value
    track = tracks_lib.get_track(state_dir, "feat-lane-001", "vnx-dev")
    assert json.loads(track["metadata_json"])["lane_hint"] == "governed"


def test_set_lane_hint_command_round_trips(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path)
    assert planning_cli.cmd_objective_add(_add_args(state_dir)) == 0

    rc = planning_cli.main([
        "objective", "set-lane-hint", "feat-lane-001", "direct",
        "--project-id", "vnx-dev", "--state-dir", str(state_dir),
    ])
    assert rc == 0
    assert "lane_hint=direct" in capsys.readouterr().out

    data = _show_json(state_dir, "feat-lane-001")
    assert data["lane_hint"] == "direct"


def test_set_lane_hint_can_change_value(tmp_path):
    state_dir = _bootstrap(tmp_path)
    assert planning_cli.cmd_objective_add(_add_args(state_dir, lane_hint="governed")) == 0

    rc = planning_cli.main([
        "objective", "set-lane-hint", "feat-lane-001", "direct",
        "--project-id", "vnx-dev", "--state-dir", str(state_dir),
    ])
    assert rc == 0
    assert _show_json(state_dir, "feat-lane-001")["lane_hint"] == "direct"

    rc = planning_cli.main([
        "objective", "set-lane-hint", "feat-lane-001", "unset",
        "--project-id", "vnx-dev", "--state-dir", str(state_dir),
    ])
    assert rc == 0
    assert _show_json(state_dir, "feat-lane-001")["lane_hint"] == "unset"


def test_set_lane_hint_preserves_other_metadata(tmp_path):
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(
        state_dir, "feat-lane-meta", "vnx-dev", "Meta track", "shipped",
        metadata_json=json.dumps({"pr_queue": ["#42"]}),
    )
    rc = planning_cli.main([
        "objective", "set-lane-hint", "feat-lane-meta", "governed",
        "--project-id", "vnx-dev", "--state-dir", str(state_dir),
    ])
    assert rc == 0
    track = tracks_lib.get_track(state_dir, "feat-lane-meta", "vnx-dev")
    meta = json.loads(track["metadata_json"])
    assert meta["lane_hint"] == "governed"
    assert meta["pr_queue"] == ["#42"]


def test_set_lane_hint_missing_track_fails_cleanly(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path)
    rc = planning_cli.main([
        "objective", "set-lane-hint", "does-not-exist", "governed",
        "--project-id", "vnx-dev", "--state-dir", str(state_dir),
    ])
    assert rc == 2
    assert "track not found" in capsys.readouterr().err


def test_invalid_lane_hint_value_rejected(tmp_path):
    state_dir = _bootstrap(tmp_path)
    assert planning_cli.cmd_objective_add(_add_args(state_dir)) == 0
    with pytest.raises(SystemExit):
        planning_cli.main([
            "objective", "set-lane-hint", "feat-lane-001", "bogus",
            "--project-id", "vnx-dev", "--state-dir", str(state_dir),
        ])


# --- backward compatibility: pre-existing records with no lane_hint ---

def test_track_without_metadata_key_reads_unset(tmp_path):
    """A track whose metadata_json has no lane_hint key (e.g. seeded before this
    feature existed) must read as 'unset', not raise."""
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(
        state_dir, "feat-legacy", "vnx-dev", "Legacy track", "shipped",
        metadata_json=json.dumps({"some_other_field": True}),
    )
    data = _show_json(state_dir, "feat-legacy")
    assert data["lane_hint"] == "unset"


def test_track_with_null_metadata_reads_unset(tmp_path):
    """A row with metadata_json = NULL (older schema state) must not crash."""
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(state_dir, "feat-null-meta", "vnx-dev", "Null meta", "shipped")
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "UPDATE tracks SET metadata_json = NULL WHERE track_id = ? AND project_id = ?",
        ("feat-null-meta", "vnx-dev"),
    )
    conn.commit()
    conn.close()
    data = _show_json(state_dir, "feat-null-meta")
    assert data["lane_hint"] == "unset"


def test_track_with_malformed_metadata_reads_unset(tmp_path):
    """A row with corrupt (non-JSON) metadata_json must not crash the read path."""
    state_dir = _bootstrap(tmp_path)
    tracks_lib.create_track(state_dir, "feat-bad-meta", "vnx-dev", "Bad meta", "shipped")
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "UPDATE tracks SET metadata_json = ? WHERE track_id = ? AND project_id = ?",
        ("not-json{{", "feat-bad-meta", "vnx-dev"),
    )
    conn.commit()
    conn.close()
    data = _show_json(state_dir, "feat-bad-meta")
    assert data["lane_hint"] == "unset"


def test_show_text_output_includes_lane_hint_line(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path)
    assert planning_cli.cmd_objective_add(_add_args(state_dir, lane_hint="direct")) == 0
    rc = planning_cli.main([
        "objective", "show", "feat-lane-001",
        "--project-id", "vnx-dev", "--state-dir", str(state_dir),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "lane_hint: direct" in out


# --- objective sync (ROADMAP seeder) must not clobber a manually-set lane_hint ---

_SAMPLE_ROADMAP = """
roadmap_id: test-roadmap
title: Test
features:
  - feature_id: feat-a
    title: Feature A
    risk_class: high
    depends_on: []
    milestone: "1.0"
    status: planned
"""

_SAMPLE_ROADMAP_RISK_CHANGED = """
roadmap_id: test-roadmap
title: Test
features:
  - feature_id: feat-a
    title: Feature A
    risk_class: low
    depends_on: []
    milestone: "1.0"
    status: planned
"""


def _sync_args(state_dir: Path, roadmap: Path, **over) -> argparse.Namespace:
    base = dict(
        state_dir=str(state_dir), roadmap=str(roadmap), project_id="vnx-dev",
        json=False, apply=True,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_sync_apply_preserves_lane_hint_when_nothing_else_changes(tmp_path):
    state_dir = _bootstrap(tmp_path)
    roadmap = tmp_path / "ROADMAP.yaml"
    roadmap.write_text(_SAMPLE_ROADMAP, encoding="utf-8")

    assert seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)["summary"]["created"] == 1
    rc = planning_cli.main([
        "objective", "set-lane-hint", "feat-a", "governed",
        "--project-id", "vnx-dev", "--state-dir", str(state_dir),
    ])
    assert rc == 0

    rc = planning_cli.cmd_objective_sync(_sync_args(state_dir, roadmap))
    assert rc == 0
    assert _lane_hint_via_get_track(state_dir, "feat-a") == "governed"


def test_sync_apply_preserves_lane_hint_across_real_authored_change(tmp_path):
    """lane_hint must survive a sync that legitimately rewrites metadata_json
    for an unrelated ROADMAP-authored field (risk_class -> priority)."""
    state_dir = _bootstrap(tmp_path)
    roadmap = tmp_path / "ROADMAP.yaml"
    roadmap.write_text(_SAMPLE_ROADMAP, encoding="utf-8")
    seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)

    rc = planning_cli.main([
        "objective", "set-lane-hint", "feat-a", "direct",
        "--project-id", "vnx-dev", "--state-dir", str(state_dir),
    ])
    assert rc == 0

    roadmap.write_text(_SAMPLE_ROADMAP_RISK_CHANGED, encoding="utf-8")
    rc = planning_cli.cmd_objective_sync(_sync_args(state_dir, roadmap))
    assert rc == 0

    track = tracks_lib.get_track(state_dir, "feat-a", "vnx-dev")
    assert planning_cli._lane_hint_of(track) == "direct"
    # confirm the risk_class-driven field actually changed underneath it
    assert json.loads(track["metadata_json"])["risk_class"] == "low"


def test_sync_check_mode_never_writes_lane_hint(tmp_path):
    """CHECK mode (no --apply) must not touch the store at all."""
    state_dir = _bootstrap(tmp_path)
    roadmap = tmp_path / "ROADMAP.yaml"
    roadmap.write_text(_SAMPLE_ROADMAP, encoding="utf-8")
    seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    planning_cli.main([
        "objective", "set-lane-hint", "feat-a", "governed",
        "--project-id", "vnx-dev", "--state-dir", str(state_dir),
    ])

    rc = planning_cli.cmd_objective_sync(_sync_args(state_dir, roadmap, apply=False))
    assert rc == 0
    assert _lane_hint_via_get_track(state_dir, "feat-a") == "governed"


def _lane_hint_via_get_track(state_dir: Path, track_id: str, project_id: str = "vnx-dev") -> str:
    track = tracks_lib.get_track(state_dir, track_id, project_id)
    return planning_cli._lane_hint_of(track)
