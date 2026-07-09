#!/usr/bin/env python3
"""Tests for dispatch-agent claude-CLI preflight (audit-dx-dispatch-preflight).

Covers:
  1. Missing `claude` CLI on PATH -> preflight warning printed to stderr (non-Claude lanes
     remain valid, so this is a warning, not a hard block).
  2. Present `claude` CLI -> no preflight warning.
  3. Failed door result -> actionable `vnx doctor` hint printed, exit code 1.
  4. Successful door result -> no failure hint, exit code 0.

VNX_DISPATCH_LEGACY=1 is set so deliver_via_door takes the `legacy()` branch (the mocked
deliver_with_recovery) instead of routing through the real single-entry door's bridge_dispatch,
which would attempt to stage and spawn a real dispatch.
"""

from __future__ import annotations

import shutil
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_REAL_WHICH = shutil.which


def _make_agent(base: Path, name: str = "hello-world", default_instruction: str = "Say hi") -> Path:
    agent_dir = base / "examples" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "CLAUDE.md").write_text(f"# {name} agent")
    (agent_dir / "config.yaml").write_text(
        f'governance_profile: minimal\ndefault_instruction: "{default_instruction}"\n'
    )
    return agent_dir


def _run_dispatch(tmp_path: Path, monkeypatch, *, claude_present: bool, door_success: bool) -> int:
    """Invoke vnx_dispatch_agent with the door forced to the legacy (mocked) lane."""
    _make_agent(tmp_path)
    monkeypatch.setenv("VNX_DISPATCH_LEGACY", "1")

    def fake_which(tool):
        if tool == "claude":
            return "/usr/local/bin/claude" if claude_present else None
        return _REAL_WHICH(tool)

    from vnx_cli import _engine
    with patch.object(_engine, "engine_root", return_value=tmp_path), \
         patch("vnx_cli.commands.dispatch_agent._engine.ensure_engine_on_path"), \
         patch("shutil.which", side_effect=fake_which), \
         patch.dict(
             "sys.modules",
             {"subprocess_dispatch": MagicMock(deliver_with_recovery=lambda **kw: door_success)},
         ):
        from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent
        args = Namespace(agent="hello-world", instruction=None, model="sonnet", project_dir=str(tmp_path))
        rc = vnx_dispatch_agent(args)
    return rc


class TestClaudePreflightWarning:
    def test_missing_claude_prints_warning(self, tmp_path, monkeypatch, capsys):
        rc = _run_dispatch(tmp_path, monkeypatch, claude_present=False, door_success=True)

        captured = capsys.readouterr()
        assert "'claude' CLI not found" in captured.err
        assert "vnx doctor" in captured.err
        assert rc == 0

    def test_present_claude_no_warning(self, tmp_path, monkeypatch, capsys):
        rc = _run_dispatch(tmp_path, monkeypatch, claude_present=True, door_success=True)

        captured = capsys.readouterr()
        assert "not found on PATH" not in captured.err
        assert rc == 0


class TestFailedDispatchHint:
    def test_failed_dispatch_prints_actionable_hint_and_nonzero_exit(self, tmp_path, monkeypatch, capsys):
        rc = _run_dispatch(tmp_path, monkeypatch, claude_present=False, door_success=False)

        captured = capsys.readouterr()
        assert rc == 1
        assert "status      : failed" in captured.out
        assert "vnx doctor" in captured.err
        assert "Dispatch failed" in captured.err

    def test_successful_dispatch_has_no_failure_hint(self, tmp_path, monkeypatch, capsys):
        rc = _run_dispatch(tmp_path, monkeypatch, claude_present=True, door_success=True)

        captured = capsys.readouterr()
        assert rc == 0
        assert "status      : done" in captured.out
        assert "Dispatch failed" not in captured.err
