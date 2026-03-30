#!/usr/bin/env python3
"""Tests for VNX Mode — detection, storage, and command gating (PR-2).

Validates mode persistence, command tier enforcement, feature flags,
and backward compatibility with pre-init state.
"""

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_mode import (
    VNXMode,
    ModeGateError,
    TIER_UNIVERSAL,
    TIER_STARTER_OPERATOR,
    TIER_OPERATOR_ONLY,
    TIER_DEMO_ONLY,
    MODE_COMMANDS,
    read_mode,
    write_mode,
    read_mode_raw,
    check_command_allowed,
    get_available_commands,
    get_mode_description,
    is_feature_enabled,
    check_mode_feature_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def data_dir(tmp_path):
    """Create a temp .vnx-data directory and set VNX_DATA_DIR."""
    d = tmp_path / ".vnx-data"
    d.mkdir()
    old = os.environ.get("VNX_DATA_DIR")
    os.environ["VNX_DATA_DIR"] = str(d)
    yield d
    if old:
        os.environ["VNX_DATA_DIR"] = old
    else:
        os.environ.pop("VNX_DATA_DIR", None)


# ---------------------------------------------------------------------------
# Mode read/write
# ---------------------------------------------------------------------------

class TestModeReadWrite:
    def test_read_mode_returns_none_when_no_file(self, data_dir):
        assert read_mode(str(data_dir)) is None

    def test_write_and_read_starter(self, data_dir):
        write_mode(VNXMode.STARTER, str(data_dir))
        assert read_mode(str(data_dir)) == VNXMode.STARTER

    def test_write_and_read_operator(self, data_dir):
        write_mode(VNXMode.OPERATOR, str(data_dir))
        assert read_mode(str(data_dir)) == VNXMode.OPERATOR

    def test_write_and_read_demo(self, data_dir):
        write_mode(VNXMode.DEMO, str(data_dir))
        assert read_mode(str(data_dir)) == VNXMode.DEMO

    def test_mode_file_is_valid_json(self, data_dir):
        write_mode(VNXMode.STARTER, str(data_dir))
        mode_file = data_dir / "mode.json"
        data = json.loads(mode_file.read_text())
        assert data["mode"] == "starter"
        assert data["schema_version"] == 1
        assert "set_at" in data

    def test_read_mode_raw(self, data_dir):
        write_mode(VNXMode.OPERATOR, str(data_dir))
        raw = read_mode_raw(str(data_dir))
        assert raw["mode"] == "operator"
        assert "set_at" in raw

    def test_read_mode_handles_corrupt_json(self, data_dir):
        (data_dir / "mode.json").write_text("not json")
        assert read_mode(str(data_dir)) is None

    def test_read_mode_handles_missing_key(self, data_dir):
        (data_dir / "mode.json").write_text('{"schema_version": 1}')
        assert read_mode(str(data_dir)) is None

    def test_write_mode_overwrites(self, data_dir):
        write_mode(VNXMode.STARTER, str(data_dir))
        assert read_mode(str(data_dir)) == VNXMode.STARTER
        write_mode(VNXMode.OPERATOR, str(data_dir))
        assert read_mode(str(data_dir)) == VNXMode.OPERATOR

    def test_read_mode_from_env(self, data_dir):
        """read_mode() without args uses VNX_DATA_DIR from env."""
        write_mode(VNXMode.STARTER, str(data_dir))
        assert read_mode() == VNXMode.STARTER


# ---------------------------------------------------------------------------
# Command gating
# ---------------------------------------------------------------------------

class TestCommandGating:
    def test_universal_commands_allowed_in_all_modes(self, data_dir):
        for mode in VNXMode:
            for cmd in TIER_UNIVERSAL:
                check_command_allowed(cmd, mode)  # Should not raise

    def test_starter_allows_tier2(self, data_dir):
        for cmd in TIER_STARTER_OPERATOR:
            check_command_allowed(cmd, VNXMode.STARTER)  # Should not raise

    def test_starter_blocks_operator_commands(self, data_dir):
        for cmd in TIER_OPERATOR_ONLY:
            with pytest.raises(ModeGateError) as exc:
                check_command_allowed(cmd, VNXMode.STARTER)
            assert "starter" in str(exc.value)
            assert "vnx init --operator" in str(exc.value)

    def test_operator_allows_everything(self, data_dir):
        all_cmds = TIER_UNIVERSAL | TIER_STARTER_OPERATOR | TIER_OPERATOR_ONLY
        for cmd in all_cmds:
            check_command_allowed(cmd, VNXMode.OPERATOR)

    def test_demo_blocks_non_universal(self, data_dir):
        for cmd in TIER_OPERATOR_ONLY:
            with pytest.raises(ModeGateError):
                check_command_allowed(cmd, VNXMode.DEMO)

    def test_demo_allows_demo_command(self, data_dir):
        check_command_allowed("demo", VNXMode.DEMO)

    def test_demo_error_suggests_init(self, data_dir):
        with pytest.raises(ModeGateError) as exc:
            check_command_allowed("start", VNXMode.DEMO)
        assert "vnx init" in str(exc.value)

    def test_pre_init_allows_everything(self, data_dir):
        """No mode.json = pre-init state, all commands allowed."""
        check_command_allowed("start")  # Should not raise
        check_command_allowed("demo")

    def test_gating_reads_from_file(self, data_dir):
        write_mode(VNXMode.STARTER, str(data_dir))
        with pytest.raises(ModeGateError):
            check_command_allowed("start")  # reads mode from file


# ---------------------------------------------------------------------------
# Available commands
# ---------------------------------------------------------------------------

class TestAvailableCommands:
    def test_starter_command_count(self):
        cmds = get_available_commands(VNXMode.STARTER)
        assert cmds == TIER_UNIVERSAL | TIER_STARTER_OPERATOR

    def test_operator_has_most_commands(self):
        starter = get_available_commands(VNXMode.STARTER)
        operator = get_available_commands(VNXMode.OPERATOR)
        assert len(operator) > len(starter)

    def test_pre_init_returns_all(self, data_dir):
        cmds = get_available_commands(None)
        assert TIER_UNIVERSAL <= cmds
        assert TIER_OPERATOR_ONLY <= cmds


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_starter_enabled_by_default(self):
        os.environ.pop("VNX_STARTER_MODE_ENABLED", None)
        assert check_mode_feature_enabled(VNXMode.STARTER) is True

    def test_starter_disabled_by_flag(self):
        os.environ["VNX_STARTER_MODE_ENABLED"] = "0"
        assert check_mode_feature_enabled(VNXMode.STARTER) is False
        os.environ.pop("VNX_STARTER_MODE_ENABLED")

    def test_demo_enabled_by_default(self):
        os.environ.pop("VNX_DEMO_MODE_ENABLED", None)
        assert check_mode_feature_enabled(VNXMode.DEMO) is True

    def test_operator_always_enabled(self):
        assert check_mode_feature_enabled(VNXMode.OPERATOR) is True

    def test_mode_gating_flag(self):
        os.environ.pop("VNX_MODE_GATING_ENABLED", None)
        assert is_feature_enabled("VNX_MODE_GATING_ENABLED") is True
        os.environ["VNX_MODE_GATING_ENABLED"] = "0"
        assert is_feature_enabled("VNX_MODE_GATING_ENABLED") is False
        os.environ.pop("VNX_MODE_GATING_ENABLED")


# ---------------------------------------------------------------------------
# Mode descriptions
# ---------------------------------------------------------------------------

class TestModeDescriptions:
    def test_all_modes_have_descriptions(self):
        for mode in VNXMode:
            desc = get_mode_description(mode)
            assert len(desc) > 10

    def test_vnx_mode_str(self):
        assert str(VNXMode.STARTER) == "starter"
        assert str(VNXMode.OPERATOR) == "operator"
        assert str(VNXMode.DEMO) == "demo"


# ---------------------------------------------------------------------------
# Tier completeness
# ---------------------------------------------------------------------------

class TestTierCompleteness:
    def test_no_overlap_between_exclusive_tiers(self):
        assert TIER_OPERATOR_ONLY & TIER_DEMO_ONLY == frozenset()
        assert TIER_UNIVERSAL & TIER_OPERATOR_ONLY == frozenset()
        assert TIER_UNIVERSAL & TIER_DEMO_ONLY == frozenset()

    def test_operator_mode_includes_universal_and_starter(self):
        op = MODE_COMMANDS[VNXMode.OPERATOR]
        assert TIER_UNIVERSAL <= op
        assert TIER_STARTER_OPERATOR <= op
        assert TIER_OPERATOR_ONLY <= op
