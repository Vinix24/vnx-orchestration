"""Verdict logic of scripts/cleanup_reviewed_worktrees.py (OI-1629 B, W3-W6).

Every test builds a real tmp git repo with a bare origin and a real locked worktree and runs the
real functions; only ``run_gh_pr_list`` (the network call) is stubbed.
"""
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

import cleanup_reviewed_worktrees as crw  # noqa: E402
import dispatch_worktree_isolation as dwi  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV}
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class Fixture:
    def __init__(self, tmp_path: Path, project_id: str = "alpha") -> None:
        self.tmp = tmp_path
        # The project's central store exists before the first claim is written, so
        # the claim/occupancy registry (resolved through vnx_paths) and the sweep
        # resolve to ONE place for the whole test instead of following whichever
        # directory happens to exist at the moment of each call.
        (Path.home() / ".vnx-data" / project_id / "state").mkdir(parents=True, exist_ok=True)
        self.origin = tmp_path / "origin.git"
        self.repo = tmp_path / "repo"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(tmp_path, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        (self.repo / ".vnx-project-id").write_text(project_id + "\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text(".vnx-data/\n", encoding="utf-8")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")

    def worktree(self, dispatch_id: str, *, commit: bool = True, push: bool = False) -> crw.Candidate:
        path = self.repo / ".vnx-data" / "worktrees" / f"dispatch-{dispatch_id}"
        branch = f"dispatch/{dispatch_id}"
        git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
        if commit:
            (path / f"{dispatch_id}.txt").write_text("work\n", encoding="utf-8")
            git(path, "add", "-A")
            git(path, "commit", "-q", "-m", "work")
        if push:
            git(path, "push", "-q", "origin", branch)
        # What create_dispatch_worktree leaves behind: a claim naming this path and an
        # existing (unheld) occupancy slot. Without them the guard refuses the removal.
        dwi._write_claim_atomic(dispatch_id, worktree_path=path, project_root=self.repo)
        dwi._acquire_occupancy(dispatch_id, path, self.repo)
        dwi._release_occupancy(path)
        git(self.repo, "worktree", "lock", "--reason", "vnx preserve 2026", str(path))
        found = [w for w in crw.discover_candidates(self.repo) if w.dispatch_dir_id == dispatch_id]
        assert len(found) == 1
        return found[0]

    def branch_exists(self, branch: str) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=self.repo, capture_output=True
        )
        return proc.returncode == 0


