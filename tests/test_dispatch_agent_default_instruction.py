#!/usr/bin/env python3
"""Tests for dispatch-agent default instruction and engine-root fallback (PR-README-QS).

Covers:
  1. _read_default_instruction parses quoted and unquoted values
  2. _read_default_instruction returns None for missing key / unreadable file
  3. _resolve_agent_path falls back to engine_root when not in project_dir
  4. vnx_dispatch_agent uses default_instruction when --instruction omitted
  5. vnx_dispatch_agent errors when no instruction and no default
  6. hello-world example ships default_instruction in config.yaml
  7. examples/ is declared in pyproject.toml package-data
"""

from __future__ import annotations

import sys
import tempfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vnx_cli.commands.dispatch_agent import (
    _read_default_instruction,
    _resolve_agent_path,
)


# ---------------------------------------------------------------------------
# 1-2. _read_default_instruction
# ---------------------------------------------------------------------------

class TestReadDefaultInstruction:
    def test_reads_double_quoted_value(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text('governance_profile: minimal\ndefault_instruction: "Hello world"\n')
        assert _read_default_instruction(cfg) == "Hello world"

    def test_reads_single_quoted_value(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("default_instruction: 'Do something'\n")
        assert _read_default_instruction(cfg) == "Do something"

    def test_reads_unquoted_value(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("default_instruction: Write a greeting\n")
        assert _read_default_instruction(cfg) == "Write a greeting"

    def test_missing_key_returns_none(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("governance_profile: minimal\n")
        assert _read_default_instruction(cfg) is None

    def test_nonexistent_file_returns_none(self, tmp_path):
        assert _read_default_instruction(tmp_path / "nonexistent.yaml") is None

    def test_empty_value_returns_none(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("default_instruction:\n")
        assert _read_default_instruction(cfg) is None


# ---------------------------------------------------------------------------
# 3. _resolve_agent_path engine-root fallback
# ---------------------------------------------------------------------------

class TestResolveAgentPathFallback:
    def test_finds_agent_in_project_agents(self, tmp_path):
        agent_dir = tmp_path / "agents" / "my-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "CLAUDE.md").write_text("# Agent")
        result = _resolve_agent_path(tmp_path, "my-agent")
        assert result is not None
        assert result == agent_dir / "CLAUDE.md"

    def test_finds_agent_in_project_examples(self, tmp_path):
        ex_dir = tmp_path / "examples" / "demo-agent"
        ex_dir.mkdir(parents=True)
        (ex_dir / "CLAUDE.md").write_text("# Demo")
        result = _resolve_agent_path(tmp_path, "demo-agent")
        assert result is not None
        assert result == ex_dir / "CLAUDE.md"

    def test_falls_back_to_engine_root(self, tmp_path):
        """When agent not in project_dir, falls back to engine_root/examples/."""
        fake_engine = tmp_path / "engine"
        ex_dir = fake_engine / "examples" / "hello-world"
        ex_dir.mkdir(parents=True)
        (ex_dir / "CLAUDE.md").write_text("# Hello World")

        from vnx_cli import _engine
        with patch.object(_engine, "engine_root", return_value=fake_engine):
            result = _resolve_agent_path(tmp_path / "empty-project", "hello-world")

        assert result is not None
        assert result == ex_dir / "CLAUDE.md"

    def test_unknown_agent_returns_none(self, tmp_path):
        from vnx_cli import _engine
        with patch.object(_engine, "engine_root", return_value=tmp_path):
            result = _resolve_agent_path(tmp_path, "nonexistent-xyz")
        assert result is None


# ---------------------------------------------------------------------------
# 4-5. vnx_dispatch_agent uses default_instruction when --instruction omitted
# ---------------------------------------------------------------------------

class TestDispatchAgentDefaultInstruction:
    def _make_agent(self, base: Path, name: str, default_instruction: str | None = None):
        """Create a minimal agent CLAUDE.md + config.yaml under base/examples/<name>/."""
        agent_dir = base / "examples" / name
        agent_dir.mkdir(parents=True)
        (agent_dir / "CLAUDE.md").write_text(f"# {name} agent")
        cfg = "governance_profile: minimal\n"
        if default_instruction:
            cfg += f'default_instruction: "{default_instruction}"\n'
        (agent_dir / "config.yaml").write_text(cfg)
        return agent_dir

    def test_uses_default_instruction_when_none_given(self, tmp_path):
        """When instruction is None and config.yaml has default_instruction, dispatch proceeds."""
        self._make_agent(tmp_path, "hello-world", "Write a greeting")

        captured_instruction = []

        def fake_deliver(**kwargs):
            captured_instruction.append(kwargs.get("instruction"))
            return True

        from vnx_cli import _engine
        with patch.object(_engine, "engine_root", return_value=tmp_path), \
             patch("vnx_cli.commands.dispatch_agent._engine.ensure_engine_on_path"), \
             patch.dict("sys.modules", {"subprocess_dispatch": MagicMock(deliver_with_recovery=fake_deliver)}):
            from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent
            args = Namespace(agent="hello-world", instruction=None, model="sonnet", project_dir=str(tmp_path))
            rc = vnx_dispatch_agent(args)

        assert rc == 0
        assert captured_instruction == ["Write a greeting"]

    def test_errors_when_no_instruction_and_no_default(self, tmp_path, capsys):
        """When instruction is None and config.yaml has no default, error is printed."""
        self._make_agent(tmp_path, "bare-agent", default_instruction=None)

        from vnx_cli import _engine
        with patch.object(_engine, "engine_root", return_value=tmp_path):
            from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent
            args = Namespace(agent="bare-agent", instruction=None, model="sonnet", project_dir=str(tmp_path))
            rc = vnx_dispatch_agent(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "--instruction" in captured.err

    def test_derives_project_id_from_project_dir_and_passes_to_door(self, tmp_path):
        """The dispatch project_id must come from the TARGET --project-dir's
        .vnx-project-id and be threaded to the door — NOT resolved from the
        engine/cwd. Without it a consumer dispatch mis-routes its entire
        governance state into the wrong store (sales-copilot -> vnx-dev)."""
        self._make_agent(tmp_path, "greeter", "Say hi")
        (tmp_path / ".vnx-project-id").write_text("my-target\n")

        captured = {}

        def fake_door(legacy, **kwargs):
            captured.update(kwargs)
            return True

        from vnx_cli import _engine
        with patch.object(_engine, "engine_root", return_value=tmp_path), \
             patch("vnx_cli.commands.dispatch_agent._engine.ensure_engine_on_path"), \
             patch.dict("sys.modules", {
                 "subprocess_dispatch": MagicMock(deliver_with_recovery=lambda **k: True),
                 "dispatch_bridge": MagicMock(deliver_via_door=fake_door),
             }):
            from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent
            args = Namespace(agent="greeter", instruction="build it", model="sonnet", project_dir=str(tmp_path))
            rc = vnx_dispatch_agent(args)

        assert rc == 0
        assert captured.get("project_id") == "my-target"

    def test_explicit_instruction_overrides_default(self, tmp_path):
        """Explicit --instruction takes precedence over default_instruction."""
        self._make_agent(tmp_path, "hello-world", "Default instruction")

        captured_instruction = []

        def fake_deliver(**kwargs):
            captured_instruction.append(kwargs.get("instruction"))
            return True

        from vnx_cli import _engine
        with patch.object(_engine, "engine_root", return_value=tmp_path), \
             patch("vnx_cli.commands.dispatch_agent._engine.ensure_engine_on_path"), \
             patch.dict("sys.modules", {"subprocess_dispatch": MagicMock(deliver_with_recovery=fake_deliver)}):
            from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent
            args = Namespace(agent="hello-world", instruction="Override", model="sonnet", project_dir=str(tmp_path))
            rc = vnx_dispatch_agent(args)

        assert rc == 0
        assert captured_instruction == ["Override"]


# ---------------------------------------------------------------------------
# 6. hello-world ships default_instruction
# ---------------------------------------------------------------------------

def test_hello_world_config_has_default_instruction():
    """examples/hello-world/config.yaml must have default_instruction."""
    config_path = REPO_ROOT / "examples" / "hello-world" / "config.yaml"
    assert config_path.is_file(), "examples/hello-world/config.yaml missing"
    instruction = _read_default_instruction(config_path)
    assert instruction, "default_instruction must be non-empty in hello-world config.yaml"


def test_hello_world_resolves_without_project_dir():
    """hello-world resolves from engine root even in an empty project dir."""
    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp)
        result = _resolve_agent_path(empty, "hello-world")
    assert result is not None, "hello-world not found via engine root fallback"
    assert result.name == "CLAUDE.md"


def test_backend_developer_config_has_default_instruction():
    """examples/backend-developer/config.yaml must have default_instruction."""
    config_path = REPO_ROOT / "examples" / "backend-developer" / "config.yaml"
    assert config_path.is_file(), "examples/backend-developer/config.yaml missing"
    instruction = _read_default_instruction(config_path)
    assert instruction, "default_instruction must be non-empty in backend-developer config.yaml"


def test_backend_developer_resolves_without_project_dir():
    """backend-developer resolves from engine root even in an empty project dir,
    same packaged-examples fallback consumer repos (MC/SEO/sales-copilot) rely on
    since they ship no local agents/ dir."""
    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp)
        result = _resolve_agent_path(empty, "backend-developer")
    assert result is not None, "backend-developer not found via engine root fallback"
    assert result.name == "CLAUDE.md"


# ---------------------------------------------------------------------------
# 7. pyproject.toml declares examples/ in package-data
# ---------------------------------------------------------------------------

def test_pyproject_includes_examples():
    """pyproject.toml must declare examples/**/* in vnx_orchestration package-data."""
    pyproject = REPO_ROOT / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    assert '"examples/**/*"' in content or "'examples/**/*'" in content, (
        "examples/**/* not found in pyproject.toml package-data"
    )
