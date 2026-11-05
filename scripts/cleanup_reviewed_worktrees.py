"""cleanup_reviewed_worktrees.py — release fabric-owned worktrees once nothing
depends on them any more.

The candidates are the ones ``scripts/lib/worktree_inventory.py`` measures:
every fabric-OWNED worktree of the repo (dispatch, gate, pool, bench) except
the checkout this process runs from and worktrees of another repo. A tree
whose lock, liveness, occupancy or HEAD argues against removal is reported
with the reason it stays — never dropped from the report.

Two evidence regimes, and then one gate:

* A tree with a lock (``vnx preserve`` or a plain ``git worktree lock``) keeps
  the reviewed-worktree rules below: its branch is detached, unmeasurable,
  dirty, or carries a commit that is not provably captured in main, a MERGED
  or CLOSED pull request, or a T0 ``reject`` decision.
* A tree WITHOUT a lock must be clean, have no OPEN pull request (unmeasurable
  without ``gh`` = keep) and carry no commit outside origin.
* Whichever passing verdict it gets, the removal itself goes through
  ``scripts/lib/worktree_removal_guard.guarded_remove``: this sweep decides no
  liveness, occupancy or HEAD. The guard is handed the HEAD sha the evidence
  evaluated and refuses when the tree moved off it, so a commit made between
  evaluation and removal is never removed unexamined. A lock is dropped only
  inside that guard, after its under-lock revalidation and its decision write,
  right before the removal — so a tree the guard keeps stays locked.
* ``orphan_sweep``'s worktree kind keeps its own removal: it is driven by the
  tmux/register liveness model, not by this evidence, and rewiring it is not a
  small change.

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

For a LOCKED tree, "reviewed" means one specific, git-provable fact, never an
age guess (``.claude/rules/rode-test-eigenschap-of-momentopname.md``): the
worktree's checked-out branch has NO commit that isn't already captured
somewhere safe, AND the working tree carries no uncommitted change. Two ways a
branch counts as captured:

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

Two flags widen what the sweep may release, both OFF by default:
``--max-age-days N`` makes a tree a candidate only when ``worktree_inventory``
measures it older than N days; ``--include-dirty`` salvages a dirty or
unpushed tree (bundle, patches, untracked archive), verifies exactly what the
salvage wrote, and only then removes. With either flag an OPEN pull request
keeps a tree (an unmeasurable PR state — no ``gh``, a failing lookup, an
unreadable reply — is never a green light), and so does a live or unmeasurable
process. The dirty route keeps the local branch; neither flag is in the
launchd template, because turning them on for the fleet is Vincent's call.

Usage::

    python3 scripts/cleanup_reviewed_worktrees.py --repo-root DIR
                                                    [--only DISPATCH_ID ...]
                                                    [--apply] [--json] [--all]
                                                    [--max-age-days N]
                                                    [--include-dirty]

``--all`` prints ``scripts/lib/worktree_inventory.py``'s read-only inventory of
EVERY worktree git knows about in the repo (dispatch, gate, pool, bench and
external ones), then exits 0 — it evaluates, locks and releases nothing. A
failed ``git worktree list`` exits 2 with git's own stderr and prints no
inventory at all.

Exit 0 on a normal run (this is a maintenance report, not a CI gate) unless
argument/repo resolution fails or the worktree inventory cannot be read (a
``git worktree list`` failure exits 2 — "cannot measure" is never "nothing to
do").
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NotRequired, TypedDict

_LIB_DIR = Path(__file__).resolve().parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_DISPATCH_DIR_RE = re.compile(r"^dispatch-(?P<id>[A-Za-z0-9][A-Za-z0-9_-]*)$")

# Reasons this sweep's own evidence gives for keeping an UNLOCKED tree. The
# removal layer reports ``worktree_removal_guard.REASON_*`` instead.
REASON_NO_GH = "no_gh"
REASON_OPEN_PR = "open_pr"
REASON_COMMITS_OUTSIDE_ORIGIN = "commits_outside_origin"
REASON_UNLOCK_FAILED = "unlock_failed"
# PR-5: the age gate and the salvage route. The guard names its own refusals.
REASON_TOO_YOUNG = "too_young"
REASON_AGE_UNMEASURABLE = "age_unmeasurable"
REASON_SALVAGE_FAILED = "salvage_failed"
REASON_SALVAGE_UNVERIFIED = "salvage_unverified"
# An empty envelope dispatch id keeps the store's boundary guard from archiving
# a live T0 stream; the removed dispatch is named inside ``data`` instead.
TEARDOWN_EVENT_TERMINAL = "T0"


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
class Candidate:
    """One worktree the sweep may release.

    ``lock_reason`` is None for a tree without a ``git worktree lock``; that
    is the whole difference between the two evidence regimes. ``kind`` is the
    inventory's classification (dispatch, gate, pool, bench, other).
    """

    path: Path
    dispatch_dir_id: str | None
    branch: str | None  # short form, e.g. "dispatch/D-030d7e66"; None when detached
    head_sha: str | None
    lock_reason: str | None
    kind: str = "dispatch"


@dataclass
class Verdict:
    safe: bool
    reason: str
    # False keeps the local branch after the worktree is released: the proof
    # that fired does not show the commits exist on the remote.
    delete_branch: bool = True
    # The HEAD sha the evidence actually judged. The removal is bound to it:
    # a tree that moved off it before the guard runs is refused, so a commit
    # made after evaluation never rides along on this verdict.
    evaluated_head: str | None = None


@dataclass
class ReleaseResult:
    released: bool
    reason: str = ""
    detail: str = ""
    errors: list[str] = field(default_factory=list)
    # True only when a local branch was actually deleted after the removal.
    branch_deleted: bool = False


def discover_candidates(repo_root: Path) -> list[Candidate]:
    """Every fabric-OWNED worktree of *repo_root*, except the main checkout.

    The one measurement is ``worktree_inventory.inventory``: a worktree of
    another repo is not in it, and a path outside the managed roots is not
    ``owned``. A tree git still knows about but that is gone on disk stays a
    candidate — the guard names it ``absent`` rather than the sweep silently
    dropping it. A failed ``git worktree list`` raises
    ``WorktreeInventoryError``: "cannot measure" must never read as "nothing
    to do", so there is no empty-list fallback here.
    """
    from worktree_inventory import inventory

    candidates: list[Candidate] = []
    for entry in inventory(repo_root):
        if not entry.owned:
            continue
        m = _DISPATCH_DIR_RE.match(entry.path.name)
        candidates.append(
            Candidate(
                path=entry.path,
                dispatch_dir_id=m.group("id") if m else None,
                branch=entry.branch,
                head_sha=entry.head,
                lock_reason=entry.lock_reason,
                kind=entry.kind,
            )
        )
    return candidates


def is_current_worktree(wt: Candidate) -> bool:
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


def _parse_pr_json(raw: str) -> list[dict[str, Any]] | None:
    """``gh pr list --json`` output as its object entries; None when unusable.

    ``raw == ""`` is ``run_gh_pr_list``'s "gh missing or failed" reply, and
    anything else that is not a JSON list is unmeasurable too — never "no
    pull request exists"."""
    if not raw:
        return None
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(entries, list):
        return None
    return [entry for entry in entries if isinstance(entry, dict)]


