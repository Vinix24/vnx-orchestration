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

A git command that FAILS is neither "no drift" nor "skipped". Every call goes
through ``_git``, the module's only subprocess call site: it returns the answer,
returns the documented absence of one, or raises ``MeasurementFailed`` with the
command and its stderr. ``measure`` reports that as ``MEASURE_FAILED`` so the
doctor check blocks instead of passing on an unmeasured release.
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
MEASURE_FAILED = "measure-failed"

#: Statuses the doctor check may PASS on: level, or a documented skip. Every
#: other value — BEHIND, MEASURE_FAILED, and any status a newer module adds that
#: this doctor does not know — blocks.
NON_BLOCKING_STATUSES = (UP_TO_DATE, NOT_A_CHECKOUT, NO_MAIN_REF, UNKNOWN_RELEASE)

MAIN_REF_CANDIDATES = ("origin/main", "main")
_NOT_A_REPO = "not a git repository"
_REC = "\x1e"
_FIELD = "\x1f"


class MeasurementFailed(Exception):
    """A git command that had to answer did not; the message names it and its stderr."""


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
    error: str = ""


@dataclass(frozen=True)
class GitCall:
    """One git invocation: its answer, or the documented absence of one."""

    ok: bool
    output: str = ""
    absent: bool = False
    command: str = ""

    @property
    def text(self) -> str:
        return self.output.strip()


def _git(
    repo: Path, *args: str, absent_stderr: tuple[str, ...] = (), quiet_absent: bool = False
) -> GitCall:
    """Run one read-only git command: its output, or ``absent``, or raise.

    The module's single subprocess call site, so no caller can mistake a failed
    command for a legitimate answer. Exit 0 returns the output — an empty output
    is a real empty answer (no tag points at HEAD, no commits between two refs),
    never "the ref is missing". A caller declares the answers that ARE "nothing
    here": ``quiet_absent`` for a ``--verify --quiet`` existence probe (git exits
    non-zero and says nothing) and ``absent_stderr`` for a documented message
    such as ``not a git repository``. Every other non-zero exit, and a git that
    cannot be launched, raises ``MeasurementFailed`` with the command and stderr.
    """
    label = f"git -C {repo} " + " ".join(args)
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise MeasurementFailed(f"`{label}` could not run: {exc}") from exc
    if proc.returncode == 0:
        return GitCall(True, proc.stdout or "", command=label)
    stderr = (proc.stderr or "").strip()
    if (quiet_absent and not stderr) or any(marker in stderr for marker in absent_stderr):
        return GitCall(False, absent=True, command=label)
    raise MeasurementFailed(f"`{label}` exited {proc.returncode}: {stderr or '(no stderr)'}")


def _sha(call: GitCall, what: str) -> str:
    """The sha a successful call printed; a silent or absent answer is a failure."""
    if not call.ok or not call.text:
        raise MeasurementFailed(f"`{call.command}` did not resolve {what}")
    return call.text


def _ref_commit(repo: Path, name: str) -> GitCall:
    """The commit ``name`` resolves to, or ``absent`` when there is no such ref."""
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}", quiet_absent=True)


def is_git_checkout(path: Path) -> bool:
    """True only on positive evidence: git answered "inside a work tree".

    A path that is not a repository is ``False``; a git that cannot answer is a
    ``MeasurementFailed``, not a "not a checkout".
    """
    repo = Path(path).expanduser()
    try:
        if not repo.is_dir():
            return False
    except OSError:
        return False
    call = _git(repo, "rev-parse", "--is-inside-work-tree", absent_stderr=(_NOT_A_REPO,))
    return call.ok and call.text == "true"


def release_ref(release_dir: Path) -> str:
    """The ref the release dir runs: its exact tag, else the short HEAD sha.

    Raises ``MeasurementFailed`` when git cannot name it — an unnamed release is
    not comparable and must not fall back to a made-up label.
    """
    repo = Path(release_dir).expanduser()
    tags = _git(repo, "tag", "--points-at", "HEAD")
    if not tags.ok:
        raise MeasurementFailed(f"`{tags.command}` did not list the tags at HEAD")
    names = [line.strip() for line in tags.output.splitlines() if line.strip()]
    if names:
        return names[0]
    return _sha(_git(repo, "rev-parse", "--short=7", "HEAD"), "HEAD")


def _main_ref(repo: Path) -> tuple[str, str] | None:
    """The first main ref this checkout knows, or ``None`` when it knows none."""
    for name in MAIN_REF_CANDIDATES:
        found = _ref_commit(repo, name)
        if not found.ok:
            continue  # this checkout has no such ref: try the next candidate name
        return name, _sha(found, f"the {name} ref")
    return None


def _count(call: GitCall) -> int:
    """The commit count ``rev-list --count`` printed; silence is not zero."""
    if not call.ok or not call.text.isdigit():
        found = call.text or "no output"
        raise MeasurementFailed(f"`{call.command}` reported no commit count (got {found!r})")
    return int(call.text)


def _commits(repo: Path, release_sha: str, main_sha: str) -> tuple[DriftCommit, ...]:
    log = _git(
        repo, "log", f"--format={_REC}%H{_FIELD}%s", "--name-only", main_sha, f"^{release_sha}"
    )
    if not log.ok:
        raise MeasurementFailed(f"`{log.command}` did not list the missing commits")
    commits = []
    for chunk in log.output.split(_REC):
        header, _, names = chunk.strip("\n").partition("\n")
        if not header:
            continue
        sha, _, subject = header.partition(_FIELD)
        paths = tuple(line.strip() for line in names.splitlines() if line.strip())
        commits.append(DriftCommit(sha.strip(), subject.strip(), paths))
    return tuple(commits)


def _measure(release_dir: Path, main_repo: Path | None) -> ReleaseDrift:
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
        if not is_git_checkout(repo):
            continue
        found = _main_ref(repo)
        if found is None:
            continue
        saw_main = True
        main_name, main_sha = found
        release_here = _ref_commit(repo, release)
        if not release_here.ok:
            continue  # legitimately absent here: the other checkout may know it
        release_sha = _sha(release_here, f"release {release}")
        behind = _count(_git(repo, "rev-list", "--count", f"{release_sha}..{main_sha}"))
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


def measure(release_dir: Path, main_repo: Path | None = None) -> ReleaseDrift:
    """Compare the release in ``release_dir`` with a main ref in the same or a
    second checkout (``main_repo``, which must know the release ref).

    A git command that fails — ``rev-list``, a ``log``, an unlaunchable git —
    yields ``MEASURE_FAILED`` with the command and its stderr, never a skip and
    never "level": the doctor check blocks on it.
    """
    release_dir = Path(release_dir).expanduser()
    try:
        return _measure(release_dir, main_repo)
    except MeasurementFailed as exc:
        return ReleaseDrift(MEASURE_FAILED, error=str(exc), looked_in=str(release_dir))


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
    if drift.status == MEASURE_FAILED:
        return f"measurement failed in {drift.looked_in}: {drift.error}"
    return f"release {drift.release} could not be measured ({drift.status})"
