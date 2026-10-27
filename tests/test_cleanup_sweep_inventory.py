"""PR-4b: the sweep's candidates are the inventory's owned worktrees, and every
removal goes through ``worktree_removal_guard.guarded_remove``.

Real git repos under ``tmp_path`` and a real second process for the occupancy
test. The only stubbed boundary is ``run_gh_pr_list`` (GitHub is not reachable
from a test); it is stubbed to its own documented "gh missing" reply where the
test is about that case.
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
import dispatch_process_registry as registry  # noqa: E402
import dispatch_worktree_isolation as dwi  # noqa: E402
from gate_worktree import _release_lifetime_lock, create_gate_worktree  # noqa: E402

LIB_DIR = REPO / "scripts" / "lib"

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
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.origin = tmp_path / "origin.git"
        self.repo = tmp_path / "repo"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(tmp_path, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        (self.repo / ".vnx-project-id").write_text("alpha\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text(".vnx-data/\n", encoding="utf-8")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        # one feature branch that ships through main — what a gate seat reviews
        git(self.repo, "checkout", "-q", "-b", "feature/ready")
        (self.repo / "feature.txt").write_text("ready\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "feature")
        git(self.repo, "push", "-q", "origin", "feature/ready")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature/ready")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        self.worktrees = self.repo / ".vnx-data" / "worktrees"

    def dispatch_tree(self, dispatch_id: str, *, dirty: bool = False, claim: bool = True) -> Path:
        """A published, merged dispatch tree with the claim/occupancy a real one has."""
        path = self.worktrees / f"dispatch-{dispatch_id}"
        branch = f"dispatch/{dispatch_id}"
        git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
        (path / f"{dispatch_id}.txt").write_text("work\n", encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "work")
        git(path, "push", "-q", "origin", branch)
        git(self.repo, "merge", "-q", "--no-ff", "-m", f"merge {dispatch_id}", branch)
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        if claim:
            dwi._write_claim_atomic(dispatch_id, worktree_path=path, project_root=self.repo)
            dwi._acquire_occupancy(dispatch_id, path, self.repo)
            dwi._release_occupancy(path)
        if dirty:
            (path / "uncommitted.txt").write_text("must survive\n", encoding="utf-8")
        return path

    def gate_tree(self) -> Path:
        """A gate checkout whose seat has gone (sidecar released, tree stays)."""
        path = create_gate_worktree(
            branch="feature/ready", gate="codex_gate", identifier="seat", project_root=self.repo,
        )
        _release_lifetime_lock(path)
        return path

    def branch_exists(self, branch: str) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
            cwd=self.repo, capture_output=True,
        )
        return proc.returncode == 0


@pytest.fixture
def fx(tmp_path, monkeypatch) -> Fixture:
    """The conftest data-dir pin is kept on purpose: the claim registry, the
    pgid registry and the receipts then all resolve to one per-test store."""
    for key in ("VNX_DISPATCH_ID", "VNX_CURRENT_DISPATCH_ID", "VNX_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
    return Fixture(tmp_path)


def run_main(fx: Fixture, capsys, *extra: str) -> dict:
    rc = crw.main(["--repo-root", str(fx.repo), "--json", *extra])
    out, _ = capsys.readouterr()
    assert rc == 0, out
    return json.loads(out)


def candidate_for(payload: dict, path: Path) -> dict:
    found = [c for c in payload["candidates"] if Path(c["path"]) == path.resolve()]
    assert len(found) == 1, [c["path"] for c in payload["candidates"]]
    return found[0]


def stub_no_prs(monkeypatch) -> None:
    """A github origin (the tmp repo's origin is a local path, so the slug parser
    cannot recognise it) reporting no pull request for any branch."""
    monkeypatch.setattr(crw, "get_repo_slug", lambda repo_root: "o/r")
    monkeypatch.setattr(crw, "run_gh_pr_list", lambda slug, branch: "[]")


# ---------------------------------------------------------------------------
# 1. all three owned trees are candidates (red on origin/main: the report is
#    empty — only locked dispatch trees were ever looked at)
# ---------------------------------------------------------------------------

def test_all_three_owned_trees_are_in_the_report(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    gate = fx.gate_tree()
    clean = fx.dispatch_tree("clean-one")
    dirty = fx.dispatch_tree("dirty-two", dirty=True)
    payload = run_main(fx, capsys)
    assert {Path(c["path"]) for c in payload["candidates"]} == {
        gate.resolve(), clean.resolve(), dirty.resolve(),
    }


# ---------------------------------------------------------------------------
# 2. a clean, merged gate tree without a lock is released
# ---------------------------------------------------------------------------

def test_a_clean_merged_gate_tree_is_released(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    gate = fx.gate_tree()
    payload = run_main(fx, capsys, "--apply")
    entry = candidate_for(payload, gate)
    assert entry["kind"] == "gate" and entry["lock_reason"] is None
    assert entry["safe_to_release"] is True, entry["reason"]
    assert entry["released"] is True, (entry["guard_reason"], entry["guard_detail"])
    assert not gate.exists()


# ---------------------------------------------------------------------------
# 3. a clean, merged dispatch tree without a lock is released
# ---------------------------------------------------------------------------

def test_a_clean_merged_dispatch_tree_without_a_lock_is_released(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    tree = fx.dispatch_tree("no-lock")
    payload = run_main(fx, capsys, "--apply")
    entry = candidate_for(payload, tree)
    assert entry["safe_to_release"] is True, entry["reason"]
    assert entry["released"] is True, (entry["guard_reason"], entry["guard_detail"])
    assert not tree.exists()
    assert not fx.branch_exists("dispatch/no-lock"), "every commit is on origin; the local branch goes"


# ---------------------------------------------------------------------------
# 4. a dirty tree stays, with its reason
# ---------------------------------------------------------------------------

def test_a_dirty_unlocked_tree_stays_with_its_reason(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    tree = fx.dispatch_tree("dirty-one", dirty=True)
    payload = run_main(fx, capsys, "--apply")
    entry = candidate_for(payload, tree)
    assert entry["released"] is False
    assert entry["safe_to_release"] is False
    assert "niet schoon" in entry["reason"]
    assert tree.exists() and (tree / "uncommitted.txt").exists()


# ---------------------------------------------------------------------------
# 5. an unmeasurable process group keeps the tree (guard's reason, not ours)
# ---------------------------------------------------------------------------

def test_an_unknown_process_group_keeps_the_tree(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    tree = fx.dispatch_tree("unknown-pgid")
    registry.record_dispatch_pgids("unknown-pgid", [os.getpgid(0)], repo_root=fx.repo)

    def _denied(pid):
        raise PermissionError(f"not allowed to inspect {pid}")

    monkeypatch.setattr(os, "getpgid", _denied)
    payload = run_main(fx, capsys, "--apply")
    entry = candidate_for(payload, tree)
    assert entry["released"] is False
    assert entry["guard_reason"] == "liveness_unknown", entry["guard_detail"]
    assert tree.exists()


# ---------------------------------------------------------------------------
# 6. a slot another process holds keeps the tree
# ---------------------------------------------------------------------------

_HOLDER_SCRIPT = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import dispatch_worktree_isolation as dwi
dwi._acquire_occupancy(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
print("ready", flush=True)
sys.stdin.readline()
"""


