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

3. GitHub reports a CLOSED (rejected) pull request whose ``headRefOid``
   equals the worktree's HEAD or has it as an ancestor. The commits stay
   reachable as ``refs/pull/<n>/head``. The worktree is released like a
   MERGED one, but the local branch is deleted only after ``git ls-remote
   origin`` shows ``refs/pull/<n>/head`` or ``refs/heads/<branch>`` at HEAD
   (or a descendant of it). Without that proof, or when ``ls-remote`` fails,
   the branch stays and the reason says which.
4. The T0 decision log of the project holds an ``outcome_decision`` of
   ``reject`` for the dispatch id. The worktree is released and the local
   branch is KEPT: its commits may exist nowhere else.

The project is the one in ``<repo-root>/.vnx-project-id`` (``VNX_PROJECT_ID``
is used when the marker is absent); a ``VNX_PROJECT_ID`` that differs from the
marker is refused by name. ``--repo-root`` is required: there is no default
checkout, so a job can never sweep the engine install by accident.

Releasing under a proof can never discard work: every commit the worktree
holds is already durably recorded elsewhere (in main's history, in a PR, or
on a branch kept by the reject rule), and a tree with no uncommitted change
(an untracked symlink that resolves inside the repo is not a change) has
nothing beyond those commits to lose.

Dry-run by default — this script only ever WRITES when called with
``--apply``. Even then it only touches ``git worktree``/``git branch``
plumbing in the target repo; it never touches ``~/Library/LaunchAgents`` or
any other machine state, and it always refuses to act on the worktree it is
currently running from (via ``VNX_DISPATCH_ID``/``VNX_CURRENT_DISPATCH_ID``
and via cwd).

Usage::

    python3 scripts/cleanup_reviewed_worktrees.py --repo-root DIR
                                                    [--only DISPATCH_ID ...]
                                                    [--apply] [--json] [--all]

``--all`` prints ``scripts/lib/worktree_inventory.py``'s read-only inventory of
EVERY worktree git knows about in the repo (dispatch, gate, pool, bench and
external ones), then exits 0 — it evaluates, locks and releases nothing. It is
the answer to "the sweep sees only locked dispatch trees, what else is there?";
the default run is untouched by it. A failed ``git worktree list`` exits 2 with
git's own stderr and prints no inventory at all.

Exit 0 on a normal run (this is a maintenance report, not a CI gate) unless
argument/repo resolution or ``--all``'s git call fails.
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
from typing import Any, TypedDict

_LIB_DIR = Path(__file__).resolve().parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

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
    # False keeps the local branch after the worktree is released: the proof
    # that fired does not show the commits exist on the remote.
    delete_branch: bool = True


@dataclass
class ReleaseResult:
    released: bool
    errors: list[str] = field(default_factory=list)


def discover_locked_dispatch_worktrees(repo_root: Path) -> list[LockedWorktree]:
    """Every ``.vnx-data/worktrees/dispatch-*`` entry that is currently
    ``git worktree lock``-ed with a ``vnx preserve`` reason. Anything locked
    for a DIFFERENT reason (an operator's own manual lock) is left out —
    this tool only ever acts on locks the engine itself created.

    The porcelain parser lives in ``worktree_inventory`` so this sweep and the
    ``--all`` inventory can never read the same output two different ways."""
    from worktree_inventory import parse_worktree_list_porcelain

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


def is_worktree_clean(wt_path: Path, repo_root: Path) -> bool:
    """Clean = nothing but untracked internal symlinks. Anything the parser
    cannot turn into a path counts as dirty: fail toward keeping the work.

    The status read and the internal-symlink rule live in
    ``worktree_inventory`` (one implementation shared with ``--all``)."""
    from worktree_inventory import git_status_bytes, is_internal_symlink

    raw = git_status_bytes(wt_path)
    if raw is None:
        return False
    for record in raw.split(b"\0"):
        if not record:
            continue
        # A rename/copy record is not "?? ", so it returns False here before
        # its second (source) path is ever read as a record of its own.
        if not record.startswith(b"?? "):
            return False
        try:
            name = os.fsdecode(record[3:])
        except (UnicodeError, ValueError):
            return False
        if not name or not is_internal_symlink(wt_path / name, repo_root):
            return False
    return True


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


@dataclass(frozen=True)
class PrEntry:
    number: int | None
    state: str
    head_ref_oid: str


def find_captured_prs(repo_slug: str, branch: str) -> list[PrEntry]:
    """MERGED and CLOSED pull requests for *branch* that carry a
    ``headRefOid``, MERGED first. Empty when ``gh`` found none or is not
    usable. Multiple PRs can share a branch name over time; any one of them
    that holds the worktree's HEAD is sufficient proof."""
    raw = run_gh_pr_list(repo_slug, branch)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        return []
    found: list[PrEntry] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("headRefOid"):
            continue
        state = entry.get("state")
        if state not in ("MERGED", "CLOSED"):
            continue
        number = entry.get("number")
        found.append(PrEntry(number if isinstance(number, int) else None, state, str(entry["headRefOid"])))
    found.sort(key=lambda e: e.state != "MERGED")
    return found


