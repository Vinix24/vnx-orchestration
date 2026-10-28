"""The untracked archive of a worktree salvage (WT-2b).

Every test builds a real bare ``origin``, a real main checkout and a real ``git
worktree`` under ``tmp_path``, then runs the real functions: the archive is read
back with ``tarfile`` and the list is parsed with the record format the module
docstring states, so a passing test means the saved bytes really restore the
work.

The OI-2361 reproduction leads: git reports a nested repository as ONE
``nested/`` directory record, so an archive that only kept that record would let
a removal delete ``nested/precious.txt`` while ``verify`` called the tree clean.
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import worktree_salvage as ws

STAMP = "20261027T120000Z"
UNCOVERED = "tree has state this salvage does not cover: "
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}
DEFAULT_IGNORES = ".vnx-data/\n__pycache__/\nnode_modules/\nbuild/\n*.log\n"


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **GIT_ENV},
    )
    assert proc.returncode == 0, f"git {' '.join(args)} in {cwd}: {proc.stderr}"
    return proc


class Fixture:
    """A bare origin, a main checkout that can hold worktrees, and a dest dir."""

    def __init__(self, root: Path, ignores: str = DEFAULT_IGNORES) -> None:
        self.tmp = root
        self.origin = root / "origin.git"
        self.repo = root / "repo"
        self.dest = root / "salvage"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "config", "tag.gpgsign", "false")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        (self.repo / ".gitignore").write_text(ignores, encoding="utf-8")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")

    def worktree(self, name: str = "worktree-one", parent: str = "one") -> Path:
        wt = self.repo / ".vnx-data" / "worktrees" / parent / name
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "-q", "-b", f"dispatch/{parent}-{name}",
            str(wt), "origin/main")
        return wt


@pytest.fixture()
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    monkeypatch.setattr(ws, "_utc_stamp", lambda: STAMP)
    return Fixture(tmp_path)


@pytest.fixture()
def ignored_otherwise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fixture factory for ignore rules the default fixture does not model."""
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


class Record(NamedTuple):
    """One record of the documented list format."""
    kind: str
    size: int
    digest: str
    path: str


def list_path(result: "ws.SalvageResult") -> Path:
    return result.salvage_dir / f"{result.name}{ws.UNTRACKED_LIST_SUFFIX}"


def archive_path(result: "ws.SalvageResult") -> Path:
    return result.salvage_dir / f"{result.name}{ws.UNTRACKED_ARCHIVE_SUFFIX}"


def records_of(result: "ws.SalvageResult") -> list[Record]:
    """The list as the contract states it: NUL-separated ``kind size sha path``."""
    body = list_path(result).read_bytes()
    return [Record(kind, int(size), digest, path)
            for kind, size, digest, path in
            (record.split(" ", 3) for record in
             (os.fsdecode(chunk) for chunk in body.split(b"\0") if chunk))]


def archived_names(result: "ws.SalvageResult") -> list[str]:
    with tarfile.open(archive_path(result), "r:gz") as tar:
        return tar.getnames()


def rebuild_without(result: "ws.SalvageResult", drop: str) -> None:
    """Re-pack the archive without one member: the list still promises it."""
    archive = archive_path(result)
    with tarfile.open(archive, "r:gz") as tar:
        kept = [(info, tar.extractfile(info).read() if info.isfile() else None)
                for info in tar.getmembers() if info.name != drop]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for info, data in kept:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    archive.write_bytes(buffer.getvalue())


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 1. the OI-2361 reproduction: a nested repository is one directory record
# ---------------------------------------------------------------------------


def test_a_nested_repository_is_archived_file_by_file(fx: Fixture) -> None:
    """git names the embedded repo as ``nested/``; the archive must hold
    ``nested/precious.txt`` and its own ``.git``, not just the directory name."""
    wt = fx.worktree()
    nested = wt / "nested"
    git(wt, "init", "-q", "-b", "main", str(nested))
    (nested / "precious.txt").write_text("precious\n", encoding="utf-8")
    assert ws._status_entries(wt) == [("?", "nested/")], ws._status_entries(wt)

    result = salvage(fx, wt)

    assert result.uncovered == []
    assert [Path(a).name for a in result.artifacts] == [
        "worktree-one.untracked.tgz", "worktree-one.untracked.list"]
    names = archived_names(result)
    assert "nested/precious.txt" in names, names
    assert "nested/.git/HEAD" in names, "a nested .git is walked too"
    listed = {record.path: record for record in records_of(result)}
    assert listed["nested/precious.txt"].kind == "f"
    assert listed["nested/precious.txt"].digest == sha(b"precious\n")
    assert verify(fx, result) == []

    rebuild_without(result, "nested/precious.txt")

    problems = verify(fx, result)
    assert problems == [
        f"{result.salvage_dir.name}/worktree-one.untracked.tgz: member "
        "nested/precious.txt is in the list but not in the archive"], problems


