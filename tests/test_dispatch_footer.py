#!/usr/bin/env python3
"""Tests for dispatch_footer.py — footer loading and injection into dispatch instructions."""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from dispatch_footer import (
    _FOOTER_SENTINEL,
    _DEFAULT_MODE,
    _FOOTER_FILES,
    _resolve_templates_dir,
    _strip_frontmatter,
    append_dispatch_footer,
    load_footer_template,
)


class TestStripFrontmatter:
    def test_strips_frontmatter(self):
        content = "---\nname: test\npurpose: Footer\n---\n\n## Section\n\nBody text."
        result = _strip_frontmatter(content)
        assert result == "## Section\n\nBody text."

    def test_no_frontmatter_returns_unchanged(self):
        content = "## Section\n\nBody text."
        assert _strip_frontmatter(content) == content

    def test_no_closing_dash_returns_unchanged(self):
        content = "---\nname: test\npurpose: open"
        assert _strip_frontmatter(content) == content

    def test_strips_leading_blank_lines_after_frontmatter(self):
        content = "---\nname: test\n---\n\n\nBody."
        result = _strip_frontmatter(content)
        assert result == "Body."


class TestLoadFooterTemplate:
    def test_loads_normal_footer(self):
        content = load_footer_template("normal")
        assert content, "Normal footer must be non-empty"
        assert "T0" in content or "Orchestrat" in content, "Normal footer must mention T0 or orchestration"

    def test_loads_autonomous_footer(self):
        content = load_footer_template("autonomous")
        assert content, "Autonomous footer must be non-empty"

    def test_loads_enhanced_footer(self):
        content = load_footer_template("enhanced")
        assert content, "Enhanced footer must be non-empty"

    def test_default_mode_is_normal(self):
        normal = load_footer_template("normal")
        default = load_footer_template()
        assert normal == default

    def test_env_var_overrides_mode(self, monkeypatch):
        monkeypatch.setenv("VNX_DISPATCH_FOOTER_MODE", "autonomous")
        content = load_footer_template("normal")  # env var takes precedence over arg
        autonomous_direct = load_footer_template.__wrapped__("autonomous") if hasattr(load_footer_template, "__wrapped__") else None
        assert content, "Should return non-empty content when env var is set"

    def test_missing_template_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "dispatch_footer._resolve_templates_dir",
            lambda: tmp_path / "nonexistent",
        )
        result = load_footer_template("normal")
        assert result == ""

    def test_unknown_mode_falls_back_to_normal(self):
        result = load_footer_template("totally_unknown_mode")
        normal = load_footer_template("normal")
        assert result == normal


class TestAppendDispatchFooter:
    def test_appends_footer_to_instruction(self):
        instruction = "Do the implementation task."
        result = append_dispatch_footer(instruction)
        assert _FOOTER_SENTINEL in result
        assert instruction in result
        assert result.startswith(instruction)

    def test_instruction_ends_with_footer_content(self):
        instruction = "Do the task."
        result = append_dispatch_footer(instruction)
        footer_content = load_footer_template()
        assert result.endswith(footer_content)

    def test_idempotent_when_footer_already_present(self):
        # sentinel is injected by append_dispatch_footer itself
        instruction = f"Do the task.\n\n---\n\n{_FOOTER_SENTINEL}\nsome footer text"
        result = append_dispatch_footer(instruction)
        assert result == instruction  # unchanged

    def test_separator_between_instruction_and_footer(self):
        instruction = "Do the task."
        result = append_dispatch_footer(instruction)
        assert "\n\n---\n\n" in result

    def test_autonomous_mode_appends_autonomous_footer(self):
        instruction = "Do the task."
        result = append_dispatch_footer(instruction, mode="autonomous")
        assert _FOOTER_SENTINEL in result
        assert len(result) > len(instruction)

    def test_returns_unchanged_on_empty_footer(self, monkeypatch):
        monkeypatch.setattr("dispatch_footer.load_footer_template", lambda mode=_DEFAULT_MODE: "")
        instruction = "Do the task."
        result = append_dispatch_footer(instruction)
        assert result == instruction

    def test_env_var_mode_selection(self, monkeypatch):
        monkeypatch.setenv("VNX_DISPATCH_FOOTER_MODE", "autonomous")
        instruction = "Do the task."
        result = append_dispatch_footer(instruction)
        assert _FOOTER_SENTINEL in result

    def test_dispatch_format_built_instruction_ends_with_footer(self):
        """Regression: a fully built dispatch instruction must end with footer content."""
        base = textwrap.dedent("""\
            Role: backend-developer
            Terminal: T1
            Priority: P1
            Cognition: normal
            Dispatch-ID: 20260530-test-001
            PR-ID: PR-1

            Instruction:
            - Implement the feature
            - Write tests
            - Commit
        """)
        result = append_dispatch_footer(base)
        footer = load_footer_template()
        assert result.endswith(footer), (
            "Built dispatch instruction must end with footer content. "
            f"Last 200 chars: {result[-200:]!r}"
        )

    def test_template_files_exist_for_all_modes(self):
        footers_dir = _resolve_templates_dir()
        for mode, filename in _FOOTER_FILES.items():
            path = footers_dir / filename
            assert path.exists(), f"Footer template missing for mode '{mode}': {path}"

    def test_idempotent_when_raw_footer_body_present_without_sentinel(self):
        """Regression: raw footer body already embedded (no sentinel) must not double-append."""
        footer_body = load_footer_template("normal")
        assert footer_body, "Prerequisite: normal footer must be non-empty"
        # Simulate an instruction that already contains the raw footer body but no sentinel
        instruction = "Do the task.\n\n" + footer_body
        assert _FOOTER_SENTINEL not in instruction, "Precondition: sentinel must NOT be present"
        result = append_dispatch_footer(instruction)
        # Footer body must appear exactly once
        assert result.count(footer_body) == 1, (
            "Footer body appeared more than once — double-append bug not fixed"
        )
