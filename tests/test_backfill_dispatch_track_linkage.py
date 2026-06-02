#!/usr/bin/env python3
"""Tests for scripts/backfill_dispatch_track_linkage.py.

Builds a temp coordination DB that mimics the divergence the backfill targets:
  - tracks that map to a ROADMAP merged PR (some with pr_ref already set, one empty)
  - dispatches carrying a pr_ref with track = NULL (relinkable)
  - one dispatch with a pre-existing non-null track (must be preserved)
  - the reconciler's auxiliary tables (track_open_items, track_dependencies) empty

Asserts the full safety + correctness contract:
  - dry-run links on a COPY and leaves the temp DB untouched
  - --apply sets dispatches.track for matching pr_refs
  - --apply sets the track-level evidence the reconciler reads (tracks.pr_ref)
    and adds a pr_merged coordination_event tied to a linked dispatch
  - derived_status flips to 'done' after the backfill
  - a pre-existing non-null dispatches.track is never overwritten
  - 2nd --apply is a no-op (idempotent)
  - a backup file is written; integrity_check is ok
  - declared phase + ROADMAP.yaml are never written
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_LIB = _SCRIPTS / "lib"
for p in (str(_SCRIPTS), str(_LIB)):
    if p not in sys.path:
        sys.path.insert(0, p)

import backfill_dispatch_track_linkage as bf  # noqa: E402
import track_reconciler  # noqa: E402

PROJECT_ID = "vnx-dev"

# ---------------------------------------------------------------------------
# Minimal schema mirroring runtime_coordination.db (only what the code touches)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE tracks (
    track_id     TEXT NOT NULL,
    project_id   TEXT NOT NULL DEFAULT 'vnx-dev',
    title        TEXT NOT NULL,
    phase        TEXT NOT NULL DEFAULT 'queued',
    sort_order   INTEGER NOT NULL DEFAULT 0,
    pr_ref       TEXT,
    derived_status TEXT,
    PRIMARY KEY (track_id, project_id)
);
CREATE TABLE dispatches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    state       TEXT,
    track       TEXT,
    pr_ref      TEXT,
    project_id  TEXT NOT NULL DEFAULT 'vnx-dev'
);
CREATE TABLE coordination_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT NOT NULL UNIQUE,
    event_type    TEXT NOT NULL,
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    from_state    TEXT,
    to_state      TEXT,
    actor         TEXT NOT NULL DEFAULT 'runtime',
    reason        TEXT,
    metadata_json TEXT DEFAULT '{}',
    occurred_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    project_id    TEXT NOT NULL DEFAULT 'vnx-dev'
);
CREATE TABLE track_open_items (
    track_id  TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    oi_id     TEXT NOT NULL,
    link_type TEXT NOT NULL,
    link_source TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (track_id, project_id, oi_id, link_type)
);
CREATE TABLE track_dependencies (
    from_track_id   TEXT NOT NULL,
    from_project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    to_track_id     TEXT NOT NULL,
    to_project_id   TEXT NOT NULL DEFAULT 'vnx-dev',
    kind            TEXT NOT NULL DEFAULT 'hard',
    derivation_source TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (from_track_id, from_project_id, to_track_id, to_project_id)
);
"""

ROADMAP_YAML = """
roadmap_id: test-roadmap
title: Test
features:
  - feature_id: launch-readme
    title: "README repositioning"
    milestone: "1.0"
    status: done
    pr_queue:
      - pr_id: "#757"
        title: "docs(readme)"
        status: merged
  - feature_id: launch-renames
    title: "Renames"
    milestone: "1.0"
    status: done
    pr_queue:
      - pr_id: "#759"
        title: "renames"
        status: merged
  - feature_id: open-feature
    title: "Still open"
    milestone: "1.x"
    status: planned
    pr_queue:
      - pr_id: "#900"
        title: "open"
        status: open
"""


