#!/usr/bin/env python3
"""adapters/__init__.py — Provider adapter registry and factory.

resolve_adapter() is the single entry point for obtaining a ProviderAdapter
for a given terminal.  The provider is selected from VNX_PROVIDER_<terminal>
env var, defaulting to 'claude'.

BILLING SAFETY: No Anthropic SDK.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure scripts/lib is importable when this package is imported directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider_adapter import ProviderAdapter


def format_for_provider(assembled: object, provider: str) -> dict:
    """Format an AssembledPrompt for a specific provider's invocation.

    Delegates to ``prompt_assembler.format_for_provider()`` so callers don't
    need to import the assembler module directly.

    Returns a dict with provider-specific payload keys:
      claude  → {"pipe_input": "<full user message string>"}
      gemini  → {"system_instruction": "<L1+L2>", "prompt": "<L3>"}
      codex   → {"pipe_input": "<full concatenated string>"}
      ollama  → {"system": "<L1+L2>", "prompt": "<L3>"}

    Args:
        assembled: AssembledPrompt from PromptAssembler.assemble().
        provider:  One of "claude", "gemini", "codex", "ollama".

    Raises:
        ValueError: When provider is not one of the supported values.
    """
    from prompt_assembler import format_for_provider as _fmt  # noqa: PLC0415
    return _fmt(assembled, provider)


def resolve_adapter(terminal_id: str) -> ProviderAdapter:
    """Resolve adapter from VNX_PROVIDER_<terminal> env var.

    Defaults to ClaudeAdapter when the env var is unset.

    Args:
        terminal_id: Terminal identifier, e.g. 'T1', 'T2', 'T3'.

    Returns:
        Configured ProviderAdapter instance for the terminal.

    Raises:
        ValueError: When the configured provider name is unknown.
    """
    provider = os.environ.get(f"VNX_PROVIDER_{terminal_id}", "claude").lower()

    if provider == "claude":
        from adapters.claude_adapter import ClaudeAdapter  # noqa: PLC0415
        return ClaudeAdapter(terminal_id)

    if provider == "gemini":
        from adapters.gemini_adapter import GeminiAdapter  # noqa: PLC0415
        return GeminiAdapter(terminal_id)

    if provider == "codex":
        from adapters.codex_adapter import CodexAdapter  # noqa: PLC0415
        return CodexAdapter(terminal_id)

    if provider == "ollama":
        from adapters.ollama_adapter import OllamaAdapter  # noqa: PLC0415
        return OllamaAdapter(terminal_id)

    raise ValueError(
        f"Unknown provider '{provider}' for terminal {terminal_id}. "
        f"Set VNX_PROVIDER_{terminal_id}=claude|gemini|codex|ollama or leave unset for default."
    )
