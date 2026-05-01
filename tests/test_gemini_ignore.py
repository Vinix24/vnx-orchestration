#!/usr/bin/env python3
"""OI-1154 regression: .geminiignore must not exclude dashboard source files.

Background: The Gemini CLI uses .geminiignore (gitignore-format) to filter
files it includes in review context. A broad 'dashboard/' or 'dashboard/**'
pattern causes dashboard PRs to produce empty review prompts.

Fix: .geminiignore at repo root must only exclude build artifacts
(node_modules/, .next/, dist/, etc.), never whole dashboard/ source trees.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GEMINIIGNORE = REPO_ROOT / ".geminiignore"

DASHBOARD_SOURCE_FILES = [
    "dashboard/api_operator.py",
    "dashboard/api_intelligence.py",
    "dashboard/serve_dashboard.py",
    "dashboard/api_health.py",
    "dashboard/index.html",
]

DASHBOARD_BUILD_DIRS_EXCLUDED = [
    "dashboard/node_modules",
    "dashboard/.next",
    "dashboard/dist",
    "dashboard/build",
    "dashboard/token-dashboard/node_modules",
]


class TestGeminiIgnoreExists:
    def test_geminiignore_exists_at_repo_root(self):
        assert GEMINIIGNORE.exists(), (
            ".geminiignore must exist at repo root (OI-1154). "
            "Gemini CLI uses it to filter review context."
        )

    def test_geminiignore_is_not_empty(self):
        assert GEMINIIGNORE.stat().st_size > 0, ".geminiignore must not be empty"


class TestGeminiIgnoreContent:
    """Parse .geminiignore and assert no whole-directory dashboard exclusions."""

    def _non_comment_lines(self) -> list[str]:
        content = GEMINIIGNORE.read_text(encoding="utf-8")
        return [
            ln.strip()
            for ln in content.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]

    def test_dashboard_source_dir_not_globally_excluded(self):
        lines = self._non_comment_lines()
        # Patterns that would exclude ALL dashboard source files
        forbidden = {"dashboard/", "dashboard/**", "dashboard/*", "/dashboard/"}
        offending = [ln for ln in lines if ln in forbidden]
        assert not offending, (
            f".geminiignore must not globally exclude dashboard source: {offending}. "
            "Only exclude build artifacts like dashboard/node_modules/."
        )

    def test_known_build_artifact_dirs_excluded(self):
        content = GEMINIIGNORE.read_text(encoding="utf-8")
        for artifact in ("dashboard/node_modules/", "node_modules/"):
            assert artifact in content, (
                f"Expected {artifact!r} to be excluded in .geminiignore "
                "(build artifacts should not appear in Gemini review context)"
            )


class TestGeminiIgnoreViaGitLsFiles:
    """Functional: git ls-files --exclude-from must NOT exclude dashboard sources."""

    @pytest.mark.parametrize("rel_path", DASHBOARD_SOURCE_FILES)
    def test_dashboard_source_not_excluded(self, tmp_path, rel_path):
        """A simulated dashboard source file must not be excluded by .geminiignore."""
        # Create a temp git repo so git ls-files works correctly
        result = subprocess.run(
            ["git", "init", str(tmp_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            pytest.skip("git init failed — git unavailable in test environment")

        # Place an untracked file at the dashboard source path inside the temp repo
        file_path = tmp_path / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("# placeholder\n")

        # Run git ls-files --others --ignored --exclude-from=.geminiignore
        result = subprocess.run(
            [
                "git", "ls-files",
                "--others", "--ignored",
                f"--exclude-from={GEMINIIGNORE}",
                str(file_path),
            ],
            capture_output=True, text=True,
            cwd=str(tmp_path),
            timeout=10,
        )
        assert result.returncode == 0, f"git ls-files failed: {result.stderr}"

        # If the file appears in output, it IS being excluded (bad)
        assert str(file_path) not in result.stdout and rel_path not in result.stdout, (
            f"Dashboard source file {rel_path!r} is excluded by .geminiignore. "
            "Fix: remove any broad dashboard/ pattern from .geminiignore."
        )

    @pytest.mark.parametrize("build_dir", DASHBOARD_BUILD_DIRS_EXCLUDED)
    def test_build_artifact_is_excluded(self, tmp_path, build_dir):
        """Build artifact directories SHOULD be excluded by .geminiignore."""
        result = subprocess.run(
            ["git", "init", str(tmp_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            pytest.skip("git init failed")

        # Create a fake build artifact file inside the build dir
        artifact = tmp_path / build_dir / "some_generated_file.js"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("// generated\n")

        result = subprocess.run(
            [
                "git", "ls-files",
                "--others", "--ignored",
                f"--exclude-from={GEMINIIGNORE}",
                str(artifact),
            ],
            capture_output=True, text=True,
            cwd=str(tmp_path),
            timeout=10,
        )
        if result.returncode != 0:
            pytest.skip("git ls-files failed")

        # Build artifacts SHOULD appear in excluded output
        assert str(artifact) in result.stdout or str(artifact.relative_to(tmp_path)) in result.stdout, (
            f"Expected build artifact {build_dir!r} to be excluded by .geminiignore "
            "(so large vendor dirs don't bloat the review prompt)"
        )