def find_captured_prs(repo_slug: str, branch: str) -> list[PrEntry]:
    """MERGED and CLOSED pull requests for *branch* that carry a
    ``headRefOid``, MERGED first. Empty when ``gh`` found none or is not
    usable. Multiple PRs can share a branch name over time; any one of them
    that holds the worktree's HEAD is sufficient proof."""
    entries = _parse_pr_json(run_gh_pr_list(repo_slug, branch))
    if entries is None:
        return []
    found: list[PrEntry] = []
    for entry in entries:
        if not entry.get("headRefOid"):
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


def remote_holds_head(repo_root: Path, wt: Candidate, pr_number: int | None) -> tuple[bool, str]:
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
    wt: Candidate,
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


def open_pr_refusal(repo_slug: str | None, branch: str) -> str:
    """``""`` when *branch* has no OPEN pull request; otherwise the named reason.

    Fail-closed: no recognised origin slug, no usable ``gh`` (its reply is
    ``""`` when it is missing), a lookup that raises, or an unreadable reply
    all mean the open-PR state is unmeasurable — ``no_gh``, never "no open PR"."""
    if repo_slug is None:
        return REASON_NO_GH
    try:
        entries = _parse_pr_json(run_gh_pr_list(repo_slug, branch))
    except Exception as exc:  # the lookup is the boundary: any failure keeps the tree
        return f"{REASON_NO_GH}: gh-lookup faalde ({exc})"
    if entries is None:
        return REASON_NO_GH
    for entry in entries:
        if entry.get("state") == "OPEN":
            return f"{REASON_OPEN_PR}: PR #{entry.get('number')} voor {branch!r} staat open"
    return ""


