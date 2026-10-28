"""Salvage of a worktree's commits and tracked changes (WT-2a1a/2a1b/2a2).

Every test builds a real bare ``origin``, a real main checkout and real ``git
worktree`` trees under ``tmp_path``, then runs the real functions: the bundle is
proved by cloning it, the patches by applying them in a scratch checkout of the
head, and every allowlist case by dirtying a real tree. The allowlist excuses
three kinds (WT-2a2): an IGNORED entry inside a throwaway cache DIRECTORY named
by ``THROWAWAY_IGNORED_DIRS`` (never one below ``.vnx-data/`` or ``.git/``; a
regular file that only shares a cache directory's name is not inside one), a
staged change to a tracked file and an unstaged change to a tracked file — the
latter two only because ``verify`` applies their patch and proves it reproduces
the tree. Every other status entry — untracked or unmerged, whatever its path —
is a named problem, and so is a populated submodule, which carries commits no
superproject bundle can hold.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import worktree_salvage as ws

# The clock is pinned, so a directory name is a function of its content alone.
STAMP = "20261027T120000Z"
UNCOVERED = "tree has state this salvage does not cover: "
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}
# Ignore rules of the base commit. ``.vnx-data/`` and ``__pycache__/`` are the
# WT-2a1a set; the rest give the allowlist cases a directory entry to match and
# one ignored source file (``*.log``) that is on no allowlist.
DEFAULT_IGNORES = ".vnx-data/\n__pycache__/\nnode_modules/\nbuild/\n*.log\n"


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **GIT_ENV},
    )
    assert proc.returncode == 0, f"git {' '.join(args)} in {cwd}: {proc.stderr}"
    return proc


def git_status(wt: Path) -> str:
    return git(wt, "status", "--porcelain=v1", "--untracked-files=all").stdout


def head_of(tree: Path) -> str:
    return git(tree, "rev-parse", "HEAD").stdout.strip()


class Fixture:
    """A bare origin, a main checkout that can hold worktrees, and a dest dir."""

    def __init__(self, tmp_path: Path, ignores: str = DEFAULT_IGNORES) -> None:
        self.tmp = tmp_path
        self.origin = tmp_path / "origin.git"
        self.repo = tmp_path / "repo"
        self.dest = tmp_path / "salvage"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(tmp_path, "init", "-q", "-b", "main", str(self.repo))
        self.repo_config()
        git(self.repo, "remote", "add", "origin", str(self.origin))
        (self.repo / ".gitignore").write_text(ignores, encoding="utf-8")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")

    def repo_config(self) -> None:
        """No signing, whatever the operator's global config says."""
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "config", "tag.gpgsign", "false")

    def worktree(self, name: str = "worktree-one", parent: str = "one") -> Path:
        """A dispatch-style worktree on its own branch, cut from origin/main."""
        wt = self.repo / ".vnx-data" / "worktrees" / parent / name
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "-q", "-b", f"dispatch/{parent}-{name}",
            str(wt), "origin/main")
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


