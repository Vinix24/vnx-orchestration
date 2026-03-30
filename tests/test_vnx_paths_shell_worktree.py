#!/usr/bin/env python3
"""Shell-level regression tests for vnx_paths.sh worktree resolution."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _copy_repo(src: Path, dest: Path) -> None:
    shutil.copytree(
        src,
        dest,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", ".vnx-data", ".vnx-intelligence", "__pycache__"),
    )


def _init_git_repo(repo_root: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_root, check=True, capture_output=True)


def test_shell_resolver_keeps_runtime_local_and_intelligence_canonical(tmp_path):
    project_root = tmp_path / "project"
    canonical_root = project_root / ".claude" / "vnx-system"
    _copy_repo(REPO_ROOT, canonical_root)
    _init_git_repo(canonical_root)

    worktree_root = tmp_path / "vnx-system-wt-upgrade"
    subprocess.run(
        ["git", "-C", str(canonical_root), "worktree", "add", "-b", "feature/test-shell-paths", str(worktree_root)],
        check=True,
        capture_output=True,
    )

    env = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "TERM": os.environ.get("TERM", "xterm-256color"),
    }
    script = """
set -euo pipefail
cd "$1"
source scripts/lib/vnx_paths.sh
printf 'PROJECT_ROOT=%s\n' "$PROJECT_ROOT"
printf 'VNX_CANONICAL_ROOT=%s\n' "$VNX_CANONICAL_ROOT"
printf 'VNX_DATA_DIR=%s\n' "$VNX_DATA_DIR"
printf 'VNX_INTELLIGENCE_DIR=%s\n' "$VNX_INTELLIGENCE_DIR"
"""
    result = subprocess.run(
        ["bash", "-lc", script, "bash", str(worktree_root)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    lines = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
    assert Path(lines["PROJECT_ROOT"]).resolve() == worktree_root.resolve()
    assert Path(lines["VNX_CANONICAL_ROOT"]).resolve() == canonical_root.resolve()
    assert Path(lines["VNX_DATA_DIR"]).resolve() == (worktree_root / ".vnx-data").resolve()
    assert Path(lines["VNX_INTELLIGENCE_DIR"]).resolve() == (canonical_root / ".vnx-intelligence").resolve()
