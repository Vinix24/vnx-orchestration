#!/usr/bin/env python3
"""Unit tests for vnx_cli/commands/doctor.py hook-path probe."""

import json
from pathlib import Path

import pytest

from vnx_cli.commands.doctor import _check_hook_paths


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
