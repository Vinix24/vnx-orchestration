"""The read-only worktree inventory (``scripts/lib/worktree_inventory.py``) and
``cleanup_reviewed_worktrees.py --all``.

Every test builds a real tmp git repo with a bare origin and real worktrees and
runs the real code; nothing here reimplements a parser or a counter.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import cleanup_reviewed_worktrees as crw  # noqa: E402
import worktree_inventory as wi  # noqa: E402

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
    """One locked dispatch tree, one unlocked gate tree with an untracked file,
    one bench tree with a commit no origin ref carries."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bench = tmp_path / "bench"
        self.origin = tmp_path / "origin.git"
        self.repo = tmp_path / "repo"
        self.dispatch = self.repo / ".vnx-data" / "worktrees" / "dispatch-d1"
        self.gate = self.repo / ".vnx-data" / "worktrees" / "gate-g1-abc-1a2b3c4d"
        self.bench_tree = self.bench / "dispatch-bench1"
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

        git(self.repo, "worktree", "add", "-q", "-b", "dispatch/d1", str(self.dispatch), "main")
        git(self.repo, "worktree", "lock", "--reason", "vnx preserve 2026-10-01", str(self.dispatch))
        git(self.repo, "worktree", "add", "-q", "--detach", str(self.gate), "main")
        (self.gate / "loose.txt").write_text("x\n", encoding="utf-8")
        git(self.repo, "worktree", "add", "-q", "-b", "dispatch/bench1", str(self.bench_tree), "main")
        (self.bench_tree / "bench.txt").write_text("x\n", encoding="utf-8")
        git(self.bench_tree, "add", "-A")
        git(self.bench_tree, "commit", "-q", "-m", "bench work")

    def owned(self) -> dict[Path, wi.WorktreeEntry]:
        return {e.path: e for e in wi.inventory(self.repo) if e.owned}


