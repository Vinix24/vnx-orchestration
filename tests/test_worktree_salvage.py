"""Salvage of a dirty dispatch worktree: bundle, patch, tarball, MANIFEST line.

Every test builds a real bare ``origin``, a real main checkout and a real
``git worktree`` under ``tmp_path`` and runs the real functions.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import worktree_salvage as ws

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **GIT_ENV, **(env or {})},
    )
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def snapshot(tree: Path) -> tuple[str, str, str]:
    """Tree id of the index, tree id of the whole working tree (untracked
    included, via a scratch index) and the porcelain status."""
    scratch = {"GIT_INDEX_FILE": str(tree.parent / f".{tree.name}.snapshot-index")}
    git(tree, "add", "-A", env=scratch)
    return (
        git(tree, "write-tree"), git(tree, "write-tree", env=scratch),
        git(tree, "status", "--porcelain=v1"),
    )


def restore(fx: "Fixture", result: ws.SalvageResult) -> Path:
    """Rebuild a tree at ``result.head`` from the artifacts alone: the staged
    patch into index and tree, the worktree patch into the tree, then the tarball."""
    fresh = fx.tmp / f"fresh-{result.name}"
    git(fx.repo, "worktree", "add", "-q", "--detach", str(fresh), result.head)
    for suffix, flags in ((".staged.patch", ["--index"]), (".patch", [])):
        if f"{result.name}{suffix}" in result.artifacts:
            git(fresh, "apply", *flags, str(fx.dest / f"{result.name}{suffix}"))
    if f"{result.name}.untracked.tgz" in result.artifacts:
        with tarfile.open(fx.dest / f"{result.name}.untracked.tgz") as tar:
            tar.extractall(fresh, filter="data")
    return fresh


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.origin = tmp_path / "origin.git"
        self.repo = tmp_path / "repo"
        self.dest = self.repo / ".vnx-data" / "salvage" / "run"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(tmp_path, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        (self.repo / ".gitignore").write_text(".vnx-data/\n", encoding="utf-8")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")

    def dirty_worktree(self) -> tuple[Path, str]:
        """One local commit, one modified tracked file, one untracked file,
        one internal ``.venv`` convenience symlink."""
        wt = self.repo / ".vnx-data" / "worktrees" / "worktree-one"
        git(self.repo, "worktree", "add", "-q", "-b", "dispatch/one", str(wt), "origin/main")
        (wt / "local.txt").write_text("local\n", encoding="utf-8")
        git(wt, "add", "local.txt")
        git(wt, "commit", "-q", "-m", "local work")
        head = git(wt, "rev-parse", "HEAD")
        (wt / "a.txt").write_text("a\nmodified\n", encoding="utf-8")
        (wt / "scratch").mkdir()
        (wt / "scratch" / "notes.txt").write_text("notes\n", encoding="utf-8")
        (wt / ".venv").symlink_to(self.repo / ".venv")
        return wt, head

    def clean_worktree(self) -> Path:
        wt = self.repo / ".vnx-data" / "worktrees" / "worktree-clean"
        git(self.repo, "worktree", "add", "-q", "-b", "dispatch/clean", str(wt), "origin/main")
        return wt


@pytest.fixture()
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_real_salvage_writes_bundle_patch_tarball_and_manifest(fx: Fixture) -> None:
    wt, head = fx.dirty_worktree()
    result = ws.salvage(wt, fx.dest, repo_root=fx.repo, dry_run=False)

    assert result.written is True
    assert (result.name, result.branch, result.head, result.unpushed) == (
        "worktree-one", "dispatch/one", head, 1,
    )
    assert result.artifacts == [
        "worktree-one.bundle", "worktree-one.patch", "worktree-one.untracked.tgz",
    ]

    # The bundle carries the local commit; a thin bundle restores by fetch.
    clone = fx.tmp / "clone"
    git(fx.tmp, "clone", "-q", str(fx.origin), str(clone))
    git(clone, "fetch", "-q", str(fx.dest / "worktree-one.bundle"), "HEAD")
    assert git(clone, "rev-parse", "FETCH_HEAD") == head

    # The patch applies to a pristine checkout of head.
    pristine = fx.tmp / "pristine"
    git(fx.repo, "worktree", "add", "-q", "--detach", str(pristine), head)
    git(pristine, "apply", "--check", str(fx.dest / "worktree-one.patch"))

    # The tarball holds the untracked work and skips the internal symlink.
    with tarfile.open(fx.dest / "worktree-one.untracked.tgz") as tar:
        names = tar.getnames()
    assert "scratch/notes.txt" in names
    assert ".venv" not in names

    manifest = (fx.dest / "MANIFEST.txt").read_text(encoding="utf-8")
    pattern = (
        rf"worktree-one \| dispatch/one \| head={re.escape(head)} "
        rf"\| niet-op-remote=1 \| worktree-one\.bundle worktree-one\.patch "
        rf"worktree-one\.untracked\.tgz \n"
    )
    assert re.fullmatch(pattern, manifest), manifest


def test_clean_pushed_tree_has_no_artifacts_and_zero_count(fx: Fixture) -> None:
    wt = fx.clean_worktree()
    result = ws.salvage(wt, fx.dest, repo_root=fx.repo, dry_run=False)

    head = git(wt, "rev-parse", "HEAD")
    assert result.artifacts == []
    assert result.unpushed == 0
    assert result.written is True
    assert result.manifest_line == (
        f"worktree-clean | dispatch/clean | head={head} | niet-op-remote=0 | "
    )
    assert (fx.dest / "MANIFEST.txt").read_text(encoding="utf-8") == result.manifest_line + "\n"
    assert sorted(p.name for p in fx.dest.iterdir()) == ["MANIFEST.txt"]


def test_dry_run_computes_everything_and_writes_nothing(fx: Fixture) -> None:
    wt, _head = fx.dirty_worktree()
    result = ws.salvage(wt, fx.dest, repo_root=fx.repo)

    assert result.written is False
    assert result.artifacts == [
        "worktree-one.bundle", "worktree-one.patch", "worktree-one.untracked.tgz",
    ]
    assert result.manifest_line.endswith("worktree-one.untracked.tgz ")
    assert not fx.dest.exists()


def test_verify_passes_then_names_empty_and_missing_artifacts(fx: Fixture) -> None:
    wt, _head = fx.dirty_worktree()
    result = ws.salvage(wt, fx.dest, repo_root=fx.repo, dry_run=False)

    assert ws.verify(result, fx.dest) == []

    (fx.dest / "worktree-one.bundle").write_bytes(b"")
    problems = ws.verify(result, fx.dest)
    assert len(problems) == 1
    assert "worktree-one.bundle" in problems[0]

    (fx.dest / "worktree-one.patch").unlink()
    problems = ws.verify(result, fx.dest)
    assert len(problems) == 2
    assert any("worktree-one.patch" in problem for problem in problems)


def stage_then_undo_in_tree(wt: Path) -> None:
    """The codex reproduction: the index holds a change the tree undoes (MM)."""
    (wt / "a.txt").write_text("a\nstaged\n", encoding="utf-8")
    git(wt, "add", "a.txt")
    (wt / "a.txt").write_text("a\n", encoding="utf-8")


def test_staged_change_the_tree_undoes_is_salvaged_and_restores(fx: Fixture) -> None:
    wt = fx.clean_worktree()
    stage_then_undo_in_tree(wt)
    assert git(wt, "status", "--porcelain=v1") == "MM a.txt"

    result = ws.salvage(wt, fx.dest, repo_root=fx.repo, dry_run=False)

    assert result.artifacts == ["worktree-clean.staged.patch", "worktree-clean.patch"]
    assert ws.verify(result, fx.dest, repo_root=fx.repo) == []
    fresh = restore(fx, result)
    assert git(fresh, "show", ":a.txt") == "a\nstaged"
    assert snapshot(fresh) == snapshot(wt)


def staged_rename(wt: Path) -> None:
    git(wt, "mv", "a.txt", "moved.txt")
    (wt / "moved.txt").write_text("a\nafter the move\n", encoding="utf-8")


def staged_deletion_restored_in_tree(wt: Path) -> None:
    git(wt, "rm", "-q", "a.txt")
    (wt / "a.txt").write_text("a\n", encoding="utf-8")


def staged_binary_changed_in_tree(wt: Path) -> None:
    (wt / "blob.bin").write_bytes(bytes(range(256)))
    git(wt, "add", "blob.bin")
    (wt / "blob.bin").write_bytes(bytes(reversed(range(256))))


@pytest.mark.parametrize(("change", "status", "suffixes"), [
    (staged_rename, "RM a.txt -> moved.txt", [".staged.patch", ".patch"]),
    (staged_deletion_restored_in_tree, "D  a.txt\n?? a.txt", [".staged.patch", ".untracked.tgz"]),
    (staged_binary_changed_in_tree, "AM blob.bin", [".staged.patch", ".patch"]),
])
def test_index_and_tree_restore_from_the_artifacts(fx: Fixture, change, status, suffixes) -> None:
    wt = fx.clean_worktree()
    change(wt)
    assert git(wt, "status", "--porcelain=v1") == status

    result = ws.salvage(wt, fx.dest, repo_root=fx.repo, dry_run=False)

    assert result.artifacts == [f"worktree-clean{suffix}" for suffix in suffixes]
    objects = git(fx.repo, "count-objects")
    assert ws.verify(result, fx.dest, repo_root=fx.repo) == []
    assert git(fx.repo, "count-objects") == objects  # verify wrote no blob
    assert snapshot(restore(fx, result)) == snapshot(wt)


def test_verify_names_a_patch_that_does_not_apply_in_order(fx: Fixture, tmp_path: Path) -> None:
    wt = fx.clean_worktree()
    stage_then_undo_in_tree(wt)
    result = ws.salvage(wt, fx.dest, repo_root=fx.repo, dry_run=False)
    staged = fx.dest / "worktree-clean.staged.patch"
    tree = fx.dest / "worktree-clean.patch"
    staged_bytes, tree_bytes = staged.read_bytes(), tree.read_bytes()

    staged.write_bytes(tree_bytes)  # a tree-side patch does not apply to head
    problems = ws.verify(result, fx.dest, repo_root=fx.repo)
    assert len(problems) == 1 and "worktree-clean.staged.patch" in problems[0]

    staged.write_bytes(staged_bytes)
    tree.write_bytes(staged_bytes)  # the staged change applied twice
    problems = ws.verify(result, fx.dest, repo_root=fx.repo)
    assert len(problems) == 1 and "worktree-clean.patch:" in problems[0]

    # No repository to apply against is a problem, never a clean result.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "worktree-clean.staged.patch").write_bytes(staged_bytes)
    (outside / "worktree-clean.patch").write_bytes(tree_bytes)
    problems = ws.verify(result, outside)
    assert len(problems) == 1 and "no repository" in problems[0]


def test_salvage_refuses_unmerged_paths(fx: Fixture) -> None:
    wt = fx.clean_worktree()
    git(wt, "checkout", "-q", "-b", "theirs")
    (wt / "a.txt").write_text("theirs\n", encoding="utf-8")
    git(wt, "commit", "-q", "-am", "theirs")
    git(wt, "checkout", "-q", "dispatch/clean")
    (wt / "a.txt").write_text("ours\n", encoding="utf-8")
    git(wt, "commit", "-q", "-am", "ours")
    merge = subprocess.run(
        ["git", "merge", "-q", "theirs"], cwd=str(wt), capture_output=True,
        env={**os.environ, **GIT_ENV},
    )
    assert merge.returncode == 1
    assert git(wt, "status", "--porcelain=v1") == "UU a.txt"

    with pytest.raises(ws.SalvageError, match="unmerged paths no patch can carry: a.txt"):
        ws.salvage(wt, fx.dest, repo_root=fx.repo)


def test_salvage_refuses_a_status_change_no_diff_carries(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = fx.clean_worktree()
    (wt / "a.txt").write_text("changed\n", encoding="utf-8")
    real_git = ws._run_git
    monkeypatch.setattr(
        ws, "_run_git",
        lambda args, cwd, what: b"" if args[0] == "diff" else real_git(args, cwd, what),
    )
    with pytest.raises(ws.SalvageError, match="1 tracked change"):
        ws.salvage(wt, fx.dest, repo_root=fx.repo)


def test_salvage_refuses_a_path_that_is_not_a_git_worktree(fx: Fixture, tmp_path: Path) -> None:
    plain = tmp_path / "not-a-worktree"
    plain.mkdir()
    with pytest.raises(ws.SalvageError) as excinfo:
        ws.salvage(plain, fx.dest, repo_root=fx.repo, dry_run=True)
    assert "git" in str(excinfo.value)
