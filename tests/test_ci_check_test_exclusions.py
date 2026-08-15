"""Tests for scripts/ci/check_test_exclusions.py.

The gate enforces that every entry in scripts/ci/test_exclusions.txt carries
an inline reason, resolves to a real file, and is not duplicated. Each test
builds a minimal repo layout with a fake exclusions file and asserts the
check's exit code and output.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


_CHECK_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "ci" / "check_test_exclusions.py"
)


def _write_repo(repo: Path, exclusions_text: str) -> None:
    """Create a minimal repo with a scripts/ci/test_exclusions.txt."""
    (repo / "scripts" / "ci").mkdir(parents=True)
    (repo / "scripts" / "ci" / "test_exclusions.txt").write_text(
        exclusions_text, encoding="utf-8"
    )
    # A real test file the entries can point at.
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text("def test_ok(): pass\n", encoding="utf-8")


def _run_check(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CHECK_SCRIPT), str(repo)],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={**os.environ, "PYTHONPATH": str(repo)},
    )


def test_entry_without_reason_is_flagged(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo(repo, "tests/test_ok.py\n")

    result = _run_check(repo)
    assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "missing exclusion reason" in result.stdout
    assert "test_ok.py" in result.stdout


def test_entry_with_reason_passes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo(repo, "tests/test_ok.py  # OI-999: red on main, repair tracked\n")

    result = _run_check(repo)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "PASS" in result.stdout


def test_stale_path_is_flagged(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo(
        repo,
        "tests/test_deleted.py  # OI-999: was red on main, now repaired and gone\n",
    )

    result = _run_check(repo)
    assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "path does not exist" in result.stdout


def test_duplicate_entry_is_flagged(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo(
        repo,
        "tests/test_ok.py  # OI-999: first listing\n"
        "tests/test_ok.py  # OI-999: second listing\n",
    )

    result = _run_check(repo)
    assert result.returncode == 1, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "duplicate entry" in result.stdout


def test_header_comments_and_blanks_are_ignored(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo(
        repo,
        "# Full-suite exclusions.\n"
        "# Each entry cites its tracking item.\n"
        "\n"
        "tests/test_ok.py  # OI-999: documented reason\n"
        "\n",
    )

    result = _run_check(repo)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_missing_exclusions_file_skips(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = _run_check(repo)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "skip" in result.stdout


def test_local_ci_registers_test_exclusion_reason_gate():
    """The gate must stay registered in local-ci.sh or a bare exclusion goes silent."""
    repo_root = Path(__file__).resolve().parent.parent
    script = (repo_root / "scripts" / "local-ci.sh").read_text(encoding="utf-8")
    assert 'run_gate "test-exclusion-reason"' in script
    assert "check_test_exclusions.py" in script


def test_current_tree_is_green():
    """The check must pass on the current (clean) tree — every exclusion has a reason."""
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(_CHECK_SCRIPT), str(repo_root)],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env={**os.environ, "PYTHONPATH": str(repo_root)},
    )
    assert result.returncode == 0, (
        f"Check failed on current tree:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "PASS" in result.stdout
