"""tests/test_plan_gate_deliverables_source.py — the plan-gate reads deliverables.

Before this fix, `resolve_plan_source` had exactly two sources: `--doc` and the
track's bare `goal_state`. Rubric axes 3 (deliverables, scoped/task_class) and 5
(routing FLOOR per deliverable) were structurally unanswerable — the panelists
were never shown the track's deliverables at all, even when the track had them.

Proves, against `planning_cli.resolve_plan_source` directly (pure function, no
DB — same contract as `goal_state` already had) and against the real
`cmd_plan_gate_run` command (real store, model dispatch stubbed):

  (a) a track WITH deliverables produces plan text that contains them (id,
      output_kind, title, status), and explicitly marks task_class/routing_floor
      as missing rather than omitting them when the deliverable does not carry
      them;
  (b) a track with ZERO deliverables and no --doc is refused, and the refusal
      names the deliverables — not the goal length — as the reason. This is
      REFUSED_NO_DELIVERABLES, a bucket distinct from REFUSED_THIN;
  (c) `--doc` still wins over goal+deliverables and still sets `ignored_goal`.

Each of (a)/(b)/(c) fails against the pre-fix `resolve_plan_source` (no
`deliverables` parameter existed, and a thick-goal/zero-deliverable track was
never refused) and passes against the fix.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
_SCRIPTS = REPO_ROOT / "scripts"
_MIGRATIONS = REPO_ROOT / "schemas" / "migrations"
for _p in (str(_LIB), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import schema_migration  # noqa: E402
import tracks  # noqa: E402
import planning_cli  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402

_THICK_GOAL = "Ship a coherent plan for the widget. " * 10  # 390 chars


# ---------------------------------------------------------------------------
# Part 1: unit tests directly against resolve_plan_source — no DB, no CLI.
# ---------------------------------------------------------------------------

def _deliverable(
    ref="dlv-abc123", output_kind="post", title="Q3 launch post",
    status="proposed", metadata=None,
):
    return {
        "deliverable_ref": ref,
        "output_kind": output_kind,
        "title": title,
        "derived_status": status,
        "metadata": metadata or {},
    }


def test_resolve_plan_source_includes_deliverables_in_plan_text():
    """(a) A track with deliverables produces plan text containing them: id,
    output_kind, title, status all appear, and task_class/routing_floor (absent
    on this deliverable) are marked missing rather than silently dropped."""
    result = planning_cli.resolve_plan_source(
        doc_text=None,
        goal_state=_THICK_GOAL,
        deliverables=[_deliverable()],
        min_goal_chars=200,
        track_id="feat-alpha",
    )
    assert result.source == "goal"
    assert _THICK_GOAL.strip() in result.plan_text
    assert "dlv-abc123" in result.plan_text
    assert "post" in result.plan_text
    assert "Q3 launch post" in result.plan_text
    assert "proposed" in result.plan_text
    # Never silently omitted — an absent field reads as absent, not missing text.
    assert "task_class: (missing" in result.plan_text
    assert "routing_floor: (missing" in result.plan_text


def test_resolve_plan_source_surfaces_task_class_and_routing_floor_when_present():
    """When a deliverable DOES carry task_class/routing_floor in its metadata,
    the actual values show up instead of the missing-marker."""
    d = _deliverable(metadata={"task_class": "backend-safe", "routing_floor": "sonnet"})
    result = planning_cli.resolve_plan_source(
        doc_text=None, goal_state=_THICK_GOAL, deliverables=[d],
        min_goal_chars=200, track_id="feat-alpha",
    )
    assert "task_class: backend-safe" in result.plan_text
    assert "routing_floor: sonnet" in result.plan_text
    assert "(missing" not in result.plan_text


def test_resolve_plan_source_refuses_zero_deliverables_naming_deliverables():
    """(b) A thick goal with ZERO deliverables and no --doc is refused — and the
    reason names deliverables, not goal length. Distinct from the thin-goal
    refusal (same exception type, different `kind`)."""
    with pytest.raises(planning_cli.PlanRefusal) as excinfo:
        planning_cli.resolve_plan_source(
            doc_text=None, goal_state=_THICK_GOAL, deliverables=[],
            min_goal_chars=200, track_id="feat-empty",
        )
    exc = excinfo.value
    assert exc.kind == "no_deliverables"
    assert "deliverable" in str(exc).lower()
    assert "feat-empty" in str(exc)
    # This is NOT the thin-goal message — the goal clears the threshold fine.
    assert "too thin" not in str(exc)


def test_thin_goal_refusal_still_fires_before_deliverables_check():
    """The pre-existing thin-goal refusal is untouched and still fires first —
    a too-thin goal is refused for its own reason even with deliverables absent
    or present."""
    with pytest.raises(planning_cli.PlanRefusal) as excinfo:
        planning_cli.resolve_plan_source(
            doc_text=None, goal_state="too short", deliverables=[_deliverable()],
            min_goal_chars=200, track_id="feat-thin",
        )
    exc = excinfo.value
    assert exc.kind == "thin_goal"
    assert exc.length == len("too short")
    assert exc.threshold == 200


def test_doc_still_wins_and_sets_ignored_goal_unit():
    """(c) --doc wins explicitly over goal+deliverables, even with zero
    deliverables (which would otherwise refuse) — and `ignored_goal` is set."""
    result = planning_cli.resolve_plan_source(
        doc_text="## From the doc\nNot the goal.",
        goal_state=_THICK_GOAL,
        deliverables=[],
        min_goal_chars=200,
        track_id="feat-docwins",
    )
    assert result.source == "doc"
    assert result.plan_text == "## From the doc\nNot the goal."
    assert result.ignored_goal is True


# ---------------------------------------------------------------------------
# Part 2: integration — the real `cmd_plan_gate_run` against a real store,
# model dispatch stubbed (same pattern as test_plan_gate_goal_state_plan.py).
# ---------------------------------------------------------------------------

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
    # Migration 0022 REBUILDS the dispatches table, so output_ref/output_kind
    # (needed by the 0027 deliverables view) must be added AFTER it runs —
    # same ordering as test_plan_gate_batch.py::_bootstrap.
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


def _run(state_dir: Path, track_id: str, *, doc=None, **extra):
    args = argparse.Namespace(
        track_id=track_id, project_id="p1", state_dir=str(state_dir),
        doc=doc, json=False, panel_seats=None,
        **extra,
    )
    return planning_cli.cmd_plan_gate_run(args)


def test_track_with_deliverable_gates_and_panel_sees_it(tmp_path, monkeypatch, capsys):
    """(a) integration: a real track with a real deliverable (added via `vnx
    deliverable add`) gates successfully without --doc, and the text handed to
    the panel contains the deliverable's title and output_kind."""
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-with-dlv", "p1", "t", _THICK_GOAL, phase="queued")
    rc_add = planning_cli.main([
        "deliverable", "add",
        "--objective", "feat-with-dlv", "--output-kind", "pr",
        "--title", "Implement the widget", "--project-id", "p1",
        "--state-dir", str(state_dir),
    ])
    assert rc_add == 0
    capsys.readouterr()

    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)

    rc = _run(state_dir, "feat-with-dlv")
    err = capsys.readouterr().err

    assert rc == 0
    assert len(captured) == 1
    doc_text = captured[0]["doc_text"]
    assert _THICK_GOAL.strip() in doc_text
    assert "Implement the widget" in doc_text
    assert "pr" in doc_text
    assert "plan-gate source: track goal_state + 1 deliverable(s)" in err


