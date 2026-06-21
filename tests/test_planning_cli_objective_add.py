"""tests/test_planning_cli_objective_add.py — the PM-skill ad-hoc objective add.

`planning_cli.py objective add` is the thin wrapper over tracks.create_track that
lets the PM queue a feature without a ROADMAP edit, while keeping tracks.py the
single writer (the gap GLM-5.2 flagged in the PM-skill design panel). New tracks
must start `queued` (the plan-first gate must pass before anything promotes).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"
for p in (str(_LIB), str(_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import schema_migration  # noqa: E402
import tracks  # noqa: E402
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


def _args(state_dir: Path, **over) -> argparse.Namespace:
    base = dict(
        track_id="feat-pm-001", title="PM skill", goal_state="shipped",
        horizon="now", priority=None, project_id="vnx-dev", state_dir=str(state_dir),
        json=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_objective_add_creates_queued_track(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path)
    rc = planning_cli.cmd_objective_add(_args(state_dir))
    assert rc == 0
    t = tracks.get_track(state_dir, "feat-pm-001", "vnx-dev")
    assert t is not None
    assert t["phase"] == "queued"        # plan-first: never starts active
    assert t["title"] == "PM skill"


def test_objective_add_is_tenant_scoped(tmp_path):
    state_dir = _bootstrap(tmp_path)
    assert planning_cli.cmd_objective_add(_args(state_dir, project_id="vnx-dev")) == 0
    assert planning_cli.cmd_objective_add(
        _args(state_dir, project_id="seocrawler-v2", track_id="feat-pm-001")
    ) == 0
    # same track_id coexists across tenants (ADR-007)
    assert tracks.get_track(state_dir, "feat-pm-001", "vnx-dev") is not None
    assert tracks.get_track(state_dir, "feat-pm-001", "seocrawler-v2") is not None


def test_objective_add_duplicate_fails_cleanly(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path)
    assert planning_cli.cmd_objective_add(_args(state_dir)) == 0
    rc = planning_cli.cmd_objective_add(_args(state_dir))  # same id+project
    assert rc == 1
    assert "objective add failed" in capsys.readouterr().err


# --- plan-first promotion lock (step 3) ---

def _seed_proposed_deliverable(state_dir: Path, track_id: str, did: str, pid="vnx-dev"):
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
        (did, pid, "proposed", track_id),
    )
    conn.commit(); conn.close()


def _set_derived_status(state_dir: Path, track_id: str, status, pid="vnx-dev"):
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "UPDATE tracks SET derived_status = ? WHERE track_id = ? AND project_id = ?",
        (status, track_id, pid),
    )
    conn.commit(); conn.close()


def _promote_args(state_dir, did, pid="vnx-dev"):
    return argparse.Namespace(dispatch_id=did, project_id=pid, state_dir=str(state_dir))


def test_promote_refused_while_track_blocked(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path)
    assert planning_cli.cmd_objective_add(_args(state_dir, track_id="feat-x")) == 0
    _seed_proposed_deliverable(state_dir, "feat-x", "dlv-1")
    _set_derived_status(state_dir, "feat-x", "blocked")        # OI-PLAN open
    rc = planning_cli.cmd_deliverable_promote(_promote_args(state_dir, "dlv-1"))
    assert rc == 1
    assert "blocked" in capsys.readouterr().err


def test_promote_allowed_when_track_unblocked(tmp_path):
    state_dir = _bootstrap(tmp_path)
    assert planning_cli.cmd_objective_add(_args(state_dir, track_id="feat-y")) == 0
    _seed_proposed_deliverable(state_dir, "feat-y", "dlv-2")
    _set_derived_status(state_dir, "feat-y", "active")         # plan gate passed
    rc = planning_cli.cmd_deliverable_promote(_promote_args(state_dir, "dlv-2"))
    assert rc == 0


def test_promote_ungated_when_no_track(tmp_path):
    # a deliverable with no track is not plan-first-gated here (graceful)
    state_dir = _bootstrap(tmp_path)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
                 ("dlv-3", "vnx-dev", "proposed", None))
    conn.commit(); conn.close()
    assert planning_cli.cmd_deliverable_promote(_promote_args(state_dir, "dlv-3")) == 0
