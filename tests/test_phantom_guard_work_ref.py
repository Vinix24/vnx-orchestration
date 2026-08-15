"""test_phantom_guard_work_ref.py — OI-1137: the pushed-branch fallback for fix-forward dispatches.

A fix-forward dispatch declares a work target (``work_ref`` / ``pr_id`` / ``parent_dispatch``)
instead of landing on its own ``dispatch/<id>`` branch. Its own worktree/branch diff reads empty
while real work sits on the PUSHED branch. ``resolve_pushed_work_diff`` is the resolver that
weighs that pushed branch in, and ``guard_at_govern`` calls it when the own diff is empty (or the
own ref is unresolvable).

The property that must NEVER regress is monotonicity: the fallback only ever upgrades an empty
diff to non-empty when real pushed work exists. It never downgrades, never manufactures evidence
for a plain dispatch, and never raises. Those invariants are tested explicitly here.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import phantom_guard as pg


@dataclass
class _FakeProc:
    returncode: int
    stdout: str = ""


# ---------------------------------------------------------------------------
# _normalize_branch_name — prefix stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("dispatch/20260815-foo", "dispatch/20260815-foo"),
        ("refs/heads/dispatch/foo", "dispatch/foo"),
        ("refs/remotes/origin/dispatch/foo", "dispatch/foo"),
        ("origin/dispatch/foo", "dispatch/foo"),
        ("  dispatch/padded  ", "dispatch/padded"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_branch_name_strips_prefixes(raw, expected):
    assert pg._normalize_branch_name(raw) == expected


# ---------------------------------------------------------------------------
# resolve_pushed_work_diff — precedence, monotonicity, failure-is-empty
# ---------------------------------------------------------------------------


def test_resolve_no_target_declared_short_circuits_no_git(monkeypatch):
    # A plain dispatch (no work_ref/pr_id/parent_dispatch) must return '' WITHOUT touching git
    # or gh — the monotonic short-circuit that keeps the fallback from ever weakening the guard.
    def _boom(*a, **k):
        raise AssertionError("git/gh must not be touched when no work target is declared")

    monkeypatch.setattr(pg.subprocess, "run", _boom)
    assert pg.resolve_pushed_work_diff() == ""
    assert pg.resolve_pushed_work_diff(work_ref="", pr_id="", parent_dispatch="") == ""


def test_resolve_work_ref_takes_precedence_over_pr_id_and_parent(monkeypatch):
    # work_ref is the sharpest signal — it must win over pr_id/parent_dispatch, and pr_id (which
    # would shell out to gh) must NOT be consulted when work_ref is present.
    seen_fetch = []

    def _fake_run(cmd, *a, **k):
        if cmd[:2] == ["git", "fetch"]:
            seen_fetch.append(cmd[2:])
        return _FakeProc(0, "")

    monkeypatch.setattr(pg.subprocess, "run", _fake_run)
    monkeypatch.setattr(pg, "compute_branch_diff", lambda *a, **k: "diff --git a/x b/x\n+via-work-ref\n")
    monkeypatch.setattr(pg, "resolve_pr_head_branch",
                        lambda *a, **k: pytest.fail("pr_id must not be resolved when work_ref is set"))

    result = pg.resolve_pushed_work_diff(
        work_ref="origin/dispatch/explicit", pr_id="1161", parent_dispatch="parent-001",
        repo=Path("/nonexistent"),
    )
    assert result == "diff --git a/x b/x\n+via-work-ref\n"
    # fetch targeted the NORMALIZED branch, not the raw prefixed name
    assert seen_fetch and seen_fetch[0] == ["origin", "dispatch/explicit"]


def test_resolve_parent_dispatch_derives_branch_when_no_work_ref_or_pr(monkeypatch):
    seen_fetch = []

    def _fake_run(cmd, *a, **k):
        if cmd[:2] == ["git", "fetch"]:
            seen_fetch.append(cmd[2:])
        return _FakeProc(0, "")

    monkeypatch.setattr(pg.subprocess, "run", _fake_run)
    monkeypatch.setattr(pg, "compute_branch_diff", lambda *a, **k: "diff --git a/y b/y\n+via-parent\n")

    result = pg.resolve_pushed_work_diff(parent_dispatch="20260815/foo bar", repo=Path("/nonexistent"))
    assert result == "diff --git a/y b/y\n+via-parent\n"
    # parent_dispatch is sanitized into the branch name (unsafe chars become '-')
    assert seen_fetch and seen_fetch[0] == ["origin", "dispatch/20260815-foo-bar"]


def test_resolve_pr_id_falls_through_when_no_work_ref(monkeypatch):
    monkeypatch.setattr(pg.subprocess, "run", lambda *a, **k: _FakeProc(0, ""))
    monkeypatch.setattr(pg, "resolve_pr_head_branch", lambda pr_id, **k: "dispatch/pr-branch")
    monkeypatch.setattr(pg, "compute_branch_diff", lambda *a, **k: "diff --git a/z b/z\n+via-pr\n")

    result = pg.resolve_pushed_work_diff(pr_id="1161", repo=Path("/nonexistent"))
    assert result == "diff --git a/z b/z\n+via-pr\n"


def test_resolve_fetch_or_diff_failure_returns_empty_never_raises(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.CalledProcessError(128, "git", stderr="fatal: bad ref")

    monkeypatch.setattr(pg.subprocess, "run", _raise)
    # compute_branch_diff raises CalledProcessError -> caught -> '' (no false-reject, no raise)
    assert pg.resolve_pushed_work_diff(work_ref="dispatch/gone", repo=Path("/nonexistent")) == ""


def test_resolve_branch_not_pushed_fetch_nonzero_still_empty(monkeypatch):
    # fetch returns nonzero (branch not pushed) — compute_branch_diff then raises on the missing
    # ref; either way the result is '', never a fabricated diff.
    def _fetch_nonzero(cmd, *a, **k):
        if cmd[:2] == ["git", "fetch"]:
            return _FakeProc(1, "")
        raise subprocess.CalledProcessError(128, "git", stderr="fatal: bad ref")

    monkeypatch.setattr(pg.subprocess, "run", _fetch_nonzero)
    assert pg.resolve_pushed_work_diff(work_ref="dispatch/never-pushed", repo=Path("/nonexistent")) == ""


# ---------------------------------------------------------------------------
# guard_at_govern — the fallback only upgrades empty->non-empty, never weakens
# ---------------------------------------------------------------------------


def test_guard_empty_own_diff_upgrades_via_work_ref_not_phantom(monkeypatch):
    # A fix-forward dispatch: own diff empty, pushed branch carries real work -> PASSES.
    monkeypatch.setattr(pg.subprocess, "run", lambda *a, **k: _FakeProc(0, ""))
    monkeypatch.setattr(pg, "compute_branch_diff",
                        lambda *a, **k: "diff --git a/fix.txt b/fix.txt\n+real fix\n")

    v = pg.guard_at_govern(dispatch_id="fixfwd-d1", role="backend-developer", status="done",
                           token_usage=0, worktree_diff="", work_ref="dispatch/real-branch",
                           repo=Path("/nonexistent"))
    assert not v.is_phantom


def test_guard_empty_own_diff_no_pushed_work_still_phantom(monkeypatch):
    # Same shape but the pushed branch carries NOTHING -> still phantom. This is the guard-preservation
    # invariant: an empty resolve must not upgrade the diff.
    monkeypatch.setattr(pg.subprocess, "run", lambda *a, **k: _FakeProc(0, ""))
    monkeypatch.setattr(pg, "compute_branch_diff", lambda *a, **k: "")

    v = pg.guard_at_govern(dispatch_id="fixfwd-d2", role="backend-developer", status="done",
                           token_usage=0, worktree_diff="", work_ref="dispatch/empty-branch",
                           repo=Path("/nonexistent"))
    assert v.is_phantom


def test_guard_non_empty_own_diff_never_triggers_resolution(monkeypatch):
    # A normal dispatch with real own-worktree work must NOT call resolve_pushed_work_diff at all.
    def _boom(*a, **k):
        raise AssertionError("resolve_pushed_work_diff must not be called when own diff is non-empty")

    monkeypatch.setattr(pg, "resolve_pushed_work_diff", _boom)
    v = pg.guard_at_govern(dispatch_id="normal-d1", role="backend-developer", status="done",
                           token_usage=10, worktree_diff="diff --git a/x b/x\n+own work\n",
                           work_ref="dispatch/some-branch", repo=Path("/nonexistent"))
    assert not v.is_phantom


def test_guard_unresolvable_own_ref_falls_back_to_pushed_work(monkeypatch):
    # Own branch/worktree is gone (the fix-forward shape at govern time) -> the pushed branch is
    # weighed in before abstaining. Real pushed work -> PASSES.
    def _fake_diff(head_ref, *a, **k):
        if head_ref.startswith("dispatch/"):
            raise subprocess.CalledProcessError(128, "git", stderr="fatal: own branch gone")
        return "diff --git a/fix.txt b/fix.txt\n+pushed work\n"

    monkeypatch.setattr(pg.subprocess, "run", lambda *a, **k: _FakeProc(0, ""))
    monkeypatch.setattr(pg, "compute_branch_diff", _fake_diff)

    v = pg.guard_at_govern(dispatch_id="no-such-dispatch-xyz", role="backend-developer",
                           status="done", token_usage=0, work_ref="dispatch/pushed-branch",
                           repo=Path("/nonexistent"))
    assert not v.is_phantom


def test_guard_unresolvable_own_ref_no_pushed_work_still_abstains(monkeypatch):
    # Own branch gone AND the pushed branch carries nothing -> ABSTAIN (never false-reject as
    # "empty diff" on a torn-down ref).
    def _fake_diff(head_ref, *a, **k):
        if head_ref.startswith("dispatch/"):
            raise subprocess.CalledProcessError(128, "git", stderr="fatal: own branch gone")
        return ""

    monkeypatch.setattr(pg.subprocess, "run", lambda *a, **k: _FakeProc(0, ""))
    monkeypatch.setattr(pg, "compute_branch_diff", _fake_diff)

    v = pg.guard_at_govern(dispatch_id="no-such-dispatch-xyz", role="backend-developer",
                           status="done", token_usage=0, work_ref="dispatch/missing-branch",
                           repo=Path("/nonexistent"))
    assert not v.is_phantom
    assert "ABSTAIN" in v.reason
