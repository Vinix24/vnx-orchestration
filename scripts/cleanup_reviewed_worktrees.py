"""cleanup_reviewed_worktrees.py — release dispatch worktrees preserved for
identity review, once that review is provably done (OI-1111).

Background: ``vnx-orchestration``'s ``tmux_worktree.reap()`` classifies a
worktree ``dirty`` — and preserves it forever via ``git worktree lock
--reason "vnx preserve <ts>"`` — in two cases: (a) plain uncommitted tracked
changes, or (b) OI-1124 BRANCH-IDENTITY DRIFT, where a fix-forward dispatch
checked out an EXISTING branch (often naming a different dispatch id) instead
of its own. Both are the right call at teardown time: reaping a dirty tree
risks losing work. But nothing ever performs the second half — releasing the
worktree once whatever it was preserved for has actually shipped. Measured
17 august 2026 on this Mac: 32 of 61 ``.vnx-data/worktrees/dispatch-*``
worktrees sit locked this way, three of them freed by hand overnight
16/17-08 (dispatch-D-e6712386, dispatch-D-0c729d74, dispatch-D-06334377)
because nothing else does it.

"Reviewed" here means one specific, git-provable fact, never an age guess
(``.claude/rules/rode-test-eigenschap-of-momentopname.md``): the worktree's
checked-out branch has NO commit that isn't already captured somewhere safe,
AND the working tree carries no uncommitted change. Two ways a branch counts
as captured:

1. Its HEAD is an ancestor of (or equal to) ``origin/main`` — the ordinary
   merge/rebase-merge case.
2. GitHub reports a MERGED pull request for that branch, and the worktree's
   HEAD is an ancestor of (or equal to) that PR's ``headRefOid`` — the
   squash-merge case, where main's integration commit has a different SHA
   than anything the branch ever held, so (1) alone would never fire even
   though the PR shipped everything the worktree has.

Releasing under either proof can never discard work: every commit the
worktree holds is already durably recorded elsewhere (in main's history, or
as the head of a merged PR), and an empty ``git status --porcelain`` means
there is nothing beyond those commits to lose.

Dry-run by default — this script only ever WRITES when called with
``--apply``. Even then it only touches ``git worktree``/``git branch``
plumbing in the target repo; it never touches ``~/Library/LaunchAgents`` or
any other machine state, and it always refuses to act on the worktree it is
currently running from (via ``VNX_DISPATCH_ID``/``VNX_CURRENT_DISPATCH_ID``
and via cwd).

Usage::

    python3 scripts/cleanup_reviewed_worktrees.py [--repo-root DIR]
                                                    [--only DISPATCH_ID ...]
                                                    [--apply] [--json]

Exit 0 on a normal run (this is a maintenance report, not a CI gate) unless
argument/repo resolution itself fails.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
LOCK_REASON_PREFIX = "vnx preserve"
_DISPATCH_DIR_RE = re.compile(r"^dispatch-(?P<id>[A-Za-z0-9][A-Za-z0-9_-]*)$")


def _run(args: list[str], cwd: Path | str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Thin subprocess wrapper — no shell=True, mockable in tests."""
    return subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout, check=False)


def run_git(args: list[str], cwd: Path | str | None = None) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=cwd)


def run_gh_pr_list(repo_slug: str, branch: str) -> str:
    """stdout of ``gh pr list --state all --head <branch> --json ...``.

    Thin wrapper so tests only monkeypatch this call, same pattern as
    ``ci_launchd_inventory_check.run_launchctl_list``. Returns ``""`` (not
    an exception) when ``gh`` itself is missing — callers treat that as
    "cannot verify via PR", not as "no PR exists"."""
    try:
        proc = _run(
            [
                "gh", "pr", "list",
                "--repo", repo_slug,
                "--state", "all",
                "--head", branch,
                "--json", "number,state,mergedAt,headRefOid",
            ],
            timeout=30,
        )
    except FileNotFoundError:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


