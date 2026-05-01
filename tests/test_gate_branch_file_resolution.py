#!/usr/bin/env python3
"""W3D — Gate file resolution from PR branch (resolves OI-1112 + OI-1114).

Verifies that gate prompt builders pull file content from the PR branch via
`git show branch:path` rather than reading the cwd-relative file. Without
this fix, gates run from the main worktree miss files added on the PR branch
and return blocked verdicts referencing missing files (e.g. PR #375 issue:
"The specified file 'tests/test_session_store_concurrent.py' was not found").
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import vertex_ai_runner as _vtx


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def branched_repo(tmp_path, monkeypatch):
    """Build a repo with main and a feature branch that adds + modifies files.

    Layout:
      main:        existing.py = 'main version'
      feature/x:   existing.py = 'feature version'
                   new_only.py = 'feature only'
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "config", "user.email", "test@test"], repo)
    _run(["git", "config", "user.name", "Test"], repo)

    (repo / "existing.py").write_text("main version\n")
    _run(["git", "add", "existing.py"], repo)
    _run(["git", "commit", "-q", "-m", "main: add existing"], repo)

    _run(["git", "checkout", "-q", "-b", "feature/x"], repo)
    (repo / "existing.py").write_text("feature version\n")
    (repo / "new_only.py").write_text("feature only\n")
    _run(["git", "add", "existing.py", "new_only.py"], repo)
    _run(["git", "commit", "-q", "-m", "feature: modify existing, add new_only"], repo)

    # Switch back to main so cwd reflects the "main worktree" scenario.
    _run(["git", "checkout", "-q", "main"], repo)

    # Run gate code as if invoked from the main worktree (cwd = repo on main).
    monkeypatch.chdir(repo)
    return repo


class TestPromptBuilderResolvesFromBranch:
    """build_gemini_prompt and build_codex_prompt must read from the PR branch."""

    def test_gemini_prompt_uses_branch_version_of_modified_file(self, branched_repo):
        """File modified on PR branch is rendered with branch content, not cwd."""
        payload = {
            "branch": "feature/x",
            "risk_class": "medium",
            "pr_number": 1,
            "changed_files": ["existing.py"],
        }
        prompt = _vtx.build_gemini_prompt(payload, subprocess_run=subprocess.run)

        assert "feature version" in prompt
        assert "main version" not in prompt
        assert "--- FILE: existing.py" in prompt

    def test_gemini_prompt_includes_file_added_only_on_branch(self, branched_repo):
        """File that exists only on the PR branch is included in the prompt.

        This is the exact PR #375 scenario: the file was added on the feature
        branch, gate ran from main worktree, prompt section was empty.
        """
        # Sanity: the file genuinely does NOT exist on disk in the main worktree.
        assert not (branched_repo / "new_only.py").exists()

        payload = {
            "branch": "feature/x",
            "risk_class": "medium",
            "pr_number": 1,
            "changed_files": ["new_only.py"],
        }
        prompt = _vtx.build_gemini_prompt(payload, subprocess_run=subprocess.run)

        assert "feature only" in prompt
        assert "--- FILE: new_only.py" in prompt

    def test_codex_prompt_uses_branch_version(self, branched_repo):
        """Codex prompt builder also resolves from the PR branch."""
        payload = {
            "branch": "feature/x",
            "risk_class": "high",
            "pr_number": 2,
            "changed_files": ["existing.py", "new_only.py"],
        }
        prompt = _vtx.build_codex_prompt(payload, subprocess_run=subprocess.run)

        assert "feature version" in prompt
        assert "feature only" in prompt
        assert "main version" not in prompt

    def test_collect_file_contents_uses_branch(self, branched_repo):
        """collect_file_contents (used for contract-prompt enrichment) honors branch."""
        payload = {
            "branch": "feature/x",
            "changed_files": ["existing.py", "new_only.py"],
        }
        contents = _vtx.collect_file_contents(payload, subprocess_run=subprocess.run)

        assert "feature version" in contents
        assert "feature only" in contents
        assert "main version" not in contents

    def test_falls_back_to_filesystem_when_file_not_in_branch(self, branched_repo):
        """Files not committed to the branch fall back to filesystem read.

        This preserves local-iteration ergonomics: an uncommitted file
        on disk still appears in the prompt.
        """
        local_only = branched_repo / "uncommitted.py"
        local_only.write_text("local edit\n")

        payload = {
            "branch": "feature/x",
            "risk_class": "low",
            "pr_number": 3,
            "changed_files": ["existing.py", "uncommitted.py"],
        }
        prompt = _vtx.build_gemini_prompt(payload, subprocess_run=subprocess.run)

        assert "feature version" in prompt  # via git show
        assert "local edit" in prompt        # via filesystem fallback

    def test_no_branch_falls_back_to_filesystem(self, branched_repo):
        """When branch is empty, behavior matches pre-fix filesystem read."""
        payload = {
            "branch": "",
            "risk_class": "medium",
            "pr_number": 4,
            "changed_files": ["existing.py"],
        }
        prompt = _vtx.build_gemini_prompt(payload, subprocess_run=subprocess.run)

        # cwd is on main, so we expect main's content.
        assert "main version" in prompt
        assert "feature version" not in prompt

    def test_absolute_paths_skip_git_show(self, branched_repo, tmp_path):
        """Absolute paths are not tracked git paths; resolver must fall through.

        Guards against accidentally invoking `git show branch:/abs/path`,
        which would always fail and silently drop legitimate filesystem files.
        """
        outside = tmp_path / "outside.py"
        outside.write_text("absolute path content\n")

        payload = {
            "branch": "feature/x",
            "risk_class": "medium",
            "pr_number": 5,
            "changed_files": [str(outside)],
        }
        prompt = _vtx.build_gemini_prompt(payload, subprocess_run=subprocess.run)

        assert "absolute path content" in prompt


class TestBranchResolutionPreservesByteCap:
    """VNX_GEMINI_MAX_PROMPT_BYTES must still bound branch-resolved content."""

    def test_byte_cap_applied_to_branch_content(self, branched_repo, monkeypatch):
        """Cap applies whether content comes from git show or filesystem."""
        big = "X" * 5000 + "\n"
        (branched_repo / "big.py").write_text(big)
        _run(["git", "checkout", "-q", "feature/x"], branched_repo)
        _run(["git", "add", "big.py"], branched_repo)
        _run(["git", "commit", "-q", "-m", "add big"], branched_repo)
        _run(["git", "checkout", "-q", "main"], branched_repo)

        monkeypatch.setenv("VNX_GEMINI_MAX_PROMPT_BYTES", "300")

        payload = {
            "branch": "feature/x",
            "risk_class": "medium",
            "pr_number": 6,
            "changed_files": ["big.py"],
        }
        prompt = _vtx.build_gemini_prompt(payload, subprocess_run=subprocess.run)

        file_sections = prompt.split("--- FILE:")[1:]
        total = sum(len(s.encode("utf-8")) for s in file_sections)
        assert total <= 500, f"branch content not bounded by max bytes: {total}"