# ---------------------------------------------------------------------------
# 2. a plain untracked file and an ignored non-throwaway file
# ---------------------------------------------------------------------------


def test_an_untracked_and_an_ignored_file_are_both_archived(ignored_otherwise) -> None:
    fx = ignored_otherwise(DEFAULT_IGNORES + "dist/\n")
    wt = fx.worktree()
    (wt / "notes.txt").write_text("notes\n", encoding="utf-8")
    (wt / "dist").mkdir()
    (wt / "dist" / "out.bin").write_bytes(b"artifact\n")

    result = salvage(fx, wt)

    assert result.uncovered == []
    assert set(archived_names(result)) == {"notes.txt", "dist/out.bin"}
    listed = {record.path: record for record in records_of(result)}
    assert (listed["notes.txt"].kind, listed["notes.txt"].size) == ("f", 6)
    assert (listed["dist/out.bin"].digest, listed["dist/out.bin"].size) == (
        sha(b"artifact\n"), len(b"artifact\n"))
    assert verify(fx, result) == []
    manifest = (fx.dest / ws.MANIFEST_NAME).read_text(encoding="utf-8")
    assert result.manifest_line.endswith(
        f"{result.salvage_dir.name}/worktree-one.untracked.tgz "
        f"{result.salvage_dir.name}/worktree-one.untracked.list ")
    assert manifest == result.manifest_line + "\n"


def test_a_dry_run_lists_the_archive_but_writes_nothing(fx: Fixture) -> None:
    wt = fx.worktree()
    (wt / "notes.txt").write_text("notes\n", encoding="utf-8")

    result = ws.salvage(wt, fx.dest, repo_root=fx.repo)

    assert result.written is False
    assert [Path(a).name for a in result.artifacts] == [
        "worktree-one.untracked.tgz", "worktree-one.untracked.list"]
    assert not fx.dest.exists()


# ---------------------------------------------------------------------------
# 3. what stays outside the archive: evidence, and the throwaway caches
# ---------------------------------------------------------------------------


def test_evidence_below_vnx_data_blocks_and_a_cache_is_not_archived(fx: Fixture) -> None:
    wt = fx.worktree()
    notes = wt / ".vnx-data" / "unique-worker-notes.txt"
    notes.parent.mkdir(parents=True)
    notes.write_text("notes\n", encoding="utf-8")
    cache = wt / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "x.pyc").write_bytes(b"cache\n")

    result = salvage(fx, wt)

    assert sorted(ws._status_entries(wt)) == [
        ("!", ".vnx-data/"), ("!", "pkg/__pycache__/")]
    assert result.uncovered == [".vnx-data/"], result.uncovered
    assert result.artifacts == [], "nothing is left to carry"
    assert verify(fx, result) == [UNCOVERED + ".vnx-data/"]


# ---------------------------------------------------------------------------
# 4. symlinks: an internal convenience link, and one pointing outside
# ---------------------------------------------------------------------------


def test_an_internal_symlink_is_covered_without_being_archived(fx: Fixture) -> None:
    """``.venv -> <repo_root>/.venv`` holds no work: covered, never carried."""
    (fx.repo / ".venv").mkdir()
    wt = fx.worktree()
    (wt / ".venv").symlink_to(fx.repo / ".venv", target_is_directory=True)
    assert ws._status_entries(wt) == [("?", ".venv")], ws._status_entries(wt)

    result = salvage(fx, wt)

    assert result.uncovered == []
    assert result.artifacts == [], "the convenience link is not archived"
    assert verify(fx, result) == []


def test_an_untracked_symlink_outside_the_repo_is_archived_as_a_symlink(
        fx: Fixture) -> None:
    outside = fx.tmp / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("x\n", encoding="utf-8")
    wt = fx.worktree()
    (wt / "link").symlink_to(outside)

    result = salvage(fx, wt)

    assert result.uncovered == []
    assert [(record.kind, record.path) for record in records_of(result)] == [("l", "link")]
    assert records_of(result)[0].size == len(os.fsencode(str(outside)))
    with tarfile.open(archive_path(result), "r:gz") as tar:
        info = tar.getmember("link")
        assert info.issym(), "a symlink is archived as a symlink"
        assert info.linkname == str(outside)
        assert "link/x.txt" not in tar.getnames(), "the link was never followed"
    assert verify(fx, result) == []


# ---------------------------------------------------------------------------
# 5. the tree moving after the salvage
# ---------------------------------------------------------------------------


def test_an_untracked_file_edited_after_the_salvage_is_named(fx: Fixture) -> None:
    wt = fx.worktree()
    (wt / "notes.txt").write_text("notes\n", encoding="utf-8")
    result = salvage(fx, wt)
    assert verify(fx, result) == []

    (wt / "notes.txt").write_text("notes\nedited\n", encoding="utf-8")

    problems = verify(fx, result)

    assert problems == [
        "tree worktree-one: notes.txt differs from the untracked list"], problems


