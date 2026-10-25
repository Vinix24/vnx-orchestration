"""closure_track_fixture.py — a tracks DB for whole-feature closure tests (ADR-047 §3).

``verify_closure`` reads the closing track from runtime_coordination.db, not
from a plan file. This builds that store from the real migrations, the same
way tests/test_reconciler_delivery_hold.py does, and seeds one track.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, Optional

_ROOT = Path(__file__).resolve().parent.parent.parent
_LIB = _ROOT / "scripts" / "lib"
_MIGRATIONS = _ROOT / "schemas" / "migrations"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import schema_migration  # noqa: E402

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402

CLOSURE_PROJECT_ID = "closure-test"
CLOSURE_TRACK_ID = "demo-track"


def build_tracks_db(state_dir: Path) -> None:
    """Create runtime_coordination.db with the track layer up to migration 0032."""
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.execute("""
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
        """)
        conn.execute("""
            CREATE TABLE coordination_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL,
                from_state TEXT, to_state TEXT, actor TEXT NOT NULL DEFAULT 'runtime',
                reason TEXT, metadata_json TEXT DEFAULT '{}', occurred_at TEXT NOT NULL,
                project_id TEXT
            )
        """)
        for ver, fname in ((22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")):
            schema_migration.apply_script_if_below(conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8"))
            conn.commit()
        ensure_dispatches_columns(conn)
        conn.execute("PRAGMA user_version = 26")
        conn.commit()
        for ver, fname in (
            (27, "0027_planning_horizon_and_deliverable_view.sql"),
            (28, "0028_tracks_derived_status.sql"),
            (29, "0029_track_type_discriminator.sql"),
            (30, "0030_track_oi_resolved_at.sql"),
            (32, "0032_track_pr_delivery.sql"),
        ):
            schema_migration.apply_script_if_below(conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8"))
            conn.commit()
    finally:
        conn.close()


def _write(state_dir: Path, sql: str, params: tuple) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def seed_track(
    state_dir: Path,
    *,
    derived_status: Optional[str],
    pr_ref: Optional[str],
    track_id: str = CLOSURE_TRACK_ID,
    project_id: str = CLOSURE_PROJECT_ID,
    title: str = "Demo Feature",
) -> None:
    _write(
        state_dir,
        "INSERT INTO tracks (track_id, project_id, title, goal_state, phase, pr_ref, derived_status) "
        "VALUES (?, ?, ?, ?, 'active', ?, ?)",
        (track_id, project_id, title, f"{track_id} done", pr_ref, derived_status),
    )


def add_dispatch(
    state_dir: Path,
    *,
    dispatch_id: str,
    pr_ref: Optional[str],
    track: str = CLOSURE_TRACK_ID,
    project_id: str = CLOSURE_PROJECT_ID,
) -> None:
    _write(
        state_dir,
        "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref) VALUES (?, ?, 'completed', ?, ?)",
        (dispatch_id, project_id, track, pr_ref),
    )


def set_delivery(state_dir: Path, pr_number: int, kind: str, *, track_id: str = CLOSURE_TRACK_ID) -> None:
    _write(
        state_dir,
        "INSERT OR REPLACE INTO track_pr_delivery (project_id, track_id, pr_number, delivery_kind, set_by) "
        "VALUES (?, ?, ?, ?, 'operator')",
        (CLOSURE_PROJECT_ID, track_id, pr_number, kind),
    )


def link_blocker(state_dir: Path, oi_id: str, *, track_id: str = CLOSURE_TRACK_ID) -> None:
    """An unresolved ``blocks`` open item on the track: _compute_derived_status says blocked."""
    _write(
        state_dir,
        "INSERT INTO track_open_items (track_id, project_id, oi_id, link_type, link_source) "
        "VALUES (?, ?, ?, 'blocks', 'manual')",
        (track_id, CLOSURE_PROJECT_ID, oi_id),
    )


def record_merged(state_dir: Path, pr_numbers: Iterable[int]) -> None:
    """Append the ``pr_merged`` receipts the track reconciler reads as merge evidence."""
    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as fh:
        for number in pr_numbers:
            fh.write(json.dumps({"event_type": "pr_merged", "pr_number": number}) + "\n")


def seed_done_track(
    state_dir: Path, monkeypatch, pr_numbers: Iterable[int] = (45,), *, merged: bool = True
) -> None:
    """One done track read offline (no gh) in project CLOSURE_PROJECT_ID.

    ``merged=False`` leaves the merge evidence to the test, for tests that
    write their own ``pr_merged`` receipt and read the latest one back.
    """
    numbers = list(pr_numbers)
    monkeypatch.setenv("VNX_PROJECT_ID", CLOSURE_PROJECT_ID)
    monkeypatch.setenv("VNX_RECONCILE_GIT", "0")
    build_tracks_db(state_dir)
    seed_track(state_dir, derived_status="done", pr_ref=",".join(f"#{n}" for n in numbers))
    if merged:
        record_merged(state_dir, numbers)
