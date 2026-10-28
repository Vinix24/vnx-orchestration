"""tests/test_launchd_pinned_start.py — a launchd job starts from its project's pin (PR-3/PR-4).

Both halves run the REAL files:

* both install paths render the job templates so their ``ProgramArguments`` call
  ``vnx_pinned_launcher.sh`` instead of running the script from whatever tree the install
  ran in. PR-3 did it for ``com.vnx.gate-obligation-runner`` (``reload_plist.sh``);
  PR-4 adds ``com.vnx.receipt-processor`` (a bash script), ``com.vnx.cleanup-reviewed-
  worktrees`` and ``com.vnx.ledger-health`` through BOTH ``vnx init`` and ``reload_plist.sh``.
* the launcher resolves the pin at every start: it starts an older pin's script from a
  tree holding nothing but that script, picks the interpreter from the script's own
  extension (``*.sh`` -> bash, ``*.py`` -> python3, anything else -> exit 78), a dead pin
  exits 78 with one line, a pinned tree without the script is refused, and two starts
  with a changed pin in between run two different stubs.

Registry, store and LaunchAgents all live under a tmp ``HOME``; ``launchctl`` is the
recording stub from ``launchd_test_support``.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from launchd_test_support import (
    make_central_engine,
    pinned_store,
    recording_launchctl,
    register_engine,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vnx_cli.commands import init_cmd  # noqa: E402

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


def _run_launcher(
    home: Path, *extra: str, script: str = RELATIVE_SCRIPT
) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "VNX_HOME": str(home / ".vnx-system" / "current"),
    }
    return subprocess.run(
        ["bash", str(LAUNCHER), PROJECT, script, *extra],
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


# ─────────────────────────────────────────────────────────────────────────
# PR-4: the three families that still started from the rendering tree now
# start through the same launcher, installed by BOTH install paths, and the
# launcher picks the interpreter from the script's own extension.
# ─────────────────────────────────────────────────────────────────────────

_PROJECT_ROOT_ARG = "<project-root>"

# (template, relative script, the script's own arguments, in order)
FAMILIES = [
    pytest.param(
        "com.vnx.receipt-processor",
        "scripts/receipt_processor_supervisor.sh",
        [],
        id="receipt-processor",
    ),
    pytest.param(
        "com.vnx.cleanup-reviewed-worktrees",
        "scripts/cleanup_reviewed_worktrees.py",
        ["--repo-root", _PROJECT_ROOT_ARG, "--apply"],
        id="cleanup-reviewed-worktrees",
    ),
    pytest.param(
        "com.vnx.ledger-health",
        "scripts/ledger_health.py",
        [],
        id="ledger-health",
    ),
]


def _expected_argv(engine: Path, relative: str, extra: list, project_root: Path) -> list:
    return [
        "/bin/bash",
        f"{engine}/scripts/launchd/vnx_pinned_launcher.sh",
        PROJECT,
        relative,
        *[str(project_root) if arg == _PROJECT_ROOT_ARG else arg for arg in extra],
    ]


@pytest.mark.parametrize("template,relative,extra", FAMILIES)
def test_init_install_starts_each_family_through_the_launcher(
    tmp_path: Path, monkeypatch, template: str, relative: str, extra: list
) -> None:
    """``vnx init``'s install path: the job calls the launcher, never a tree's script."""
    home = _home(tmp_path)
    engine = make_central_engine(tmp_path / "engine", real_scripts=REPO / "scripts")
    monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: engine)
    monkeypatch.setattr(Path, "home", lambda: home)
    recording_launchctl(tmp_path / "bin", monkeypatch)
    project_root = tmp_path / "project-checkout"
    project_root.mkdir()

    assert init_cmd._install_launchd_agent(
        str(engine), template, project_id=PROJECT, project_root=str(project_root)
    )

    dest = home / "Library" / "LaunchAgents" / f"{template}.{PROJECT}.plist"
    data = plistlib.loads(dest.read_bytes())
    assert data["ProgramArguments"] == _expected_argv(engine, relative, extra, project_root)
    joined = " ".join(data["ProgramArguments"])
    assert "-c" not in data["ProgramArguments"], joined
    assert f"exec python3 {relative}" not in joined, joined
    assert f"exec bash {relative}" not in joined, joined


@pytest.mark.parametrize("template,relative,extra", FAMILIES)
def test_reload_plist_starts_each_family_through_the_launcher(
    tmp_path: Path, monkeypatch, template: str, relative: str, extra: list
) -> None:
    """``reload_plist.sh``'s install path: the same argv, for the same template."""
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
        ["bash", str(LAUNCHD_DIR / "reload_plist.sh"), template, PROJECT],
        capture_output=True, text=True, env=env, timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    dest = home / "Library" / "LaunchAgents" / f"{template}.{PROJECT}.plist"
    data = plistlib.loads(dest.read_bytes())
    # reload_plist.sh looks the project checkout up in the registry, which
    # register_engine above points at the engine root itself.
    assert data["ProgramArguments"] == _expected_argv(engine, relative, extra, engine)