def _holds_head(repo_root: Path, head_sha: str, oid: str) -> bool:
    if head_sha == oid:
        return True
    try_fetch_sha(repo_root, oid)
    return is_ancestor(repo_root, head_sha, oid)


def remote_holds_head(repo_root: Path, wt: LockedWorktree, pr_number: int | None) -> tuple[bool, str]:
    """Proof that the commits of *wt* exist on the remote: ``git ls-remote
    origin`` shows ``refs/pull/<n>/head`` or ``refs/heads/<branch>`` at HEAD or
    at a descendant of it. Returns ``(proven, what was found or why not)``."""
    refs = [f"refs/heads/{wt.branch}"]
    if pr_number is not None:
        refs.insert(0, f"refs/pull/{pr_number}/head")
    proc = run_git(["ls-remote", "origin", *refs], cwd=repo_root)
    if proc.returncode != 0:
        return False, f"git ls-remote origin faalde ({(proc.stderr or '').strip()})"
    for line in proc.stdout.splitlines():
        oid, _, ref = line.partition("\t")
        if oid and ref in refs and _holds_head(repo_root, wt.head_sha or "", oid):
            return True, f"{ref} staat op {oid[:12]}"
    return False, "geen van " + " / ".join(refs) + " op de remote bevat HEAD"


def evaluate_worktree(
    repo_root: Path,
    wt: LockedWorktree,
    repo_slug: str | None,
    decisions: dict[str, dict[str, Any]] | None = None,
) -> Verdict:
    if wt.branch is None:
        return Verdict(False, "worktree staat op een detached HEAD — geen branch-identiteit om te verifiëren")
    if wt.head_sha is None:
        return Verdict(False, "kon HEAD-sha niet bepalen")
    if not is_worktree_clean(wt.path, repo_root):
        return Verdict(False, "werkmap is niet schoon (ongecommitte wijzigingen of untracked bestanden) — dataverlies-risico")

    if is_ancestor(repo_root, wt.head_sha, "origin/main"):
        return Verdict(True, f"HEAD ({wt.head_sha[:12]}) is ancestor van origin/main")

    pr_note = ""
    if repo_slug is None:
        pr_note = "HEAD zit niet in origin/main en de PR-status kon niet gecontroleerd worden (geen origin-remote herkend)"
    else:
        prs = find_captured_prs(repo_slug, wt.branch)
        for pr in prs:
            if not _holds_head(repo_root, wt.head_sha, pr.head_ref_oid):
                continue
            if pr.state == "MERGED":
                return Verdict(True, f"HEAD is (ancestor van) de headRefOid ({pr.head_ref_oid[:12]}) van een MERGED PR voor {wt.branch!r}")
            proven, found = remote_holds_head(repo_root, wt, pr.number)
            head_proof = f"HEAD is (ancestor van) de headRefOid ({pr.head_ref_oid[:12]}) van een CLOSED PR voor {wt.branch!r}"
            if proven:
                return Verdict(True, f"{head_proof}; remote bewijs: {found}")
            return Verdict(True, f"{head_proof}; lokale branch blijft: {found}", delete_branch=False)
        if not prs:
            pr_note = f"geen MERGED of CLOSED pull request gevonden voor branch {wt.branch!r} — werk is niet aantoonbaar afgerond"
        else:
            pr_note = (
                f"branch {wt.branch!r} heeft een PR, maar HEAD ({wt.head_sha[:12]}) bevat commits die niet in "
                "de PR-head terechtkwamen — mogelijk verder gewerkt na de laatste push vanuit deze worktree"
            )

    decision = (decisions or {}).get(wt.dispatch_dir_id or "")
    if decision is not None and decision.get("decision") == "reject":
        return Verdict(
            True,
            f"T0 heeft dispatch {wt.dispatch_dir_id} afgewezen (reject) en de werkmap is schoon; lokale branch blijft",
            delete_branch=False,
        )
    return Verdict(False, pr_note)


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


def release_worktree(repo_root: Path, wt: LockedWorktree, *, delete_branch: bool) -> ReleaseResult:
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

        if wt.branch and delete_branch:
            branch_delete = run_git(["branch", "-D", wt.branch], cwd=repo_root)
            if branch_delete.returncode != 0:
                errors.append(f"branch delete failed (worktree already removed): {(branch_delete.stderr or '').strip()}")

    return ReleaseResult(released=True, errors=errors)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--repo-root", type=Path, required=True,
        help="de checkout van het project waarvan .vnx-data/worktrees wordt opgeruimd (verplicht)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="DISPATCH_ID",
        help="beperk tot deze dispatch-id('s) (bv. D-4cbc8faf); mag meerdere keren opgegeven worden",
    )
    parser.add_argument("--apply", action="store_true", help="release SAFE_TO_RELEASE-worktrees echt (default: dry-run)")
    parser.add_argument("--json", action="store_true", help="schrijf het rapport als JSON naar stdout")
    parser.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="print de volledige read-only worktree-inventaris (dispatch, gate, pool, bench) en stop; "
             "evalueert en released niets",
    )
    return parser


