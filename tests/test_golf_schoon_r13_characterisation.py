"""Characterisation tests for golf-schoon R13 (extract-method, no behaviour change).

Pins the observable behaviour of ``cmd_objective_link_pr`` and ``_build_parser``
so the refactor can be shown green on both the old and the new code.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
_LIB = _SCRIPTS / "lib"
_MIGRATIONS = _ROOT / "schemas" / "migrations"
for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import schema_migration  # noqa: E402
import tracks as tracks_lib  # noqa: E402
from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


planning_cli = _load("golf_r13_planning_cli", "scripts/planning_cli.py")

PROJECT_ID = "r13-proj"


def _build_db(tmp_path: Path, delivery_table: bool = True) -> Path:
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
        schema_migration.apply_script_if_below(conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8"))
        conn.commit()
    ensure_dispatches_columns(conn)
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    for ver, fname in ((27, "0027_planning_horizon_and_deliverable_view.sql"),
                       (28, "0028_tracks_derived_status.sql")):
        schema_migration.apply_script_if_below(conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8"))
        conn.commit()
    if delivery_table:
        schema_migration.apply_script_if_below(
            conn, 32, (_MIGRATIONS / "0032_track_pr_delivery.sql").read_text(encoding="utf-8")
        )
        conn.commit()
    conn.close()
    return state_dir


def _args(sd: Path, track: str, *prs: str, json_out: bool = False, delivery: str = "partial",
          project_id: str = PROJECT_ID) -> argparse.Namespace:
    return argparse.Namespace(state_dir=str(sd), project_id=project_id, track_id=track,
                              pr=list(prs), json=json_out, delivery=delivery)


def _row(sd: Path, sql: str, params=()):
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _events(sd: Path) -> list[dict]:
    path = sd.parent / "events" / "track_events.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _track(sd: Path, pr_ref: str = "#100"):
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref=pr_ref)


# ---------------------------------------------------------------- link-pr

def test_link_pr_linked_table_output_and_writes(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _track(sd)
    rc = planning_cli.cmd_objective_link_pr(_args(sd, "T", "#397,398", "#100", delivery="complete"))
    out = capsys.readouterr()
    assert rc == 0
    assert out.out == (
        f"\nvnx objective link-pr — T (project '{PROJECT_ID}')\n"
        "  pr_ref: #100,#397,#398\n"
        "  + added: #397, #398\n"
        "  = already present: #100\n"
        "  delivery (complete) recorded for: #100, #397, #398\n\n"
    )
    assert out.err == ""
    assert _row(sd, "SELECT pr_ref FROM tracks WHERE track_id='T'") == [("#100,#397,#398",)]
    assert _row(sd, "SELECT pr_number, delivery_kind FROM track_pr_delivery ORDER BY pr_number") == [
        (100, "complete"), (397, "complete"), (398, "complete")]
    ev = [e for e in _events(sd) if e.get("event_type") == "track_pr_linked"]
    assert len(ev) == 1 and ev[0]["track_id"] == "T"


def test_link_pr_linked_json_payload(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _track(sd)
    rc = planning_cli.cmd_objective_link_pr(_args(sd, "T", "#5", json_out=True))
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "track_id": "T", "project_id": PROJECT_ID, "pr_ref": "#100,#5", "added": ["#5"],
        "already_present": [], "delivery": "partial", "delivery_written": True,
        "action": "linked", "applied": True,
    }


def test_link_pr_noop_upgrades_delivery_without_event(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _track(sd)
    planning_cli.cmd_objective_link_pr(_args(sd, "T", "#100"))
    capsys.readouterr()
    n_events = len([e for e in _events(sd) if e.get("event_type") == "track_pr_linked"])
    rc = planning_cli.cmd_objective_link_pr(_args(sd, "T", "#100", delivery="complete"))
    out = capsys.readouterr()
    assert rc == 0
    assert out.out == (
        f"\nvnx objective link-pr — T (project '{PROJECT_ID}')\n"
        "  pr_ref: #100\n"
        "  delivery (complete) recorded for: #100\n"
        "  all provided PR refs already present; no pr_ref change made.\n\n"
    )
    assert _row(sd, "SELECT delivery_kind FROM track_pr_delivery WHERE pr_number=100") == [("complete",)]
    assert len([e for e in _events(sd) if e.get("event_type") == "track_pr_linked"]) == n_events


def test_link_pr_noop_json_payload(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _track(sd)
    rc = planning_cli.cmd_objective_link_pr(_args(sd, "T", "100", json_out=True))
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "track_id": "T", "project_id": PROJECT_ID, "pr_ref": "#100", "added": [],
        "already_present": ["#100"], "delivery": "partial", "delivery_written": True,
        "action": "noop_no_change", "applied": False,
    }


def test_link_pr_missing_delivery_table_reports_error(tmp_path, capsys):
    sd = _build_db(tmp_path, delivery_table=False)
    _track(sd)
    rc = planning_cli.cmd_objective_link_pr(_args(sd, "T", "#7"))
    out = capsys.readouterr()
    assert rc == 0
    assert out.err == f"  ERROR: {planning_cli._DELIVERY_TABLE_MISSING_MSG}\n"
    assert "delivery (partial) recorded" not in out.out
    assert _row(sd, "SELECT pr_ref FROM tracks WHERE track_id='T'") == [("#100,#7",)]
    rc = planning_cli.cmd_objective_link_pr(_args(sd, "T", "#7", json_out=True))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["delivery_written"] is False
    assert payload["error"] == planning_cli._DELIVERY_TABLE_MISSING_MSG
    assert payload["action"] == "noop_no_change"


def test_link_pr_unknown_track_exit_1(tmp_path, capsys):
    sd = _build_db(tmp_path)
    rc = planning_cli.cmd_objective_link_pr(_args(sd, "nope", "#1"))
    out = capsys.readouterr()
    assert rc == 1 and out.out == ""
    assert out.err == (f"objective link-pr: track not found: 'nope' (project '{PROJECT_ID}'). "
                       "No change made.\n")


def test_link_pr_no_valid_refs_exit_2(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _track(sd, pr_ref="")
    rc = planning_cli.cmd_objective_link_pr(_args(sd, "T", "not-a-pr"))
    out = capsys.readouterr()
    assert rc == 2
    assert out.err == "objective link-pr: no valid PR refs provided for 'T'. No change made.\n"
    assert _row(sd, "SELECT pr_ref FROM tracks WHERE track_id='T'") in ([("",)], [(None,)])


# ---------------------------------------------------------------- _build_parser

def _subparser_names(parser, path):
    node = parser
    for name in path:
        sub = next(a for a in node._actions if isinstance(a, argparse._SubParsersAction))
        node = sub.choices[name]
    sub = next(a for a in node._actions if isinstance(a, argparse._SubParsersAction))
    return list(sub.choices)


def test_build_parser_objective_action_order():
    parser = planning_cli._build_parser()
    names = _subparser_names(parser, ["objective"])
    for earlier, later in [("show", "sync"), ("sync", "drift"), ("drift", "reconcile"),
                           ("set-lane-hint", "set-plan-scope"),
                           ("set-plan-scope", "reconcile-review"),
                           ("reconcile-review", "reconcile-streak")]:
        assert names.index(earlier) < names.index(later)
    assert "list" in names and names.index("list") < names.index("show")
    assert names.index("reconcile") < names.index("set-lane-hint")
    assert "link-pr" in names and "plan-gate" not in names
    assert "plan-gate" in list(next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices)


def test_build_parser_parses_and_binds_handlers(monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    p = planning_cli._build_parser()
    a = p.parse_args(["objective", "show", "T1", "--json"])
    assert a.func is planning_cli.cmd_objective_show
    assert (a.track_id, a.json, a.project_id, a.state_dir) == ("T1", True, "vnx-dev", "")
    a = p.parse_args(["horizon", "sync", "--apply", "--roadmap", "R.yaml"])
    assert a.func is planning_cli.cmd_objective_sync and a.apply and a.roadmap == "R.yaml"
    a = p.parse_args(["objective", "drift", "--repo-root", "/x"])
    assert a.func is planning_cli.cmd_objective_drift and a.repo_root == "/x"
    a = p.parse_args(["objective", "reconcile", "--apply", "--allow-closed-siblings",
                      "--max-gh-calls", "7"])
    assert a.func is planning_cli.cmd_objective_reconcile
    assert (a.apply, a.allow_closed_siblings, a.max_gh_calls, a.repo_root) == (True, True, 7, "")
    a = p.parse_args(["objective", "reconcile"])
    assert a.max_gh_calls == 50 and a.apply is False and a.allow_closed_siblings is False
    a = p.parse_args(["objective", "set-lane-hint", "T1", "governed"])
    assert a.func is planning_cli.cmd_objective_set_lane_hint and a.lane_hint == "governed"
    a = p.parse_args(["objective", "set-plan-scope", "T1", "light", "--reason", "why"])
    assert a.func is planning_cli.cmd_objective_set_plan_scope and a.reason == "why"
    a = p.parse_args(["objective", "reconcile-review", "run1", "--reviewer", "me",
                      "--verdict", "false-candidate", "--note", "n"])
    assert a.func is planning_cli.cmd_objective_reconcile_review
    assert (a.run_id, a.reviewer, a.verdict, a.note) == ("run1", "me", "false-candidate", "n")
    a = p.parse_args(["objective", "reconcile-streak"])
    assert a.func is planning_cli.cmd_objective_reconcile_streak


def test_build_parser_project_id_default_from_env(monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "from-env")
    a = planning_cli._build_parser().parse_args(["objective", "show", "T1"])
    assert a.project_id == "from-env"


@pytest.mark.parametrize("argv", [
    ["objective"], ["objective", "set-lane-hint", "T1", "bogus"],
    ["objective", "reconcile-review", "r", "--reviewer", "x", "--verdict", "maybe"],
    ["objective", "reconcile-review", "r", "--verdict", "ok"],
    ["objective", "reconcile-streak", "--max-gh-calls", "1"],
])
def test_build_parser_rejects_bad_input(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        planning_cli._build_parser().parse_args(argv)
    assert exc.value.code == 2
