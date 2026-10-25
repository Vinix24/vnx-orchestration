"""tests/test_objective_list_blocked_count.py — `objective list --count-blocked`.

Cluster point 11 measures whether the number of blocked tracks falls from the 76
measured on 14-08. That comparison only holds while one definition produces the
number: every track whose STORED derived_status is 'blocked' (what
``objective list --all --json`` reports), over ALL tracks. Each blocked track is
counted ONCE, in the first matching category of the fixed order plan-gate (open
``OI-PLAN-*`` blocker) > dependency (unmet hard dependency) > other. ``--json``
lists every reason per track, so overlap stays visible.

Fixture: a real migrated tracks DB, with derived_status produced by the real
read-only derivation (``peek_derived_status``, merged-PR evidence pinned empty so
no ``gh`` call happens) — never hand-written, so the seeded blockers are what
makes each track blocked.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"
for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import planning_cli  # noqa: E402
import schema_migration  # noqa: E402
import track_reconciler  # noqa: E402
import tracks as tracks_lib  # noqa: E402

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402

PROJECT_ID = "test-proj"
OTHER_PROJECT_ID = "other-proj"


def _build_db(tmp_path: Path) -> Path:
    """Minimal modern tracks DB (migrations 22/24/27/28 + 0030 resolved_at)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev', state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT,
            gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT, actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT, metadata_json TEXT DEFAULT '{}', occurred_at TEXT NOT NULL, project_id TEXT
        )
        """
    )
    conn.commit()
    for ver, fname in ((22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8"))
        conn.commit()
    ensure_dispatches_columns(conn)
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    for ver, fname in (
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8"))
        conn.commit()
    conn.close()
    return state_dir


def _reconcile(state_dir: Path, project_id: str, track_ids: list[str]) -> None:
    """Write the REAL derived_status for each track, with no gh/network evidence."""
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    for track_id in track_ids:
        result = track_reconciler.peek_derived_status(
            state_dir, track_id, project_id, _merged_pr_numbers=frozenset())
        conn.execute(
            "UPDATE tracks SET derived_status = ? WHERE track_id = ? AND project_id = ?",
            (result["derived_status"], track_id, project_id),
        )
    conn.commit()
    conn.close()


def _add(state_dir: Path, track_id: str, project_id: str = PROJECT_ID) -> None:
    tracks_lib.create_track(state_dir, track_id, project_id, f"title {track_id}", "shipped")


def _resolve_plan_oi(state_dir: Path, track_id: str) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "UPDATE track_open_items SET resolved_at = '2026-01-01T00:00:00Z' "
        "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = 'blocks'",
        (track_id, PROJECT_ID, f"OI-PLAN-{track_id}"),
    )
    conn.commit()
    conn.close()


def _seeded_store(tmp_path: Path) -> Path:
    """One track per category, plus overlap, a lifted blocker and a foreign tenant."""
    state_dir = _build_db(tmp_path)
    for track_id in (
        "trk-plan-only", "trk-dep-only", "trk-both", "trk-other",
        "trk-lifted", "trk-light", "trk-dep-target",
    ):
        _add(state_dir, track_id)

    tracks_lib.link_open_item(
        state_dir, "trk-plan-only", PROJECT_ID, "OI-PLAN-trk-plan-only", "blocks", "manual")
    tracks_lib.link_open_item(
        state_dir, "trk-both", PROJECT_ID, "OI-PLAN-trk-both", "blocks", "manual")
    tracks_lib.add_dependency(
        state_dir, "trk-dep-only", PROJECT_ID, "trk-dep-target", PROJECT_ID, "hard", "manual")
    tracks_lib.add_dependency(
        state_dir, "trk-both", PROJECT_ID, "trk-dep-target", PROJECT_ID, "hard", "manual")
    tracks_lib.link_open_item(
        state_dir, "trk-other", PROJECT_ID, "OI-1234", "blocks", "manual")
    # A plan blocker that was lifted (resolved) must stop blocking — "open" matters.
    tracks_lib.link_open_item(
        state_dir, "trk-lifted", PROJECT_ID, "OI-PLAN-trk-lifted", "blocks", "manual")
    _resolve_plan_oi(state_dir, "trk-lifted")

    _reconcile(state_dir, PROJECT_ID, [
        "trk-plan-only", "trk-dep-only", "trk-both", "trk-other", "trk-lifted",
        "trk-light", "trk-dep-target",
    ])

    # A blocked track in ANOTHER tenant must never leak into this project's count.
    _add(state_dir, "trk-foreign", OTHER_PROJECT_ID)
    tracks_lib.link_open_item(
        state_dir, "trk-foreign", OTHER_PROJECT_ID, "OI-PLAN-trk-foreign", "blocks", "manual")
    _reconcile(state_dir, OTHER_PROJECT_ID, ["trk-foreign"])
    return state_dir


def _run(state_dir: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = planning_cli.main([
            "objective", "list", "--project-id", PROJECT_ID,
            "--state-dir", str(state_dir), *argv,
        ])
    return rc, out.getvalue(), err.getvalue()


def test_count_blocked_text_line(tmp_path):
    state_dir = _seeded_store(tmp_path)
    rc, out, err = _run(state_dir, "--count-blocked")
    assert rc == 0, err
    assert out.strip() == "4 blocked (2 plan-gate, 1 dependency, 1 other)"


def test_count_blocked_json_split_and_categories(tmp_path):
    state_dir = _seeded_store(tmp_path)
    rc, out, err = _run(state_dir, "--count-blocked", "--json")
    assert rc == 0, err
    data = json.loads(out)
    assert data["total"] == 4
    assert data["split"] == {"plan-gate": 2, "dependency": 1, "other": 1}

    by_track = {row["track_id"]: row for row in data["tracks"]}
    assert set(by_track) == {"trk-plan-only", "trk-dep-only", "trk-both", "trk-other"}
    assert by_track["trk-plan-only"]["category"] == "plan-gate"
    assert by_track["trk-dep-only"]["category"] == "dependency"
    assert by_track["trk-dep-only"]["reasons"] == [{
        "kind": "dependency", "track_id": "trk-dep-target", "phase": "queued", "edge_kind": "hard",
    }]
    assert by_track["trk-other"]["category"] == "other"
    assert by_track["trk-other"]["reasons"] == [{"kind": "other", "oi_id": "OI-1234"}]
    # A resolved plan blocker is not an open blocker: the track is not blocked.
    assert "trk-lifted" not in by_track
    assert "trk-light" not in by_track
    # Other tenant's blocked track is out of scope (ADR-007).
    assert "trk-foreign" not in by_track


def test_track_blocked_by_both_counts_once_under_plan_gate_and_shows_all_reasons(tmp_path):
    state_dir = _seeded_store(tmp_path)
    rc, out, err = _run(state_dir, "--count-blocked", "--json")
    assert rc == 0, err
    both = {row["track_id"]: row for row in json.loads(out)["tracks"]}["trk-both"]
    assert both["category"] == "plan-gate"
    kinds = {reason["kind"] for reason in both["reasons"]}
    assert kinds == {"plan-gate", "dependency"}
    assert both["reasons"] == [
        {"kind": "plan-gate", "oi_id": "OI-PLAN-trk-both"},
        {"kind": "dependency", "track_id": "trk-dep-target", "phase": "queued", "edge_kind": "hard"},
    ]
    # Counted exactly once: total equals the sum of the split.
    assert sum(json.loads(out)["split"].values()) == json.loads(out)["total"] == 4


def test_stored_blocked_without_a_live_reason_stays_blocking_under_other(tmp_path):
    """Fail-closed: an unexplained stored 'blocked' is counted, never dropped."""
    state_dir = _build_db(tmp_path)
    _add(state_dir, "trk-stale")
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "UPDATE tracks SET derived_status = 'blocked' WHERE track_id = 'trk-stale' "
        "AND project_id = ?", (PROJECT_ID,))
    conn.commit()
    conn.close()

    rc, out, err = _run(state_dir, "--count-blocked", "--json")
    assert rc == 0, err
    data = json.loads(out)
    assert data["total"] == 1
    assert data["split"] == {"plan-gate": 0, "dependency": 0, "other": 1}
    assert data["tracks"][0]["reasons"][0]["kind"] == "derived"


def test_scope_filters_are_refused_rather_than_silently_narrowing_the_number(tmp_path):
    state_dir = _seeded_store(tmp_path)
    rc, out, err = _run(state_dir, "--count-blocked", "--phase", "queued")
    assert rc == 2
    assert out == ""
    assert "do not apply" in err
