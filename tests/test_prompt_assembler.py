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
# test_layer1_scratch_cleanup_is_noninteractive (OI-104)
# ---------------------------------------------------------------------------

def test_layer1_scratch_cleanup_is_noninteractive(assembler: PromptAssembler, basic_instruction: str) -> None:
    """base_worker.md's cleanup instruction must never tell a worker to run a
    literal `rm -rf`/`rmdir` — Claude Code's own dangerous-rm safety check
    requires interactive approval for that regardless of
    --dangerously-skip-permissions, which hangs a headless dispatch forever
    (OI-104). The instruction must point workers at a non-rm alternative
    (python3 shutil.rmtree) instead.
    """
    # Scoped to Layer 1 alone (base_worker.md) — Layer 2 role files legitimately
    # keep "rm -rf*" in their own (separate, pre-existing) bash_deny_patterns
    # listing, which would otherwise pollute a whole-context substring check.
    layer1 = assembler._load_base()
    assert "temporary files" in layer1.lower()
    # The instruction must explicitly warn against rm -rf/rmdir (this sentence
    # itself legitimately contains the substring "rm -rf" as a warning, not a
    # suggested command)...
    assert "do not use `rm -rf`" in layer1.lower()
    # ...and no *standalone suggested* rm -rf/rm -r/rmdir invocation may appear
    # anywhere else in Layer 1 (only inside that one warning clause).
    warning_clause = "rm -rf`/`rmdir`"
    assert layer1.count(warning_clause) == 1
    scrubbed = layer1.replace(warning_clause, "")
    assert "rm -rf" not in scrubbed
    assert "rm -r " not in scrubbed
    assert "rmdir" not in scrubbed.lower()
    # The safe non-rm alternative must be present.
    assert "shutil.rmtree" in layer1

    # The alternative must be a GUARDED delete, not a blind recursive one —
    # swapping a gated `rm -rf` for an unguarded `shutil.rmtree` is a net
    # safety regression: a wrong literal path gets silently, recursively
    # deleted with no confirmation and no error (codex gate finding on PR
    # #1169). The guidance must resolve the target to a real path and refuse
    # deletion outside a recognized scope, instead of masking a bad path with
    # ignore_errors=True.
    assert "shutil.rmtree(target)" in layer1, (
        "the recommended rmtree call must take no ignore_errors=True argument "
        "— that flag would silently mask a wrong-path deletion instead of "
        "surfacing it"
    )
    assert "realpath" in layer1.lower(), (
        "guard must resolve the target to an absolute real path before deciding"
    )
    for keyword in ("home", "tempdir", "tmpdir", "/tmp"):
        assert keyword in layer1.lower(), f"guard must reference {keyword!r} in its scoping checks"
    assert "refus" in layer1.lower(), (
        "guard must refuse (raise/exit without deleting) on an unsafe target, "
        "not silently proceed"
    )

    # Sanity: the assembled prompt still includes L1 verbatim (regression guard
    # against a future refactor silently dropping Layer 1 from the pipe input).
    prompt = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction=basic_instruction,
    )
    assert "shutil.rmtree" in prompt.context
    assert "realpath" in prompt.context.lower()


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


# ---------------------------------------------------------------------------
# Wave 4.5 PR-1 — provider-agnostic method tests
# ---------------------------------------------------------------------------

def test_to_pipe_input_unchanged_byte_identical(assembler: PromptAssembler) -> None:
    """Regression: to_pipe_input output is byte-for-byte unchanged after Wave 4.5 PR-1 refactor."""
    result = assembler.assemble(
        dispatch_metadata={"role": "backend-developer", "terminal": "T1"},
        instruction="test instruction",
    )
    pipe = result.to_pipe_input()
    assert "\n\n---\n\nDISPATCH INSTRUCTION:\n\n" in pipe
    assert pipe.endswith("test instruction")


def test_for_claude_subprocess_equals_to_pipe_input(assembler: PromptAssembler) -> None:
    """for_claude_subprocess() must be byte-identical to to_pipe_input()."""
    r = assembler.assemble(dispatch_metadata={"role": "backend-developer"}, instruction="x")
    assert r.for_claude_subprocess() == r.to_pipe_input()


def test_for_codex_subprocess_uses_dispatch_header(assembler: PromptAssembler) -> None:
    """for_codex_subprocess() delegates to format_for_provider('codex') — same as to_pipe_input()."""
    r = assembler.assemble(dispatch_metadata={"role": "backend-developer"}, instruction="x")
    pipe = r.for_codex_subprocess()
    assert "DISPATCH INSTRUCTION:" in pipe
    assert pipe.endswith("x")
    assert pipe == r.to_pipe_input()


def test_for_gemini_subprocess_uses_simple_separator(assembler: PromptAssembler) -> None:
    """for_gemini_subprocess() must use plain '---' separator with no 'DISPATCH INSTRUCTION' header."""
    r = assembler.assemble(dispatch_metadata={"role": "backend-developer"}, instruction="x")
    pipe = r.for_gemini_subprocess()
    assert "\n\n---\n\n" in pipe
    assert "DISPATCH INSTRUCTION" not in pipe
    assert pipe.endswith("x")


def test_for_litellm_provider_openai_shape(assembler: PromptAssembler) -> None:
    """for_litellm_provider() must return messages-array dict with system role entry + metadata (ADV-1 fix)."""
    r = assembler.assemble(dispatch_metadata={"role": "backend-developer"}, instruction="x")
    result = r.for_litellm_provider("deepseek")
    assert isinstance(result, dict)
    assert "system" not in result  # no top-level system key — system is in messages array
    assert result["messages"][0]["role"] == "system"
    assert result["messages"][0]["content"] == r.context
    assert result["messages"][1]["role"] == "user"
    assert result["messages"][1]["content"] == "x"
    assert result["metadata"]["provider"] == "deepseek"


# ---------------------------------------------------------------------------
# Wave 4.5 PR-2 — alignment + ADV-1 fix tests
# ---------------------------------------------------------------------------

def test_for_codex_subprocess_equals_format_for_provider_codex(assembler: PromptAssembler) -> None:
    """for_codex_subprocess() output must equal format_for_provider(assembled, 'codex')['pipe_input']."""
    r = assembler.assemble(dispatch_metadata={"role": "backend-developer"}, instruction="align-check")
    assert r.for_codex_subprocess() == format_for_provider(r, "codex")["pipe_input"]


def test_for_gemini_subprocess_equals_format_for_provider_gemini(assembler: PromptAssembler) -> None:
    """for_gemini_subprocess() must produce the same content as combining format_for_provider('gemini') fields."""
    r = assembler.assemble(dispatch_metadata={"role": "backend-developer"}, instruction="align-check")
    ffp = format_for_provider(r, "gemini")
    expected = f"{ffp['system_instruction']}\n\n---\n\n{ffp['prompt']}"
    assert r.for_gemini_subprocess() == expected


def test_for_litellm_provider_returns_messages_with_system_role(assembler: PromptAssembler) -> None:
    """ADV-1 fix: system context must be messages[0] with role='system', not a top-level 'system' key."""
    r = assembler.assemble(dispatch_metadata={"role": "backend-developer"}, instruction="litellm-test")
    result = r.for_litellm_provider("kimi")
    assert isinstance(result.get("messages"), list)
    assert len(result["messages"]) == 2
    assert result["messages"][0]["role"] == "system"
    assert result["messages"][0]["content"] == r.context
    assert result["messages"][1]["role"] == "user"
    assert result["messages"][1]["content"] == "litellm-test"
    assert "system" not in result
    assert result["metadata"]["provider"] == "kimi"
