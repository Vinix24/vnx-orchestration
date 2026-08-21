"""test_worktree_release_repo_root.py — PROJECT_ROOT vs cwd repo-root conflict
guard (OI-1389 fix-forward on PR #1649).

Regression coverage for the defect where a stale PROJECT_ROOT silently won
over cwd in ``_resolve_repo_root``, so ``--apply`` could unlock and remove
worktrees in the wrong repository with no warning. See
``worktree_release._resolve_repo_root`` and ``RepoRootConflictError``.

Uses real throwaway git repos under tmp_path, mirroring test_worktree_release.py.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from worktree_release import (
    EXIT_REPO_ROOT_CONFLICT,
    RepoRootConflictError,
    _resolve_repo_root,
    main,
)


def _init_plain_repo(path: Path) -> Path:
    """Create a minimal real git repo with one commit. Returns the resolved root."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(path)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.local"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("init\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    return path.resolve()


# ---------------------------------------------------------------------------
# _resolve_repo_root — unit-level conflict detection
# ---------------------------------------------------------------------------

def test_resolve_repo_root_no_conflict_when_paths_match(tmp_path, monkeypatch):
    """PROJECT_ROOT and cwd pointing at the same repo: unchanged behavior."""
    repo = _init_plain_repo(tmp_path / "repo-a")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PROJECT_ROOT", str(repo))

    result = _resolve_repo_root(dry_run=False)

    assert result == repo


def test_resolve_repo_root_conflict_refuses_on_apply(tmp_path, monkeypatch):
    """PROJECT_ROOT and cwd naming DIFFERENT repos, apply mode: hard refusal."""
    repo_a = _init_plain_repo(tmp_path / "repo-a")
    repo_b = _init_plain_repo(tmp_path / "repo-b")
    monkeypatch.chdir(repo_a)
    monkeypatch.setenv("PROJECT_ROOT", str(repo_b))

    with pytest.raises(RepoRootConflictError) as excinfo:
        _resolve_repo_root(dry_run=False)

    exc = excinfo.value
    assert exc.env_root == repo_b
    assert exc.cwd_root == repo_a
    # The message must name BOTH paths literally — that's the whole point.
    assert str(repo_a) in str(exc)
    assert str(repo_b) in str(exc)


def test_resolve_repo_root_conflict_warns_and_proceeds_on_dry_run(
    tmp_path, monkeypatch, caplog,
):
    """Same conflict, dry-run mode: proceeds (unchanged) but warns loudly."""
    repo_a = _init_plain_repo(tmp_path / "repo-a")
    repo_b = _init_plain_repo(tmp_path / "repo-b")
    monkeypatch.chdir(repo_a)
    monkeypatch.setenv("PROJECT_ROOT", str(repo_b))

    with caplog.at_level(logging.WARNING, logger="worktree_release"):
        result = _resolve_repo_root(dry_run=True)

    # PROJECT_ROOT still wins for a dry-run — no behavior change there.
    assert result == repo_b
    # But the disagreement is surfaced with both paths before --apply is typed.
    assert str(repo_a) in caplog.text
    assert str(repo_b) in caplog.text


def test_resolve_repo_root_no_conflict_when_cwd_has_no_git_root(tmp_path, monkeypatch):
    """cwd isn't inside any git repo: PROJECT_ROOT wins, no conflict raised."""
    repo_b = _init_plain_repo(tmp_path / "repo-b")
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    monkeypatch.chdir(non_repo)
    monkeypatch.setenv("PROJECT_ROOT", str(repo_b))

    result = _resolve_repo_root(dry_run=False)

    assert result == repo_b


def test_resolve_repo_root_symlink_is_not_a_false_conflict(tmp_path, monkeypatch):
    """A symlinked path to the SAME repo must not register as a conflict.

    Comparison must happen on resolved, normalized paths — otherwise e.g.
    macOS's /tmp -> /private/tmp symlink would fire a false-positive refusal.
    """
    repo = _init_plain_repo(tmp_path / "real-repo")
    symlink_path = tmp_path / "repo-symlink"
    symlink_path.symlink_to(repo, target_is_directory=True)

    monkeypatch.chdir(symlink_path)
    monkeypatch.setenv("PROJECT_ROOT", str(repo))

    result = _resolve_repo_root(dry_run=False)

    assert result == repo


# ---------------------------------------------------------------------------
# main() — CLI-level exit code and explicit --repo-root precedence
# ---------------------------------------------------------------------------

def test_main_cli_refuses_apply_on_conflict(tmp_path, monkeypatch, capsys):
    """`--apply` with a PROJECT_ROOT/cwd conflict exits EXIT_REPO_ROOT_CONFLICT."""
    repo_a = _init_plain_repo(tmp_path / "repo-a")
    repo_b = _init_plain_repo(tmp_path / "repo-b")
    monkeypatch.chdir(repo_a)
    monkeypatch.setenv("PROJECT_ROOT", str(repo_b))

    rc = main(["--apply"])

    assert rc == EXIT_REPO_ROOT_CONFLICT
    captured = capsys.readouterr()
    assert str(repo_a) in captured.err
    assert str(repo_b) in captured.err


def test_main_cli_explicit_repo_root_wins_over_conflict(tmp_path, monkeypatch, capsys):
    """An explicit --repo-root is the operator's answer — refusal never fires."""
    repo_a = _init_plain_repo(tmp_path / "repo-a")
    repo_b = _init_plain_repo(tmp_path / "repo-b")
    monkeypatch.chdir(repo_a)
    monkeypatch.setenv("PROJECT_ROOT", str(repo_b))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "vnxdata"))

    rc = main(["--apply", "--repo-root", str(repo_a)])

    assert rc == 0
    captured = capsys.readouterr()
    assert "ERROR" not in captured.err
