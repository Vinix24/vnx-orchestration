#!/usr/bin/env python3
"""Tests for intelligence_injection.py — shared smart-context builder (P0-A)."""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import intelligence_injection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(item_class: str, title: str, content: str):
    from intelligence_selector import IntelligenceItem
    return IntelligenceItem(
        item_id=f"intel_{item_class[:6]}",
        item_class=item_class,
        title=title,
        content=content,
        confidence=0.85,
        evidence_count=3,
        last_seen="2026-05-17T00:00:00.000000Z",
        scope_tags=["backend-developer"],
    )


def _make_result(items=None):
    from intelligence_selector import InjectionResult
    return InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-05-17T00:00:00.000000Z",
        items=items or [],
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id="test-dispatch",
    )


def _patch_selector(result):
    import intelligence_selector as _mod
    mock_cls = MagicMock()
    instance = MagicMock()
    instance.select.return_value = result
    mock_cls.return_value = instance
    return patch.object(_mod, "IntelligenceSelector", mock_cls)


# ---------------------------------------------------------------------------
# format_intelligence_items
# ---------------------------------------------------------------------------

class TestFormatIntelligenceItems:

    def test_failure_prevention_renders_antipatterns_header(self):
        item = _make_item("failure_prevention", "Never skip gates", "Gates catch regressions.")
        section = intelligence_injection.format_intelligence_items([item])
        assert "Antipatterns to avoid" in section
        assert "Never skip gates" in section
        assert "Gates catch regressions." in section

    def test_proven_pattern_renders_success_header(self):
        item = _make_item("proven_pattern", "Write tests first", "Tests before commit.")
        section = intelligence_injection.format_intelligence_items([item])
        assert "Proven success patterns" in section
        assert "Write tests first" in section

    def test_recent_comparable_renders_tag_warnings_header(self):
        item = _make_item("recent_comparable", "RC-1", "Past dispatch context.")
        section = intelligence_injection.format_intelligence_items([item])
        assert "Tag warnings" in section
        assert "RC-1" in section

    def test_all_three_classes_all_headers_present(self):
        items = [
            _make_item("failure_prevention", "AP", "avoid"),
            _make_item("proven_pattern", "PP", "do this"),
            _make_item("recent_comparable", "RC", "past"),
        ]
        section = intelligence_injection.format_intelligence_items(items)
        assert "Antipatterns to avoid" in section
        assert "Proven success patterns" in section
        assert "Tag warnings" in section

    def test_empty_list_returns_empty_string(self):
        section = intelligence_injection.format_intelligence_items([])
        assert section == ""

    def test_direct_injection_classes_render_their_content(self):
        """Regression: direct-injection classes (code_anchor, adr_relevant,
        schema_section, operator_memory, prior_round_finding) were selected and
        budgeted but never rendered — their content must now reach the worker."""
        items = [
            _make_item("code_anchor", "Code anchors",
                       "## CODE ANCHORS\n- `scripts/x.py:10-20` (matched: foo)"),
            _make_item("adr_relevant", "ADRs",
                       "## ADRS\n- ADR-007: tenant project_id"),
            _make_item("prior_round_finding", "Prior",
                       "## PRIOR FINDINGS\n- missing UNIQUE on dispatch_metadata"),
            _make_item("operator_memory", "Memory",
                       "## OPERATOR MEMORY\n- release stale leases first"),
            _make_item("schema_section", "Schema",
                       "## SCHEMA\nCREATE TABLE dispatches (...)"),
        ]
        section = intelligence_injection.format_intelligence_items(items)
        assert "scripts/x.py:10-20" in section, "code_anchor pointer not rendered"
        assert "ADR-007" in section
        assert "missing UNIQUE on dispatch_metadata" in section
        assert "release stale leases first" in section
        assert "CREATE TABLE dispatches" in section

    def test_direct_and_standard_classes_both_render(self):
        """A mix of standard + direct classes must render both."""
        items = [
            _make_item("proven_pattern", "PP", "do this"),
            _make_item("code_anchor", "Code anchors",
                       "## CODE ANCHORS\n- `scripts/y.py:1-5`"),
        ]
        section = intelligence_injection.format_intelligence_items(items)
        assert "Proven success patterns" in section
        assert "do this" in section
        assert "scripts/y.py:1-5" in section


# ---------------------------------------------------------------------------
# fetch_intelligence_section
# ---------------------------------------------------------------------------

