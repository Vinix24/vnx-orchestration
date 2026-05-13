#!/usr/bin/env python3
"""tests/test_codex_adapter_prompt_assembly.py — Unit tests for CodexAdapter._build_prompt.

Verifies that _build_prompt routes through PromptAssembler when role is provided,
and falls back to raw instruction+files when no role is given (backward compat).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib" / "adapters"))

from codex_adapter import CodexAdapter


@pytest.fixture()
def adapter() -> CodexAdapter:
    return CodexAdapter(terminal_id="T1")


def _mock_collect(payload: dict, subprocess_run=None) -> str:
    """Stub that returns empty file contents so tests stay filesystem-independent."""
    return ""


# ---------------------------------------------------------------------------
# test_codex_build_prompt_uses_assembler_when_role_given
# ---------------------------------------------------------------------------

def test_codex_build_prompt_uses_assembler_when_role_given(adapter: CodexAdapter) -> None:
    """When role= is given, _build_prompt must route through PromptAssembler."""
    with patch("vertex_ai_runner.collect_file_contents", side_effect=_mock_collect):
        result = adapter._build_prompt(
            instruction="Fix the parser",
            changed_files=[],
            role="backend-developer",
        )
    assert isinstance(result, str)
    assert len(result) > len("Fix the parser"), "Assembler should expand prompt with L1+L2 context"
    assert "DISPATCH INSTRUCTION:" in result


# ---------------------------------------------------------------------------
# test_codex_build_prompt_fallback_to_raw_when_no_role
# ---------------------------------------------------------------------------

def test_codex_build_prompt_fallback_to_raw_when_no_role(adapter: CodexAdapter) -> None:
    """When role= is None and dispatch_metadata is absent, _build_prompt returns raw instruction."""
    with patch("vertex_ai_runner.collect_file_contents", side_effect=_mock_collect):
        result = adapter._build_prompt(
            instruction="Fix the parser",
            changed_files=[],
        )
    assert result == "Fix the parser"


# ---------------------------------------------------------------------------
# test_codex_prompt_includes_base_worker_rules
# ---------------------------------------------------------------------------

def test_codex_prompt_includes_base_worker_rules(adapter: CodexAdapter) -> None:
    """Assembled codex prompt must include Layer 1 base worker rules (billing safety)."""
    with patch("vertex_ai_runner.collect_file_contents", side_effect=_mock_collect):
        result = adapter._build_prompt(
            instruction="Do work",
            changed_files=[],
            role="backend-developer",
        )
    content_lower = result.lower()
    assert "billing" in content_lower or "anthropic" in content_lower, \
        "Layer 1 billing safety must appear in assembled codex prompt"


# ---------------------------------------------------------------------------
# test_codex_prompt_includes_role_context
# ---------------------------------------------------------------------------

def test_codex_prompt_includes_role_context(adapter: CodexAdapter) -> None:
    """Assembled codex prompt must include role-specific context for 'backend-developer'."""
    with patch("vertex_ai_runner.collect_file_contents", side_effect=_mock_collect):
        result = adapter._build_prompt(
            instruction="Do work",
            changed_files=[],
            role="backend-developer",
        )
    assert "backend" in result.lower() or "backend-developer" in result.lower(), \
        "Role context for backend-developer must appear in assembled codex prompt"