@dataclass(frozen=True)
class LockedWorktree:
    path: Path
    dispatch_dir_id: str | None
    branch: str | None  # short form, e.g. "dispatch/D-030d7e66"; None when detached
    head_sha: str | None
    lock_reason: str | None


@dataclass
class Verdict:
    safe: bool
    reason: str


@dataclass
class ReleaseResult:
    released: bool
    errors: list[str] = field(default_factory=list)


class WorktreePorcelainRecord(TypedDict, total=False):
    """One ``git worktree list --porcelain`` block. Every key is optional —
    presence tracks whether the corresponding line appeared at all."""

    path: str
    head: str
    branch: str
    locked: str
    detached: bool
    bare: bool
    prunable: bool


def parse_worktree_list_porcelain(text: str) -> list[WorktreePorcelainRecord]:
    """Parse ``git worktree list --porcelain`` into one dict per worktree
    block. Keys present only when the corresponding line was present:
    ``path``, ``head``, ``branch``, ``locked`` (reason, possibly ""),
    ``detached`` (bool)."""
    records: list[WorktreePorcelainRecord] = []
    current: WorktreePorcelainRecord = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):]
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):]
        elif line == "detached":
            current["detached"] = True
        elif line == "locked":
            current["locked"] = ""
        elif line.startswith("locked "):
            current["locked"] = line[len("locked "):]
        elif line == "bare":
            current["bare"] = True
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable"] = True
    if current:
        records.append(current)
    return records


def discover_locked_dispatch_worktrees(repo_root: Path) -> list[LockedWorktree]:
    """Every ``.vnx-data/worktrees/dispatch-*`` entry that is currently
    ``git worktree lock``-ed with a ``vnx preserve`` reason. Anything locked
    for a DIFFERENT reason (an operator's own manual lock) is left out —
    this tool only ever acts on locks the engine itself created."""
    proc = run_git(["worktree", "list", "--porcelain"], cwd=repo_root)
    if proc.returncode != 0:
        return []
    worktrees_dir = (repo_root / ".vnx-data" / "worktrees").resolve()
    result: list[LockedWorktree] = []
    for rec in parse_worktree_list_porcelain(proc.stdout):
        raw_path = rec.get("path")
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.parent != worktrees_dir:
            continue
        m = _DISPATCH_DIR_RE.match(resolved.name)
        if not m:
            continue
        lock_reason = rec.get("locked")
        if lock_reason is None or not lock_reason.startswith(LOCK_REASON_PREFIX):
            continue
        branch_ref = rec.get("branch")
        branch = branch_ref[len("refs/heads/"):] if branch_ref and branch_ref.startswith("refs/heads/") else branch_ref
        result.append(
            LockedWorktree(
                path=resolved,
                dispatch_dir_id=m.group("id"),
                branch=branch,
                head_sha=rec.get("head"),
                lock_reason=lock_reason,
            )
        )
    return result


def is_current_worktree(wt: LockedWorktree) -> bool:
    """Never act on the worktree this process is itself running from —
    checked two ways so either signal alone is enough to protect it."""
    dispatch_env = os.environ.get("VNX_DISPATCH_ID") or os.environ.get("VNX_CURRENT_DISPATCH_ID")
    if dispatch_env and wt.dispatch_dir_id == dispatch_env:
        return True
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return False
    return cwd == wt.path or wt.path in cwd.parents


def get_repo_slug(repo_root: Path) -> str | None:
    proc = run_git(["remote", "get-url", "origin"], cwd=repo_root)
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def is_worktree_clean(wt_path: Path) -> bool:
    proc = run_git(["status", "--porcelain"], cwd=wt_path)
    return proc.returncode == 0 and proc.stdout.strip() == ""


def is_ancestor(repo_root: Path, ancestor_sha: str, descendant_sha: str) -> bool:
    proc = run_git(["merge-base", "--is-ancestor", ancestor_sha, descendant_sha], cwd=repo_root)
    return proc.returncode == 0


