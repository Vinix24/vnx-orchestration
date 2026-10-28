"""``scripts/lib/branch_gc.py`` — real tmp git repos, ``gh`` injected."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import branch_gc as bgc  # noqa: E402

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, **ENV})
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def make_repo(tmp_path):
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "remote", "add", "origin", str(origin))
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    return repo


def branch(repo, name, commits=1):
    git(repo, "checkout", "-q", "-b", name, "main")
    for index in range(commits):
        (repo / f"{name.replace('/', '-')}-{index}.txt").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"{name} {index}")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return head


def merge_main(repo, name):
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", name)
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")


def exists(repo, name):
    proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
                          cwd=repo, capture_output=True, text=True)
    return proc.returncode == 0


def gh_of(mapping=None, raises=None):
    def gh(name):
        if raises is not None:
            raise raises
        value = (mapping or {}).get(name, [])
        if isinstance(value, BaseException):
            raise value
        return value
    return gh


def pr(number, state, head):
    return {"number": number, "state": state, "headRefOid": head}


def one(repo, name, gh):
    decisions = bgc.plan(repo, gh=gh)
    matches = [decision for decision in decisions if decision.branch == name]
    assert len(matches) == 1, [decision.branch for decision in decisions]
    return matches[0]


def run(capsys, repo, *args, gh):
    rc = bgc.main(["--repo-root", str(repo), *args], gh=gh)
    out, _ = capsys.readouterr()
    return rc, out


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def test_dry_run_lists_a_merged_branch_and_apply_deletes_it(repo, capsys):
    head = branch(repo, "dispatch/merged")
    merge_main(repo, "dispatch/merged")
    git(repo, "push", "-q", "origin", "dispatch/merged")
    decision = one(repo, "dispatch/merged", gh_of())
    assert (decision.delete, decision.rule, decision.head) == (True, bgc.RULE_ANCESTOR, head)
    assert exists(repo, "dispatch/merged"), "plan() must not delete"
    rc, out = run(capsys, repo, gh=gh_of())
    assert rc == 0 and any(line.startswith("would-delete") and "dispatch/merged" in line
                           for line in out.splitlines())
    assert exists(repo, "dispatch/merged"), "a dry-run must not delete"
    rc, out = run(capsys, repo, "--apply", gh=gh_of())
    assert rc == 0 and any(line.startswith("deleted") and "dispatch/merged" in line
                           for line in out.splitlines())
    assert not exists(repo, "dispatch/merged")
    assert git(repo, "ls-remote", "origin", "refs/heads/dispatch/merged"), "remote is never touched"


def test_unmerged_and_non_dispatch_branches_are_kept(repo):
    branch(repo, "dispatch/unmerged")
    branch(repo, "feature/keep")
    merge_main(repo, "feature/keep")
    decisions = bgc.plan(repo, gh=gh_of())
    assert all(decision.branch.startswith("dispatch/") for decision in decisions)
    decision = one(repo, "dispatch/unmerged", gh_of())
    assert (decision.delete, decision.rule) == (False, bgc.RULE_NOT_MERGED)
    bgc.apply(decisions)
    assert exists(repo, "dispatch/unmerged") and exists(repo, "feature/keep")


def test_a_branch_checked_out_in_a_worktree_or_the_main_checkout_is_kept(repo, tmp_path):
    branch(repo, "dispatch/checked")
    merge_main(repo, "dispatch/checked")
    git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "dispatch/checked")
    decision = one(repo, "dispatch/checked", gh_of())
    assert (decision.delete, decision.rule) == (False, bgc.RULE_CHECKED_OUT)
    git(repo, "checkout", "-q", "-b", "dispatch/on-main", "main")
    try:
        assert one(repo, "dispatch/on-main", gh_of()).rule == bgc.RULE_CHECKED_OUT
    finally:
        git(repo, "checkout", "-q", "main")
    bgc.apply([decision])
    assert exists(repo, "dispatch/checked")


def test_a_merged_pr_with_the_same_head_deletes_the_branch(repo):
    head = branch(repo, "dispatch/pr-merged")
    decision = one(repo, "dispatch/pr-merged", gh_of({"dispatch/pr-merged": [pr(4, "MERGED", head)]}))
    assert (decision.delete, decision.rule, decision.pr_number) == (True, bgc.RULE_MERGED_PR, 4)
    bgc.apply([decision])
    assert not exists(repo, "dispatch/pr-merged")


def test_a_merged_pr_on_an_older_head_keeps_the_branch(repo):
    head = branch(repo, "dispatch/pr-extra", commits=2)
    merged_head = git(repo, "rev-parse", "dispatch/pr-extra~1")
    assert merged_head != head
    decision = one(repo, "dispatch/pr-extra", gh_of({"dispatch/pr-extra": [pr(5, "MERGED", merged_head)]}))
    assert (decision.delete, decision.rule) == (False, bgc.RULE_HEAD_DIFFERS)
    bgc.apply([decision])
    assert exists(repo, "dispatch/pr-extra")


def test_a_closed_unmerged_pr_keeps_the_branch(repo):
    head = branch(repo, "dispatch/pr-closed")
    decision = one(repo, "dispatch/pr-closed", gh_of({"dispatch/pr-closed": [pr(6, "CLOSED", head)]}))
    assert (decision.delete, decision.rule) == (False, bgc.RULE_CLOSED_UNMERGED)
    bgc.apply([decision])
    assert exists(repo, "dispatch/pr-closed")


def test_an_open_pr_keeps_the_branch_even_when_its_head_is_in_main(repo):
    head = branch(repo, "dispatch/pr-open")
    merge_main(repo, "dispatch/pr-open")
    decision = one(repo, "dispatch/pr-open", gh_of({"dispatch/pr-open": [pr(7, "OPEN", head)]}))
    assert (decision.delete, decision.rule) == (False, bgc.RULE_OPEN_PR)
    bgc.apply([decision])
    assert exists(repo, "dispatch/pr-open")


def test_a_failing_gh_leaves_only_ancestor_deletes(repo):
    branch(repo, "dispatch/ancestor")
    merge_main(repo, "dispatch/ancestor")
    branch(repo, "dispatch/no-evidence")
    decisions = bgc.plan(repo, gh=gh_of(raises=bgc.GhUnavailable("gh ontbreekt")))
    by_branch = {decision.branch: decision for decision in decisions}
    assert by_branch["dispatch/ancestor"].rule == bgc.RULE_ANCESTOR
    assert by_branch["dispatch/ancestor"].delete is True
    assert by_branch["dispatch/no-evidence"].rule == bgc.RULE_GH_UNAVAILABLE
    assert by_branch["dispatch/no-evidence"].delete is False
    bgc.apply(decisions)
    assert not exists(repo, "dispatch/ancestor") and exists(repo, "dispatch/no-evidence")


def test_malformed_gh_and_a_missing_origin_main_both_keep_the_branch(repo):
    branch(repo, "dispatch/unmerged")
    assert one(repo, "dispatch/unmerged", lambda name: "geen lijst").rule == bgc.RULE_GH_UNAVAILABLE
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert one(repo, "dispatch/unmerged", gh_of()).rule == bgc.RULE_BASE_UNREADABLE


def test_a_head_that_moves_between_plan_and_apply_is_kept(repo):
    head = branch(repo, "dispatch/moved")
    decision = one(repo, "dispatch/moved", gh_of({"dispatch/moved": [pr(8, "MERGED", head)]}))
    assert decision.delete is True
    moved = git(repo, "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"), "-p", head, "-m", "moved")
    git(repo, "update-ref", "refs/heads/dispatch/moved", moved)
    bgc.apply([decision])
    assert (decision.delete, decision.deleted, decision.rule) == (False, False, bgc.RULE_HEAD_MOVED)
    assert exists(repo, "dispatch/moved") and git(repo, "rev-parse", "dispatch/moved") == moved


def test_a_head_that_moves_between_verification_and_deletion_is_kept(repo, monkeypatch):
    """The codex reproduction: the ref is re-pointed after ``apply`` verified the
    head but before the delete runs. The forced delete must not carry the new,
    possibly unmerged, commit away with it."""
    head = branch(repo, "dispatch/race")
    decision = one(repo, "dispatch/race", gh_of({"dispatch/race": [pr(9, "MERGED", head)]}))
    assert (decision.delete, decision.rule) == (True, bgc.RULE_MERGED_PR)
    new_head = git(repo, "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"),
                   "-p", head, "-m", "unmerged work")
    real_rev_verify, raced = bgc._rev_verify, []

    def rev_verify_then_move(root, ref):
        current = real_rev_verify(root, ref)
        if ref == "refs/heads/dispatch/race" and not raced:
            git(repo, "update-ref", ref, new_head)
            raced.append(current)
        return current

    monkeypatch.setattr(bgc, "_rev_verify", rev_verify_then_move)
    bgc.apply([decision])
    assert (decision.delete, decision.deleted, decision.rule) == (False, False, bgc.RULE_HEAD_MOVED)
    assert raced == [head], "the race must have happened between verification and deletion"
    assert exists(repo, "dispatch/race") and git(repo, "rev-parse", "dispatch/race") == new_head
    git(repo, "cat-file", "-e", new_head)


def test_a_branch_checked_out_after_the_plan_is_kept(repo, tmp_path):
    """``update-ref`` does not know about worktrees, so the checked-out set is
    re-read right before the delete: a branch a worktree took since the plan
    stays."""
    head = branch(repo, "dispatch/late-checkout")
    merge_main(repo, "dispatch/late-checkout")
    decision = one(repo, "dispatch/late-checkout", gh_of())
    assert (decision.delete, decision.rule, decision.head) == (True, bgc.RULE_ANCESTOR, head)
    git(repo, "worktree", "add", "-q", str(tmp_path / "late-wt"), "dispatch/late-checkout")
    bgc.apply([decision])
    assert (decision.delete, decision.deleted, decision.rule) == (False, False, bgc.RULE_CHECKED_OUT)
    assert str(tmp_path / "late-wt") in decision.reason
    assert exists(repo, "dispatch/late-checkout")


def test_an_unreadable_worktree_inventory_raises_a_branch_gc_error(repo, monkeypatch):
    import worktree_inventory as wi

    branch(repo, "dispatch/merged")
    merge_main(repo, "dispatch/merged")
    decision = one(repo, "dispatch/merged", gh_of())
    assert decision.delete is True

    def broken_inventory(*args, **kwargs):
        raise wi.WorktreeInventoryError("git worktree list faalde")

    monkeypatch.setattr(wi, "inventory", broken_inventory)
    with pytest.raises(bgc.BranchGcError):
        bgc.apply([decision])
    assert exists(repo, "dispatch/merged")


def test_json_output_carries_counts_and_decisions(repo, capsys):
    branch(repo, "dispatch/merged")
    merge_main(repo, "dispatch/merged")
    branch(repo, "dispatch/unmerged")
    rc, out = run(capsys, repo, "--json", gh=gh_of())
    payload = json.loads(out)
    assert rc == 0 and payload["applied"] is False
    assert payload["counts"] == {bgc.RULE_ANCESTOR: 1, bgc.RULE_NOT_MERGED: 1}
    assert {entry["branch"]: entry["rule"] for entry in payload["decisions"]} == {
        "dispatch/merged": bgc.RULE_ANCESTOR, "dispatch/unmerged": bgc.RULE_NOT_MERGED}
    assert exists(repo, "dispatch/merged")


def test_main_without_repo_root_asks_git_for_the_toplevel(repo, monkeypatch, capsys):
    """``main`` derives its default root from git, not from the project-root resolver.

    ``scripts/lib/branch_gc.py`` is anchored inside a real temporary checkout by
    moving ``__file__`` there (no source is copied), so
    ``git -C <module dir> rev-parse --show-toplevel`` is the call under test.
    ``subprocess.run`` is wrapped rather than replaced so the command shape and
    the resolved root are both observable while git itself still answers.
    """
    module_dir = repo / "scripts" / "lib"
    module_dir.mkdir(parents=True)
    monkeypatch.setattr(bgc, "__file__", str(module_dir / "branch_gc.py"))
    calls = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(bgc.subprocess, "run", spy)
    rc = bgc.main(["--json"], gh=gh_of())
    out, err = capsys.readouterr()

    assert rc == 0, err
    assert ["git", "-C", str(module_dir), "rev-parse", "--show-toplevel"] in calls
    assert Path(json.loads(out)["repo_root"]).resolve() == repo.resolve()


def test_a_failing_toplevel_probe_exits_2_with_one_stderr_line(tmp_path, monkeypatch, capsys):
    """A ``rev-parse --show-toplevel`` that fails is exit 2, never a guessed root."""
    module_dir = tmp_path / "stub" / "scripts" / "lib"
    module_dir.mkdir(parents=True)
    monkeypatch.setattr(bgc, "__file__", str(module_dir / "branch_gc.py"))

    class Failed:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository (or any of the parent directories): .git\n"

    def failing_run(cmd, *args, **kwargs):
        return Failed()

    monkeypatch.setattr(bgc.subprocess, "run", failing_run)
    rc = bgc.main([], gh=gh_of())
    out, err = capsys.readouterr()

    assert rc == 2
    assert out == ""
    lines = err.strip().splitlines()
    assert len(lines) == 1, err
    assert lines[0].startswith("branch_gc: ")
    assert "--show-toplevel" in lines[0]