def test_an_untracked_file_deleted_after_the_salvage_is_named(fx: Fixture) -> None:
    wt = fx.worktree()
    (wt / "notes.txt").write_text("notes\n", encoding="utf-8")
    result = salvage(fx, wt)
    assert verify(fx, result) == []

    (wt / "notes.txt").unlink()

    problems = verify(fx, result)

    assert problems == [
        "tree worktree-one: notes.txt is not in the tree as the untracked list "
        "records it"], problems


def test_an_untracked_file_added_after_the_salvage_is_named(fx: Fixture) -> None:
    """A new file rides on no coverage the archive proved: it is named too."""
    wt = fx.worktree()
    (wt / "notes.txt").write_text("notes\n", encoding="utf-8")
    result = salvage(fx, wt)
    assert verify(fx, result) == []

    (wt / "later.txt").write_text("later\n", encoding="utf-8")

    problems = verify(fx, result)

    assert problems == [
        "tree worktree-one: later.txt is in the tree but not in the untracked list"], problems


# ---------------------------------------------------------------------------
# 6. an empty directory is a member, and a broken archive is a named problem
# ---------------------------------------------------------------------------


def test_an_empty_directory_is_archived_as_a_member(fx: Fixture) -> None:
    """No file implies an empty directory, so it is a ``d`` member of its own."""
    wt = fx.worktree()
    nested = wt / "nested"
    git(wt, "init", "-q", "-b", "main", str(nested))
    (nested / "empty").mkdir()

    result = salvage(fx, wt)

    listed = {record.path: record for record in records_of(result)}
    assert listed["nested/empty"] == Record("d", 0, "-", "nested/empty")
    with tarfile.open(archive_path(result), "r:gz") as tar:
        assert tar.getmember("nested/empty").isdir()
    assert verify(fx, result) == []


def test_a_truncated_archive_is_named(fx: Fixture) -> None:
    wt = fx.worktree()
    (wt / "notes.txt").write_text("notes\n", encoding="utf-8")
    result = salvage(fx, wt)
    assert verify(fx, result) == []

    archive_path(result).write_bytes(b"")

    problems = verify(fx, result)

    assert any(p.endswith("worktree-one.untracked.tgz: empty") for p in problems), problems
    assert any("untracked content is not archived" in p for p in problems), problems


def test_a_missing_untracked_list_is_named(fx: Fixture) -> None:
    wt = fx.worktree()
    (wt / "notes.txt").write_text("notes\n", encoding="utf-8")
    result = salvage(fx, wt)
    assert verify(fx, result) == []

    list_path(result).unlink()

    problems = verify(fx, result)

    assert any(p.endswith("worktree-one.untracked.list: missing") for p in problems), problems
    assert any("untracked.tgz: no worktree-one.untracked.list is listed" in p
               for p in problems), problems


def test_an_unlisted_archive_is_named(fx: Fixture) -> None:
    """A result that carries the tree's work but lists no archive is not clean."""
    wt = fx.worktree()
    (wt / "notes.txt").write_text("notes\n", encoding="utf-8")
    result = salvage(fx, wt)
    assert verify(fx, result) == []

    result.artifacts = []

    problems = verify(fx, result)

    assert problems == [
        "tree worktree-one: untracked content is not archived "
        "(no worktree-one.untracked.tgz is listed)"], problems


# ---------------------------------------------------------------------------
# 7. two trees with one basename keep their own archives
# ---------------------------------------------------------------------------


def test_two_trees_with_one_basename_keep_both_archives(fx: Fixture) -> None:
    first = fx.worktree(name="worktree-one", parent="alpha")
    second = fx.worktree(name="worktree-one", parent="beta")
    (first / "one.txt").write_text("one\n", encoding="utf-8")
    (second / "two.txt").write_text("two\n", encoding="utf-8")

    one = salvage(fx, first)
    two = salvage(fx, second)

    assert one.salvage_dir != two.salvage_dir
    assert archived_names(one) == ["one.txt"]
    assert archived_names(two) == ["two.txt"]
    assert verify(fx, one) == []
    assert verify(fx, two) == []


# ---------------------------------------------------------------------------
# 8. the record separator survives a filename that holds spaces and newlines
# ---------------------------------------------------------------------------


def test_a_filename_with_spaces_and_newlines_round_trips(fx: Fixture) -> None:
    """The list is NUL-separated, so the path field may hold whatever the name
    holds — the record still parses to one member with its own bytes."""
    wt = fx.worktree()
    (wt / "with space.txt").write_text("space\n", encoding="utf-8")
    (wt / "two\nlines.txt").write_text("newline\n", encoding="utf-8")

    result = salvage(fx, wt)

    listed = {record.path: record for record in records_of(result)}
    assert sorted(listed) == ["two\nlines.txt", "with space.txt"], listed
    assert listed["with space.txt"].digest == sha(b"space\n")
    assert archived_names(result) == ["two\nlines.txt", "with space.txt"]
    assert verify(fx, result) == []
