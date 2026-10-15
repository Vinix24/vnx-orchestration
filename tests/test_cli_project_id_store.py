"""tests/test_cli_project_id_store.py — an explicit --project-id selects that project's store.

Reparatiegolf R3 (OI-2049, ADR-007 tenant isolation). Before the fix
``vnx deliverable|horizon|objective|track`` opened the store of ``--project-dir``
whatever ``--project-id`` said; from $HOME that is the shared store frozen before
ADR-026. Every test runs under a tmp HOME and tmp stores: no real ``~/.vnx-data``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts" / "lib"), str(REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_horizon_cli import _bootstrap_store  # noqa: E402

from vnx_cli import _engine  # noqa: E402
from vnx_cli.main import main as vnx_main  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A tmp HOME with no ambient store override; the CWD is HOME (the default `.`)."""
    for key in (
        "VNX_DATA_HOME", "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_PROJECT_ID",
        "VNX_STATE_DIR", "VNX_CANONICAL_ROOT", "PROJECT_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.chdir(h)
    return h


def _seed(state_dir: Path, project_id: str, track_id: str) -> None:
    _bootstrap_store(state_dir)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state, phase) "
            "VALUES (?, ?, ?, ?, 'queued')",
            (track_id, project_id, f"Title {track_id}", "goal for the row"),
        )
        conn.commit()
    finally:
        conn.close()


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["vnx", *argv])
    with pytest.raises(SystemExit) as exc:
        vnx_main()
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


def _marker(directory: Path, project_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".vnx-project-id").write_text(project_id + "\n", encoding="utf-8")
    return directory


def _ids(out: str) -> set[str]:
    return {d["track_id"] for d in json.loads(out)}


# (a) explicit --project-id from a tmp HOME gives <root>/<pid>/state

def test_resolver_gives_the_project_store_from_home(home):
    state = _engine.resolve_project_state_dir(home, "alpha")
    assert state == (home / ".vnx-data" / "alpha" / "state").resolve()


def test_resolver_ignores_a_shared_store_under_home(home):
    """The pre-ADR-026 shared store `~/.vnx-data` exists; alpha still gets its own."""
    (home / ".vnx-data" / "state").mkdir(parents=True)
    state = _engine.resolve_project_state_dir(home, "alpha")
    assert state == (home / ".vnx-data" / "alpha" / "state").resolve()


def test_resolver_without_project_id_keeps_the_project_dir_behaviour(home):
    state = _engine.resolve_project_state_dir(home)
    assert state == _engine.resolve_data_root(home) / "state"


@pytest.mark.parametrize(
    "argv_prefix",
    [["deliverable"], ["horizon", "deliverable"], ["horizon"], ["objective"]],
)
def test_planning_commands_open_the_project_store(home, monkeypatch, capsys, argv_prefix):
    _seed(home / ".vnx-data" / "alpha" / "state", "alpha", "trk-alpha")
    verb = ["list", "--project-id", "alpha", "--json"]
    rc, out, err = _run(monkeypatch, capsys, [*argv_prefix, *verb])
    assert rc == 0, err
    assert not (home / ".vnx-data" / "state").exists()


def test_track_list_opens_the_project_store(home, monkeypatch, capsys):
    _seed(home / ".vnx-data" / "alpha" / "state", "alpha", "trk-alpha")
    rc, out, err = _run(monkeypatch, capsys, ["track", "list", "--project-id", "alpha"])
    assert rc == 0, err
    assert "trk-alpha" in out


# (b) configured store override: same project honoured, other project refused by name

def test_override_of_the_same_project_is_honoured(home, monkeypatch, capsys):
    store = home / ".vnx-data" / "alpha"
    _seed(store / "state", "alpha", "trk-alpha")
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(store))
    rc, out, err = _run(monkeypatch, capsys, ["horizon", "list", "--project-id", "alpha", "--json"])
    assert rc == 0, err
    assert _ids(out) == {"trk-alpha"}


