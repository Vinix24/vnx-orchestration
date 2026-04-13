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

    raise ValueError(
        f"Unknown provider '{provider}' for terminal {terminal_id}. "
        f"Set VNX_PROVIDER_{terminal_id}=claude|gemini|codex or leave unset for default."
    )
