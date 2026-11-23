#!/usr/bin/env python3
"""OI-1230 regression: gate_runner must not double-inline file contents into the Gemini prompt.

Background: ``gate_runner._resolve_prompt`` previously appended
``collect_file_contents`` to a prompt that already carried the
file bodies, producing two copies of every file section. Since #479
(90dbf23ca) the self-built gemini prompt carries the ``gh pr diff`` text
instead of inlined ``--- FILE:`` sections, so the diff body must appear exactly
once and no file section may be appended on top of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


@pytest.fixture(autouse=True)
def _fake_pr_diff(monkeypatch):
    """The self-built prompt reads ``gh pr diff``; serve a fixed diff instead."""
    monkeypatch.setattr(
        GateRunner, "_fetch_gh_pr_diff",
        staticmethod(lambda pr_number: DIFF_TEXT),
    )


DIFF_TEXT = (
    "diff --git a/alpha.py b/alpha.py\n"
    "+++ b/alpha.py\n"
    "+UNIQUE_MARKER_TOKEN_42\n"
)


def test_resolve_prompt_inlines_files_exactly_once_with_branch(tmp_path):
    """The diff is in the prompt once and no FILE section is appended."""
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
        gate="gemini_review", request_payload=payload,
    )

    assert prompt.count("+++ b/alpha.py") == 1
    assert "--- FILE:" not in prompt, "file bodies must not be appended to the diff prompt"
    assert str(file_b) not in prompt


def test_resolve_prompt_inlines_files_exactly_once_without_branch(tmp_path):
    """The subprocess path carries the diff once, with no FILE section."""
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
        gate="gemini_review", request_payload=payload,
    )

    assert prompt.count("+++ b/alpha.py") == 1
    assert "--- FILE:" not in prompt


def test_file_contents_appear_once(tmp_path):
    """The diff body text must appear exactly once."""
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
        gate="gemini_review", request_payload=payload,
    )

    assert prompt.count("UNIQUE_MARKER_TOKEN_42") == 1