def test_override_outside_the_central_root_is_the_operators_choice(home, tmp_path, monkeypatch, capsys):
    store = tmp_path / "elsewhere"
    _seed(store / "state", "alpha", "trk-alpha")
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(store))
    rc, out, err = _run(monkeypatch, capsys, ["horizon", "list", "--project-id", "alpha", "--json"])
    assert rc == 0, err
    assert _ids(out) == {"trk-alpha"}


@pytest.mark.parametrize("argv", [["horizon", "list"], ["deliverable", "list"], ["track", "list"]])
def test_override_of_another_project_is_refused_by_name(home, monkeypatch, capsys, argv):
    store_b = home / ".vnx-data" / "beta"
    _seed(store_b / "state", "beta", "trk-beta")
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(store_b))
    rc, out, err = _run(monkeypatch, capsys, [*argv, "--project-id", "alpha"])
    assert rc == 2
    assert "alpha" in err and "beta" in err
    assert "trk-beta" not in out


# (c) --project-id A never reads project B's store: two populated stores

@pytest.mark.parametrize("argv", [["horizon", "list"], ["deliverable", "list"], ["track", "list"]])
def test_project_id_never_reads_the_other_projects_store(home, monkeypatch, capsys, argv):
    _seed(home / ".vnx-data" / "alpha" / "state", "alpha", "trk-alpha")
    _seed(home / ".vnx-data" / "beta" / "state", "beta", "trk-beta")
    for pid, mine, other in (("alpha", "trk-alpha", "trk-beta"), ("beta", "trk-beta", "trk-alpha")):
        rc, out, err = _run(monkeypatch, capsys, [*argv, "--project-id", pid, "--all-projects"
                                                  if argv[0] == "track" else "--json"])
        assert rc == 0, err
        if argv[0] == "horizon":
            assert _ids(out) == {mine}
        elif argv[0] == "track":
            assert mine in out and other not in out
        assert other not in out


# (d) default --project-dir . is not refused; an explicit one of another project is

def test_default_project_dir_with_explicit_project_id_is_not_refused(home, monkeypatch, capsys):
    _marker(home, "beta")
    _seed(home / ".vnx-data" / "alpha" / "state", "alpha", "trk-alpha")
    rc, out, err = _run(monkeypatch, capsys, ["horizon", "list", "--project-id", "alpha", "--json"])
    assert rc == 0, err
    assert _ids(out) == {"trk-alpha"}


@pytest.mark.parametrize("argv", [["horizon", "list"], ["deliverable", "list"], ["track", "list"]])
def test_explicit_project_dir_of_another_project_is_refused_by_name(home, monkeypatch, capsys, argv):
    proj_b = _marker(home / "proj-b", "beta")
    _seed(home / ".vnx-data" / "alpha" / "state", "alpha", "trk-alpha")
    rc, out, err = _run(monkeypatch, capsys, [*argv, "--project-id", "alpha", "--project-dir", str(proj_b)])
    assert rc == 2
    assert "alpha" in err and "beta" in err
    assert "trk-alpha" not in out


def test_explicit_project_dir_of_the_same_project_is_accepted(home, monkeypatch, capsys):
    proj_a = _marker(home / "proj-a", "alpha")
    _seed(home / ".vnx-data" / "alpha" / "state", "alpha", "trk-alpha")
    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "list", "--project-id", "alpha", "--project-dir", str(proj_a), "--json"])
    assert rc == 0, err
    assert _ids(out) == {"trk-alpha"}


def test_invalid_project_id_is_refused(home):
    with pytest.raises(_engine.ProjectStoreError):
        _engine.resolve_project_state_dir(home, "../escape")


def test_a_project_dir_only_caller_keeps_its_behaviour(home, monkeypatch, capsys):
    proj_a = _marker(home / "proj-a", "alpha")
    _seed(home / ".vnx-data" / "alpha" / "state", "alpha", "trk-alpha")
    rc, out, err = _run(monkeypatch, capsys, ["horizon", "list", "--project-dir", str(proj_a), "--json"])
    assert rc == 0, err
    assert _ids(out) == {"trk-alpha"}
