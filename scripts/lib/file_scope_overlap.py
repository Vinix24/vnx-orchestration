"""file_scope_overlap.py — warn when two open dispatch branches touch the same files.

OI-1091: nothing prevented two concurrent dispatches from claiming the same files. Three
dispatches collided in ONE worktree and a sibling reset another's branch pointer (OI-1232).
This module detects that overlap at the merge chokepoint and WARNS. It never blocks: a merge
still proceeds, but T0 sees the collision before it lands and can reconcile the two tracks.

Shape of the check:
  - ``open_dispatch_branches`` enumerates remote ``dispatch/*`` branches that are NOT yet
    merged into the base ref (an already-merged-but-not-deleted branch is not "open", so it
    cannot produce a spurious warning).
  - ``changed_files`` is the ``git diff --name-only`` of a branch against the base ref.
  - ``find_overlaps`` intersects the merging branch's files with every other open dispatch
    branch and returns the overlaps; ``warn_overlaps`` formats + emits them.

Everything is best-effort: any git/network failure degrades to "no overlaps found" and a debug
log line, never an exception that blocks the merge.

OI-1641: on the live remote (370 dispatch branches) the original implementation did ~715 serial
`git fetch` calls (one per branch, twice for the merging branch's counterparts) at ~0.59s each —
~7 minutes of silence on every merge, with zero progress output. It also treated
``git merge-base --is-ancestor`` as the only "is this merged" signal, but VNX squashes every PR,
so a squash-merged branch's tip is never main's ancestor and stayed "open" forever (345/370
measured as open, only 25 recognized). Three fixes:
  1. ``_is_merged`` adds two more layers after the cheap ancestry check: membership in a bulk
     ``gh pr list --state merged`` lookup (strongest evidence for a squash-merge), then a
     patch-equivalence fallback (the branch's files now diff empty against main) for when gh is
     unavailable or found no PR.
  2. One bulk ``git fetch`` for all ``dispatch/*`` refs replaces the per-branch fetches, and one
     bulk ``gh pr list`` replaces what would otherwise be a per-branch gh query — a per-branch
     ``gh pr list --head <branch>`` is itself a serial network call and measured 2026-09-06 at
     ~0.4s/branch, i.e. almost the exact stall the bulk git fetch was meant to eliminate.
  3. ``_run`` takes a timeout, and the branch scan itself is bounded by both a branch-count
     ceiling and a wall-clock ceiling — exceeding either aborts the scan and returns ``[]`` with
     a stderr warning, per the "never blocks a merge" contract above.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

_LOG = logging.getLogger(__name__)

DEFAULT_BASE_REF = "origin/main"

# Per-command timeout (seconds). No single git/gh invocation in this module may hang past this.
DEFAULT_RUN_TIMEOUT = 30.0

# Scan ceilings: exceeding either aborts the whole scan and returns [] (best-effort, never
# blocks a merge). max_branches is checked BEFORE any git call so a runaway branch count costs
# zero network round-trips; max_scan_seconds is checked as the per-branch merge-status loop runs.
# max_branches is a sanity ceiling against a runaway branch count, not the normal operating
# limit: measured 2026-09-06 the live remote already carries 358 dispatch branches (cleanup
# lags creation) and the bulk-fetch+bulk-gh scan covers all of them in ~17s, so a low ceiling
# here would dead-zone the whole feature on every merge until cleanup catches up — the wall-clock
# ceiling below is the real circuit breaker.
DEFAULT_MAX_BRANCHES = 1000
DEFAULT_MAX_SCAN_SECONDS = 60.0

# Progress line cadence on stderr during the merge-status scan.
_PROGRESS_EVERY_N_BRANCHES = 25
_PROGRESS_EVERY_SECONDS = 5.0

# Ceiling for the bulk `gh pr list --state merged` lookup. A growing repo needs headroom;
# gate_obligation_retire_backlog.py measured 1537 merged PRs on this repo 2026-08-23, so 5000
# leaves ample margin without needing pagination.
_GH_LIST_LIMIT = 5000

# A dispatch branch is a remote ref under refs/heads/dispatch/. A fix-forward branch keeps the
# same shape (it is the parent's dispatch/<id> branch), so the dispatch-id is the last path
# component after the dispatch/ prefix.
_DISPATCH_PREFIX = "dispatch/"


def dispatch_id_from_branch(branch: str) -> Optional[str]:
    """Extract the dispatch-id from a branch name, or None when it is not a dispatch branch.

    Accepts a bare name (``dispatch/20260815-foo``) or a prefixed one (``origin/...`` /
    ``refs/heads/...``). Returns None for a non-dispatch branch (e.g. ``main``).
    """
    name = (branch or "").strip()
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if not name.startswith(_DISPATCH_PREFIX):
        return None
    rest = name[len(_DISPATCH_PREFIX):].strip("/")
    return rest or None


def _run(
    cmd: list[str],
    *,
    repo: Optional[Path],
    check: bool = True,
    timeout: float = DEFAULT_RUN_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd``, never letting it hang past ``timeout``.

    A timeout degrades to a failed-but-benign CompletedProcess (returncode 124, like the shell
    convention for a timed-out command) instead of raising — every caller in this module already
    treats a non-zero returncode as "no signal, move on", so a slow git/gh call now costs at most
    ``timeout`` seconds instead of hanging the merge indefinitely.
    """
    try:
        return subprocess.run(
            cmd,
            cwd=str(repo) if repo else None,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _LOG.warning("file_scope_overlap: command timed out after %ss: %s", timeout, " ".join(cmd))
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="timeout")