@pytest.fixture
def fx(tmp_path, monkeypatch) -> Fixture:
    monkeypatch.setenv("VNX_BENCH_WORKTREE_ROOT", str(tmp_path / "bench"))
    for key in ("VNX_DISPATCH_ID", "VNX_CURRENT_DISPATCH_ID", "VNX_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "bench").mkdir()
    return Fixture(tmp_path)


def run_main(capsys, *args: str) -> tuple[int, str, str]:
    rc = crw.main(list(args))
    out, err = capsys.readouterr()
    return rc, out, err


class TestInventory:
    def test_three_owned_trees_with_their_kinds(self, fx):
        assert {path: entry.kind for path, entry in fx.owned().items()} == {
            fx.dispatch.resolve(): "dispatch",
            fx.gate.resolve(): "gate",
            fx.bench_tree.resolve(): "bench",
        }

    def test_main_checkout_is_unowned_other(self, fx):
        main = [e for e in wi.inventory(fx.repo) if e.path == fx.repo.resolve()]
        assert len(main) == 1 and main[0].kind == "other" and not main[0].owned

    def test_measurements(self, fx):
        owned = fx.owned()
        dispatch, gate, bench = owned[fx.dispatch.resolve()], owned[fx.gate.resolve()], owned[fx.bench_tree.resolve()]
        assert dispatch.locked and dispatch.lock_reason == "vnx preserve 2026-10-01"
        assert not gate.locked and gate.branch is None and gate.lock_reason is None
        assert gate.dirty_count == 1
        assert bench.unpushed == 1 and dispatch.unpushed == 0
        assert all(entry.age_days == 0 and not entry.missing for entry in owned.values())

    def test_age_days_is_measured_against_now(self, fx):
        entries = wi.inventory(fx.repo, now=time.time() + 5 * 86400)
        dispatch = [e for e in entries if e.path == fx.dispatch.resolve()][0]
        assert dispatch.age_days == 5

    def test_the_inventory_sees_the_trees_the_sweep_is_blind_to(self, fx):
        """Red on origin/main: ``discover_locked_dispatch_worktrees`` returns 1
        here; the inventory must show all three owned trees."""
        seen = {wt.path for wt in crw.discover_locked_dispatch_worktrees(fx.repo)}
        assert set(fx.owned()) == {fx.dispatch.resolve(), fx.gate.resolve(), fx.bench_tree.resolve()}
        assert seen == {fx.dispatch.resolve()}

    def test_the_sweep_parses_through_the_inventory_parser(self, fx, monkeypatch):
        calls = []
        real = wi.parse_worktree_list_porcelain

        def spy(text):
            calls.append(text)
            return real(text)

        monkeypatch.setattr(wi, "parse_worktree_list_porcelain", spy)
        crw.discover_locked_dispatch_worktrees(fx.repo)
        assert calls

    def test_a_missing_tree_is_reported_not_dropped(self, fx):
        shutil.rmtree(fx.gate)
        entry = fx.owned()[fx.gate.resolve()]
        assert entry.missing and entry.dirty_count is None and entry.age_days is None

    def test_a_failed_git_worktree_list_raises(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(wi.WorktreeInventoryError) as exc:
            wi.inventory(plain)
        assert "not a git repository" in str(exc.value)


def test_a_rename_is_counted_once_and_its_source_is_not_a_path(tmp_path):
    raw = b"R  new.txt\x00old.txt\x00?? other.txt\x00"
    assert wi.count_dirty_paths(raw, tmp_path, tmp_path) == 2


class TestAllFlag:
    def test_all_json_lists_three_owned_trees(self, fx, capsys):
        rc, out, err = run_main(capsys, "--repo-root", str(fx.repo), "--all", "--json")
        assert rc == 0
        owned = [e for e in json.loads(out)["worktrees"] if e["owned"]]
        assert len(owned) == 3
        assert {e["kind"] for e in owned} == {"dispatch", "gate", "bench"}

    def test_all_table_names_every_kind(self, fx, capsys):
        rc, out, err = run_main(capsys, "--repo-root", str(fx.repo), "--all")
        assert rc == 0
        for label in ("[dispatch/owned]", "[gate/owned]", "[bench/owned]"):
            assert label in out

    def test_all_never_releases_a_tree_that_would_be_safe(self, fx, capsys, monkeypatch):
        wt = crw.discover_locked_dispatch_worktrees(fx.repo)[0]
        assert crw.evaluate_worktree(fx.repo, wt, None).safe
        monkeypatch.setattr(crw, "release_worktree", lambda *a, **k: pytest.fail("--all released a worktree"))
        rc, out, err = run_main(capsys, "--repo-root", str(fx.repo), "--all", "--apply")
        assert rc == 0
        assert fx.dispatch.exists() and fx.gate.exists() and fx.bench_tree.exists()

    def test_git_failure_exits_2_with_git_stderr_and_no_stdout(self, tmp_path, capsys):
        plain = tmp_path / "plain"
        plain.mkdir()
        rc, out, err = run_main(capsys, "--repo-root", str(plain), "--all")
        assert rc == 2 and out == "" and "not a git repository" in err


class TestExistingDryRunUnchanged:
    def test_json_report_keeps_its_shape_and_releases_nothing(self, fx, capsys):
        rc, out, err = run_main(capsys, "--repo-root", str(fx.repo), "--json")
        assert rc == 0
        payload = json.loads(out)
        assert set(payload) == {"repo_root", "candidates"}
        assert len(payload["candidates"]) == 1
        candidate = payload["candidates"][0]
        assert set(candidate) == {
            "dispatch_id", "path", "branch", "head_sha", "lock_reason",
            "safe_to_release", "reason", "released", "errors", "delete_branch",
        }
        assert candidate["dispatch_id"] == "d1"
        assert candidate["safe_to_release"] and candidate["released"] is False
        assert fx.dispatch.exists()

    def test_human_dry_run_summary_is_unchanged(self, fx, capsys):
        rc, out, err = run_main(capsys, "--repo-root", str(fx.repo))
        assert rc == 0
        assert out.splitlines()[0].startswith(
            "cleanup_reviewed_worktrees: 1 locked dispatch-worktree(s) gevonden"
        )
        assert "SAFE_TO_RELEASE" in out and "geen --apply meegegeven" in out
        assert fx.dispatch.exists()
