#!/usr/bin/env python3
"""Fold the changelog.d/ fragments into CHANGELOG.md for a release.

Usage: assemble_changelog.py --version X.Y.Z [--date YYYY-MM-DD] [--rev REV] [--check]

Run by the release dispatch on its branch, before the release PR opens, so the
tag the operator cuts after that PR merges contains the assembled CHANGELOG.

A new ``## [X.Y.Z] - <date>`` heading lands directly under ``## [Unreleased]``,
which stays and is left empty.  Under it come the lines that were under
``## [Unreleased]`` (unchanged), then the fragments in the order they entered
the first-parent history of ``--rev`` (default ``origin/main``, which must be an
ancestor of HEAD).  The fragments are deleted.  The fold itself is
``changelog_fragments.assemble``, the same function the CI check runs.

Every precondition is checked before anything is written: a clean
``CHANGELOG.md`` and ``changelog.d/``, an ``## [Unreleased]`` heading, no
``## [X.Y.Z]`` yet, every fragment valid and on the first-parent history of
``--rev``.  A write or a delete that fails puts the original ``CHANGELOG.md``
and every deleted fragment back, byte for byte.  A run killed between the
steps cannot restore itself: the next run refuses the dirty tree and names
``git checkout -- CHANGELOG.md changelog.d``, after which a rerun gives the
same output.  There is no partial-rerun mode.

``--check`` prints the section it would write and the files it would delete,
and changes nothing.  Without a ``changelog.d/`` directory it does nothing.

Exit 0 done (or nothing to do), 1 refused or failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import changelog_fragments as cf
from project_root import resolve_project_root


@dataclass
class Plan:
    """Everything a run writes and deletes, plus the original bytes to restore."""

    root: Path
    new_text: str
    section: str
    names: List[str]
    changelog_original: Tuple[bytes, int]
    fragment_originals: Dict[str, Tuple[bytes, int]] = field(default_factory=dict)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.exit(2, f"{self.prog}: error: {message}\n")


def _read_with_mode(path: Path) -> Tuple[bytes, int]:
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _refuse_dirty_tree(root: Path) -> None:
    status = cf.run_git(root, "status", "--porcelain", "--", cf.CHANGELOG_PATH, cf.FRAGMENT_DIR)
    if status.strip():
        raise cf.ChangelogError(
            f"uncommitted changes in {cf.CHANGELOG_PATH} or {cf.FRAGMENT_DIR}/:\n{status.rstrip()}\n"
            "The assembler reads only the committed tree. If an earlier run was "
            f"interrupted, restore it with `{cf.ROLLBACK_COMMAND}` and run again; "
            "commit or remove an untracked file first."
        )


def _refuse_rev_not_ancestor(root: Path, rev: str) -> None:
    try:
        cf.run_git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    except cf.ChangelogError:
        raise cf.ChangelogError(f"--rev {rev} does not resolve to a commit") from None
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", rev, "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1:
        raise cf.ChangelogError(f"--rev {rev} is not an ancestor of HEAD: merge it first")
    if result.returncode != 0:
        raise cf.ChangelogError(f"git merge-base --is-ancestor {rev} HEAD failed: {result.stderr.strip()}")


def _read_fragments(root: Path, rev: str) -> Dict[str, Tuple[bytes, int]]:
    names = cf.select_fragments(root)
    in_rev = cf.fragment_names_at(root, rev)
    lost = sorted(set(in_rev) - set(names))
    if lost:
        listed = ", ".join(cf.fragment_path(name) for name in lost)
        raise cf.ChangelogError(f"present at {rev} but missing from the tree: {listed}")
    originals = {name: _read_with_mode(root / cf.fragment_path(name)) for name in names}
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
    originals = _read_fragments(root, rev)
    ordered = cf.introduction_order(root, rev, originals)
    fragments = [(name, originals[name][0].decode("utf-8")) for name in ordered]
    new_text, consumed = cf.assemble(changelog_original[0].decode("utf-8"), fragments, version, date)
    return Plan(
        root=root,
        new_text=new_text,
        section=cf.release_section(new_text, version),
        names=consumed,
        changelog_original=changelog_original,
        fragment_originals=originals,
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


def _restore(plan: Plan, changelog_written: bool, deleted: List[str]) -> bool:
    """Put back the original CHANGELOG.md and every deleted fragment; True on success."""
    try:
        if changelog_written:
            data, mode = plan.changelog_original
            _atomic_write(plan.root / cf.CHANGELOG_PATH, data, mode)
        for name in deleted:
            data, mode = plan.fragment_originals[name]
            _atomic_write(plan.root / cf.fragment_path(name), data, mode)
    except OSError as exc:
        print(f"error: restore failed: {exc}; run `{cf.ROLLBACK_COMMAND}`", file=sys.stderr)
        return False
    return True


def apply(plan: Plan) -> int:
    """Write the new CHANGELOG.md, then delete the fragments; all or nothing."""
    changelog_written = False
    deleted: List[str] = []
    try:
        _atomic_write(plan.root / cf.CHANGELOG_PATH, plan.new_text.encode("utf-8"), plan.changelog_original[1])
        changelog_written = True
        for name in plan.names:
            os.remove(plan.root / cf.fragment_path(name))
            deleted.append(name)
    except BaseException as exc:
        restored = _restore(plan, changelog_written, deleted)
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
    print(("would delete:" if check else "deleted:") + ("" if deleted else " nothing"))
    for path in deleted:
        print(f"  {path}")
    if not check:
        print("next: write the intro paragraph under the new heading (prose, no bullets), "
              f"then commit {cf.CHANGELOG_PATH} and {cf.FRAGMENT_DIR}/")


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
        plan = prepare(root, args.version, args.date, args.rev)
    except cf.ChangelogError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if args.check:
        _report(plan, check=True)
        return 0
    status = apply(plan)
    if status == 0:
        _report(plan, check=False)
    return status


if __name__ == "__main__":
    sys.exit(main())