# A pinned tree whose scripts report the interpreter that actually ran them.
_SH_PROBE = 'printf "interpreter=bash\\n"\nprintf "bash_version=%s\\n" "$BASH_VERSION"\n'
_PY_PROBE = "import sys\nprint('interpreter=python3')\nprint('executable=' + sys.executable)\n"


def _probe_tree(tmp_path: Path, script_name: str, body: str) -> Path:
    home = _home(tmp_path)
    pinned_store(home, ["v1.7.5"], current="v1.7.5", scripts={script_name: body})
    _checkout(tmp_path, home)
    return home


def test_launcher_runs_a_sh_script_with_bash(tmp_path: Path) -> None:
    """The receipt processor is a bash script: ``exec python3 x.sh`` cannot start it."""
    home = _probe_tree(tmp_path, "scripts/probe.sh", _SH_PROBE)

    result = _run_launcher(home, script="scripts/probe.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "interpreter=bash", lines
    assert lines[1].startswith("bash_version=") and len(lines[1]) > len("bash_version="), lines


def test_launcher_runs_a_py_script_with_python3(tmp_path: Path) -> None:
    """The cleanup sweeper and ledger_health are python: still python3, unchanged."""
    home = _probe_tree(tmp_path, "scripts/probe.py", _PY_PROBE)

    result = _run_launcher(home, script="scripts/probe.py")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "interpreter=python3", lines
    assert Path(lines[1].split("=", 1)[1]).name.startswith("python3"), lines


def test_a_script_with_another_extension_exits_78_with_one_line(tmp_path: Path) -> None:
    home = _probe_tree(tmp_path, "scripts/notes.txt", "hello\n")

    result = _run_launcher(home, script="scripts/notes.txt")

    assert result.returncode == 78, result.stdout + result.stderr
    lines = result.stderr.splitlines()
    assert len(lines) == 1, lines
    assert "scripts/notes.txt" in lines[0], lines
    assert result.stdout == ""


def test_pinned_tree_without_the_receipt_processor_supervisor_exits_78(
    tmp_path: Path,
) -> None:
    """PR-3's pinned-tree rule, now for a bash script: no fallback to another tree."""
    home = _home(tmp_path)
    versions = pinned_store(home, ["v1.7.5"], current="v1.7.5")
    _checkout(tmp_path, home, pin="1.7.5")
    relative = "scripts/receipt_processor_supervisor.sh"

    result = _run_launcher(home, script=relative)

    assert result.returncode == 78, result.stdout + result.stderr
    lines = result.stderr.splitlines()
    assert len(lines) == 1, lines
    assert "1.7.5" in lines[0]
    assert str(versions / "v1.7.5" / relative) in lines[0]
    assert result.stdout == ""