@pytest.fixture()
def ignored_otherwise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fixture factory for ignore rules the default fixture does not model.

    Ignore rules decide which path git prints for a file — the whole directory
    (``! .vnx-data/``) or the file inside it (``! .vnx-data/x``) — so a case
    about the path's own components needs its own rules.
    """
    monkeypatch.setattr(ws, "_utc_stamp", lambda: STAMP)
    built: list[Fixture] = []

    def build(ignores: str) -> Fixture:
        base = tmp_path / f"fixture-{len(built)}"
        base.mkdir()
        fixture = Fixture(base, ignores)
        built.append(fixture)
        return fixture

    return build


def salvage(fx: Fixture, wt: Path, **kwargs) -> "ws.SalvageResult":
    kwargs.setdefault("dry_run", False)
    return ws.salvage(wt, fx.dest, repo_root=fx.repo, **kwargs)


def verify(fx: Fixture, result: "ws.SalvageResult") -> list[str]:
    return ws.verify(result, fx.dest, repo_root=fx.repo)


def bundle_of(result: "ws.SalvageResult") -> Path:
    return result.salvage_dir / f"{result.name}{ws.BUNDLE_SUFFIX}"


def clone_bundle(fx: Fixture, bundle: Path, name: str) -> Path:
    """A clone of a thin bundle: ``--reference`` supplies the excluded history."""
    clone = fx.tmp / name
    git(fx.tmp, "clone", "-q", "--reference", str(fx.repo), str(bundle), str(clone))
    return clone


# ---------------------------------------------------------------------------
# 1-2. the commits, and a tree that has none to salvage
# ---------------------------------------------------------------------------


def test_a_local_commit_is_bundled_cloned_and_verified(fx: Fixture) -> None:
    wt = fx.worktree()
    head = fx.commit(wt, "local.txt", "local\n", "worker: local work")

    result = salvage(fx, wt)

    assert result.written is True and result.uncovered == []
    assert (result.name, result.branch, result.head, result.unpushed) == (
        "worktree-one", "dispatch/one-worktree-one", head, 1,
    )
    assert result.salvage_dir == fx.dest / f"worktree-one-{STAMP}-{head[:8]}"
    assert result.artifacts == [f"{result.salvage_dir.name}/worktree-one.bundle"]
    assert bundle_of(result).stat().st_size > 0

    clone = clone_bundle(fx, bundle_of(result), "clone-from-bundle")
    assert git(clone, "log", "-1", "--format=%s", head).stdout.strip() == "worker: local work"

    manifest = (fx.dest / ws.MANIFEST_NAME).read_text(encoding="utf-8")
    pattern = (
        rf"worktree-one \| dispatch/one-worktree-one \| head={head} "
        rf"\| niet-op-remote=1 "
        rf"\| (worktree-one-\d{{8}}T\d{{6}}Z-{head[:8]})/worktree-one\.bundle \n"
    )
    assert re.fullmatch(pattern, manifest), manifest
    assert result.manifest_line == manifest.rstrip("\n")
    assert result.manifest_line.endswith(f"{result.salvage_dir.name}/worktree-one.bundle ")
    assert verify(fx, result) == []


def test_a_clean_pushed_tree_salvages_only_its_manifest_line(fx: Fixture) -> None:
    wt = fx.worktree(name="worktree-clean", parent="clean")
    head = head_of(wt)

    result = salvage(fx, wt)

    assert result.artifacts == [] and result.unpushed == 0 and result.written is True
    assert result.manifest_line == (
        f"worktree-clean | dispatch/clean-worktree-clean | head={head} | niet-op-remote=0 | "
    )
    assert (fx.dest / ws.MANIFEST_NAME).read_text(encoding="utf-8") == result.manifest_line + "\n"
    assert sorted(p.name for p in fx.dest.iterdir()) == sorted(
        [ws.MANIFEST_NAME, result.salvage_dir.name]
    )
    assert verify(fx, result) == []


def test_a_detached_tree_names_detached_in_its_line(fx: Fixture) -> None:
    head = head_of(fx.repo)
    wt = fx.tmp / "worktree-detached"
    git(fx.repo, "worktree", "add", "-q", "--detach", str(wt), head)

    result = salvage(fx, wt)

    assert (result.branch, result.head, result.unpushed) == ("(detached)", head, 0)
    assert result.manifest_line == (
        f"worktree-detached | (detached) | head={head} | niet-op-remote=0 | "
    )
    assert verify(fx, result) == []


# ---------------------------------------------------------------------------
# 3. tracked changes are carried by patches (WT-2a2)
# ---------------------------------------------------------------------------


def index_digest(wt: Path) -> str:
    index = git(wt, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
    return hashlib.sha256(Path(index).read_bytes()).hexdigest()


def scratch_checkout(fx: Fixture, wt: Path, head: str) -> Path:
    """A fresh repository given ``head`` by sha: it borrows no object store."""
    scratch = Path(tempfile.mkdtemp(prefix="scratch.", dir=fx.tmp))
    git(fx.tmp, "init", "-q", str(scratch))
    git(scratch, "fetch", "-q", "--no-tags", str(wt), head)
    git(scratch, "checkout", "-q", "-f", "FETCH_HEAD")
    return scratch


def patch_of(result: "ws.SalvageResult", suffix: str) -> Path:
    return result.salvage_dir / f"{result.name}{suffix}"


def test_an_unstaged_change_to_a_tracked_file_is_preserved(fx: Fixture) -> None:
    """Acceptance 1: one modified tracked file, unstaged."""
    wt = fx.worktree()
    head = head_of(wt)
    (wt / "a.txt").write_text("a\nedited\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert result.uncovered == []
    assert [Path(a).name for a in result.artifacts] == ["worktree-one.unstaged.patch"]
    patch = patch_of(result, ws.UNSTAGED_SUFFIX)
    assert patch.stat().st_size > 0
    assert verify(fx, result) == []

    scratch = scratch_checkout(fx, wt, head)
    git(scratch, "apply", str(patch))
    assert (scratch / "a.txt").read_bytes() == (wt / "a.txt").read_bytes()


def test_a_staged_and_an_unstaged_change_on_one_file_are_both_preserved(fx: Fixture) -> None:
    """Acceptance 2: the index and the worktree hold different states of one file."""
    wt = fx.worktree()
    head = head_of(wt)
    (wt / "a.txt").write_text("a\nstaged\n", encoding="utf-8")
    git(wt, "add", "a.txt")
    (wt / "a.txt").write_text("a\nstaged\nunstaged\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert result.uncovered == []
    assert [Path(a).name for a in result.artifacts] == [
        "worktree-one.staged.patch", "worktree-one.unstaged.patch"]
    assert verify(fx, result) == []

    scratch = scratch_checkout(fx, wt, head)
    git(scratch, "apply", "--index", str(patch_of(result, ws.STAGED_SUFFIX)))
    assert (scratch / "a.txt").read_bytes() == b"a\nstaged\n", "the staged state"
    git(scratch, "apply", str(patch_of(result, ws.UNSTAGED_SUFFIX)))
    assert (scratch / "a.txt").read_bytes() == (wt / "a.txt").read_bytes()


def test_a_patch_that_no_longer_applies_is_named(fx: Fixture) -> None:
    """Acceptance 3: one corrupted byte in the written patch, and verify names it."""
    wt = fx.worktree()
    (wt / "a.txt").write_text("b\n", encoding="utf-8")
    result = salvage(fx, wt)
    assert verify(fx, result) == []

    patch = patch_of(result, ws.UNSTAGED_SUFFIX)
    patch.write_bytes(patch.read_bytes().replace(b"-a\n", b"-c\n"))

    problems = verify(fx, result)
    assert len(problems) == 1, problems
    assert problems[0].startswith(
        f"{result.salvage_dir.name}/worktree-one.unstaged.patch: "), problems


def test_a_staged_rename_is_preserved_through_its_patch(fx: Fixture) -> None:
    """A rename/copy entry is covered by the patch and proved by the apply."""
    wt = fx.worktree()
    (wt / "a.txt").rename(wt / "b.txt")
    git(wt, "add", "-A")
    assert ws._status_entries(wt) == [("2", "b.txt")]

    result = salvage(fx, wt)

    assert result.uncovered == []
    assert verify(fx, result) == []
    scratch = scratch_checkout(fx, wt, head_of(wt))
    git(scratch, "apply", "--index", str(patch_of(result, ws.STAGED_SUFFIX)))
    assert (scratch / "b.txt").read_bytes() == b"a\n" and not (scratch / "a.txt").exists()


def test_the_worktree_index_is_byte_identical_after_salvage_and_verify(fx: Fixture) -> None:
    """Acceptance 5: measuring the tree never writes the tree's own index."""
    wt = fx.worktree()
    (wt / "a.txt").write_text("a\nstaged\n", encoding="utf-8")
    git(wt, "add", "a.txt")
    (wt / "a.txt").write_text("a\nstaged\nunstaged\n", encoding="utf-8")
    before = index_digest(wt)

    result = salvage(fx, wt)
    problems = verify(fx, result)

    assert problems == []
    assert index_digest(wt) == before, "salvage or verify rewrote the worktree's index"


