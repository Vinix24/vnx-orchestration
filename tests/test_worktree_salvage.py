"""Salvage of a worktree's commits and tracked work (PR-2a).

Every test builds a real bare ``origin``, a real main checkout and real ``git
worktree`` trees under ``tmp_path`` and runs the real functions: the artifacts
are proved by cloning the bundle and applying the patches, never by
re-implementing what the module does.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import worktree_salvage as ws

# The clock is pinned so a directory name is a function of its content alone.
STAMP = "20261027T120000Z"
UNTRACKED = "untracked entries not salvaged (PR-2b)"
# What the module asks git for, spelled here to compare the artifact with the
# diff git produces for the same tree.
DIFF_FLAGS = (
    "--binary", "--no-color", "--no-ext-diff", "--no-textconv",
    "--src-prefix=a/", "--dst-prefix=b/",
)
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **GIT_ENV},
    )
    assert proc.returncode == 0, f"git {' '.join(args)} in {cwd}: {proc.stderr}"
    return proc


def git_bytes(cwd: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, env={**os.environ, **GIT_ENV},
    )
    assert proc.returncode == 0, f"git {' '.join(args)} in {cwd}: {proc.stderr!r}"
    return proc.stdout


def head_of(tree: Path) -> str:
    return git(tree, "rev-parse", "HEAD").stdout.strip()


class Fixture:
    """A bare origin, a main checkout that can hold worktrees, and a dest dir."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.origin = tmp_path / "origin.git"
        self.repo = tmp_path / "repo"
        self.dest = tmp_path / "salvage"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(tmp_path, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        (self.repo / ".gitignore").write_text(".vnx-data/\n", encoding="utf-8")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")

    def worktree(self, name: str = "worktree-one", parent: str = "one") -> Path:
        """A dispatch-style worktree on its own branch, cut from origin/main."""
        wt = self.repo / ".vnx-data" / "worktrees" / parent / name
        wt.parent.mkdir(parents=True, exist_ok=True)
        branch = f"dispatch/{parent}-{name}"
        git(self.repo, "worktree", "add", "-q", "-b", branch, str(wt), "origin/main")
        return wt

    def commit(self, wt: Path, name: str, content: str, message: str) -> str:
        (wt / name).write_text(content, encoding="utf-8")
        git(wt, "add", name)
        git(wt, "commit", "-q", "-m", message)
        return head_of(wt)


@pytest.fixture()
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    monkeypatch.setattr(ws, "_utc_stamp", lambda: STAMP)
    return Fixture(tmp_path)


def salvage(fx: Fixture, wt: Path, *, dry_run: bool = False, **kwargs) -> "ws.SalvageResult":
    return ws.salvage(wt, fx.dest, repo_root=fx.repo, dry_run=dry_run, **kwargs)


def restore(fx: Fixture, result: "ws.SalvageResult") -> Path:
    """Rebuild a tree at ``result.head`` from the artifacts alone."""
    fresh = fx.tmp / f"fresh-{result.salvage_dir.name}"
    git(fx.repo, "worktree", "add", "-q", "--detach", str(fresh), result.head)
    names = {artifact.rsplit("/", 1)[-1] for artifact in result.artifacts}
    if f"{result.name}{ws.STAGED_SUFFIX}" in names:
        git(fresh, "apply", "--index", str(result.salvage_dir / f"{result.name}{ws.STAGED_SUFFIX}"))
    if f"{result.name}{ws.PATCH_SUFFIX}" in names:
        git(fresh, "apply", str(result.salvage_dir / f"{result.name}{ws.PATCH_SUFFIX}"))
    return fresh


def suffixes(result: "ws.SalvageResult") -> list[str]:
    return [artifact.rsplit("/", 1)[-1] for artifact in result.artifacts]


# ---------------------------------------------------------------------------
# 1. commits and tracked changes, in one verified salvage
# ---------------------------------------------------------------------------


def test_commit_and_tracked_change_are_salvaged_and_verified(fx: Fixture) -> None:
    wt = fx.worktree()
    head = fx.commit(wt, "local.txt", "local\n", "worker: local work")
    (wt / "a.txt").write_text("a\nmodified\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert result.written is True
    assert (result.name, result.branch, result.head, result.unpushed) == (
        "worktree-one", "dispatch/one-worktree-one", head, 1,
    )
    assert result.untracked_pending is False
    assert result.salvage_dir == fx.dest / f"worktree-one-{STAMP}-{head[:8]}"
    assert suffixes(result) == ["worktree-one.bundle", "worktree-one.patch"]

    # The bundle carries the commit. It is thin, so a clone of the bundle needs
    # the objects of the history it excludes: --reference supplies that clone.
    bundle = result.salvage_dir / "worktree-one.bundle"
    clone = fx.tmp / "clone-from-bundle"
    git(fx.tmp, "clone", "-q", "--reference", str(fx.repo), str(bundle), str(clone))
    assert git(clone, "log", "-1", "--format=%s", head).stdout.strip() == "worker: local work"
    # And it restores into a checkout that has origin's history.
    restored = fx.tmp / "clone-of-origin"
    git(fx.tmp, "clone", "-q", str(fx.origin), str(restored))
    git(restored, "fetch", "-q", str(bundle), "HEAD")
    assert git(restored, "rev-parse", "FETCH_HEAD").stdout.strip() == head

    # The worktree patch applies on head and carries the edit.
    pristine = fx.tmp / "pristine"
    git(fx.repo, "worktree", "add", "-q", "--detach", str(pristine), head)
    git(pristine, "apply", str(result.salvage_dir / "worktree-one.patch"))
    assert (pristine / "a.txt").read_text(encoding="utf-8") == "a\nmodified\n"

    manifest = (fx.dest / ws.MANIFEST_NAME).read_text(encoding="utf-8")
    pattern = (
        rf"worktree-one \| dispatch/one-worktree-one \| head={head} "
        rf"\| niet-op-remote=1 \| "
        rf"(?P<dir>worktree-one-\d{{8}}T\d{{6}}Z-{head[:8]})/worktree-one\.bundle "
        rf"(?P=dir)/worktree-one\.patch \n"
    )
    assert re.fullmatch(pattern, manifest), manifest
    assert manifest.split(" | ")[-1].startswith(result.salvage_dir.name + "/")
    assert ws.verify(result, fx.dest, repo_root=fx.repo) == []


# ---------------------------------------------------------------------------
# 2. round 1: the index and the working tree are salvaged separately
# ---------------------------------------------------------------------------


def test_staged_change_the_tree_undoes_is_salvaged_as_its_own_patch(fx: Fixture) -> None:
    """The codex reproduction: the index holds a change the tree undoes (MM)."""
    wt = fx.worktree()
    (wt / "a.txt").write_text("a\nstaged\n", encoding="utf-8")
    git(wt, "add", "a.txt")
    (wt / "a.txt").write_text("a\n", encoding="utf-8")
    assert git(wt, "status", "--porcelain=v1").stdout.strip() == "MM a.txt"
    assert git(wt, "diff", "HEAD").stdout == "", "one HEAD-to-worktree diff loses this content"

    result = salvage(fx, wt)

    assert suffixes(result) == ["worktree-one.staged.patch", "worktree-one.patch"]
    assert ws.verify(result, fx.dest, repo_root=fx.repo) == []
    fresh = fx.tmp / "fresh-head"
    git(fx.repo, "worktree", "add", "-q", "--detach", str(fresh), result.head)
    git(fresh, "apply", "--index", str(result.salvage_dir / "worktree-one.staged.patch"))
    assert git(fresh, "show", ":a.txt").stdout == "a\nstaged\n"


def _staged_rename(wt: Path) -> None:
    git(wt, "mv", "a.txt", "moved.txt")
    (wt / "moved.txt").write_text("a\nafter the move\n", encoding="utf-8")


def _staged_deletion_restored_in_tree(wt: Path) -> None:
    git(wt, "rm", "-q", "a.txt")
    (wt / "a.txt").write_text("a\n", encoding="utf-8")


def _staged_binary_changed_in_tree(wt: Path) -> None:
    (wt / "blob.bin").write_bytes(bytes(range(256)))
    git(wt, "add", "blob.bin")
    (wt / "blob.bin").write_bytes(bytes(reversed(range(256))))


@pytest.mark.parametrize(("change", "status", "expected", "problems"), [
    (_staged_rename, "RM a.txt -> moved.txt", [".staged.patch", ".patch"], []),
    # The restored copy is untracked work, which PR-2b owns: it may never
    # verify clean here, however complete the tracked patches are.
    (_staged_deletion_restored_in_tree, "D  a.txt\n?? a.txt", [".staged.patch"], [UNTRACKED]),
    (_staged_binary_changed_in_tree, "AM blob.bin", [".staged.patch", ".patch"], []),
])
def test_staged_and_unstaged_work_are_carried_separately(
    fx: Fixture, change, status: str, expected: list[str], problems: list[str],
) -> None:
    wt = fx.worktree()
    change(wt)
    assert git(wt, "status", "--porcelain=v1").stdout.strip() == status

    result = salvage(fx, wt)

    assert suffixes(result) == [f"worktree-one{suffix}" for suffix in expected]
    assert ws.verify(result, fx.dest, repo_root=fx.repo) == problems
    assert (result.salvage_dir / f"worktree-one{ws.STAGED_SUFFIX}").stat().st_size > 0
    restored = restore(fx, result)
    if f"worktree-one{ws.PATCH_SUFFIX}" in suffixes(result):
        assert git(restored, "status", "--porcelain=v1").stdout.strip() == status
    assert not (restored / "a.txt").exists() or (restored / "a.txt").read_text() == "a\n"


# ---------------------------------------------------------------------------
# 3. round 3: one unique directory per salvage
# ---------------------------------------------------------------------------


def test_two_worktrees_with_the_same_basename_both_keep_their_artifacts(fx: Fixture) -> None:
    first = fx.worktree(name="worktree-one", parent="alpha")
    second = fx.worktree(name="worktree-one", parent="beta")
    (first / "a.txt").write_text("a\nfirst edit\n", encoding="utf-8")
    (second / "a.txt").write_text("a\nsecond edit\n", encoding="utf-8")

    one = salvage(fx, first)
    two = salvage(fx, second)

    assert one.salvage_dir != two.salvage_dir
    assert two.salvage_dir.name == f"{one.salvage_dir.name}-2"
    assert ws.verify(one, fx.dest, repo_root=fx.repo) == []
    assert ws.verify(two, fx.dest, repo_root=fx.repo) == []
    for result, tree, text in ((one, first, "first edit"), (two, second, "second edit")):
        patch = result.salvage_dir / "worktree-one.patch"
        # Byte-identical to the diff git produces for that tree, still intact
        # after the other salvage ran.
        assert patch.read_bytes() == git_bytes(tree, "diff", *DIFF_FLAGS)
        assert text in patch.read_text(encoding="utf-8")

    lines = (fx.dest / ws.MANIFEST_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert one.salvage_dir.name in lines[0] and two.salvage_dir.name not in lines[0]
    assert two.salvage_dir.name in lines[1]


def test_a_file_at_the_target_path_is_never_overwritten(fx: Fixture) -> None:
    wt = fx.worktree()
    head = head_of(wt)
    fx.dest.mkdir(parents=True)
    planted = fx.dest / f"worktree-one-{STAMP}-{head[:8]}"
    planted.write_text("do not overwrite\n", encoding="utf-8")
    (wt / "a.txt").write_text("a\nedit\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert planted.read_text(encoding="utf-8") == "do not overwrite\n"
    assert result.salvage_dir.name == f"{planted.name}-2"
    assert (result.salvage_dir / "worktree-one.patch").is_file()
    assert ws.verify(result, fx.dest, repo_root=fx.repo) == []


# ---------------------------------------------------------------------------
# 4-5. untracked work is out of scope; a clean tree still gets its line
# ---------------------------------------------------------------------------


def test_untracked_work_is_reported_and_never_verifies_clean(fx: Fixture) -> None:
    wt = fx.worktree()
    (wt / "scratch").mkdir()
    (wt / "scratch" / "notes.txt").write_text("notes\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert result.untracked_pending is True
    assert result.artifacts == []
    assert ws.verify(result, fx.dest, repo_root=fx.repo) == [UNTRACKED]


def test_a_clean_pushed_tree_salvages_only_its_manifest_line(fx: Fixture) -> None:
    wt = fx.worktree(name="worktree-clean", parent="clean")
    head = head_of(wt)

    result = salvage(fx, wt)

    assert result.artifacts == []
    assert result.unpushed == 0 and result.written is True
    assert result.manifest_line == (
        f"worktree-clean | dispatch/clean-worktree-clean | head={head} | niet-op-remote=0 | "
    )
    assert (fx.dest / ws.MANIFEST_NAME).read_text(encoding="utf-8") == result.manifest_line + "\n"
    assert sorted(p.name for p in fx.dest.iterdir()) == sorted(
        [ws.MANIFEST_NAME, result.salvage_dir.name]
    )
    assert ws.verify(result, fx.dest, repo_root=fx.repo) == []


# ---------------------------------------------------------------------------
# 6. dry run, and verify's evidence checks
# ---------------------------------------------------------------------------


def test_dry_run_computes_everything_and_writes_nothing(fx: Fixture) -> None:
    wt = fx.worktree()
    fx.commit(wt, "local.txt", "local\n", "worker: local work")
    (wt / "a.txt").write_text("a\nedit\n", encoding="utf-8")

    result = salvage(fx, wt, dry_run=True)

    assert result.written is False
    assert suffixes(result) == ["worktree-one.bundle", "worktree-one.patch"]
    assert result.manifest_line.startswith("worktree-one | dispatch/one-worktree-one | head=")
    assert result.manifest_line.endswith("worktree-one.patch ")
    assert not fx.dest.exists()


def test_verify_names_an_empty_then_a_missing_artifact(fx: Fixture) -> None:
    wt = fx.worktree()
    (wt / "a.txt").write_text("a\nedit\n", encoding="utf-8")
    result = salvage(fx, wt)
    patch = result.salvage_dir / "worktree-one.patch"
    assert ws.verify(result, fx.dest, repo_root=fx.repo) == []

    patch.write_bytes(b"")
    problems = ws.verify(result, fx.dest, repo_root=fx.repo)
    assert len(problems) == 1 and "worktree-one.patch: empty" in problems[0], problems

    patch.unlink()
    problems = ws.verify(result, fx.dest, repo_root=fx.repo)
    assert len(problems) == 1 and "worktree-one.patch: missing" in problems[0], problems


def test_verify_without_a_usable_repo_root_stays_blocking(fx: Fixture) -> None:
    wt = fx.worktree()
    (wt / "a.txt").write_text("a\nedit\n", encoding="utf-8")
    result = salvage(fx, wt)

    for repo_root in (None, fx.tmp / "not-a-checkout"):
        problems = ws.verify(result, fx.dest, repo_root=repo_root)
        assert problems and "no usable repo_root" in problems[0], problems


# ---------------------------------------------------------------------------
# Fail-closed: a git failure or an unmergeable index is never a clean salvage
# ---------------------------------------------------------------------------


def test_a_directory_that_is_no_git_worktree_is_a_salvage_error(fx: Fixture) -> None:
    plain = fx.tmp / "plain"
    plain.mkdir()
    with pytest.raises(ws.SalvageError):
        salvage(fx, plain)


def test_a_repo_root_of_another_repository_is_a_salvage_error(fx: Fixture) -> None:
    other = fx.tmp / "other"
    git(fx.tmp, "init", "-q", str(other))
    wt = fx.worktree()

    with pytest.raises(ws.SalvageError, match="not a worktree of"):
        ws.salvage(wt, fx.dest, repo_root=other, dry_run=False)


def test_unmerged_paths_refuse_the_salvage_and_name_the_path(fx: Fixture) -> None:
    wt = fx.worktree()
    fx.commit(wt, "a.txt", "a\nfrom the worktree\n", "worker: edit")
    (fx.repo / "a.txt").write_text("a\nfrom main\n", encoding="utf-8")
    git(fx.repo, "add", "a.txt")
    git(fx.repo, "commit", "-q", "-m", "main moves on")
    merge = subprocess.run(
        ["git", "merge", "main"], cwd=str(wt), capture_output=True,
        env={**os.environ, **GIT_ENV},
    )
    assert merge.returncode != 0, "the merge must conflict"
    assert git(wt, "status", "--porcelain=v1").stdout.startswith("UU a.txt")

    with pytest.raises(ws.SalvageError, match="unmerged"):
        salvage(fx, wt)
