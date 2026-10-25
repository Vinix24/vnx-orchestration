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

Scheduled sweep (PR-4, track ``worktree-opruiming-structureel``): candidates
are every ``owned`` inventory entry — locked or not, except the current
worktree and the main checkout; ``missing`` entries are skipped. Before a
release a liveness guard must clear it (no held occupancy lock, no live
process group in the dispatch process registry); an unmeasurable probe KEEPS.
``pool-*``/bench trees are reported, never released. Locked trees keep the
rules above; unlocked ones are released only when clean, nothing unpushed and
no OPEN PR (kept when ``gh``/the slug is unavailable). A detached ``gate-*``
tree is released when clean and its HEAD is on some ``origin/*`` ref, but only
after its lifetime-lock sidecar proves no gate seat is still using it: a held
lock (``live``) or a missing sidecar (``no_lock``, a pre-lock checkout) keeps
the tree, and the lock is re-taken for the removal itself so a seat that
starts in between also keeps it.

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

from gate_worktree import (  # noqa: E402  (sys.path is set above)
    gate_checkout_liveness, gate_lifetime_lock_path,
)
from worktree_inventory import (  # noqa: E402  (sys.path is set above)
    KIND_BENCH, KIND_DISPATCH, KIND_GATE, KIND_POOL,
    WorktreeInventoryError, inventory,
)

LOCK_REASON_PREFIX = "vnx preserve"
_DISPATCH_DIR_RE = re.compile(r"^dispatch-(?P<id>[A-Za-z0-9][A-Za-z0-9_-]*)$")

# Kinds whose trees this sweep reports but never releases: the pool manager
# reaps its own trees, and bench trees are preserved by design.
_NEVER_RELEASE_KINDS = (KIND_POOL, KIND_BENCH)

# Gate seat liveness (PR-4 round 2). A gate checkout carries a lifetime lock
# sidecar (``gate_worktree.LIFETIME_LOCK_SUFFIX``) held by the reviewing
# process for its whole lifetime. Only a sidecar that exists and is NOT held
# may go on to the ordinary release rule; everything else keeps the tree.
_GATE_LIVE_REASON = "gate seat live (lifetime lock held)"
_GATE_NO_LOCK_REASON = (
    "gate checkout without lifetime lock (created before the lock existed "
    "or not by create_gate_worktree) — worktree blijft staan"
)


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


def branch_has_open_pr(repo_slug: str | None, branch: str) -> bool | None:
    """``True`` when an OPEN PR exists for *branch*, ``False`` when gh answered
    and there is none, ``None`` when it cannot be measured (no slug, gh
    missing/timing out/failing, unparseable output). Only measured-and-empty
    clears the candidate. Thin wrapper so tests stub one call."""
    if repo_slug is None:
        return None
    try:
        proc = _run(
            ["gh", "pr", "list", "--repo", repo_slug, "--state", "open",
             "--head", branch, "--json", "number,state"],
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        entries = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(entries, list):
        return None
    # ``--state open`` already filtered: any entry is an open PR, and an
    # entry we cannot interpret is not evidence of "no open PR".
    return bool(entries)


@dataclass(frozen=True)
class WorktreeCandidate:
    """One candidate of the sweep. Built by ``discover_locked_dispatch_worktrees``
    (the legacy locked-only view) or by ``discover_sweep_candidates`` (every
    owned tree, with the measurements ``inventory`` already took)."""

    path: Path
    dispatch_dir_id: str | None
    branch: str | None  # short form, e.g. "dispatch/D-030d7e66"; None when detached
    head_sha: str | None
    lock_reason: str | None
    locked: bool = True
    kind: str = KIND_DISPATCH
    # ``dirty_count``/``unpushed`` are the inventory's measurements for the
    # unlocked path; None means "not measured" and keeps the tree.
    dirty_count: int | None = None
    unpushed: int | None = None


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


def claims_dir_for_repo(repo_root: Path) -> Path:
    """The project's dispatch claim registry — one derivation, shared by the
    sweep and its tests."""
    import vnx_paths
    from dispatch_worktree_isolation import claims_dir_for_state_dir

    return claims_dir_for_state_dir(vnx_paths.resolve_data_root(repo_root) / "state")


def check_liveness(repo_root: Path, dispatch_id: str | None) -> str | None:
    """Keep reason when the candidate has an active process or slot, else None.

    Signals: the dispatch's occupancy lock is held (a live process owns the
    slot; the claim JSON itself is NOT a signal — a preserved dirty tree keeps
    its claim file and is released all the time), or the dispatch process
    registry records a group whose members are alive. A candidate with no
    dispatch id cannot have either. Any failure to measure is a KEEP."""
    if not dispatch_id:
        return None
    import dispatch_worktree_isolation as dwi
    from dispatch_process_registry import live_process_groups

    try:
        held = dwi.probe_occupancy(claims_dir_for_repo(repo_root), dispatch_id) == "held"
        groups = live_process_groups(dispatch_id, repo_root=repo_root)
    except OSError as exc:
        return f"liveness: niet meetbaar ({exc}) — worktree blijft staan"
    if held:
        return f"liveness: de occupancy-lock van dispatch {dispatch_id!r} wordt door een levend proces vastgehouden — worktree blijft staan"
    if groups is None:
        return f"liveness: de proces-registry van {dispatch_id!r} is niet leesbaar — worktree blijft staan"
    if groups:
        return f"liveness: procesgroep {groups[0]} van dispatch {dispatch_id!r} leeft nog — worktree blijft staan"
    return None


def gate_seat_keep_reason(wt_path: Path) -> str | None:
    """Keep reason for a gate checkout, or ``None`` when it may be judged.

    A gate seat holds a lifetime lock on ``<checkout>.live`` for as long as its
    reviewer runs (``gate_worktree.create_gate_worktree``), so the checkout can
    never be deleted under a running reviewer. ``None`` is returned only on
    the positive ``released`` measurement — the sidecar exists and no process
    holds its lock. ``live``, ``no_lock``, ``unmeasured`` and any state this
    function does not recognise all keep the tree: unknown never releases.
    """
    detail: list[str] = []
    state = gate_checkout_liveness(wt_path, detail=detail)
    if state == "released":
        return None
    if state == "live":
        return _GATE_LIVE_REASON
    if state == "no_lock":
        return _GATE_NO_LOCK_REASON
    if state == "unmeasured":
        why = detail[0] if detail else "onbekende fout"
        return f"gate seat lifetime lock niet meetbaar ({why}) — worktree blijft staan"
    return f"gate seat lifetime lock: onbekende toestand {state!r} — worktree blijft staan"


def discover_locked_dispatch_worktrees(repo_root: Path) -> list[WorktreeCandidate]:
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
    result: list[WorktreeCandidate] = []
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
            WorktreeCandidate(
                path=resolved,
                dispatch_dir_id=m.group("id"),
                branch=branch,
                head_sha=rec.get("head"),
                lock_reason=lock_reason,
            )
        )
    return result


def discover_sweep_candidates(repo_root: Path) -> list[WorktreeCandidate]:
    """Every ``owned`` worktree of *repo_root* the sweep may judge.

    ``inventory`` is the one reader. A ``missing`` tree is skipped (nothing to
    release), as are the main checkout and — by the caller, which knows the
    cwd — the worktree this process runs from. Pool and bench trees stay
    candidates so the report shows them; ``evaluate_candidate`` never releases
    them. Raises ``WorktreeInventoryError`` when git cannot list at all: a
    partial candidate list must never look like a full sweep."""
    main_checkout = repo_root.resolve()
    candidates: list[WorktreeCandidate] = []
    for entry in inventory(repo_root):
        if not entry.owned or entry.missing or entry.path == main_checkout:
            continue
        match = _DISPATCH_DIR_RE.match(entry.path.name) if entry.kind == KIND_DISPATCH else None
        candidates.append(WorktreeCandidate(
            path=entry.path,
            dispatch_dir_id=match.group("id") if match else None,
            branch=entry.branch, head_sha=entry.head, lock_reason=entry.lock_reason,
            locked=entry.locked, kind=entry.kind,
            dirty_count=entry.dirty_count, unpushed=entry.unpushed,
        ))
    return candidates


def is_current_worktree(wt: WorktreeCandidate) -> bool:
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


def head_in_origin_ref(repo_root: Path, head_sha: str) -> bool | None:
    """True when an ``origin/*`` ref contains *head_sha*; ``None`` when git
    could not answer (unknown never releases). ``git branch --remotes
    --contains`` exits 0 with empty output when no remote branch holds it."""
    proc = run_git(
        ["branch", "--remotes", "--contains", head_sha, "--format=%(refname)"],
        cwd=repo_root,
    )
    if proc.returncode != 0:
        return None
    return any(
        line.strip().startswith("refs/remotes/origin/")
        for line in proc.stdout.splitlines() if line.strip()
    )


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


def remote_holds_head(repo_root: Path, wt: WorktreeCandidate, pr_number: int | None) -> tuple[bool, str]:
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
    wt: WorktreeCandidate,
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


def _evaluate_detached_gate(repo_root: Path, wt: WorktreeCandidate) -> Verdict:
    if wt.head_sha is None:
        return Verdict(False, "kon HEAD-sha van de gate-worktree niet bepalen — worktree blijft staan")
    contained = head_in_origin_ref(repo_root, wt.head_sha)
    if contained is None:
        return Verdict(False, "kon niet bepalen of HEAD in een origin/*-ref zit — worktree blijft staan")
    if not contained:
        return Verdict(False, f"detached gate-worktree; HEAD ({wt.head_sha[:12]}) zit in geen enkele origin/*-ref")
    return Verdict(
        True, f"detached gate-worktree, schoon; HEAD ({wt.head_sha[:12]}) zit in een origin/*-ref",
        delete_branch=False,
    )


def evaluate_unlocked_worktree(repo_root: Path, wt: WorktreeCandidate, repo_slug: str | None) -> Verdict:
    """Rules for a tree nobody locked. A gate tree is detached by design and
    released when its HEAD is on some ``origin/*`` ref; every other tree only
    when clean, nothing unpushed, and no OPEN pull request for its branch."""
    if wt.dirty_count is None:
        return Verdict(False, "werkmap-status onbekend (onbekend is niet schoon) — worktree blijft staan")
    if wt.dirty_count != 0:
        return Verdict(False, f"werkmap is niet schoon ({wt.dirty_count} gewijzigd pad(en)) — dataverlies-risico")
    if wt.kind == KIND_GATE and wt.branch is None:
        return _evaluate_detached_gate(repo_root, wt)
    if wt.branch is None:
        return Verdict(False, "detached HEAD buiten een gate-worktree — worktree blijft staan")
    if wt.unpushed is None:
        return Verdict(False, "onbekend of de branch commits op origin mist — worktree blijft staan")
    if wt.unpushed != 0:
        return Verdict(False, f"branch {wt.branch!r} heeft {wt.unpushed} commit(s) die geen origin/*-ref bereikt")
    has_open_pr = branch_has_open_pr(repo_slug, wt.branch)
    if has_open_pr is None:
        return Verdict(False, f"open-PR-status van {wt.branch!r} onmeetbaar (gh of origin-slug ontbreekt) — worktree blijft staan")
    if has_open_pr:
        return Verdict(False, f"branch {wt.branch!r} heeft een OPEN pull request — werk is nog in behandeling")
    return Verdict(
        True, f"niet gelockt, schoon, niets unpushed en geen OPEN PR voor {wt.branch!r}",
        delete_branch=wt.kind == KIND_DISPATCH,
    )


def evaluate_candidate(
    repo_root: Path,
    wt: WorktreeCandidate,
    repo_slug: str | None,
    decisions: dict[str, dict[str, Any]] | None = None,
) -> Verdict:
    """The whole decision for one candidate: owner-owned kinds first, then the
    liveness guard, then the locked or the unlocked rule set."""
    if wt.kind in _NEVER_RELEASE_KINDS:
        return Verdict(False, f"{wt.kind}-worktree wordt door zijn eigen beheerder opgeruimd — deze ronde blijft hij staan")
    live_reason = check_liveness(repo_root, wt.dispatch_dir_id)
    if live_reason:
        return Verdict(False, live_reason)
    if wt.kind == KIND_GATE:
        gate_reason = gate_seat_keep_reason(wt.path)
        if gate_reason:
            return Verdict(False, gate_reason)
    if wt.locked:
        if not (wt.lock_reason or "").startswith(LOCK_REASON_PREFIX):
            return Verdict(False, "handmatige worktree-lock (niet door de fabric gezet) — worktree blijft staan")
        return evaluate_worktree(repo_root, wt, repo_slug, decisions)
    return evaluate_unlocked_worktree(repo_root, wt, repo_slug)


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


class _GateLifetimeLockUnavailable(RuntimeError):
    """The sweep could not take a gate checkout's lifetime lock to release it."""


@contextmanager
def _held_gate_lifetime_lock(wt_path: Path) -> Iterator[None]:
    """Hold *wt_path*'s lifetime lock while the caller removes the checkout.

    Re-acquiring it non-blockingly closes the gap between the ``released``
    probe and the removal: when a seat started in between it now holds the
    lock, so this raises and the caller keeps the tree.
    """
    sidecar = gate_lifetime_lock_path(wt_path)
    try:
        fd = os.open(sidecar, os.O_RDWR)
    except OSError as exc:
        raise _GateLifetimeLockUnavailable(f"{sidecar} kan niet geopend worden ({exc})") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise _GateLifetimeLockUnavailable(f"{sidecar} wordt vastgehouden ({exc})") from exc
        yield
    finally:
        os.close(fd)


def _remove_worktree_and_branch(
    repo_root: Path, wt: WorktreeCandidate, *, delete_branch: bool,
) -> ReleaseResult:
    """Unlock/remove the checkout and, when asked, delete its local branch.

    The caller holds ``_flock_context`` — and, for a gate tree, the checkout's
    lifetime lock across the removal."""
    errors: list[str] = []
    if wt.locked:
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


def release_worktree(repo_root: Path, wt: WorktreeCandidate, *, delete_branch: bool) -> ReleaseResult:
    if wt.kind != KIND_GATE:
        with _flock_context(repo_root):
            return _remove_worktree_and_branch(repo_root, wt, delete_branch=delete_branch)

    # A gate checkout was measured ``released`` before this call. Take its
    # lifetime lock again for the whole removal so no seat can start between
    # the probe and `git worktree remove`; when it cannot be taken, keep it.
    try:
        with _held_gate_lifetime_lock(wt.path):
            with _flock_context(repo_root):
                result = _remove_worktree_and_branch(repo_root, wt, delete_branch=delete_branch)
    except _GateLifetimeLockUnavailable as exc:
        return ReleaseResult(
            released=False,
            errors=[f"gate lifetime lock niet verkregen: {exc} — worktree blijft staan"],
        )
    if result.released:
        try:
            gate_lifetime_lock_path(wt.path).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            result.errors.append(f"lifetime sidecar niet verwijderd: {exc}")
    return result


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
    kind: str
    locked: bool
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


def run_sweep(
    repo_root: Path,
    candidates: list[WorktreeCandidate],
    repo_slug: str | None,
    decisions: dict[str, dict[str, Any]],
    *,
    apply: bool,
) -> tuple[list[WorktreeReportEntry], int, int]:
    """Judge every candidate and, with *apply*, release the safe ones.

    Returns ``(report, released_count, skipped_self)``. ``evaluate_candidate``
    runs the liveness guard before any release decision."""
    report: list[WorktreeReportEntry] = []
    released = 0
    skipped_self = 0
    for wt in candidates:
        if is_current_worktree(wt):
            skipped_self += 1
            continue
        verdict = evaluate_candidate(repo_root, wt, repo_slug, decisions)
        entry: WorktreeReportEntry = {
            "dispatch_id": wt.dispatch_dir_id, "kind": wt.kind, "locked": wt.locked,
            "path": str(wt.path), "branch": wt.branch, "head_sha": wt.head_sha,
            "lock_reason": wt.lock_reason, "safe_to_release": verdict.safe,
            "reason": verdict.reason, "released": False, "errors": [],
            "delete_branch": verdict.delete_branch,
        }
        if verdict.safe and apply:
            result = release_worktree(repo_root, wt, delete_branch=verdict.delete_branch)
            entry["released"] = result.released
            entry["errors"] = result.errors
            if result.released:
                released += 1
        report.append(entry)
    return report, released, skipped_self


def _print_sweep_report(
    repo_root: Path,
    report: list[WorktreeReportEntry],
    *,
    as_json: bool,
    apply: bool,
    released: int,
    skipped_self: int,
) -> None:
    if as_json:
        print(json.dumps({"repo_root": str(repo_root), "candidates": report}, indent=2))
        return
    print(
        f"cleanup_reviewed_worktrees: {len(report) + skipped_self} door de fabric beheerde "
        f"worktree(s) gevonden ({skipped_self} eigen worktree overgeslagen)"
    )
    for entry in report:
        verdict_label = "SAFE_TO_RELEASE" if entry["safe_to_release"] else "BLIJFT_STAAN"
        state = "locked" if entry["locked"] else "unlocked"
        action = ""
        if entry["safe_to_release"]:
            if apply:
                action = " -> RELEASED" if entry["released"] else f" -> RELEASE FAILED: {entry['errors']}"
            else:
                action = " (dry-run, gebruik --apply om echt vrij te geven)"
        print(
            f"  [{verdict_label}] [{entry['kind']}/{state}] "
            f"{entry['dispatch_id'] or '-'} ({entry['branch']}): {entry['reason']}{action}"
        )
    if apply:
        print(f"cleanup_reviewed_worktrees: {released} worktree(s) vrijgegeven")
    else:
        safe_count = sum(1 for e in report if e["safe_to_release"])
        print(
            f"cleanup_reviewed_worktrees: {safe_count} van {len(report)} zijn "
            "SAFE_TO_RELEASE (dry-run — geen --apply meegegeven)"
        )


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

    try:
        candidates = discover_sweep_candidates(repo_root)
    except WorktreeInventoryError as exc:
        print(f"cleanup_reviewed_worktrees: {exc}", file=sys.stderr)
        return 2
    if args.only:
        wanted = set(args.only)
        candidates = [c for c in candidates if c.dispatch_dir_id in wanted]

    repo_slug = get_repo_slug(repo_root)
    if repo_slug is None:
        print("WAARSCHUWING: kon geen github.com owner/repo herkennen uit origin — PR-gebaseerde verificatie (squash-merges) wordt overgeslagen, alleen ancestor-of-main telt.", file=sys.stderr)

    report, released, skipped_self = run_sweep(
        repo_root, candidates, repo_slug, decisions, apply=args.apply
    )
    _print_sweep_report(
        repo_root, report, as_json=args.json, apply=args.apply,
        released=released, skipped_self=skipped_self,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
