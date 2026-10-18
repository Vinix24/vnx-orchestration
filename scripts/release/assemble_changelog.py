#!/usr/bin/env python3
"""Fold the changelog.d/ fragments into CHANGELOG.md for a release.

Usage: assemble_changelog.py --version X.Y.Z [--date YYYY-MM-DD] [--rev REV] [--check]

Run by the release dispatch on its branch, before the release PR opens, so the
tag the operator cuts after that PR merges contains the assembled CHANGELOG.

A new ``## [X.Y.Z] - <date>`` heading lands directly under ``## [Unreleased]``,
which stays and is left empty.  Under it come the lines that were under
``## [Unreleased]`` (unchanged), then the fragments in the order they entered
the first-parent history of ``--rev`` (default ``origin/main``, which must be an
ancestor of HEAD).  The fragments are deleted, and so is the grandfathering
list ``scripts/ci/changelog_legacy_prs.txt`` when it exists: the first release
PR after the fragment rule removes it.  The fold itself is
``changelog_fragments.assemble``, the same function the CI check runs.

Every precondition is checked before anything is written: a clean
``CHANGELOG.md`` and ``changelog.d/``, both strict UTF-8 (an invalid byte is
refused by file, line and byte offset), an ``## [Unreleased]`` heading whose
lines are bullets only, no ``## [X.Y.Z]`` yet, every fragment valid, present
at ``--rev`` with the same bytes as in the tree, and on the first-parent
history of ``--rev``.  A write or a delete that fails puts the original
``CHANGELOG.md``, every deleted fragment and the deleted list back, byte for byte.  A run killed
between the steps cannot restore itself: the next run refuses the dirty tree
and names ``git checkout -- CHANGELOG.md changelog.d``, after which a rerun
gives the same output.  There is no partial-rerun mode.

One run at a time: preparation, application and rollback happen under an
exclusive lock on ``<git-dir>/vnx-assemble-changelog.lock``.  A second run on
the same working tree refuses at once and names the lock; without it, a run
that prepared before the first one applied would meet the deleted fragments
and restore the old ``CHANGELOG.md`` over the first run's result.

``--check`` prints the section it would write and the files it would delete,
and changes nothing.  Without a ``changelog.d/`` directory it does nothing.

Exit 0 done (or nothing to do), 1 refused or failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import changelog_fragments as cf
from project_root import resolve_project_root

LOCK_NAME = "vnx-assemble-changelog.lock"


@dataclass
class Plan:
    """Everything a run writes and deletes, plus the original bytes to restore."""

    root: Path
    new_text: str
    section: str
    names: List[str]
    changelog_original: Tuple[bytes, int]
    fragment_originals: Dict[str, Tuple[bytes, int]] = field(default_factory=dict)
    legacy_original: Optional[Tuple[bytes, int]] = None


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.exit(2, f"{self.prog}: error: {message}\n")


def _read_with_mode(path: Path) -> Tuple[bytes, int]:
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _refuse_dirty_tree(root: Path) -> None:
    status = cf.run_git(root, "status", "--porcelain", "--", cf.CHANGELOG_PATH, cf.FRAGMENT_DIR,
                        cf.LEGACY_LIST_PATH)
    if status.strip():
        raise cf.ChangelogError(
            f"uncommitted changes in {cf.CHANGELOG_PATH}, {cf.FRAGMENT_DIR}/ or {cf.LEGACY_LIST_PATH}:"
            f"\n{status.rstrip()}\n"
            "The assembler reads only the committed tree. If an earlier run was "
            f"interrupted, restore it with `{_rollback_command(root)}` and run again; "
            "commit or remove an untracked file first."
        )


def _rollback_command(root: Path) -> str:
    """The restore command, naming the legacy list only when the tree tracks it."""
    tracked = cf.run_git(root, "ls-files", "--", cf.LEGACY_LIST_PATH).strip()
    return f"{cf.ROLLBACK_COMMAND} {cf.LEGACY_LIST_PATH}" if tracked else cf.ROLLBACK_COMMAND


def _refuse_rev_not_ancestor(root: Path, rev: str) -> None:
    try:
        cf.run_git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    except cf.ChangelogError:
        raise cf.ChangelogError(f"--rev {rev} does not resolve to a commit") from None
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", rev, "HEAD"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 1:
        raise cf.ChangelogError(f"--rev {rev} is not an ancestor of HEAD: merge it first")
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise cf.ChangelogError(f"git merge-base --is-ancestor {rev} HEAD failed: {stderr}")


@contextlib.contextmanager
def repo_lock(root: Path) -> Iterator[Path]:
    """Hold the exclusive assembler lock in the git dir of *root* for one run.

    Non-blocking: a second run refuses at once and names the lock rather than
    waiting and then working from a tree the first run already changed.
    Closing the descriptor releases the lock, also when the process dies.
    """
    git_dir = cf.run_git_bytes(root, "rev-parse", "--absolute-git-dir").rstrip(b"\n")
    lock_path = Path(os.fsdecode(git_dir)) / LOCK_NAME
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        raise cf.ChangelogError(f"cannot open the assembler lock {lock_path}: {exc}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise cf.ChangelogError(
                f"another assemble_changelog.py run holds the lock {lock_path}; "
                "let it finish, then check the tree and run again"
            ) from None
        yield lock_path
    finally:
        os.close(fd)


def _refuse_not_at_rev(root: Path, rev: str, originals: Dict[str, Tuple[bytes, int]]) -> None:
    """Refuse every fragment whose tree bytes are not the blob at *rev*.

    A name that once landed on *rev* says nothing about these bytes: a merged
    fragment deleted on *rev* and recreated on the release branch, or one
    edited there, is found in the history but never landed as it reads now.
    """
    entries = cf.tree_entries(root, rev, cf.FRAGMENT_DIR)
    absent, differs = [], []
    for name, (data, _mode) in sorted(originals.items()):
        entry = entries.get(cf.fragment_path(name))
        if entry is None or not cf.is_regular_blob(entry):
            absent.append(cf.fragment_path(name))
        elif cf.run_git_bytes(root, "cat-file", "blob", entry[2]) != data:
            differs.append(cf.fragment_path(name))
    problems = []
    if absent:
        problems.append(f"not in {rev}: {', '.join(absent)} "
                        f"(a fragment must be merged into {rev} before a release assembles it)")
    if differs:
        problems.append(f"differs from the blob at {rev}: {', '.join(differs)} "
                        "(a release assembles a fragment as it landed; change it in its own PR)")
    if problems:
        raise cf.ChangelogError("\n".join(problems))


def _read_fragments(root: Path, rev: str) -> Dict[str, Tuple[bytes, int]]:
    names = cf.select_fragments(root)
    in_rev = cf.fragment_names_at(root, rev)
    lost = sorted(set(in_rev) - set(names))
    if lost:
        listed = ", ".join(cf.fragment_path(name) for name in lost)
        raise cf.ChangelogError(f"present at {rev} but missing from the tree: {listed}")
    originals = {name: _read_with_mode(root / cf.fragment_path(name)) for name in names}
    _refuse_not_at_rev(root, rev, originals)
    reasons = [cf.validate_fragment(name, data) for name, (data, _mode) in originals.items()]
    reasons = [reason for reason in reasons if reason]
    if reasons:
        raise cf.ChangelogError("invalid fragment(s):\n  " + "\n  ".join(reasons))
    return originals


def prepare(root: Path, version: str, date: str, rev: str) -> Plan:
    """Check every precondition and compute the result; writes nothing."""
    _refuse_dirty_tree(root)
    _refuse_rev_not_ancestor(root, rev)
    changelog = root / cf.CHANGELOG_PATH
    if not changelog.is_file():
        raise cf.ChangelogError(f"{cf.CHANGELOG_PATH} not found in {root}")
    changelog_original = _read_with_mode(changelog)
    changelog_text = cf.decode_utf8(cf.CHANGELOG_PATH, changelog_original[0])
    originals = _read_fragments(root, rev)
    ordered = cf.introduction_order(root, rev, originals)
    fragments = [(name, cf.decode_utf8(cf.fragment_path(name), originals[name][0])) for name in ordered]
    new_text, consumed = cf.assemble(changelog_text, fragments, version, date)
    legacy = root / cf.LEGACY_LIST_PATH
    return Plan(
        root=root,
        new_text=new_text,
        section=cf.release_section(new_text, version),
        names=consumed,
        changelog_original=changelog_original,
        fragment_originals=originals,
        legacy_original=_read_with_mode(legacy) if legacy.is_file() else None,
    )


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _restore(plan: Plan, changelog_written: bool, deleted: List[str], legacy_deleted: bool = False) -> bool:
    """Put back the original CHANGELOG.md, every deleted fragment and the deleted
    legacy list; True on success."""
    try:
        if changelog_written:
            data, mode = plan.changelog_original
            _atomic_write(plan.root / cf.CHANGELOG_PATH, data, mode)
        for name in deleted:
            data, mode = plan.fragment_originals[name]
            _atomic_write(plan.root / cf.fragment_path(name), data, mode)
        if legacy_deleted and plan.legacy_original is not None:
            data, mode = plan.legacy_original
            _atomic_write(plan.root / cf.LEGACY_LIST_PATH, data, mode)
    except OSError as exc:
        print(f"error: restore failed: {exc}; run `{_rollback_command(plan.root)}`", file=sys.stderr)
        return False
    return True


def apply(plan: Plan) -> int:
    """Write the new CHANGELOG.md, then delete the fragments and the legacy list; all or nothing."""
    changelog_written = False
    legacy_deleted = False
    deleted: List[str] = []
    try:
        _atomic_write(plan.root / cf.CHANGELOG_PATH, plan.new_text.encode("utf-8"), plan.changelog_original[1])
        changelog_written = True
        for name in plan.names:
            os.remove(plan.root / cf.fragment_path(name))
            deleted.append(name)
        if plan.legacy_original is not None:
            os.remove(plan.root / cf.LEGACY_LIST_PATH)
            legacy_deleted = True
    except BaseException as exc:
        restored = _restore(plan, changelog_written, deleted, legacy_deleted)
        if not isinstance(exc, OSError):
            raise
        state = "restored the original files" if restored else "could not restore"
        print(f"error: {exc}; {state}", file=sys.stderr)
        return 1
    return 0


def _report(plan: Plan, check: bool) -> None:
    verb = "would write" if check else "wrote"
    print(f"{verb} to {cf.CHANGELOG_PATH}:\n{plan.section}")
    deleted = [cf.fragment_path(name) for name in plan.names]
    if plan.legacy_original is not None:
        deleted.append(cf.LEGACY_LIST_PATH)
    print(("would delete:" if check else "deleted:") + ("" if deleted else " nothing"))
    for path in deleted:
        print(f"  {path}")
    if not check:
        print("next: write the intro paragraph under the new heading (prose, no bullets), "
              f"then commit {cf.CHANGELOG_PATH}, {cf.FRAGMENT_DIR}/ and the removed files")


def main(argv: List[str] | None = None) -> int:
    parser = _Parser(description="Fold changelog.d/ fragments into CHANGELOG.md")
    parser.add_argument("--version", required=True, help="release version X.Y.Z")
    parser.add_argument("--date", default=datetime.date.today().isoformat(), help="release date YYYY-MM-DD")
    parser.add_argument("--rev", default="origin/main", help="input revision, an ancestor of HEAD")
    parser.add_argument("--check", action="store_true", help="print the result, change nothing")
    args = parser.parse_args(argv)
    if not cf.is_valid_version(args.version):
        parser.error(f"--version {args.version!r} is not X.Y.Z")
    if not cf.is_valid_date(args.date):
        parser.error(f"--date {args.date!r} is not YYYY-MM-DD")
    try:
        root = resolve_project_root()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not (root / cf.FRAGMENT_DIR).is_dir():
        print(f"no {cf.FRAGMENT_DIR}/ directory: nothing to assemble")
        return 0
    try:
        with repo_lock(root):
            plan = prepare(root, args.version, args.date, args.rev)
            status = 0 if args.check else apply(plan)
    except cf.ChangelogError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if status == 0:
        _report(plan, check=args.check)
    return status


if __name__ == "__main__":
    sys.exit(main())
