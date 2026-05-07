#!/usr/bin/env python3
"""Tests that gemini-2.5-pro is the default model across VNX entry points."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_start_runtime import (
    PROVIDER_GEMINI,
    StartConfig,
    TerminalConfig,
    build_provider_command,
)


class TestVnxStartRuntimeGeminiDefault:
    def test_start_config_field_default_is_pro(self):
        config = StartConfig(project_root="/p", vnx_home="/v", vnx_data_dir="/d")
        assert config.gemini_model == "gemini-2.5-pro"

    def test_from_env_default_is_pro(self):
        env = {"PROJECT_ROOT": "/p", "VNX_HOME": "/v", "VNX_DATA_DIR": "/d"}
        with patch.dict(os.environ, env, clear=True):
            config = StartConfig.from_env()
        assert config.gemini_model == "gemini-2.5-pro"

    def test_from_env_override_with_flash(self):
        env = {
            "PROJECT_ROOT": "/p",
            "VNX_HOME": "/v",
            "VNX_DATA_DIR": "/d",
            "VNX_GEMINI_MODEL": "gemini-2.5-flash",
        }
        with patch.dict(os.environ, env, clear=True):
            config = StartConfig.from_env()
        assert config.gemini_model == "gemini-2.5-flash"

    def test_build_provider_command_default_is_pro(self):
        tc = TerminalConfig("T3", PROVIDER_GEMINI, "gemini-2.5-pro", "worker", "C")
        cmd = build_provider_command(tc, project_root="/p")
        assert "gemini-2.5-pro" in cmd

    def test_build_provider_command_flash_override(self):
        tc = TerminalConfig("T3", PROVIDER_GEMINI, "gemini-2.5-flash", "worker", "C")
        cmd = build_provider_command(tc, gemini_model="gemini-2.5-flash", project_root="/p")
        assert "gemini-2.5-flash" in cmd