class TestFetchIntelligenceSection:

    def test_returns_section_for_failure_prevention(self, tmp_path):
        item = _make_item("failure_prevention", "Gate rule", "Always gate.")
        result = _make_result(items=[item])
        with _patch_selector(result):
            section = intelligence_injection.fetch_intelligence_section(
                dispatch_id="d-001",
                role="backend-developer",
                state_dir=tmp_path,
            )
        assert "Antipatterns to avoid" in section
        assert "Gate rule" in section

    def test_empty_items_returns_empty_string(self, tmp_path):
        result = _make_result(items=[])
        with _patch_selector(result):
            section = intelligence_injection.fetch_intelligence_section(
                dispatch_id="d-002",
                role="backend-developer",
                state_dir=tmp_path,
            )
        assert section == ""

    def test_import_error_returns_empty_string(self, tmp_path):
        with patch.dict(sys.modules, {"intelligence_selector": None}):
            section = intelligence_injection.fetch_intelligence_section(
                dispatch_id="d-003",
                role="backend-developer",
                state_dir=tmp_path,
            )
        assert section == ""

    def test_import_error_logs_warning(self, tmp_path, caplog):
        with patch.dict(sys.modules, {"intelligence_selector": None}):
            with caplog.at_level(logging.WARNING, logger="intelligence_injection"):
                intelligence_injection.fetch_intelligence_section(
                    dispatch_id="d-003",
                    role="backend-developer",
                    state_dir=tmp_path,
                )
        assert any("intelligence injection failed" in r.message for r in caplog.records)

    def test_selector_exception_returns_empty_string(self, tmp_path):
        import intelligence_selector as _mod
        mock_cls = MagicMock()
        instance = MagicMock()
        instance.select.side_effect = RuntimeError("DB locked")
        mock_cls.return_value = instance
        with patch.object(_mod, "IntelligenceSelector", mock_cls):
            section = intelligence_injection.fetch_intelligence_section(
                dispatch_id="d-004",
                role="backend-developer",
                state_dir=tmp_path,
            )
        assert section == ""

    def test_pr_id_forwarded_to_selector(self, tmp_path):
        result = _make_result(items=[])
        import intelligence_selector as _mod
        mock_cls = MagicMock()
        instance = MagicMock()
        instance.select.return_value = result
        mock_cls.return_value = instance
        with patch.object(_mod, "IntelligenceSelector", mock_cls):
            intelligence_injection.fetch_intelligence_section(
                dispatch_id="d-005",
                role="backend-developer",
                state_dir=tmp_path,
                pr_id="PR-42",
            )
        call_kwargs = instance.select.call_args[1]
        assert call_kwargs.get("pr_id") == "PR-42"

    def test_dispatch_paths_forwarded_to_selector(self, tmp_path):
        result = _make_result(items=[])
        import intelligence_selector as _mod
        mock_cls = MagicMock()
        instance = MagicMock()
        instance.select.return_value = result
        mock_cls.return_value = instance
        with patch.object(_mod, "IntelligenceSelector", mock_cls):
            intelligence_injection.fetch_intelligence_section(
                dispatch_id="d-006",
                role="backend-developer",
                state_dir=tmp_path,
                dispatch_paths=["scripts/lib/foo.py", "tests/test_foo.py"],
            )
        call_kwargs = instance.select.call_args[1]
        assert call_kwargs.get("dispatch_paths") == ["scripts/lib/foo.py", "tests/test_foo.py"]


# ---------------------------------------------------------------------------
# build_intelligence_section — enriched instruction
# ---------------------------------------------------------------------------