def object_available(repo_root: Path, sha: str) -> bool:
    proc = run_git(["cat-file", "-e", sha], cwd=repo_root)
    return proc.returncode == 0


def try_fetch_sha(repo_root: Path, sha: str) -> bool:
    """Best-effort: some remotes allow fetching an arbitrary reachable SHA
    even after its branch was deleted (observed working against this repo's
    origin for a squash-merged, branch-deleted PR). Never raises — a failed
    fetch just means the PR-ancestor path can't be verified this round."""
    if object_available(repo_root, sha):
        return True
    proc = run_git(["fetch", "origin", sha], cwd=repo_root)
    return proc.returncode == 0 and object_available(repo_root, sha)


def find_merged_pr_head(repo_slug: str, branch: str) -> str | None:
    """The ``headRefOid`` of a MERGED pull request for *branch*, or
    ``None`` if ``gh`` found none / isn't usable. Multiple PRs can share a
    branch name over time; any MERGED entry is sufficient proof."""
    raw = run_gh_pr_list(repo_slug, branch)
    if not raw:
        return None
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("state") == "MERGED" and entry.get("headRefOid"):
            return str(entry["headRefOid"])
    return None


def evaluate_worktree(repo_root: Path, wt: LockedWorktree, repo_slug: str | None) -> Verdict:
    if wt.branch is None:
        return Verdict(False, "worktree staat op een detached HEAD — geen branch-identiteit om te verifiëren")
    if wt.head_sha is None:
        return Verdict(False, "kon HEAD-sha niet bepalen")
    if not is_worktree_clean(wt.path):
        return Verdict(False, "werkmap is niet schoon (ongecommitte tracked wijzigingen) — dataverlies-risico")

    if is_ancestor(repo_root, wt.head_sha, "origin/main"):
        return Verdict(True, f"HEAD ({wt.head_sha[:12]}) is ancestor van origin/main")

    if repo_slug is None:
        return Verdict(False, "HEAD zit niet in origin/main en de PR-status kon niet gecontroleerd worden (geen origin-remote herkend)")

    pr_head = find_merged_pr_head(repo_slug, wt.branch)
    if pr_head is None:
        return Verdict(False, f"geen MERGED pull request gevonden voor branch {wt.branch!r} — werk is niet aantoonbaar afgerond")

    if wt.head_sha == pr_head:
        return Verdict(True, f"HEAD is exact de headRefOid van een MERGED PR voor {wt.branch!r}")

    try_fetch_sha(repo_root, pr_head)
    if is_ancestor(repo_root, wt.head_sha, pr_head):
        return Verdict(True, f"HEAD is ancestor van de headRefOid ({pr_head[:12]}) van een MERGED PR voor {wt.branch!r}")

    return Verdict(
        False,
        f"branch {wt.branch!r} heeft een MERGED PR, maar HEAD ({wt.head_sha[:12]}) bevat commits die niet in "
        f"die PR ({pr_head[:12]}) terechtkwamen — mogelijk verder gewerkt na de laatste push vanuit deze worktree",
    )


