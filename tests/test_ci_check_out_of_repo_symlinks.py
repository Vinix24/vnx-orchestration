"""Tests for scripts/ci/check_out_of_repo_symlinks.py.

Each test creates a temporary git repository, sets up the scenario, runs the
check script via subprocess, and asserts the expected outcome.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


_CHECK_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "ci" / "check_out_of_repo_symlinks.py"
)


def _run_check(repo_dir: Path) -> subprocess.CompletedProcess:
    """Run the check script inside *repo_dir* and return the CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(_CHECK_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
        env={**os.environ, "PYTHONPATH": str(repo_dir)},
    )


def _git_init(repo_dir: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(repo_dir), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_dir), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo_dir), check=True, capture_output=True,
    )


def _git_add_and_commit(repo_dir: Path, message: str = "initial") -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo_dir), check=True, capture_output=True,
    )


def _git_add_specific(repo_dir: Path, *paths: str) -> None:
    subprocess.run(
        ["git", "add", *paths],
        cwd=str(repo_dir), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add select files"],
        cwd=str(repo_dir), check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Signal 1 — tracked symlinks
# ---------------------------------------------------------------------------


def test_tracked_symlink_outside_detected(tmp_path: Path):
    """A tracked symlink pointing outside the repo is flagged."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    outside = tmp_path / "outside_target.py"
    outside.write_text("external code")

    symlink = repo / "external_link.py"
    symlink.symlink_to(outside)

    _git_add_and_commit(repo)

    result = _run_check(repo)
    assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Signal 1" in result.stdout
    assert "external_link.py" in result.stdout


def test_tracked_symlink_inside_allowed(tmp_path: Path):
    """A tracked symlink pointing inside the repo is NOT flagged."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    (repo / "target.py").write_text("real code")
    symlink = repo / "internal_link.py"
    symlink.symlink_to(repo / "target.py")

    _git_add_and_commit(repo)

    result = _run_check(repo)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "OK" in result.stdout


def test_broken_symlink_outside_still_detected(tmp_path: Path):
    """A broken symlink whose target is outside the repo is STILL flagged.

    Even though the target doesn't exist on disk, the symlink resolves to a
    path outside the repo.  On a clean checkout the target would also be
    absent — the scenario this check exists to prevent.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    nowhere = tmp_path / "does_not_exist"
    symlink = repo / "broken_link.py"
    symlink.symlink_to(nowhere)

    _git_add_and_commit(repo)

    result = _run_check(repo)
    assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Signal 1" in result.stdout


def test_relative_outside_symlink_detected(tmp_path: Path):
    """A tracked relative symlink that escapes via '../' is flagged."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    outside = tmp_path / "outside_target.py"
    outside.write_text("external code")

    symlink = repo / "subdir" / "external_link.py"
    symlink.parent.mkdir()
    # ../.. from subdir/ goes to tmp_path, then /outside_target.py
    symlink.symlink_to(Path("..") / ".." / outside.name)

    _git_add_and_commit(repo)

    result = _run_check(repo)
    assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Signal 1" in result.stdout


def test_relative_inside_symlink_allowed(tmp_path: Path):
    """A tracked relative symlink that stays within the repo is allowed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    (repo / "subdir").mkdir()
    (repo / "subdir" / "target.py").write_text("real code")

    symlink = repo / "internal_link.py"
    symlink.symlink_to(Path("subdir") / "target.py")

    _git_add_and_commit(repo)

    result = _run_check(repo)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Signal 2 — working-tree symlinks
# ---------------------------------------------------------------------------


def test_untracked_symlink_outside_detected(tmp_path: Path):
    """A symlink in the working tree (not tracked) pointing outside is flagged by signal 2."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    outside = tmp_path / "installed_module.py"
    outside.write_text("def work(): return 99\n")

    # Create a non-tracked symlink (simulates an install artifact).
    symlink = repo / "installed_module.py"
    symlink.symlink_to(outside)

    # Only track a dummy file — the symlink stays untracked.
    (repo / "README.md").write_text("# Test\n")
    _git_add_specific(repo, "README.md")

    result = _run_check(repo)
    assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Signal 2" in result.stdout
    assert "installed_module.py" in result.stdout


def test_untracked_symlink_inside_allowed(tmp_path: Path):
    """A working-tree symlink that stays within the repo is NOT flagged."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "target.py").write_text("real code")

    symlink = repo / "link.py"
    symlink.symlink_to(repo / "lib" / "target.py")

    (repo / "README.md").write_text("# Test\n")
    _git_add_specific(repo, "README.md", "lib/target.py")

    result = _run_check(repo)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "OK" in result.stdout


def test_untracked_symlink_not_duplicated_with_signal1(tmp_path: Path):
    """When a symlink is already caught by signal 1, signal 2 does not duplicate it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    outside = tmp_path / "external.py"
    outside.write_text("external")

    # Tracked symlink — caught by signal 1 only.
    symlink = repo / "external_link.py"
    symlink.symlink_to(outside)
    _git_add_and_commit(repo)

    result = _run_check(repo)
    assert result.returncode == 1
    assert "Signal 1" in result.stdout
    # Signal 2 should not also report the same symlink.
    # Count occurrences of the path in output — should appear once.
    assert result.stdout.count("external_link.py") == 1


def test_venv_node_modules_symlinks_skipped(tmp_path: Path):
    """Symlinks inside .venv/ and node_modules/ are skipped by signal 2.

    These directories are typically gitignored; signal 2's working-tree walk
    prunes them to avoid noise from build/packaging tooling.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    outside = tmp_path / "outside.py"
    outside.write_text("external")

    # Symlinks in skipped dirs — not tracked by git (as in real life).
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(outside)

    (repo / "node_modules" / ".bin").mkdir(parents=True)
    (repo / "node_modules" / ".bin" / "some-tool").symlink_to(outside)

    (repo / "README.md").write_text("# Test\n")
    _git_add_specific(repo, "README.md")

    result = _run_check(repo)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Gitignored paths — must not be reported
# ---------------------------------------------------------------------------


def test_gitignored_symlink_outside_not_reported(tmp_path: Path):
    """A symlink inside a gitignored directory is NOT flagged — runtime noise."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    (repo / ".gitignore").write_text(".vnx-data/\n")
    _git_add_and_commit(repo)

    outside = tmp_path / "external.py"
    outside.write_text("external")

    gitignored_dir = repo / ".vnx-data" / "worktrees" / "some-dispatch"
    gitignored_dir.mkdir(parents=True)
    symlink = gitignored_dir / "scripts" / "lib"
    symlink.parent.mkdir()
    symlink.symlink_to(outside)

    result = _run_check(repo)
    assert result.returncode == 0, (
        f"Expected exit 0 (gitignored symlink skipped), got {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_gitignored_symlink_inside_not_reported(tmp_path: Path):
    """A symlink inside a gitignored dir that points inside the repo is also skipped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    (repo / ".gitignore").write_text(".vnx-data/\n")
    _git_add_and_commit(repo)

    (repo / "real_target.py").write_text("real code")
    _git_add_specific(repo, "real_target.py")

    gitignored_dir = repo / ".vnx-data" / "worktrees" / "some-dispatch"
    gitignored_dir.mkdir(parents=True)
    symlink = gitignored_dir / "link.py"
    symlink.symlink_to(repo / "real_target.py")

    result = _run_check(repo)
    assert result.returncode == 0, (
        f"Expected exit 0 (gitignored symlink skipped), got {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_tracked_symlink_outside_still_detected_even_with_gitignore(tmp_path: Path):
    """A tracked symlink (Signal 1) that points outside is STILL detected even
    when a gitignore exists — ``git ls-files -s`` is the ground truth, not the
    filesystem walk.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    (repo / ".gitignore").write_text("*.pyc\n")
    _git_add_and_commit(repo)

    outside = tmp_path / "external.py"
    outside.write_text("external")

    symlink = repo / "external_link.py"
    symlink.symlink_to(outside)

    _git_add_and_commit(repo)

    result = _run_check(repo)
    assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Signal 1" in result.stdout
    assert "external_link.py" in result.stdout


# ---------------------------------------------------------------------------
# Current tree — no false positives
# ---------------------------------------------------------------------------


def test_current_tree_is_green():
    """The check must pass on the current (clean) tree — no false positives."""
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(_CHECK_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env={**os.environ, "PYTHONPATH": str(repo_root)},
    )
    assert result.returncode == 0, (
        f"Check failed on current tree:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
