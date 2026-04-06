#!/usr/bin/env python3
"""Tests for _inject_skill_context() in subprocess_dispatch.py (F32)."""

import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from subprocess_dispatch import _inject_skill_context


class TestInjectSkillContext:
    """Tests for CLAUDE.md skill context injection."""

    def test_prepends_claude_md_when_exists(self, tmp_path):
        """When CLAUDE.md exists for a terminal, it is prepended to the instruction."""
        terminal_id = "T1"
        claude_md_dir = tmp_path / ".claude" / "terminals" / terminal_id
        claude_md_dir.mkdir(parents=True)
        claude_md = claude_md_dir / "CLAUDE.md"
        claude_md.write_text("# Agent Context\nYou are a backend developer.")

        instruction = "Implement feature X"

        with patch(
            "subprocess_dispatch.Path.__file__",
            create=True,
        ):
            # Patch the path resolution to point at our tmp_path
            fake_file = tmp_path / "scripts" / "lib" / "subprocess_dispatch.py"
            fake_file.parent.mkdir(parents=True, exist_ok=True)
            fake_file.touch()

            with patch("subprocess_dispatch.Path") as MockPath:
                # Make Path(__file__).resolve().parents[2] return tmp_path
                mock_resolved = MockPath.return_value.resolve.return_value
                mock_resolved.parents.__getitem__ = lambda self, idx: tmp_path if idx == 2 else None
                # But we also need Path / ".claude" / "terminals" / ... to work
                # Simpler: just patch at function level
                pass

        # Direct approach: call function with patched __file__ location
        result = _call_with_fake_root(tmp_path, terminal_id, instruction)

        assert result.startswith("# Agent Context")
        assert "You are a backend developer." in result
        assert "---\n\nDISPATCH INSTRUCTION:\n\n" in result
        assert result.endswith(instruction)

    def test_returns_unchanged_when_no_claude_md(self, tmp_path):
        """When no CLAUDE.md exists, instruction is returned unchanged."""
        instruction = "Implement feature Y"
        result = _call_with_fake_root(tmp_path, "T99", instruction)
        assert result == instruction

    def test_returns_unchanged_for_empty_terminal_dir(self, tmp_path):
        """When terminal directory exists but CLAUDE.md does not, instruction unchanged."""
        terminal_dir = tmp_path / ".claude" / "terminals" / "T1"
        terminal_dir.mkdir(parents=True)
        # No CLAUDE.md file created
        instruction = "Do something"
        result = _call_with_fake_root(tmp_path, "T1", instruction)
        assert result == instruction

    def test_context_separator_format(self, tmp_path):
        """Verify the separator between context and instruction."""
        terminal_id = "T1"
        claude_md_dir = tmp_path / ".claude" / "terminals" / terminal_id
        claude_md_dir.mkdir(parents=True)
        (claude_md_dir / "CLAUDE.md").write_text("Context here")

        result = _call_with_fake_root(tmp_path, terminal_id, "Task")
        parts = result.split("\n\n---\n\nDISPATCH INSTRUCTION:\n\n")
        assert len(parts) == 2
        assert parts[0] == "Context here"
        assert parts[1] == "Task"


def _call_with_fake_root(fake_root: Path, terminal_id: str, instruction: str) -> str:
    """Call _inject_skill_context with a patched repo root path."""
    import subprocess_dispatch

    original_file = subprocess_dispatch.__file__
    fake_file = fake_root / "scripts" / "lib" / "subprocess_dispatch.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.touch()

    with patch.object(subprocess_dispatch, "__file__", str(fake_file)):
        return _inject_skill_context(terminal_id, instruction)