@contextmanager
def _flock_context(repo_root: Path) -> Iterator[None]:
    git_dir_proc = run_git(["rev-parse", "--git-common-dir"], cwd=repo_root)
    git_dir = Path(git_dir_proc.stdout.strip()) if git_dir_proc.returncode == 0 else (repo_root / ".git")
    if not git_dir.is_absolute():
        git_dir = (repo_root / git_dir).resolve()
    lock_dir = git_dir / "worktrees"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".vnx-cleanup-lock"
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def release_worktree(repo_root: Path, wt: LockedWorktree) -> ReleaseResult:
    errors: list[str] = []
    with _flock_context(repo_root):
        unlock = run_git(["worktree", "unlock", str(wt.path)], cwd=repo_root)
        if unlock.returncode != 0:
            errors.append(f"unlock failed: {(unlock.stderr or '').strip()}")
            return ReleaseResult(released=False, errors=errors)

        remove = run_git(["worktree", "remove", "--force", str(wt.path)], cwd=repo_root)
        if remove.returncode != 0:
            errors.append(f"worktree remove failed: {(remove.stderr or '').strip()}")
            return ReleaseResult(released=False, errors=errors)

        if wt.branch:
            branch_delete = run_git(["branch", "-D", wt.branch], cwd=repo_root)
            if branch_delete.returncode != 0:
                errors.append(f"branch delete failed (worktree already removed): {(branch_delete.stderr or '').strip()}")

    return ReleaseResult(released=True, errors=errors)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT_DEFAULT)
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="DISPATCH_ID",
        help="beperk tot deze dispatch-id('s) (bv. D-4cbc8faf); mag meerdere keren opgegeven worden",
    )
    parser.add_argument("--apply", action="store_true", help="release SAFE_TO_RELEASE-worktrees echt (default: dry-run)")
    parser.add_argument("--json", action="store_true", help="schrijf het rapport als JSON naar stdout")
    return parser


class WorktreeReportEntry(TypedDict):
    """One row of the human/JSON report — always carries every key, unlike
    :class:`WorktreePorcelainRecord` which is a partial parse."""

    dispatch_id: str | None
    path: str
    branch: str | None
    head_sha: str | None
    lock_reason: str | None
    safe_to_release: bool
    reason: str
    released: bool
    errors: list[str]


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()

    candidates = discover_locked_dispatch_worktrees(repo_root)
    if args.only:
        wanted = set(args.only)
        candidates = [c for c in candidates if c.dispatch_dir_id in wanted]

    repo_slug = get_repo_slug(repo_root)
    if repo_slug is None:
        print("WAARSCHUWING: kon geen github.com owner/repo herkennen uit origin — PR-gebaseerde verificatie (squash-merges) wordt overgeslagen, alleen ancestor-of-main telt.")

    report: list[WorktreeReportEntry] = []
    released = 0
    skipped_self = 0
    for wt in candidates:
        if is_current_worktree(wt):
            skipped_self += 1
            continue
        verdict = evaluate_worktree(repo_root, wt, repo_slug)
        entry: WorktreeReportEntry = {
            "dispatch_id": wt.dispatch_dir_id,
            "path": str(wt.path),
            "branch": wt.branch,
            "head_sha": wt.head_sha,
            "lock_reason": wt.lock_reason,
            "safe_to_release": verdict.safe,
            "reason": verdict.reason,
            "released": False,
            "errors": [],
        }
        if verdict.safe and args.apply:
            result = release_worktree(repo_root, wt)
            entry["released"] = result.released
            entry["errors"] = result.errors
            if result.released:
                released += 1
        report.append(entry)

    if args.json:
        print(json.dumps({"repo_root": str(repo_root), "candidates": report}, indent=2))
    else:
        print(f"cleanup_reviewed_worktrees: {len(candidates)} locked dispatch-worktree(s) gevonden ({skipped_self} eigen worktree overgeslagen)")
        for entry in report:
            verdict_label = "SAFE_TO_RELEASE" if entry["safe_to_release"] else "BLIJFT_STAAN"
            action = ""
            if entry["safe_to_release"]:
                if args.apply:
                    action = " -> RELEASED" if entry["released"] else f" -> RELEASE FAILED: {entry['errors']}"
                else:
                    action = " (dry-run, gebruik --apply om echt vrij te geven)"
            print(f"  [{verdict_label}] {entry['dispatch_id']} ({entry['branch']}): {entry['reason']}{action}")
        if args.apply:
            print(f"cleanup_reviewed_worktrees: {released} worktree(s) vrijgegeven")
        else:
            safe_count = sum(1 for e in report if e["safe_to_release"])
            print(f"cleanup_reviewed_worktrees: {safe_count} van {len(report)} zijn SAFE_TO_RELEASE (dry-run — geen --apply meegegeven)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
