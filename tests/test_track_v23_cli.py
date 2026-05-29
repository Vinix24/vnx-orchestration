"""tests/test_track_v23_cli.py — CLI tests for project_id-required mutations.

Verifies:
- `vnx track new track-01 --project-id vnx-dev ...` succeeds
- `vnx track new track-01 --title ... --goal ...` (missing --project-id) exits 2
- `vnx track list --all-projects` returns tracks from all project_ids
- `vnx track list` (no --project-id, no VNX_PROJECT_ID) falls back to git remote
- `vnx track activate/park/unpark/dispatch` all reject missing --project-id
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_MIGRATIONS = _ROOT / "schemas" / "migrations"
_VNXCLI = _ROOT / "vnx_cli"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import schema_migration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    """Return state_dir Path with 0022+0024 applied.

    Creates DB at tmp_path/.vnx-data/state/ to match CLI's _resolve_state_dir()
    which builds project_dir/.vnx-data/state/.
    """
    state_dir = tmp_path / ".vnx-data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT,
            priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, event_type TEXT, entity_type TEXT,
            entity_id TEXT, from_state TEXT, to_state TEXT,
            actor TEXT, reason TEXT, metadata_json TEXT,
            occurred_at TEXT, project_id TEXT
        )
    """)
    conn.commit()
    for version, filename in [(22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.close()
    return state_dir


def _make_args(**kwargs) -> argparse.Namespace:
    defaults = {
        "project_dir": ".",
        "project_id": None,
        "all_projects": False,
        "phase": None,
        "reason": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _noop_emit(*args, **kwargs):
    pass


# ---------------------------------------------------------------------------
# Tests: --project-id required for mutations
# ---------------------------------------------------------------------------

def test_new_requires_project_id_via_argparse():
    """argparse must reject 'new' without --project-id (exit 2)."""
    from vnx_cli.main import _register_track_subparser
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    track_parser = subs.add_parser("track")
    track_subs = track_parser.add_subparsers(dest="track_subcommand")
    _register_track_subparser.__globals__  # confirm it's importable
    from vnx_cli.main import main as _main_unused
    # Import the registered parser directly
    import importlib
    main_mod = importlib.import_module("vnx_cli.main")
    p2 = argparse.ArgumentParser()
    s2 = p2.add_subparsers(dest="command")
    main_mod._register_track_subparser(s2)
    with pytest.raises(SystemExit) as exc_info:
        p2.parse_args(["track", "new", "track-01", "--title", "T", "--goal", "G"])
    assert exc_info.value.code == 2


def test_activate_requires_project_id_via_argparse():
    import importlib
    main_mod = importlib.import_module("vnx_cli.main")
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="command")
    main_mod._register_track_subparser(s)
    with pytest.raises(SystemExit) as exc_info:
        p.parse_args(["track", "activate", "track-01"])
    assert exc_info.value.code == 2


def test_park_requires_project_id_via_argparse():
    import importlib
    main_mod = importlib.import_module("vnx_cli.main")
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="command")
    main_mod._register_track_subparser(s)
    with pytest.raises(SystemExit) as exc_info:
        p.parse_args(["track", "park", "track-01", "--reason", "R"])
    assert exc_info.value.code == 2


def test_unpark_requires_project_id_via_argparse():
    import importlib
    main_mod = importlib.import_module("vnx_cli.main")
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="command")
    main_mod._register_track_subparser(s)
    with pytest.raises(SystemExit) as exc_info:
        p.parse_args(["track", "unpark", "track-01"])
    assert exc_info.value.code == 2


def test_dispatch_requires_project_id_via_argparse():
    import importlib
    main_mod = importlib.import_module("vnx_cli.main")
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="command")
    main_mod._register_track_subparser(s)
    with pytest.raises(SystemExit) as exc_info:
        p.parse_args(["track", "dispatch", "track-01", "--pr", "PR-001", "--terminal", "T1"])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Tests: new command succeeds with --project-id
# ---------------------------------------------------------------------------

def test_new_with_project_id_succeeds(tmp_path):
    import tracks as tracks_lib
    state_dir = _make_db(tmp_path)

    args = _make_args(
        track_id="track-01",
        project_id="vnx-dev",
        title="My Title",
        goal="My Goal",
        priority=None,
        project_dir=str(tmp_path),
        track_subcommand="new",
    )

    from vnx_cli.commands.track import _cmd_new
    with patch.object(tracks_lib, "_emit_track_event", _noop_emit):
        ret = _cmd_new(args)
    assert ret == 0


# ---------------------------------------------------------------------------
# Tests: list --all-projects
# ---------------------------------------------------------------------------

def test_list_all_projects(tmp_path, capsys):
    import tracks as tracks_lib
    state_dir = _make_db(tmp_path)  # creates at tmp_path/.vnx-data/state/

    with patch.object(tracks_lib, "_emit_track_event", _noop_emit):
        tracks_lib.create_track(state_dir, "track-01", "vnx-dev", "VNX Title", "G")
        tracks_lib.create_track(state_dir, "track-01", "seocrawler-v2", "SEO Title", "G")

    args = _make_args(
        project_dir=str(tmp_path),
        project_id=None,
        all_projects=True,
        phase=None,
        track_subcommand="list",
    )

    from vnx_cli.commands.track import _cmd_list
    ret = _cmd_list(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "vnx-dev" in out
    assert "seocrawler-v2" in out


def test_list_scoped_by_project_id(tmp_path, capsys):
    import tracks as tracks_lib
    state_dir = _make_db(tmp_path)

    with patch.object(tracks_lib, "_emit_track_event", _noop_emit):
        tracks_lib.create_track(state_dir, "track-01", "vnx-dev", "VNX Title", "G")
        tracks_lib.create_track(state_dir, "track-01", "seocrawler-v2", "SEO Title", "G")

    args = _make_args(
        project_dir=str(tmp_path),
        project_id="vnx-dev",
        all_projects=False,
        phase=None,
        track_subcommand="list",
    )

    from vnx_cli.commands.track import _cmd_list
    ret = _cmd_list(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "VNX Title" in out
    assert "SEO Title" not in out


# ---------------------------------------------------------------------------
# Tests: list without --project-id falls back to resolve_project_id()
# ---------------------------------------------------------------------------

def test_list_falls_back_to_resolve_project_id(tmp_path, capsys):
    import tracks as tracks_lib
    state_dir = _make_db(tmp_path)  # at tmp_path/.vnx-data/state/

    with patch.object(tracks_lib, "_emit_track_event", _noop_emit):
        tracks_lib.create_track(state_dir, "track-01", "my-project", "My Title", "G")

    args = _make_args(
        project_dir=str(tmp_path),
        project_id=None,
        all_projects=False,
        phase=None,
        track_subcommand="list",
    )

    from vnx_cli.commands import track as track_cmd
    # Patch the resolve_project_id imported inside _resolve_project_id_for_read
    import project_root as pr_mod
    with patch.object(pr_mod, "resolve_project_id", return_value="my-project"):
        ret = track_cmd._cmd_list(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "My Title" in out


def test_list_exits_2_when_resolve_project_id_fails(tmp_path):
    args = _make_args(
        project_dir=str(tmp_path),
        project_id=None,
        all_projects=False,
        phase=None,
        track_subcommand="list",
    )

    from vnx_cli.commands import track as track_cmd
    import project_root as pr_mod
    with patch.object(pr_mod, "resolve_project_id", side_effect=RuntimeError("no git remote")):
        with pytest.raises(SystemExit) as exc_info:
            track_cmd._cmd_list(args)
    assert exc_info.value.code == 2
