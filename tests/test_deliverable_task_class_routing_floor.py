"""tests/test_deliverable_task_class_routing_floor.py — the deliverable-tag door.

PR #1560 made the plan-gate READ `task_class`/`routing_floor` from a
deliverable's metadata (rubric axes 3/5, via `_format_deliverable_for_plan`).
Nothing could WRITE them: `vnx deliverable add --help` exposed only
`--objective`/`--output-kind`/`--title`, and no verb touched an EXISTING
deliverable's metadata at all. Every real `ready` deliverable therefore
rendered both fields as `(missing — not set on this deliverable)` regardless
of what an operator intended.

This suite proves the three cases the dispatch calls out, each of which fails
against the pre-fix CLI (no `--task-class`/`--routing-floor` flags on `add`,
no `set` verb, `list` never surfaced the fields):

  (a) `deliverable add --task-class ... --routing-floor ...` stores both, and
      `deliverable list` shows them (human + JSON).
  (b) `deliverable set <id> --task-class ... --routing-floor ...` on an
      ALREADY-EXISTING deliverable works, and does not overwrite `title` or
      any other pre-existing metadata key.
  (c) a deliverable with neither field keeps working exactly as before, and
      both `list` and the plan-gate plan text show them as explicitly missing
      — never silently omitted.

Plus the validation contract from point 4 of the dispatch: `task_class` is
checked against `smart_router.TASK_CLASSES` (the SAME closed set
`vnx horizon plan-gate run --task-class` already documents — see
HORIZON_PLANNING.md's `task_class == 01_code_generation` new-feature axis);
`routing_floor` is free text (no canonical vocabulary for it exists anywhere
in the repo — `plan_gate_panel.py` calls it a "quality FLOOR", not a closed
set), so it is never validated against a list.

Real-command-output proof of the plan-gate seeing tagged values (not
`(missing ...)`) lives in `test_set_tag_reaches_real_plan_gate_text` at the
bottom — the panel model call is stubbed (same pattern as
`test_plan_gate_deliverables_source.py`), but `cmd_deliverable_add` /
`cmd_plan_gate_run` / `resolve_plan_source` / `_format_deliverable_for_plan`
are all the real, unmodified functions.
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

import planning_cli  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402
import schema_migration  # noqa: E402
import smart_router  # noqa: E402
import tracks as tracks_lib  # noqa: E402

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402

PROJECT_ID = "test-proj"
_THICK_GOAL = "Ship a coherent plan for the widget. " * 10  # 390 chars


def _build_db(tmp_path: Path) -> Path:
    """Return a state_dir with migrations 0022 + 0024 + 0027 applied — same
    shape as test_deliverable_close.py / test_planning_deliverables.py."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)

    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
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
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch',
            entity_id TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT,
            metadata_json TEXT DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            project_id TEXT
        )
        """
    )
    conn.commit()

    for ver, fname in (
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()

    ensure_dispatches_columns(conn)
    conn.execute("PRAGMA user_version = 26")
    conn.commit()

    schema_migration.apply_script_if_below(
        conn,
        27,
        (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8"),
    )
    conn.commit()
    conn.close()
    return state_dir


@pytest.fixture()
def state_with_track(tmp_path: Path) -> tuple[Path, str]:
    state_dir = _build_db(tmp_path)
    track_id = "feat-alpha"
    tracks_lib.create_track(
        state_dir,
        track_id,
        PROJECT_ID,
        title="Feature Alpha",
        goal_state="ship Feature Alpha",
        phase="queued",
        horizon="now",
    )
    return state_dir, track_id


def _dispatch_ids(state_dir: Path, state: str) -> list[str]:
    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    rows = conn.execute(
        "SELECT dispatch_id FROM dispatches WHERE project_id = ? AND state = ? ORDER BY id",
        (PROJECT_ID, state),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _metadata(state_dir: Path, dispatch_id: str) -> dict:
    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    row = conn.execute(
        "SELECT metadata_json FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
        (dispatch_id, PROJECT_ID),
    ).fetchone()
    conn.close()
    return json.loads(row[0]) if row and row[0] else {}


# ---------------------------------------------------------------------------
# smart_router closed set sanity: the fixture below assumes these two values
# are real members. If the classifier's vocabulary ever changes, this fails
# loud here instead of silently validating against a stale assumption.
# ---------------------------------------------------------------------------

def test_fixture_task_classes_are_real_smart_router_values():
    assert "01_code_generation" in smart_router.TASK_CLASSES
    assert "02_code_review" in smart_router.TASK_CLASSES


# ---------------------------------------------------------------------------
# (a) `deliverable add` with both flags stores them, `list` shows them
# ---------------------------------------------------------------------------

def test_add_with_task_class_and_routing_floor_stored_and_listed(
    state_with_track: tuple[Path, str], capsys
):
    state_dir, track_id = state_with_track
    rc = planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "pr",
        "--title", "Implement the widget",
        "--task-class", "01_code_generation",
        "--routing-floor", "sonnet",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    add_out = capsys.readouterr().out
    assert "task_class   : 01_code_generation" in add_out
    assert "routing_floor: sonnet" in add_out

    dispatch_id = _dispatch_ids(state_dir, "proposed")[-1]
    meta = _metadata(state_dir, dispatch_id)
    assert meta["task_class"] == "01_code_generation"
    assert meta["routing_floor"] == "sonnet"
    assert meta["title"] == "Implement the widget"  # untouched

    rc = planning_cli.main([
        "deliverable", "list",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    list_out = capsys.readouterr().out
    assert "task_class: 01_code_generation" in list_out
    assert "routing_floor: sonnet" in list_out
    assert "(missing" not in list_out

    rc = planning_cli.main([
        "deliverable", "list",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
        "--json",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["task_class"] == "01_code_generation"
    assert data[0]["routing_floor"] == "sonnet"


def test_add_with_unknown_task_class_refused(state_with_track: tuple[Path, str], capsys):
    """Point 4: task_class IS validated against the smart-router closed set —
    an unknown value is refused loud, nothing is created."""
    state_dir, track_id = state_with_track
    rc = planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "pr",
        "--title", "Bad tag",
        "--task-class", "not-a-real-class",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown task_class" in err
    assert _dispatch_ids(state_dir, "proposed") == []


def test_add_routing_floor_is_free_text_not_validated(
    state_with_track: tuple[Path, str], capsys
):
    """routing_floor has NO canonical vocabulary anywhere in the repo (point
    4) — any non-empty value is accepted."""
    state_dir, track_id = state_with_track
    rc = planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "pr",
        "--title", "Free-text floor",
        "--routing-floor", "some-made-up-floor-value",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    capsys.readouterr()
    dispatch_id = _dispatch_ids(state_dir, "proposed")[-1]
    assert _metadata(state_dir, dispatch_id)["routing_floor"] == "some-made-up-floor-value"


# ---------------------------------------------------------------------------
# (b) `deliverable set` on an EXISTING deliverable, title untouched
# ---------------------------------------------------------------------------

def test_set_on_existing_deliverable_works_and_preserves_title(
    state_with_track: tuple[Path, str], capsys
):
    state_dir, track_id = state_with_track
    rc = planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "post",
        "--title", "Q3 launch post",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    capsys.readouterr()
    dispatch_id = _dispatch_ids(state_dir, "proposed")[-1]
    assert "task_class" not in _metadata(state_dir, dispatch_id)

    rc = planning_cli.main([
        "deliverable", "set", dispatch_id,
        "--task-class", "02_code_review",
        "--routing-floor", "opus",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "task_class=02_code_review" in out
    assert "routing_floor=opus" in out

    meta = _metadata(state_dir, dispatch_id)
    assert meta["task_class"] == "02_code_review"
    assert meta["routing_floor"] == "opus"
    assert meta["title"] == "Q3 launch post"  # NOT overwritten
    assert meta["deliverable"] is True  # NOT dropped


def test_set_partial_update_leaves_the_other_field_untouched(
    state_with_track: tuple[Path, str], capsys
):
    """Setting only --task-class does not clobber a routing_floor set earlier
    (and vice versa) — a patch, not a replace."""
    state_dir, track_id = state_with_track
    planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "post",
        "--title", "Partial update target",
        "--routing-floor", "kimi",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    capsys.readouterr()
    dispatch_id = _dispatch_ids(state_dir, "proposed")[-1]

    rc = planning_cli.main([
        "deliverable", "set", dispatch_id,
        "--task-class", "03_refactoring",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    capsys.readouterr()

    meta = _metadata(state_dir, dispatch_id)
    assert meta["task_class"] == "03_refactoring"
    assert meta["routing_floor"] == "kimi"  # untouched by the task_class-only set


def test_set_unknown_task_class_refused_no_change(
    state_with_track: tuple[Path, str], capsys
):
    state_dir, track_id = state_with_track
    planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "post",
        "--title", "Guard me",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    capsys.readouterr()
    dispatch_id = _dispatch_ids(state_dir, "proposed")[-1]

    rc = planning_cli.main([
        "deliverable", "set", dispatch_id,
        "--task-class", "definitely-not-in-the-closed-set",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown task_class" in err
    assert "task_class" not in _metadata(state_dir, dispatch_id)


def test_set_requires_at_least_one_field(state_with_track: tuple[Path, str], capsys):
    state_dir, track_id = state_with_track
    planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "post",
        "--title", "Nothing to set",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    capsys.readouterr()
    dispatch_id = _dispatch_ids(state_dir, "proposed")[-1]

    rc = planning_cli.main([
        "deliverable", "set", dispatch_id,
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "nothing to set" in err.lower()


def test_set_unknown_deliverable_fails_loud(state_with_track: tuple[Path, str], capsys):
    state_dir, _ = state_with_track
    rc = planning_cli.main([
        "deliverable", "set", "no-such-deliverable",
        "--task-class", "01_code_generation",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err


# ---------------------------------------------------------------------------
# (c) a deliverable with NEITHER field keeps working, shown as missing
# ---------------------------------------------------------------------------

def test_untagged_deliverable_still_works_and_shows_missing(
    state_with_track: tuple[Path, str], capsys
):
    state_dir, track_id = state_with_track
    rc = planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "doc",
        "--title", "No tags here",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    add_out = capsys.readouterr().out
    assert "task_class   : (not set)" in add_out
    assert "routing_floor: (not set)" in add_out

    dispatch_id = _dispatch_ids(state_dir, "proposed")[-1]
    meta = _metadata(state_dir, dispatch_id)
    assert "task_class" not in meta
    assert "routing_floor" not in meta
    assert meta["title"] == "No tags here"  # the deliverable is otherwise intact

    rc = planning_cli.main([
        "deliverable", "list",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "task_class: (missing" in out
    assert "routing_floor: (missing" in out

    # promote still works normally — tagging is orthogonal to the lifecycle gate.
    rc = planning_cli.main([
        "deliverable", "promote", dispatch_id,
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0


# ---------------------------------------------------------------------------
# Real-command-output proof: the plan-gate plan text sees the SET values.
# ---------------------------------------------------------------------------

def _bootstrap_plan_gate(tmp_path: Path) -> Path:
    """Same migration set as test_plan_gate_deliverables_source.py's
    _bootstrap — the plan-gate path additionally needs 28/29/30/33."""
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
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    ensure_dispatches_columns(conn)
    conn.commit()
    for version, filename in [
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
        (33, "0033_track_decision_ref.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.close()
    return state_dir


def _pass_result(track_id: str, project_id: str, panel: list) -> dict:
    return {
        "track_id": track_id, "project_id": project_id, "decision": "PASS",
        "summary": {"decision": "PASS", "pass_count": len(panel),
                    "revise_count": 0, "block_count": 0, "rationale": "ok"},
        "panelists": [], "doc_truncation": {"truncated": False},
    }


def _stub_panel(monkeypatch, tmp_path: Path, captured: list) -> None:
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")

    def _capture_run_panel(doc_path, *, doc_text=None, track_id, project_id, panel, data_dir, **kw):
        captured.append({"doc_path": doc_path, "doc_text": doc_text, "panel": panel})
        return _pass_result(track_id, project_id, panel)

    monkeypatch.setattr(pgp, "run_panel", _capture_run_panel)
    monkeypatch.setattr(planning_cli, "_resolve_plan_blocker", lambda *a, **k: True)
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)


def test_set_tag_reaches_real_plan_gate_text(tmp_path, monkeypatch, capsys):
    """(a)+(c) proved via real command output at the plan-gate boundary: a
    track with one tagged deliverable and one untagged deliverable gates
    successfully, and the text handed to the panel shows the tagged
    deliverable's real values while the untagged one still reads as missing —
    exactly the axis-3/5 gap #1560 flagged and this dispatch closes."""
    state_dir = _bootstrap_plan_gate(tmp_path)
    tracks_lib.create_track(state_dir, "feat-tagged", "p1", "t", _THICK_GOAL, phase="queued")

    rc_tagged = planning_cli.main([
        "deliverable", "add",
        "--objective", "feat-tagged", "--output-kind", "pr",
        "--title", "Tagged deliverable",
        "--task-class", "01_code_generation", "--routing-floor", "opus",
        "--project-id", "p1", "--state-dir", str(state_dir),
    ])
    assert rc_tagged == 0
    tagged_out = capsys.readouterr().out
    assert "task_class   : 01_code_generation" in tagged_out

    rc_untagged = planning_cli.main([
        "deliverable", "add",
        "--objective", "feat-tagged", "--output-kind", "doc",
        "--title", "Untagged deliverable",
        "--project-id", "p1", "--state-dir", str(state_dir),
    ])
    assert rc_untagged == 0
    capsys.readouterr()

    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)

    args = argparse.Namespace(
        track_id="feat-tagged", project_id="p1", state_dir=str(state_dir),
        doc=None, json=False, panel_seats=None,
    )
    rc = planning_cli.cmd_plan_gate_run(args)
    err = capsys.readouterr().err

    assert rc == 0
    assert len(captured) == 1
    doc_text = captured[0]["doc_text"]

    # Real command output — the SET values reach the panel-visible plan text.
    assert "task_class: 01_code_generation" in doc_text
    assert "routing_floor: opus" in doc_text
    # The untagged sibling still reads as explicitly missing, not omitted.
    assert "task_class: (missing" in doc_text
    assert "routing_floor: (missing" in doc_text
    assert "plan-gate source: track goal_state + 2 deliverable(s)" in err