def _make_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    # Tracks: launch-readme has pr_ref already; launch-renames pr_ref empty (to be set).
    conn.executemany(
        "INSERT INTO tracks (track_id, project_id, title, phase, sort_order, pr_ref, derived_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("launch-readme", PROJECT_ID, "README", "done", 0, "#757", "queued"),
            ("launch-renames", PROJECT_ID, "Renames", "done", 1, None, "queued"),
            ("open-feature", PROJECT_ID, "Open", "queued", 2, None, "queued"),
        ],
    )
    # Dispatches:
    #   d1 -> pr #757, track NULL, terminal     -> relinks to launch-readme, flips done
    #   d2 -> pr #759, track NULL, terminal     -> relinks to launch-renames, flips done
    #   d3 -> pr #757, track ALREADY 'C', term  -> must NOT be overwritten
    #   d4 -> pr #900 (open PR), track NULL      -> no merged mapping, stays NULL
    conn.executemany(
        "INSERT INTO dispatches (dispatch_id, state, track, pr_ref, project_id) VALUES (?, ?, ?, ?, ?)",
        [
            ("d1-readme", "completed", None, "#757", PROJECT_ID),
            ("d2-renames", "completed", None, "#759", PROJECT_ID),
            ("d3-readme-lane", "completed", "C", "#757", PROJECT_ID),
            ("d4-open", "completed", None, "#900", PROJECT_ID),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def env(tmp_path):
    """A temp state dir with the DB and a ROADMAP.yaml."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / bf.DB_FILENAME
    _make_db(db_path)
    roadmap_path = tmp_path / "ROADMAP.yaml"
    roadmap_path.write_text(ROADMAP_YAML, encoding="utf-8")
    return {"state_dir": state_dir, "db_path": db_path, "roadmap_path": roadmap_path}


def _snapshot(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        disp = dict(conn.execute("SELECT dispatch_id, track FROM dispatches").fetchall())
        tracks = dict(conn.execute("SELECT track_id, pr_ref FROM tracks").fetchall())
        phases = dict(conn.execute("SELECT track_id, phase FROM tracks").fetchall())
        events = conn.execute(
            "SELECT COUNT(*) FROM coordination_events WHERE event_type='pr_merged'"
        ).fetchone()[0]
        return {"disp": disp, "tracks": tracks, "phases": phases, "pr_merged": events}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ROADMAP loader
# ---------------------------------------------------------------------------


def test_load_roadmap_only_merged_prs(env):
    pr_map = bf.load_roadmap_pr_map(env["roadmap_path"])
    assert set(pr_map.keys()) == {"launch-readme", "launch-renames"}
    assert pr_map["launch-readme"]["merged_pr_ids"] == ["#757"]
    assert pr_map["launch-renames"]["representative"] == "#759"
    # open-feature's #900 is status=open -> excluded
    assert "open-feature" not in pr_map


def test_pr_to_feature_inversion(env):
    pr_map = bf.load_roadmap_pr_map(env["roadmap_path"])
    inv = bf._pr_to_feature(pr_map)
    assert inv == {"#757": "launch-readme", "#759": "launch-renames"}


# ---------------------------------------------------------------------------
# DIAGNOSE
# ---------------------------------------------------------------------------


def test_diagnose_reports_before_state(env):
    pr_map = bf.load_roadmap_pr_map(env["roadmap_path"])
    conn = sqlite3.connect(str(env["db_path"]))
    try:
        d = bf.diagnose(conn, pr_map, PROJECT_ID)
    finally:
        conn.close()
    assert d["roadmap_features_with_merged_pr"] == 2
    assert d["tracks_total"] == 3
    assert d["tracks_with_roadmap_pr"] == 2
    assert d["dispatches_total"] == 4
    assert d["dispatches_track_null"] == 3       # d1, d2, d4
    assert d["dispatches_track_set"] == 1        # d3 (lane C)
    assert d["dispatches_linkable"] == 2         # d1 (#757), d2 (#759); d4 #900 not merged
    assert d["pr_merged_events"] == 0


# ---------------------------------------------------------------------------
# DRY-RUN
# ---------------------------------------------------------------------------


def test_dry_run_leaves_db_untouched(env, capsys):
    before = _snapshot(env["db_path"])
    bf.dry_run(env["db_path"], env["roadmap_path"], PROJECT_ID)
    after = _snapshot(env["db_path"])
    assert before == after, "dry-run must not mutate the live/temp DB"
    out = capsys.readouterr().out
    assert "DRY-RUN MODE" in out
    assert "Live-DB untouched assertion: [ok]" in out


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------


def test_apply_links_and_flips_derived(env, capsys):
    bf.apply_to_live(env["db_path"], env["roadmap_path"], PROJECT_ID)
    snap = _snapshot(env["db_path"])

    # Step 1: relinked dispatches.
    assert snap["disp"]["d1-readme"] == "launch-readme"
    assert snap["disp"]["d2-renames"] == "launch-renames"
    # d4 maps to an OPEN pr (#900) -> not merged -> stays NULL.
    assert snap["disp"]["d4-open"] is None
    # Pre-existing non-null track preserved (never overwritten).
    assert snap["disp"]["d3-readme-lane"] == "C"

    # Step 2: track-level pr_ref evidence set where empty; existing one untouched.
    assert snap["tracks"]["launch-renames"] == "#759"
    assert snap["tracks"]["launch-readme"] == "#757"

    # Step 3: pr_merged events added (one per linked track).
    assert snap["pr_merged"] >= 2

    # Declared phase never written.
    assert snap["phases"] == {
        "launch-readme": "done",
        "launch-renames": "done",
        "open-feature": "queued",
    }

    # derived_status recomputed to 'done' for the two linked, terminal, merged tracks.
    conn = sqlite3.connect(str(env["db_path"]))
    conn.row_factory = sqlite3.Row
    try:
        derived = dict(
            (r["track_id"], r["derived_status"])
            for r in conn.execute("SELECT track_id, derived_status FROM tracks")
        )
    finally:
        conn.close()
    assert derived["launch-readme"] == "done"
    assert derived["launch-renames"] == "done"


def test_apply_writes_backup_and_integrity_ok(env, capsys):
    bf.apply_to_live(env["db_path"], env["roadmap_path"], PROJECT_ID)
    backups = list(env["db_path"].parent.glob(f"{bf.DB_FILENAME}.bak-linkage-*"))
    assert len(backups) == 1, "exactly one timestamped backup must be written"
    out = capsys.readouterr().out
    assert "Integrity check (live): [ok]" in out
    assert "Non-null track preservation: [ok]" in out

    # Integrity of the live DB after apply.
    conn = sqlite3.connect(str(env["db_path"]))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_apply_is_idempotent(env):
    bf.apply_to_live(env["db_path"], env["roadmap_path"], PROJECT_ID)
    snap1 = _snapshot(env["db_path"])
    bf.apply_to_live(env["db_path"], env["roadmap_path"], PROJECT_ID)
    snap2 = _snapshot(env["db_path"])
    assert snap1 == snap2, "second --apply must be a no-op (idempotent)"
    # No duplicate pr_merged events.
    assert snap1["pr_merged"] == snap2["pr_merged"]


def test_apply_never_overwrites_nonnull_track(env):
    bf.apply_to_live(env["db_path"], env["roadmap_path"], PROJECT_ID)
    conn = sqlite3.connect(str(env["db_path"]))
    try:
        track = conn.execute(
            "SELECT track FROM dispatches WHERE dispatch_id = 'd3-readme-lane'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert track == "C"


def test_apply_never_writes_roadmap(env):
    original = env["roadmap_path"].read_text(encoding="utf-8")
    bf.apply_to_live(env["db_path"], env["roadmap_path"], PROJECT_ID)
    assert env["roadmap_path"].read_text(encoding="utf-8") == original


def test_apply_never_writes_declared_phase(env):
    before = _snapshot(env["db_path"])["phases"]
    bf.apply_to_live(env["db_path"], env["roadmap_path"], PROJECT_ID)
    after = _snapshot(env["db_path"])["phases"]
    assert before == after


# ---------------------------------------------------------------------------
# Negative / edge paths
# ---------------------------------------------------------------------------


def test_load_roadmap_missing_features_key(tmp_path):
    rp = tmp_path / "ROADMAP.yaml"
    rp.write_text("roadmap_id: x\n", encoding="utf-8")
    assert bf.load_roadmap_pr_map(rp) == {}


def test_load_roadmap_features_not_a_list(tmp_path):
    rp = tmp_path / "ROADMAP.yaml"
    rp.write_text("features: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        bf.load_roadmap_pr_map(rp)


def test_backfill_no_track_row_skips_evidence(env):
    """A merged ROADMAP PR whose feature has no track row must not crash and must
    set no track-level evidence."""
    # Remove launch-renames track row; its dispatch d2 still carries #759.
    conn = sqlite3.connect(str(env["db_path"]))
    conn.execute("DELETE FROM tracks WHERE track_id = 'launch-renames'")
    conn.commit()
    conn.close()

    bf.apply_to_live(env["db_path"], env["roadmap_path"], PROJECT_ID)
    snap = _snapshot(env["db_path"])
    # Dispatch still relinks (additive), but no track row -> no pr_ref/event for it.
    assert snap["disp"]["d2-renames"] == "launch-renames"
    assert "launch-renames" not in snap["tracks"]


def test_main_dry_run_via_cli(env, capsys):
    rc = bf.main(
        [
            "--db", str(env["db_path"]),
            "--roadmap", str(env["roadmap_path"]),
            "--project-id", PROJECT_ID,
        ]
    )
    assert rc == 0
    # Default (no --apply) is dry-run -> DB untouched.
    snap = _snapshot(env["db_path"])
    assert snap["disp"]["d1-readme"] is None
