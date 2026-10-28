#!/usr/bin/env python3
"""branch_gc.py — remove local ``dispatch/*`` branches that hold no work.

Measured 27-10-2026: 497 local ``dispatch/*`` branches against 41 worktrees,
and nothing removes them. ``gate_runner._capture_branch_heads`` ignores the
prefix, so a delete can never trip a running gate seat. A branch goes only
when (1) it starts with ``dispatch/``; (2) no worktree checks it out, from
``worktree_inventory.inventory``, main checkout included; (3) its head is an
ancestor of ``origin/main`` (3a) or its PR is ``MERGED`` with ``headRefOid``
exactly the local head (3b); (4) no ``OPEN`` PR has it as head.

Unknown evidence keeps the branch: ``CLOSED`` proves no merge, a ``MERGED``
PR on an older head proves later work, and without a usable ``gh`` rules 3b
and 4 are unmeasurable, so only 3a deletes and the rest stays as
``gh_unavailable``. :func:`apply` deletes with ``git update-ref -d
refs/heads/<branch> <verified head>``, so a ref that moved after the plan is
refused instead of carried away, and re-reads the checked-out set right before
each delete because ``update-ref`` does not check worktrees the way
``git branch -D`` does. Remotes are never touched; the CLI is dry-run unless
``--apply``.

BILLING SAFETY: no Anthropic SDK, CLI-only git/gh via subprocess.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DISPATCH_PREFIX = "dispatch/"
BASE_REF = "origin/main"

# A deleted branch carries the rule that deleted it; a kept branch carries
# the rule that would have had to hold and did not.
(RULE_ANCESTOR, RULE_MERGED_PR, RULE_CHECKED_OUT, RULE_OPEN_PR, RULE_GH_UNAVAILABLE,
 RULE_NOT_MERGED, RULE_HEAD_DIFFERS, RULE_CLOSED_UNMERGED, RULE_HEAD_UNREADABLE,
 RULE_BASE_UNREADABLE, RULE_HEAD_MOVED, RULE_DELETE_FAILED) = (
    "ancestor_of_main", "merged_pr_head", "checked_out", "open_pr", "gh_unavailable",
    "not_merged", "head_differs_from_merged_pr", "closed_unmerged", "head_unreadable",
    "base_unreadable", "head_moved", "delete_failed")

GhCallable = Callable[[str], list[dict[str, Any]]]


class BranchGcError(RuntimeError):
    """A git read the plan depends on failed; there is no partial plan."""


class GhUnavailable(RuntimeError):
    """``gh`` could not answer, so PR state is unmeasurable."""


@dataclass
class BranchDecision:
    """One branch, its head, the deciding rule and — when kept — why.

    ``repo_root`` is carried so :func:`apply` can re-read the head and run the
    delete without a second argument."""

    repo_root: Path
    branch: str
    head: str | None
    delete: bool
    rule: str
    reason: str = ""
    pr_number: int | None = None
    deleted: bool = False

    def keep(self, rule: str, reason: str) -> None:
        self.delete, self.rule, self.reason = False, rule, reason

    def to_dict(self) -> dict[str, Any]:
        return {"branch": self.branch, "head": self.head, "delete": self.delete,
                "deleted": self.deleted, "rule": self.rule, "reason": self.reason,
                "pr_number": self.pr_number}


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=str(repo_root), capture_output=True, text=True,
                              timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BranchGcError(f"git {' '.join(args)} niet uitvoerbaar: {exc}") from exc


def _rev_verify(repo_root: Path, ref: str) -> str | None:
    proc = _git(repo_root, "rev-parse", "--verify", "--quiet", ref)
    sha = (proc.stdout or "").strip()
    return sha if proc.returncode == 0 and sha else None


def _dispatch_heads(repo_root: Path) -> dict[str, str]:
    """Every local ``dispatch/*`` branch and its head, in one ``for-each-ref``.
    A failed read raises: an empty answer must never look like "nothing to do"."""
    proc = _git(repo_root, "for-each-ref", "--format=%(refname:short) %(objectname)",
                f"refs/heads/{DISPATCH_PREFIX}")
    if proc.returncode != 0:
        raise BranchGcError(f"git for-each-ref faalde: {(proc.stderr or '').strip()}")
    heads: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        name, _, sha = line.strip().partition(" ")
        if name.startswith(DISPATCH_PREFIX) and sha:
            heads[name] = sha
    return heads


def _checked_out(repo_root: Path) -> dict[str, str]:
    """Branch -> the worktree path that checks it out, from the one inventory.

    An unreadable inventory raises: an empty set would read as "free to
    delete", so the failure stays loud as :class:`BranchGcError`."""
    from worktree_inventory import WorktreeInventoryError, inventory

    try:
        entries = inventory(repo_root)
    except WorktreeInventoryError as exc:
        raise BranchGcError(f"worktree-inventaris niet leesbaar: {exc}") from exc
    return {entry.branch: str(entry.path) for entry in entries if entry.branch}


def _ancestor(repo_root: Path, head: str, base_head: str | None) -> bool | None:
    if base_head is None:
        return None
    proc = _git(repo_root, "merge-base", "--is-ancestor", head, base_head)
    return True if proc.returncode == 0 else False if proc.returncode == 1 else None


def _prs(gh: GhCallable, branch: str) -> tuple[list[dict[str, Any]] | None, str]:
    """The branch's PR entries, or (None, why) when the answer is unmeasurable."""
    try:
        entries = gh(branch)
    except Exception as exc:  # vnx-silent-except: unreadable PR state keeps the branch
        return None, str(exc) or exc.__class__.__name__
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        return None, "gh gaf geen bruikbare lijst"
    return entries, ""


def _int_or_none(entry: dict[str, Any]) -> int | None:
    number = entry.get("number")
    return number if isinstance(number, int) else None


def _decide(repo_root: Path, branch: str, head: str | None, checked_out: dict[str, str],
            base_head: str | None, gh: GhCallable) -> BranchDecision:
    """Apply the four rules to one branch. Every unknown answer keeps it."""
    def keep(rule: str, reason: str, number: int | None = None) -> BranchDecision:
        return BranchDecision(repo_root, branch, head, False, rule, reason, number)

    if head is None:
        return keep(RULE_HEAD_UNREADABLE, "kop van de branch niet leesbaar")
    if branch in checked_out:
        return keep(RULE_CHECKED_OUT, f"worktree {checked_out[branch]} checkt deze branch uit")
    entries, gh_reason = _prs(gh, branch)
    if entries is not None:
        for entry in entries:
            if str(entry.get("state") or "").upper() == "OPEN":
                return keep(RULE_OPEN_PR, f"PR #{_int_or_none(entry)} staat open", _int_or_none(entry))
    ancestor = _ancestor(repo_root, head, base_head)
    if ancestor is True:
        return BranchDecision(repo_root, branch, head, True, RULE_ANCESTOR)
    if entries is None:
        reason = gh_reason if base_head else f"{gh_reason}; {BASE_REF} niet leesbaar"
        return keep(RULE_GH_UNAVAILABLE, reason)
    if base_head is None:
        return keep(RULE_BASE_UNREADABLE, f"{BASE_REF} niet leesbaar")
    merged = [entry for entry in entries if str(entry.get("state") or "").upper() == "MERGED"]
    exact = [entry for entry in merged if str(entry.get("headRefOid") or "") == head]
    if exact:
        return BranchDecision(repo_root, branch, head, True, RULE_MERGED_PR, "", _int_or_none(exact[0]))
    if merged:
        return keep(RULE_HEAD_DIFFERS,
                    f"PR #{_int_or_none(merged[0])} is MERGED op {merged[0].get('headRefOid')}, "
                    f"branch staat op {head}", _int_or_none(merged[0]))
    closed = [entry for entry in entries if str(entry.get("state") or "").upper() == "CLOSED"]
    if closed:
        return keep(RULE_CLOSED_UNMERGED, f"PR #{_int_or_none(closed[0])} is CLOSED zonder merge",
                    _int_or_none(closed[0]))
    return keep(RULE_NOT_MERGED, "geen merge-bewijs")


class GhClient:
    """Default ``gh`` evidence: ``gh pr list --head <branch> --state all``."""

    def __init__(self, repo_root: Path, *, timeout: float = 30.0) -> None:
        self.repo_root, self.timeout = Path(repo_root), timeout

    def __call__(self, branch: str) -> list[dict[str, Any]]:
        cmd = ["gh", "pr", "list", "--state", "all", "--head", branch,
               "--json", "number,state,headRefOid"]
        try:
            proc = subprocess.run(cmd, cwd=str(self.repo_root), capture_output=True, text=True,
                                  timeout=self.timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GhUnavailable(f"gh niet uitvoerbaar: {exc}") from exc
        if proc.returncode != 0:
            raise GhUnavailable(f"gh pr list faalde (exit {proc.returncode}): "
                                f"{(proc.stderr or '').strip()[:300] or 'geen stderr'}")
        try:
            data = json.loads(proc.stdout or "")
        except ValueError as exc:
            raise GhUnavailable("gh pr list gaf geen JSON") from exc
        if not isinstance(data, list):
            raise GhUnavailable("gh pr list gaf geen lijst")
        return [entry for entry in data if isinstance(entry, dict)]


def plan(repo_root: Path, *, gh: GhCallable | None = None) -> list[BranchDecision]:
    """One decision per local ``dispatch/*`` branch, in name order.

    *gh* is the injection point for tests. An unreadable branch or worktree
    inventory raises — a partial plan must never look complete."""
    root = Path(repo_root).resolve()
    checked_out, base_head = _checked_out(root), _rev_verify(root, f"refs/remotes/{BASE_REF}")
    gh_call: GhCallable = gh if gh is not None else GhClient(root)
    return [_decide(root, branch, head, checked_out, base_head, gh_call)
            for branch, head in sorted(_dispatch_heads(root).items())]


def _delete_at_head(decision: BranchDecision) -> None:
    """Compare-and-delete one ref at the head the plan verified.

    ``update-ref -d`` refuses when the ref no longer points at that head; a
    refusal keeps the branch (``head_moved``) and is never retried, so the
    other deletions carry on."""
    expected = decision.head
    if not expected:  # fail closed: a delete needs a head to compare against
        decision.keep(RULE_HEAD_UNREADABLE, "geen geverifieerde kop om op te deleten")
        return
    proc = _git(decision.repo_root, "update-ref", "-d", f"refs/heads/{decision.branch}", expected)
    if proc.returncode == 0:
        decision.deleted = True
        return
    after = _rev_verify(decision.repo_root, f"refs/heads/{decision.branch}")
    if after != expected:
        decision.keep(RULE_HEAD_MOVED,
                      f"update-ref weigerde: kop verschoof van {expected} naar {after or 'weg'}")
    else:
        decision.keep(RULE_DELETE_FAILED,
                      (proc.stderr or "").strip()[:300] or "git update-ref -d faalde")


def apply(decisions: list[BranchDecision]) -> list[BranchDecision]:
    """Delete every planned branch at the head the plan verified.

    A head that moved (or became unreadable) is kept. The checked-out set is
    re-read right before each delete because ``update-ref`` — unlike
    ``git branch -D`` — does not check worktrees; a branch that a worktree now
    checks out is kept as ``checked_out``. Returns the same list."""
    for decision in decisions:
        if not decision.delete:
            continue
        current = _rev_verify(decision.repo_root, f"refs/heads/{decision.branch}")
        if current is None:
            decision.keep(RULE_HEAD_UNREADABLE, "kop niet leesbaar vlak voor de delete")
            continue
        if current != decision.head:
            decision.keep(RULE_HEAD_MOVED, f"kop verschoof van {decision.head} naar {current}")
            continue
        checked_out = _checked_out(decision.repo_root)
        if decision.branch in checked_out:
            decision.keep(RULE_CHECKED_OUT,
                          f"worktree {checked_out[decision.branch]} checkt deze branch nu uit")
            continue
        _delete_at_head(decision)
    return decisions


def _counts(decisions: list[BranchDecision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.rule] = counts.get(decision.rule, 0) + 1
    return dict(sorted(counts.items()))


def _print_report(decisions: list[BranchDecision], *, applied: bool) -> None:
    for decision in decisions:
        action = "deleted" if decision.deleted else "would-delete" if decision.delete else "keep"
        detail = f" {decision.reason}" if decision.reason else ""
        print(f"{action:12} {decision.branch} {(decision.head or '?')[:12]} [{decision.rule}]{detail}")
    summary = ", ".join(f"{rule}={count}" for rule, count in _counts(decisions).items()) or "geen branches"
    print(f"\n{len(decisions)} dispatch-branches: {summary}")
    if not applied:
        print("dry-run: niets verwijderd (gebruik --apply om te verwijderen)")


def main(argv: list[str] | None = None, *, gh: GhCallable | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ruim lokale dispatch/*-branches op zonder werk.")
    parser.add_argument("--repo-root", default=None, help="repo-root (standaard: project-root van dit script)")
    parser.add_argument("--apply", action="store_true", help="verwijder de branches (standaard: dry-run)")
    parser.add_argument("--json", action="store_true", help="print de beslissingen als JSON")
    args = parser.parse_args(argv)
    try:
        if args.repo_root:
            root = Path(args.repo_root).resolve()
        else:
            from project_root import resolve_project_root

            root = resolve_project_root(__file__)
        decisions = plan(root, gh=gh)
    except Exception as exc:  # vnx-silent-except: reported as a non-zero exit, not a traceback
        print(f"branch_gc: {exc}", file=sys.stderr)
        return 2
    planned = sum(1 for decision in decisions if decision.delete)
    failed = 0
    if args.apply:
        decisions = apply(decisions)
        failed = planned - sum(1 for decision in decisions if decision.deleted)
    if args.json:
        print(json.dumps({"repo_root": str(root), "applied": args.apply, "counts": _counts(decisions),
                          "decisions": [decision.to_dict() for decision in decisions]},
                         indent=2, sort_keys=True))
    else:
        _print_report(decisions, applied=args.apply)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