def test_track_without_deliverables_refused_naming_deliverables(tmp_path, monkeypatch, capsys):
    """(b) integration: a thick-goal track with NO deliverables and no --doc is
    refused (exit != 0) before the panel ever runs, and the message names
    deliverables — not goal length."""
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-no-dlv", "p1", "t", _THICK_GOAL, phase="queued")
    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)

    rc = _run(state_dir, "feat-no-dlv")
    err = capsys.readouterr().err

    assert rc != 0
    assert captured == []  # the panel never ran — no seats burned
    assert "deliverable" in err.lower()
    assert "too thin" not in err


def test_doc_wins_over_goal_and_deliverables_integration(tmp_path, monkeypatch, capsys):
    """(c) integration: --doc wins even for a track with zero deliverables
    (which would otherwise refuse), and the output says the goal was ignored."""
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-doc-wins2", "p1", "t", _THICK_GOAL, phase="queued")
    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)

    doc = tmp_path / "plan.md"
    doc.write_text("## Approach\nFrom the doc, not the goal.\n", encoding="utf-8")

    rc = _run(state_dir, "feat-doc-wins2", doc=str(doc))
    err = capsys.readouterr().err

    assert rc == 0
    assert len(captured) == 1
    assert "## Approach" in captured[0]["doc_text"]
    assert "plan-gate source: doc" in err
    assert "track goal_state ignored" in err
