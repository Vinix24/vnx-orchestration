#!/usr/bin/env python3
"""OI-1230 regression: gate_runner must not double-inline file contents into the Gemini prompt.

Background: ``vertex_ai_runner.build_gemini_prompt`` already inlines each
changed file as a ``--- FILE: <path> ---`` section. ``gate_runner._resolve_prompt``
previously appended ``vertex_ai_runner.collect_file_contents`` again whenever the
Vertex path was used, producing two copies of every file section. The fix is to
let build_gemini_prompt own the inlining; gate_runner must not duplicate it.
"""

from __future__ import annotations

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))

from gate_runner import GateRunner  # noqa: E402


def _make_runner(tmp_path: Path) -> GateRunner:
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    state_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    return GateRunner(state_dir=state_dir, reports_dir=reports_dir)


def test_resolve_prompt_inlines_files_exactly_once_for_vertex(tmp_path):
    """When using_vertex=True, each --- FILE: section appears exactly once."""
    file_a = tmp_path / "alpha.py"
    file_a.write_text("ALPHA_CONTENT\n")
    file_b = tmp_path / "beta.py"
    file_b.write_text("BETA_CONTENT\n")

    payload = {
        "changed_files": [str(file_a), str(file_b)],
        "branch": "fix/oi-1230",
        "risk_class": "low",
        "pr_number": 1,
    }

    runner = _make_runner(tmp_path)
    prompt = runner._resolve_prompt(
        gate="gemini_review", request_payload=payload, using_vertex=True,
    )

    assert prompt.count(f"--- FILE: {file_a} ---") == 1, (
        "alpha.py must be inlined exactly once, not duplicated"
    )
    assert prompt.count(f"--- FILE: {file_b} ---") == 1, (
        "beta.py must be inlined exactly once, not duplicated"
    )


def test_resolve_prompt_inlines_files_exactly_once_without_vertex(tmp_path):
    """Subprocess (non-Vertex) path also inlines files exactly once."""
    file_a = tmp_path / "alpha.py"
    file_a.write_text("ALPHA_CONTENT\n")

    payload = {
        "changed_files": [str(file_a)],
        "branch": "fix/oi-1230",
        "risk_class": "low",
        "pr_number": 1,
    }

    runner = _make_runner(tmp_path)
    prompt = runner._resolve_prompt(
        gate="gemini_review", request_payload=payload, using_vertex=False,
    )

    assert prompt.count(f"--- FILE: {file_a} ---") == 1


def test_file_contents_appear_once(tmp_path):
    """Inline file body text must appear exactly once even on the Vertex path."""
    file_a = tmp_path / "unique.py"
    file_a.write_text("UNIQUE_MARKER_TOKEN_42\n")

    payload = {
        "changed_files": [str(file_a)],
        "branch": "fix/oi-1230",
        "risk_class": "medium",
        "pr_number": 7,
    }

    runner = _make_runner(tmp_path)
    prompt = runner._resolve_prompt(
        gate="gemini_review", request_payload=payload, using_vertex=True,
    )

    assert prompt.count("UNIQUE_MARKER_TOKEN_42") == 1


def test_external_prompt_gets_file_contents_appended_exactly_once(tmp_path):
    """Externally supplied prompts (e.g. contract prompts) must be enriched with
    file contents on the Vertex path, with each --- FILE: section appearing
    exactly once.
    """
    file_a = tmp_path / "alpha.py"
    file_a.write_text("ALPHA_BODY_ONLY_ONCE\n")

    payload = {
        "prompt": "EXTERNAL_CONTRACT_PROMPT",
        "changed_files": [str(file_a)],
        "branch": "fix/oi-1230",
        "risk_class": "low",
        "pr_number": 1,
    }

    runner = _make_runner(tmp_path)
    prompt = runner._resolve_prompt(
        gate="gemini_review", request_payload=payload, using_vertex=True,
    )

    assert "EXTERNAL_CONTRACT_PROMPT" in prompt
    assert prompt.count(f"--- FILE: {file_a} ---") == 1
    assert prompt.count("ALPHA_BODY_ONLY_ONCE") == 1
