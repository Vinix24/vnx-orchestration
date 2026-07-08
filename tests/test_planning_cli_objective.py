"""tests/test_planning_cli_objective.py — `vnx objective list/show` read surface.

Verifies (against a temp DB seeded from a sample ROADMAP):
- `objective list` renders rows grouped by horizon
- `objective list --json` is machine-readable with deps
- `objective list --horizon now` filters
- `objective show <id>` renders one objective + deps
- `objective show` on a missing id exits non-zero
"""

from __future__ import annotations

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

import schema_migration
import seed_tracks_from_roadmap as seeder
import planning_cli
import tracks as tracks_lib


SAMPLE_ROADMAP = """
roadmap_id: test-roadmap
title: Test
features:
  - feature_id: feat-a
    title: Feature A
    risk_class: high
    depends_on: []
    milestone: "1.0"
    status: planned
    notes: Build A.
  - feature_id: feat-b
    title: Feature B
    risk_class: low
    depends_on: [feat-a]
    milestone: "1.0"
    status: done
  - feature_id: feat-c
    title: Feature C
    risk_class: medium
    depends_on: []
    milestone: "1.x"
    status: planned
"""


@pytest.fixture()
def seeded_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT,
            gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.commit()
    for ver, fname in ((22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 27, (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    conn.close()

    roadmap = tmp_path / "ROADMAP.yaml"
    roadmap.write_text(SAMPLE_ROADMAP, encoding="utf-8")
    seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    return state_dir


def test_objective_list_renders(seeded_state, capsys):
    rc = planning_cli.main([
        "objective", "list", "--project-id", "vnx-dev", "--state-dir", str(seeded_state),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "feat-a" in out
    assert "feat-b" in out
    assert "feat-c" in out
    # Grouped by horizon bands.
    assert "NOW" in out
    assert "LATER" in out


def test_objective_list_json(seeded_state, capsys):
    rc = planning_cli.main([
        "objective", "list", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state), "--json",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    by_id = {d["track_id"]: d for d in data}
    assert set(by_id) == {"feat-a", "feat-b", "feat-c"}
    # feat-b depends on feat-a.
    assert by_id["feat-b"]["depends_on"] == ["feat-a"]
    assert by_id["feat-a"]["phase"] == "queued"  # planned -> queued
    assert by_id["feat-c"]["horizon"] == "later"


def test_objective_list_horizon_filter(seeded_state, capsys):
    rc = planning_cli.main([
        "objective", "list", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state), "--horizon", "now", "--json",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    ids = {d["track_id"] for d in data}
    # feat-a is 1.0 + planned -> horizon now; feat-c (1.x) excluded.
    assert "feat-a" in ids
    assert "feat-c" not in ids


def test_objective_show(seeded_state, capsys):
    rc = planning_cli.main([
        "objective", "show", "feat-b", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "feat-b" in out
    assert "feat-a" in out  # dependency listed


def test_objective_show_json(seeded_state, capsys):
    rc = planning_cli.main([
        "objective", "show", "feat-b", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state), "--json",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["track_id"] == "feat-b"
    assert data["depends_on"] == ["feat-a"]


def test_objective_show_missing_returns_nonzero(seeded_state, capsys):
    rc = planning_cli.main([
        "objective", "show", "does-not-exist", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state),
    ])
    assert rc == 1


def test_objective_show_no_open_items(seeded_state, capsys):
    rc = planning_cli.main([
        "objective", "show", "feat-a", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "open items : (none)" in out


def test_objective_show_lists_open_blocking_finding(seeded_state, capsys):
    """A gate-recorded `blocks` open-item must surface here without any PR read."""
    tracks_lib.link_open_item(
        seeded_state, "feat-a", "vnx-dev", "gate:pre_merge_gate:d-1", "blocks", "manual",
    )
    rc = planning_cli.main([
        "objective", "show", "feat-a", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "open items (unresolved):" in out
    assert "[blocks] gate:pre_merge_gate:d-1" in out


def test_objective_show_json_includes_open_items(seeded_state, capsys):
    tracks_lib.link_open_item(
        seeded_state, "feat-a", "vnx-dev", "gate:pre_merge_gate:d-1", "blocks", "manual",
    )
    rc = planning_cli.main([
        "objective", "show", "feat-a", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state), "--json",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["open_items"]) == 1
    assert data["open_items"][0]["oi_id"] == "gate:pre_merge_gate:d-1"
