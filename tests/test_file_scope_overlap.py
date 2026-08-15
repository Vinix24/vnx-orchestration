"""test_file_scope_overlap.py — OI-1091: overlap detection between open dispatch branches.

Covers the pure branch-name handling (dispatch_id_from_branch) and the git plumbing
(changed_files / open_dispatch_branches / find_overlaps / warn_overlaps) against a REAL local
git repo (bare origin + working checkout), so the fetch + merge-base + diff plumbing is proven,
not just the branching logic. The contract under test: the warning names the OTHER dispatch and
lists the shared files, and it never blocks (best-effort, no raises).
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

import file_scope_overlap as fso


# ---------------------------------------------------------------------------
# dispatch_id_from_branch — pure branch-name handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "branch,expected",
    [
        ("dispatch/20260815-foo", "20260815-foo"),
        ("origin/dispatch/20260815-foo", "20260815-foo"),
        ("refs/heads/dispatch/20260815-foo", "20260815-foo"),
        ("refs/remotes/origin/dispatch/20260815-foo", "20260815-foo"),
        ("main", None),
        ("dispatch/", None),
        ("", None),
        (None, None),
    ],
)
def test_dispatch_id_from_branch(branch, expected):
    assert fso.dispatch_id_from_branch(branch) == expected


# ---------------------------------------------------------------------------
# Real git repo fixture — proves fetch + merge-base + diff plumbing
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


@pytest.fixture()
def git_fixture(tmp_path: Path):
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


def _push_branch(work: Path, branch: str, filename: str) -> None:
    """Create a branch off main carrying one new file and push it, then drop the local branch."""
    _run_git(work, "checkout", "-b", branch, "main")
    (work / filename).write_text(f"change for {filename}\n", encoding="utf-8")
    _run_git(work, "add", filename)
    _run_git(work, "commit", "-m", f"add {filename}")
    _run_git(work, "push", "origin", branch)
    _run_git(work, "checkout", "main")
    _run_git(work, "branch", "-D", branch)


def _push_empty_branch(work: Path, branch: str) -> None:
    """Push a branch that equals main (nothing new) — trivially already merged."""
    _run_git(work, "checkout", "-b", branch, "main")
    _run_git(work, "push", "origin", branch)
    _run_git(work, "checkout", "main")
    _run_git(work, "branch", "-D", branch)


# ---------------------------------------------------------------------------
# changed_files
# ---------------------------------------------------------------------------


def test_changed_files_returns_pushed_files(git_fixture):
    _push_branch(git_fixture, "dispatch/a", "a.txt")
    assert fso.changed_files("dispatch/a", base_ref="origin/main", repo=git_fixture) == {"a.txt"}


def test_changed_files_empty_branch_returns_empty(git_fixture):
    _push_empty_branch(git_fixture, "dispatch/empty")
    assert fso.changed_files("dispatch/empty", base_ref="origin/main", repo=git_fixture) == set()


def test_changed_files_missing_branch_returns_empty(git_fixture):
    assert fso.changed_files("dispatch/never-pushed", base_ref="origin/main", repo=git_fixture) == set()


# ---------------------------------------------------------------------------
# open_dispatch_branches — merged vs unmerged
# ---------------------------------------------------------------------------


def test_open_dispatch_branches_excludes_merged(git_fixture):
    _push_branch(git_fixture, "dispatch/open-a", "open-a.txt")
    _push_empty_branch(git_fixture, "dispatch/already-merged")
    open_branches = fso.open_dispatch_branches(base_ref="origin/main", repo=git_fixture)
    assert "dispatch/open-a" in open_branches
    assert "dispatch/already-merged" not in open_branches


def test_open_dispatch_branches_empty_without_branches(git_fixture):
    assert fso.open_dispatch_branches(base_ref="origin/main", repo=git_fixture) == []


# ---------------------------------------------------------------------------
# find_overlaps / warn_overlaps — the OI-1091 contract
# ---------------------------------------------------------------------------


def test_find_overlaps_returns_colliding_dispatch_and_files(git_fixture):
    _push_branch(git_fixture, "dispatch/other", "shared.txt")
    _push_branch(git_fixture, "dispatch/merging", "shared.txt")
    overlaps = fso.find_overlaps("dispatch/merging", base_ref="origin/main", repo=git_fixture)
    assert overlaps == [("other", ["shared.txt"])]


def test_find_overlaps_ignores_disjoint_branch(git_fixture):
    _push_branch(git_fixture, "dispatch/other", "shared.txt")
    _push_branch(git_fixture, "dispatch/disjoint", "disjoint.txt")
    _push_branch(git_fixture, "dispatch/merging", "shared.txt")
    overlaps = fso.find_overlaps("dispatch/merging", base_ref="origin/main", repo=git_fixture)
    # only the colliding branch appears; the disjoint one is silent
    assert overlaps == [("other", ["shared.txt"])]


def test_find_overlaps_no_open_branches_returns_empty(git_fixture):
    _push_branch(git_fixture, "dispatch/merging", "shared.txt")
    assert fso.find_overlaps("dispatch/merging", base_ref="origin/main", repo=git_fixture) == []


def test_warn_overlaps_names_dispatch_and_files(git_fixture):
    _push_branch(git_fixture, "dispatch/other", "shared.txt")
    _push_branch(git_fixture, "dispatch/merging", "shared.txt")
    capture = io.StringIO()
    overlaps = fso.warn_overlaps(
        "dispatch/merging", base_ref="origin/main", repo=git_fixture, stream=capture,
    )
    assert overlaps == [("other", ["shared.txt"])]
    emitted = capture.getvalue()
    assert "dispatch 'other'" in emitted
    assert "shared.txt" in emitted


def test_warn_overlaps_no_collision_emits_nothing(git_fixture):
    _push_branch(git_fixture, "dispatch/merging", "only.txt")
    capture = io.StringIO()
    overlaps = fso.warn_overlaps(
        "dispatch/merging", base_ref="origin/main", repo=git_fixture, stream=capture,
    )
    assert overlaps == []
    assert capture.getvalue() == ""


# ---------------------------------------------------------------------------
# pr_merge wiring — merge_pr surfaces the overlap in its result
# ---------------------------------------------------------------------------


def test_merge_pr_surfaces_overlap_warning(monkeypatch):
    import pr_merge

    monkeypatch.setattr(pr_merge, "_query_pr",
                        lambda pr: {"title": "fix: t", "headRefName": "dispatch/merging"})
    monkeypatch.setattr(pr_merge, "_do_merge", lambda pr, method: (True, ""))
    monkeypatch.setattr(pr_merge, "_emit_receipt", lambda **kw: {"append_status": "ok"})
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **kw: True)
    monkeypatch.setattr(
        fso, "warn_overlaps", lambda branch, **kw: [("other", ["shared.txt"])],
    )

    result = pr_merge.merge_pr(99, dispatch_id="d1")
    assert result["success"] is True
    assert result["overlaps"] == [("other", ["shared.txt"])]


def test_merge_pr_overlap_check_failure_does_not_block(monkeypatch):
    import pr_merge

    monkeypatch.setattr(pr_merge, "_query_pr",
                        lambda pr: {"title": "fix: t", "headRefName": "dispatch/merging"})
    monkeypatch.setattr(pr_merge, "_do_merge", lambda pr, method: (True, ""))
    monkeypatch.setattr(pr_merge, "_emit_receipt", lambda **kw: {"append_status": "ok"})
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **kw: True)

    def _boom(branch, **kw):
        raise RuntimeError("git unavailable")

    monkeypatch.setattr(fso, "warn_overlaps", _boom)
    # must still merge and report success — the overlap check is best-effort
    result = pr_merge.merge_pr(99, dispatch_id="d1")
    assert result["success"] is True
    assert result["overlaps"] == []
