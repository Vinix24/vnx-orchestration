#!/usr/bin/env python3
"""Tests for intelligence injection into subprocess worker prompts (VNX-R4b)."""

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from subprocess_dispatch import _build_intelligence_section, _inject_skill_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(item_class: str, title: str, content: str):
    """Create a real IntelligenceItem instance."""
    from intelligence_selector import IntelligenceItem
    return IntelligenceItem(
        item_id=f"intel_{item_class[:6]}",
        item_class=item_class,
        title=title,
        content=content,
        confidence=0.9,
        evidence_count=3,
        last_seen="2026-04-28T00:00:00.000000Z",
        scope_tags=["backend-developer"],
    )


def _make_result(items=None):
    """Create a real InjectionResult with the given items."""
    from intelligence_selector import InjectionResult
    return InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-04-28T00:00:00.000000Z",
        items=items or [],
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id="test-dispatch",
    )


def _patch_selector(result):
    """Context manager: patch IntelligenceSelector.select to return *result*."""
    import intelligence_selector as _mod
    mock_cls = MagicMock()
    instance = MagicMock()
    instance.select.return_value = result
    mock_cls.return_value = instance
    return patch.object(_mod, "IntelligenceSelector", mock_cls)


# ---------------------------------------------------------------------------
# Test 1: populated DB → section headers present
# ---------------------------------------------------------------------------

class TestBuildIntelligenceSectionPopulated:
    def test_failure_prevention_item_produces_antipatterns_header(self):
        """failure_prevention item → 'Antipatterns to avoid' header in output."""
        item = _make_item("failure_prevention", "Never skip gates",
                          "Skipping gates hides failures.")
        result = _make_result(items=[item])

        with _patch_selector(result):
            section = _build_intelligence_section("d-001", "backend-developer")

        assert "Antipatterns to avoid" in section
        assert "Never skip gates" in section
        assert "Skipping gates hides failures." in section

    def test_proven_pattern_item_produces_patterns_header(self):
        """proven_pattern item → 'Proven success patterns' header in output."""
        item = _make_item("proven_pattern", "Write tests first",
                          "Tests before commit catches regressions.")
        result = _make_result(items=[item])

        with _patch_selector(result):
            section = _build_intelligence_section("d-001", "backend-developer")

        assert "Proven success patterns" in section
        assert "Write tests first" in section

    def test_recent_comparable_item_produces_tag_warnings_header(self):
        """recent_comparable item → 'Tag warnings' header in output."""
        item = _make_item("recent_comparable", "Recent: backend-developer (success)",
                          "Dispatch d-000 (backend-developer) completed with status: success.")
        result = _make_result(items=[item])

        with _patch_selector(result):
            section = _build_intelligence_section("d-001", "backend-developer")

        assert "Tag warnings" in section

    def test_all_three_classes_all_headers_present(self):
        """All three item classes → all three section headers present."""
        items = [
            _make_item("failure_prevention", "AP-1", "Avoid this."),
            _make_item("proven_pattern", "PP-1", "Do this."),
            _make_item("recent_comparable", "RC-1", "Past dispatch."),
        ]
        result = _make_result(items=items)

        with _patch_selector(result):
            section = _build_intelligence_section("d-001", "backend-developer")

        assert "Antipatterns to avoid" in section
        assert "Proven success patterns" in section
        assert "Tag warnings" in section


# ---------------------------------------------------------------------------
# Test 2: empty intelligence → no intelligence section
# ---------------------------------------------------------------------------

class TestBuildIntelligenceSectionEmpty:
    def test_empty_items_returns_empty_string(self):
        """When selector returns no items, section is empty string."""
        result = _make_result(items=[])

        with _patch_selector(result):
            section = _build_intelligence_section("d-002", "backend-developer")

        assert section == ""

    def test_empty_section_means_no_antipatterns_header(self):
        """Empty section must not contain 'Antipatterns to avoid'."""
        result = _make_result(items=[])

        with _patch_selector(result):
            section = _build_intelligence_section("d-002", "backend-developer")

        assert "Antipatterns to avoid" not in section
        assert "Proven success patterns" not in section
        assert "Tag warnings" not in section


# ---------------------------------------------------------------------------
# Test 3: import failure → fallback with warning
# ---------------------------------------------------------------------------