def evaluate_unlocked(repo_root: Path, wt: Candidate, repo_slug: str | None) -> Verdict:
    """Evidence for a tree that carries no ``git worktree lock``.

    Nothing it holds may exist nowhere else (a clean tree, every commit of
    HEAD on origin) and no OPEN pull request may still be building on it.
    Liveness, occupancy and HEAD are not judged here — ``guarded_remove``
    owns those and refuses in its own words.

    A detached tree has no branch, so it can have no open PR; only its
    commits need to be on origin (the ordinary leaked gate checkout)."""
    from worktree_inventory import dirty_count, unpushed_count

    if wt.head_sha is None:
        return Verdict(False, "kon HEAD-sha niet bepalen")
    dirty = dirty_count(wt.path, repo_root)
    if dirty is None:
        return Verdict(False, "werkmap-status onmeetbaar (git status faalde) — dataverlies-risico")
    if dirty != 0:
        return Verdict(False, f"werkmap is niet schoon ({dirty} gewijzigd pad) — dataverlies-risico")
    if wt.branch is not None:
        refusal = open_pr_refusal(repo_slug, wt.branch)
        if refusal:
            return Verdict(False, refusal)
    outside = unpushed_count(repo_root, wt.head_sha)
    if outside is None:
        return Verdict(False, "kon niet meten of alle commits van HEAD op origin staan")
    if outside != 0:
        return Verdict(
            False,
            f"{REASON_COMMITS_OUTSIDE_ORIGIN}: {outside} commit(s) van HEAD staan niet op origin — dataverlies-risico",
        )
    return Verdict(True, f"schoon, geen open PR, en alle commits van HEAD ({wt.head_sha[:12]}) staan op origin")


def evaluate_candidate(
    repo_root: Path,
    wt: Candidate,
    repo_slug: str | None,
    decisions: dict[str, dict[str, Any]] | None = None,
) -> Verdict:
    """Locked trees keep the reviewed-worktree rules; unlocked ones need the
    extra evidence that nothing of theirs is unpublished and no PR is open.

    The verdict names the HEAD sha it judged in ``evaluated_head``. That sha
    is handed to ``guarded_remove``, which refuses the removal when the tree
    no longer sits on it — a commit made after this evaluation never passes
    unnoticed into the removal."""
    if wt.lock_reason is None:
        verdict = evaluate_unlocked(repo_root, wt, repo_slug)
    else:
        verdict = evaluate_worktree(repo_root, wt, repo_slug, decisions)
    verdict.evaluated_head = wt.head_sha
    return verdict


def _unlock_for_removal(repo_root: Path, path: Path):
    """``before_remove`` hook: drop the lock only once the guard removes.

    The guard calls it inside the repository lock, after the under-lock
    revalidation AND after the removal decision is persisted, immediately
    before ``git worktree remove`` — every refusal that keeps the tree lies
    behind it, so a tree the guard keeps stays locked. A failed unlock aborts
    the removal instead of force-removing a locked tree.
    """

    def hook(_label: str) -> None:
        proc = run_git(["worktree", "unlock", str(path)], cwd=repo_root)
        if proc.returncode != 0:
            raise OSError(f"unlock failed: {(proc.stderr or '').strip()}")

    return hook


