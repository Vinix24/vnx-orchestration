"""Characterisation of scripts/orphan_sweep.py::main (golf schoon R10)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS / "lib"))

from orphan_sweep import DEFAULT_MAX_ORPHANS, OrphanSweepResult

_spec = importlib.util.spec_from_file_location(
    "orphan_sweep_cli_r10", str(_SCRIPTS / "orphan_sweep.py")
)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.fixture
def calls(monkeypatch):
    seen = []
    result = OrphanSweepResult()

    def fake_sweep(**kwargs):
        seen.append(kwargs)
        return result

    monkeypatch.setattr(cli, "sweep", fake_sweep)
    monkeypatch.delenv("VNX_CURRENT_DISPATCH_ID", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    return seen, result


def test_defaults_reach_sweep_as_none(calls, capsys):
    seen, _ = calls
    assert cli.main([]) == 0
    kw = seen[0]
    assert kw["repo_root"] is None and kw["data_dir"] is None
    assert kw["state_dir"] is None and kw["account_data_root"] is None
    assert kw["project_id"] == "vnx-dev"
    assert kw["current_dispatch_id"] is None
    assert kw["max_orphans"] == DEFAULT_MAX_ORPHANS
    assert kw["dry_run"] is False
    assert capsys.readouterr().out == (
        "orphan_sweep: did kill 0 tmux session(s), reap 0 worktree(s), "
        "mark 0 dirty worktree(s), recover 0 active manifest(s)\n"
    )


def test_explicit_paths_and_flags(calls, tmp_path):
    seen, _ = calls
    cli.main([
        "--repo-root", str(tmp_path / "r"), "--data-dir", str(tmp_path / "d"),
        "--state-dir", str(tmp_path / "s"), "--account-data-root", str(tmp_path / "a"),
        "--project-id", "p1", "--current-dispatch", "dx", "--max-orphans", "3",
        "--dry-run",
    ])
    kw = seen[0]
    assert kw["repo_root"] == tmp_path / "r"
    assert kw["data_dir"] == tmp_path / "d"
    assert kw["state_dir"] == tmp_path / "s"
    assert kw["account_data_root"] == tmp_path / "a"
    assert (kw["project_id"], kw["current_dispatch_id"]) == ("p1", "dx")
    assert kw["max_orphans"] == 3 and kw["dry_run"] is True


def test_environment_defaults(calls, monkeypatch):
    seen, _ = calls
    monkeypatch.setenv("VNX_PROJECT_ID", "envproj")
    monkeypatch.setenv("VNX_CURRENT_DISPATCH_ID", "  d-env  ")
    cli.main([])
    assert seen[0]["project_id"] == "envproj"
    assert seen[0]["current_dispatch_id"] == "d-env"


def test_max_orphans_below_one_is_a_parser_error(calls, capsys):
    seen, _ = calls
    with pytest.raises(SystemExit) as exc:
        cli.main(["--max-orphans", "0"])
    assert exc.value.code == 2
    assert "--max-orphans must be >= 1" in capsys.readouterr().err
    assert seen == []


def test_json_output(calls, capsys):
    _, result = calls
    result.tmux_killed.append("vnx-a")
    assert cli.main(["--json"]) == 0
    out = capsys.readouterr().out
    assert out.endswith("\n") and out.count("\n") == 1
    assert out == json.dumps(result.to_dict(), separators=(",", ":")) + "\n"


def test_summary_dry_run_capped_with_errors_exits_one(calls, capsys):
    _, result = calls
    result.tmux_killed.append("vnx-a")
    result.worktrees_removed.extend(["w1", "w2"])
    result.worktrees_preserved.append("w3")
    result.active_recovered.append("m1")
    result.active_capped = True
    result.errors.append("boom")
    assert cli.main(["--dry-run", "--max-orphans", "4"]) == 1
    assert capsys.readouterr().out == (
        "orphan_sweep: would kill 1 tmux session(s), reap 2 worktree(s), "
        "mark 1 dirty worktree(s), recover 1 active manifest(s) (CAPPED at 4); "
        "1 error(s)\n"
    )


def test_verbose_sets_info_logging(calls, monkeypatch):
    import logging

    levels = []
    monkeypatch.setattr(
        logging, "basicConfig", lambda **kw: levels.append(kw["level"])
    )
    cli.main(["--verbose"])
    cli.main([])
    assert levels == [logging.INFO, logging.WARNING]
