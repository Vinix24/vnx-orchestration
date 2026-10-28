"""PR-5: ``--max-age-days`` and ``--include-dirty`` on the reviewed-worktree sweep.

Both flags are off by default, so nothing about the existing sweep changes
unless an operator asks for it. What they ask for is narrow and fail-closed:

* an age gate measured exactly like ``worktree_inventory`` measures it (the
  mtime of the tree's ``.git``), so a tree cannot be young in one report and
  old in the other;
* before any dirty or unpushed tree is removed: no OPEN pull request (an
  unmeasurable or failing ``gh`` keeps the tree), no live or unknown holder
  (the process register and the occupancy slot), then a *written* salvage
  whose ``verify`` passes on exactly the artifacts it produced;
* a failed salvage or verify keeps the tree and names why;
* every removal goes through ``guarded_remove``, and the launchd template
  carries neither flag — turning either on stays a decision for Vincent.

Every test builds a real bare ``origin``, a real main checkout and real
``git worktree`` trees under ``tmp_path``. The only stubbed boundary is
``run_gh_pr_list`` (GitHub is not reachable from a test).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import cleanup_reviewed_worktrees as crw  # noqa: E402
import dispatch_process_registry as registry  # noqa: E402
import dispatch_worktree_isolation as dwi  # noqa: E402
import worktree_salvage as ws  # noqa: E402

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
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        (self.repo / ".vnx-project-id").write_text("alpha\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text(".vnx-data/\n", encoding="utf-8")
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        self.worktrees = self.repo / ".vnx-data" / "worktrees"

    def tree(self, dispatch_id: str, *, days_old: int = 20, dirty: bool = True,
             unpushed: bool = True, claim: bool = True, lock: bool = True) -> Path:
        """A preserved dispatch tree: an unpushed commit, a tracked change, an untracked file."""
        path = self.worktrees / f"dispatch-{dispatch_id}"
        branch = f"dispatch/{dispatch_id}"
        git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
        if unpushed:
            (path / "work.txt").write_text("work\n", encoding="utf-8")
            git(path, "add", "-A")
            git(path, "commit", "-q", "-m", "work")
        if dirty:
            (path / "a.txt").write_text("changed\n", encoding="utf-8")       # unstaged patch
            (path / "scratch.txt").write_text("scratch\n", encoding="utf-8")  # untracked tarball
        if claim:
            dwi._write_claim_atomic(dispatch_id, worktree_path=path, project_root=self.repo)
            dwi._acquire_occupancy(dispatch_id, path, self.repo)
            dwi._release_occupancy(path)
        if lock:
            git(self.repo, "worktree", "lock", "--reason", "vnx preserve 2026", str(path))
        age = time.time() - days_old * 86400
        os.utime(path / ".git", (age, age))
        return path

    def branch_exists(self, branch: str) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=self.repo, capture_output=True
        )
        return proc.returncode == 0


@pytest.fixture
def fx(tmp_path, monkeypatch) -> Fixture:
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
    """A github origin (the tmp repo's origin is a local path) reporting no PR."""
    monkeypatch.setattr(crw, "get_repo_slug", lambda repo_root: "o/r")
    monkeypatch.setattr(crw, "run_gh_pr_list", lambda slug, branch: "[]")


# ---------------------------------------------------------------------------
# 1. age gate: a young tree is not a candidate, and nothing is salvaged
# ---------------------------------------------------------------------------

def test_a_young_tree_is_kept_by_the_age_gate(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    tree = fx.tree("young-one", days_old=2)
    payload = run_main(fx, capsys, "--max-age-days", "14", "--apply")
    entry = candidate_for(payload, tree)
    assert entry["released"] is False
    assert "too_young" in entry["reason"], entry["reason"]
    assert "salvage_dir" not in entry, "no salvage keys outside the --include-dirty route"
    assert tree.exists()


# ---------------------------------------------------------------------------
# 2. without --include-dirty the dirty/unpushed tree stays, with no salvage
# ---------------------------------------------------------------------------

def test_an_old_dirty_unpushed_tree_stays_without_include_dirty(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    tree = fx.tree("dirty-old")
    payload = run_main(fx, capsys, "--max-age-days", "14", "--apply")
    entry = candidate_for(payload, tree)
    assert entry["released"] is False
    assert entry["safe_to_release"] is False
    assert "niet schoon" in entry["reason"], entry["reason"]
    assert "salvage_dir" not in entry, "no salvage keys outside the --include-dirty route"
    assert tree.exists() and (tree / "scratch.txt").exists()


# ---------------------------------------------------------------------------
# 3. --include-dirty: bundle + patch + tarball, verify passes, then the tree goes
# ---------------------------------------------------------------------------

def test_include_dirty_salvages_verifies_and_then_removes(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    tree = fx.tree("dirty-old")
    payload = run_main(fx, capsys, "--max-age-days", "14", "--include-dirty", "--apply")
    entry = candidate_for(payload, tree)
    assert entry["released"] is True, (entry["reason"], entry["guard_reason"], entry["guard_detail"])
    assert entry["salvage_verified"] is True
    assert not tree.exists(), "a verified salvage lets the tree go"

    names = {Path(artifact).name for artifact in entry["salvage_artifacts"]}
    assert any(name.endswith(ws.BUNDLE_SUFFIX) for name in names), names
    assert any(name.endswith(ws.UNSTAGED_SUFFIX) for name in names), names
    assert any(name.endswith(ws.UNTRACKED_ARCHIVE_SUFFIX) for name in names), names
    for artifact in entry["salvage_artifacts"]:
        assert Path(artifact).is_file(), artifact
    assert (Path(entry["salvage_dir"]).parent / ws.MANIFEST_NAME).is_file()
    assert fx.branch_exists("dispatch/dirty-old"), "the dirty route never deletes the branch"


# ---------------------------------------------------------------------------
# 4. an OPEN pull request keeps the tree, also with --include-dirty
# ---------------------------------------------------------------------------

def test_an_open_pr_keeps_the_tree_even_with_include_dirty(fx, monkeypatch, capsys):
    monkeypatch.setattr(crw, "get_repo_slug", lambda repo_root: "o/r")
    monkeypatch.setattr(
        crw, "run_gh_pr_list",
        lambda slug, branch: json.dumps([{"number": 9, "state": "OPEN", "headRefOid": "0" * 40}]),
    )
    tree = fx.tree("dirty-open-pr")
    payload = run_main(fx, capsys, "--max-age-days", "14", "--include-dirty", "--apply")
    entry = candidate_for(payload, tree)
    assert entry["released"] is False
    assert "open_pr" in entry["reason"], entry["reason"]
    assert "salvage_dir" not in entry, "no salvage for a tree a PR still owns"
    assert tree.exists()


# ---------------------------------------------------------------------------
# 5. a gh lookup that fails is unmeasurable, never "no open PR"
# ---------------------------------------------------------------------------

def test_a_gh_lookup_that_raises_keeps_the_tree(fx, monkeypatch, capsys):
    monkeypatch.setattr(crw, "get_repo_slug", lambda repo_root: "o/r")

    def boom(slug, branch):
        raise RuntimeError("gh is not installed")

    monkeypatch.setattr(crw, "run_gh_pr_list", boom)
    tree = fx.tree("dirty-no-gh")
    payload = run_main(fx, capsys, "--max-age-days", "14", "--include-dirty", "--apply")
    entry = candidate_for(payload, tree)
    assert entry["released"] is False
    assert "no_gh" in entry["reason"], entry["reason"]
    assert "salvage_dir" not in entry, "no salvage keys outside the --include-dirty route"
    assert tree.exists()


# ---------------------------------------------------------------------------
# 6. a live (alive) dispatch keeps the tree
# ---------------------------------------------------------------------------

def test_a_live_dispatch_keeps_the_tree(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    tree = fx.tree("live-one")
    registry.record_dispatch_pgids("live-one", [os.getpgid(0)], repo_root=fx.repo)
    payload = run_main(fx, capsys, "--max-age-days", "14", "--include-dirty", "--apply")
    entry = candidate_for(payload, tree)
    assert entry["released"] is False
    assert "liveness_alive" in entry["reason"], entry["reason"]
    assert "salvage_dir" not in entry, "no salvage keys outside the --include-dirty route"
    assert tree.exists()


# ---------------------------------------------------------------------------
# 7. an UNKNOWN liveness keeps the tree exactly like alive
# ---------------------------------------------------------------------------

def test_an_unknown_liveness_keeps_the_tree(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    tree = fx.tree("unknown-one")
    registry.record_dispatch_pgids("unknown-one", [os.getpgid(0)], repo_root=fx.repo)

    def _denied(pid):
        raise PermissionError(f"not allowed to inspect {pid}")

    monkeypatch.setattr(os, "getpgid", _denied)
    payload = run_main(fx, capsys, "--max-age-days", "14", "--include-dirty", "--apply")
    entry = candidate_for(payload, tree)
    assert entry["released"] is False
    assert "liveness_unknown" in entry["reason"], entry["reason"]
    assert tree.exists()


# ---------------------------------------------------------------------------
# 8. a verify that fails keeps the tree; the artifacts stay as evidence
# ---------------------------------------------------------------------------

def test_a_failed_verify_keeps_the_tree(fx, monkeypatch, capsys):
    stub_no_prs(monkeypatch)
    tree = fx.tree("verify-fails")
    monkeypatch.setattr(
        ws, "verify",
        lambda result, dest_dir, *, repo_root: ["artifact missing: the bundle"],
    )
    payload = run_main(fx, capsys, "--max-age-days", "14", "--include-dirty", "--apply")
    entry = candidate_for(payload, tree)
    assert entry["released"] is False
    assert entry["salvage_verified"] is False
    assert "salvage_unverified" in entry["reason"], entry["reason"]
    assert "artifact missing" in entry["reason"], entry["reason"]
    assert tree.exists()
    assert Path(entry["salvage_dir"]).is_dir(), "the artifacts stay as evidence"


# ---------------------------------------------------------------------------
# 9. the launchd template carries neither flag (Vincent's decision, not a job's)
# ---------------------------------------------------------------------------

def test_the_launchd_template_carries_neither_flag():
    template = REPO / "scripts" / "launchd" / "com.vnx.cleanup-reviewed-worktrees.plist"
    text = template.read_text(encoding="utf-8")
    assert "--max-age-days" not in text
    assert "--include-dirty" not in text