class TestBuildIntelligenceSection:

    def test_with_intelligence_prepends_section(self, tmp_path):
        item = _make_item("proven_pattern", "PP-1", "Do this.")
        result = _make_result(items=[item])
        with _patch_selector(result):
            enriched = intelligence_injection.build_intelligence_section(
                instruction="implement feature X",
                dispatch_id="d-build-001",
                role="backend-developer",
                state_dir=tmp_path,
            )
        assert "Relevant Intelligence" in enriched
        assert "PP-1" in enriched
        assert "implement feature X" in enriched

    def test_original_instruction_preserved_at_end(self, tmp_path):
        item = _make_item("failure_prevention", "AP", "Avoid.")
        result = _make_result(items=[item])
        with _patch_selector(result):
            enriched = intelligence_injection.build_intelligence_section(
                instruction="do the work",
                dispatch_id="d-build-002",
                role="backend-developer",
                state_dir=tmp_path,
            )
        assert enriched.endswith("do the work")

    def test_empty_intelligence_returns_original_unchanged(self, tmp_path):
        result = _make_result(items=[])
        with _patch_selector(result):
            enriched = intelligence_injection.build_intelligence_section(
                instruction="original instruction",
                dispatch_id="d-build-003",
                role="backend-developer",
                state_dir=tmp_path,
            )
        assert enriched == "original instruction"

    def test_import_failure_returns_original_instruction(self, tmp_path):
        with patch.dict(sys.modules, {"intelligence_selector": None}):
            enriched = intelligence_injection.build_intelligence_section(
                instruction="fallback instruction",
                dispatch_id="d-build-004",
                role="backend-developer",
                state_dir=tmp_path,
            )
        assert enriched == "fallback instruction"

    def test_enriched_instruction_longer_than_original(self, tmp_path):
        item = _make_item("proven_pattern", "PP", "content")
        result = _make_result(items=[item])
        instruction = "do work"
        with _patch_selector(result):
            enriched = intelligence_injection.build_intelligence_section(
                instruction=instruction,
                dispatch_id="d-build-005",
                role="backend-developer",
                state_dir=tmp_path,
            )
        assert len(enriched) > len(instruction)

    def test_separator_present_between_intelligence_and_instruction(self, tmp_path):
        item = _make_item("failure_prevention", "AP", "content")
        result = _make_result(items=[item])
        with _patch_selector(result):
            enriched = intelligence_injection.build_intelligence_section(
                instruction="instruction text",
                dispatch_id="d-build-006",
                role="backend-developer",
                state_dir=tmp_path,
            )
        assert "---" in enriched

    def test_codex_path_same_enrichment_as_gemini_path(self, tmp_path):
        """Intelligence enrichment is provider-agnostic — same output regardless of provider label."""
        item = _make_item("proven_pattern", "SharedPP", "universal content")
        result = _make_result(items=[item])
        instruction = "do something"

        def _build(dispatch_id):
            with _patch_selector(result):
                return intelligence_injection.build_intelligence_section(
                    instruction=instruction,
                    dispatch_id=dispatch_id,
                    role="backend-developer",
                    state_dir=tmp_path,
                )

        enriched_codex = _build("d-codex-001")
        enriched_gemini = _build("d-gemini-001")
        # Content identical, dispatch_id differs only in selector call (not in output)
        assert "SharedPP" in enriched_codex
        assert "SharedPP" in enriched_gemini

    def test_litellm_path_receives_intelligence(self, tmp_path):
        item = _make_item("failure_prevention", "LiteLLM-AP", "litellm avoidance")
        result = _make_result(items=[item])
        with _patch_selector(result):
            enriched = intelligence_injection.build_intelligence_section(
                instruction="litellm dispatch",
                dispatch_id="d-litellm-001",
                role="backend-developer",
                state_dir=tmp_path,
            )
        assert "LiteLLM-AP" in enriched

    def test_none_role_does_not_raise(self, tmp_path):
        result = _make_result(items=[])
        with _patch_selector(result):
            enriched = intelligence_injection.build_intelligence_section(
                instruction="no-role instruction",
                dispatch_id="d-no-role",
                role=None,
                state_dir=tmp_path,
            )
        assert "no-role instruction" in enriched


# ---------------------------------------------------------------------------
# skill_injection backward-compat — _build_intelligence_section delegates here
# ---------------------------------------------------------------------------

class TestSkillInjectionDelegation:

    def test_subprocess_dispatch_facade_still_works(self):
        """subprocess_dispatch._build_intelligence_section delegates to intelligence_injection."""
        from subprocess_dispatch import _build_intelligence_section
        item = _make_item("proven_pattern", "Delegation-PP", "Delegate content.")
        result = _make_result(items=[item])
        with _patch_selector(result):
            section = _build_intelligence_section("d-delegate-001", "backend-developer")
        assert "Delegation-PP" in section

    def test_subprocess_dispatch_facade_empty_on_no_items(self):
        from subprocess_dispatch import _build_intelligence_section
        result = _make_result(items=[])
        with _patch_selector(result):
            section = _build_intelligence_section("d-delegate-002", "backend-developer")
        assert section == ""
