"""The scheduled worktree sweep sees every owned worktree, locked or not
(PR-4 of track ``worktree-opruiming-structureel``), and never releases a live
one.

Every test builds a real tmp repo with a bare ``origin`` and real worktrees and
runs the real ``main``; only the two network/OS seams (``branch_has_open_pr``,
the occupancy probe) are stubbed. The occupancy and gate-lifetime tests take
the REAL flock.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import cleanup_reviewed_worktrees as crw  # noqa: E402
import dispatch_worktree_isolation as dwi  # noqa: E402
from dispatch_process_registry import record_dispatch_pgids  # noqa: E402

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
    """One clean detached gate tree, one clean merged dispatch tree, one dirty
    dispatch tree, one clean pool tree and one clean bench tree."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.origin = tmp_path / "origin.git"
        self.repo = tmp_path / "repo"
        self.bench_root = tmp_path / "bench"
        worktrees = self.repo / ".vnx-data" / "worktrees"
        self.gate = worktrees / "gate-g1-abc-1a2b3c4d"
        self.merged = worktrees / "dispatch-merged"
        self.dirty = worktrees / "dispatch-dirty"
        self.pool = worktrees / "pool-p1"
        self.bench = self.bench_root / "dispatch-bench1"
        self.bench_root.mkdir()
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(tmp_path, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        (self.repo / ".vnx-project-id").write_text("alpha\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text(".vnx-data/\n", encoding="utf-8")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")

        git(self.repo, "worktree", "add", "-q", "--detach", str(self.gate), "origin/main")
        # The lifetime sidecar a `create_gate_worktree` call leaves behind,
        # present and unheld: this tree's seat is over, so the ordinary gate
        # release rule applies. Tests that want a live or missing seat
        # override it.
        self.gate_live = self.gate.parent / (self.gate.name + ".live")
        self.gate_live.write_text("", encoding="utf-8")
        git(self.repo, "worktree", "add", "-q", "-b", "dispatch/merged", str(self.merged), "main")
        (self.merged / "merged.txt").write_text("work\n", encoding="utf-8")
        git(self.merged, "add", "-A")
        git(self.merged, "commit", "-q", "-m", "work")
        git(self.merged, "push", "-q", "origin", "dispatch/merged")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge dispatch/merged", "dispatch/merged")
        git(self.repo, "push", "-q", "origin", "main")

        git(self.repo, "worktree", "add", "-q", "-b", "dispatch/dirty", str(self.dirty), "main")
        (self.dirty / "notes.txt").write_text("x\n", encoding="utf-8")
        git(self.repo, "worktree", "add", "-q", "-b", "pool/p1", str(self.pool), "main")
        git(self.repo, "worktree", "add", "-q", "-b", "dispatch/bench1", str(self.bench), "main")


@pytest.fixture
def fx(tmp_path, monkeypatch) -> Fixture:
    monkeypatch.delenv("VNX_DISPATCH_ID", raising=False)
    monkeypatch.delenv("VNX_CURRENT_DISPATCH_ID", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    for key in ("VNX_DATA_DIR_EXPLICIT", "VNX_DATA_DIR", "VNX_DATA_HOME", "VNX_STATE_DIR"):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("VNX_BENCH_WORKTREE_ROOT", str(tmp_path / "bench"))
    return Fixture(tmp_path)


def stub_open_prs(monkeypatch, has_open: bool | None) -> None:
    monkeypatch.setattr(crw, "branch_has_open_pr", lambda slug, branch: has_open, raising=False)


def run_main(fx: Fixture, *extra: str) -> list[dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = crw.main(["--repo-root", str(fx.repo), "--json", *extra])
    assert rc == 0
    return json.loads(buf.getvalue())["candidates"]


def entry_for(entries: list[dict], path: Path) -> dict:
    resolved = path.resolve()
    matches = [e for e in entries if Path(e["path"]) == resolved]
    assert matches, f"{resolved} not in report: {[e['path'] for e in entries]}"
    return matches[0]


class TestUnlockedRelease:
    def test_a_clean_detached_gate_tree_on_origin_is_released(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        entries = run_main(fx, "--apply")
        assert entry_for(entries, fx.gate)["released"] is True
        assert not fx.gate.exists()

    def test_a_clean_merged_dispatch_tree_is_released(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        entries = run_main(fx, "--apply")
        entry = entry_for(entries, fx.merged)
        assert entry["released"] is True and entry["delete_branch"] is True
        assert not fx.merged.exists()

    def test_a_dirty_unlocked_tree_is_kept_with_the_reason(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        entry = entry_for(run_main(fx, "--apply"), fx.dirty)
        assert entry["safe_to_release"] is False and entry["released"] is False
        assert "niet schoon" in entry["reason"]
        assert fx.dirty.exists()

    def test_an_open_pr_keeps_the_clean_unlocked_tree(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, True)
        entry = entry_for(run_main(fx, "--apply"), fx.merged)
        assert entry["safe_to_release"] is False and "OPEN" in entry["reason"]
        assert fx.merged.exists()

    def test_an_unmeasurable_open_pr_check_keeps_the_tree(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, None)
        entry = entry_for(run_main(fx, "--apply"), fx.merged)
        assert entry["safe_to_release"] is False and "onmeetbaar" in entry["reason"]
        assert fx.merged.exists()

    def test_a_gate_head_outside_every_origin_ref_is_kept(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        (fx.gate / "gate-work.txt").write_text("w\n", encoding="utf-8")
        git(fx.gate, "add", "-A")
        git(fx.gate, "commit", "-q", "-m", "only here")
        entry = entry_for(run_main(fx, "--apply"), fx.gate)
        assert entry["safe_to_release"] is False and "origin/*" in entry["reason"]
        assert fx.gate.exists()

    def test_the_main_checkout_is_never_a_candidate(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        entries = run_main(fx, "--apply")
        assert fx.repo.resolve() not in {Path(e["path"]) for e in entries}


class TestGateSeatLiveness:
    """A gate checkout is only released when its lifetime-lock sidecar exists
    and no reviewer holds it. The REAL flock is taken here, as in the
    occupancy test: a second open+LOCK_EX is a live seat (flock is per open
    file description, so this conflicts even inside one process)."""

    def test_a_live_gate_seat_is_kept_and_apply_never_removes_it(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        with open(fx.gate_live, "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            try:
                entries = run_main(fx, "--apply")
            finally:
                fcntl.flock(holder, fcntl.LOCK_UN)
        entry = entry_for(entries, fx.gate)
        assert entry["safe_to_release"] is False
        assert entry["reason"] == "gate seat live (lifetime lock held)"
        assert entry["released"] is False
        assert fx.gate.exists()

    def test_the_same_tree_is_released_after_the_holder_exits(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        with open(fx.gate_live, "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            assert entry_for(run_main(fx), fx.gate)["safe_to_release"] is False

        entry = entry_for(run_main(fx, "--apply"), fx.gate)

        assert entry["released"] is True
        assert not fx.gate.exists()
        assert not fx.gate_live.exists()

    def test_a_gate_tree_without_a_sidecar_is_kept(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        fx.gate_live.unlink()
        entry = entry_for(run_main(fx, "--apply"), fx.gate)
        assert entry["safe_to_release"] is False
        assert "without lifetime lock" in entry["reason"]
        assert entry["released"] is False
        assert fx.gate.exists()

    def test_an_unmeasurable_lifetime_lock_keeps_the_tree(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        fx.gate_live.unlink()
        fx.gate_live.mkdir()  # cannot be opened O_RDWR -> unmeasured
        entry = entry_for(run_main(fx, "--apply"), fx.gate)
        assert entry["safe_to_release"] is False
        assert "niet meetbaar" in entry["reason"]
        assert fx.gate.exists()

    def test_release_refuses_when_a_seat_starts_between_probe_and_removal(self, fx):
        """The lock is re-taken for the removal itself: a seat that grabbed it
        after the probe keeps the tree instead of being deleted under it."""
        candidate = crw.WorktreeCandidate(
            path=fx.gate.resolve(), dispatch_dir_id=None, branch=None,
            head_sha=git(fx.gate, "rev-parse", "HEAD"), lock_reason=None,
            locked=False, kind="gate", dirty_count=0, unpushed=0,
        )
        with open(fx.gate_live, "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            try:
                result = crw.release_worktree(fx.repo, candidate, delete_branch=False)
            finally:
                fcntl.flock(holder, fcntl.LOCK_UN)
        assert result.released is False
        assert "lifetime lock" in result.errors[0]
        assert fx.gate.exists()


class TestLivenessGuard:
    def test_a_held_occupancy_lock_keeps_a_clean_merged_tree(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        lock_path = crw.claims_dir_for_repo(fx.repo) / "merged.occupancy"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            try:
                entries = run_main(fx, "--apply")
            finally:
                fcntl.flock(holder, fcntl.LOCK_UN)
        entry = entry_for(entries, fx.merged)
        assert entry["safe_to_release"] is False, entry["reason"]
        assert "liveness" in entry["reason"]
        assert fx.merged.exists()

    def test_a_live_recorded_process_group_keeps_the_tree(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        record_dispatch_pgids("merged", [os.getpgrp()], repo_root=fx.repo)
        entry = entry_for(run_main(fx, "--apply"), fx.merged)
        assert entry["safe_to_release"] is False, entry["reason"]
        assert "liveness" in entry["reason"] and "procesgroep" in entry["reason"]
        assert fx.merged.exists()

    def test_an_unmeasurable_liveness_probe_keeps_the_tree(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)

        def boom(*args, **kwargs):
            raise OSError("probe kapot")

        monkeypatch.setattr(dwi, "probe_occupancy", boom)
        entry = entry_for(run_main(fx, "--apply"), fx.merged)
        assert entry["safe_to_release"] is False and "liveness" in entry["reason"]
        assert fx.merged.exists()


class TestOwnerOwnedKinds:
    def test_pool_and_bench_are_listed_but_never_released(self, fx, monkeypatch):
        stub_open_prs(monkeypatch, False)
        entries = run_main(fx, "--apply")
        pool, bench = entry_for(entries, fx.pool), entry_for(entries, fx.bench)
        assert (pool["kind"], bench["kind"]) == ("pool", "bench")
        assert pool["safe_to_release"] is False and bench["safe_to_release"] is False
        assert "pool" in pool["reason"] and "bench" in bench["reason"]
        assert fx.pool.exists() and fx.bench.exists()


def test_an_unreadable_process_registry_keeps_the_tree(fx, monkeypatch):
    """A corrupt registry entry is unmeasurable, not evidence of a dead group."""
    stub_open_prs(monkeypatch, False)
    entry_path = fx.repo / ".vnx-data" / "state" / "dispatch_pgids" / "merged.json"
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text("{not json", encoding="utf-8")
    entry = entry_for(run_main(fx, "--apply"), fx.merged)
    assert entry["safe_to_release"] is False and "liveness" in entry["reason"]
    assert fx.merged.exists()
