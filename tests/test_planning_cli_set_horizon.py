"""`objective set-horizon` — move an existing track to now|next|later (OI-2272).

Only `objective add --horizon` could set a horizon, so moving an existing track
meant editing the coordination DB; the operator decision of 09-11-2026 (two
existing tracks to `now`) needs this audited verb.

Verifies: the move shows in `objective show` / `list --horizon`, the event
carries old and new, the same value is "unchanged" with no event, unknown value
and unknown track change nothing, another project's same track_id is untouched
(ADR-007), a pre-0027 store refuses, the pip CLI routes to the same engine.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT, _ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import schema_migration  # noqa: E402
import tracks as tracks_lib  # noqa: E402
import planning_cli  # noqa: E402

from fixtures.dispatches_schema_fixture import dispatches_ddl  # noqa: E402
from vnx_cli.main import main as vnx_main  # noqa: E402

PID = "vnx-dev"
_MIGRATIONS = _ROOT / "schemas" / "migrations"


def _bootstrap(tmp_path: Path, *, horizon: bool = True) -> Path:
    """A store migrated through 0022/0024 (+0027/0028 for the horizon ledger)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(dispatches_ddl())
    steps = [(22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")]
    if horizon:
        steps += [(27, "0027_planning_horizon_and_deliverable_view.sql"),
                  (28, "0028_tracks_derived_status.sql")]
    for version, filename in steps:
        schema_migration.apply_script_if_below(
            conn, version, (_MIGRATIONS / filename).read_text(encoding="utf-8"))
        conn.commit()
    conn.close()
    return state_dir


def _track(state_dir: Path, track_id: str, horizon, project_id: str = PID) -> None:
    tracks_lib.create_track(
        state_dir, track_id, project_id, f"Track {track_id}", "shipped", horizon=horizon)


def _cli(state_dir: Path, *argv: str, project_id: str = PID) -> tuple[int, str, str]:
    """Run planning_cli with an explicit store; argparse errors raise SystemExit."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = planning_cli.main([*argv, "--project-id", project_id, "--state-dir", str(state_dir)])
    return rc, out.getvalue(), err.getvalue()


def _set(state_dir: Path, track_id: str, horizon: str, project_id: str = PID) -> int:
    return planning_cli.main([
        "objective", "set-horizon", track_id, horizon,
        "--project-id", project_id, "--state-dir", str(state_dir)])


def _show(state_dir: Path, track_id: str, project_id: str = PID) -> dict:
    rc, out, err = _cli(state_dir, "objective", "show", track_id, "--json", project_id=project_id)
    assert rc == 0, err
    return json.loads(out)


def _events(state_dir: Path, track_id: str | None = None) -> list[dict]:
    path = state_dir.parent / "events" / "track_events.ndjson"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [e for e in (json.loads(line) for line in lines if line.strip())
            if e["event_type"] == "track_horizon_set"
            and (track_id is None or e["track_id"] == track_id)]


def test_move_later_to_now_and_repeat_is_unchanged(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path)
    _track(state_dir, "rekenslot", "later")

    assert _set(state_dir, "rekenslot", "now") == 0
    out = capsys.readouterr().out
    assert "Set horizon=now on rekenslot" in out and "later" in out
    assert _show(state_dir, "rekenslot")["horizon"] == "now"

    def listed(h):
        return {r["track_id"] for r in json.loads(
            _cli(state_dir, "objective", "list", "--horizon", h, "--json")[1])}
    assert "rekenslot" in listed("now") and "rekenslot" not in listed("later")

    assert _set(state_dir, "rekenslot", "now") == 0  # already there
    assert "unchanged" in capsys.readouterr().out
    assert len(_events(state_dir)) == 1


def test_event_carries_old_and_new_horizon(tmp_path):
    state_dir = _bootstrap(tmp_path)
    _track(state_dir, "rekenslot", "later")
    _track(state_dir, "unscheduled", None)

    assert _set(state_dir, "rekenslot", "now") == 0
    assert _set(state_dir, "unscheduled", "later") == 0
    assert [e["details"] for e in _events(state_dir)] == [
        {"old_horizon": "later", "new_horizon": "now"},
        {"old_horizon": None, "new_horizon": "later"},
    ]
    first = _events(state_dir, "rekenslot")[0]
    assert (first["project_id"], first["actor"]) == (PID, "operator")


def test_refusals_exit_non_zero_and_change_nothing(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path)
    _track(state_dir, "rekenslot", "later")

    with pytest.raises(SystemExit) as exc:  # argparse choices
        _set(state_dir, "rekenslot", "someday")
    assert exc.value.code != 0
    with pytest.raises(ValueError):  # the DAL's own guard, below the CLI
        tracks_lib.set_horizon(state_dir, "rekenslot", PID, "someday")
    assert _show(state_dir, "rekenslot")["horizon"] == "later"

    assert _set(state_dir, "does-not-exist", "now") == 2
    assert "track not found" in capsys.readouterr().err
    assert _events(state_dir) == []


def test_other_project_with_same_track_id_is_untouched(tmp_path):
    state_dir = _bootstrap(tmp_path)
    _track(state_dir, "shared-track", "later", project_id="proj-a")
    _track(state_dir, "shared-track", "next", project_id="proj-b")

    assert _set(state_dir, "shared-track", "now", project_id="proj-a") == 0
    assert _show(state_dir, "shared-track", "proj-a")["horizon"] == "now"
    assert _show(state_dir, "shared-track", "proj-b")["horizon"] == "next"
    assert [e["project_id"] for e in _events(state_dir)] == ["proj-a"]

    # The DAL's WHERE clause is the guard, not the CLI's read-then-write.
    with pytest.raises(tracks_lib.TrackNotFoundError):
        tracks_lib.set_horizon(state_dir, "shared-track", "proj-c", "later")
    assert _show(state_dir, "shared-track", "proj-b")["horizon"] == "next"


def test_store_without_horizon_column_refuses(tmp_path, capsys):
    state_dir = _bootstrap(tmp_path, horizon=False)
    _track(state_dir, "old-store-track", None)

    assert _set(state_dir, "old-store-track", "now") == 1
    assert "migration 0027" in capsys.readouterr().err
    with pytest.raises(tracks_lib.HorizonColumnMissingError):
        tracks_lib.set_horizon(state_dir, "old-store-track", PID, "now")

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
        row = conn.execute("SELECT track_id FROM tracks WHERE track_id = ? AND project_id = ?",
                           ("old-store-track", PID)).fetchone()
    finally:
        conn.close()
    assert "horizon" not in columns and row is not None
    assert _events(state_dir) == []


def test_pip_cli_routes_set_horizon_to_the_same_engine(tmp_path, monkeypatch, capsys):
    """`vnx horizon set-horizon` (central store) delegates to the engine function."""
    from vnx_cli.commands import horizon as horizon_mod

    for key in ("VNX_DATA_HOME", "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_PROJECT_ID",
                "VNX_STATE_DIR", "VNX_CANONICAL_ROOT", "PROJECT_ROOT"):
        monkeypatch.delenv(key, raising=False)  # deterministic store + project resolution
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    data_root = tmp_path / "central-data"
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
    monkeypatch.chdir(project_dir)
    state_dir = _bootstrap(data_root)
    _track(state_dir, "rekenslot", "later", project_id="horizon-test")
    argv = ["horizon", "set-horizon", "rekenslot", "now",
            "--project-id", "horizon-test", "--project-dir", str(project_dir)]

    calls = []

    def _stub(args):
        calls.append(args)
        return 42

    engine = planning_cli.cmd_objective_set_horizon
    monkeypatch.setattr(planning_cli, "cmd_objective_set_horizon", _stub)
    monkeypatch.setattr(sys, "argv", ["vnx", *argv])
    with pytest.raises(SystemExit) as exc:
        vnx_main()
    assert exc.value.code == 42
    assert [(a.track_id, a.horizon, a.project_id) for a in calls] == [
        ("rekenslot", "now", "horizon-test")]
    assert horizon_mod._VERB_DISPATCH["set-horizon"] is horizon_mod._cmd_set_horizon

    # The real route moves the track in the central store it resolved.
    monkeypatch.setattr(planning_cli, "cmd_objective_set_horizon", engine)
    with pytest.raises(SystemExit) as exc:
        vnx_main()
    assert exc.value.code == 0, capsys.readouterr().err
    assert tracks_lib.get_track(state_dir, "rekenslot", "horizon-test")["horizon"] == "now"
    assert _events(state_dir, "rekenslot")[0]["details"] == {
        "old_horizon": "later", "new_horizon": "now"}