def test_an_occupancy_slot_held_elsewhere_keeps_the_tree(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    tree = fx.dispatch_tree("occupied-one")
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(LIB_DIR), "occupied-one", str(tree), str(fx.repo)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready", holder.stderr.read()
        payload = run_main(fx, capsys, "--apply")
        entry = candidate_for(payload, tree)
        assert entry["released"] is False
        assert entry["guard_reason"] == "occupied", entry["guard_detail"]
        assert tree.exists()
    finally:
        if holder.stdin is not None:
            holder.stdin.write("\n")
            holder.stdin.flush()
        holder.wait(timeout=30)


# ---------------------------------------------------------------------------
# 7. without gh an unlocked tree stays (no_gh), never "no open PR"
# ---------------------------------------------------------------------------

def test_without_gh_an_unlocked_tree_stays(fx, monkeypatch, capsys):
    tree = fx.dispatch_tree("no-gh")
    monkeypatch.setattr(crw, "get_repo_slug", lambda repo_root: "o/r")
    # run_gh_pr_list's own reply when gh is not installed: "" (not "[]").
    monkeypatch.setattr(crw, "run_gh_pr_list", lambda slug, branch: "")
    payload = run_main(fx, capsys, "--apply")
    entry = candidate_for(payload, tree)
    assert entry["released"] is False
    assert entry["safe_to_release"] is False
    assert "no_gh" in entry["reason"]
    assert tree.exists()


# ---------------------------------------------------------------------------
# 8. the default is a dry run: nothing is removed
# ---------------------------------------------------------------------------

def test_the_default_is_a_dry_run(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    gate = fx.gate_tree()
    tree = fx.dispatch_tree("dry-one")
    payload = run_main(fx, capsys)
    for path in (gate, tree):
        entry = candidate_for(payload, path)
        assert entry["safe_to_release"] is True, entry["reason"]
        assert entry["released"] is False
        assert entry["guard_reason"] == "dry_run", entry["guard_detail"]
    assert gate.exists() and tree.exists()


# ---------------------------------------------------------------------------
# 9. an unreadable inventory is exit 2, never an empty report
# ---------------------------------------------------------------------------

def test_a_failed_inventory_is_not_an_empty_report(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    rc = crw.main(["--repo-root", str(plain), "--json"])
    out, err = capsys.readouterr()
    assert rc == 2
    assert out == ""
    assert "not a git repository" in err
