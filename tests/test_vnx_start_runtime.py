#!/usr/bin/env python3
"""Tests for VNX Start Runtime — provider building, env management, state files (PR-3).

Tests the unified provider command builder, VNX_VARS canonical list,
profile/preset resolution, and state file generation. Validates that
starter and operator modes share the same runtime model (A-R1).
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_start_runtime import (
    VNX_VARS,
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_GEMINI,
    PROVIDER_ALIASES,
    TerminalConfig,
    StartConfig,
    PresetResolution,
    build_provider_command,
    build_provider_label,
    build_pane_title,
    build_env_clean_cmd,
    build_env_set_cmd,
    build_launch_command,
    generate_panes_json,
    generate_terminal_state_json,
    write_state_files,
    resolve_preset,
)


# ---------------------------------------------------------------------------
# VNX_VARS canonical list
# ---------------------------------------------------------------------------

class TestVNXVars:
    def test_contains_all_known_vars(self):
        expected = {
            "PROJECT_ROOT", "VNX_HOME", "VNX_DATA_DIR", "VNX_STATE_DIR",
            "VNX_DISPATCH_DIR", "VNX_LOGS_DIR", "VNX_SKILLS_DIR",
            "VNX_PIDS_DIR", "VNX_LOCKS_DIR", "VNX_REPORTS_DIR", "VNX_DB_DIR",
        }
        assert set(VNX_VARS) == expected

    def test_is_tuple(self):
        assert isinstance(VNX_VARS, tuple)

    def test_no_duplicates(self):
        assert len(VNX_VARS) == len(set(VNX_VARS))


# ---------------------------------------------------------------------------
# Provider command building
# ---------------------------------------------------------------------------

class TestBuildProviderCommand:
    def test_claude_default(self):
        tc = TerminalConfig("T1", PROVIDER_CLAUDE, "sonnet", "worker", "A")
        cmd = build_provider_command(tc)
        assert cmd == "claude --model sonnet"

    def test_claude_with_skip_permissions(self):
        tc = TerminalConfig("T0", PROVIDER_CLAUDE, "default", "orchestrator", skip_permissions=True)
        cmd = build_provider_command(tc)
        assert "--dangerously-skip-permissions" in cmd

    def test_claude_with_extra_flags(self):
        tc = TerminalConfig("T0", PROVIDER_CLAUDE, "default", "orchestrator", extra_flags="--verbose")
        cmd = build_provider_command(tc)
        assert "--verbose" in cmd

    def test_codex_provider(self):
        tc = TerminalConfig("T1", PROVIDER_CODEX, "gpt-5.1-codex-mini", "worker", "A")
        cmd = build_provider_command(tc, codex_model="gpt-5.1-codex-mini")
        assert cmd.startswith("codex -m gpt-5.1-codex-mini")

    def test_codex_with_skip(self):
        tc = TerminalConfig("T1", PROVIDER_CODEX, "gpt-5.1-codex-mini", "worker", "A", skip_permissions=True)
        cmd = build_provider_command(tc)
        assert "--full-auto" in cmd

    def test_gemini_provider(self):
        tc = TerminalConfig("T1", PROVIDER_GEMINI, "gemini-2.5-flash", "worker", "A")
        cmd = build_provider_command(tc, gemini_model="gemini-2.5-flash", project_root="/my/project")
        assert "gemini --yolo" in cmd
        assert "--include-directories '/my/project'" in cmd

    def test_alias_resolution(self):
        tc = TerminalConfig("T1", "codex_cli", "model", "worker", "A")
        cmd = build_provider_command(tc)
        assert cmd.startswith("codex")

    def test_unknown_provider_defaults_to_claude(self):
        tc = TerminalConfig("T1", "unknown_provider", "sonnet", "worker", "A")
        cmd = build_provider_command(tc)
        assert cmd.startswith("claude")

    def test_unified_command_for_reheal_and_fresh(self):
        """PR-3: Same function used for both re-heal and fresh start paths."""
        tc = TerminalConfig("T1", "codex_cli", "model", "worker", "A", skip_permissions=True)
        cmd1 = build_provider_command(tc, codex_model="gpt-5.1-codex-mini")
        cmd2 = build_provider_command(tc, codex_model="gpt-5.1-codex-mini")
        assert cmd1 == cmd2


class TestBuildProviderLabel:
    def test_claude_label(self):
        assert build_provider_label(PROVIDER_CLAUDE) == "Claude"

    def test_codex_label(self):
        assert build_provider_label(PROVIDER_CODEX) == "Codex CLI"

    def test_gemini_label(self):
        assert build_provider_label(PROVIDER_GEMINI) == "Gemini CLI"


class TestBuildPaneTitle:
    def test_claude_title(self):
        assert build_pane_title("T1", PROVIDER_CLAUDE) == "T1"

    def test_codex_title(self):
        assert build_pane_title("T1", PROVIDER_CODEX) == "T1 [CODEX]"

    def test_gemini_title(self):
        assert build_pane_title("T2", PROVIDER_GEMINI) == "T2 [GEMINI]"

    def test_alias_title(self):
        assert build_pane_title("T1", "codex_cli") == "T1 [CODEX]"


# ---------------------------------------------------------------------------
# Environment management
# ---------------------------------------------------------------------------

class TestEnvCommands:
    def test_env_clean_contains_all_vars(self):
        cmd = build_env_clean_cmd()
        assert cmd.startswith("unset ")
        for var in VNX_VARS:
            assert var in cmd

    def test_env_set_basic(self):
        cmd = build_env_set_cmd("/project", "/vnx", "/data")
        assert "PROJECT_ROOT='/project'" in cmd
        assert "VNX_HOME='/vnx'" in cmd
        assert "VNX_DATA_DIR='/data'" in cmd

    def test_env_set_with_skills(self):
        cmd = build_env_set_cmd("/p", "/v", "/d", "/skills")
        assert "VNX_SKILLS_DIR='/skills'" in cmd

    def test_env_set_without_skills(self):
        cmd = build_env_set_cmd("/p", "/v", "/d")
        assert "VNX_SKILLS_DIR" not in cmd


# ---------------------------------------------------------------------------
# State file generation
# ---------------------------------------------------------------------------

class TestGeneratePanesJson:
    @pytest.fixture
    def config(self):
        return StartConfig(
            project_root="/project",
            vnx_home="/vnx",
            vnx_data_dir="/data",
            terminals={
                "T0": TerminalConfig("T0", PROVIDER_CLAUDE, "default", "orchestrator"),
                "T1": TerminalConfig("T1", PROVIDER_CLAUDE, "sonnet", "worker", "A"),
                "T2": TerminalConfig("T2", PROVIDER_CODEX, "gpt-5.1", "worker", "B"),
                "T3": TerminalConfig("T3", PROVIDER_CLAUDE, "default", "worker", "C"),
            },
        )

    def test_has_session(self, config):
        panes = {"T0": "%0", "T1": "%1", "T2": "%2", "T3": "%3"}
        result = generate_panes_json("vnx-test", panes, config)
        assert result["session"] == "vnx-test"

    def test_has_all_terminals(self, config):
        panes = {"T0": "%0", "T1": "%1", "T2": "%2", "T3": "%3"}
        result = generate_panes_json("vnx-test", panes, config)
        assert "T0" in result
        assert "T1" in result
        assert "T2" in result
        assert "T3" in result

    def test_t0_is_orchestrator(self, config):
        panes = {"T0": "%0", "T1": "%1", "T2": "%2", "T3": "%3"}
        result = generate_panes_json("vnx-test", panes, config)
        assert result["T0"]["role"] == "orchestrator"
        assert result["T0"]["do_not_target"] is True

    def test_tracks_present(self, config):
        panes = {"T0": "%0", "T1": "%1", "T2": "%2", "T3": "%3"}
        result = generate_panes_json("vnx-test", panes, config)
        assert "tracks" in result
        assert "A" in result["tracks"]
        assert "B" in result["tracks"]
        assert "C" in result["tracks"]

    def test_t3_has_deep_role(self, config):
        panes = {"T0": "%0", "T1": "%1", "T2": "%2", "T3": "%3"}
        result = generate_panes_json("vnx-test", panes, config)
        assert result["T3"].get("role") == "deep"


class TestGenerateTerminalStateJson:
    def test_has_schema_version(self):
        result = generate_terminal_state_json()
        assert result["schema_version"] == 1

    def test_all_terminals_idle(self):
        result = generate_terminal_state_json()
        for tid in ("T1", "T2", "T3"):
            assert result["terminals"][tid]["status"] == "idle"
            assert result["terminals"][tid]["version"] == 1

    def test_has_last_activity(self):
        result = generate_terminal_state_json()
        for tid in ("T1", "T2", "T3"):
            assert "last_activity" in result["terminals"][tid]

    def test_mode_independence(self):
        """A-R1: Same state structure regardless of mode."""
        result1 = generate_terminal_state_json()
        result2 = generate_terminal_state_json()
        assert result1["schema_version"] == result2["schema_version"]
        assert set(result1["terminals"].keys()) == set(result2["terminals"].keys())


class TestWriteStateFiles:
    def test_writes_both_files(self, tmp_path):
        config = StartConfig(
            project_root="/project",
            vnx_home="/vnx",
            vnx_data_dir="/data",
            terminals={
                "T0": TerminalConfig("T0", PROVIDER_CLAUDE, "default", "orchestrator"),
                "T1": TerminalConfig("T1", PROVIDER_CLAUDE, "sonnet", "worker", "A"),
                "T2": TerminalConfig("T2", PROVIDER_CLAUDE, "sonnet", "worker", "B"),
                "T3": TerminalConfig("T3", PROVIDER_CLAUDE, "default", "worker", "C"),
            },
        )
        pane_ids = {"T0": "%0", "T1": "%1", "T2": "%2", "T3": "%3"}
        state_dir = str(tmp_path / "state")

        write_state_files(state_dir, "vnx-test", pane_ids, config)

        assert (tmp_path / "state" / "panes.json").exists()
        assert (tmp_path / "state" / "terminal_state.json").exists()

        panes = json.loads((tmp_path / "state" / "panes.json").read_text())
        assert panes["session"] == "vnx-test"

        ts = json.loads((tmp_path / "state" / "terminal_state.json").read_text())
        assert ts["schema_version"] == 1


# ---------------------------------------------------------------------------
# Profile/preset resolution
# ---------------------------------------------------------------------------

class TestResolvePreset:
    def test_preset_found(self, tmp_path):
        presets = tmp_path / "presets"
        presets.mkdir()
        (presets / "fast.env").write_text("VNX_T1_PROVIDER=codex\n")

        result = resolve_preset(preset_name="fast", presets_dir=str(presets))
        assert result.source == "preset"
        assert result.name == "fast"
        assert result.error == ""

    def test_preset_not_found(self, tmp_path):
        presets = tmp_path / "presets"
        presets.mkdir()

        result = resolve_preset(preset_name="missing", presets_dir=str(presets))
        assert result.source == "preset"
        assert result.error != ""

    def test_last_used(self, tmp_path):
        presets = tmp_path / "presets"
        presets.mkdir()
        target = presets / "fast.env"
        target.write_text("VNX_T1_PROVIDER=codex\n")
        last = presets / "last-used.env"
        last.symlink_to(target)

        result = resolve_preset(use_last=True, presets_dir=str(presets))
        assert result.source == "last"

    def test_last_not_found(self, tmp_path):
        presets = tmp_path / "presets"
        presets.mkdir()

        result = resolve_preset(use_last=True, presets_dir=str(presets))
        assert result.source == "last"
        assert result.error != ""

    def test_profile_found(self, tmp_path):
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "dev.env").write_text("VNX_T1_PROVIDER=claude_code\n")

        result = resolve_preset(profile_name="dev", profiles_dir=str(profiles))
        assert result.source == "profile"
        assert result.name == "dev"

    def test_config_env_fallback(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("VNX_T1_MODEL=sonnet\n")

        result = resolve_preset(config_env=str(config))
        assert result.source == "config"

    def test_default_fallback(self):
        result = resolve_preset()
        assert result.source == "default"
        assert result.env_file == ""

    def test_priority_preset_over_profile(self, tmp_path):
        presets = tmp_path / "presets"
        presets.mkdir()
        (presets / "fast.env").write_text("")
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "dev.env").write_text("")

        result = resolve_preset(
            preset_name="fast",
            profile_name="dev",
            presets_dir=str(presets),
            profiles_dir=str(profiles),
        )
        assert result.source == "preset"


# ---------------------------------------------------------------------------
# StartConfig.from_env
# ---------------------------------------------------------------------------

class TestStartConfigFromEnv:
    def test_defaults(self):
        env = {
            "PROJECT_ROOT": "/project",
            "VNX_HOME": "/vnx",
            "VNX_DATA_DIR": "/data",
        }
        with patch.dict(os.environ, env, clear=True):
            config = StartConfig.from_env()
            assert config.project_root == "/project"
            assert "T0" in config.terminals
            assert "T1" in config.terminals
            assert "T2" in config.terminals
            assert "T3" in config.terminals
            assert config.terminals["T0"].role == "orchestrator"
            assert config.terminals["T1"].role == "worker"

    def test_custom_providers(self):
        env = {
            "PROJECT_ROOT": "/project",
            "VNX_HOME": "/vnx",
            "VNX_DATA_DIR": "/data",
            "VNX_T1_PROVIDER": "codex_cli",
            "VNX_T2_PROVIDER": "gemini_cli",
        }
        with patch.dict(os.environ, env, clear=True):
            config = StartConfig.from_env()
            assert config.terminals["T1"].provider == PROVIDER_CODEX
            assert config.terminals["T2"].provider == PROVIDER_GEMINI

    def test_skip_permissions(self):
        env = {
            "PROJECT_ROOT": "/project",
            "VNX_HOME": "/vnx",
            "VNX_DATA_DIR": "/data",
            "VNX_T1_SKIP_PERMISSIONS": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            config = StartConfig.from_env()
            assert config.terminals["T1"].skip_permissions is True
            assert config.terminals["T2"].skip_permissions is False


# ---------------------------------------------------------------------------
# Launch command building
# ---------------------------------------------------------------------------

class TestBuildLaunchCommand:
    def test_contains_env_clean_and_set(self):
        tc = TerminalConfig("T1", PROVIDER_CLAUDE, "sonnet", "worker", "A")
        config = StartConfig(
            project_root="/project",
            vnx_home="/vnx",
            vnx_data_dir="/data",
            terminals={"T1": tc},
        )
        cmd = build_launch_command(tc, config, "/terms")
        assert "unset " in cmd
        assert "PROJECT_ROOT=" in cmd
        assert "cd '/terms/T1'" in cmd
        assert "claude --model sonnet" in cmd

    def test_includes_track_for_workers(self):
        tc = TerminalConfig("T2", PROVIDER_CLAUDE, "sonnet", "worker", "B")
        config = StartConfig(
            project_root="/project",
            vnx_home="/vnx",
            vnx_data_dir="/data",
            terminals={"T2": tc},
        )
        cmd = build_launch_command(tc, config, "/terms")
        assert "CLAUDE_TRACK=B" in cmd

    def test_no_track_for_orchestrator(self):
        tc = TerminalConfig("T0", PROVIDER_CLAUDE, "default", "orchestrator")
        config = StartConfig(
            project_root="/project",
            vnx_home="/vnx",
            vnx_data_dir="/data",
            terminals={"T0": tc},
        )
        cmd = build_launch_command(tc, config, "/terms")
        assert "CLAUDE_TRACK" not in cmd