def remove_candidate(
    repo_root: Path,
    wt: Candidate,
    *,
    expected_head: str,
    delete_branch: bool,
    dry_run: bool,
    dirty_proof: Callable[[], str] | None = None,
) -> ReleaseResult:
    """Remove *wt* through the one guard and report the guard's verdict.

    This sweep hands the guard the tree, the dispatch id and the HEAD sha the
    evidence evaluated; the guard decides nothing less than liveness,
    occupancy and HEAD. The branch is deleted only after a removal that
    passed, so a ``head_moved`` refusal leaves both the tree and its branch.
    The lock is dropped by the guard as its last step before the removal; if
    the removal itself then fails, the lock is put back so a tree that stays
    also stays locked.

    *dirty_proof* is the verified-salvage proof of ``--include-dirty``; the
    guard re-runs it under the repository lock, so work written after the
    salvage refuses the removal instead of riding along.
    """
    from worktree_removal_guard import guarded_remove

    unlocked = False

    def before_remove(label: str) -> None:
        nonlocal unlocked
        _unlock_for_removal(repo_root, wt.path)(label)
        unlocked = True

    try:
        result = guarded_remove(
            wt.path,
            expected_head=expected_head,
            dispatch_id=wt.dispatch_dir_id or "",
            repo_root=repo_root,
            dry_run=dry_run,
            before_remove=before_remove if wt.lock_reason is not None else None,
            dirty_proof=dirty_proof,
        )
    except OSError as exc:  # an unlock the guard could not do never removes
        return ReleaseResult(False, REASON_UNLOCK_FAILED, str(exc), [str(exc)])
    errors: list[str] = []
    if unlocked and not result.removed and wt.path.exists():
        relock = run_git(["worktree", "lock", "--reason", wt.lock_reason or "", str(wt.path)], cwd=repo_root)
        if relock.returncode != 0:
            errors.append(f"re-lock failed (tree kept, lock dropped): {(relock.stderr or '').strip()}")
    branch_deleted = False
    if result.removed and delete_branch and wt.branch:
        branch_delete = run_git(["branch", "-D", wt.branch], cwd=repo_root)
        if branch_delete.returncode != 0:
            errors.append(f"branch delete failed (worktree already removed): {(branch_delete.stderr or '').strip()}")
        else:
            branch_deleted = True
    return ReleaseResult(result.removed, result.reason, result.detail, errors, branch_deleted)


def _non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"moet 0 of hoger zijn, niet {value}")
    return value


def live_holder_refusal(repo_root: Path, wt: Candidate) -> str:
    """Named reason a live or unmeasurable holder keeps *wt*; ``""`` when none does.

    The guard's own liveness measure and the dispatch occupancy slot are both
    consulted; an unmeasurable answer keeps the tree like a live one. A gate,
    pool or bench tree has no dispatch id — its seat is a lifetime flock the
    guard measures at the removal itself.
    """
    from dispatch_worktree_isolation import _claim_dir, probe_occupancy
    from worktree_removal_guard import liveness_refusal

    reason, detail = liveness_refusal(wt.dispatch_dir_id or "", repo_root)
    if reason:
        return f"{reason}: {detail}"
    if not wt.dispatch_dir_id:
        return ""
    try:
        state = probe_occupancy(_claim_dir(repo_root), wt.dispatch_dir_id)
    except (OSError, RuntimeError) as exc:
        return f"slot_unavailable: bezettingsslot onleesbaar ({exc})"
    if state == "held":
        return f"occupied: dispatch {wt.dispatch_dir_id!r} houdt het bezettingsslot"
    if state not in ("absent", "released"):
        return f"slot_unavailable: bezettingsstatus {state!r}"
    return ""


