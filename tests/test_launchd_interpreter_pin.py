"""tests/test_launchd_interpreter_pin.py — a launchd job starts an interpreter inside requires-python (OI-2564).

Runs the REAL ``vnx_pinned_launcher.sh`` and ``vnx_python.sh``. Interpreters are shims in
``tmp_path``; ``launchctl`` is never called. The PATH of every run puts a ``python3`` shim
first that reports 3.15 and cannot import yaml, the interpreter Homebrew relinked on
2026-11-05.
"""
from __future__ import annotations

import importlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from launchd_test_support import interpreter_version, pinned_store, register_engine, write_pyproject

REPO = Path(__file__).resolve().parent.parent
LAUNCHD_DIR = REPO / "scripts" / "launchd"
LAUNCHER = LAUNCHD_DIR / "vnx_pinned_launcher.sh"
RESOLVER = LAUNCHD_DIR / "vnx_python.sh"
PINNED_312 = Path("/opt/homebrew/opt/python@3.12/bin/python3.12")
PROJECT = "interp-pin"
SCRIPT = "scripts/gate_obligation_runner.py"


def _shim(path: Path, version: str, body: str = "") -> Path:
    """An executable that answers ``--version`` and otherwise runs ``body``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/bash\n"
        f'if [ "${{1:-}}" = "--version" ]; then echo "Python {version}"; exit 0; fi\n'
        f"{body}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _path_with_bad_python3(tmp_path: Path) -> str:
    bad = tmp_path / "badbin"
    _shim(bad / "python3", "3.15.0", 'echo "No module named yaml" >&2; exit 1')
    return f"{bad}{os.pathsep}{os.environ.get('PATH', '')}"


def _engine(tmp_path: Path, home: Path) -> Path:
    engine = tmp_path / "engine"
    register_engine(home, engine, PROJECT)
    (engine / "scripts").mkdir()
    (engine / SCRIPT).write_text("print('job ran')\n", encoding="utf-8")
    return engine


def _launch(tmp_path: Path, home: Path, engine: Path) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "PATH": _path_with_bad_python3(tmp_path),
        "VNX_HOME": str(engine),
    }
    return subprocess.run(
        ["bash", str(LAUNCHER), PROJECT, SCRIPT],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    return home, _engine(tmp_path, home)


def test_py_job_starts_with_the_venv_of_the_engine_root(tmp_path: Path) -> None:
    home, engine = _setup(tmp_path)
    _shim(engine / ".venv" / "bin" / "python", "3.12.9", 'echo "venv ran $1"')

    result = _launch(tmp_path, home, engine)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == f"venv ran {engine / SCRIPT}"
    assert "No module named yaml" not in result.stderr


@pytest.mark.skipif(not PINNED_312.exists(), reason="the pinned Homebrew 3.12 is not installed on this runner")
def test_without_a_venv_the_pinned_312_is_taken(tmp_path: Path) -> None:
    home, engine = _setup(tmp_path)

    result = _launch(tmp_path, home, engine)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "job ran"


def test_interpreter_outside_requires_python_is_refused_with_78(tmp_path: Path) -> None:
    home, engine = _setup(tmp_path)
    _shim(engine / ".venv" / "bin" / "python", "3.15.0", 'echo "must not run"')

    result = _launch(tmp_path, home, engine)

    assert result.returncode == 78, result.stdout + result.stderr
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert len(lines) == 1, lines
    assert str(engine / ".venv" / "bin" / "python") in lines[0] and "3.15" in lines[0]


@pytest.mark.parametrize("version", ["3.10.9", "3.14.0", "3.15.1"])
def test_resolver_refuses_below_and_above_the_range(tmp_path: Path, version: str) -> None:
    root = tmp_path / "root"
    write_pyproject(root)
    _shim(root / ".venv" / "bin" / "python", version)

    proc = subprocess.run(["bash", str(RESOLVER), str(root)], capture_output=True, text=True, timeout=30)

    assert proc.returncode == 78
    assert proc.stdout == ""
    assert version.rsplit(".", 1)[0] in proc.stderr


def test_resolver_prints_the_venv_python_when_inside_the_range(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_pyproject(root)
    venv = _shim(root / ".venv" / "bin" / "python", "3.12.9")

    proc = subprocess.run(["bash", str(RESOLVER), str(root)], capture_output=True, text=True, timeout=30)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(venv)


@pytest.mark.parametrize("pyproject", [None, "[project]\nname = 'x'\n", '[project]\nrequires-python = "~=3.12"\n'])
def test_resolver_refuses_when_requires_python_is_missing_or_not_understood(tmp_path: Path, pyproject) -> None:
    root = tmp_path / "root"
    root.mkdir()
    if pyproject is not None:
        (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    _shim(root / ".venv" / "bin" / "python", "3.12.9")

    proc = subprocess.run(["bash", str(RESOLVER), str(root)], capture_output=True, text=True, timeout=30)

    assert proc.returncode == 78
    assert proc.stdout == ""
    assert len(proc.stderr.splitlines()) == 1


def test_resolver_refuses_an_unreadable_version(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_pyproject(root)
    _shim(root / ".venv" / "bin" / "python", "garbage")

    proc = subprocess.run(["bash", str(RESOLVER), str(root)], capture_output=True, text=True, timeout=30)

    assert proc.returncode == 78
    assert proc.stdout == ""


def test_launcher_runs_its_resolver_on_the_launchers_own_tree_interpreter(tmp_path: Path) -> None:
    """A launcher tree whose only interpreter is out of range refuses before resolving a pin."""
    tree = tmp_path / "tree"
    write_pyproject(tree)
    (tree / "scripts").mkdir()
    os.symlink(LAUNCHD_DIR, tree / "scripts" / "launchd", target_is_directory=True)
    os.symlink(REPO / "scripts" / "lib", tree / "scripts" / "lib", target_is_directory=True)
    _shim(tree / ".venv" / "bin" / "python", "3.15.0")
    home = tmp_path / "home"
    home.mkdir()

    proc = subprocess.run(
        ["bash", str(tree / "scripts" / "launchd" / "vnx_pinned_launcher.sh"), PROJECT, SCRIPT],
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(home), "PATH": _path_with_bad_python3(tmp_path)},
    )

    assert proc.returncode == 78, proc.stdout + proc.stderr
    assert "3.15" in proc.stderr


def test_pinned_store_release_without_a_venv_uses_the_resolver_pick_or_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    pinned_store(home, ["v1.7.5"], script="print('pinned job')\n", current="v1.7.5")
    register_engine(home, tmp_path / "checkout", PROJECT)
    (tmp_path / "checkout" / ".vnx-version").write_text("1.7.5\n", encoding="utf-8")
    env = {
        "HOME": str(home),
        "PATH": _path_with_bad_python3(tmp_path),
        "VNX_HOME": str(home / ".vnx-system" / "current"),
    }

    proc = subprocess.run(["bash", str(LAUNCHER), PROJECT, SCRIPT], capture_output=True, text=True, env=env, timeout=60)

    sys.path.insert(0, str(REPO / "scripts" / "lib"))
    path_parity = importlib.import_module("path_parity")
    # the launcher resolves again on the pinned release with its PATH plus ~/.local/bin
    search = f"{env['PATH']}{os.pathsep}{home}/.local/bin"
    pick = path_parity.launchd_engine_interpreter(home / ".vnx-system" / "versions" / "v1.7.5", search_path=search)
    version = interpreter_version(pick) if pick else None
    requires = path_parity.parse_requires_python(home / ".vnx-system" / "versions" / "v1.7.5" / "pyproject.toml")
    if version and path_parity.version_in_range(version, requires) is True:
        assert proc.returncode == 0, (pick, proc.stdout + proc.stderr)
        assert proc.stdout.strip() == "pinned job"
    else:
        assert proc.returncode == 78, (pick, proc.stdout + proc.stderr)
        assert len(proc.stderr.strip().splitlines()) == 1, proc.stderr
        assert (Path(pick).name if pick else "python3") in proc.stderr, (pick, proc.stderr)


def test_no_template_starts_a_bare_python3() -> None:
    offenders = [
        p.name for p in sorted(LAUNCHD_DIR.glob("*.plist")) if "exec python3" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "template",
    ["com.vnx.fleet-role-drift", "com.vnx.fleet-pin-audit", "com.vnx.subsystem-probe"],
)
def test_direct_templates_resolve_their_interpreter_through_vnx_python(template: str) -> None:
    text = (LAUNCHD_DIR / f"{template}.plist").read_text(encoding="utf-8")
    assert "scripts/launchd/vnx_python.sh ${VNX_HOME}" in text
    assert 'exec "$PY"' in text
