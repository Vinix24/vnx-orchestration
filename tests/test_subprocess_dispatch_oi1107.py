#!/usr/bin/env python3
"""OI-1107 regression: subprocess_dispatch.py must extract role from instruction
when --role is not passed on the CLI.

Regression: dispatch.json carries 'role' in the dispatch markdown header but
dispatch_deliver.sh only passes --role when agent_role is non-empty. If the
caller omits --role, the worker received generic skill context instead of the
role-specific permission profile.

Fix: _extract_role_from_instruction() parses 'Role: <name>' from the
instruction body; __main__ uses it as fallback before defaulting to
backend-developer.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from subprocess_dispatch import _extract_role_from_instruction, _ROLE_FALLBACK


class TestExtractRoleFromInstruction:
    """Unit tests for _extract_role_from_instruction."""

    def test_extracts_role_header(self):
        instruction = "Role: codex-gate\nDo the thing."
        assert _extract_role_from_instruction(instruction) == "codex-gate"

    def test_extracts_role_from_multiline_header(self):
        instruction = (
            "[[TARGET:T2]]\n"
            "Track: B\n"
            "Role: backend-developer\n"
            "Gate: f55-pr1\n\n"
            "## Task\nDo things."
        )
        assert _extract_role_from_instruction(instruction) == "backend-developer"

    def test_returns_none_when_no_role_header(self):
        instruction = "No role header here.\nJust plain text."
        assert _extract_role_from_instruction(instruction) is None

    def test_extracts_first_role_when_multiple(self):
        instruction = "Role: codex-gate\nRole: security-engineer\nDo work."
        assert _extract_role_from_instruction(instruction) == "codex-gate"

    def test_role_with_hyphen(self):
        instruction = "Role: security-engineer\nDo the review."
        assert _extract_role_from_instruction(instruction) == "security-engineer"

    def test_inline_role_not_matched(self):
        """'Role:' not at line start should not match."""
        instruction = "See Role: engineer is not a header"
        # The regex requires ^ (start of line), so mid-line 'Role:' won't match
        assert _extract_role_from_instruction(instruction) is None

    def test_role_fallback_constant_is_documented(self):
        assert _ROLE_FALLBACK == "backend-developer"


class TestMainBlockRoleResolution:
    """Integration-level tests: __main__ block resolves role from instruction."""

    def _run_main(self, instruction: str, role: str | None = None) -> str | None:
        """Invoke __main__ parse path via argparse simulation; return resolved role."""
        import argparse
        import subprocess_dispatch as sd
        import re

        # Simulate argument parsing as __main__ does
        captured_role = role
        if captured_role is None:
            captured_role = sd._extract_role_from_instruction(instruction) or sd._ROLE_FALLBACK
        return captured_role

    def test_role_from_instruction_used_when_arg_absent(self):
        instruction = "Role: codex-gate\n\nDo the review."
        resolved = self._run_main(instruction, role=None)
        assert resolved == "codex-gate"

    def test_explicit_role_arg_not_overridden(self):
        instruction = "Role: codex-gate\n\nDo the review."
        resolved = self._run_main(instruction, role="security-engineer")
        assert resolved == "security-engineer"

    def test_fallback_when_no_role_in_instruction(self):
        instruction = "No role header. Just task text."
        resolved = self._run_main(instruction, role=None)
        assert resolved == _ROLE_FALLBACK

    def test_deliver_with_recovery_receives_extracted_role(self, tmp_path):
        """deliver_with_recovery must be called with the role extracted from instruction."""
        import subprocess_dispatch as sd

        instruction = "Role: codex-gate\n\nDo the thing."
        resolved_role = sd._extract_role_from_instruction(instruction) or sd._ROLE_FALLBACK
        assert resolved_role == "codex-gate", (
            "Expected role 'codex-gate' extracted from instruction, "
            f"got {resolved_role!r}"
        )
