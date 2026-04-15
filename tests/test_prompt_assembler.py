#!/usr/bin/env python3
"""tests/test_prompt_assembler.py — Unit tests for PromptAssembler (F58-PR3).

Tests the 3-layer user message architecture:
  Layer 1 — base_worker.md (universal rules, billing safety)
  Layer 2 — roles/<role>.md (per-role capabilities and permissions)
  Layer 3 — dispatch payload (instruction + enrichments)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure scripts/lib is on the path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from prompt_assembler import AssembledPrompt, PromptAssembler, format_for_provider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def assembler() -> PromptAssembler:
    return PromptAssembler()


@pytest.fixture()
def basic_instruction() -> str:
    return "Fix the bug in scripts/lib/foo.py where the parser fails on empty input."


# ---------------------------------------------------------------------------
# test_assemble_backend_developer
# ---------------------------------------------------------------------------

def test_assemble_backend_developer(assembler: PromptAssembler, basic_instruction: str) -> None:
    """Layer 2 must contain backend-developer role text."""
    prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=basic_instruction,
    )

    assert isinstance(prompt, AssembledPrompt)
    assert "backend" in prompt.context.lower() or "backend-developer" in prompt.context.lower()
    assert prompt.metadata["role"] == "backend-developer"
    assert "layer2_chars" in prompt.metadata
    assert prompt.metadata["layer2_chars"] > 0


# ---------------------------------------------------------------------------
# test_assemble_test_engineer
# ---------------------------------------------------------------------------

def test_assemble_test_engineer(assembler: PromptAssembler, basic_instruction: str) -> None:
    """Layer 2 for test-engineer must be different from backend-developer."""
    be_prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=basic_instruction,
    )
    te_prompt = assembler.assemble(
        dispatch_metadata={"role": "test-engineer", "terminal": "T2"},
        instruction=basic_instruction,
    )

    # Role labels differ
    assert be_prompt.metadata["role"] != te_prompt.metadata["role"]
    # Context (L1+L2) differs between roles
    assert be_prompt.context != te_prompt.context
    # test-engineer context mentions test-specific content
    assert "test" in te_prompt.context.lower()


# ---------------------------------------------------------------------------
# test_layer1_always_included
# ---------------------------------------------------------------------------

def test_layer1_always_included(assembler: PromptAssembler, basic_instruction: str) -> None:
    """Base worker rules must appear in every assembled prompt regardless of role."""
    for role in ("backend-developer", "test-engineer", "frontend-developer", "architect"):
        prompt = assembler.assemble(
            dispatch_metadata={"role": role, "terminal": "T1"},
            instruction=basic_instruction,
        )
        # Layer 1 content includes billing safety and report discipline
        assert "billing" in prompt.context.lower() or "anthropic" in prompt.context.lower(), \
            f"Billing safety not found in L1 for role={role}"
        assert prompt.metadata["layer1_chars"] > 0, f"L1 empty for role={role}"


# ---------------------------------------------------------------------------
# test_unknown_role_falls_back_to_base
# ---------------------------------------------------------------------------

def test_unknown_role_falls_back_to_base(assembler: PromptAssembler, basic_instruction: str) -> None:
    """Unknown role must gracefully degrade — no exception, non-empty context."""
    prompt = assembler.assemble(
        dispatch_metadata={"role": "nonexistent-role-xyz", "terminal": "T1"},
        instruction=basic_instruction,
    )
    assert prompt is not None
    assert len(prompt.context) > 0
    # Falls back to base content — billing safety still present
    assert "billing" in prompt.context.lower() or "anthropic" in prompt.context.lower()
    assert prompt.metadata["role"] == "nonexistent-role-xyz"


# ---------------------------------------------------------------------------
# test_enrichments_appended_to_layer3
# ---------------------------------------------------------------------------

def test_enrichments_appended_to_layer3(assembler: PromptAssembler) -> None:
    """Repo map and intelligence blocks must appear in the instruction (L3)."""
    repo_map_text = "### Repo Map\n\nfoo.py: class Foo\n  def bar()"
    intelligence_text = "Pattern: always read before writing."
    historical_text = "Previous dispatch f57-pr1: success."

    prompt = assembler.assemble(
        dispatch_metadata={
            "role": "backend-developer",
            "terminal": "T1",
            "repo_map": repo_map_text,
            "intelligence": intelligence_text,
            "historical": historical_text,
        },
        instruction="Do the work.",
    )

    assert "foo.py" in prompt.instruction
    assert "always read before writing" in prompt.instruction
    assert "f57-pr1" in prompt.instruction
    assert prompt.metadata["enrichments_applied"] == ["repo_map", "intelligence", "historical"]


# ---------------------------------------------------------------------------
# test_to_pipe_input_format
# ---------------------------------------------------------------------------

def test_to_pipe_input_format(assembler: PromptAssembler, basic_instruction: str) -> None:
    """to_pipe_input() must produce: context + separator + DISPATCH INSTRUCTION header + instruction."""
    prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=basic_instruction,
    )
    pipe = prompt.to_pipe_input()

    assert "---" in pipe
    assert "DISPATCH INSTRUCTION:" in pipe
    assert basic_instruction in pipe
    # Context appears before the separator
    sep_idx = pipe.index("---")
    assert prompt.context[:50] in pipe[:sep_idx + 100]


# ---------------------------------------------------------------------------
# test_format_for_claude
# ---------------------------------------------------------------------------

def test_format_for_claude(assembler: PromptAssembler, basic_instruction: str) -> None:
    """format_for_provider('claude') must return a single pipe_input string."""
    prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=basic_instruction,
    )
    result = format_for_provider(prompt, "claude")

    assert "pipe_input" in result
    assert isinstance(result["pipe_input"], str)
    assert "system" not in result
    assert result["pipe_input"] == prompt.to_pipe_input()


# ---------------------------------------------------------------------------
# test_format_for_ollama
# ---------------------------------------------------------------------------

def test_format_for_ollama(assembler: PromptAssembler, basic_instruction: str) -> None:
    """format_for_provider('ollama') must split into system + prompt fields."""
    prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=basic_instruction,
    )
    result = format_for_provider(prompt, "ollama")

    assert "system" in result
    assert "prompt" in result
    assert result["system"] == prompt.context
    assert result["prompt"] == prompt.instruction
    assert "pipe_input" not in result


# ---------------------------------------------------------------------------
# test_format_for_gemini
# ---------------------------------------------------------------------------

def test_format_for_gemini(assembler: PromptAssembler, basic_instruction: str) -> None:
    """format_for_provider('gemini') must return system_instruction + prompt fields."""
    prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=basic_instruction,
    )
    result = format_for_provider(prompt, "gemini")

    assert "system_instruction" in result
    assert "prompt" in result
    assert result["system_instruction"] == prompt.context
    assert result["prompt"] == prompt.instruction


# ---------------------------------------------------------------------------
# test_format_for_codex
# ---------------------------------------------------------------------------

def test_format_for_codex(assembler: PromptAssembler, basic_instruction: str) -> None:
    """format_for_provider('codex') must return single pipe_input (no system field)."""
    prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=basic_instruction,
    )
    result = format_for_provider(prompt, "codex")

    assert "pipe_input" in result
    assert "system" not in result
    assert result["pipe_input"] == prompt.to_pipe_input()


# ---------------------------------------------------------------------------
# test_format_for_unknown_provider_raises
# ---------------------------------------------------------------------------

def test_format_for_unknown_provider_raises(assembler: PromptAssembler, basic_instruction: str) -> None:
    """format_for_provider must raise ValueError for unknown providers."""
    prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=basic_instruction,
    )
    with pytest.raises(ValueError, match="Unknown provider"):
        format_for_provider(prompt, "openai")


# ---------------------------------------------------------------------------
# test_permission_profile_in_layer2
# ---------------------------------------------------------------------------

def test_permission_profile_in_layer2(assembler: PromptAssembler, basic_instruction: str) -> None:
    """Layer 2 for backend-developer must include permission constraints."""
    prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=basic_instruction,
    )

    # Permission profile content from worker_permissions.yaml should be in L2
    assert "pytest" in prompt.context.lower() or "allowed" in prompt.context.lower(), \
        "Expected permission constraints (pytest, allowed tools) in L2 context"
    assert "rm -rf" in prompt.context or "denied" in prompt.context.lower(), \
        "Expected denied bash patterns in L2 context"


# ---------------------------------------------------------------------------
# test_target_header_stripped
# ---------------------------------------------------------------------------

def test_target_header_stripped(assembler: PromptAssembler) -> None:
    """[[TARGET:T1]] header must be stripped from the instruction."""
    raw = "[[TARGET:T1]]\nTrack: A\n\nDo the actual work here."
    prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=raw,
    )
    assert "[[TARGET:T1]]" not in prompt.instruction
    assert "Do the actual work here." in prompt.instruction


# ---------------------------------------------------------------------------
# test_dispatch_metadata_footer_in_layer3
# ---------------------------------------------------------------------------

def test_dispatch_metadata_footer_in_layer3(assembler: PromptAssembler, basic_instruction: str) -> None:
    """Dispatch-ID must appear in Layer 3 when dispatch_id is provided."""
    prompt = assembler.assemble(
        dispatch_metadata={
            "role": "backend-developer",
            "terminal": "T1",
            "dispatch_id": "abc-123",
            "gate": "f58-pr3",
        },
        instruction=basic_instruction,
    )
    assert "abc-123" in prompt.instruction
    assert "f58-pr3" in prompt.instruction


# ---------------------------------------------------------------------------
# test_no_enrichments_produces_minimal_layer3
# ---------------------------------------------------------------------------

def test_no_enrichments_produces_minimal_layer3(assembler: PromptAssembler) -> None:
    """With no enrichments, Layer 3 must equal the bare instruction."""
    instruction = "Simple task with no enrichments."
    prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=instruction,
    )
    assert prompt.instruction.strip() == instruction
    assert prompt.metadata["enrichments_applied"] == []
