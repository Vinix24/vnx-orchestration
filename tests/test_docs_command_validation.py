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
        if cmd and cmd not in ("init", "doctor", "status", "help"):
            commands.add(cmd)
        elif cmd:
            commands.add(cmd)
    return commands


# A case branch label: an indented lowercase word followed by `)`.
# Necessary but NOT sufficient — see _extract_bin_vnx_commands (OI-1482).
_BRANCH_RE = re.compile(r"^\s+([a-z][a-z0-9_-]*)\)")
# `<<DELIM`, `<<'DELIM'`, `<<"DELIM"`, and the `<<-` indented variant.
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_CASE_OPEN_RE = re.compile(r"(^|\s|;)case\s.*\sin\s*$")
_ESAC_RE = re.compile(r"^\s*esac\b")


def _extract_bin_vnx_commands(vnx_path: Path) -> set:
    """Extract command case branches from bin/vnx.

    OI-1482: this used to run ``_BRANCH_RE`` over the whole file and return
    every match, which is the label shape without any of the structure that
    makes a label a BRANCH. Two conditions are now checked, both measured
    against the real file rather than assumed:

    1. **Not inside a heredoc.** This is the one that actually bit. bin/vnx
       embeds its own help text in ``cat <<'HELP'`` blocks, and prose wraps
       wherever it wraps. On 96c8bdd6 a help line broke onto
       ``sentence), REACH (...`` and this function reported a ``sentence``
       command that no tier declares, turning VNX CI red on a help-text
       rewording. Measured: heredoc-awareness alone drops exactly that one
       phantom and nothing else, 69 names -> 68.
    2. **Inside a ``case ... in`` / ``esac`` block.** Redundant on today's
       file (both conditions give 68), and kept because it is what the
       function's own name claims to check. A label outside every case block
       is not a branch no matter how it is spelled.

    What is deliberately NOT required is a ``;;`` terminator, even though a
    proximity check on it would also have caught the phantom. Two reasons,
    both measured: bash accepts a final branch without ``;;`` before ``esac``
    (verified: ``case $x in a) echo one ;; b) echo two esac`` passes
    ``bash -n`` and prints ``two``), so requiring one would reject a legal
    branch; and a naive "``;;`` within N lines" window drops
    ``regen-worker-permissions``, a real branch whose body is long — the
    fix would have quietly deleted a genuine command from the check.

    This stays a small state machine and must never grow into a bash parser,
    the same discipline ``scripts/commands/t0_role_audit.sh`` states for its
    own frontmatter check.
    """
    commands = set()
    heredoc_delim = None
    case_depth = 0

    for line in vnx_path.read_text().splitlines():
        if heredoc_delim is not None:
            if line.strip() == heredoc_delim:
                heredoc_delim = None
            continue

        stripped = line.lstrip()
        if not stripped.startswith("#"):
            opened = _HEREDOC_RE.search(line)
            if opened:
                heredoc_delim = opened.group(2)
                continue

        if _CASE_OPEN_RE.search(line):
            case_depth += 1
            continue
        if _ESAC_RE.match(line):
            case_depth = max(0, case_depth - 1)
            continue

        branch = _BRANCH_RE.match(line)
        if branch and case_depth > 0:
            commands.add(branch.group(1))

    return commands


def _all_mode_commands() -> set:
    """All commands across all modes in vnx_mode.py."""
    all_cmds = set()
    all_cmds |= TIER_UNIVERSAL
    all_cmds |= TIER_STARTER_OPERATOR
    all_cmds |= TIER_OPERATOR_ONLY
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
            (TIER_STARTER_OPERATOR, TIER_OPERATOR_ONLY),
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

    def test_every_command_in_at_least_one_mode(self):
        all_cmds = _all_mode_commands()
        for mode in VNXMode:
            for cmd in MODE_COMMANDS[mode]:
                assert cmd in all_cmds


# ---------------------------------------------------------------------------
# bin/vnx commands vs vnx_mode.py
# ---------------------------------------------------------------------------

