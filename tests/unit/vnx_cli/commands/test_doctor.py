#!/usr/bin/env python3
"""Unit tests for vnx_cli/commands/doctor.py hook-path probe and agent enumeration."""

import ast
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

    def test_absolute_path_outside_project_dead_is_flagged(self, tmp_path):
        """OI-1123 follow-up regression guard.

        A fabric-deployed hook pin is ALWAYS an absolute path outside project_dir
        by design (``~/.vnx-system/versions/<v>/...``). A prior implementation
        skipped existence-checking any absolute path that resolved outside
        project_dir before touching the filesystem, which reported PASS on two
        confirmed-dead pins in mission-control (a stop-report hook and a
        raw-claude-spawn security guard) instead of catching them. This must
        fail again if that ``in_project`` skip is ever reintroduced.
        """
        project = tmp_path / "project"
        project.mkdir()
        # Deliberately never created — simulates a fabric version dir that
        # vanished after a version rotation (e.g. edge -> a pinned release).
        dead_pin = tmp_path / "outside-fabric" / "versions" / "edge" / "hooks" / "stop_report.sh"

        settings = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": f"bash {dead_pin}"}],
                    }
                ]
            }
        }
        _write_settings(project, settings)

        result = _check_hook_paths(project)

        assert result.status == "WARN"
        assert "stop_report.sh" in result.detail

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
                                "command": (
                                    'bash -c \'exec bash '
                                    '"$CLAUDE_PROJECT_DIR/scripts/hooks/present.sh"\''
                                ),
                            }
                        ],
                    }
                ]
            }
        }
        _write_settings(project, settings)

        result = _check_hook_paths(project)

        assert result.status == "PASS"


class TestHookPathFunctionSize:
    """OI-547: _check_hook_paths must stay within the 70-line codex advisory.

    It now delegates path resolution entirely to hookpin_check.check_project_hook_pins
    (OI-1123 consolidation) rather than maintaining a second in-repo resolver, so there
    is no longer a separate _collect_dead_hook_paths helper to size-guard. AST
    line-count guards the advisory the same way test_doctor_refactor_oi1573.py guards
    cmd_doctor (≤190 lines).
    """

    def test_check_hook_paths_under_70_lines(self) -> None:
        source = Path(__file__).parent.parent.parent.parent.parent / "vnx_cli" / "commands" / "doctor.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_check_hook_paths"
        )
        assert fn.end_lineno - fn.lineno + 1 <= 70


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
