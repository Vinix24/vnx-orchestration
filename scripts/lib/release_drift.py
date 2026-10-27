#!/usr/bin/env python3
"""release_drift — how far the running release lags ``main``, measured locally.

``~/.vnx-system/current`` follows a tag while ``main`` moves on, and nothing
measured that gap: ``install:mode`` compares a project's pin with ``current``,
and ``vnx update`` only measures at a cutover. This module measures it: the
commits on a main ref that the release lacks, and the paths they touch. Never
fetches, never writes — a checkout with no main ref is reported as
skipped-with-a-reason, never as "no drift". The main ref may have to come from
a second checkout that knows the release ref (a materialized release dir has no
remote-tracking refs at all).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

UP_TO_DATE = "up-to-date"
BEHIND = "behind"
NOT_A_CHECKOUT = "not-a-git-checkout"
NO_MAIN_REF = "no-main-ref"
UNKNOWN_RELEASE = "release-ref-unknown"

MAIN_REF_CANDIDATES = ("origin/main", "main")
_REC = "\x1e"
_FIELD = "\x1f"


@dataclass(frozen=True)
class DriftCommit:
    sha: str
    subject: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseDrift:
    status: str
    release: str = ""
    main: str = ""
    behind: int = 0
    commits: tuple[DriftCommit, ...] = ()
    looked_in: str = ""


def _git(repo: Path, *args: str) -> str | None:
    """Run one read-only git command; ``None`` when git or the ref fails."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def is_git_checkout(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    return (_git(path, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def release_ref(release_dir: Path) -> str:
    """The ref the release dir runs: its exact tag, else the short HEAD sha."""
    tag = (_git(release_dir, "describe", "--tags", "--exact-match", "HEAD") or "").strip()
    if tag:
        return tag
    return (_git(release_dir, "rev-parse", "--short=7", "HEAD") or "").strip() or "HEAD"


def _main_ref(repo: Path) -> tuple[str, str] | None:
    for name in MAIN_REF_CANDIDATES:
        sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        if sha and sha.strip():
            return name, sha.strip()
    return None


def _commits(repo: Path, release_sha: str, main_sha: str) -> tuple[DriftCommit, ...]:
    out = _git(
        repo, "log", f"--format={_REC}%H{_FIELD}%s", "--name-only", main_sha, f"^{release_sha}"
    )
    commits = []
    for chunk in (out or "").split(_REC):
        header, _, names = chunk.strip("\n").partition("\n")
        if not header:
            continue
        sha, _, subject = header.partition(_FIELD)
        paths = tuple(line.strip() for line in names.splitlines() if line.strip())
        commits.append(DriftCommit(sha.strip(), subject.strip(), paths))
    return tuple(commits)


def measure(release_dir: Path, main_repo: Path | None = None) -> ReleaseDrift:
    """Compare the release in ``release_dir`` with a main ref in the same or a
    second checkout (``main_repo``, which must know the release ref)."""
    release_dir = Path(release_dir).expanduser()
    if not is_git_checkout(release_dir):
        return ReleaseDrift(NOT_A_CHECKOUT, release=str(release_dir))
    release = release_ref(release_dir)
    repos = [release_dir]
    if main_repo is not None:
        extra = Path(main_repo).expanduser()
        if extra.resolve() != release_dir.resolve():
            repos.append(extra)

    saw_main = False
    for repo in repos:
        found = _main_ref(repo) if is_git_checkout(repo) else None
        if found is None:
            continue
        saw_main = True
        main_name, main_sha = found
        release_sha = (_git(repo, "rev-parse", "--verify", "--quiet", f"{release}^{{commit}}") or "").strip()
        if not release_sha:
            continue
        raw = (_git(repo, "rev-list", "--count", f"{release_sha}..{main_sha}") or "").strip()
        if not raw.isdigit():  # fail-closed: an unparsable count is not "level"
            continue
        behind = int(raw)
        return ReleaseDrift(
            UP_TO_DATE if behind == 0 else BEHIND,
            release=release,
            main=main_name,
            behind=behind,
            commits=_commits(repo, release_sha, main_sha) if behind else (),
            looked_in=str(repo),
        )

    return ReleaseDrift(
        UNKNOWN_RELEASE if saw_main else NO_MAIN_REF, release=release, looked_in=str(release_dir)
    )


def render_detail(drift: ReleaseDrift, commit_limit: int = 3) -> str:
    """One doctor line: the finding and its evidence, or the reason for a skip."""
    if drift.status == UP_TO_DATE:
        return f"release {drift.release} is level with {drift.main} (0 commits there are missing from it)"
    if drift.status == BEHIND:
        parts = []
        for commit in drift.commits[:commit_limit]:
            shown = ", ".join(commit.paths[:3]) or "(no paths)"
            extra = len(commit.paths) - 3
            parts.append(f"{commit.sha[:7]} {commit.subject} -> {shown}{f' (+{extra} more)' if extra > 0 else ''}")
        hidden = drift.behind - len(drift.commits[:commit_limit])
        tail = f"; +{hidden} more commit{'s' if hidden != 1 else ''}" if hidden > 0 else ""
        word = "commit" if drift.behind == 1 else "commits"
        return f"release {drift.release} is {drift.behind} {word} behind {drift.main} in {drift.looked_in}: " \
            + "; ".join(parts) + tail
    if drift.status == NOT_A_CHECKOUT:
        return f"skipped: {drift.release} is not a git checkout — there is no release/main comparison to make"
    if drift.status == NO_MAIN_REF:
        return f"skipped: no {' or '.join(MAIN_REF_CANDIDATES)} ref in {drift.looked_in} — this check " \
            f"never fetches, so release {drift.release} cannot be compared with main here"
    if drift.status == UNKNOWN_RELEASE:
        return f"skipped: release {drift.release} is unknown to {drift.looked_in}, whose main ref it would be compared against"
    return f"skipped: release {drift.release} could not be measured ({drift.status})"
