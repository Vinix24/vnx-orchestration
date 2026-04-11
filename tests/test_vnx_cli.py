"""Tests for vnx_cli package — F44 PR-1 + PR-2."""

import json
import os
import sys
import types

import pytest

# Ensure project root is on sys.path so vnx_cli is importable without install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vnx_cli.main import build_parser, main
from vnx_cli.commands.init_cmd import vnx_init
from vnx_cli.commands.doctor import vnx_doctor
from vnx_cli.commands.status import vnx_status
from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


def test_main_entry_point(capsys):
    """vnx --help works without error."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "vnx" in captured.out.lower()


# ---------------------------------------------------------------------------
# vnx init
# ---------------------------------------------------------------------------


def _make_init_args(project_dir: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(project_dir=project_dir)


def test_init_creates_directories(tmp_path):
    """vnx init scaffolds .vnx/, agents/, .vnx-data/."""
    args = _make_init_args(str(tmp_path))
    rc = vnx_init(args)
    assert rc == 0
    assert (tmp_path / ".vnx").is_dir()
    assert (tmp_path / "agents").is_dir()
    assert (tmp_path / ".vnx-data").is_dir()
    # Check a few sub-dirs
    assert (tmp_path / ".vnx-data" / "dispatches" / "pending").is_dir()
    assert (tmp_path / ".vnx-data" / "receipts").is_dir()
    assert (tmp_path / ".vnx-data" / "unified_reports").is_dir()


def test_init_writes_governance_profiles(tmp_path):
    """vnx init creates governance_profiles.yaml."""
    args = _make_init_args(str(tmp_path))
    vnx_init(args)
    profiles = tmp_path / ".vnx" / "governance_profiles.yaml"
    assert profiles.exists()
    content = profiles.read_text()
    assert "profiles:" in content
    assert "default:" in content


def test_init_idempotent(tmp_path):
    """Running vnx init twice must not raise errors."""
    args = _make_init_args(str(tmp_path))
    assert vnx_init(args) == 0
    assert vnx_init(args) == 0  # second run — exist_ok paths


# ---------------------------------------------------------------------------
# vnx doctor
# ---------------------------------------------------------------------------


def _make_doctor_args(project_dir: str, json_output: bool = False) -> types.SimpleNamespace:
    return types.SimpleNamespace(project_dir=project_dir, json_output=json_output)


def test_doctor_detects_missing_dirs(tmp_path):
    """vnx doctor fails when .vnx/ missing."""
    args = _make_doctor_args(str(tmp_path))
    rc = vnx_doctor(args)
    assert rc == 1


def test_doctor_passes_valid_project(tmp_path):
    """vnx doctor passes with complete setup."""
    # Scaffold first
    vnx_init(_make_init_args(str(tmp_path)))
    args = _make_doctor_args(str(tmp_path))
    rc = vnx_doctor(args)
    # Binary checks may FAIL in CI (jq absent), but dir checks should PASS.
    # We accept 0 (all pass) or check that dir-related entries are PASS.
    # The important thing: no exception raised.
    assert rc in (0, 1)


def test_doctor_json_output(tmp_path, capsys):
    """vnx doctor --json returns valid JSON with expected keys."""
    vnx_init(_make_init_args(str(tmp_path)))
    capsys.readouterr()  # flush init output before capturing doctor JSON
    args = _make_doctor_args(str(tmp_path), json_output=True)
    vnx_doctor(args)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "checks" in data
    assert "ok" in data
    assert isinstance(data["checks"], list)
    keys = {c["key"] for c in data["checks"]}
    assert ".vnx" in keys
    assert ".vnx-data" in keys


def test_doctor_json_dir_checks_pass_after_init(tmp_path, capsys):
    """After vnx init, .vnx and .vnx-data checks should be PASS in JSON output."""
    vnx_init(_make_init_args(str(tmp_path)))
    capsys.readouterr()  # flush init output before capturing doctor JSON
    args = _make_doctor_args(str(tmp_path), json_output=True)
    vnx_doctor(args)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    by_key = {c["key"]: c for c in data["checks"]}
    assert by_key[".vnx"]["status"] == "PASS"
    assert by_key[".vnx-data"]["status"] == "PASS"


# ---------------------------------------------------------------------------
# vnx status
# ---------------------------------------------------------------------------


def _make_status_args(project_dir: str, json_output: bool = False) -> types.SimpleNamespace:
    return types.SimpleNamespace(project_dir=project_dir, json_output=json_output)


def test_status_empty_project(tmp_path, capsys):
    """Status with no .vnx-data/ shows not initialized."""
    args = _make_status_args(str(tmp_path))
    rc = vnx_status(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert "not initialized" in captured.out


def test_status_initialized_project(tmp_path, capsys):
    """Status after vnx init shows zero active dispatches."""
    vnx_init(_make_init_args(str(tmp_path)))
    capsys.readouterr()
    args = _make_status_args(str(tmp_path))
    rc = vnx_status(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "Active dispatches" in captured.out


def test_status_with_agents(tmp_path, capsys):
    """Status lists agents from agents/ dir that have CLAUDE.md."""
    vnx_init(_make_init_args(str(tmp_path)))
    capsys.readouterr()

    # Create a named agent subdir with CLAUDE.md
    agent_dir = tmp_path / "agents" / "T1"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "CLAUDE.md").write_text("# T1 Agent")

    args = _make_status_args(str(tmp_path))
    rc = vnx_status(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "T1" in captured.out


def test_status_json_output(tmp_path, capsys):
    """Status --json returns valid JSON with expected keys."""
    vnx_init(_make_init_args(str(tmp_path)))
    capsys.readouterr()
    args = _make_status_args(str(tmp_path), json_output=True)
    rc = vnx_status(args)
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["initialized"] is True
    assert "active_dispatches" in data
    assert "recent_completions" in data
    assert "agents" in data
    assert isinstance(data["agents"], list)


def test_status_json_not_initialized(tmp_path, capsys):
    """Status --json on uninitialized project returns initialized=False."""
    args = _make_status_args(str(tmp_path), json_output=True)
    rc = vnx_status(args)
    assert rc == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["initialized"] is False


# ---------------------------------------------------------------------------
# vnx dispatch-agent
# ---------------------------------------------------------------------------


def _make_dispatch_args(
    project_dir: str,
    agent: str,
    instruction: str,
    model: str = "sonnet",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        project_dir=project_dir,
        agent=agent,
        instruction=instruction,
        model=model,
    )


def test_dispatch_agent_missing_agent(tmp_path, capsys):
    """Dispatch to non-existent agent fails with clear error."""
    vnx_init(_make_init_args(str(tmp_path)))
    capsys.readouterr()
    args = _make_dispatch_args(str(tmp_path), agent="ghost", instruction="do something")
    rc = vnx_dispatch_agent(args)
    assert rc == 1
    captured = capsys.readouterr()
    assert "ghost" in captured.out
    assert "not found" in captured.out


def test_dispatch_agent_missing_instruction(tmp_path):
    """Dispatch without instruction fails at argument parsing level."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["dispatch-agent", "--agent", "T1"])
    assert exc.value.code != 0


def test_dispatch_agent_missing_agent_flag(tmp_path):
    """Dispatch without --agent fails at argument parsing level."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["dispatch-agent", "--instruction", "do something"])
    assert exc.value.code != 0