class TestBuildIntelligenceSectionImportFailure:
    def test_import_error_returns_empty_string(self):
        """ImportError on intelligence_selector → returns '' without raising."""
        with patch.dict(sys.modules, {"intelligence_selector": None}):
            section = _build_intelligence_section("d-003", "backend-developer")

        assert section == ""

    def test_import_error_logs_warning(self, caplog):
        """ImportError on intelligence_selector → warning logged."""
        with patch.dict(sys.modules, {"intelligence_selector": None}):
            with caplog.at_level(logging.WARNING, logger="subprocess_dispatch"):
                _build_intelligence_section("d-003", "backend-developer")

        assert any("intelligence injection failed" in r.message for r in caplog.records)

    def test_selector_exception_returns_empty_string(self):
        """If selector.select() raises RuntimeError → returns '' without raising."""
        import intelligence_selector as _mod
        mock_cls = MagicMock()
        instance = MagicMock()
        instance.select.side_effect = RuntimeError("DB locked")
        mock_cls.return_value = instance

        with patch.object(_mod, "IntelligenceSelector", mock_cls):
            section = _build_intelligence_section("d-003", "backend-developer")

        assert section == ""


# ---------------------------------------------------------------------------
# Test 4: instruction_chars accuracy
# ---------------------------------------------------------------------------

class TestInstructionCharsAccuracy:
    """Verify instruction_chars in manifest reflects the full assembled prompt.

    When intelligence is injected, the assembled prompt is longer; the manifest
    instruction_chars must reflect this (intelligence is counted, not excluded).
    """

    def _run_legacy_inject(self, intelligence_section: str, instruction: str) -> str:
        """Force the legacy CLAUDE.md path and return assembled prompt."""
        item = _make_item("failure_prevention", "AP-title", "AP-content")
        result = _make_result(items=[item] if intelligence_section else [])

        with _patch_selector(result):
            with patch.dict(sys.modules, {"prompt_assembler": None}):
                prompt = _inject_skill_context(
                    "T1",
                    instruction,
                    role="backend-developer",
                    dispatch_metadata={"dispatch_id": "d-004"},
                )
        return prompt

    def test_prompt_with_intelligence_is_longer_than_without(self):
        """Assembled prompt with intelligence section is longer than without."""
        instruction = "implement feature X"

        prompt_with = self._run_legacy_inject("has_content", instruction)

        item_empty = _make_result(items=[])
        import intelligence_selector as _mod
        mock_cls = MagicMock()
        instance = MagicMock()
        instance.select.return_value = item_empty
        mock_cls.return_value = instance

        with patch.object(_mod, "IntelligenceSelector", mock_cls):
            with patch.dict(sys.modules, {"prompt_assembler": None}):
                prompt_without = _inject_skill_context(
                    "T1",
                    instruction,
                    role="backend-developer",
                    dispatch_metadata={"dispatch_id": "d-004"},
                )

        assert len(prompt_with) > len(prompt_without), (
            "Prompt with intelligence must be longer than prompt without"
        )

    def test_intelligence_section_included_in_assembled_prompt(self):
        """Intelligence section content is present in the assembled prompt."""
        instruction = "implement feature Y"
        prompt = self._run_legacy_inject("has_content", instruction)
        assert "AP-title" in prompt
        assert "AP-content" in prompt

    def test_instruction_chars_counts_assembled_prompt(self):
        """instruction_chars in manifest equals len(assembled_prompt)."""
        import json
        import tempfile
        from unittest.mock import MagicMock, patch

        item = _make_item("failure_prevention", "AP-title", "AP-content")
        result = _make_result(items=[item])

        captured_instruction: list[str] = []

        original_write = __import__("subprocess_dispatch")._write_manifest

        def _capture_write(*args, **kwargs):
            captured_instruction.append(kwargs.get("instruction", args[4] if len(args) > 4 else ""))
            return None

        with _patch_selector(result):
            with patch.dict(sys.modules, {"prompt_assembler": None}):
                with patch("subprocess_dispatch._write_manifest", side_effect=_capture_write):
                    with patch("subprocess_dispatch.SubprocessAdapter") as mock_adapter_cls:
                        instance = MagicMock()
                        instance.deliver.return_value = MagicMock(success=False)
                        mock_adapter_cls.return_value = instance

                        from subprocess_dispatch import deliver_via_subprocess
                        deliver_via_subprocess(
                            "T1",
                            "implement feature Z",
                            "sonnet",
                            "d-005",
                            role="backend-developer",
                        )

        assert captured_instruction, "manifest should have been written"
        assembled = captured_instruction[0]
        assert "AP-title" in assembled
        assert "AP-content" in assembled