def pre_gate_refusal(
    repo_root: Path, wt: Candidate, repo_slug: str | None, max_age_days: int | None,
) -> str:
    """The PR-5 gates before any evidence work or salvage: ``""`` when all pass.

    The age is ``worktree_inventory``'s own measure and an unmeasurable one
    keeps the tree; a detached tree has no branch that could carry a PR."""
    from worktree_inventory import age_days

    if max_age_days is not None:
        age = age_days(wt.path)
        if age is None:
            return f"{REASON_AGE_UNMEASURABLE}: leeftijd van {wt.path} niet meetbaar — niets verwijderd"
        if age <= max_age_days:
            return f"{REASON_TOO_YOUNG}: {age} dag(en) oud, --max-age-days {max_age_days} vraagt ouder"
    if wt.branch is not None:
        refusal = open_pr_refusal(repo_slug, wt.branch)
        if refusal:
            return refusal
    return live_holder_refusal(repo_root, wt)


@dataclass
class SalvageOutcome:
    """A verified salvage the sweep may remove after, or the named reason it may not."""

    refusal: str = ""
    note: str = ""
    dir: str = ""
    artifacts: list[str] = field(default_factory=list)
    verified: bool = False
    proof: Callable[[], str] | None = None


def salvage_for_removal(repo_root: Path, wt: Candidate, *, apply: bool) -> SalvageOutcome:
    """Preserve a dirty or unpushed tree and prove it, before anything is removed.

    A dry run writes nothing. An ``--apply`` run writes the artifacts and runs
    ``worktree_salvage.verify`` on exactly those; only ``[]`` lets the caller
    remove. Any failure is a named refusal: the tree stays, the sweep goes on.
    """
    import vnx_paths
    from worktree_salvage import salvage, verify

    stamp = time.strftime("worktrees-%Y-%m-%d", time.gmtime())
    dest = vnx_paths.resolve_data_root(repo_root) / "salvage" / stamp
    try:
        result = salvage(wt.path, dest, repo_root=repo_root, dry_run=not apply)
    except Exception as exc:  # the sweep's fail-closed boundary: keep the tree, name why
        return SalvageOutcome(refusal=f"{REASON_SALVAGE_FAILED}: {exc}")
    outcome = SalvageOutcome(
        dir=str(result.salvage_dir),
        artifacts=[str(dest / artifact) for artifact in result.artifacts],
        note=f"salvage van {len(result.artifacts)} artifact(en) in {result.salvage_dir}",
    )
    if not apply:
        outcome.note += " (dry-run: niets geschreven; verify volgt bij --apply)"
        return outcome
    try:
        problems = verify(result, dest, repo_root=repo_root)
    except Exception as exc:  # verify only ever answers problems; a raise is one too
        problems = [f"verify raised {exc}"]
    if problems:
        outcome.refusal = f"{REASON_SALVAGE_UNVERIFIED}: " + "; ".join(problems)
        return outcome
    outcome.verified = True
    outcome.note += "; verify geslaagd"

    def proof() -> str:
        try:
            return "; ".join(verify(result, dest, repo_root=repo_root))
        except Exception as exc:  # a re-proof that raises is a problem, never a pass
            return f"{REASON_SALVAGE_UNVERIFIED}: verify raised {exc}"

    outcome.proof = proof
    return outcome