def _list_remote_dispatch_branches(
    repo: Optional[Path], *, run_timeout: float = DEFAULT_RUN_TIMEOUT
) -> list[str]:
    """Return remote ``dispatch/*`` branch names (bare, no origin/ prefix) from the remote.

    Uses ``git ls-remote --heads origin`` so the result reflects what is actually pushed, not a
    possibly-stale local remote-tracking view.
    """
    proc = _run(
        ["git", "ls-remote", "--heads", "origin", "refs/heads/dispatch/*"],
        repo=repo, check=False, timeout=run_timeout,
    )
    if proc.returncode != 0:
        return []
    branches: list[str] = []
    for line in proc.stdout.splitlines():
        # "<sha>\trefs/heads/dispatch/<id>" — the ref column is after the first tab.
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        ref = parts[1].strip()
        if ref.startswith("refs/heads/"):
            branches.append(ref[len("refs/heads/"):])
    return branches


def _fetch_all_dispatch_branches(repo: Optional[Path], *, timeout: float = DEFAULT_RUN_TIMEOUT) -> None:
    """One fetch for every remote ``dispatch/*`` branch — replaces N per-branch fetches.

    Best-effort: on failure, callers proceed with whatever remote-tracking refs already exist
    locally (same degrade-to-no-signal behavior as every other call in this module).
    """
    _run(
        ["git", "fetch", "origin", "--prune", "+refs/heads/dispatch/*:refs/remotes/origin/dispatch/*"],
        repo=repo, check=False, timeout=timeout,
    )


def changed_files(
    branch: str,
    *,
    base_ref: str = DEFAULT_BASE_REF,
    repo: Optional[Path] = None,
    fetch: bool = True,
    run_timeout: float = DEFAULT_RUN_TIMEOUT,
) -> set[str]:
    """Return the files ``branch`` changed relative to ``base_ref`` (``git diff --name-only``).

    Fetches the branch first (unless ``fetch=False``, for a caller that already did a bulk fetch
    covering this ref) so the comparison is against the PUSHED tip, not a stale local ref. Empty
    set on any failure (a missing branch, a bad ref, no network).
    """
    if fetch:
        _run(["git", "fetch", "origin", branch], repo=repo, check=False, timeout=run_timeout)
    merge_base = _run(
        ["git", "merge-base", base_ref, f"origin/{branch}"], repo=repo, check=False, timeout=run_timeout,
    ).stdout.strip()
    if not merge_base:
        return set()
    diff = _run(
        ["git", "diff", "--name-only", f"{merge_base}..origin/{branch}"], repo=repo, check=False, timeout=run_timeout,
    ).stdout
    return {line for line in diff.splitlines() if line.strip()}


def _fetch_merged_pr_branches(repo: Optional[Path], *, run_timeout: float = DEFAULT_RUN_TIMEOUT) -> set[str]:
    """Bulk ``gh pr list --state merged``, once, keyed by head branch name.

    One gh call instead of one per branch: a `gh pr list --head <branch>` query is itself a
    serial network round-trip, and a per-branch version of this check measured 2026-09-06 at
    ~0.4s/branch on the live remote — reintroducing almost the exact stall the bulk git fetch
    above was meant to eliminate. Same bulk-list-then-lookup shape as
    ``gate_obligation_retire_backlog.py::fetch_prs``. Empty set on any failure (gh missing, not
    authenticated, bad JSON, timeout) — every caller here treats "branch not in the set" as
    inconclusive and falls through to patch-equivalence, so a total gh failure degrades exactly
    like "gh found no merged PR for any branch", never a false negative.
    """
    try:
        proc = _run(
            ["gh", "pr", "list", "--state", "merged", "--json", "headRefName", "--limit", str(_GH_LIST_LIMIT)],
            repo=repo, check=False, timeout=run_timeout,
        )
    except OSError:
        return set()
    if proc.returncode != 0:
        return set()
    try:
        data = json.loads(proc.stdout or "[]")
    except ValueError:
        return set()
    if not isinstance(data, list):
        return set()
    return {item["headRefName"] for item in data if isinstance(item, dict) and item.get("headRefName")}


