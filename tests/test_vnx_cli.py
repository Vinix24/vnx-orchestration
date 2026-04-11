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

from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent
from vnx_cli.commands.doctor import vnx_doctor, PASS, FAIL, WARN
from vnx_cli.commands.init_cmd import vnx_init
from vnx_cli.commands.status import vnx_status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doctor_args(project_dir, *, json_flag=False):
    return Namespace(project_dir=str(project_dir), json=json_flag)


def _init_args(project_dir):
    return Namespace(project_dir=str(project_dir))


def _status_args(project_dir, *, json_flag=False):
    return Namespace(project_dir=str(project_dir), json=json_flag)


def _dispatch_agent_args(project_dir, agent, instruction, model="sonnet"):
    return Namespace(
        project_dir=str(project_dir),
        agent=agent,
        instruction=instruction,
        model=model,
    )


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


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_empty_project(tmp_path, capsys):
    """Status with no .vnx-data/ shows not initialized."""
    rc = vnx_status(_status_args(tmp_path))

    assert rc == 1
    captured = capsys.readouterr()
    assert "not initialized" in captured.out.lower() or "not initialized" in captured.err.lower()


def test_status_with_agents(tmp_path, capsys):
    """Status lists agents from agents/ dir."""
    vnx_init(_init_args(tmp_path))
    (tmp_path / "agents" / "T1").mkdir(parents=True)
    (tmp_path / "agents" / "T2").mkdir(parents=True)

    rc = vnx_status(_status_args(tmp_path))

    assert rc == 0
    captured = capsys.readouterr()
    assert "T1" in captured.out
    assert "T2" in captured.out


def test_status_json_output(tmp_path):
    """Status --json returns valid JSON with expected structure."""
    vnx_init(_init_args(tmp_path))
    (tmp_path / "agents" / "T1").mkdir(parents=True)

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = vnx_status(_status_args(tmp_path, json_flag=True))

    assert rc == 0
    data = json.loads(buf.getvalue())
    assert data["initialized"] is True
    assert "active_dispatches" in data
    assert "agents" in data
    assert "T1" in data["agents"]


def test_status_json_not_initialized(tmp_path):
    """Status --json with no .vnx-data/ returns JSON with initialized=False."""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = vnx_status(_status_args(tmp_path, json_flag=True))

    assert rc == 1
    data = json.loads(buf.getvalue())
    assert data["initialized"] is False


# ---------------------------------------------------------------------------
# dispatch-agent
# ---------------------------------------------------------------------------

def test_dispatch_agent_missing_agent(tmp_path, capsys):
    """Dispatch to non-existent agent fails with clear error."""
    rc = vnx_dispatch_agent(_dispatch_agent_args(tmp_path, "NonExistent", "do something"))

    assert rc == 1
    captured = capsys.readouterr()
    assert "NonExistent" in captured.err or "not found" in captured.err.lower()


def test_dispatch_agent_missing_claude_md(tmp_path, capsys):
    """Dispatch fails when agent dir exists but CLAUDE.md is missing."""
    (tmp_path / "agents" / "T9").mkdir(parents=True)
    # No CLAUDE.md created

    rc = vnx_dispatch_agent(_dispatch_agent_args(tmp_path, "T9", "do something"))

    assert rc == 1
    captured = capsys.readouterr()
    assert "T9" in captured.err