def emit_teardown_event(
    repo_root: Path, wt: Candidate, *, salvaged: bool, preserved_path: str, branch_deleted: bool,
) -> str:
    """Append the existing ``provider_teardown_worktree`` event; ``""`` on success.

    No new event type (that belongs to ``audit-spoor-volledig``). A failure is
    returned as the named reason; it never undoes a removal already made.
    """
    try:
        import vnx_paths
        from event_store import EventStore

        store = EventStore(events_dir=vnx_paths.resolve_data_root(repo_root) / "events")
        store.append(
            TEARDOWN_EVENT_TERMINAL,
            {
                "type": "provider_teardown_worktree",
                "data": {
                    "dispatch_id": wt.dispatch_dir_id or "",
                    "worktree_kind": wt.kind,
                    "worktree_state": "salvaged" if salvaged else "clean",
                    "branch_kept_local": bool(wt.branch) and not branch_deleted,
                    "branch_kept_remote": None,  # never measured: the sweep deletes no remote ref
                    "preserved_path": preserved_path or None,
                    "source": "cleanup_reviewed_worktrees",
                },
            },
            dispatch_id="",
        )
    except Exception as exc:  # an event failure is reported, never a reason to hide a removal
        return f"teardown event niet geschreven: {exc}"
    return ""


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
    parser.add_argument(
        "--max-age-days",
        type=_non_negative_int,
        default=None,
        metavar="N",
        help="alleen bomen die de inventaris ouder dan N dagen meet zijn kandidaat (default: uit)",
    )
    parser.add_argument(
        "--include-dirty",
        action="store_true",
        help="een vuile of ongepushte boom eerst salveren en verifiëren, dan pas verwijderen (default: uit)",
    )
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
    kind: str
    safe_to_release: bool
    reason: str
    guard_reason: str
    guard_detail: str
    released: bool
    errors: list[str]
    delete_branch: bool
    # --include-dirty only: absent everywhere else, so the default JSON report
    # keeps exactly the shape its consumers already parse.
    salvage_dir: NotRequired[str]
    salvage_artifacts: NotRequired[list[str]]
    salvage_verified: NotRequired[bool]


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
    from worktree_inventory import WorktreeInventoryError

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

    try:
        candidates = discover_candidates(repo_root)
    except WorktreeInventoryError as exc:
        print(f"cleanup_reviewed_worktrees: {exc}", file=sys.stderr)
        return 2
    if args.only:
        wanted = set(args.only)
        candidates = [c for c in candidates if c.dispatch_dir_id in wanted]

    repo_slug = get_repo_slug(repo_root)
    if repo_slug is None:
        print("WAARSCHUWING: kon geen github.com owner/repo herkennen uit origin — PR-gebaseerde verificatie (squash-merges) wordt overgeslagen, en een boom zonder lock blijft staan.", file=sys.stderr)

    report: list[WorktreeReportEntry] = []
    released = 0
    skipped_self = 0
    for wt in candidates:
        if is_current_worktree(wt):
            skipped_self += 1
            continue
        report.append(_sweep_candidate(
            repo_root, wt, repo_slug, decisions,
            apply=args.apply,
            max_age_days=args.max_age_days,
            include_dirty=args.include_dirty,
        ))
        if report[-1]["released"]:
            released += 1

    if args.json:
        print(json.dumps({"repo_root": str(repo_root), "candidates": report}, indent=2))
    else:
        print(f"cleanup_reviewed_worktrees: {len(candidates)} worktree(s) uit de inventaris gevonden ({skipped_self} eigen worktree overgeslagen)")
        for entry in report:
            verdict_label = "SAFE_TO_RELEASE" if entry["safe_to_release"] else "BLIJFT_STAAN"
            action = ""
            if entry["safe_to_release"]:
                if args.apply:
                    action = " -> RELEASED" if entry["released"] else f" -> GUARD HIELD TEGEN ({entry['guard_reason']}): {entry['guard_detail'] or entry['errors']}"
                else:
                    action = f" (dry-run, guard: {entry['guard_reason'] or 'niet bereikt'})"
            print(f"  [{verdict_label}] {entry['dispatch_id'] or entry['kind']} ({entry['branch']}): {entry['reason']}{action}")
        if args.apply:
            print(f"cleanup_reviewed_worktrees: {released} worktree(s) vrijgegeven")
        else:
            safe_count = sum(1 for e in report if e["safe_to_release"])
            print(f"cleanup_reviewed_worktrees: {safe_count} van {len(report)} zijn SAFE_TO_RELEASE (dry-run — geen --apply meegegeven)")

    return 0


def _report_entry(wt: Candidate, verdict: Verdict) -> WorktreeReportEntry:
    return {
        "dispatch_id": wt.dispatch_dir_id,
        "path": str(wt.path),
        "branch": wt.branch,
        "head_sha": wt.head_sha,
        "lock_reason": wt.lock_reason,
        "kind": wt.kind,
        "safe_to_release": verdict.safe,
        "reason": verdict.reason,
        "guard_reason": "",
        "guard_detail": "",
        "released": False,
        "errors": [],
        "delete_branch": verdict.delete_branch,
    }