@pytest.fixture
def fx(tmp_path, monkeypatch) -> Fixture:
    monkeypatch.delenv("VNX_DISPATCH_ID", raising=False)
    monkeypatch.delenv("VNX_CURRENT_DISPATCH_ID", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    # conftest pins the data root; the sweeper resolves the store from the project, so the
    # tests lay the decision log out under a patched home the way a central install has it
    for key in ("VNX_DATA_DIR_EXPLICIT", "VNX_DATA_DIR", "VNX_DATA_HOME", "VNX_STATE_DIR"):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return Fixture(tmp_path)


def stub_prs(monkeypatch, entries) -> None:
    monkeypatch.setattr(crw, "run_gh_pr_list", lambda slug, branch: json.dumps(entries))


def write_decision(home: Path, project_id: str, dispatch_id: str, decision: str, log_project: str) -> None:
    state = home / ".vnx-data" / log_project / "state"
    state.mkdir(parents=True, exist_ok=True)
    record = {
        "decision_type": "outcome_decision", "project_id": project_id,
        "dispatch_id": dispatch_id, "decision": decision,
    }
    with open(state / "t0_decision_log.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


class TestClosedPr:
    """W3."""

    def test_closed_pr_at_head_with_remote_proof_releases_and_deletes_the_branch(self, fx, monkeypatch):
        wt = fx.worktree("x", push=True)
        stub_prs(monkeypatch, [{"number": 7, "state": "CLOSED", "headRefOid": wt.head_sha}])
        verdict = crw.evaluate_worktree(fx.repo, wt, "o/r")
        assert verdict.safe and verdict.delete_branch
        assert "CLOSED" in verdict.reason and "refs/heads/dispatch/x" in verdict.reason

    def test_closed_pr_without_remote_proof_releases_but_keeps_the_branch(self, fx, monkeypatch):
        wt = fx.worktree("x", push=False)
        stub_prs(monkeypatch, [{"number": 7, "state": "CLOSED", "headRefOid": wt.head_sha}])
        verdict = crw.evaluate_worktree(fx.repo, wt, "o/r")
        assert verdict.safe and not verdict.delete_branch
        assert "lokale branch blijft" in verdict.reason

    def test_closed_pr_with_failing_ls_remote_keeps_the_branch_and_says_why(self, fx, monkeypatch):
        wt = fx.worktree("x", push=True)
        stub_prs(monkeypatch, [{"number": 7, "state": "CLOSED", "headRefOid": wt.head_sha}])
        git(fx.repo, "remote", "set-url", "origin", str(fx.tmp / "gone.git"))
        verdict = crw.evaluate_worktree(fx.repo, wt, "o/r")
        assert verdict.safe and not verdict.delete_branch
        assert "ls-remote" in verdict.reason

    def test_closed_pr_whose_head_is_not_head_stays(self, fx, monkeypatch):
        wt = fx.worktree("x", push=True)
        stub_prs(monkeypatch, [{"number": 7, "state": "CLOSED", "headRefOid": git(fx.repo, "rev-parse", "main")}])
        assert not crw.evaluate_worktree(fx.repo, wt, "o/r").safe

    def test_closed_pr_with_uncommitted_work_stays(self, fx, monkeypatch):
        wt = fx.worktree("x", push=True)
        (wt.path / "a.txt").write_text("changed\n", encoding="utf-8")
        stub_prs(monkeypatch, [{"number": 7, "state": "CLOSED", "headRefOid": wt.head_sha}])
        verdict = crw.evaluate_worktree(fx.repo, wt, "o/r")
        assert not verdict.safe and "niet schoon" in verdict.reason

    def test_merged_pr_still_releases_with_branch_delete(self, fx, monkeypatch):
        wt = fx.worktree("x")
        stub_prs(monkeypatch, [{"number": 7, "state": "MERGED", "headRefOid": wt.head_sha}])
        verdict = crw.evaluate_worktree(fx.repo, wt, "o/r")
        assert verdict.safe and verdict.delete_branch and "MERGED" in verdict.reason

    def test_open_pr_does_not_release(self, fx, monkeypatch):
        wt = fx.worktree("x", push=True)
        stub_prs(monkeypatch, [{"number": 7, "state": "OPEN", "headRefOid": wt.head_sha}])
        assert not crw.evaluate_worktree(fx.repo, wt, "o/r").safe


class TestSymlinkIsNotWork:
    """W4."""

    def _merged(self, fx, monkeypatch):
        wt = fx.worktree("x")
        stub_prs(monkeypatch, [{"number": 7, "state": "MERGED", "headRefOid": wt.head_sha}])
        return wt

    def test_untracked_symlink_into_the_repo_releases(self, fx, monkeypatch):
        wt = self._merged(fx, monkeypatch)
        (fx.repo / ".venv").mkdir()
        os.symlink(fx.repo / ".venv", wt.path / ".venv")
        assert crw.evaluate_worktree(fx.repo, wt, "o/r").safe

    def test_symlink_to_a_missing_target_inside_the_repo_releases(self, fx, monkeypatch):
        wt = self._merged(fx, monkeypatch)
        os.symlink(fx.repo / "node_modules", wt.path / "node_modules")
        assert crw.evaluate_worktree(fx.repo, wt, "o/r").safe

    def test_symlink_pointing_outside_the_repo_blocks(self, fx, monkeypatch):
        wt = self._merged(fx, monkeypatch)
        outside = fx.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, wt.path / "link")
        assert not crw.evaluate_worktree(fx.repo, wt, "o/r").safe

    def test_untracked_regular_file_blocks(self, fx, monkeypatch):
        wt = self._merged(fx, monkeypatch)
        (wt.path / "notes.txt").write_text("x\n", encoding="utf-8")
        assert not crw.evaluate_worktree(fx.repo, wt, "o/r").safe

    def test_untracked_directory_blocks(self, fx, monkeypatch):
        wt = self._merged(fx, monkeypatch)
        (wt.path / "dir").mkdir()
        (wt.path / "dir" / "f").write_text("x\n", encoding="utf-8")
        assert not crw.evaluate_worktree(fx.repo, wt, "o/r").safe

    def test_tracked_change_blocks_even_next_to_a_harmless_symlink(self, fx, monkeypatch):
        wt = self._merged(fx, monkeypatch)
        os.symlink(fx.repo / ".venv", wt.path / ".venv")
        (wt.path / "a.txt").write_text("changed\n", encoding="utf-8")
        assert not crw.evaluate_worktree(fx.repo, wt, "o/r").safe

    def test_staged_change_blocks(self, fx, monkeypatch):
        wt = self._merged(fx, monkeypatch)
        (wt.path / "b.txt").write_text("b\n", encoding="utf-8")
        git(wt.path, "add", "b.txt")
        assert not crw.evaluate_worktree(fx.repo, wt, "o/r").safe


class TestCleanCheckReadsBytes:
    """The clean check reads ``git status -z`` as bytes: a name with ``\\r`` or ``\\n`` is the
    name on disk, not a universal-newline rewrite of it."""

    @staticmethod
    def _make(path: Path, *, file: bool = False, link: bool = False, target: Path | None = None,
              name_file: str = "", name_link: str = "") -> None:
        try:
            if file:
                (path / name_file).write_text("work\n", encoding="utf-8")
            if link:
                os.symlink(target, path / name_link)
        except OSError as exc:
            pytest.skip(f"filesystem refuses the filename: {exc}")

    def test_cr_file_next_to_an_internal_symlink_named_with_newline_is_not_clean(self, fx):
        wt = fx.worktree("x")
        (fx.repo / ".venv").mkdir()
        self._make(wt.path, file=True, link=True, target=fx.repo / ".venv",
                   name_file="notes\r", name_link="notes\n")
        assert not crw.is_worktree_clean(wt.path, fx.repo)

    def test_untracked_file_with_a_newline_in_its_name_is_not_clean(self, fx):
        wt = fx.worktree("x")
        self._make(wt.path, file=True, name_file="a\nb")
        assert not crw.is_worktree_clean(wt.path, fx.repo)

    def test_only_an_internal_symlink_is_clean(self, fx):
        wt = fx.worktree("x")
        (fx.repo / ".venv").mkdir()
        os.symlink(fx.repo / ".venv", wt.path / ".venv")
        assert crw.is_worktree_clean(wt.path, fx.repo)

    def test_staged_rename_is_not_clean_and_second_path_is_not_judged_alone(self, fx):
        wt = fx.worktree("x")
        (fx.repo / ".venv").mkdir()
        # the rename source is itself named like an untracked internal symlink record
        git(wt.path, "mv", "a.txt", "renamed.txt")
        os.symlink(fx.repo / ".venv", wt.path / ".venv")
        assert not crw.is_worktree_clean(wt.path, fx.repo)

    def test_failing_git_status_is_not_clean(self, fx, tmp_path):
        assert not crw.is_worktree_clean(tmp_path / "missing", fx.repo)


def run_main(fx: Fixture, *extra: str) -> dict:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = crw.main(["--repo-root", str(fx.repo), "--apply", "--json", *extra])
    assert rc == 0
    return json.loads(buf.getvalue())


class TestT0Reject:
    """W5."""

    def test_reject_releases_the_worktree_and_keeps_the_branch(self, fx, monkeypatch, tmp_path):
        monkeypatch.setenv("VNX_PROJECT_ID", "alpha")
        wt = fx.worktree("x")
        stub_prs(monkeypatch, [])
        write_decision(tmp_path / "home", "alpha", "x", "reject", "alpha")
        report = run_main(fx)
        assert report["candidates"][0]["released"] is True, report["candidates"][0]["reason"]
        assert not wt.path.exists()
        assert fx.branch_exists("dispatch/x")

    def test_reject_in_another_projects_log_releases_nothing(self, fx, monkeypatch, tmp_path):
        monkeypatch.setenv("VNX_PROJECT_ID", "alpha")
        wt = fx.worktree("x")
        stub_prs(monkeypatch, [])
        write_decision(tmp_path / "home", "beta", "x", "reject", "alpha")
        write_decision(tmp_path / "home", "beta", "x", "reject", "beta")
        report = run_main(fx)
        assert report["candidates"][0]["released"] is False
        assert wt.path.exists()

    def test_reject_followed_by_accept_releases_nothing(self, fx, monkeypatch, tmp_path):
        wt = fx.worktree("x")
        stub_prs(monkeypatch, [])
        write_decision(tmp_path / "home", "alpha", "x", "reject", "alpha")
        write_decision(tmp_path / "home", "alpha", "x", "accept", "alpha")
        run_main(fx)
        assert wt.path.exists()

    def test_reject_with_a_dirty_tree_stays(self, fx, monkeypatch, tmp_path):
        wt = fx.worktree("x")
        stub_prs(monkeypatch, [])
        write_decision(tmp_path / "home", "alpha", "x", "reject", "alpha")
        (wt.path / "a.txt").write_text("changed\n", encoding="utf-8")
        run_main(fx)
        assert wt.path.exists() and fx.branch_exists("dispatch/x")

    def test_project_id_comes_from_the_marker_when_the_env_is_unset(self, fx, monkeypatch, tmp_path):
        wt = fx.worktree("x")
        stub_prs(monkeypatch, [])
        write_decision(tmp_path / "home", "alpha", "x", "reject", "alpha")
        run_main(fx)
        assert not wt.path.exists()

    def test_mismatching_env_project_is_refused_by_name(self, fx, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("VNX_PROJECT_ID", "beta")
        wt = fx.worktree("x")
        write_decision(tmp_path / "home", "alpha", "x", "reject", "alpha")
        rc = crw.main(["--repo-root", str(fx.repo), "--apply"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "beta" in err and "alpha" in err
        assert wt.path.exists()


class TestSafety:
    """W6."""

    def test_without_apply_nothing_changes(self, fx, monkeypatch, tmp_path):
        wt = fx.worktree("x")
        stub_prs(monkeypatch, [{"number": 7, "state": "MERGED", "headRefOid": wt.head_sha}])
        rc = crw.main(["--repo-root", str(fx.repo), "--json"])
        assert rc == 0
        assert wt.path.exists() and fx.branch_exists("dispatch/x")
        assert any(w.dispatch_dir_id == "x" for w in crw.discover_candidates(fx.repo))

    def test_current_worktree_stays(self, fx, monkeypatch):
        wt = fx.worktree("x")
        stub_prs(monkeypatch, [{"number": 7, "state": "MERGED", "headRefOid": wt.head_sha}])
        monkeypatch.setenv("VNX_DISPATCH_ID", "x")
        run_main(fx)
        assert wt.path.exists()

    def test_cwd_inside_the_worktree_stays(self, fx, monkeypatch):
        wt = fx.worktree("x")
        stub_prs(monkeypatch, [{"number": 7, "state": "MERGED", "headRefOid": wt.head_sha}])
        monkeypatch.chdir(wt.path)
        run_main(fx)
        assert wt.path.exists()

    def test_repo_root_is_required(self):
        with pytest.raises(SystemExit):
            crw.main([])

    def test_an_unlocked_worktree_is_a_candidate_too(self, fx, monkeypatch):
        """PR-4b: the sweep's candidates come from the inventory, so dropping the
        preserve lock no longer hides a tree from it. The guard, not the sweep,
        then decides whether it may go."""
        wt = fx.worktree("x")
        git(fx.repo, "worktree", "unlock", str(wt.path))
        candidates = [c for c in crw.discover_candidates(fx.repo) if c.dispatch_dir_id == "x"]
        assert len(candidates) == 1 and candidates[0].lock_reason is None
        assert candidates[0].kind == "dispatch"

    def test_a_guarded_refusal_leaves_the_preserve_lock_in_place(self, fx, monkeypatch):
        """A locked tree the guard keeps must stay LOCKED: the sweep drops the
        lock only inside the guard's removal, never while it is still judging."""
        import dispatch_process_registry as registry

        wt = fx.worktree("x")
        git(fx.repo, "merge", "-q", "--no-ff", "-m", "merge", "dispatch/x")
        git(fx.repo, "push", "-q", "origin", "main")
        git(fx.repo, "fetch", "-q", "origin")
        registry.record_dispatch_pgids("x", [os.getpgid(0)], repo_root=fx.repo)

        def _denied(pid):
            raise PermissionError(f"not allowed to inspect {pid}")

        monkeypatch.setattr(os, "getpgid", _denied)
        report = run_main(fx)
        candidate = report["candidates"][0]
        assert candidate["released"] is False
        assert candidate["guard_reason"] == "liveness_unknown", candidate
        assert wt.path.exists()
        assert "locked" in git(fx.repo, "worktree", "list", "--porcelain")
