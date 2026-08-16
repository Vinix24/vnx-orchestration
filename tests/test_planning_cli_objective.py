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

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns


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
    ensure_dispatches_columns(conn)
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
    assert "feat-c" in out
    # feat-b is done -> hidden by default (actionable-by-default view).
    assert "feat-b" not in out
    assert "(1 done hidden" in out
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
    # feat-b is done -> hidden by default; --all reveals it (see below).
    assert set(by_id) == {"feat-a", "feat-c"}
    assert by_id["feat-a"]["phase"] == "queued"  # planned -> queued
    assert by_id["feat-c"]["horizon"] == "later"


def test_objective_list_json_all_includes_done(seeded_state, capsys):
    rc = planning_cli.main([
        "objective", "list", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state), "--json", "--all",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    by_id = {d["track_id"]: d for d in data}
    assert set(by_id) == {"feat-a", "feat-b", "feat-c"}
    # feat-b depends on feat-a.
    assert by_id["feat-b"]["depends_on"] == ["feat-a"]


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


# ---------------------------------------------------------------------------
# objective show — pr_delivery visibility (OI-829)
# ---------------------------------------------------------------------------

def _apply_migration_0032(state_dir: Path) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    schema_migration.apply_script_if_below(
        conn, 32, (_MIGRATIONS / "0032_track_pr_delivery.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    conn.close()


def _set_delivery(state_dir: Path, track_id: str, pr_number: int, kind: str, project_id: str = "vnx-dev") -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO track_pr_delivery (project_id, track_id, pr_number, delivery_kind, set_by) "
        "VALUES (?,?,?,?,?)",
        (project_id, track_id, pr_number, kind, "operator"),
    )
    conn.commit()
    conn.close()


def test_objective_show_displays_pr_delivery_status(seeded_state, capsys):
    """#1221 partial, #1239 partial style breakdown must be readable from `show`."""
    _apply_migration_0032(seeded_state)
    tracks_lib.update_authored_fields(
        seeded_state, "feat-a", "vnx-dev", pr_ref="#1221,#1239", actor="operator",
    )
    _set_delivery(seeded_state, "feat-a", 1221, "partial")
    _set_delivery(seeded_state, "feat-a", 1239, "partial")

    rc = planning_cli.main([
        "objective", "show", "feat-a", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "delivery :" in out
    assert "#1221 partial" in out
    assert "#1239 partial" in out


def test_objective_show_json_includes_pr_delivery(seeded_state, capsys):
    _apply_migration_0032(seeded_state)
    tracks_lib.update_authored_fields(
        seeded_state, "feat-a", "vnx-dev", pr_ref="#1221,#1239", actor="operator",
    )
    _set_delivery(seeded_state, "feat-a", 1239, "complete")

    rc = planning_cli.main([
        "objective", "show", "feat-a", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state), "--json",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    by_pr = {e["pr_number"]: e["delivery_kind"] for e in data["pr_delivery"]}
    assert by_pr == {1221: "unmarked", 1239: "complete"}


def test_objective_show_unmarked_when_no_delivery_row(seeded_state, capsys):
    """pr_ref set but no track_pr_delivery row at all -> shown as 'unmarked', never as done."""
    _apply_migration_0032(seeded_state)
    tracks_lib.update_authored_fields(
        seeded_state, "feat-c", "vnx-dev", pr_ref="#999", actor="operator",
    )

    rc = planning_cli.main([
        "objective", "show", "feat-c", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "#999 unmarked" in out


def test_objective_show_no_pr_ref_shows_no_linked_prs(seeded_state, capsys):
    _apply_migration_0032(seeded_state)
    rc = planning_cli.main([
        "objective", "show", "feat-c", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "delivery : (no linked PRs)" in out


def test_objective_show_raises_on_unknown_delivery_kind(seeded_state):
    """An unrecognized delivery_kind on an existing row must fail loudly, never
    read as 'not complete' silently (mirrors close_track_if_done)."""
    _apply_migration_0032(seeded_state)
    tracks_lib.update_authored_fields(
        seeded_state, "feat-a", "vnx-dev", pr_ref="#777", actor="operator",
    )
    conn = sqlite3.connect(str(seeded_state / "runtime_coordination.db"))
    conn.execute("PRAGMA ignore_check_constraints = 1")
    conn.execute(
        "INSERT INTO track_pr_delivery (project_id, track_id, pr_number, delivery_kind, set_by) "
        "VALUES (?,?,?,?,?)",
        ("vnx-dev", "feat-a", 777, "bogus", "test"),
    )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="unrecognized delivery_kind"):
        planning_cli.main([
            "objective", "show", "feat-a", "--project-id", "vnx-dev",
            "--state-dir", str(seeded_state),
        ])


def test_objective_show_no_table_gracefully_shows_unmarked(seeded_state, capsys):
    """DB without migration 0032 applied: show must not crash — pr_delivery
    degrades to empty/'unmarked', matching the migration-not-applied fallback."""
    tracks_lib.update_authored_fields(
        seeded_state, "feat-c", "vnx-dev", pr_ref="#888", actor="operator",
    )
    rc = planning_cli.main([
        "objective", "show", "feat-c", "--project-id", "vnx-dev",
        "--state-dir", str(seeded_state),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "#888 unmarked" in out


# ---------------------------------------------------------------------------
# objective list/show — plan-gate review disposition (unread vs refused)
# ---------------------------------------------------------------------------

@pytest.fixture()
def review_seeded_state(seeded_state: Path) -> Path:
    """seeded_state + migrations 28/30/33 so the OI-PLAN blocker (resolved_at)
    and the durable decision_ref both exist, and the disposition can be derived."""
    conn = sqlite3.connect(str(seeded_state / "runtime_coordination.db"))
    for ver, fname in (
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
        (33, "0033_track_decision_ref.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()
    conn.close()
    return seeded_state


def _plan_blocker_oi(track_id: str) -> str:
    return f"OI-PLAN-{track_id}"


def _link_plan_blocker(state_dir: Path, track_id: str) -> None:
    tracks_lib.link_open_item(
        state_dir, track_id, "vnx-dev", _plan_blocker_oi(track_id), "blocks", "manual",
    )


def _set_refused(state_dir: Path, track_id: str) -> None:
    tracks_lib.set_decision_ref(
        state_dir, track_id, "vnx-dev",
        json.dumps({
            "decision": "REVISE",
            "reports": [{"seat": "opus"}],
            "rejected_alternatives": [{"alternative": "thin goal", "reason": "no plan"}],
            "set_at": "2026-08-16T00:00:00Z",
        }),
    )


class TestObjectiveListReviewSplit:
    def test_json_reports_unread_and_refused(self, review_seeded_state, capsys):
        _link_plan_blocker(review_seeded_state, "feat-a")       # never gated -> unread
        _link_plan_blocker(review_seeded_state, "feat-c")       # gated REVISE -> refused
        _set_refused(review_seeded_state, "feat-c")
        rc = planning_cli.main([
            "objective", "list", "--project-id", "vnx-dev",
            "--state-dir", str(review_seeded_state), "--json",
        ])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        by_id = {d["track_id"]: d for d in data}
        assert by_id["feat-a"]["plan_gate_review"] == "unread"
        assert by_id["feat-c"]["plan_gate_review"] == "refused"

    def test_text_shows_badges_and_total_equals_sum(self, review_seeded_state, capsys):
        _link_plan_blocker(review_seeded_state, "feat-a")
        _link_plan_blocker(review_seeded_state, "feat-c")
        _set_refused(review_seeded_state, "feat-c")
        rc = planning_cli.main([
            "objective", "list", "--project-id", "vnx-dev",
            "--state-dir", str(review_seeded_state),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "~blocked:unread" in out
        assert "~blocked:refused" in out
        # total (2) == unread (1) + refused (1), both numbers apart.
        assert "2 blocked on the plan gate (1 unread, 1 refused)" in out

    def test_cleared_tracks_get_no_badge_and_no_summary(self, review_seeded_state, capsys):
        rc = planning_cli.main([
            "objective", "list", "--project-id", "vnx-dev",
            "--state-dir", str(review_seeded_state),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "~blocked:" not in out
        assert "plan-gate review:" not in out


class TestObjectiveShowReviewSplit:
    def test_show_reports_unread(self, review_seeded_state, capsys):
        _link_plan_blocker(review_seeded_state, "feat-a")
        rc = planning_cli.main([
            "objective", "show", "feat-a", "--project-id", "vnx-dev",
            "--state-dir", str(review_seeded_state),
        ])
        assert rc == 0
        assert "plan-gate: unread" in capsys.readouterr().out

    def test_show_reports_refused_with_reason(self, review_seeded_state, capsys):
        _link_plan_blocker(review_seeded_state, "feat-c")
        _set_refused(review_seeded_state, "feat-c")
        rc = planning_cli.main([
            "objective", "show", "feat-c", "--project-id", "vnx-dev",
            "--state-dir", str(review_seeded_state),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "plan-gate: refused" in out
        assert "REVISE" in out  # the reason surfaces via decision_ref

    def test_show_json_reports_review(self, review_seeded_state, capsys):
        _link_plan_blocker(review_seeded_state, "feat-a")
        rc = planning_cli.main([
            "objective", "show", "feat-a", "--project-id", "vnx-dev",
            "--state-dir", str(review_seeded_state), "--json",
        ])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["plan_gate_review"] == "unread"

    def test_show_reports_cleared_when_no_blocker(self, review_seeded_state, capsys):
        rc = planning_cli.main([
            "objective", "show", "feat-a", "--project-id", "vnx-dev",
            "--state-dir", str(review_seeded_state),
        ])
        assert rc == 0
        assert "plan-gate: cleared" in capsys.readouterr().out


class TestReviewStateTransition:
    """A track crossing unread -> refused -> cleared, each leg shown via `show`."""

    def test_track_transitions_unread_refused_cleared(self, review_seeded_state, capsys):
        _link_plan_blocker(review_seeded_state, "feat-a")

        assert planning_cli.main([
            "objective", "show", "feat-a", "--project-id", "vnx-dev",
            "--state-dir", str(review_seeded_state)]) == 0
        assert "plan-gate: unread" in capsys.readouterr().out

        _set_refused(review_seeded_state, "feat-a")
        assert planning_cli.main([
            "objective", "show", "feat-a", "--project-id", "vnx-dev",
            "--state-dir", str(review_seeded_state)]) == 0
        assert "plan-gate: refused" in capsys.readouterr().out

        tracks_lib.unlink_open_item(
            review_seeded_state, "feat-a", "vnx-dev",
            _plan_blocker_oi("feat-a"), "blocks", reason="plan gate passed",
        )
        assert planning_cli.main([
            "objective", "show", "feat-a", "--project-id", "vnx-dev",
            "--state-dir", str(review_seeded_state)]) == 0
        assert "plan-gate: cleared" in capsys.readouterr().out
