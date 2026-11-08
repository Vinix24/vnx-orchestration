"""tests/test_launchd_project_daemon_cwd.py: a project-bound daemon starts in its checkout.

OI-2653 / OI-2616. ``vnx_pinned_launcher.sh`` used to ``cd`` into the resolved ENGINE tree
(the read-only release under ``~/.vnx-system/versions/<v>``) and run the script there. A
project-bound daemon then resolved its project root from the engine tree and ``vnx_paths``
refused ("cannot write project state under immutable central install"); the
gate-obligation-runner ran ``git fetch`` in the release tree and hit a read-only
``.git/FETCH_HEAD``.

The launcher now runs ``$ENGINE_ROOT/<script>`` with cwd = the project checkout, exports
``PROJECT_ROOT`` and ``VNX_PROJECT_ROOT`` (the variable ``vnx_paths`` reads as an explicit
project root) set to the checkout, and keeps ``VNX_HOME`` on the engine.

The engine tree here is really read-only (``chmod -w``). The scripts are tiny probes that
source/import the REAL ``vnx_paths`` and write nothing. No test calls ``launchctl`` or
touches ``~/Library/LaunchAgents``; registry and store live under a tmp ``HOME``.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Iterator

import pytest

from launchd_test_support import pinned_store, register_engine

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "scripts" / "launchd" / "vnx_pinned_launcher.sh"
PROJECT = "daemon-cwd"
SH_SCRIPT = "scripts/probe_daemon.sh"
PY_SCRIPT = "scripts/probe_daemon.py"

SH_PROBE = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    'echo "cwd=$(pwd -P)"\n'
    'echo "PROJECT_ROOT_ENV=${PROJECT_ROOT:-}"\n'
    'echo "VNX_HOME=${VNX_HOME:-}"\n'
    'source "$VNX_HOME/scripts/lib/vnx_paths.sh"\n'
    'echo "resolved=$PROJECT_ROOT"\n'
)

PY_PROBE = (
    "import os, sys\n"
    "from pathlib import Path\n"
    "print('cwd=' + str(Path.cwd().resolve()))\n"
    "print('PROJECT_ROOT_ENV=' + os.environ.get('PROJECT_ROOT', ''))\n"
    "print('VNX_HOME=' + os.environ.get('VNX_HOME', ''))\n"
    "sys.path.insert(0, os.path.join(os.environ['VNX_HOME'], 'scripts', 'lib'))\n"
    "import vnx_paths\n"
    "print('resolved=' + vnx_paths.resolve_paths()['PROJECT_ROOT'])\n"
)


def _set_readonly(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _set_writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        path.chmod(path.stat().st_mode | stat.S_IWUSR)


class World:
    def __init__(self, home: Path, engine: Path, checkout: Path) -> None:
        self.home = home
        self.engine = engine
        self.checkout = checkout

    def run(self, script: str, *extra: str) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            "VNX_HOME": str(self.home / ".vnx-system" / "current"),
        }
        return subprocess.run(
            ["bash", str(LAUNCHER), PROJECT, script, *extra],
            capture_output=True, text=True, env=env, timeout=60, cwd=str(self.home),
        )


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    versions = pinned_store(
        home, ["v1.8.0"], current="v1.8.0",
        scripts={SH_SCRIPT: SH_PROBE, PY_SCRIPT: PY_PROBE},
    )
    engine = versions / "v1.8.0"
    (engine / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    (engine / "scripts" / "lib").symlink_to(REPO / "scripts" / "lib", target_is_directory=True)
    checkout = tmp_path / "checkout"
    register_engine(home, checkout, PROJECT)
    _set_readonly(engine)
    try:
        yield World(home, engine.resolve(), checkout.resolve())
    finally:
        _set_writable(engine)


def _fields(result: subprocess.CompletedProcess) -> dict:
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_sh_daemon_runs_in_the_checkout_with_project_root(world: World) -> None:
    result = world.run(SH_SCRIPT)

    assert result.returncode == 0, result.stdout + result.stderr
    fields = _fields(result)
    assert fields["cwd"] == str(world.checkout)
    assert fields["PROJECT_ROOT_ENV"] == str(world.checkout)
    assert fields["VNX_HOME"] == str(world.engine)
    assert fields["resolved"] == str(world.checkout)


def test_py_daemon_runs_in_the_checkout_and_vnx_paths_resolves_it(world: World) -> None:
    result = world.run(PY_SCRIPT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PROJECT_ROOT mis-detected" not in result.stderr
    fields = _fields(result)
    assert fields["cwd"] == str(world.checkout)
    assert fields["PROJECT_ROOT_ENV"] == str(world.checkout)
    assert fields["VNX_HOME"] == str(world.engine)
    assert fields["resolved"] == str(world.checkout)


def test_arguments_reach_the_script_unchanged(world: World) -> None:
    (world.engine / "scripts").chmod(0o755)
    args_script = world.engine / "scripts" / "args.sh"
    args_script.write_text('#!/usr/bin/env bash\nprintf "%s|" "$@"\n', encoding="utf-8")

    result = world.run("scripts/args.sh", "--flag", "a b")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "--flag|a b|"


def test_a_missing_checkout_exits_78_with_one_line(world: World) -> None:
    _set_writable(world.checkout)
    checkout = world.checkout
    for child in sorted(checkout.rglob("*"), reverse=True):
        child.unlink() if child.is_file() or child.is_symlink() else child.rmdir()
    checkout.rmdir()

    result = world.run(SH_SCRIPT)

    assert result.returncode == 78, result.stdout + result.stderr
    lines = result.stderr.splitlines()
    assert len(lines) == 1, lines
    # The registry reader drops a path that is gone or not a directory and says so.
    assert PROJECT in lines[0] and "path is gone" in lines[0]
    assert result.stdout == ""


def test_a_checkout_that_is_a_file_exits_78_with_one_line(world: World) -> None:
    checkout = world.checkout
    for child in sorted(checkout.rglob("*"), reverse=True):
        child.unlink() if child.is_file() or child.is_symlink() else child.rmdir()
    checkout.rmdir()
    checkout.write_text("not a directory\n", encoding="utf-8")

    result = world.run(SH_SCRIPT)

    assert result.returncode == 78, result.stdout + result.stderr
    lines = result.stderr.splitlines()
    assert len(lines) == 1, lines
    # The registry reader drops a path that is gone or not a directory and says so.
    assert PROJECT in lines[0] and "path is gone" in lines[0]
    assert result.stdout == ""


def test_a_tree_without_the_script_still_exits_78(world: World) -> None:
    result = world.run("scripts/not_there.sh")

    assert result.returncode == 78, result.stdout + result.stderr
    assert len(result.stderr.splitlines()) == 1
    assert result.stdout == ""


def test_an_unsupported_extension_still_exits_78(world: World) -> None:
    result = world.run("scripts/probe_daemon.rb")

    assert result.returncode == 78, result.stdout + result.stderr
    assert len(result.stderr.splitlines()) == 1


def test_an_unregistered_project_still_exits_78(world: World) -> None:
    env = {
        "HOME": str(world.home),
        "PATH": os.environ.get("PATH", ""),
        "VNX_HOME": str(world.home / ".vnx-system" / "current"),
    }
    result = subprocess.run(
        ["bash", str(LAUNCHER), "no-such-project", SH_SCRIPT],
        capture_output=True, text=True, env=env, timeout=60,
    )

    assert result.returncode == 78, result.stdout + result.stderr
    assert len(result.stderr.splitlines()) == 1
