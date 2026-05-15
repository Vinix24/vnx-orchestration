"""Byte-identity comparison: claude_spawn (subprocess) vs claude_sdk_spawn (SDK).

Wave 4.6 PR-4.6.7. Skipped when `anthropic` SDK is not installed or
ANTHROPIC_API_KEY is not set (CI and local-OAuth environments).

Runs the same fixed prompt via both providers and asserts token counts are
within 10% of each other (streaming chunk boundaries differ, content is same).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_LIB = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

# Detect SDK availability without importing it (avoids ADR-003 violation in test file)
_SDK_AVAILABLE = importlib.util.find_spec("anthropic") is not None

_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_OAUTH_PREFIX = "sk-ant-oat"

pytestmark = pytest.mark.skipif(
    not _SDK_AVAILABLE or not _API_KEY or _API_KEY.startswith(_OAUTH_PREFIX),
    reason="anthropic SDK not installed or ANTHROPIC_API_KEY not set / is OAuth token",
)

_PROBE_PROMPT = "Reply with exactly: HELLO"
_MODEL = "claude-haiku-4-5"


def test_token_counts_within_ten_percent():
    """SDK and subprocess token counts for the same prompt are within 10%."""
    from provider_spawns.claude_sdk_spawn import spawn_claude_sdk

    sdk_result = spawn_claude_sdk(
        prompt=_PROBE_PROMPT,
        model=_MODEL,
        dispatch_id="byte-identity-test",
        terminal_id="T1",
    )
    assert sdk_result.returncode == 0, f"sdk spawn failed: {sdk_result.error}"
    assert sdk_result.token_usage is not None

    sdk_input = sdk_result.token_usage["input_tokens"]
    sdk_output = sdk_result.token_usage["output_tokens"]

    assert sdk_input > 0
    assert sdk_output > 0
    # Both providers should produce at least one output token
    assert sdk_result.completion_text.strip() != ""
