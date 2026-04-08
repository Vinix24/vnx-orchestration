#!/usr/bin/env python3
"""Docs-vs-behavior validation tests (PR-7).

Validates that README command tables, mode tier definitions, and public
documentation match the actual command surface in bin/vnx and vnx_mode.py.

Gate: gate_pr7_qa_and_certification — docs validated against behavior.
"""

import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_mode import (
    TIER_UNIVERSAL,
    TIER_STARTER_OPERATOR,
    TIER_OPERATOR_ONLY,
    TIER_DEMO_ONLY,
    MODE_COMMANDS,
    VNXMode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_readme_commands(readme_path: Path) -> set:
    """Extract `vnx <command>` from README command tables."""
    text = readme_path.read_text()
    # Match patterns like `vnx init`, `vnx doctor`, `vnx start [profile]`
    pattern = r"`vnx\s+([a-z][a-z0-9_-]*)"
    matches = re.findall(pattern, text)
    # Normalize: strip trailing arguments, deduplicate
    commands = set()
    for m in matches:
        cmd = m.split()[0].strip("`").strip()
        if cmd and cmd not in ("demo", "init", "doctor", "status", "help"):
            commands.add(cmd)
        elif cmd:
            commands.add(cmd)
    return commands


def _extract_bin_vnx_commands(vnx_path: Path) -> set:
    """Extract command case branches from bin/vnx."""
    text = vnx_path.read_text()
    # Match case branches like:  init)  or  staging-list)
    pattern = r"^\s+([a-z][a-z0-9_-]*)\)"
    matches = re.findall(pattern, text, re.MULTILINE)
    return set(matches)


def _all_mode_commands() -> set:
    """All commands across all modes in vnx_mode.py."""
    all_cmds = set()
    all_cmds |= TIER_UNIVERSAL
    all_cmds |= TIER_STARTER_OPERATOR
    all_cmds |= TIER_OPERATOR_ONLY
    all_cmds |= TIER_DEMO_ONLY
    return all_cmds


# ---------------------------------------------------------------------------
# Mode tier consistency
# ---------------------------------------------------------------------------

class TestModeTierConsistency:
    """vnx_mode.py tier sets must be non-overlapping and complete."""

    def test_tiers_are_disjoint(self):
        pairs = [
            (TIER_UNIVERSAL, TIER_STARTER_OPERATOR),
            (TIER_UNIVERSAL, TIER_OPERATOR_ONLY),
            (TIER_UNIVERSAL, TIER_DEMO_ONLY),
            (TIER_STARTER_OPERATOR, TIER_OPERATOR_ONLY),
            (TIER_STARTER_OPERATOR, TIER_DEMO_ONLY),
            (TIER_OPERATOR_ONLY, TIER_DEMO_ONLY),
        ]
        for a, b in pairs:
            overlap = a & b
            assert not overlap, f"Tier overlap: {overlap}"

    def test_starter_includes_universal(self):
        starter_cmds = MODE_COMMANDS[VNXMode.STARTER]
        assert TIER_UNIVERSAL.issubset(starter_cmds)

    def test_operator_includes_all_non_demo(self):
        operator_cmds = MODE_COMMANDS[VNXMode.OPERATOR]
        expected = TIER_UNIVERSAL | TIER_STARTER_OPERATOR | TIER_OPERATOR_ONLY
        assert expected == operator_cmds

    def test_demo_includes_universal_and_demo_only(self):
        demo_cmds = MODE_COMMANDS[VNXMode.DEMO]
        expected = TIER_UNIVERSAL | TIER_DEMO_ONLY
        assert expected == demo_cmds

    def test_every_command_in_at_least_one_mode(self):
        all_cmds = _all_mode_commands()
        for mode in VNXMode:
            for cmd in MODE_COMMANDS[mode]:
                assert cmd in all_cmds


# ---------------------------------------------------------------------------
# bin/vnx commands vs vnx_mode.py
# ---------------------------------------------------------------------------

class TestBinVnxVsModeCommands:
    """Commands handled in bin/vnx must be registered in vnx_mode.py tiers."""

    def test_bin_commands_covered_by_mode_tiers(self):
        vnx_path = REPO_ROOT / "bin" / "vnx"
        if not vnx_path.exists():
            pytest.skip("bin/vnx not found")
        bin_cmds = _extract_bin_vnx_commands(vnx_path)
        mode_cmds = _all_mode_commands()
        # Filter out internal-only commands not meant for mode gating
        internal = {"help", "version", "--help", "-h"}
        bin_cmds -= internal
        uncovered = bin_cmds - mode_cmds
        assert not uncovered, \
            f"bin/vnx commands not in vnx_mode.py tiers: {uncovered}"

    def test_mode_commands_implemented_in_bin(self):
        vnx_path = REPO_ROOT / "bin" / "vnx"
        if not vnx_path.exists():
            pytest.skip("bin/vnx not found")
        bin_cmds = _extract_bin_vnx_commands(vnx_path)
        mode_cmds = _all_mode_commands()
        # Commands in mode tiers that have no bin/vnx handler
        unimplemented = mode_cmds - bin_cmds
        # Allow commands handled via flags, subcommands, or aliases
        allowed_missing = {"version", "help"}
        unimplemented -= allowed_missing
        assert not unimplemented, \
            f"vnx_mode.py commands missing from bin/vnx: {unimplemented}"


# ---------------------------------------------------------------------------
# README commands vs mode tiers
# ---------------------------------------------------------------------------

class TestReadmeVsModeTiers:
    """Commands documented in README must exist in the actual command surface."""

    def test_readme_commands_exist_in_mode_tiers(self):
        readme = REPO_ROOT / "README.md"
        if not readme.exists():
            pytest.skip("README.md not found")
        readme_cmds = _extract_readme_commands(readme)
        mode_cmds = _all_mode_commands()
        # Also check bin/vnx for commands README mentions
        vnx_path = REPO_ROOT / "bin" / "vnx"
        bin_cmds = _extract_bin_vnx_commands(vnx_path) if vnx_path.exists() else set()
        all_known = mode_cmds | bin_cmds
        # Remove non-command words and subcommand prefixes that regex picks up
        noise = {"clone", "install", "path", "cd", "brew", "ref", "pip", "worktree"}
        readme_cmds -= noise
        phantom = readme_cmds - all_known
        assert not phantom, \
            f"README documents commands that don't exist: {phantom}"


# ---------------------------------------------------------------------------
# Productization contract mode definitions
# ---------------------------------------------------------------------------

class TestProductizationContract:
    """PRODUCTIZATION_CONTRACT.md mode definitions must match vnx_mode.py."""

    def test_contract_exists(self):
        contract = REPO_ROOT / "docs" / "contracts" / "PRODUCTIZATION_CONTRACT.md"
        assert contract.exists(), "docs/contracts/PRODUCTIZATION_CONTRACT.md missing"

    def test_contract_mentions_all_modes(self):
        contract = REPO_ROOT / "docs" / "contracts" / "PRODUCTIZATION_CONTRACT.md"
        if not contract.exists():
            pytest.skip("docs/contracts/PRODUCTIZATION_CONTRACT.md missing")
        text = contract.read_text().lower()
        for mode in VNXMode:
            assert mode.value in text, \
                f"Mode '{mode.value}' not mentioned in productization contract"


# ---------------------------------------------------------------------------
# install.sh help text consistency
# ---------------------------------------------------------------------------

class TestInstallHelpConsistency:
    """install.sh help text must match actual post-install commands."""

    def test_install_help_mentions_setup(self):
        install_sh = REPO_ROOT / "install.sh"
        if not install_sh.exists():
            pytest.skip("install.sh not found")
        text = install_sh.read_text()
        assert "vnx setup" in text or "setup" in text, \
            "install.sh help should mention vnx setup"

    def test_install_help_mentions_starter(self):
        install_sh = REPO_ROOT / "install.sh"
        if not install_sh.exists():
            pytest.skip("install.sh not found")
        text = install_sh.read_text()
        assert "starter" in text.lower(), \
            "install.sh help should mention starter mode"