def _patch_equivalent(
    branch: str, *, base_ref: str = DEFAULT_BASE_REF, repo: Optional[Path], run_timeout: float = DEFAULT_RUN_TIMEOUT,
) -> bool:
    """True when the files ``branch`` touched now diff empty against ``base_ref``.

    This is the squash-merge signature: a squash commit on main applies the same net change to
    the same files, so after the squash the branch tip and main agree on those files' content
    even though the branch tip is not an ancestor of main. An empty-diff branch (no files) is NOT
    treated as equivalent here — ``_is_merged``'s ancestry check already covers that case, and a
    branch with zero changed files but a divergent tip is ambiguous, not evidence of a merge.
    """
    files = changed_files(branch, base_ref=base_ref, repo=repo, fetch=False, run_timeout=run_timeout)
    if not files:
        return False
    diff = _run(
        ["git", "diff", f"origin/{branch}", base_ref, "--", *sorted(files)],
        repo=repo, check=False, timeout=run_timeout,
    ).stdout
    return diff.strip() == ""


def _is_merged(
    branch: str,
    *,
    base_ref: str = DEFAULT_BASE_REF,
    repo: Optional[Path] = None,
    run_timeout: float = DEFAULT_RUN_TIMEOUT,
    merged_pr_branches: Optional[set[str]] = None,
) -> bool:
    """True when ``branch`` is already reflected in ``base_ref``, squash-merges included.

    Three layers, cheapest and most certain first:
      1. Ancestry (``merge-base --is-ancestor``) — a real fast-forward/merge-commit history.
      2. Membership in ``merged_pr_branches`` — the strongest evidence for a squash-merge.
      3. Patch-equivalence — no PR evidence, but the branch's files already diff empty against
         base_ref (the squash landed the same content another way).

    ``merged_pr_branches`` is the result of :func:`_fetch_merged_pr_branches`. A caller scanning
    many branches (``open_dispatch_branches``) fetches it ONCE and passes it in here to avoid a
    gh call per branch; a caller checking a single branch (tests, ad-hoc CLI use) can omit it and
    this function fetches it itself.
    """
    proc = _run(
        ["git", "merge-base", "--is-ancestor", f"origin/{branch}", base_ref],
        repo=repo, check=False, timeout=run_timeout,
    )
    if proc.returncode == 0:
        return True
    if merged_pr_branches is None:
        merged_pr_branches = _fetch_merged_pr_branches(repo, run_timeout=run_timeout)
    if branch in merged_pr_branches:
        return True
    return _patch_equivalent(branch, base_ref=base_ref, repo=repo, run_timeout=run_timeout)


def open_dispatch_branches(
    *,
    base_ref: str = DEFAULT_BASE_REF,
    repo: Optional[Path] = None,
    max_branches: int = DEFAULT_MAX_BRANCHES,
    max_scan_seconds: float = DEFAULT_MAX_SCAN_SECONDS,
    run_timeout: float = DEFAULT_RUN_TIMEOUT,
    stream: Optional[Any] = None,
) -> list[str]:
    """Remote ``dispatch/*`` branches that are not yet merged into ``base_ref``.

    An already-merged branch (merged but its remote ref not deleted) is excluded: its files are
    already on the base, so it cannot collide with a new merge.

    Bounded on both axes so this can never turn back into the OI-1641 7-minute stall: a branch
    count above ``max_branches`` skips the scan entirely (zero git calls), and a scan running
    past ``max_scan_seconds`` aborts mid-loop. Either case returns ``[]`` with a stderr warning —
    "no overlaps found" is the documented best-effort degrade, never a block.
    """
    out = sys.stderr if stream is None else stream
    branches = _list_remote_dispatch_branches(repo, run_timeout=run_timeout)
    total = len(branches)
    if total > max_branches:
        message = (
            f"WARN: file-scope overlap scan skipped — {total} remote dispatch branches exceeds "
            f"the {max_branches}-branch limit; returning no overlaps (best-effort, never blocks a merge)"
        )
        print(message, file=out)
        _LOG.warning("file_scope_overlap: %s", message)
        return []

    _fetch_all_dispatch_branches(repo, timeout=run_timeout)
    merged_pr_branches = _fetch_merged_pr_branches(repo, run_timeout=run_timeout)

    start = time.monotonic()
    last_progress = start
    open_branches: list[str] = []
    for i, branch in enumerate(branches, start=1):
        elapsed = time.monotonic() - start
        if elapsed > max_scan_seconds:
            message = (
                f"WARN: file-scope overlap scan aborted after {elapsed:.1f}s (limit "
                f"{max_scan_seconds}s) at {i - 1}/{total} branches; returning no overlaps "
                f"(best-effort, never blocks a merge)"
            )
            print(message, file=out)
            _LOG.warning("file_scope_overlap: %s", message)
            return []
        if not _is_merged(
            branch, base_ref=base_ref, repo=repo, run_timeout=run_timeout,
            merged_pr_branches=merged_pr_branches,
        ):
            open_branches.append(branch)
        now = time.monotonic()
        if i % _PROGRESS_EVERY_N_BRANCHES == 0 or (now - last_progress) >= _PROGRESS_EVERY_SECONDS:
            print(f"[overlap] {i}/{total} branches, {now - start:.1f}s", file=out)
            last_progress = now
    return open_branches


