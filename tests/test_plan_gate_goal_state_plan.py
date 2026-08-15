"""tests/test_plan_gate_goal_state_plan.py — plan-gate accepts goal_state as the plan.

The plan-gate (`vnx horizon plan-gate run <track>`) previously required `--doc`,
so 66 of 80 blocked tracks whose plan already lives in their `goal_state` could
not be gated without duplicating that text into a file. This proves the real
`planning_cli.cmd_plan_gate_run` path:

  - runs WITHOUT `--doc`, judging the track's `goal_state`, and the output names
    that source;
  - `--doc` wins over a present goal, and the output says the goal was ignored;
  - a goal under the threshold is refused loud (exit != 0, length + threshold);
  - a whitespace-only goal above the threshold length is refused too;
  - a track with no goal and no `--doc` is refused with the same clarity;
  - the threshold is read from config, not a hardcoded constant.

Real model dispatch is out of scope (as in test_plan_gate_panel.py): `run_panel`
is stubbed with a capture, but the argparse/source-resolution/threshold logic is
the real `cmd_plan_gate_run`. Self-contained (tests/ has no __init__.py).
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


def _bootstrap(tmp_path: Path) -> Path:
    """A pre-migrated store (tracks + plan-gate schema), same as
    test_plan_gate_panel.py::_bootstrap."""
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
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
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
    """Stub model dispatch + seat config so cmd_plan_gate_run runs end-to-end
    against a real store without touching a live provider or the repo config."""
    # Absent config -> load_panel_seats falls back to DEFAULT_PANEL and
    # load_goal_min_chars falls back to DEFAULT_GOAL_MIN_CHARS (200).
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")

    def _capture_run_panel(doc_path, *, doc_text=None, track_id, project_id, panel, data_dir, **kw):
        captured.append({"doc_path": doc_path, "doc_text": doc_text, "panel": panel})
        return _pass_result(track_id, project_id, panel)

    monkeypatch.setattr(pgp, "run_panel", _capture_run_panel)
    monkeypatch.setattr(planning_cli, "_resolve_plan_blocker", lambda *a, **k: True)
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)


def _run(state_dir: Path, track_id: str, *, doc=None, goal_state="", captured=None, **extra):
    """Build a Namespace the way argparse would and run the real handler."""
    args = argparse.Namespace(
        track_id=track_id, project_id="p1", state_dir=str(state_dir),
        doc=doc, json=False, panel_seats=None,
        **extra,
    )
    rc = planning_cli.cmd_plan_gate_run(args)
    return rc


_THICK_GOAL = "Ship a coherent plan for the widget. " * 10  # 390 chars


def test_run_without_doc_uses_goal_state_and_names_source(tmp_path, monkeypatch, capsys):
    """A track with a thick goal gates without --doc, and the output names the
    source. Asserts the plan text actually handed to run_panel is the goal."""
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-goal", "p1", "t", _THICK_GOAL, phase="queued")
    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)

    rc = _run(state_dir, "feat-goal", goal_state=_THICK_GOAL, captured=captured)
    err = capsys.readouterr().err

    assert rc == 0
    assert len(captured) == 1
    assert captured[0]["doc_path"] is None
    assert captured[0]["doc_text"] == _THICK_GOAL.strip()
    assert "plan-gate source: track goal_state" in err


def test_run_with_doc_wins_over_goal_and_names_ignored(tmp_path, monkeypatch, capsys):
    """When both --doc and a goal are present, the doc wins and the output says
    the goal was ignored."""
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-docwins", "p1", "t", _THICK_GOAL, phase="queued")
    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)

    doc = tmp_path / "plan.md"
    doc.write_text("## Approach\nFrom the doc, not the goal.\n", encoding="utf-8")

    rc = _run(state_dir, "feat-docwins", doc=str(doc), goal_state=_THICK_GOAL, captured=captured)
    err = capsys.readouterr().err

    assert rc == 0
    assert len(captured) == 1
    assert str(captured[0]["doc_path"]) == str(doc)
    assert "## Approach" in captured[0]["doc_text"]
    assert "plan-gate source: doc" in err
    assert "track goal_state ignored" in err


def test_thin_goal_refused_with_length_and_threshold(tmp_path, monkeypatch, capsys):
    """A goal under the threshold is refused (exit != 0) naming both the
    measured length and the threshold."""
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-thin", "p1", "t", "too short", phase="queued")
    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)

    rc = _run(state_dir, "feat-thin", goal_state="too short", captured=captured)
    err = capsys.readouterr().err

    assert rc != 0
    assert captured == []  # the panel never ran
    assert "measured 9 meaningful chars" in err
    assert "threshold 200" in err
    assert "--doc" in err  # the remediation is named


def test_whitespace_only_goal_refused(tmp_path, monkeypatch, capsys):
    """A goal of pure whitespace above the threshold length is refused — the
    length is measured AFTER stripping, so 250 spaces measure 0."""
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-ws", "p1", "t", " " * 250, phase="queued")
    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)

    rc = _run(state_dir, "feat-ws", goal_state=" " * 250, captured=captured)
    err = capsys.readouterr().err

    assert rc != 0
    assert captured == []
    assert "measured 0 meaningful chars" in err
    assert "threshold 200" in err


def test_no_goal_and_no_doc_refused(tmp_path, monkeypatch, capsys):
    """A track with no goal and no --doc is refused with the same clarity
    (length 0 + threshold + remediation), not a bare exit."""
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-none", "p1", "t", "", phase="queued")
    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)

    rc = _run(state_dir, "feat-none", goal_state="", captured=captured)
    err = capsys.readouterr().err

    assert rc != 0
    assert captured == []
    assert "measured 0 meaningful chars" in err
    assert "threshold 200" in err
    assert "--doc" in err


def test_threshold_comes_from_config_not_hardcoded(tmp_path, monkeypatch, capsys):
    """The threshold is read from configs/plan_gate_panel.yaml, not a hardcoded
    200: with goal_min_chars=50 a 90-char goal passes (it would fail a hardcoded
    200), and a 30-char goal is refused at 50."""
    cfg = tmp_path / "panel.yaml"
    cfg.write_text("version: 1\ngoal_min_chars: 50\nseats: []\n", encoding="utf-8")

    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, "feat-ok", "p1", "t", "g" * 90, phase="queued")
    tracks.create_track(state_dir, "feat-short", "p1", "t", "g" * 30, phase="queued")
    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)
    # Override the config path _stub_panel just set to absent.yaml: the threshold
    # must come from this 50-char config. Seat composition is orthogonal to the
    # threshold; keep it deterministic via DEFAULT_PANEL.
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: cfg)
    monkeypatch.setattr(pgp, "load_panel_seats", lambda *a, **k: list(pgp.DEFAULT_PANEL))

    rc_ok = _run(state_dir, "feat-ok", goal_state="g" * 90, captured=captured)
    assert rc_ok == 0, "90 chars >= 50 must pass when the threshold is read from config"
    assert len(captured) == 1

    rc_short = _run(state_dir, "feat-short", goal_state="g" * 30, captured=captured)
    err = capsys.readouterr().err
    assert rc_short != 0
    assert "threshold 50" in err


def test_load_goal_min_chars_reads_config_key(tmp_path):
    """The loader reads the goal_min_chars key from the YAML, and falls back to
    the default when the key is absent."""
    cfg = tmp_path / "panel.yaml"
    cfg.write_text("version: 1\ngoal_min_chars: 321\nseats: []\n", encoding="utf-8")
    assert pgp.load_goal_min_chars(cfg) == 321

    no_key = tmp_path / "no_key.yaml"
    no_key.write_text("version: 1\nseats: []\n", encoding="utf-8")
    assert pgp.load_goal_min_chars(no_key) == pgp.DEFAULT_GOAL_MIN_CHARS

    assert pgp.load_goal_min_chars(tmp_path / "absent.yaml") == pgp.DEFAULT_GOAL_MIN_CHARS


def test_load_goal_min_chars_rejects_invalid_value(tmp_path):
    """A present-but-invalid threshold (<= 0, non-int) fails loud rather than
    silently disabling the refusal."""
    for bad in ("0", "-5", "abc"):
        cfg = tmp_path / f"bad_{bad}.yaml"
        cfg.write_text(f"version: 1\ngoal_min_chars: {bad}\nseats: []\n", encoding="utf-8")
        with pytest.raises(ValueError):
            pgp.load_goal_min_chars(cfg)


def test_run_argparse_accepts_missing_doc():
    """`--doc` is optional at the argparse layer: `plan-gate run <track>` parses
    with doc=None (the engine surface; the horizon surface parity is guarded by
    tests/test_horizon_parity.py)."""
    args = planning_cli._build_parser().parse_args(["plan-gate", "run", "track-id"])
    assert args.doc is None
    assert args.track_id == "track-id"