def test_a_tracked_change_without_its_patch_is_a_problem(fx: Fixture) -> None:
    """Verify proves a patch by applying it, so a result that lists none is not clean."""
    wt = fx.worktree()
    (wt / "a.txt").write_text("a\nedited\n", encoding="utf-8")
    result = salvage(fx, wt)
    assert verify(fx, result) == []

    result.artifacts = []

    problems = verify(fx, result)

    assert len(problems) == 1, problems
    assert "no worktree-one.unstaged.patch artifact is listed" in problems[0], problems


def test_a_missing_patch_file_is_named(fx: Fixture) -> None:
    wt = fx.worktree()
    (wt / "a.txt").write_text("a\nedited\n", encoding="utf-8")
    result = salvage(fx, wt)
    patch_of(result, ws.UNSTAGED_SUFFIX).unlink()

    problems = verify(fx, result)

    assert len(problems) == 1, problems
    assert problems[0].endswith("worktree-one.unstaged.patch: missing"), problems


# ---------------------------------------------------------------------------
# 3a. every other status entry is still a named problem
# ---------------------------------------------------------------------------


def test_an_untracked_file_is_not_covered(fx: Fixture) -> None:
    wt = fx.worktree()
    (wt / "notes.txt").write_text("notes\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert result.uncovered == ["notes.txt"]
    assert verify(fx, result) == [UNCOVERED + "notes.txt"]


def test_an_ignored_file_is_not_covered(fx: Fixture) -> None:
    """OI-2417: ``--untracked-files=all`` alone never looks at ignored files."""
    wt = fx.worktree()
    notes = wt / ".vnx-data" / "unique-worker-notes.txt"
    notes.parent.mkdir(parents=True)
    notes.write_text("notes\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert result.uncovered == [".vnx-data/"], result.uncovered
    assert verify(fx, result) == [UNCOVERED + ".vnx-data/"]


def test_an_ignored_top_level_pycache_is_throwaway(fx: Fixture) -> None:
    """WT-2a1a pinned the empty allowlist here; WT-2a1b excuses this directory
    and nothing else, so every other case in this section stays blocking."""
    wt = fx.worktree()
    cache = wt / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_bytes(b"cache\n")

    result = salvage(fx, wt)

    assert result.uncovered == [], result.uncovered
    assert verify(fx, result) == []
    assert ws._status_entries(wt) == [("!", "__pycache__/")]


def test_the_problem_names_the_first_path_and_counts_the_rest(fx: Fixture) -> None:
    wt = fx.worktree()
    (wt / "notes.txt").write_text("notes\n", encoding="utf-8")
    (wt / "other.txt").write_text("other\n", encoding="utf-8")

    problems = verify(fx, salvage(fx, wt))

    assert problems == [UNCOVERED + "notes.txt (+1 more)"], problems


# ---------------------------------------------------------------------------
# 3b. the throwaway ignored caches, and nothing else (WT-2a1b, OI-2417)
# ---------------------------------------------------------------------------


def test_an_ignored_pycache_directory_is_throwaway(fx: Fixture) -> None:
    """``__pycache__/`` comes back as one ignored directory entry."""
    wt = fx.worktree()
    cache = wt / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "m.cpython-312.pyc").write_bytes(b"cache\n")

    result = salvage(fx, wt)

    assert result.uncovered == [], result.uncovered
    assert verify(fx, result) == []
    assert ws._status_entries(wt) == [("!", "pkg/__pycache__/")]


def test_an_ignored_node_modules_directory_is_throwaway(fx: Fixture) -> None:
    wt = fx.worktree()
    installed = wt / "node_modules" / "x"
    installed.mkdir(parents=True)
    (installed / "index.js").write_text("module.exports = 1\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert result.uncovered == [], result.uncovered
    assert verify(fx, result) == []
    assert ws._status_entries(wt) == [("!", "node_modules/")]


def test_an_ignored_file_named_like_a_cache_directory_still_blocks(
        ignored_otherwise) -> None:
    """A regular file sharing the directory's name is not that directory.

    The ignore rule ``node_modules`` (no trailing slash) also catches a FILE
    called ``node_modules``, and git prints it without the ``/`` that marks a
    directory entry. Nothing bundles an ignored file, so excusing it would
    approve losing it (codex round 2 on the WT-2a1b allowlist).
    """
    fx = ignored_otherwise("node_modules\n")
    wt = fx.worktree()
    (wt / "node_modules").write_text("a file, not a tree of modules\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert ws._status_entries(wt) == [("!", "node_modules")], ws._status_entries(wt)
    assert result.uncovered == ["node_modules"], result.uncovered
    assert verify(fx, result) == [UNCOVERED + "node_modules"]


def test_an_ignored_file_named_like_a_nested_cache_directory_still_blocks(
        ignored_otherwise) -> None:
    """The same for ``pkg/__pycache__`` as a file: the leaf is not a directory."""
    fx = ignored_otherwise("__pycache__\n")
    wt = fx.worktree()
    (wt / "pkg").mkdir()
    (wt / "pkg" / "__pycache__").write_text("a file, not bytecode\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert ws._status_entries(wt) == [("!", "pkg/__pycache__")], ws._status_entries(wt)
    assert result.uncovered == ["pkg/__pycache__"], result.uncovered
    assert verify(fx, result) == [UNCOVERED + "pkg/__pycache__"]


def test_a_file_inside_an_ignored_node_modules_directory_is_throwaway(
        ignored_otherwise) -> None:
    """The file entry inside the ignored directory, named on its own, is excused."""
    fx = ignored_otherwise("node_modules/*\n")
    wt = fx.worktree()
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "index.js").write_text(
        "module.exports = 1\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert ws._status_entries(wt) == [("!", "node_modules/index.js")], ws._status_entries(wt)
    assert result.uncovered == [], result.uncovered
    assert verify(fx, result) == []


def test_an_ignored_file_inside_an_allowlisted_directory_is_throwaway(
        ignored_otherwise) -> None:
    """A file entry, not a directory entry: the ignore rule names the file."""
    fx = ignored_otherwise(".vnx-data/\n*.ruff\n")
    wt = fx.worktree()
    (wt / ".ruff_cache").mkdir()
    (wt / ".ruff_cache" / "cache.ruff").write_bytes(b"cache\n")

    result = salvage(fx, wt)

    assert result.uncovered == [], result.uncovered
    assert verify(fx, result) == []
    assert ws._status_entries(wt) == [("!", ".ruff_cache/cache.ruff")]


def test_an_ignored_file_below_vnx_data_still_blocks(fx: Fixture) -> None:
    """OI-2417 point 3, restated next to the allowlist it must not widen."""
    wt = fx.worktree()
    notes = wt / ".vnx-data" / "unique-worker-notes.txt"
    notes.parent.mkdir(parents=True)
    notes.write_text("notes\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert result.uncovered == [".vnx-data/"], result.uncovered
    assert verify(fx, result) == [UNCOVERED + ".vnx-data/"]


def test_an_ignored_cache_entry_below_vnx_data_still_blocks(ignored_otherwise) -> None:
    """The allowlist name is in the path, yet a protected root precedes it.

    The ignore rules name the directory's contents, so the entry git prints is
    ``.vnx-data/__pycache__/`` — an allowlisted component below ``.vnx-data/``.
    """
    fx = ignored_otherwise(".vnx-data/*\n__pycache__/\n")
    wt = fx.worktree()
    cache = wt / ".vnx-data" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "x.pyc").write_bytes(b"cache\n")

    result = salvage(fx, wt)

    assert result.uncovered == [".vnx-data/__pycache__/"], result.uncovered
    assert verify(fx, result) == [UNCOVERED + ".vnx-data/__pycache__/"]
    assert ws._status_entries(wt) == [("!", ".vnx-data/__pycache__/")]


def test_a_cache_directory_below_dot_git_is_never_throwaway() -> None:
    """git never prints ``.git`` in a status entry; the rule holds anyway."""
    assert ws._is_throwaway_ignored("!", ".git/__pycache__/x.pyc") is False
    assert ws._is_throwaway_ignored("!", "pkg/__pycache__/x.pyc") is True


def test_a_leaf_that_is_no_directory_is_never_throwaway() -> None:
    """Only DIRECTORY components match the allowlist (codex round 2, WT-2a1b).

    A trailing ``/`` is git's marker for a directory entry; a leaf without it is
    a regular file, whatever its name.
    """
    assert ws._is_throwaway_ignored("!", "node_modules") is False
    assert ws._is_throwaway_ignored("!", "pkg/__pycache__") is False
    assert ws._is_throwaway_ignored("!", "x/node_modules") is False
    assert ws._is_throwaway_ignored("!", "node_modules/") is True
    assert ws._is_throwaway_ignored("!", "pkg/__pycache__/") is True
    assert ws._is_throwaway_ignored("!", ".vnx-data/node_modules") is False
    assert ws._is_throwaway_ignored("!", ".vnx-data/node_modules/") is False


def test_an_ignored_build_directory_still_blocks(fx: Fixture) -> None:
    """A directory on no allowlist is not excused by looking disposable."""
    wt = fx.worktree()
    (wt / "build").mkdir()
    (wt / "build" / "out.bin").write_bytes(b"artifact\n")

    result = salvage(fx, wt)

    assert result.uncovered == ["build/"], result.uncovered
    assert verify(fx, result) == [UNCOVERED + "build/"]


def test_an_untracked_file_under_an_allowlisted_directory_still_blocks(
        ignored_otherwise) -> None:
    """Only an IGNORED entry can be throwaway; a ``?`` entry blocks either way."""
    fx = ignored_otherwise(".vnx-data/\n")
    wt = fx.worktree()
    cache = wt / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "m.py").write_text("source\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert result.uncovered == ["pkg/__pycache__/m.py"], result.uncovered
    assert verify(fx, result) == [UNCOVERED + "pkg/__pycache__/m.py"]
    assert ws._status_entries(wt) == [("?", "pkg/__pycache__/m.py")]


def test_a_tracked_pyc_under_an_allowlisted_directory_is_carried_by_a_patch(
        fx: Fixture) -> None:
    """A committed ``.pyc`` that is modified is a tracked entry, not an ignored one:
    the throwaway allowlist does not touch it, its patch carries it instead."""
    wt = fx.worktree()
    cache = wt / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "m.pyc").write_bytes(b"committed\n")
    git(wt, "add", "-f", "pkg/__pycache__/m.pyc")
    git(wt, "commit", "-q", "-m", "worker: a committed .pyc")
    (cache / "m.pyc").write_bytes(b"edited\n")

    result = salvage(fx, wt)

    assert result.uncovered == [], result.uncovered
    assert result.artifacts[-1].endswith(ws.UNSTAGED_SUFFIX), result.artifacts
    assert verify(fx, result) == []


def test_an_allowlisted_cache_beside_one_blocking_file_gives_one_problem(
        fx: Fixture) -> None:
    wt = fx.worktree()
    cache = wt / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "x.pyc").write_bytes(b"cache\n")
    (wt / "debug.log").write_text("worker log\n", encoding="utf-8")

    problems = verify(fx, salvage(fx, wt))

    assert problems == [UNCOVERED + "debug.log"], problems
    assert sorted(ws._status_entries(wt)) == [
        ("!", "debug.log"), ("!", "pkg/__pycache__/")]


def add_submodule(fx: Fixture, wt: Path, name: str = "sub") -> Path:
    """A real populated submodule, committed into ``wt``'s index."""
    src = fx.tmp / f"{name}src"
    git(fx.tmp, "init", "-q", "-b", "main", str(src))
    git(src, "config", "commit.gpgsign", "false")
    (src / "f.txt").write_text("base\n", encoding="utf-8")
    git(src, "add", "f.txt")
    git(src, "commit", "-q", "-m", "sub base")
    git(wt, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), name)
    git(wt, "commit", "-q", "-m", f"add the submodule {name}")
    git(wt / name, "config", "commit.gpgsign", "false")
    return wt / name


def test_a_dirty_submodule_checkout_is_not_covered(fx: Fixture) -> None:
    """OI-2405: the submodule's checkout has an edited tracked file."""
    wt = fx.worktree()
    add_submodule(fx, wt)
    assert git_status(wt) == ""

    (wt / "sub" / "f.txt").write_text("base\nedited in the submodule\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert result.uncovered == ["sub"], git_status(wt)
    assert verify(fx, result) == [UNCOVERED + "sub"]
    assert bundle_of(result).is_file(), "the commit on top of origin is still salvaged"


def test_a_clean_populated_submodule_is_not_preserved(fx: Fixture) -> None:
    """No local submodule commit, yet its store is not in the bundle either."""
    wt = fx.worktree()
    add_submodule(fx, wt)
    assert git_status(wt) == ""

    result = salvage(fx, wt)

    assert result.uncovered == ["sub"]
    assert verify(fx, result) == [UNCOVERED + "sub"]


def test_an_unpushed_commit_inside_a_clean_submodule_is_not_preserved(fx: Fixture) -> None:
    """The codex repro of #2291: the gitlink is committed and the tree is clean,
    but the submodule commit lives in no bundle the superproject carries."""
    wt = fx.worktree()
    sub = add_submodule(fx, wt)
    (sub / "g.txt").write_text("sub work\n", encoding="utf-8")
    git(sub, "add", "g.txt")
    git(sub, "commit", "-q", "-m", "sub: local work")
    git(wt, "add", "sub")
    git(wt, "commit", "-q", "-m", "worker: advance the submodule")
    assert git_status(wt) == ""

    result = salvage(fx, wt)

    assert result.uncovered == ["sub"]
    assert verify(fx, result) == [UNCOVERED + "sub"]


def test_an_uninitialised_gitlink_is_not_a_problem(fx: Fixture) -> None:
    """A gitlink whose checkout was never made has nothing local to lose."""
    add_submodule(fx, fx.repo)
    git(fx.repo, "push", "-q", "origin", "main")
    wt = fx.worktree()
    assert not (wt / "sub" / ".git").exists()
    assert git_status(wt) == ""

    result = salvage(fx, wt)

    assert result.uncovered == []
    assert verify(fx, result) == []


def test_an_unmerged_path_is_not_covered(fx: Fixture) -> None:
    wt = fx.worktree()
    fx.commit(wt, "a.txt", "a\nfrom the worktree\n", "worker: edit")
    (fx.repo / "a.txt").write_text("a\nfrom main\n", encoding="utf-8")
    git(fx.repo, "add", "a.txt")
    git(fx.repo, "commit", "-q", "-m", "main moves on")
    merge = subprocess.run(
        ["git", "merge", "main"], cwd=str(wt), capture_output=True, text=True,
        env={**os.environ, **GIT_ENV},
    )
    assert merge.returncode != 0, "the merge must conflict"
    assert git(wt, "status", "--porcelain=v2").stdout.startswith("u UU ")

    result = salvage(fx, wt)

    assert result.uncovered == ["a.txt"]
    assert verify(fx, result) == [UNCOVERED + "a.txt"]


# ---------------------------------------------------------------------------
# 4. a valid bundle header over a pack that carries no object
# ---------------------------------------------------------------------------


def _salvaged_bundle(fx: Fixture) -> tuple["ws.SalvageResult", Path]:
    wt = fx.worktree()
    fx.commit(wt, "local.txt", "local\n", "worker: local work")
    result = salvage(fx, wt)
    bundle = bundle_of(result)
    assert bundle.is_file(), result.artifacts
    return result, bundle


def _empty_pack(repo: Path) -> bytes:
    """A valid packfile that carries no object at all (header + empty trailer)."""
    proc = subprocess.run(
        ["git", "pack-objects", "--stdout"], cwd=str(repo), input=b"", capture_output=True,
        env={**os.environ, **GIT_ENV},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout[:4] == b"PACK" and proc.stdout[8:12] == b"\0\0\0\0", proc.stdout[:16]
    return proc.stdout


def test_a_bundle_header_over_an_empty_pack_is_a_problem(fx: Fixture) -> None:
    """The round-3 case of #2277: git calls it valid, the pack restores nothing."""
    result, bundle = _salvaged_bundle(fx)
    assert verify(fx, result) == []

    data = bundle.read_bytes()
    header = data[: data.index(b"\n\n") + 2]
    bundle.write_bytes(header + _empty_pack(fx.repo))
    assert git(fx.repo, "bundle", "verify", str(bundle)).returncode == 0

    problems = verify(fx, result)
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"{result.salvage_dir.name}/worktree-one.bundle: "), problems
    assert "unpack" in problems[0], problems


# ---------------------------------------------------------------------------
# 5. one unique directory per salvage, and never an existing one
# ---------------------------------------------------------------------------


def test_two_trees_with_the_same_basename_keep_their_own_directories(fx: Fixture) -> None:
    first = fx.worktree(name="worktree-one", parent="alpha")
    second = fx.worktree(name="worktree-one", parent="beta")
    head = fx.commit(first, "one.txt", "one\n", "alpha work")
    # Both trees at one head: the two salvages collide on the directory name.
    git(second, "reset", "-q", "--hard", head)

    one = salvage(fx, first)
    two = salvage(fx, second)

    assert one.salvage_dir != two.salvage_dir
    assert two.salvage_dir.name == f"{one.salvage_dir.name}-2"
    for result, name in ((one, "clone-one"), (two, "clone-two")):
        assert (result.head, result.unpushed) == (head, 1)
        clone = clone_bundle(fx, bundle_of(result), name)
        assert git(clone, "rev-parse", head).returncode == 0
    assert verify(fx, one) == []
    assert verify(fx, two) == []

    lines = (fx.dest / ws.MANIFEST_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert one.salvage_dir.name in lines[0] and two.salvage_dir.name not in lines[0]
    assert two.salvage_dir.name in lines[1]


def test_two_writers_of_one_artifact_never_share_a_scratch_path(
        fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """The staged bundle is named for the call, never for the destination (OI-1486).

    A scratch name derived from the destination alone is one path for every
    writer of that destination: two writers overwrite each other's bytes and the
    first to publish lands the other's content under its own name. Each call
    stages inside a directory ``tempfile.mkdtemp`` names, so two stagings of one
    destination can never pick the same path.
    """
    wt = fx.worktree()
    fx.commit(wt, "local.txt", "local\n", "worker: local work")
    dest = fx.tmp / "publish"
    dest.mkdir()
    staged_in: list[Path] = []
    real_mkdtemp = ws.tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs) -> str:
        path = real_mkdtemp(*args, **kwargs)
        staged_in.append(Path(path))
        return path

    monkeypatch.setattr(ws.tempfile, "mkdtemp", recording_mkdtemp)
    ws._write_bundle(dest, "one.bundle", wt, "origin")
    (dest / "one.bundle").unlink()  # the same artifact, staged a second time
    ws._write_bundle(dest, "one.bundle", wt, "origin")

    assert len(staged_in) == 2, staged_in
    assert staged_in[0] != staged_in[1], (
        f"both writers staged in {staged_in[0]} — one scratch path for one "
        "destination is shared, so the writers overwrite each other (OI-1486)"
    )
    for scratch in staged_in:
        assert scratch.parent == dest, f"{scratch} is not inside the destination"
        assert scratch.name.startswith(".one.bundle."), (
            f"{scratch.name} carries no marker tying it to this write")
        assert not scratch.exists(), f"a scratch directory survived the publish: {scratch}"
    assert [p.name for p in dest.iterdir()] == ["one.bundle"]
    assert (dest / "one.bundle").stat().st_size > 0


def test_a_failed_bundle_leaves_no_scratch_behind(fx: Fixture) -> None:
    """A git failure must not leave a staging directory in the destination."""
    dest = fx.tmp / "publish"
    dest.mkdir()
    not_a_repo = fx.tmp / "not-a-repo"
    not_a_repo.mkdir()

    with pytest.raises(ws.SalvageError) as failure:
        ws._write_bundle(dest, "one.bundle", not_a_repo, "origin")

    assert "create bundle" in str(failure.value), failure.value
    assert list(dest.iterdir()) == [], "the scratch directory outlived the failed bundle"


def test_a_file_at_the_target_path_is_never_overwritten(fx: Fixture) -> None:
    wt = fx.worktree()
    head = head_of(wt)
    fx.dest.mkdir(parents=True)
    planted = fx.dest / f"worktree-one-{STAMP}-{head[:8]}"
    planted.write_text("do not overwrite\n", encoding="utf-8")

    result = salvage(fx, wt)

    assert planted.read_text(encoding="utf-8") == "do not overwrite\n"
    assert result.salvage_dir.name == f"{planted.name}-2"
    assert result.salvage_dir.is_dir() and verify(fx, result) == []


# ---------------------------------------------------------------------------
# 6. dry run, and the evidence checks of verify
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(fx: Fixture) -> None:
    wt = fx.worktree()
    head = fx.commit(wt, "local.txt", "local\n", "worker: local work")

    result = ws.salvage(wt, fx.dest, repo_root=fx.repo)

    assert result.written is False
    assert result.artifacts == [f"worktree-one-{STAMP}-{head[:8]}/worktree-one.bundle"]
    assert result.manifest_line == (
        f"worktree-one | dispatch/one-worktree-one | head={head} | niet-op-remote=1 | "
        f"worktree-one-{STAMP}-{head[:8]}/worktree-one.bundle "
    )
    assert not fx.dest.exists()


def test_an_empty_bundle_is_named(fx: Fixture) -> None:
    result, bundle = _salvaged_bundle(fx)
    assert verify(fx, result) == []

    bundle.write_bytes(b"")

    problems = verify(fx, result)
    assert len(problems) == 1, problems
    assert problems[0].endswith("worktree-one.bundle: empty"), problems


def test_a_missing_bundle_is_named(fx: Fixture) -> None:
    result, bundle = _salvaged_bundle(fx)

    bundle.unlink()

    problems = verify(fx, result)
    assert len(problems) == 1, problems
    assert problems[0].endswith("worktree-one.bundle: missing"), problems


def test_an_artifact_kind_verify_cannot_check_is_a_problem(fx: Fixture) -> None:
    result, _bundle = _salvaged_bundle(fx)
    (result.salvage_dir / "worktree-one.patch").write_text("not a diff\n", encoding="utf-8")
    result.artifacts = [*result.artifacts, f"{result.salvage_dir.name}/worktree-one.patch"]

    problems = verify(fx, result)

    assert len(problems) == 1, problems
    assert "not an artifact kind this verify can check" in problems[0], problems


def test_a_result_without_its_bundle_is_a_problem(fx: Fixture) -> None:
    result, _bundle = _salvaged_bundle(fx)
    assert result.unpushed == 1

    result.artifacts = []

    problems = verify(fx, result)

    assert len(problems) == 1, problems
    assert "no bundle is listed" in problems[0], problems


def test_verify_names_a_head_that_moved_after_the_salvage(fx: Fixture) -> None:
    wt = fx.worktree()
    head = fx.commit(wt, "local.txt", "local\n", "worker: local work")
    result = salvage(fx, wt)
    assert verify(fx, result) == []

    later = fx.commit(wt, "later.txt", "later\n", "worker: more work")

    problems = verify(fx, result)
    assert len(problems) == 1, problems
    assert head in problems[0] and later in problems[0], problems


def test_verify_without_the_tree_stays_blocking(fx: Fixture) -> None:
    wt = fx.worktree()
    fx.commit(wt, "local.txt", "local\n", "worker: local work")
    result = salvage(fx, wt)

    git(fx.repo, "worktree", "remove", "--force", str(wt))

    problems = verify(fx, result)
    assert len(problems) == 1, problems
    assert "no directory" in problems[0], problems


def test_a_tree_without_head_is_a_problem(fx: Fixture) -> None:
    empty = fx.tmp / "empty"
    git(fx.tmp, "init", "-q", "-b", "main", str(empty))
    result = ws.SalvageResult(
        name="empty", branch="main", head="0" * 40, unpushed=0,
        salvage_dir=fx.tmp / "nowhere", worktree=empty,
    )

    problems = verify(fx, result)

    assert len(problems) == 1, problems
    assert "HEAD" in problems[0], problems


def test_verify_without_a_usable_repo_root_stays_blocking(fx: Fixture) -> None:
    result, _bundle = _salvaged_bundle(fx)

    problems = ws.verify(result, fx.dest, repo_root=fx.tmp / "not-a-checkout")

    assert problems and "no usable repo_root" in problems[0], problems


# ---------------------------------------------------------------------------
# 7. an artifact inside the worktree removal would delete is refused (OI-2414)
# ---------------------------------------------------------------------------


def test_a_dest_dir_inside_the_worktree_is_refused(fx: Fixture) -> None:
    """The codex repro on #2291: the ignored directory hides the bundle from
    ``git status``, yet removal deletes it together with the tree."""
    wt = fx.worktree()
    head = fx.commit(wt, "local.txt", "local\n", "worker: local work")
    dest = wt / ".vnx-data" / "salvage"

    result = ws.salvage(wt, dest, repo_root=fx.repo, dry_run=False)

    assert result.unpushed == 1
    assert bundle_of(result).is_file(), "the bundle really lands inside the tree"
    problems = ws.verify(result, dest, repo_root=fx.repo)
    assert problems, "nothing may pass while the evidence sits in the tree"
    assert any("resolves inside the worktree" in p for p in problems), problems
    assert head_of(wt) == head and wt.is_dir(), "nothing authorized a removal"


def test_a_dest_dir_inside_the_worktree_without_artifacts_is_refused(fx: Fixture) -> None:
    """Even a tree with no commits to carry may not point its dest_dir inward."""
    wt = fx.worktree(name="worktree-clean", parent="clean")
    dest = wt / ".vnx-data" / "salvage"

    result = ws.salvage(wt, dest, repo_root=fx.repo, dry_run=False)

    assert result.artifacts == []
    problems = ws.verify(result, dest, repo_root=fx.repo)
    assert any("resolves inside the worktree" in p for p in problems), problems
    assert wt.is_dir(), "nothing authorized a removal"


def test_a_symlink_outside_the_worktree_that_points_inside_is_refused(fx: Fixture) -> None:
    """``dest_dir`` is spelled outside the tree; it lands inside it."""
    wt = fx.worktree()
    fx.commit(wt, "local.txt", "local\n", "worker: local work")
    inside = wt / ".vnx-data" / "salvage"
    inside.mkdir(parents=True)
    dest = fx.tmp / "salvage-link"
    dest.symlink_to(inside, target_is_directory=True)

    result = ws.salvage(wt, dest, repo_root=fx.repo, dry_run=False)

    assert bundle_of(result).is_file(), "written through the link, inside the tree"
    problems = ws.verify(result, dest, repo_root=fx.repo)
    assert any("resolves inside the worktree" in p for p in problems), problems
    assert wt.is_dir(), "nothing authorized a removal"


def test_an_artifact_symlinked_into_the_worktree_is_refused(fx: Fixture) -> None:
    """The artifact file itself is the link; the path's spelling is outside."""
    result, bundle = _salvaged_bundle(fx)
    assert verify(fx, result) == []

    inside = result.worktree / ".vnx-data" / "salvage"
    inside.mkdir(parents=True)
    hidden = inside / bundle.name
    hidden.write_bytes(bundle.read_bytes())
    bundle.unlink()
    bundle.symlink_to(hidden)

    problems = verify(fx, result)
    assert any("resolves inside the worktree" in p for p in problems), problems


def test_an_artifact_whose_location_cannot_be_resolved_is_refused(fx: Fixture) -> None:
    """A symlink loop proves no location at all; that is a problem, never a pass."""
    result, bundle = _salvaged_bundle(fx)
    assert verify(fx, result) == []

    bundle.unlink()
    bundle.symlink_to(bundle)

    problems = verify(fx, result)
    assert len(problems) == 1, problems
    assert "cannot resolve its location" in problems[0], problems


def test_a_dest_dir_beside_the_worktree_is_outside(fx: Fixture) -> None:
    """A path that merely shares the worktree's name prefix is not inside it."""
    wt = fx.worktree()
    fx.commit(wt, "local.txt", "local\n", "worker: local work")
    dest = fx.tmp / f"{wt.name}-salvage"

    result = ws.salvage(wt, dest, repo_root=fx.repo, dry_run=False)

    assert ws.verify(result, dest, repo_root=fx.repo) == []


# ---------------------------------------------------------------------------
# Fail-closed: a git failure is a SalvageError, never a clean salvage
# ---------------------------------------------------------------------------


def test_a_directory_that_is_no_git_worktree_is_a_salvage_error(fx: Fixture) -> None:
    plain = fx.tmp / "plain"
    plain.mkdir()

    with pytest.raises(ws.SalvageError):
        salvage(fx, plain)


def test_gitlinks_of_a_directory_that_is_no_worktree_are_a_salvage_error(fx: Fixture) -> None:
    plain = fx.tmp / "plain"
    plain.mkdir()

    with pytest.raises(ws.SalvageError, match="read gitlinks"):
        ws._populated_submodules(plain)


def test_a_worktree_of_another_repository_is_a_salvage_error(fx: Fixture) -> None:
    other = fx.tmp / "other"
    git(fx.tmp, "init", "-q", "-b", "main", str(other))
    wt = fx.worktree()

    with pytest.raises(ws.SalvageError, match="not a worktree of"):
        ws.salvage(wt, fx.dest, repo_root=other, dry_run=False)
