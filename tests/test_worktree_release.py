"""test_worktree_release.py — Tests for governed worktree release path (OI-1052).

Covers: list_locked_worktrees, classify_for_release, rescue_worktree,
release_locked_worktrees, dry-run-safety.

Uses real git repos in tempdir fixtures, mirroring test_tmux_worktree.py.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from worktree_release import (
    ReleaseEntry,
    ReleaseReport,
    _run,
    _resolve_repo_root,
    classify_for_release,
    format_report,
    list_locked_worktrees,
    release_locked_worktrees,
    rescue_worktree,
)


# ---------------------------------------------------------------------------
# Real-git-repo fixtures — mirror test_tmux_worktree.py pattern
# ---------------------------------------------------------------------------

def _init_git_repo_with_origin(tmp_path: Path) -> Path:
    """Create a bare origin + local clone with an initial commit."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(bare), str(local)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "checkout", "-b", "main"],
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.email", "test@test.local"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )

    readme = local / "README.md"
    readme.write_text("init\n")
    subprocess.run(
        ["git", "-C", str(local), "add", "README.md"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "push", "-u", "origin", "main"],
        check=True, capture_output=True,
    )
    return local


def _make_worktree(repo_root: Path, branch: str, wt_path: Path) -> None:
    """Create a worktree on a new branch from origin/main."""
    subprocess.run(
        ["git", "-C", str(repo_root), "fetch", "origin", "main"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [
            "git", "-C", str(repo_root), "worktree", "add",
            "-b", branch, str(wt_path), "origin/main",
        ],
        check=True, capture_output=True,
    )


def _lock_worktree(repo_root: Path, wt_path: str, reason: str = "vnx preserve test") -> None:
    """Lock a worktree via git."""
    subprocess.run(
        [
            "git", "-C", str(repo_root), "worktree", "lock",
            str(wt_path), "--reason", reason,
        ],
        check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# list_locked_worktrees
# ---------------------------------------------------------------------------

def test_list_locked_worktrees_empty(tmp_path):
    """No locked worktrees returns empty list."""
    local = _init_git_repo_with_origin(tmp_path)
    result = list_locked_worktrees(local)
    assert result == []


def test_list_locked_worktrees_with_locked(tmp_path):
    """A locked worktree is detected."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "locked-wt"
    _make_worktree(local, "feature/locked-1", wt)
    _lock_worktree(local, str(wt))

    result = list_locked_worktrees(local)
    assert len(result) == 1
    assert result[0]["locked"] is True
    assert "locked-wt" in result[0]["worktree"]


def test_list_locked_worktrees_skips_unlocked(tmp_path):
    """Only locked worktrees are listed."""
    local = _init_git_repo_with_origin(tmp_path)

    wt_locked = tmp_path / "locked-wt"
    _make_worktree(local, "feature/locked-1", wt_locked)
    _lock_worktree(local, str(wt_locked))

    wt_unlocked = tmp_path / "unlocked-wt"
    _make_worktree(local, "feature/unlocked-1", wt_unlocked)

    result = list_locked_worktrees(local)
    assert len(result) == 1
    assert "locked-wt" in result[0]["worktree"]


# ---------------------------------------------------------------------------
# classify_for_release
# ---------------------------------------------------------------------------

def test_classify_releasable_clean(tmp_path):
    """Clean worktree classifies as releasable."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "clean-wt"
    _make_worktree(local, "feature/clean-1", wt)

    cls, detail = classify_for_release(str(wt), "feature/clean-1")
    assert cls == "releasable"
    assert "clean" in detail


def test_classify_releasable_pushed(tmp_path):
    """Worktree with pushed commits classifies as releasable."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "pushed-wt"
    _make_worktree(local, "feature/pushed-1", wt)

    # Make a commit and push it
    (wt / "work.txt").write_text("pushed work\n")
    subprocess.run(
        ["git", "-C", str(wt), "add", "work.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", "pushed commit"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wt), "push", "-u", "origin", "feature/pushed-1"],
        check=True, capture_output=True,
    )

    cls, detail = classify_for_release(str(wt), "feature/pushed-1")
    assert cls == "releasable"


def test_classify_committable(tmp_path):
    """Uncommitted changes only classifies as committable."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "dirty-wt"
    _make_worktree(local, "feature/dirty-1", wt)

    (wt / "dirty.txt").write_text("uncommitted\n")

    cls, detail = classify_for_release(str(wt), "feature/dirty-1")
    assert cls == "committable"
    assert "uncommitted" in detail


def test_classify_unpushed_commits(tmp_path):
    """Local commits not on origin classifies as unpushed_commits."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "committed-wt"
    _make_worktree(local, "feature/committed-1", wt)

    (wt / "work.txt").write_text("local work\n")
    subprocess.run(
        ["git", "-C", str(wt), "add", "work.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", "local only"],
        check=True, capture_output=True,
    )

    cls, detail = classify_for_release(str(wt), "feature/committed-1")
    assert cls == "unpushed_commits"
    assert "local commit" in detail


def test_classify_both(tmp_path):
    """Both uncommitted changes and local commits classifies as both."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "both-wt"
    _make_worktree(local, "feature/both-1", wt)

    # Make a local commit
    (wt / "committed.txt").write_text("committed\n")
    subprocess.run(
        ["git", "-C", str(wt), "add", "committed.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", "local commit"],
        check=True, capture_output=True,
    )

    # Then make an uncommitted change
    (wt / "dirty.txt").write_text("uncommitted too\n")

    cls, detail = classify_for_release(str(wt), "feature/both-1")
    assert cls == "both"
    assert "uncommitted" in detail
    assert "local commit" in detail


def test_classify_unreachable(tmp_path):
    """Non-existent path classifies as unreachable."""
    cls, detail = classify_for_release("/tmp/does-not-exist-xyz", "ghost-branch")
    assert cls == "unreachable"


# ---------------------------------------------------------------------------
# rescue_worktree
# ---------------------------------------------------------------------------

def test_rescue_dry_run_committable(tmp_path):
    """Dry-run rescue for committable worktree doesn't actually commit/push."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "rescue-dry-wt"
    _make_worktree(local, "feature/rescue-dry-1", wt)

    (wt / "new.txt").write_text("should not be committed\n")

    success, branch, commit = rescue_worktree(
        str(wt), "feature/rescue-dry-1", "committable", dry_run=True,
    )
    assert success is True
    assert branch.startswith("vnx-release/")
    assert commit == "[would create]"

    # Verify nothing was actually committed
    status = subprocess.check_output(
        ["git", "-C", str(wt), "status", "--porcelain"],
        text=True,
    )
    assert "new.txt" in status  # Still uncommitted


def test_rescue_dry_run_unpushed(tmp_path):
    """Dry-run rescue for unpushed_commits reports what would be pushed."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "rescue-unpushed-wt"
    _make_worktree(local, "feature/rescue-unpushed-1", wt)

    (wt / "work.txt").write_text("local work\n")
    subprocess.run(
        ["git", "-C", str(wt), "add", "work.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", "local"],
        check=True, capture_output=True,
    )

    success, branch, commit = rescue_worktree(
        str(wt), "feature/rescue-unpushed-1", "unpushed_commits", dry_run=True,
    )
    assert success is True
    assert branch == "feature/rescue-unpushed-1"
    assert commit == "[would push]"


def test_rescue_apply_committable(tmp_path):
    """Apply rescue for committable worktree commits and pushes to rescue branch."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "rescue-apply-wt"
    _make_worktree(local, "feature/rescue-apply-1", wt)

    (wt / "salvage.txt").write_text("rescue me\n")

    success, branch, commit = rescue_worktree(
        str(wt), "feature/rescue-apply-1", "committable", dry_run=False,
    )
    assert success is True
    assert branch.startswith("vnx-release/")
    assert len(commit) == 8  # Short SHA

    # Verify the branch exists on origin
    remote_refs = subprocess.check_output(
        ["git", "-C", str(local), "ls-remote", "origin", branch],
        text=True,
    ).strip()
    assert branch in remote_refs


def test_rescue_apply_unpushed(tmp_path):
    """Apply rescue for unpushed_commits pushes the branch to origin."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "rescue-push-wt"
    _make_worktree(local, "feature/rescue-push-1", wt)

    (wt / "work.txt").write_text("push me\n")
    subprocess.run(
        ["git", "-C", str(wt), "add", "work.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", "to push"],
        check=True, capture_output=True,
    )

    success, branch, commit = rescue_worktree(
        str(wt), "feature/rescue-push-1", "unpushed_commits", dry_run=False,
    )
    assert success is True
    assert branch == "feature/rescue-push-1"
    assert len(commit) == 8

    # Verify on origin
    remote_refs = subprocess.check_output(
        ["git", "-C", str(local), "ls-remote", "origin", "feature/rescue-push-1"],
        text=True,
    ).strip()
    assert "feature/rescue-push-1" in remote_refs


def test_rescue_apply_both(tmp_path):
    """Apply rescue for 'both': commits uncommitted changes, then pushes."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "rescue-both-wt"
    _make_worktree(local, "feature/rescue-both-1", wt)

    # First make a committed change
    (wt / "committed.txt").write_text("already committed\n")
    subprocess.run(
        ["git", "-C", str(wt), "add", "committed.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", "first commit"],
        check=True, capture_output=True,
    )

    # Then make an uncommitted change
    (wt / "uncommitted.txt").write_text("not yet committed\n")

    success, branch, commit = rescue_worktree(
        str(wt), "feature/rescue-both-1", "both", dry_run=False,
    )
    assert success is True
    assert branch == "feature/rescue-both-1"
    assert len(commit) == 8

    # Verify on origin
    remote_refs = subprocess.check_output(
        ["git", "-C", str(local), "ls-remote", "origin", "feature/rescue-both-1"],
        text=True,
    ).strip()
    assert "feature/rescue-both-1" in remote_refs


def test_rescue_releasable_noop(tmp_path):
    """Rescue for releasable worktree is a no-op."""
    local = _init_git_repo_with_origin(tmp_path)
    wt = tmp_path / "noop-wt"
    _make_worktree(local, "feature/noop-1", wt)

    success, branch, commit = rescue_worktree(
        str(wt), "feature/noop-1", "releasable", dry_run=False,
    )
    assert success is True
    assert branch == ""
    assert commit == ""


# ---------------------------------------------------------------------------
# release_locked_worktrees (integration)
# ---------------------------------------------------------------------------

def test_release_dry_run_no_changes(tmp_path):
    """Dry-run release_locked_worktrees makes no changes to locked worktrees."""
    local = _init_git_repo_with_origin(tmp_path)

    # Create a locked worktree with uncommitted changes
    wt = tmp_path / "release-dry-wt"
    _make_worktree(local, "feature/release-dry-1", wt)
    (wt / "data.txt").write_text("precious data\n")
    _lock_worktree(local, str(wt))

    report = release_locked_worktrees(repo_root=local, dry_run=True)

    assert report.dry_run is True
    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.classification == "committable"
    assert entry.rescued is True  # Dry-run rescue "succeeds"
    assert entry.removed is False  # Not actually removed

    # Verify the worktree still exists and is still locked
    result = list_locked_worktrees(local)
    assert len(result) == 1
    assert wt.is_dir()

    # Verify data is still there
    assert (wt / "data.txt").read_text() == "precious data\n"


def test_release_apply_clean(tmp_path):
    """Apply release removes a locked clean worktree."""
    local = _init_git_repo_with_origin(tmp_path)

    wt = tmp_path / "release-clean-wt"
    _make_worktree(local, "feature/release-clean-1", wt)
    _lock_worktree(local, str(wt))

    report = release_locked_worktrees(repo_root=local, dry_run=False)

    assert report.dry_run is False
    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.classification == "releasable"
    assert entry.removed is True

    # Verify worktree is gone
    assert not wt.exists()

    # Verify no more locked worktrees
    remaining = list_locked_worktrees(local)
    assert len(remaining) == 0


def test_release_apply_committable_rescues_then_removes(tmp_path):
    """Apply release rescues committable work, then removes the worktree."""
    local = _init_git_repo_with_origin(tmp_path)

    wt = tmp_path / "release-committable-wt"
    _make_worktree(local, "feature/release-comm-1", wt)
    (wt / "vital.txt").write_text("vital data\n")
    _lock_worktree(local, str(wt))

    report = release_locked_worktrees(repo_root=local, dry_run=False)

    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.classification == "committable"
    assert entry.rescued is True
    assert entry.removed is True
    assert entry.rescue_branch.startswith("vnx-release/")

    # Verify worktree removed
    assert not wt.exists()

    # Verify rescue branch exists on origin
    remote_refs = subprocess.check_output(
        ["git", "-C", str(local), "ls-remote", "origin", entry.rescue_branch],
        text=True,
    ).strip()
    assert entry.rescue_branch in remote_refs


def test_release_apply_unpushed_rescues_then_removes(tmp_path):
    """Apply release pushes unpushed commits, then removes the worktree."""
    local = _init_git_repo_with_origin(tmp_path)

    wt = tmp_path / "release-unpushed-wt"
    _make_worktree(local, "feature/release-unp-1", wt)
    (wt / "work.txt").write_text("pushed work\n")
    subprocess.run(
        ["git", "-C", str(wt), "add", "work.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", "release me"],
        check=True, capture_output=True,
    )
    _lock_worktree(local, str(wt))

    report = release_locked_worktrees(repo_root=local, dry_run=False)

    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.classification == "unpushed_commits"
    assert entry.rescued is True
    assert entry.removed is True

    # Verify worktree removed
    assert not wt.exists()

    # Verify branch exists on origin
    remote_refs = subprocess.check_output(
        ["git", "-C", str(local), "ls-remote", "origin", "feature/release-unp-1"],
        text=True,
    ).strip()
    assert "feature/release-unp-1" in remote_refs


def test_release_unreachable_left_alone(tmp_path):
    """An unreachable worktree is reported but not removed."""
    local = _init_git_repo_with_origin(tmp_path)

    # Create a worktree, lock it, then delete the directory
    wt = tmp_path / "ghost-wt"
    _make_worktree(local, "feature/ghost-1", wt)
    _lock_worktree(local, str(wt))

    # Remove the directory but leave the lock metadata
    import shutil
    shutil.rmtree(str(wt))

    report = release_locked_worktrees(repo_root=local, dry_run=False)

    # The ghost worktree should still appear
    ghost_entries = [e for e in report.entries if "ghost-wt" in e.worktree]
    assert len(ghost_entries) == 1
    assert ghost_entries[0].classification == "unreachable"
    assert ghost_entries[0].removed is False


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------

def test_format_report_dry_run():
    """format_report includes DRY-RUN marker when dry_run is True."""
    report = ReleaseReport(
        entries=[
            ReleaseEntry(
                worktree="/tmp/wt1",
                branch="feature/test-1",
                locked=True,
                classification="releasable",
                detail="clean",
            ),
        ],
        dry_run=True,
        timestamp="2026-01-01T00:00:00Z",
    )
    output = format_report(report)
    assert "[DRY-RUN]" in output
    assert "DRY-RUN: nothing was changed" in output


def test_format_report_apply():
    """format_report includes APPLY marker when dry_run is False."""
    report = ReleaseReport(
        entries=[
            ReleaseEntry(
                worktree="/tmp/wt1",
                branch="feature/test-1",
                locked=True,
                classification="releasable",
                detail="clean",
                removed=True,
            ),
        ],
        dry_run=False,
        timestamp="2026-01-01T00:00:00Z",
    )
    output = format_report(report)
    assert "[APPLY]" in output
    assert "DRY-RUN" not in output


# ---------------------------------------------------------------------------
# ReleaseReport count
# ---------------------------------------------------------------------------

def test_release_report_counts():
    """ReleaseReport.counts aggregates classifications correctly."""
    report = ReleaseReport(entries=[
        ReleaseEntry(worktree="w1", branch="b1", locked=True, classification="releasable", detail=""),
        ReleaseEntry(worktree="w2", branch="b2", locked=True, classification="releasable", detail=""),
        ReleaseEntry(worktree="w3", branch="b3", locked=True, classification="committable", detail=""),
        ReleaseEntry(worktree="w4", branch="b4", locked=True, classification="unpushed_commits", detail=""),
    ])
    counts = report.counts
    assert counts["releasable"] == 2
    assert counts["committable"] == 1
    assert counts["unpushed_commits"] == 1