class WorktreeReportEntry(TypedDict):
    """One row of the human/JSON report — always carries every key, unlike
    ``worktree_inventory.WorktreePorcelainRecord`` which is a partial parse."""

    dispatch_id: str | None
    path: str
    branch: str | None
    head_sha: str | None
    lock_reason: str | None
    safe_to_release: bool
    reason: str
    released: bool
    errors: list[str]
    delete_branch: bool


def resolve_project_id(repo_root: Path) -> tuple[str, str | None]:
    """``(project_id, refusal)``: the id of the checkout (``.vnx-project-id``,
    else ``VNX_PROJECT_ID``). A ``VNX_PROJECT_ID`` that differs from the marker
    is refused by name."""
    from forge_project_target import checkout_project_id

    marker_id = checkout_project_id(repo_root)
    env_id = os.environ.get("VNX_PROJECT_ID", "").strip()
    if marker_id and env_id and marker_id != env_id:
        return marker_id, (
            f"VNX_PROJECT_ID={env_id!r} wijkt af van het project van {repo_root} "
            f"({marker_id!r} in .vnx-project-id): niets opgeruimd"
        )
    return marker_id or env_id, None


def read_reject_decisions(repo_root: Path, project_id: str) -> dict[str, dict[str, Any]]:
    """Outcome decisions of *project_id* from its own decision log."""
    if not project_id:
        return {}
    import vnx_paths
    from open_outcomes import DECISION_LOG_NAME, read_outcome_decisions

    state_dir = vnx_paths.resolve_data_root(repo_root) / "state"
    return read_outcome_decisions(state_dir / DECISION_LOG_NAME, project_id)


def _format_inventory_entry(entry: Any) -> str:
    """One table row. Unknown evidence prints as ``?`` — never as 0."""
    state = "MISSING" if entry.missing else ("locked" if entry.locked else "unlocked")
    dirty = "?" if entry.dirty_count is None else str(entry.dirty_count)
    unpushed = "?" if entry.unpushed is None else str(entry.unpushed)
    age = "?" if entry.age_days is None else f"{entry.age_days}d"
    owner = "owned" if entry.owned else "external"
    return (
        f"  [{entry.kind}/{owner}] {state} dirty={dirty} unpushed={unpushed} "
        f"age={age} {entry.branch or '(detached)'} {entry.path}"
    )


def _print_inventory(repo_root: Path, *, as_json: bool) -> int:
    """``--all``: print the read-only inventory and stop. Nothing is
    evaluated, locked or released; a failed git call is exit 2, no stdout."""
    from worktree_inventory import WorktreeInventoryError, inventory

    try:
        entries = inventory(repo_root)
    except WorktreeInventoryError as exc:
        print(f"cleanup_reviewed_worktrees: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps({"repo_root": str(repo_root), "worktrees": [e.to_dict() for e in entries]}, indent=2))
        return 0
    owned = sum(1 for entry in entries if entry.owned)
    print(
        f"cleanup_reviewed_worktrees --all: {len(entries)} worktree(s), "
        f"waarvan {owned} door de fabric beheerd (alleen lezen — niets geëvalueerd of vrijgegeven)"
    )
    for entry in entries:
        print(_format_inventory_entry(entry))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()

    if args.show_all:
        return _print_inventory(repo_root, as_json=args.json)

    project_id, refusal = resolve_project_id(repo_root)
    if refusal:
        print(f"cleanup_reviewed_worktrees: {refusal}", file=sys.stderr)
        return 2
    if not project_id:
        print("WAARSCHUWING: geen project-id (.vnx-project-id of VNX_PROJECT_ID) — T0-reject-besluiten worden niet gelezen.", file=sys.stderr)
    decisions = read_reject_decisions(repo_root, project_id)

    candidates = discover_locked_dispatch_worktrees(repo_root)
    if args.only:
        wanted = set(args.only)
        candidates = [c for c in candidates if c.dispatch_dir_id in wanted]

    repo_slug = get_repo_slug(repo_root)
    if repo_slug is None:
        print("WAARSCHUWING: kon geen github.com owner/repo herkennen uit origin — PR-gebaseerde verificatie (squash-merges) wordt overgeslagen, alleen ancestor-of-main telt.", file=sys.stderr)

    report: list[WorktreeReportEntry] = []
    released = 0
    skipped_self = 0
    for wt in candidates:
        if is_current_worktree(wt):
            skipped_self += 1
            continue
        verdict = evaluate_worktree(repo_root, wt, repo_slug, decisions)
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
            "delete_branch": verdict.delete_branch,
        }
        if verdict.safe and args.apply:
            result = release_worktree(repo_root, wt, delete_branch=verdict.delete_branch)
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