def _finish_removal(
    entry: WorktreeReportEntry,
    result: ReleaseResult,
    repo_root: Path,
    wt: Candidate,
    *,
    salvaged: bool,
) -> None:
    """Record the guard's verdict and emit the teardown event of a removal."""
    entry["guard_reason"] = result.reason
    entry["guard_detail"] = result.detail
    entry["released"] = result.released
    entry["errors"] = list(result.errors)
    if not result.released:
        return
    failure = emit_teardown_event(
        repo_root, wt,
        salvaged=salvaged,
        preserved_path=entry.get("salvage_dir", ""),
        branch_deleted=result.branch_deleted,
    )
    if failure:
        entry["errors"].append(failure)


def _salvage_route(
    repo_root: Path, wt: Candidate, entry: WorktreeReportEntry, verdict: Verdict, *, apply: bool,
) -> WorktreeReportEntry:
    """``--include-dirty``: preserve the work, prove it, and only then remove."""
    from worktree_inventory import dirty_count, unpushed_count
    from worktree_removal_guard import REASON_DRY_RUN

    # Unknown never reads as zero: an unmeasurable worktree keeps its tree.
    dirty = dirty_count(wt.path, repo_root)
    unpushed = None if dirty is None else unpushed_count(repo_root, wt.head_sha)
    if dirty is None or unpushed is None:
        what = "werkmap-status" if dirty is None else "commits buiten origin"
        entry["reason"] = f"{what} onmeetbaar (meting faalde) — niets verwijderd"
        return entry
    if dirty == 0 and unpushed == 0:
        entry["reason"] = verdict.reason  # nothing to preserve: the evidence's own refusal stands
        return entry
    outcome = salvage_for_removal(repo_root, wt, apply=apply)
    entry["salvage_dir"] = outcome.dir
    entry["salvage_artifacts"] = outcome.artifacts
    entry["salvage_verified"] = outcome.verified
    if outcome.refusal:
        entry["reason"] = outcome.refusal
        return entry
    entry["safe_to_release"] = True
    entry["delete_branch"] = False
    if not apply:
        entry["reason"] = (
            f"{outcome.note}: {dirty} gewijzigd pad(en), {unpushed} commit(s) buiten origin "
            "— nog niets verwijderd"
        )
        entry["guard_reason"] = REASON_DRY_RUN
        return entry
    result = remove_candidate(
        repo_root,
        wt,
        expected_head=wt.head_sha or "",
        delete_branch=False,
        dry_run=False,
        dirty_proof=outcome.proof,
    )
    _finish_removal(entry, result, repo_root, wt, salvaged=True)
    entry["reason"] = f"{outcome.note}; {result.reason}"
    return entry


def _sweep_candidate(
    repo_root: Path,
    wt: Candidate,
    repo_slug: str | None,
    decisions: dict[str, dict[str, Any]],
    *,
    apply: bool,
    max_age_days: int | None = None,
    include_dirty: bool = False,
) -> WorktreeReportEntry:
    """One candidate: PR-5 gates, evidence, then (only when it passes) the guard.

    A tree the evidence refuses is never handed to the guard and keeps the
    evidence's own reason; a refusal there is reported in the guard's words.
    The PR-5 gates run only when one of the two flags is set, so the default
    run is unchanged.
    """
    verdict = evaluate_candidate(repo_root, wt, repo_slug, decisions)
    entry = _report_entry(wt, verdict)
    if max_age_days is not None or include_dirty:
        refusal = pre_gate_refusal(repo_root, wt, repo_slug, max_age_days)
        if refusal:
            entry["reason"] = refusal
            return entry
    if verdict.safe:
        result = remove_candidate(
            repo_root,
            wt,
            expected_head=verdict.evaluated_head or "",
            delete_branch=verdict.delete_branch,
            dry_run=not apply,
        )
        _finish_removal(entry, result, repo_root, wt, salvaged=False)
        return entry
    if not include_dirty:
        entry["reason"] = verdict.reason
        return entry
    return _salvage_route(repo_root, wt, entry, verdict, apply=apply)


if __name__ == "__main__":
    sys.exit(main())
