"""test_pr_enforcement_dirty_substantive.py — OI-1119.

pr_enforcement.enforce_pr_exists() used to treat EVERY ``dirty`` worktree_state
identically: not-applicable, ok=True, no receipt. That conflated two very
different situations — a worker's own scratch/editor droppings (harmless) and
real tracked source/test edits the worker simply never committed (the exact
thing the reaper then destroys). This file proves the split:

  - non-substantive dirty (untracked scratch only)   -> unchanged: not-applicable
  - substantive dirty (tracked files never committed) -> loud (ok=False,
    receipt-visible) AND salvaged (committed under an unmistakable
    "[SALVAGED, UNREVIEWED]" marker, pushed, draft PR)

Uses REAL git repos + REAL tmux_worktree.allocate() worktrees (mirrors the
fixture in test_tmux_worktree.py) so the git add/commit/push steps run for
real against a local bare "origin" — only gh_pr_ensure.ensure_pr (real GitHub)
and append_receipt.append_receipt_payload (real NDJSON I/O) are mocked.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
for _p in (_LIB_DIR, _SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pr_enforcement as pe
import tmux_worktree


# ---------------------------------------------------------------------------
# Real-git-repo fixture (mirrors test_tmux_worktree.py's _init_git_repo_with_origin)
# ---------------------------------------------------------------------------

def _init_git_repo_with_origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(bare), str(local)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "checkout", "-b", "main"], capture_output=True)
    subprocess.run(
        ["git", "-C", str(local), "config", "user.email", "test@test.local"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    for name in ("alpha.py", "beta.py", "gamma.py", "delta.py", "epsilon.py"):
        (local / name).write_text(f"# {name}\nprint('{name}')\n")
    subprocess.run(
        ["git", "-C", str(local), "add", "alpha.py", "beta.py", "gamma.py", "delta.py", "epsilon.py"],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(local), "commit", "-m", "initial"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return local


def _allocate(local: Path, dispatch_id: str) -> "tmux_worktree.WorktreeHandle":
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        return tmux_worktree.allocate(dispatch_id, repo_root=local)


def _kwargs(handle, local, **overrides):
    base = dict(
        dispatch_id=handle.dispatch_id,
        branch=handle.branch,
        worktree_state="dirty",
        repo_root=local,
        receipts_file="/tmp/does-not-matter.ndjson",
        pr_title="t",
        pr_body="b",
        wt_path=handle.path,
    )
    base.update(overrides)
    return base


def _branch_commit_messages(local: Path, branch: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(local), "log", branch, "--format=%B"],
        capture_output=True, text=True,
    )
    return out.stdout


# ---------------------------------------------------------------------------
# _classify_dirty_worktree — the substantive/non-substantive split itself
# ---------------------------------------------------------------------------


def test_classify_substantive_tracked_edits(tmp_path):
    """5 tracked files modified, 0 commits — case 2 of OI-1119's evidence table."""
    local = _init_git_repo_with_origin(tmp_path)
    handle = _allocate(local, "cls-sub-1")
    for name in ("alpha.py", "beta.py", "gamma.py", "delta.py", "epsilon.py"):
        (handle.path / name).write_text("# edited\nprint('edited')\n")

    result = pe._classify_dirty_worktree(wt_path=handle.path)

    assert result.substantive is True
    assert len(result.tracked_paths) == 5
    assert set(result.tracked_paths) == {"alpha.py", "beta.py", "gamma.py", "delta.py", "epsilon.py"}
    assert "alpha.py" in result.evidence


def test_classify_non_substantive_untracked_only(tmp_path):
    """Only a never-`git add`ed scratch file — case 1 (unchanged safe path)."""
    local = _init_git_repo_with_origin(tmp_path)
    handle = _allocate(local, "cls-nonsub-1")
    (handle.path / "scratch.tmp").write_text("editor droppings\n")

    result = pe._classify_dirty_worktree(wt_path=handle.path)

    assert result.substantive is False
    assert result.tracked_paths == ()
    assert "untracked" in result.evidence


