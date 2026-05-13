#!/usr/bin/env python3
"""Tests for per-provider intelligence injection (Wave 4.5 PR-3).

Coverage:
- IntelligenceContext.serialize_for per provider
- Claude path byte-identical regression against _format_intelligence_items
- Codex CONTEXT: header
- Gemini markdown format
- Unknown provider fallback
- Codex/Gemini adapter _build_prompt includes prior_round_findings
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(item_class: str, title: str, content: str):
    from intelligence_selector import IntelligenceItem
    return IntelligenceItem(
        item_id=f"intel_{item_class[:8]}_{title[:6]}",
        item_class=item_class,
        title=title,
        content=content,
        confidence=0.9,
        evidence_count=3,
        last_seen="2026-05-13T00:00:00.000000Z",
        scope_tags=["backend-developer"],
    )


def _make_injection_result(items):
    from intelligence_selector import InjectionResult
    return InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-05-13T00:00:00.000000Z",
        items=items,
        suppressed=[],
        task_class="coding_interactive",
        dispatch_id="test-dispatch-per-provider",
    )


# ---------------------------------------------------------------------------
# IntelligenceContext serialization tests
# ---------------------------------------------------------------------------

class TestIntelligenceContextSerializesForClaude:
    """Regression: serialize_for('claude') must match _format_intelligence_items."""

    def _format_intelligence_items(self, items: list) -> str:
        """Replicate the original _format_intelligence_items logic verbatim."""
        by_class: dict[str, list] = {}
        for item in items:
            by_class.setdefault(item.item_class, []).append(item)
        parts: list[str] = []
        if "failure_prevention" in by_class:
            parts.append("### Antipatterns to avoid")
            for item in by_class["failure_prevention"]:
                parts.append(f"- **{item.title}**: {item.content}")
            parts.append("")
        if "proven_pattern" in by_class:
            parts.append("### Proven success patterns")
            for item in by_class["proven_pattern"]:
                parts.append(f"- **{item.title}**: {item.content}")
            parts.append("")
        if "recent_comparable" in by_class:
            parts.append("### Tag warnings")
            for item in by_class["recent_comparable"]:
                parts.append(f"- **{item.title}**: {item.content}")
            parts.append("")
        return "\n".join(parts)

    def test_failure_prevention_only(self):
        from intelligence_selector import IntelligenceContext
        items = [_make_item("failure_prevention", "Avoid mocks", "Don't mock the DB")]
        result = _make_injection_result(items)
        ctx = IntelligenceContext.from_injection_result(result)
        expected = self._format_intelligence_items(items)
        assert ctx.serialize_for("claude") == expected

    def test_proven_pattern_only(self):
        from intelligence_selector import IntelligenceContext
        items = [_make_item("proven_pattern", "Atomic writes", "Use os.replace()")]
        result = _make_injection_result(items)
        ctx = IntelligenceContext.from_injection_result(result)
        expected = self._format_intelligence_items(items)
        assert ctx.serialize_for("claude") == expected

    def test_all_standard_classes(self):
        from intelligence_selector import IntelligenceContext
        items = [
            _make_item("failure_prevention", "No mocks", "Real DB only"),
            _make_item("proven_pattern", "Atomic writes", "Use os.replace()"),
            _make_item("recent_comparable", "Similar dispatch", "Succeeded on test-001"),
        ]
        result = _make_injection_result(items)
        ctx = IntelligenceContext.from_injection_result(result)
        expected = self._format_intelligence_items(items)
        assert ctx.serialize_for("claude") == expected

    def test_empty_items_returns_empty_string(self):
        from intelligence_selector import IntelligenceContext
        result = _make_injection_result([])
        ctx = IntelligenceContext.from_injection_result(result)
        assert ctx.serialize_for("claude") == ""


class TestIntelligenceContextSerializesForCodex:
    def test_has_context_header(self):
        from intelligence_selector import IntelligenceContext
        items = [_make_item("failure_prevention", "No mocks", "Use real fixtures")]
        ctx = IntelligenceContext.from_injection_result(_make_injection_result(items))
        out = ctx.serialize_for("codex")
        assert out.startswith("CONTEXT:\n\n")

    def test_content_follows_header(self):
        from intelligence_selector import IntelligenceContext
        items = [_make_item("proven_pattern", "Atomic writes", "Use os.replace()")]
        ctx = IntelligenceContext.from_injection_result(_make_injection_result(items))
        out = ctx.serialize_for("codex")
        assert "### Proven success patterns" in out
        assert "Atomic writes" in out

    def test_empty_items_returns_empty_string(self):
        from intelligence_selector import IntelligenceContext
        ctx = IntelligenceContext.from_injection_result(_make_injection_result([]))
        assert ctx.serialize_for("codex") == ""

    def test_prior_round_finding_appears_under_context_header(self):
        from intelligence_selector import IntelligenceContext
        items = [_make_item("prior_round_finding", "Stale lease", "Release lease after receipt")]
        ctx = IntelligenceContext.from_injection_result(_make_injection_result(items))
        out = ctx.serialize_for("codex")
        assert out.startswith("CONTEXT:\n\n")
        assert "Stale lease" in out
        assert "Prior round findings" in out


class TestIntelligenceContextSerializesForGemini:
    def test_no_context_header(self):
        from intelligence_selector import IntelligenceContext
        items = [_make_item("failure_prevention", "No raw tmux", "Use dispatch files")]
        ctx = IntelligenceContext.from_injection_result(_make_injection_result(items))
        out = ctx.serialize_for("gemini")
        assert not out.startswith("CONTEXT:")

    def test_markdown_headers_present(self):
        from intelligence_selector import IntelligenceContext
        items = [
            _make_item("failure_prevention", "No raw tmux", "Use dispatch files"),
            _make_item("proven_pattern", "Atomic writes", "Use os.replace()"),
        ]
        ctx = IntelligenceContext.from_injection_result(_make_injection_result(items))
        out = ctx.serialize_for("gemini")
        assert "### Antipatterns to avoid" in out
        assert "### Proven success patterns" in out

    def test_wave5_adr_matches_rendered(self):
        from intelligence_selector import IntelligenceContext
        items = [_make_item("adr_relevant", "ADR-010", "Subprocess adapter is canonical")]
        ctx = IntelligenceContext.from_injection_result(_make_injection_result(items))
        out = ctx.serialize_for("gemini")
        assert "ADR-010" in out
        assert "### ADR matches" in out


class TestIntelligenceContextUnknownProviderFallback:
    def test_falls_back_to_markdown(self):
        from intelligence_selector import IntelligenceContext
        items = [_make_item("failure_prevention", "No mocks", "Real DB only")]
        ctx = IntelligenceContext.from_injection_result(_make_injection_result(items))
        out_unknown = ctx.serialize_for("unknown_provider")
        out_claude = ctx.serialize_for("claude")
        assert out_unknown == out_claude

    def test_litellm_prefix_falls_back_to_markdown(self):
        from intelligence_selector import IntelligenceContext
        items = [_make_item("proven_pattern", "Atomic writes", "Use os.replace()")]
        ctx = IntelligenceContext.from_injection_result(_make_injection_result(items))
        out_litellm = ctx.serialize_for("litellm:openai")
        out_claude = ctx.serialize_for("claude")
        assert out_litellm == out_claude


# ---------------------------------------------------------------------------
# Adapter integration tests
# ---------------------------------------------------------------------------

def _patch_build_intelligence_context(return_value):
    """Context manager patching build_intelligence_context in adapters."""
    import intelligence_selector as _mod
    return patch.object(_mod, "build_intelligence_context", return_value=return_value)


def _make_ctx_with_prior_finding(content: str):
    """Return IntelligenceContext with one prior_round_finding item."""
    from intelligence_selector import IntelligenceContext
    item = _make_item("prior_round_finding", "Gate skip detected", content)
    result = _make_injection_result([item])
    return IntelligenceContext.from_injection_result(result)


class TestCodexAdapterIncludesPriorRoundFindings:
    def test_prior_round_finding_in_prompt(self, tmp_path):
        from adapters.codex_adapter import CodexAdapter

        ctx = _make_ctx_with_prior_finding("T0 skipped all gates before merge")

        with _patch_build_intelligence_context(ctx):
            adapter = CodexAdapter("T3")
            prompt = adapter._build_prompt(
                instruction="Review this PR",
                changed_files=[],
                role="reviewer",
                dispatch_metadata={"dispatch_id": "test-codex-001"},
            )

        assert "T0 skipped all gates before merge" in prompt
        assert "Gate skip detected" in prompt

    def test_codex_context_header_present(self, tmp_path):
        from adapters.codex_adapter import CodexAdapter

        ctx = _make_ctx_with_prior_finding("Never skip codex gate")

        with _patch_build_intelligence_context(ctx):
            adapter = CodexAdapter("T3")
            prompt = adapter._build_prompt(
                instruction="Do review",
                changed_files=[],
                role="reviewer",
                dispatch_metadata={"dispatch_id": "test-codex-002"},
            )

        assert "CONTEXT:" in prompt

    def test_no_role_skips_intelligence(self):
        from adapters.codex_adapter import CodexAdapter

        with patch("intelligence_selector.build_intelligence_context") as mock_bic, \
             patch("adapters.codex_adapter.collect_file_contents", return_value=""):
            adapter = CodexAdapter("T3")
            prompt = adapter._build_prompt(
                instruction="bare instruction",
                changed_files=[],
                role=None,
                dispatch_metadata=None,
            )

        mock_bic.assert_not_called()
        assert prompt == "bare instruction"


class TestGeminiAdapterIncludesPriorRoundFindings:
    def test_prior_round_finding_in_prompt(self):
        from adapters.gemini_adapter import GeminiAdapter

        ctx = _make_ctx_with_prior_finding("Release lease after every receipt")

        with _patch_build_intelligence_context(ctx):
            adapter = GeminiAdapter("T2")
            prompt = adapter._build_prompt(
                instruction="Gemini review",
                changed_files=[],
                role="reviewer",
                dispatch_metadata={"dispatch_id": "test-gemini-001"},
            )

        assert "Release lease after every receipt" in prompt

    def test_gemini_no_context_header(self):
        from adapters.gemini_adapter import GeminiAdapter

        ctx = _make_ctx_with_prior_finding("Use dispatch files not tmux")

        with _patch_build_intelligence_context(ctx):
            adapter = GeminiAdapter("T2")
            prompt = adapter._build_prompt(
                instruction="Gemini review",
                changed_files=[],
                role="reviewer",
                dispatch_metadata={"dispatch_id": "test-gemini-002"},
            )

        # Gemini uses markdown, not CONTEXT: prefix
        assert not prompt.startswith("CONTEXT:")
        assert "Use dispatch files not tmux" in prompt

    def test_no_role_skips_intelligence(self):
        from adapters.gemini_adapter import GeminiAdapter

        with patch("intelligence_selector.build_intelligence_context") as mock_bic, \
             patch("adapters.gemini_adapter.collect_file_contents", return_value=""):
            adapter = GeminiAdapter("T2")
            prompt = adapter._build_prompt(
                instruction="bare instruction",
                changed_files=[],
                role=None,
                dispatch_metadata=None,
            )

        mock_bic.assert_not_called()
        assert prompt == "bare instruction"


# ---------------------------------------------------------------------------
# Top-level kwarg sourcing tests (codex round-1 fix)
# ---------------------------------------------------------------------------

class TestCodexAdapterTopLevelDispatchId:
    """dispatch_id/pr_id passed at top level (not in dispatch_metadata) must work."""

    def test_intelligence_with_top_level_dispatch_id(self):
        from adapters.codex_adapter import CodexAdapter

        ctx = _make_ctx_with_prior_finding("Gate skip is unacceptable")

        with _patch_build_intelligence_context(ctx):
            adapter = CodexAdapter("T3")
            prompt = adapter._build_prompt(
                instruction="Review PR",
                changed_files=[],
                role="reviewer",
                dispatch_id="top-level-dispatch-001",
                # intentionally no dispatch_metadata
            )

        assert "Gate skip is unacceptable" in prompt

    def test_top_level_dispatch_id_forwarded_to_selector(self):
        """build_intelligence_context must receive the top-level dispatch_id, not ''."""
        import intelligence_selector as _mod
        from adapters.codex_adapter import CodexAdapter

        with patch.object(_mod, "build_intelligence_context", return_value=None) as mock_bic:
            adapter = CodexAdapter("T3")
            adapter._build_prompt(
                instruction="Review PR",
                changed_files=[],
                role="reviewer",
                dispatch_id="explicit-id-codex",
                pr_id="474",
            )

        mock_bic.assert_called_once()
        call_kwargs = mock_bic.call_args[1] if mock_bic.call_args[1] else mock_bic.call_args[0]
        assert call_kwargs.get("dispatch_id") == "explicit-id-codex"
        assert call_kwargs.get("pr_id") == "474"

    def test_intelligence_fallback_to_metadata(self):
        """Backward compat: dispatch_id in dispatch_metadata still works."""
        from adapters.codex_adapter import CodexAdapter

        ctx = _make_ctx_with_prior_finding("Always release lease after receipt")

        with _patch_build_intelligence_context(ctx):
            adapter = CodexAdapter("T3")
            prompt = adapter._build_prompt(
                instruction="Review PR",
                changed_files=[],
                role="reviewer",
                dispatch_metadata={"dispatch_id": "meta-dispatch-001", "pr_id": "474"},
            )

        assert "Always release lease after receipt" in prompt


class TestGeminiAdapterTopLevelDispatchId:
    """Same top-level kwarg sourcing for GeminiAdapter."""

    def test_intelligence_with_top_level_dispatch_id(self):
        from adapters.gemini_adapter import GeminiAdapter

        ctx = _make_ctx_with_prior_finding("Never skip gates before merge")

        with _patch_build_intelligence_context(ctx):
            adapter = GeminiAdapter("T2")
            prompt = adapter._build_prompt(
                instruction="Gemini review",
                changed_files=[],
                role="reviewer",
                dispatch_id="top-level-gemini-001",
            )

        assert "Never skip gates before merge" in prompt

    def test_top_level_dispatch_id_forwarded_to_selector(self):
        import intelligence_selector as _mod
        from adapters.gemini_adapter import GeminiAdapter

        with patch.object(_mod, "build_intelligence_context", return_value=None) as mock_bic:
            adapter = GeminiAdapter("T2")
            adapter._build_prompt(
                instruction="Gemini review",
                changed_files=[],
                role="reviewer",
                dispatch_id="explicit-id-gemini",
                pr_id="474",
            )

        mock_bic.assert_called_once()
        call_kwargs = mock_bic.call_args[1] if mock_bic.call_args[1] else mock_bic.call_args[0]
        assert call_kwargs.get("dispatch_id") == "explicit-id-gemini"
        assert call_kwargs.get("pr_id") == "474"