def find_overlaps(
    merging_branch: str,
    *,
    base_ref: str = DEFAULT_BASE_REF,
    repo: Optional[Path] = None,
    max_branches: int = DEFAULT_MAX_BRANCHES,
    max_scan_seconds: float = DEFAULT_MAX_SCAN_SECONDS,
    run_timeout: float = DEFAULT_RUN_TIMEOUT,
    stream: Optional[Any] = None,
) -> list[tuple[str, list[str]]]:
    """Return [(other_dispatch_id, [overlapping files])] for the branch being merged.

    ``merging_branch`` is the PR head branch (bare name, e.g. ``dispatch/20260815-foo``). Each
    entry names the OTHER open dispatch whose files intersect, with the shared files listed.
    Best-effort: on any failure returns [].
    """
    try:
        mine = changed_files(merging_branch, base_ref=base_ref, repo=repo, run_timeout=run_timeout)
        if not mine:
            return []
        overlaps: list[tuple[str, list[str]]] = []
        others = open_dispatch_branches(
            base_ref=base_ref, repo=repo, max_branches=max_branches,
            max_scan_seconds=max_scan_seconds, run_timeout=run_timeout, stream=stream,
        )
        for other in others:
            if other == merging_branch:
                continue
            other_id = dispatch_id_from_branch(other)
            if not other_id:
                continue
            # open_dispatch_branches already did the bulk fetch — no per-branch fetch here.
            theirs = changed_files(other, base_ref=base_ref, repo=repo, fetch=False, run_timeout=run_timeout)
            shared = sorted(mine & theirs)
            if shared:
                overlaps.append((other_id, shared))
        return overlaps
    except Exception as exc:  # noqa: BLE001 — a warning must never block a merge
        _LOG.debug("file_scope_overlap: overlap check failed branch=%s: %s", merging_branch, exc)
        return []


def warn_overlaps(
    merging_branch: str,
    *,
    base_ref: str = DEFAULT_BASE_REF,
    repo: Optional[Path] = None,
    stream: Optional[Any] = None,
    max_branches: int = DEFAULT_MAX_BRANCHES,
    max_scan_seconds: float = DEFAULT_MAX_SCAN_SECONDS,
    run_timeout: float = DEFAULT_RUN_TIMEOUT,
) -> list[tuple[str, list[str]]]:
    """Compute overlaps for ``merging_branch`` and emit a WARN line per colliding dispatch.

    Returns the overlaps so the caller can also surface them in its result payload. The warning
    names the other dispatch AND lists the shared files (OI-1091's contract). Emits to ``stream``
    (default stderr) and the logger; never raises.
    """
    overlaps = find_overlaps(
        merging_branch, base_ref=base_ref, repo=repo, max_branches=max_branches,
        max_scan_seconds=max_scan_seconds, run_timeout=run_timeout, stream=stream,
    )
    if not overlaps:
        return overlaps
    out = sys.stderr if stream is None else stream
    for other_id, files in overlaps:
        message = (
            f"WARN: file-scope overlap — dispatch {other_id!r} (open) also touches "
            f"{len(files)} file(s) in this merge: {', '.join(files)}"
        )
        print(message, file=out)
        _LOG.warning("file_scope_overlap: %s", message)
    return overlaps
