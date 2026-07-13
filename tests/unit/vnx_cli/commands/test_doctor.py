#!/usr/bin/env python3
"""Unit tests for vnx_cli/commands/doctor.py hook-path probe and agent enumeration."""

import json
from pathlib import Path

import pytest

from vnx_cli.commands.doctor import PASS, WARN, _check_agents, _check_hook_paths


def _make_agent(base: Path, name: str, rel: str = "agents") -> Path:
    agent_dir = base / rel / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "CLAUDE.md").write_text(f"# {name}")
    return agent_dir


def _write_settings(project_dir: Path, settings: dict) -> Path:
    settings_path = project_dir / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    return settings_path


class TestHookPathResolution:
    def test_live_and_dead_hook_paths(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        hooks_dir = project / "scripts" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "live_hook.sh").write_text("#!/bin/bash\n")

        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    'bash -c \'exec bash "'
                                    '$(git rev-parse --show-toplevel 2>/dev/null || echo .)'
                                    '/scripts/hooks/live_hook.sh"\''
                                ),
                            }
                        ],
                    },
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    'bash -c \'exec bash "'
                                    '$(git rev-parse --show-toplevel 2>/dev/null || echo .)'
                                    '/scripts/hooks/dead_hook.sh"\''
                                ),
                            }
                        ],
                    },
                ]
            }
        }
        _write_settings(project, settings)

        result = _check_hook_paths(project)

        assert result.status == "WARN"
        assert "dead_hook.sh" in result.detail
        assert "live_hook.sh" not in result.detail
        assert "canonical .claude/vnx-system/hooks/" in result.detail

    def test_missing_settings_is_clean(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        result = _check_hook_paths(project)

        assert result.status == "PASS"
        assert "skipping" in result.detail

    def test_malformed_settings_warns(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        settings_path = project / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text("{not valid json", encoding="utf-8")

        result = _check_hook_paths(project)

        assert result.status == "WARN"
        assert "unparseable" in result.detail.lower()

    def test_hardcoded_absolute_project_path_warns(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"bash {project / 'scripts' / 'hooks' / 'gone.sh'}",
                            }
                        ],
                    }
                ]
            }
        }
        _write_settings(project, settings)

        result = _check_hook_paths(project)

        assert result.status == "WARN"
        assert "gone.sh" in result.detail
        assert "hardcoded absolute" in result.detail.lower()

    def test_all_paths_live_passes(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        hooks_dir = project / "scripts" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "present.sh").write_text("#!/bin/bash\n")

        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'bash -c \'exec bash "$ROOT/scripts/hooks/present.sh"\'',
                            }
                        ],
                    }
                ]
            }
        }
        _write_settings(project, settings)

        result = _check_hook_paths(project)

        assert result.status == "PASS"


class TestCheckAgents:
    """The `agents` check must count the FULL resolution chain, not just project-local."""

    def test_project_local_only_passes(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        _make_agent(project_dir, "local-agent", rel="agents")
        engine_root = tmp_path / "engine"
        engine_root.mkdir()
        monkeypatch.setattr("vnx_cli.commands.doctor._engine.engine_root", lambda: engine_root)

        result = _check_agents(project_dir)
        assert result.status == PASS
        assert "1 agent(s) resolvable" in result.detail
        assert "project" in result.detail

    def test_engine_fleet_only_project_no_false_zero(self, tmp_path, monkeypatch):
        """Project-local agents/ empty, engine populated — must NOT WARN '0 agents'.

        This is the exact defect from the dispatch: dispatch_agent resolves via
        the full chain (project -> engine fallback), so the doctor readout must
        agree instead of reporting an empty project-local dir as broken.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        engine_root = tmp_path / "engine"
        _make_agent(engine_root, "backend-developer", rel="agents")
        monkeypatch.setattr("vnx_cli.commands.doctor._engine.engine_root", lambda: engine_root)

        result = _check_agents(project_dir)
        assert result.status == PASS
        assert "1 agent(s) resolvable" in result.detail
        assert "engine" in result.detail

    def test_examples_tier_counted(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        _make_agent(project_dir, "demo-agent", rel="examples")
        engine_root = tmp_path / "engine"
        engine_root.mkdir()
        monkeypatch.setattr("vnx_cli.commands.doctor._engine.engine_root", lambda: engine_root)

        result = _check_agents(project_dir)
        assert result.status == PASS
        assert "1 agent(s) resolvable" in result.detail
        assert "examples" in result.detail

    def test_empty_everywhere_warns(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        engine_root = tmp_path / "engine"
        engine_root.mkdir()
        monkeypatch.setattr("vnx_cli.commands.doctor._engine.engine_root", lambda: engine_root)

        result = _check_agents(project_dir)
        assert result.status == WARN
        assert "no agents found" in result.detail.lower()