def test_classify_mixed_ignores_untracked_counts_only_tracked(tmp_path):
    """A tracked edit plus scratch junk: substantive, but only the tracked file
    is reported — scratch never pollutes the evidence or the salvage set."""
    local = _init_git_repo_with_origin(tmp_path)
    handle = _allocate(local, "cls-mixed-1")
    (handle.path / "alpha.py").write_text("# edited\n")
    (handle.path / ".DS_Store").write_text("junk\n")

    result = pe._classify_dirty_worktree(wt_path=handle.path)

    assert result.substantive is True
    assert result.tracked_paths == ("alpha.py",)


def test_classify_degrades_safe_on_git_failure(tmp_path):
    """Not a git repo at all -> git status fails; must degrade to non-substantive,
    never raise."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    result = pe._classify_dirty_worktree(wt_path=not_a_repo)

    assert result.substantive is False
    assert "git status" in result.evidence


# ---------------------------------------------------------------------------
# Case 2 reconstructed end-to-end: 5 tracked files, 0 commits -> loud + salvaged,
# receipt-visible (assert on the receipt payload, not the log).
# ---------------------------------------------------------------------------


def test_case2_five_tracked_files_zero_commits_is_loud_and_receipt_visible(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    handle = _allocate(local, "case2-dirty")
    for name in ("alpha.py", "beta.py", "gamma.py", "delta.py", "epsilon.py"):
        (handle.path / name).write_text("# edited\nprint('edited')\n")

    # Sanity: 0 commits beyond the shared base (worktree_state would be "dirty",
    # not "committed" — classify() confirms independently of enforce_pr_exists).
    assert tmux_worktree.classify(handle) == "dirty"

    captured_receipt = {}
    with patch("gh_pr_ensure.ensure_pr",
               return_value={"pr_number": 909, "created": True, "reason": None}) as ensure_pr_mock, \
         patch("append_receipt.append_receipt_payload",
               side_effect=lambda payload, **kw: captured_receipt.update(payload)):
        result = pe.enforce_pr_exists(**_kwargs(handle, local))

    # -- Loud: ok=False, applicable=True (does NOT silently resolve as done) --
    assert result.applicable is True
    assert result.ok is False
    assert result.pushed is True

    # -- Receipt-visible: assert on the payload, not the log --
    assert captured_receipt, "no corrective receipt was appended"
    assert captured_receipt["status"] == "failed"
    assert captured_receipt["autopr_rejected"] is True
    assert captured_receipt["autopr_kind"] == "dirty_substantive_salvaged"
    assert captured_receipt["dirty_substantive"] is True
    assert captured_receipt["dirty_file_count"] == 5
    assert set(captured_receipt["dirty_files"]) == {
        "alpha.py", "beta.py", "gamma.py", "delta.py", "epsilon.py",
    }
    assert captured_receipt["salvaged"] is True
    assert captured_receipt["salvage_pr_number"] == 909

    # -- Salvaged for real: the branch now has a commit, on origin, marked unvouched --
    remote_log = subprocess.run(
        ["git", "-C", str(local), "log", f"origin/{handle.branch}", "--format=%s"],
        capture_output=True, text=True,
    ).stdout
    assert "[SALVAGED, UNREVIEWED]" in remote_log

    # -- Draft PR, unmistakably marked, never a normal ready-for-review title --
    _, kwargs_used = ensure_pr_mock.call_args
    assert kwargs_used["draft"] is True
    assert kwargs_used["title"].startswith("[SALVAGED-UNVOUCHED]")
    assert "No worker committed" in kwargs_used["body"]


# ---------------------------------------------------------------------------
# Case 3 reconstructed: a truly empty worktree (0 changes, 0 commits) stays
# not-applicable — unaffected by the OI-1119 split.
# ---------------------------------------------------------------------------


def test_case3_empty_worktree_zero_commits_stays_not_applicable(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    handle = _allocate(local, "case3-empty")

    assert tmux_worktree.classify(handle) == "clean"

    with patch("append_receipt.append_receipt_payload") as append_mock, \
         patch("gh_pr_ensure.ensure_pr") as ensure_pr_mock:
        result = pe.enforce_pr_exists(**_kwargs(handle, local, worktree_state="clean"))

    assert result.applicable is False
    assert result.ok is True
    append_mock.assert_not_called()
    ensure_pr_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Non-substantive dirty end-to-end: scratch-only stays not-applicable, no
# commit is created, nothing pushed, no receipt.
# ---------------------------------------------------------------------------


def test_dirty_non_substantive_end_to_end_stays_not_applicable(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    handle = _allocate(local, "case1-scratch")
    (handle.path / "scratch.tmp").write_text("editor droppings\n")

    assert tmux_worktree.classify(handle) == "dirty"  # untracked still counts for the raw verdict

    with patch("append_receipt.append_receipt_payload") as append_mock, \
         patch("gh_pr_ensure.ensure_pr") as ensure_pr_mock, \
         patch.object(pe, "_push_branch") as push_mock:
        result = pe.enforce_pr_exists(**_kwargs(handle, local))

    assert result.applicable is False
    assert result.ok is True
    append_mock.assert_not_called()
    ensure_pr_mock.assert_not_called()
    push_mock.assert_not_called()

    log = subprocess.run(
        ["git", "-C", str(handle.path), "log", "--oneline"],
        capture_output=True, text=True,
    ).stdout
    assert "SALVAGED" not in log


# ---------------------------------------------------------------------------
# Salvage push failure: still loud, marked unsalvaged, no PR attempted.
# ---------------------------------------------------------------------------


def test_dirty_substantive_push_failure_is_loud_and_unsalvaged(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    handle = _allocate(local, "case2-pushfail")
    (handle.path / "alpha.py").write_text("# edited\n")

    captured_receipt = {}
    with patch.object(pe, "_push_branch",
                      return_value=pe._PushOutcome(ok=False, reason="simulated network failure")), \
         patch("gh_pr_ensure.ensure_pr") as ensure_pr_mock, \
         patch("append_receipt.append_receipt_payload",
               side_effect=lambda payload, **kw: captured_receipt.update(payload)):
        result = pe.enforce_pr_exists(**_kwargs(handle, local))

    assert result.applicable is True
    assert result.ok is False
    assert result.pushed is False
    ensure_pr_mock.assert_not_called()  # never attempt a PR for an unpushed salvage commit

    assert captured_receipt["autopr_kind"] == "dirty_substantive_unsalvaged"
    assert captured_receipt["salvaged"] is False
    assert "simulated network failure" in captured_receipt["autopr_reason"]

    # The commit itself still happened locally (data preserved even though
    # the push failed) — verify on the worktree, not the (unreachable) remote.
    local_log = subprocess.run(
        ["git", "-C", str(handle.path), "log", "--format=%s"],
        capture_output=True, text=True,
    ).stdout
    assert "[SALVAGED, UNREVIEWED]" in local_log


# ---------------------------------------------------------------------------
# Back-compat: a caller that doesn't pass wt_path keeps the pre-OI-1119
# behaviour unchanged for ANY dirty tree, substantive or not.
# ---------------------------------------------------------------------------


def test_dirty_without_wt_path_is_unchanged_even_when_substantive(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    handle = _allocate(local, "case2-nowtpath")
    (handle.path / "alpha.py").write_text("# edited\n")

    with patch("append_receipt.append_receipt_payload") as append_mock, \
         patch("gh_pr_ensure.ensure_pr") as ensure_pr_mock:
        result = pe.enforce_pr_exists(**_kwargs(handle, local, wt_path=None))

    assert result.applicable is False
    assert result.ok is True
    append_mock.assert_not_called()
    ensure_pr_mock.assert_not_called()


# ---------------------------------------------------------------------------
# skip_pr honored on the salvage path too: push, but no second PR.
# ---------------------------------------------------------------------------


def test_dirty_substantive_skip_pr_pushes_but_opens_no_pr(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    handle = _allocate(local, "case2-skippr")
    (handle.path / "alpha.py").write_text("# edited\n")

    with patch("gh_pr_ensure.ensure_pr") as ensure_pr_mock, \
         patch("append_receipt.append_receipt_payload"):
        result = pe.enforce_pr_exists(**_kwargs(handle, local, skip_pr=True))

    assert result.applicable is True
    assert result.ok is False  # still loud — skip_pr only skips PR creation, not the failure
    assert result.pushed is True
    assert result.pr_number is None
    ensure_pr_mock.assert_not_called()
