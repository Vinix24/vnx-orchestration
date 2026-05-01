#!/usr/bin/env python3
"""W3G regression tests for vertex_ai_runner.build_gemini_prompt.

OI-1230: build_gemini_prompt must inline each changed file exactly once.
OI-1093: build_gemini_prompt must include an explicit grounding guardrail
that instructs the model to only flag findings about code present in the
FILE blocks (mitigates hallucinated file paths / decorator names).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from vertex_ai_runner import build_gemini_prompt  # noqa: E402


def _no_branch_subprocess() -> MagicMock:
    """subprocess_run double that makes git show fail so filesystem fallback is used."""
    run = MagicMock()
    run.return_value = MagicMock(returncode=128, stdout="", stderr="")
    return run


def test_build_gemini_prompt_inlines_each_file_exactly_once(tmp_path):
    """OI-1230: a 2-file PR yields exactly 2 ``--- FILE:`` markers, not 4."""
    file_a = tmp_path / "alpha.py"
    file_a.write_text("ALPHA_BODY\n")
    file_b = tmp_path / "beta.py"
    file_b.write_text("BETA_BODY\n")

    payload = {
        "changed_files": [str(file_a), str(file_b)],
        "branch": "",
        "risk_class": "low",
        "pr_number": 1,
    }
    prompt = build_gemini_prompt(payload, subprocess_run=_no_branch_subprocess())

    assert prompt.count("--- FILE:") == 2, (
        "build_gemini_prompt must emit exactly one FILE marker per changed file"
    )
    assert prompt.count(f"--- FILE: {file_a} ---") == 1
    assert prompt.count(f"--- FILE: {file_b} ---") == 1
    assert prompt.count("ALPHA_BODY") == 1
    assert prompt.count("BETA_BODY") == 1


def test_build_gemini_prompt_grounding_guardrail_present(tmp_path):
    """OI-1093: the prompt must contain a grounding guardrail instructing the
    model to only flag findings about code present in the FILE blocks."""
    file_a = tmp_path / "only.py"
    file_a.write_text("x = 1\n")
    payload = {
        "changed_files": [str(file_a)],
        "branch": "",
        "risk_class": "medium",
        "pr_number": 42,
    }
    prompt = build_gemini_prompt(payload, subprocess_run=_no_branch_subprocess())

    assert "Grounding rule" in prompt, "missing grounding rule header"
    assert "Only flag findings about code present in the FILE blocks" in prompt, (
        "grounding instruction must explicitly restrict findings to the FILE blocks"
    )
    assert "Do NOT invent file paths" in prompt, (
        "grounding instruction must explicitly forbid inventing file paths"
    )
    assert "quotable verbatim" in prompt, (
        "grounding instruction must require quotable verbatim references"
    )


def test_build_gemini_prompt_preserves_output_schema(tmp_path):
    """The structured JSON verdict schema (verdict/findings/residual_risk/rerun_required)
    must remain present after the W3G changes — gemini gate parser depends on it."""
    file_a = tmp_path / "a.py"
    file_a.write_text("y = 2\n")
    payload = {
        "changed_files": [str(file_a)],
        "branch": "",
        "risk_class": "high",
        "pr_number": 7,
    }
    prompt = build_gemini_prompt(payload, subprocess_run=_no_branch_subprocess())

    assert '"verdict": "pass|fail|blocked"' in prompt
    assert '"findings"' in prompt
    assert '"residual_risk"' in prompt
    assert '"rerun_required"' in prompt
    assert '"rerun_reason"' in prompt
