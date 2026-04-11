#!/usr/bin/env python3
"""Tests for vnx_cli — pyproject.toml CLI skeleton (F44 PR-1)."""

import json
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the repo root is on sys.path so vnx_cli is importable without install
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vnx_cli.commands.doctor import vnx_doctor, PASS, FAIL, WARN
from vnx_cli.commands.init_cmd import vnx_init


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doctor_args(project_dir, *, json_flag=False):
    return Namespace(project_dir=str(project_dir), json=json_flag)


def _init_args(project_dir):
    return Namespace(project_dir=str(project_dir))


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def test_main_entry_point(capsys):
    """vnx --help exits 0 and prints usage."""
    from vnx_cli.main import main

    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["vnx", "--help"]):
            main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "vnx" in captured.out.lower() or "usage" in captured.out.lower()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def test_init_creates_directories(tmp_path):
    """vnx init scaffolds .vnx/, agents/, .vnx-data/."""
    rc = vnx_init(_init_args(tmp_path))

    assert rc == 0
    assert (tmp_path / ".vnx").is_dir()
    assert (tmp_path / "agents").is_dir()
    assert (tmp_path / ".vnx-data").is_dir()


def test_init_writes_governance_profiles(tmp_path):
    """vnx init creates .vnx/governance_profiles.yaml."""
    vnx_init(_init_args(tmp_path))

    profiles = tmp_path / ".vnx" / "governance_profiles.yaml"
    assert profiles.exists()
    content = profiles.read_text()
    assert "profiles:" in content
    assert "default" in content


def test_init_creates_vnx_data_subdirs(tmp_path):
    """vnx init creates dispatches/pending, receipts, unified_reports, logs."""
    vnx_init(_init_args(tmp_path))

    vnx_data = tmp_path / ".vnx-data"
    for subdir in ("dispatches/pending", "dispatches/active", "receipts", "unified_reports", "logs"):
        assert (vnx_data / subdir).is_dir(), f"missing {subdir}"


def test_init_idempotent(tmp_path):
    """Running vnx init twice does not raise or overwrite existing files."""
    vnx_init(_init_args(tmp_path))
    profiles_before = (tmp_path / ".vnx" / "governance_profiles.yaml").read_text()

    rc = vnx_init(_init_args(tmp_path))

    assert rc == 0
    profiles_after = (tmp_path / ".vnx" / "governance_profiles.yaml").read_text()
    assert profiles_before == profiles_after


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def test_doctor_detects_missing_dirs(tmp_path, capsys):
    """vnx doctor fails when .vnx/ and .vnx-data/ are missing."""
    rc = vnx_doctor(_doctor_args(tmp_path))

    assert rc == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.out


def test_doctor_passes_valid_project(tmp_path, capsys):
    """vnx doctor passes with complete setup (after vnx init)."""
    vnx_init(_init_args(tmp_path))

    # Add a dummy agent dir so the agents check is PASS not WARN
    (tmp_path / "agents" / "T1").mkdir(parents=True)

    rc = vnx_doctor(_doctor_args(tmp_path))

    assert rc == 0
    captured = capsys.readouterr()
    assert "FAIL" not in captured.out


def test_doctor_json_output(tmp_path):
    """vnx doctor --json returns valid JSON with expected structure."""
    vnx_init(_init_args(tmp_path))
    (tmp_path / "agents" / "T1").mkdir(parents=True)

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        vnx_doctor(_doctor_args(tmp_path, json_flag=True))

    data = json.loads(buf.getvalue())
    assert "checks" in data
    assert "project_dir" in data
    assert all("name" in c and "status" in c for c in data["checks"])
