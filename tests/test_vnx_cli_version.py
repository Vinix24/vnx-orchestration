#!/usr/bin/env python3
"""Tests for vnx version subcommand."""

import io
import sys
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vnx_cli.commands.version import vnx_version, _read_version_file, _read_pin


def _version_args(project_dir="."):
    return Namespace(project_dir=str(project_dir))


def _capture_version(args) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = vnx_version(args)
    return buf.getvalue(), rc


# ---------------------------------------------------------------------------
# _read_version_file
# ---------------------------------------------------------------------------

def test_read_version_file_returns_string():
    version = _read_version_file()
    assert isinstance(version, str)
    assert len(version) > 0


def test_read_version_file_matches_repo_version():
    version_file = REPO_ROOT / "VERSION"
    if version_file.is_file():
        expected = version_file.read_text(encoding="utf-8").strip()
        assert _read_version_file() == expected


def test_read_version_file_prefers_engine_version_over_dist_info(tmp_path, monkeypatch):
    """The RESOLVED engine's VERSION wins over stale pip dist-info metadata.

    Reproduces the 'vnx version lies' bug: the editable finder points at a
    version dir (e.g. versions/edge=1.1.0) whose VERSION differs from the
    dist-info name (...-1.0.0). vnx version must report the engine's VERSION.
    """
    import vnx_cli._engine as _engine
    fake_engine = tmp_path / "engine"
    fake_engine.mkdir()
    (fake_engine / "VERSION").write_text("9.9.9\n")
    monkeypatch.setattr(_engine, "engine_root", lambda: fake_engine)
    assert _read_version_file() == "9.9.9"


def test_read_version_file_falls_back_when_no_version_file(tmp_path, monkeypatch):
    """No VERSION file in the engine -> fall back to package __version__."""
    import vnx_cli._engine as _engine
    from vnx_cli import __version__
    empty = tmp_path / "no_version"
    empty.mkdir()
    monkeypatch.setattr(_engine, "engine_root", lambda: empty)
    assert _read_version_file() == __version__


# ---------------------------------------------------------------------------
# _read_pin
# ---------------------------------------------------------------------------

def test_read_pin_no_file(tmp_path):
    assert _read_pin(tmp_path) == "current"


def test_read_pin_with_file(tmp_path):
    (tmp_path / ".vnx-version").write_text("1.0.0-rc3\n")
    assert _read_pin(tmp_path) == "1.0.0-rc3 (project)"


def test_read_pin_empty_file(tmp_path):
    (tmp_path / ".vnx-version").write_text("   \n")
    assert _read_pin(tmp_path) == "current"


# ---------------------------------------------------------------------------
# vnx_version output structure
# ---------------------------------------------------------------------------

def test_version_exit_code_zero(tmp_path):
    _, rc = _capture_version(_version_args(tmp_path))
    assert rc == 0


def test_version_prints_vnx_prefix(tmp_path):
    output, _ = _capture_version(_version_args(tmp_path))
    assert output.startswith("VNX ")


def test_version_contains_commit_line(tmp_path):
    output, _ = _capture_version(_version_args(tmp_path))
    assert any(line.startswith("Commit:") for line in output.splitlines())


def test_version_contains_vnx_home_line(tmp_path):
    output, _ = _capture_version(_version_args(tmp_path))
    assert any(line.startswith("VNX_HOME:") for line in output.splitlines())


def test_version_contains_pin_line(tmp_path):
    output, _ = _capture_version(_version_args(tmp_path))
    assert any(line.startswith("Pin:") for line in output.splitlines())


def test_version_contains_python_line(tmp_path):
    output, _ = _capture_version(_version_args(tmp_path))
    python_lines = [l for l in output.splitlines() if l.startswith("Python:")]
    assert len(python_lines) == 1
    # Must contain major.minor.patch
    assert f"{sys.version_info.major}.{sys.version_info.minor}" in python_lines[0]


def test_version_five_lines_total(tmp_path):
    output, _ = _capture_version(_version_args(tmp_path))
    lines = [l for l in output.splitlines() if l.strip()]
    assert len(lines) == 5


def test_version_pin_reflects_vnx_version_file(tmp_path):
    (tmp_path / ".vnx-version").write_text("1.0.0-rc99")
    output, _ = _capture_version(_version_args(tmp_path))
    assert "1.0.0-rc99 (project)" in output
