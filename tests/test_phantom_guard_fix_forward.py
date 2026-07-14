"""test_phantom_guard_fix_forward.py — fix-forward false-reject fix.

A fix-forward dispatch pushes its commit onto an EXISTING PR branch (per its instruction)
instead of its own dispatch/<id> worktree branch, so the own-worktree diff reads empty even
though the commit really landed. Covers:

1. phantom_guard.resolve_pr_head_branch — PR-id -> head-branch resolution via `gh pr view`,
   isolated from real gh/network via a stubbed subprocess.run.
2. dispatch_envelope._resolve_fix_forward_diff — the caller-side diff-source fix, unit-tested
   with monkeypatched resolution + against a REAL local git repo (git fetch + git diff really
   run) so the plumbing itself is proven, not just the branching logic.
3. The three mandatory end-to-end scenarios via phantom_guard.phantom_guard(): fix-forward
   passes, normal dispatch is unchanged, genuinely-empty still phantom.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_envelope
import phantom_guard as pg
from dispatch_envelope import EnvelopeSpec, _resolve_fix_forward_diff


# ---------------------------------------------------------------------------
# resolve_pr_head_branch — gh CLI resolution, isolated from real gh/network
# ---------------------------------------------------------------------------


@dataclass
class _FakeProc:
    returncode: int
    stdout: str = ""


def test_resolve_pr_head_branch_empty_pr_id_no_subprocess_call(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("subprocess.run must not be called for an empty pr_id")

    monkeypatch.setattr(pg.subprocess, "run", _boom)
    assert pg.resolve_pr_head_branch("") is None
    assert pg.resolve_pr_head_branch(None) is None


def test_resolve_pr_head_branch_success(monkeypatch):
    monkeypatch.setattr(
        pg.subprocess, "run",
        lambda *a, **k: _FakeProc(0, '{"headRefName": "dispatch/20260710-existing-pr"}'),
    )
    assert pg.resolve_pr_head_branch("1161") == "dispatch/20260710-existing-pr"


def test_resolve_pr_head_branch_gh_nonzero_exit_returns_none(monkeypatch):
    monkeypatch.setattr(pg.subprocess, "run", lambda *a, **k: _FakeProc(1, ""))
    assert pg.resolve_pr_head_branch("999999") is None


def test_resolve_pr_head_branch_malformed_json_returns_none(monkeypatch):
    monkeypatch.setattr(pg.subprocess, "run", lambda *a, **k: _FakeProc(0, "not json"))
    assert pg.resolve_pr_head_branch("1161") is None


def test_resolve_pr_head_branch_timeout_returns_none(monkeypatch):
    def _raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=10)

    monkeypatch.setattr(pg.subprocess, "run", _raise_timeout)
    assert pg.resolve_pr_head_branch("1161") is None


def test_resolve_pr_head_branch_gh_missing_returns_none(monkeypatch):
    def _raise_oserror(*a, **k):
        raise OSError("gh: command not found")

    monkeypatch.setattr(pg.subprocess, "run", _raise_oserror)
    assert pg.resolve_pr_head_branch("1161") is None


def test_resolve_pr_head_branch_blank_headref_returns_none(monkeypatch):
    monkeypatch.setattr(pg.subprocess, "run", lambda *a, **k: _FakeProc(0, '{"headRefName": ""}'))
    assert pg.resolve_pr_head_branch("1161") is None


# ---------------------------------------------------------------------------
# _resolve_fix_forward_diff — pure branching, mocked resolution
# ---------------------------------------------------------------------------


def _spec(*, pr_id: Optional[str], tmp_path: Path) -> EnvelopeSpec:
    return EnvelopeSpec(
        dispatch_id="fixfwd-dispatch-001",
        terminal_id="T1",
        provider="claude",
        model="sonnet",
        instruction="fix forward onto the existing PR branch",
        role="backend-developer",
        pr_id=pr_id,
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )


def test_non_empty_own_diff_short_circuits_no_resolution_attempted(tmp_path, monkeypatch):
    # Normal dispatch: a real own-worktree diff must be returned unchanged and must NEVER
    # trigger a gh/git resolution call — pr_id being set must not matter here.
    def _boom(*a, **k):
        raise AssertionError("resolve_pr_head_branch must not be called when own_diff is non-empty")

    monkeypatch.setattr(pg, "resolve_pr_head_branch", _boom)
    spec = _spec(pr_id="1161", tmp_path=tmp_path)
    own_diff = "diff --git a/x b/x\n+hello\n"
    assert _resolve_fix_forward_diff(spec, own_diff) == own_diff


def test_empty_own_diff_no_pr_id_returns_unchanged_no_resolution(tmp_path, monkeypatch):
    # No pr_id -> not a fix-forward candidate; empty own_diff passes through untouched.
    def _boom(*a, **k):
        raise AssertionError("resolve_pr_head_branch must not be called without pr_id")

    monkeypatch.setattr(pg, "resolve_pr_head_branch", _boom)
    spec = _spec(pr_id=None, tmp_path=tmp_path)
    assert _resolve_fix_forward_diff(spec, "") == ""
    assert _resolve_fix_forward_diff(spec, None) is None


def test_empty_own_diff_pr_id_set_resolution_fails_falls_back_to_own_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "resolve_pr_head_branch", lambda pr_id, **k: None)
    spec = _spec(pr_id="1161", tmp_path=tmp_path)
    assert _resolve_fix_forward_diff(spec, "") == ""


def test_empty_own_diff_pr_id_set_branch_resolved_but_diff_empty_falls_back(tmp_path, monkeypatch):
    # Branch resolves, but its diff against base is also empty (e.g. not pushed yet) — must
    # still fall back to own_diff, not manufacture non-empty evidence.
    # repo=tmp_path (not a real git checkout) so this stays isolated: the "git fetch" best-effort
    # call harmlessly no-ops (check=False) and compute_branch_diff is stubbed directly, so no real
    # git/subprocess plumbing — and critically, no monkeypatching of the shared subprocess module
    # itself (that would also break project_root.py's own subprocess.check_output call, since
    # `dispatch_envelope.subprocess` and `project_root.subprocess` are the SAME module object).
    monkeypatch.setattr(pg, "resolve_pr_head_branch", lambda pr_id, **k: "some-branch")
    monkeypatch.setattr(pg, "compute_branch_diff", lambda *a, **k: "")
    spec = _spec(pr_id="1161", tmp_path=tmp_path)
    assert _resolve_fix_forward_diff(spec, "", repo=tmp_path) == ""


def test_empty_own_diff_pr_id_set_pushed_diff_non_empty_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "resolve_pr_head_branch", lambda pr_id, **k: "some-branch")
    monkeypatch.setattr(pg, "compute_branch_diff", lambda *a, **k: "diff --git a/y b/y\n+fix\n")
    spec = _spec(pr_id="1161", tmp_path=tmp_path)
    result = _resolve_fix_forward_diff(spec, "", repo=tmp_path)
    assert result == "diff --git a/y b/y\n+fix\n"


def test_resolution_exception_never_raises_falls_back_to_own_diff(tmp_path, monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(pg, "resolve_pr_head_branch", _raise)
    spec = _spec(pr_id="1161", tmp_path=tmp_path)
    # must not raise
    assert _resolve_fix_forward_diff(spec, "") == ""


# ---------------------------------------------------------------------------
# Real git repo fixture — proves the actual git plumbing (fetch + diff), not just branching
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


@pytest.fixture()
def git_fixture(tmp_path: Path):
    """A bare 'origin' remote + a working checkout, mirroring the real fabric shape: the
    dispatch orchestrator's own repo checkout with a remote it fetches pushed branches from.
    """
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    bare.mkdir()
    _run_git(bare, "init", "--bare", "-b", "main")

    work.mkdir()
    _run_git(work, "init", "-b", "main")
    _run_git(work, "config", "user.email", "test@example.com")
    _run_git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    _run_git(work, "add", "README.md")
    _run_git(work, "commit", "-m", "base commit")
    _run_git(work, "remote", "add", "origin", str(bare))
    _run_git(work, "push", "origin", "main")
    return work


def _push_branch_with_commit(work: Path, branch: str, filename: str) -> None:
    """Simulate a worker pushing a fix-forward commit onto an existing PR branch."""
    _run_git(work, "checkout", "-b", branch, "main")
    (work / filename).write_text("fix-forward change\n", encoding="utf-8")
    _run_git(work, "add", filename)
    _run_git(work, "commit", "-m", f"fix-forward: {filename}")
    _run_git(work, "push", "origin", branch)
    _run_git(work, "checkout", "main")
    _run_git(work, "branch", "-D", branch)  # own worktree no longer carries the branch locally


def _push_branch_no_commit(work: Path, branch: str) -> None:
    """Simulate an 'existing PR branch' with nothing new pushed onto it (still == main)."""
    _run_git(work, "checkout", "-b", branch, "main")
    _run_git(work, "push", "origin", branch)
    _run_git(work, "checkout", "main")
    _run_git(work, "branch", "-D", branch)


def test_fix_forward_real_git_pushed_branch_diff_resolved(git_fixture, monkeypatch):
    # own worktree has NOTHING (own_diff = ""); the fix-forward commit is only on origin's
    # pre-existing PR branch. resolve_pr_head_branch is stubbed (no real gh call) to point at
    # that real branch; git fetch + git diff run for REAL against the bare origin.
    _push_branch_with_commit(git_fixture, "dispatch/original-pr-branch", "fix.txt")
    monkeypatch.setattr(pg, "resolve_pr_head_branch", lambda pr_id, **k: "dispatch/original-pr-branch")

    spec = EnvelopeSpec(
        dispatch_id="fixfwd-dispatch-002", terminal_id="T1", provider="claude", model="sonnet",
        instruction="fix forward", role="backend-developer", pr_id="1161",
        state_dir=git_fixture / "state", data_dir=git_fixture / "data",
    )
    result = _resolve_fix_forward_diff(spec, "", base_ref="main", repo=git_fixture)
    assert result is not None and result.strip()
    assert "fix.txt" in result


def test_fix_forward_real_git_genuinely_empty_pushed_branch_falls_back(git_fixture, monkeypatch):
    # the "existing PR branch" resolves but carries NO diff from base (nothing was pushed yet)
    # -> falls back to own_diff (still empty here) so the caller still reads it as empty.
    _push_branch_no_commit(git_fixture, "dispatch/stale-pr-branch")
    monkeypatch.setattr(pg, "resolve_pr_head_branch", lambda pr_id, **k: "dispatch/stale-pr-branch")

    spec = EnvelopeSpec(
        dispatch_id="fixfwd-dispatch-003", terminal_id="T1", provider="claude", model="sonnet",
        instruction="fix forward", role="backend-developer", pr_id="1161",
        state_dir=git_fixture / "state", data_dir=git_fixture / "data",
    )
    result = _resolve_fix_forward_diff(spec, "", base_ref="main", repo=git_fixture)
    assert (result or "") == ""


def test_fix_forward_real_git_unresolvable_branch_falls_back(git_fixture, monkeypatch):
    # gh cannot resolve pr_id at all (deleted PR, bad id, no gh) -> ABSTAIN to own_diff, never
    # raise, never fabricate a diff.
    monkeypatch.setattr(pg, "resolve_pr_head_branch", lambda pr_id, **k: None)

    spec = EnvelopeSpec(
        dispatch_id="fixfwd-dispatch-004", terminal_id="T1", provider="claude", model="sonnet",
        instruction="fix forward", role="backend-developer", pr_id="99999-does-not-exist",
        state_dir=git_fixture / "state", data_dir=git_fixture / "data",
    )
    result = _resolve_fix_forward_diff(spec, "", base_ref="main", repo=git_fixture)
    assert (result or "") == ""


# ---------------------------------------------------------------------------
# End-to-end: the three mandatory Verify scenarios, through phantom_guard() itself
# ---------------------------------------------------------------------------


def test_verify_fix_forward_dispatch_passes_not_phantom(git_fixture, monkeypatch):
    """Fix-forward dispatch: own worktree empty but pushed branch carries a real commit ->
    guard PASSES (not phantom)."""
    _push_branch_with_commit(git_fixture, "dispatch/original-pr-branch", "fix.txt")
    monkeypatch.setattr(pg, "resolve_pr_head_branch", lambda pr_id, **k: "dispatch/original-pr-branch")

    spec = EnvelopeSpec(
        dispatch_id="fixfwd-e2e-pass", terminal_id="T1", provider="claude", model="sonnet",
        instruction="fix forward", role="backend-developer", pr_id="1161",
        state_dir=git_fixture / "state", data_dir=git_fixture / "data",
    )
    effective_diff = _resolve_fix_forward_diff(spec, "", base_ref="main", repo=git_fixture)
    verdict = pg.phantom_guard(status="done", worktree_diff=effective_diff, token_usage=None,
                               role="backend-developer")
    assert not verdict.is_phantom


def test_verify_normal_dispatch_non_empty_own_diff_unchanged(tmp_path):
    """Normal dispatch with a non-empty own-worktree diff -> still passes (unchanged)."""
    spec = _spec(pr_id=None, tmp_path=tmp_path)
    own_diff = "diff --git a/x b/x\n+real work\n"
    effective_diff = _resolve_fix_forward_diff(spec, own_diff)
    assert effective_diff == own_diff
    verdict = pg.phantom_guard(status="done", worktree_diff=effective_diff, token_usage=None,
                               role="backend-developer")
    assert not verdict.is_phantom


def test_verify_genuinely_empty_case_still_phantom(git_fixture, monkeypatch):
    """Genuinely empty case (no own-worktree diff AND no pushed-branch commit) -> still caught
    as phantom."""
    _push_branch_no_commit(git_fixture, "dispatch/stale-pr-branch")
    monkeypatch.setattr(pg, "resolve_pr_head_branch", lambda pr_id, **k: "dispatch/stale-pr-branch")

    spec = EnvelopeSpec(
        dispatch_id="fixfwd-e2e-phantom", terminal_id="T1", provider="claude", model="sonnet",
        instruction="fix forward", role="backend-developer", pr_id="1161",
        state_dir=git_fixture / "state", data_dir=git_fixture / "data",
    )
    effective_diff = _resolve_fix_forward_diff(spec, "", base_ref="main", repo=git_fixture)
    verdict = pg.phantom_guard(status="done", worktree_diff=effective_diff, token_usage=None,
                               role="backend-developer")
    assert verdict.is_phantom


def test_verify_genuinely_empty_no_pr_id_still_phantom(tmp_path):
    """No pr_id at all (not even a fix-forward candidate) + empty own diff -> still phantom."""
    spec = _spec(pr_id=None, tmp_path=tmp_path)
    effective_diff = _resolve_fix_forward_diff(spec, "")
    verdict = pg.phantom_guard(status="done", worktree_diff=effective_diff, token_usage=None,
                               role="backend-developer")
    assert verdict.is_phantom
