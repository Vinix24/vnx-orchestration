"""tests/test_planning_turnon.py — planning-layer turn-on surface.

Covers the three turn-on pieces, all against temp DBs + ROADMAP fixtures
(NEVER the live .vnx-data DB):

- `vnx objective sync --roadmap <file>` CHECK mode reports the would-change set
  and mutates nothing; `--apply` mutates idempotently (2nd --apply = no-op).
- `vnx objective sync` WITHOUT `--roadmap` exits 2 and writes nothing: the
  tracks DB is the plan source (ADR-047), a file is imported only when named.
  The flag-gated auto-seed hook (`maybe_auto_seed`) is gone with it — its tests
  were removed here (C2, dlv-8c87936d5e6a).
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

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402


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
    ensure_dispatches_columns(conn)
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


def test_sync_without_roadmap_exits_2_and_writes_nothing(empty_state, capsys):
    """No derived default any more (ADR-047): the tracks DB is the source, a
    file is imported only when the operator names it."""
    state_dir, _ = empty_state
    rc = planning_cli.main([
        "objective", "sync", "--apply", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir), "--json",
    ])
    assert rc == 2
    assert tracks_lib.list_tracks(state_dir, "vnx-dev") == []
    captured = capsys.readouterr()
    assert "--roadmap" in captured.err
    assert "ADR-047" in captured.err
    assert captured.out == ""


def test_sync_without_roadmap_ignores_a_planted_root_file(empty_state, capsys, monkeypatch):
    """Even a ROADMAP.yaml in the CWD/git root is not picked up implicitly."""
    state_dir, _ = empty_state
    monkeypatch.chdir(state_dir.parent)
    (state_dir.parent / "ROADMAP.yaml").write_text(SAMPLE_ROADMAP, encoding="utf-8")
    rc = planning_cli.main([
        "objective", "sync", "--apply", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir),
    ])
    assert rc == 2
    assert tracks_lib.list_tracks(state_dir, "vnx-dev") == []


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


def test_drift_exit_zero_and_stdout_even_when_state_write_fails(seeded_state, monkeypatch, capsys):
    """Advisory contract: drift must exit 0 and emit its report even if the
    planning_drift.json write fails (permissions error, disk full, etc.)."""
    state_dir, _ = seeded_state

    def _raise(*_a, **_kw):
        raise OSError("simulated write failure")

    monkeypatch.setattr(planning_cli, "_atomic_write_json", _raise)

    rc = planning_cli.main([
        "objective", "drift", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir), "--json",
    ])
    assert rc == 0

    out = capsys.readouterr().out
    summary = json.loads(out)
    assert "divergent_count" in summary
    assert "total_tracks" in summary


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


# ---------------------------------------------------------------------------
# Part 4 — drift reason: plan-gate unread vs refused (OI-823)
# ---------------------------------------------------------------------------
#
# `_drift_reason` collapsed every 'blocked' track behind an open
# OI-PLAN-<track> blocker to the single string "blocked by open item" —
# whether the plan-gate had never looked at the plan (queue depth, not a real
# problem) or had reviewed it and REFUSED (REVISE/BLOCK, a finding on
# record). Measured on the live store 2026-09-04: 88 of 91 divergent
# `vnx objective drift` rows carried that identical reason, all 88 backed by
# an OI-PLAN-<track> blocker, splitting ~72 unread / ~16 refused via
# `plan_gate_enforcement.classify_review_state` — the SAME classifier
# `cmd_objective_list` already uses for its unread/refused badge (see
# tests/test_planning_cli_objective.py::TestObjectiveListReviewSplit). A
# signal where 88/91 rows say the same thing sends no signal; this section
# proves `objective drift`'s per-row reason distinguishes the two, and keeps
# the ordinary non-plan-gate "blocked by open item" path distinct from both.

@pytest.fixture()
def plan_gate_seeded_state(seeded_state: tuple[Path, Path]) -> tuple[Path, Path]:
    """seeded_state + migrations 29/30/33 so the OI-PLAN blocker (resolved_at)
    and the durable decision_ref both exist and drift's reason can classify
    unread vs refused (mirrors review_seeded_state in
    tests/test_planning_cli_objective.py)."""
    state_dir, roadmap = seeded_state
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    for ver, fname in (
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
        (33, "0033_track_decision_ref.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()
    conn.close()
    return state_dir, roadmap


def test_drift_reason_distinguishes_unread_from_refused_plan_gate(plan_gate_seeded_state, capsys):
    state_dir, _ = plan_gate_seeded_state

    # feat-a: OI-PLAN blocker seeded, never reviewed -> unread.
    tracks_lib.link_open_item(
        state_dir, "feat-a", "vnx-dev", "OI-PLAN-feat-a", "blocks", "manual",
    )
    # feat-c: OI-PLAN blocker seeded AND a recorded REVISE decision -> refused.
    tracks_lib.link_open_item(
        state_dir, "feat-c", "vnx-dev", "OI-PLAN-feat-c", "blocks", "manual",
    )
    tracks_lib.set_decision_ref(
        state_dir, "feat-c", "vnx-dev",
        json.dumps({
            "decision": "REVISE", "reports": [], "rejected_alternatives": [],
            "set_at": "2026-09-04T00:00:00Z",
        }),
    )

    rc = planning_cli.main([
        "objective", "drift", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir), "--json",
    ])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    by_id = {d["track_id"]: d["reason"] for d in summary["divergent"]}

    assert "feat-a" in by_id
    assert "feat-c" in by_id
    reason_a = by_id["feat-a"]
    reason_c = by_id["feat-c"]

    # The core complaint: two structurally different facts must not collapse
    # to the same string.
    assert reason_a != reason_c
    assert "unread" in reason_a.lower()
    assert "refused" in reason_c.lower()
    assert "OI-PLAN-feat-a" in reason_a
    assert "OI-PLAN-feat-c" in reason_c


def test_drift_reason_keeps_generic_open_item_distinct_from_plan_gate(plan_gate_seeded_state, capsys):
    """A genuine (non-plan-gate) blocking open item must still read as a
    plain 'blocked by open item' — never absorbed into the plan-gate
    unread/refused split, and never silently indistinguishable from it."""
    state_dir, _ = plan_gate_seeded_state
    tracks_lib.link_open_item(
        state_dir, "feat-c", "vnx-dev", "OI-9001", "blocks", "manual",
    )

    rc = planning_cli.main([
        "objective", "drift", "--project-id", "vnx-dev",
        "--state-dir", str(state_dir), "--json",
    ])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    by_id = {d["track_id"]: d["reason"] for d in summary["divergent"]}

    assert "feat-c" in by_id
    reason = by_id["feat-c"]
    assert "OI-9001" in reason
    assert "unread" not in reason.lower()
    assert "refused" not in reason.lower()
