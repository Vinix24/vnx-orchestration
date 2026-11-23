"""doorloop PR-13: ``vnx daily-metrics`` owns the job; ``vnx init`` never installs it.

The command is a thin pass-through to ``scripts/daily_metrics.py`` (decision 28) plus
``install`` for one opt-in per-project job at 04:10 (decisions 14 and 29). The real
install path runs against a fake engine root and a temp ``HOME``; ``launchctl`` is
stubbed at ``subprocess.run`` and never reaches the real domain.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from launchd_test_support import make_central_engine, register_engine  # noqa: E402
from vnx_cli import _engine  # noqa: E402
from vnx_cli.commands import daily_metrics as dm_cmd  # noqa: E402
from vnx_cli.commands import init_cmd  # noqa: E402

BIN_VNX = REPO / "bin" / "vnx"
PROJECT = "proj-p"
LABEL = "com.vnx.daily-metrics"


def _args(*argv: str) -> argparse.Namespace:
    return argparse.Namespace(daily_metrics_args=list(argv))


def _fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _central_engine(tmp_path: Path, home: Path, project_id: str) -> Path:
    """A central engine that is also the registered checkout of ``project_id``."""
    engine = tmp_path / "engine"
    make_central_engine(engine, real_scripts=REPO / "scripts")
    register_engine(home, engine, project_id)
    return engine


def _stub_launchctl(monkeypatch, label: str) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        result = MagicMock()
        result.stderr = ""
        result.stdout = f"-\t0\t{label}\n" if cmd[:2] == ["launchctl", "list"] else ""
        result.returncode = 0
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _launch_agents(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def _init_args(project_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(project_path=None, project_dir=str(project_dir),
                              project_id=None, template="default", force=False,
                              non_interactive=False, set_version=None)


# The entry: help works, and an existing verb on the same branch still works.
def test_bin_vnx_daily_metrics_help_names_install():
    result = subprocess.run([str(BIN_VNX), "daily-metrics", "--help"],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "install" in combined


def test_bin_vnx_existing_readonly_verb_still_exits_zero():
    result = subprocess.run([str(BIN_VNX), "list-projects"],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


# install
def test_install_renders_a_per_project_plist(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    engine = _central_engine(tmp_path, home, PROJECT)
    monkeypatch.setattr(_engine, "engine_root", lambda: engine)
    _stub_launchctl(monkeypatch, f"{LABEL}.{PROJECT}")

    rc = dm_cmd.vnx_daily_metrics(_args("install", "--project-id", PROJECT))

    assert rc == 0
    dest = _launch_agents(home) / f"{LABEL}.{PROJECT}.plist"
    data = plistlib.loads(dest.read_bytes())
    assert data["Label"] == f"{LABEL}.{PROJECT}"
    assert data["StartCalendarInterval"] == {"Hour": 4, "Minute": 10}
    assert data["ProgramArguments"] == [
        "/bin/bash",
        f"{engine}/scripts/launchd/vnx_pinned_launcher.sh",
        PROJECT,
        "scripts/daily_metrics.py",
        "daily",
    ]
    assert data["RunAtLoad"] is True
    assert data["StandardOutPath"] == f"/tmp/vnx-daily-metrics-{PROJECT}.log"
    assert data["StandardErrorPath"] == f"/tmp/vnx-daily-metrics-{PROJECT}.err"
    assert data["EnvironmentVariables"]["VNX_PROJECT_ID"] == PROJECT
    assert "${" not in dest.read_text(encoding="utf-8"), "a placeholder survived"


def test_install_twice_is_idempotent(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    engine = _central_engine(tmp_path, home, PROJECT)
    monkeypatch.setattr(_engine, "engine_root", lambda: engine)
    _stub_launchctl(monkeypatch, f"{LABEL}.{PROJECT}")

    first = dm_cmd.vnx_daily_metrics(_args("install", "--project-id", PROJECT))
    second = dm_cmd.vnx_daily_metrics(_args("install", "--project-id", PROJECT))

    assert (first, second) == (0, 0)
    assert sorted(p.name for p in _launch_agents(home).glob(f"{LABEL}.*")) == [
        f"{LABEL}.{PROJECT}.plist"
    ]


def test_install_fails_with_one_stderr_line_when_the_template_is_missing(
    tmp_path, monkeypatch, capsys
):
    home = _fake_home(tmp_path, monkeypatch)
    engine = tmp_path / "bare-engine"
    make_central_engine(engine)  # no scripts/ tree -> no template
    register_engine(home, engine, PROJECT)
    monkeypatch.setattr(_engine, "engine_root", lambda: engine)
    monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: engine)
    _stub_launchctl(monkeypatch, f"{LABEL}.{PROJECT}")

    rc = dm_cmd.vnx_daily_metrics(_args("install", "--project-id", PROJECT))

    assert rc == 1
    assert len(capsys.readouterr().err.strip().splitlines()) == 1
    assert not list(_launch_agents(home).glob(f"{LABEL}.*"))


# opt-in: vnx init installs no daily-metrics job
def test_vnx_init_installs_no_daily_metrics_job(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    engine = tmp_path / "init-engine"
    make_central_engine(engine)
    for directory in ("templates", "scripts", "schemas"):
        os.symlink(REPO / directory, engine / directory, target_is_directory=True)
    monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: engine)
    _stub_launchctl(monkeypatch, "com.vnx.gate-obligation-runner.unused")

    assert init_cmd.vnx_init(_init_args(tmp_path / "project")) == 0

    installed = sorted(p.name for p in _launch_agents(home).glob("*.plist"))
    assert not [name for name in installed if name.startswith(f"{LABEL}.")], installed
    # Guard against a vacuous pass: init's own install path did run.
    assert [name for name in installed if name.startswith("com.vnx.gate-obligation-runner.")]


# pass-through
def test_passthrough_returns_the_scripts_exit_code():
    assert dm_cmd.vnx_daily_metrics(_args("not-a-verb")) == 2


def test_passthrough_help_exits_zero():
    assert dm_cmd.vnx_daily_metrics(_args("collect", "--help")) == 0


def test_passthrough_survives_the_fabric_pythonpath(monkeypatch):
    """`bin/vnx` exports the engine's scripts/lib on PYTHONPATH. Without `-P` the
    script's sibling ``scripts/daily_metrics.py`` shadows the same-named package
    and the run dies with a circular import (measured on the real entrance)."""
    monkeypatch.setenv("PYTHONPATH", str(REPO / "scripts" / "lib"))
    assert dm_cmd.vnx_daily_metrics(_args("collect", "--help")) == 0


def test_no_verb_is_a_usage_error():
    assert dm_cmd.vnx_daily_metrics(_args()) == 2


def test_install_without_project_id_exits_two():
    with pytest.raises(SystemExit) as excinfo:
        dm_cmd.vnx_daily_metrics(_args("install"))
    assert excinfo.value.code == 2


# a launch failure of the pass-through script is a structured error, not a traceback
def test_passthrough_start_failure_is_a_structured_error(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", boom)

    rc = dm_cmd.vnx_daily_metrics(_args("collect"))

    captured = capsys.readouterr()
    assert rc == 1
    assert "could not be started" in captured.err
    assert "boom" in captured.err
    assert "Traceback" not in captured.err


# the help and description name only `install`: `uninstall` returns in PR-13b
def test_daily_metrics_help_and_description_name_only_install():
    import vnx_cli.main as main_mod

    parser = main_mod._SuggestionArgumentParser(prog="vnx")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    main_mod._register_daily_metrics_subparser(subparsers)
    sub_help = subparsers._name_parser_map["daily-metrics"].format_help()
    combined = (parser.format_help() + sub_help).lower()

    assert "install" in combined
    assert "uninstall" not in combined
