#!/usr/bin/env python3
"""Wave 5 production plumbing tests — dispatch_paths/instruction_text/pr_id threading.

Verifies that _build_intelligence_section forwards the W5 params to
IntelligenceSelector.select(), and that _inject_skill_context extracts them
from dispatch_metadata and threads them through the call chain.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from subprocess_dispatch_internals.skill_injection import (
    _build_intelligence_section,
    _inject_skill_context,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_state_dir(tmp_path):
    """Redirect _default_state_dir so tests never touch .vnx-data."""
    with patch("subprocess_dispatch._default_state_dir", return_value=tmp_path):
        yield tmp_path


@pytest.fixture
def mock_selector_class():
    """Patch IntelligenceSelector; return the mock class and its instance."""
    mock_result = MagicMock()
    mock_result.items = []
    instance = MagicMock()
    instance.select.return_value = mock_result
    with patch(
        "intelligence_selector.IntelligenceSelector",
        return_value=instance,
    ) as cls:
        yield cls, instance


# ---------------------------------------------------------------------------
# _build_intelligence_section tests
# ---------------------------------------------------------------------------

class TestBuildIntelligenceSectionForwardsW5Params:
    def test_build_intelligence_section_forwards_dispatch_paths(
        self, mock_selector_class
    ):
        """dispatch_paths list is forwarded to selector.select() as-is."""
        _, selector_instance = mock_selector_class
        paths = ["scripts/lib/code_anchor_finder.py", "schemas/quality_intelligence.sql"]

        _build_intelligence_section("d-w5-001", "backend-developer", dispatch_paths=paths)

        call_kwargs = selector_instance.select.call_args.kwargs
        assert call_kwargs["dispatch_paths"] == paths, (
            f"Expected dispatch_paths={paths!r}, got {call_kwargs['dispatch_paths']!r}"
        )

    def test_build_intelligence_section_forwards_instruction_text(
        self, mock_selector_class
    ):
        """instruction_text string is forwarded to selector.select()."""
        _, selector_instance = mock_selector_class
        text = "Implement schema introspection injection for database workers"

        _build_intelligence_section("d-w5-002", "backend-developer", instruction_text=text)

        call_kwargs = selector_instance.select.call_args.kwargs
        assert call_kwargs["instruction_text"] == text, (
            f"Expected instruction_text={text!r}, got {call_kwargs['instruction_text']!r}"
        )

    def test_build_intelligence_section_forwards_pr_id(self, mock_selector_class):
        """pr_id is forwarded to selector.select()."""
        _, selector_instance = mock_selector_class

        _build_intelligence_section("d-w5-003", "backend-developer", pr_id="460")

        call_kwargs = selector_instance.select.call_args.kwargs
        assert call_kwargs["pr_id"] == "460", (
            f"Expected pr_id='460', got {call_kwargs['pr_id']!r}"
        )

    def test_build_intelligence_section_none_defaults_pass_empty(
        self, mock_selector_class
    ):
        """When new params are omitted, selector gets empty list/string and None."""
        _, selector_instance = mock_selector_class

        _build_intelligence_section("d-w5-004", "backend-developer")

        call_kwargs = selector_instance.select.call_args.kwargs
        assert call_kwargs["dispatch_paths"] == []
        assert call_kwargs["instruction_text"] == ""
        assert call_kwargs["pr_id"] is None


# ---------------------------------------------------------------------------
# _inject_skill_context metadata extraction test
# ---------------------------------------------------------------------------

class TestInjectSkillContextExtractsMetadata:
    def test_inject_skill_context_extracts_metadata_from_dict(
        self, mock_selector_class, tmp_path
    ):
        """_inject_skill_context extracts dispatch_paths and pr_id from dispatch_metadata."""
        _, selector_instance = mock_selector_class

        metadata = {
            "dispatch_id": "d-w5-extract-001",
            "model": "sonnet",
            "dispatch_paths": ["scripts/lib/subprocess_dispatch.py"],
            "pr_id": "458",
        }
        instruction = "Implement the layered prompt assembler with ADR grounding"

        with patch("subprocess_dispatch_internals.skill_injection._try_prompt_assembler", return_value=None):
            with patch(
                "subprocess_dispatch_internals.skill_injection._legacy_claude_md_resolution",
                return_value=instruction,
            ):
                _inject_skill_context("T1", instruction, role="backend-developer", dispatch_metadata=metadata)

        call_kwargs = selector_instance.select.call_args.kwargs
        assert call_kwargs["dispatch_paths"] == ["scripts/lib/subprocess_dispatch.py"], (
            f"dispatch_paths not extracted from metadata: {call_kwargs}"
        )
        assert call_kwargs["pr_id"] == "458", (
            f"pr_id not extracted from metadata: {call_kwargs}"
        )
        assert call_kwargs["instruction_text"] == instruction, (
            f"instruction_text not the raw instruction: {call_kwargs}"
        )
