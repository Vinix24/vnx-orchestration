"""tests/test_planning_turnon.py — planning-layer turn-on surface.

Covers the three turn-on pieces, all against temp DBs + ROADMAP fixtures
(NEVER the live .vnx-data DB):

- `vnx objective sync` CHECK mode reports the would-change set and mutates
  nothing; `--apply` mutates idempotently (2nd --apply = no-op).
- the flag-gated prelude hook `maybe_auto_seed`: unset -> no-op;
  VNX_AUTO_SEED_TRACKS=1 -> runs sync --apply (asserted via temp DB + a
  monkeypatched seeder recording apply=True).
- `vnx objective drift` reports the divergent set, writes planning_drift.json,
  exits 0 even when divergence > 0, and never touches any ROADMAP-write path.
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

import schema_migration  # noqa: E402
import seed_tracks_from_roadmap as seeder  # noqa: E402
import planning_cli  # noqa: E402
import tracks as tracks_lib  # noqa: E402


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


def _init_schema(state_dir: Path) -> None:
    """Build a v28 runtime_coordination.db (track layer + derived_status)."""
    state_dir.mkdir(parents=True, exist_ok=True)
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
        conn, 27,
        (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8"),
    )
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 28,
        (_MIGRATIONS / "0028_tracks_derived_status.sql").read_text(encoding="utf-8"),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def empty_state(tmp_path: Path) -> tuple[Path, Path]:
    """Schema-only state (no tracks) + a ROADMAP fixture file."""
    state_dir = tmp_path / "state"
    _init_schema(state_dir)
    roadmap = tmp_path / "ROADMAP.yaml"
    roadmap.write_text(SAMPLE_ROADMAP, encoding="utf-8")
    return state_dir, roadmap


@pytest.fixture()
def seeded_state(empty_state) -> tuple[Path, Path]:
    """Schema + tracks seeded from the ROADMAP fixture (apply=True)."""
    state_dir, roadmap = empty_state
    seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    return state_dir, roadmap


# ---------------------------------------------------------------------------
# Part 1 — vnx objective sync
# ---------------------------------------------------------------------------

def test_sync_check_mode_reports_would_change_and_does_not_mutate(empty_state, capsys):
    state_dir, roadmap = empty_state
    rc = planning_cli.main([
        "objective", "sync", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir), "--roadmap", str(roadmap), "--json",
    ])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["apply"] is False
    assert set(report["created"]) == {"feat-a", "feat-b", "feat-c"}
    # CHECK mode must not write anything.
    assert tracks_lib.list_tracks(state_dir, "vnx-dev") == []


def test_sync_apply_mutates_then_idempotent(empty_state, capsys):
    state_dir, roadmap = empty_state
    rc = planning_cli.main([
        "objective", "sync", "--apply", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir), "--roadmap", str(roadmap), "--json",
    ])
    assert rc == 0
    first = json.loads(capsys.readouterr().out)
    assert set(first["created"]) == {"feat-a", "feat-b", "feat-c"}
    seeded = {t["track_id"] for t in tracks_lib.list_tracks(state_dir, "vnx-dev")}
    assert seeded == {"feat-a", "feat-b", "feat-c"}

    # Second --apply is a no-op (idempotent): nothing created/updated.
    rc = planning_cli.main([
        "objective", "sync", "--apply", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir), "--roadmap", str(roadmap), "--json",
    ])
    assert rc == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created"] == []
    assert second["updated"] == []
    assert set(second["unchanged"]) == {"feat-a", "feat-b", "feat-c"}


def test_sync_missing_roadmap_returns_nonzero(empty_state, capsys):
    state_dir, _ = empty_state
    rc = planning_cli.main([
        "objective", "sync", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir), "--roadmap", str(state_dir / "nope.yaml"),
    ])
    assert rc == 1


# ---------------------------------------------------------------------------
# Part 2 — flag-gated auto-seed prelude hook
# ---------------------------------------------------------------------------

def test_auto_seed_unset_is_noop(empty_state):
    state_dir, roadmap = empty_state
    result = planning_cli.maybe_auto_seed(
        state_dir=state_dir, roadmap_path=roadmap, project_id="vnx-dev",
        env={},  # VNX_AUTO_SEED_TRACKS unset
    )
    assert result["skipped"] is True
    # No mutation when disabled.
    assert tracks_lib.list_tracks(state_dir, "vnx-dev") == []


def test_auto_seed_enabled_runs_sync_apply(empty_state):
    state_dir, roadmap = empty_state
    result = planning_cli.maybe_auto_seed(
        state_dir=state_dir, roadmap_path=roadmap, project_id="vnx-dev",
        env={"VNX_AUTO_SEED_TRACKS": "1"},
    )
    assert result["skipped"] is False
    assert result["summary"]["created"] == 3
    seeded = {t["track_id"] for t in tracks_lib.list_tracks(state_dir, "vnx-dev")}
    assert seeded == {"feat-a", "feat-b", "feat-c"}


def test_auto_seed_enabled_calls_seeder_with_apply_true(empty_state, monkeypatch):
    state_dir, roadmap = empty_state
    calls: list[dict] = []

    def _spy(s_dir, r_path, project_id, *, apply=False):
        calls.append({"apply": apply, "project_id": project_id})
        return {"summary": {"created": 0, "updated": 0, "unchanged": 0,
                            "phase_drift": 0, "orphan": 0}}

    monkeypatch.setattr(planning_cli.seeder, "seed", _spy)
    planning_cli.maybe_auto_seed(
        state_dir=state_dir, roadmap_path=roadmap, project_id="vnx-dev",
        env={"VNX_AUTO_SEED_TRACKS": "1"},
    )
    assert calls == [{"apply": True, "project_id": "vnx-dev"}]


# ---------------------------------------------------------------------------
# Part 3 — advisory drift-gate
# ---------------------------------------------------------------------------

def test_drift_reports_divergent_and_writes_state_file(seeded_state, capsys):
    state_dir, _ = seeded_state
    rc = planning_cli.main([
        "objective", "drift", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir), "--json",
    ])
    # Advisory: exit 0 even though divergence > 0.
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["divergent_count"] >= 1
    ids = {d["track_id"] for d in summary["divergent"]}
    # feat-b declared 'done' but blocked by unmet dependency feat-a.
    assert "feat-b" in ids
    feat_b = next(d for d in summary["divergent"] if d["track_id"] == "feat-b")
    assert feat_b["declared_phase"] == "done"
    assert feat_b["derived_status"] == "blocked"
    assert "feat-a" in feat_b["reason"]
    assert "linkage backfill" in summary["note"]

    # State file written atomically for the dashboard / T0.
    drift_path = state_dir / "planning_drift.json"
    assert drift_path.exists()
    on_disk = json.loads(drift_path.read_text(encoding="utf-8"))
    assert on_disk["divergent_count"] == summary["divergent_count"]


def test_drift_exit_zero_when_divergent(seeded_state):
    state_dir, _ = seeded_state
    rc = planning_cli.main([
        "objective", "drift", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir),
    ])
    assert rc == 0


def test_drift_never_writes_roadmap_or_calls_seeder(seeded_state, monkeypatch):
    state_dir, roadmap = seeded_state
    before_roadmap = roadmap.read_text(encoding="utf-8")
    before_phases = {
        t["track_id"]: t["phase"] for t in tracks_lib.list_tracks(state_dir, "vnx-dev")
    }

    # Drift must never reach any seeder write-path.
    def _boom(*a, **k):
        raise AssertionError("drift must not invoke the ROADMAP->tracks seeder")

    monkeypatch.setattr(planning_cli.seeder, "seed", _boom)

    rc = planning_cli.main([
        "objective", "drift", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir), "--json",
    ])
    assert rc == 0

    # ROADMAP file untouched.
    assert roadmap.read_text(encoding="utf-8") == before_roadmap
    # Declared phases untouched (reconciler writes only derived_status).
    after_phases = {
        t["track_id"]: t["phase"] for t in tracks_lib.list_tracks(state_dir, "vnx-dev")
    }
    assert after_phases == before_phases
