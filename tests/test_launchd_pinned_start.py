"""tests/test_launchd_pinned_start.py — a launchd job starts from its project's pin (PR-3).

Both halves run the REAL files:

* ``scripts/launchd/reload_plist.sh`` renders ``com.vnx.gate-obligation-runner`` so its
  ``ProgramArguments`` call ``vnx_pinned_launcher.sh`` instead of running
  ``python3 scripts/gate_obligation_runner.py`` from whatever tree the reload ran in.
* the launcher resolves the pin at every start: it starts an older pin's script from a
  tree holding nothing but that script, a dead pin exits 78 with one line, a pinned tree
  without the script is refused, and two starts with a changed pin in between run two
  different stubs.

Registry, store and LaunchAgents all live under a tmp ``HOME``; ``launchctl`` is the
recording stub from ``launchd_test_support``.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from launchd_test_support import pinned_store, recording_launchctl, register_engine

REPO = Path(__file__).resolve().parent.parent
LAUNCHD_DIR = REPO / "scripts" / "launchd"
LAUNCHER = LAUNCHD_DIR / "vnx_pinned_launcher.sh"
PROJECT = "pinned-start"
TEMPLATE = "com.vnx.gate-obligation-runner"
RELATIVE_SCRIPT = "scripts/gate_obligation_runner.py"

# The only thing a pinned tree must carry, and all the launcher may rely on: it prints
# where it ran, in which directory, with which VNX_HOME, and the arguments it was given.
STUB = (
    "import os, pathlib, sys\n"
    "print(pathlib.Path(__file__).resolve())\n"
    "print(pathlib.Path.cwd())\n"
    "print(os.environ['VNX_HOME'])\n"
    "print(' '.join(sys.argv[1:]))\n"
)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    return home


def _checkout(tmp_path: Path, home: Path, pin=None) -> Path:
    checkout = tmp_path / "checkout"
    register_engine(home, checkout, PROJECT)
    if pin is not None:
        (checkout / ".vnx-version").write_text(pin + "\n", encoding="utf-8")
    return checkout


def _run_launcher(home: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "VNX_HOME": str(home / ".vnx-system" / "current"),
    }
    return subprocess.run(
        ["bash", str(LAUNCHER), PROJECT, RELATIVE_SCRIPT, *extra],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_program_arguments_call_the_pinned_launcher(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path)
    engine = tmp_path / "engine"
    engine.mkdir()
    os.symlink(REPO / "scripts", engine / "scripts", target_is_directory=True)
    register_engine(home, engine, PROJECT)
    recording_launchctl(tmp_path / "bin", monkeypatch)
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "VNX_HOME": str(engine),
        "VNX_PROJECT_ID": PROJECT,
        "VNX_TEST_LAUNCHCTL_SHIM": os.environ.get("VNX_TEST_LAUNCHCTL_SHIM", ""),
    }

    result = subprocess.run(
        ["bash", str(LAUNCHD_DIR / "reload_plist.sh"), TEMPLATE, PROJECT],
        capture_output=True, text=True, env=env, timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    data = plistlib.loads(
        (home / "Library" / "LaunchAgents" / f"{TEMPLATE}.{PROJECT}.plist").read_bytes()
    )
    argv = data["ProgramArguments"]
    assert argv == [
        "/bin/bash",
        f"{engine}/scripts/launchd/vnx_pinned_launcher.sh",
        PROJECT,
        RELATIVE_SCRIPT,
    ]
    assert "python3 scripts/gate_obligation_runner.py" not in " ".join(argv)


def test_starts_an_older_pinned_tree_from_a_newer_launcher(tmp_path: Path) -> None:
    home = _home(tmp_path)
    versions = pinned_store(home, ["v1.7.5", "v1.7.6"], script=STUB, current="v1.7.6")
    _checkout(tmp_path, home, pin="1.7.5")
    pinned = versions / "v1.7.5"

    result = _run_launcher(home, "--flag", "value")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(pinned / RELATIVE_SCRIPT),
        str(pinned),
        str(pinned),
        "--flag value",
    ]


def test_no_pin_runs_the_declared_engine(tmp_path: Path) -> None:
    home = _home(tmp_path)
    versions = pinned_store(home, ["v1.7.6"], script=STUB, current="v1.7.6")
    _checkout(tmp_path, home)

    result = _run_launcher(home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == str(versions / "v1.7.6" / RELATIVE_SCRIPT)


def test_dead_pin_exits_78_with_one_line(tmp_path: Path) -> None:
    home = _home(tmp_path)
    versions = pinned_store(home, ["v1.7.6"], script=STUB, current="v1.7.6")
    _checkout(tmp_path, home, pin="9.9.9")

    result = _run_launcher(home)

    assert result.returncode == 78, result.stdout + result.stderr
    lines = result.stderr.splitlines()
    assert len(lines) == 1, lines
    assert PROJECT in lines[0] and "9.9.9" in lines[0] and str(versions) in lines[0]
    assert result.stdout == ""


def test_pinned_tree_without_the_script_exits_78(tmp_path: Path) -> None:
    home = _home(tmp_path)
    versions = pinned_store(home, ["v1.7.5"], current="v1.7.5")
    _checkout(tmp_path, home, pin="1.7.5")

    result = _run_launcher(home)

    assert result.returncode == 78, result.stdout + result.stderr
    lines = result.stderr.splitlines()
    assert len(lines) == 1, lines
    assert "1.7.5" in lines[0]
    assert str(versions / "v1.7.5" / RELATIVE_SCRIPT) in lines[0]


def test_two_starts_with_a_changed_pin_between_pick_two_trees(tmp_path: Path) -> None:
    home = _home(tmp_path)
    versions = pinned_store(home, ["v1.7.5", "v1.7.6"], script=STUB, current="v1.7.6")
    checkout = _checkout(tmp_path, home, pin="1.7.5")

    first = _run_launcher(home)
    assert first.returncode == 0, first.stderr
    assert first.stdout.splitlines()[0] == str(versions / "v1.7.5" / RELATIVE_SCRIPT)

    (checkout / ".vnx-version").write_text("1.7.6\n", encoding="utf-8")

    second = _run_launcher(home)
    assert second.returncode == 0, second.stderr
    assert second.stdout.splitlines()[0] == str(versions / "v1.7.6" / RELATIVE_SCRIPT)
