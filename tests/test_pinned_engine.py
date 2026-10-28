"""tests/test_pinned_engine.py — a start resolves the project's pin, or refuses (fleet PR-3).

The contract of ``scripts/lib/pinned_engine.py``: no pin -> the declared home; a pin that
resolves (with or without the decorative ``v``) -> that tree; a dead, malformed or
escaping pin -> exit 78 with ONE stderr line naming project, pin and the directory
searched; an unregistered project id -> exit 78; a chosen tree without the launcher's
relative script -> exit 78 naming pin and missing path, never a fallback.
Registry, store and checkout all live under a tmp ``HOME``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB_DIR = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))

import pinned_engine
from launchd_test_support import pinned_store, register_engine

PROJECT = "pinned-proj"
RELATIVE_SCRIPT = "scripts/gate_obligation_runner.py"


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A tmp HOME: the registry, the store and the checkout all live under it."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("VNX_HOME_ROOT", raising=False)
    return fake_home


def _checkout(tmp_path: Path, home: Path, pin=None) -> Path:
    checkout = tmp_path / "checkout"
    register_engine(home, checkout, PROJECT)
    if pin is not None:
        (checkout / ".vnx-version").write_text(pin + "\n", encoding="utf-8")
    return checkout


def _cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    return subprocess.run(
        [sys.executable, str(LIB_DIR / "pinned_engine.py"), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _one_stderr_line(result: subprocess.CompletedProcess) -> str:
    lines = result.stderr.splitlines()
    assert len(lines) == 1, f"expected one stderr line, got {lines!r}"
    return lines[0]


def test_no_pin_uses_the_declared_home(tmp_path: Path, home: Path) -> None:
    versions = pinned_store(home, ["v1.7.5", "v1.7.6"], current="v1.7.6")
    _checkout(tmp_path, home)

    choice = pinned_engine.resolve_engine(PROJECT, home / ".vnx-system" / "current")

    assert (choice.source, choice.pin) == (pinned_engine.SOURCE_DECLARED, None)
    assert choice.engine_root == (versions / "v1.7.6").resolve()


@pytest.mark.parametrize("pin", ["1.7.5", "v1.7.5"])
def test_installed_pin_wins_over_the_declared_home(pin: str, tmp_path: Path, home: Path) -> None:
    versions = pinned_store(home, ["v1.7.5", "v1.7.6"], current="v1.7.6")
    _checkout(tmp_path, home, pin=pin)

    choice = pinned_engine.resolve_engine(PROJECT, home / ".vnx-system" / "current")

    assert (choice.source, choice.pin) == (pinned_engine.SOURCE_PIN, pin)
    assert choice.engine_root == (versions / "v1.7.5").resolve()


def test_no_pin_cli_prints_the_declared_engine_root(tmp_path: Path, home: Path) -> None:
    versions = pinned_store(home, ["v1.7.6"], current="v1.7.6")
    _checkout(tmp_path, home)

    result = _cli(home, PROJECT, str(home / ".vnx-system" / "current"))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str((versions / "v1.7.6").resolve())


def test_dead_pin_exits_78_naming_project_pin_and_directory(tmp_path: Path, home: Path) -> None:
    versions = pinned_store(home, ["v1.7.6"], current="v1.7.6")
    _checkout(tmp_path, home, pin="9.9.9")

    result = _cli(home, PROJECT, str(home / ".vnx-system" / "current"))

    assert result.returncode == pinned_engine.EX_CONFIG
    assert result.stdout == ""
    line = _one_stderr_line(result)
    assert PROJECT in line and "9.9.9" in line and str(versions) in line


def test_unregistered_project_exits_78(home: Path) -> None:
    pinned_store(home, ["v1.7.6"], current="v1.7.6")

    result = _cli(home, "never-registered", str(home / ".vnx-system" / "current"))

    assert result.returncode == pinned_engine.EX_CONFIG
    assert result.stdout == ""
    line = _one_stderr_line(result)
    assert "never-registered" in line and "projects.json" in line


def test_pin_with_a_parent_reference_exits_78(tmp_path: Path, home: Path) -> None:
    versions = pinned_store(home, ["v1.7.5"], current="v1.7.5")
    _checkout(tmp_path, home, pin="../v1.7.5")

    result = _cli(home, PROJECT, str(home / ".vnx-system" / "current"))

    assert result.returncode == pinned_engine.EX_CONFIG
    line = _one_stderr_line(result)
    assert PROJECT in line and ".vnx-version" in line and str(versions) in line


def test_pin_escaping_the_versions_root_exits_78(tmp_path: Path, home: Path) -> None:
    versions = pinned_store(home, ["v1.7.6"], current="v1.7.6")
    outside = tmp_path / "outside-release"
    outside.mkdir()
    os.symlink(outside, versions / "v9.9.9")
    _checkout(tmp_path, home, pin="9.9.9")

    result = _cli(home, PROJECT, str(home / ".vnx-system" / "current"))

    assert result.returncode == pinned_engine.EX_CONFIG
    line = _one_stderr_line(result)
    assert PROJECT in line and "9.9.9" in line and str(versions) in line


def test_missing_relative_script_in_the_pinned_tree_exits_78(tmp_path: Path, home: Path) -> None:
    versions = pinned_store(home, ["v1.7.5"], current="v1.7.5")
    _checkout(tmp_path, home, pin="1.7.5")

    result = _cli(home, PROJECT, str(home / ".vnx-system" / "current"), RELATIVE_SCRIPT)

    assert result.returncode == pinned_engine.EX_CONFIG
    assert result.stdout == ""
    line = _one_stderr_line(result)
    assert PROJECT in line and "1.7.5" in line
    assert str(versions / "v1.7.5" / RELATIVE_SCRIPT) in line


def test_present_relative_script_is_accepted(tmp_path: Path, home: Path) -> None:
    versions = pinned_store(home, ["v1.7.5"], script="print('stub')\n", current="v1.7.5")
    _checkout(tmp_path, home, pin="1.7.5")

    result = _cli(home, PROJECT, str(home / ".vnx-system" / "current"), RELATIVE_SCRIPT)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str((versions / "v1.7.5").resolve())