class TestBinVnxCommandExtraction:
    """The extractor itself (OI-1482), on fixtures rather than on bin/vnx.

    The two tests it broke are census tests over the real file: they answer
    "is every command covered" and cannot tell a parser bug from a coverage
    gap. These answer the prior question — does the parser return branches at
    all — so the census tests are read for what they measure.
    """

    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "vnx"
        path.write_text(body, encoding="utf-8")
        return path

    def test_help_heredoc_line_is_not_a_command(self, tmp_path):
        """The measured OI-1482 defect, in miniature.

        RED on the old parser: it returned {'start', 'drift', 'sentence'} and
        the phantom went on to fail the tier-coverage assertion.
        """
        vnx = self._write(tmp_path, """
main() {
  case "$cmd" in
    start)
      cmd_start "$@"
      ;;
    drift)
      cmd_drift "$@"
      ;;
  esac
}

cmd_help() {
    cat <<'HELP'
Usage: vnx <command>

  drift                      CONTENT is a diff against the canon, never a
                             search for a known phrase, and never a known
                             sentence), REACH asks whether anything loads it.
HELP
}
""")

        assert _extract_bin_vnx_commands(vnx) == {"start", "drift"}

    def test_a_long_bodied_branch_is_still_found(self, tmp_path):
        """The shape a naive `;;`-proximity fix would have deleted.

        `regen-worker-permissions` in the real bin/vnx is a genuine branch
        whose `;;` sits more than 8 lines below its label — the same distance
        as the phantom's. A window heuristic cannot separate them, which is
        why this parser uses structure and not distance.
        """
        vnx = self._write(tmp_path, """
  case "$cmd" in
    regen-worker-permissions)
      if ! type cmd_regen &>/dev/null; then
        _load_command "regen" 2>/dev/null || true
      fi
      if type cmd_regen &>/dev/null; then
        cmd_regen "$@"
      else
        err "[regen] command not available"
        exit 1
      fi
      ;;
  esac
""")

        assert "regen-worker-permissions" in _extract_bin_vnx_commands(vnx)

    def test_a_label_outside_every_case_block_is_not_a_branch(self, tmp_path):
        vnx = self._write(tmp_path, """
# A doc comment block written as plain text:
    orphan) this line looks like a branch and is not one

  case "$cmd" in
    real)
      do_thing
      ;;
  esac
""")

        assert _extract_bin_vnx_commands(vnx) == {"real"}

    def test_a_final_branch_without_a_terminator_is_still_a_branch(self, tmp_path):
        """bash accepts it, so the parser must too.

        Verified against bash itself: `case $x in aaa) echo one ;; bbb) echo
        two esac` passes `bash -n` and prints `two`. Requiring `;;` would
        reject a legal command.
        """
        vnx = self._write(tmp_path, """
  case "$cmd" in
    aaa)
      echo one
      ;;
    bbb)
      echo two
  esac
""")

        assert _extract_bin_vnx_commands(vnx) == {"aaa", "bbb"}

    def test_nested_case_blocks_keep_their_branches(self, tmp_path):
        """An inner `case` must not close the outer one on its `esac`."""
        vnx = self._write(tmp_path, """
  case "$cmd" in
    outer)
      case "$sub" in
        inner)
          do_inner
          ;;
      esac
      ;;
    sibling)
      do_sibling
      ;;
  esac
""")

        assert _extract_bin_vnx_commands(vnx) == {"outer", "inner", "sibling"}

    def test_unquoted_and_indented_heredocs_are_both_skipped(self, tmp_path):
        vnx = self._write(tmp_path, """
  case "$cmd" in
    real)
      cat <<USAGE
    unquoted) not a branch
USAGE
      cat <<-'INDENTED'
    dashed) also not a branch
	INDENTED
      ;;
  esac
""")

        assert _extract_bin_vnx_commands(vnx) == {"real"}

    def test_the_real_bin_vnx_yields_a_non_trivial_set(self):
        """Guard against a vacuous pass.

        Both census tests below read this function's output. An extractor
        that returned nothing would make `uncovered` empty and the coverage
        test green while checking nothing — a zero count read as a clean
        result, which is its own recurring failure mode here.
        """
        vnx_path = REPO_ROOT / "bin" / "vnx"
        if not vnx_path.exists():
            pytest.skip("bin/vnx not found")

        commands = _extract_bin_vnx_commands(vnx_path)

        assert len(commands) > 40, f"suspiciously few branches parsed: {len(commands)}"
        for known in ("init", "doctor", "start", "dispatch", "regen-worker-permissions"):
            assert known in commands, f"{known} is a real bin/vnx command and must parse"


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
        # Pip-CLI-only commands (vnx_cli/main.py) — real commands the README documents that are not
        # in the bash mode tiers or bin/vnx case branches (they route through `python -m vnx_cli.main`).
        pip_surface = {"dispatch-agent", "version", "horizon", "objective", "deliverable"}
        all_known = mode_cmds | bin_cmds | pip_surface
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
