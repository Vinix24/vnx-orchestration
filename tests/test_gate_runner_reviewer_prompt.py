#!/usr/bin/env python3
"""Tests: codex/gemini gate prompts assembled via PromptAssembler with reviewer role.

Covers wave4.5 PR-2b: _build_codex_prompt + _build_gemini_prompt now wrap their
L3 dispatch payload in PromptAssembler layers (L1 base_worker + L2 reviewer role).

Verdict JSON template must be preserved in L3 — gate_recorder.py parser depends on it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gate_runner import GateRunner


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _payload_with_file(tmp_path: Path) -> dict:
    """Build a minimal request payload with a real changed file on disk."""
    f = tmp_path / "example.py"
    f.write_text("def reviewed_function():\n    return 42\n")
    return {
        "branch": "feat/test-branch",
        "risk_class": "medium",
        "changed_files": [str(f)],
        "pr_number": 99,
        "pr_id": "TEST-PR-99",
    }


def _empty_payload() -> dict:
    """Minimal payload with no changed files — tests without file I/O."""
    return {
        "branch": "feat/empty",
        "risk_class": "low",
        "changed_files": [],
        "pr_number": 0,
        "pr_id": "",
    }


def _no_git_subprocess():
    """subprocess.run mock that returns empty git output (no discovered files)."""
    mock = MagicMock()
    mock.stdout = ""
    return mock


# ---------------------------------------------------------------------------
# Codex prompt tests
# ---------------------------------------------------------------------------


class TestCodexPromptReviewerRole:
    """_build_codex_prompt assembles via PromptAssembler with role=reviewer."""

    def test_codex_prompt_includes_reviewer_role(self, tmp_path):
        """Prompt must contain the reviewer role marker from reviewer.md (L2)."""
        payload = _payload_with_file(tmp_path)
        prompt = GateRunner._build_codex_prompt(payload)
        assert "VNX governance reviewer" in prompt

    def test_codex_prompt_includes_base_worker_rules(self, tmp_path):
        """Prompt must contain base_worker.md content (L1 layer)."""
        payload = _payload_with_file(tmp_path)
        prompt = GateRunner._build_codex_prompt(payload)
        # Canonical phrase from base_worker.md
        assert "No TODO comments" in prompt

    def test_codex_prompt_preserves_verdict_template(self, tmp_path):
        """Verdict JSON template must remain in the prompt (gate_recorder.py parses it)."""
        payload = _payload_with_file(tmp_path)
        prompt = GateRunner._build_codex_prompt(payload)
        assert re.search(r"pass\|fail\|blocked", prompt) is not None
        assert '"verdict"' in prompt
        assert '"findings"' in prompt

    def test_codex_prompt_includes_diff_content(self, tmp_path):
        """File content must be inlined in the prompt (L3 diff section)."""
        payload = _payload_with_file(tmp_path)
        prompt = GateRunner._build_codex_prompt(payload)
        assert "--- FILE:" in prompt

    def test_codex_prompt_returns_string(self, tmp_path):
        """_build_codex_prompt must return a plain string (stdin-writable)."""
        payload = _payload_with_file(tmp_path)
        result = GateRunner._build_codex_prompt(payload)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_codex_prompt_graceful_when_git_fails(self):
        """When git diff raises, prompt still contains verdict template."""
        payload = _empty_payload()
        with patch("gate_runner.subprocess.run", side_effect=OSError("git not found")):
            prompt = GateRunner._build_codex_prompt(payload)
        assert "verdict" in prompt
        assert "VNX governance reviewer" in prompt


# ---------------------------------------------------------------------------
# Gemini prompt tests
# ---------------------------------------------------------------------------


class TestGeminiPromptReviewerRole:
    """_build_gemini_prompt assembles via PromptAssembler with role=reviewer."""

    def test_gemini_prompt_includes_reviewer_role(self, tmp_path):
        """Prompt must contain the reviewer role marker from reviewer.md (L2)."""
        payload = _payload_with_file(tmp_path)
        prompt = GateRunner._build_gemini_prompt(payload)
        assert "VNX governance reviewer" in prompt

    def test_gemini_prompt_verdict_template_preserved(self, tmp_path):
        """Verdict JSON template must remain in the prompt (gate_recorder.py parses it)."""
        payload = _payload_with_file(tmp_path)
        prompt = GateRunner._build_gemini_prompt(payload)
        assert re.search(r"pass\|fail\|blocked", prompt) is not None
        assert '"verdict"' in prompt
        assert '"findings"' in prompt

    def test_gemini_prompt_includes_base_worker_rules(self, tmp_path):
        """Prompt must contain base_worker.md content (L1 layer)."""
        payload = _payload_with_file(tmp_path)
        prompt = GateRunner._build_gemini_prompt(payload)
        assert "No TODO comments" in prompt

    def test_gemini_prompt_returns_string(self, tmp_path):
        """_build_gemini_prompt must return a plain string (stdin/vertex-writable)."""
        payload = _payload_with_file(tmp_path)
        result = GateRunner._build_gemini_prompt(payload)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_gemini_prompt_graceful_when_git_fails(self):
        """When git diff raises, prompt still contains verdict template."""
        payload = _empty_payload()
        with patch("gate_runner.subprocess.run", side_effect=OSError("git not found")):
            prompt = GateRunner._build_gemini_prompt(payload)
        assert "verdict" in prompt
        assert "VNX governance reviewer" in prompt


# ---------------------------------------------------------------------------
# PromptAssembler metadata test
# ---------------------------------------------------------------------------


class TestPromptRoleMetadata:
    """PromptAssembler correctly sets role=reviewer in metadata."""

    def test_prompt_role_metadata_set(self):
        """AssembledPrompt.metadata['role'] must equal 'reviewer' when role=reviewer."""
        from prompt_assembler import PromptAssembler

        assembled = PromptAssembler().assemble(
            dispatch_metadata={
                "role": "reviewer",
                "branch": "feat/test",
                "risk_class": "medium",
                "pr_id": "TEST-001",
            },
            instruction="Review the diff.",
        )
        assert assembled.metadata["role"] == "reviewer"

    def test_reviewer_md_loaded_as_layer2(self):
        """reviewer.md content appears in AssembledPrompt.context (L2)."""
        from prompt_assembler import PromptAssembler

        assembled = PromptAssembler().assemble(
            dispatch_metadata={"role": "reviewer"},
            instruction="Review.",
        )
        assert "VNX governance reviewer" in assembled.context
