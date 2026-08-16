"""test_dispatch_worktree_lifecycle_guards.py — OI-1232, OI-1149, OI-1061.

Three gaps in ``dispatch_worktree_isolation`` left over after the OI-861
identity-claim system shipped:

1. OI-1232: the OI-861 claim system refuses two DIFFERENT dispatch ids
   racing for one worktree slot, but says nothing about the SAME dispatch id
   claimed twice by two LIVE processes at once — that idempotent re-entry
   silently hands the same worktree to a sibling still using it. Measured
   2026-08-15: three dispatches collided in ONE worktree and a sibling's
   git operation reset another's branch pointer, wiping committed files from
   disk. Fixed by an occupancy lock (see ``_acquire_occupancy`` in the
   module) held for the dispatch's full create -> ... -> remove lifetime.

2. OI-1149/OI-1061 (unpushed commits): a worktree always branches from
   ``origin/main`` by default, silently ignoring local commits the operator
   has not pushed. Fixed by ``_check_unpushed_local_commits``, which warns
   loudly (stderr + log) and stamps the divergence on the claim.

3. OI-1149/OI-1061 (fabric version): the ``~/.vnx-system/current`` symlink
   is never frozen at worktree-creation time, so a ``vnx update`` mid-dispatch
   moves the fabric under a running worker silently. Fixed by freezing the
   marker into the claim at creation and comparing it at teardown.

Test (1) is a REAL cross-process test (subprocess.Popen, not threads): the
occupancy lock is per-open-file-description, so a same-process race would
not exercise it the way two independent OS processes do — which is exactly
the OI-1232 shape (two independent dispatch attempts, not two threads of one
attempt). ``WorktreeOccupied`` does not exist on the pre-fix module, so this
test fails at import/collection time against old code — the required
"fails on old code" property.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))


def _set_shared_claim_dir(monkeypatch, tmp_path: Path) -> Path:
    """Point the claim registry at a SHARED temp state root (mirrors
    test_dispatch_worktree_identity_race.py's fixture)."""
    data_dir = tmp_path / "shared-data"
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    return data_dir


def _init_git_repo(tmp_path: Path) -> Path:
    """A real git repo on branch main with one committed seed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "lifecycle-test@vnx"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "VNX Lifecycle Test"], check=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)
    return repo


def _init_git_repo_with_origin(tmp_path: Path) -> Path:
    """Bare origin + local clone with an initial commit, pushed to origin.

    Mirrors the fixture in test_provider_dispatch_worktree_isolation.py /
    test_tmux_worktree.py.
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(bare), str(local)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "checkout", "-b", "main"], capture_output=True)
    subprocess.run(["git", "-C", str(local), "config", "user.email", "test@test.local"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "config", "user.name", "Test"], check=True, capture_output=True)
    readme = local / "README.md"
    readme.write_text("init\n")
    subprocess.run(["git", "-C", str(local), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "commit", "-m", "initial"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return local


# ─── (1) OI-1232: occupancy across two REAL processes ───────────────────────

_WORKER_TEMPLATE = """
import sys, os, time
sys.path.insert(0, {scripts_lib!r})
from pathlib import Path
from dispatch_worktree_isolation import create_dispatch_worktree

ready_marker = {ready_marker!r}
release_marker = {release_marker!r}
result_marker = {result_marker!r}

try:
    path = create_dispatch_worktree({dispatch_id!r}, project_root=Path({repo!r}))
    with open(result_marker, "w", encoding="utf-8") as f:
        f.write("ok:" + str(path))
except Exception as exc:  # noqa: BLE001
    with open(result_marker, "w", encoding="utf-8") as f:
        f.write("err:" + repr(exc))

with open(ready_marker, "w", encoding="utf-8") as f:
    f.write("ready")

deadline = time.time() + 30
while not os.path.exists(release_marker) and time.time() < deadline:
    time.sleep(0.05)
"""


class TestOccupancyAcrossProcesses:
    def test_sibling_process_cannot_reclaim_a_live_worktree(self, tmp_path, monkeypatch):
        """A SECOND live process double-firing the SAME dispatch id must be
        refused, not silently handed the first process's still-in-use worktree.

        This is the literal OI-1232 shape: on the OLD code, both processes
        would succeed and BOTH would consider themselves the sole occupant of
        the same worktree/branch — exactly what let a sibling's git operation
        reset another's branch pointer and wipe committed files.
        """
        from dispatch_worktree_isolation import (
            WorktreeOccupied,
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")
        dispatch_id = "20260816-p7-occupancy-race"

        ready_marker = tmp_path / "ready.marker"
        release_marker = tmp_path / "release.marker"
        result_marker = tmp_path / "result.marker"

        script = tmp_path / "sibling_worker.py"
        script.write_text(
            _WORKER_TEMPLATE.format(
                scripts_lib=str(SCRIPTS_LIB),
                dispatch_id=dispatch_id,
                repo=str(repo),
                ready_marker=str(ready_marker),
                release_marker=str(release_marker),
                result_marker=str(result_marker),
            ),
            encoding="utf-8",
        )

        proc = subprocess.Popen([sys.executable, str(script)])
        try:
            deadline = time.time() + 30
            while not ready_marker.exists() and time.time() < deadline:
                time.sleep(0.05)
            assert ready_marker.exists(), "sibling process never signalled ready"

            result = result_marker.read_text(encoding="utf-8")
            assert result.startswith("ok:"), f"sibling process failed to create its worktree: {result}"

            # The sibling is still alive and holding the worktree — a SECOND,
            # genuinely different process (this test process) claiming the
            # SAME dispatch id must be refused loudly, never silently handed
            # the same path.
            with pytest.raises(WorktreeOccupied, match=dispatch_id):
                create_dispatch_worktree(dispatch_id, project_root=repo)
        finally:
            release_marker.write_text("release", encoding="utf-8")
            proc.wait(timeout=30)

        # After the sibling exits, the kernel releases its flock immediately
        # (crash-safety property) — a fresh claim from this process succeeds
        # again and a normal teardown still works.
        wt = create_dispatch_worktree(dispatch_id, project_root=repo)
        assert wt.exists()
        remove_dispatch_worktree(dispatch_id, project_root=repo)

    def test_dead_sibling_releases_the_lock_automatically(self, tmp_path, monkeypatch):
        """A crashed (SIGKILLed) holder's lock is freed by the kernel at exit —
        no manual cleanup, no stale-lock timeout logic required.
        """
        from dispatch_worktree_isolation import create_dispatch_worktree

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")
        dispatch_id = "20260816-p7-occupancy-crash"

        ready_marker = tmp_path / "ready2.marker"
        release_marker = tmp_path / "release2.marker"
        result_marker = tmp_path / "result2.marker"

        script = tmp_path / "sibling_worker_crash.py"
        script.write_text(
            _WORKER_TEMPLATE.format(
                scripts_lib=str(SCRIPTS_LIB),
                dispatch_id=dispatch_id,
                repo=str(repo),
                ready_marker=str(ready_marker),
                release_marker=str(release_marker),
                result_marker=str(result_marker),
            ),
            encoding="utf-8",
        )

        proc = subprocess.Popen([sys.executable, str(script)])
        deadline = time.time() + 30
        while not ready_marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready_marker.exists()
        assert result_marker.read_text(encoding="utf-8").startswith("ok:")

        # Simulate a crash: kill -9 instead of releasing gracefully.
        proc.kill()
        proc.wait(timeout=30)

        # No manual unlock happened — the kernel released it on process exit.
        # A fresh claim for the same dispatch id must succeed immediately.
        wt = create_dispatch_worktree(dispatch_id, project_root=repo)
        assert wt.exists()


# ─── (2) OI-1149/OI-1061: unpushed local commits ────────────────────────────

class TestUnpushedLocalCommitsWarning:
    def test_warns_and_stamps_claim_when_local_main_is_ahead(self, tmp_path, monkeypatch, capsys):
        """Local main ahead of origin/main: loud stderr warning + claim field.

        Old code silently branches the worktree from origin/main with no
        signal at all that local commits exist and are missing from it.
        """
        from dispatch_worktree_isolation import create_dispatch_worktree, _read_claim

        _set_shared_claim_dir(monkeypatch, tmp_path)
        local = _init_git_repo_with_origin(tmp_path)

        (local / "unpushed.txt").write_text("wip\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(local), "add", "unpushed.txt"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(local), "commit", "-m", "wip: not pushed yet"],
            check=True, capture_output=True,
        )

        dispatch_id = "20260816-p8-unpushed-warn"
        create_dispatch_worktree(dispatch_id, project_root=local)

        captured = capsys.readouterr()
        assert "OI-1149/OI-1061" in captured.err
        assert "1 commit(s) ahead" in captured.err

        claim = _read_claim(dispatch_id, local)
        assert claim["unpushed_local_commits"] is not None
        assert claim["unpushed_local_commits"]["ahead_count"] == 1
        assert claim["unpushed_local_commits"]["local_branch"] == "main"

    def test_unpushed_warning_lands_on_the_teardown_receipt(self, tmp_path, monkeypatch):
        """The divergence recorded at creation survives into the teardown event."""
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        _set_shared_claim_dir(monkeypatch, tmp_path)
        local = _init_git_repo_with_origin(tmp_path)
        (local / "unpushed2.txt").write_text("wip2\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(local), "add", "unpushed2.txt"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(local), "commit", "-m", "wip: also not pushed"],
            check=True, capture_output=True,
        )

        dispatch_id = "20260816-p8-unpushed-receipt"
        create_dispatch_worktree(dispatch_id, project_root=local)

        mock_store = MagicMock()
        mock_store.append = MagicMock()
        with patch("event_store.EventStore", return_value=mock_store):
            remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        teardown_event = next(
            c[0][1] for c in mock_store.append.call_args_list
            if c[0][1]["type"] == "provider_teardown_worktree"
        )
        recorded = teardown_event["data"]["unpushed_local_commits_at_create"]
        assert recorded is not None
        assert recorded["ahead_count"] == 1

    def test_no_warning_when_local_matches_origin(self, tmp_path, monkeypatch, capsys):
        """Local main == origin/main: nothing to warn about."""
        from dispatch_worktree_isolation import create_dispatch_worktree, _read_claim

        _set_shared_claim_dir(monkeypatch, tmp_path)
        local = _init_git_repo_with_origin(tmp_path)

        dispatch_id = "20260816-p8-no-unpushed"
        create_dispatch_worktree(dispatch_id, project_root=local)

        captured = capsys.readouterr()
        assert "OI-1149/OI-1061" not in captured.err

        claim = _read_claim(dispatch_id, local)
        assert claim["unpushed_local_commits"] is None


# ─── (3) OI-1149/OI-1061: fabric version freeze + drift detection ──────────

class TestFabricVersionFreeze:
    def _fake_central_install(self, tmp_path: Path, version: str) -> Path:
        root = tmp_path / "fake-vnx-system"
        (root / "versions" / version).mkdir(parents=True)
        (root / "current").symlink_to(root / "versions" / version)
        return root

    def test_fabric_version_is_frozen_at_creation(self, tmp_path, monkeypatch):
        import dispatch_worktree_isolation as dwi
        from dispatch_worktree_isolation import create_dispatch_worktree, _read_claim

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")

        fake_root = self._fake_central_install(tmp_path, "v9.9.9")
        monkeypatch.setattr(dwi, "_CENTRAL_INSTALL_ROOT", fake_root)

        dispatch_id = "20260816-p8-fabric-freeze"
        create_dispatch_worktree(dispatch_id, project_root=repo)

        claim = _read_claim(dispatch_id, repo)
        assert claim["fabric_version"] == "v9.9.9"

    def test_fabric_version_drift_is_detected_and_surfaced_at_teardown(
        self, tmp_path, monkeypatch, caplog
    ):
        """A `vnx update` that moves ~/.vnx-system/current mid-dispatch must be
        visible at teardown, not silently absorbed.
        """
        import dispatch_worktree_isolation as dwi
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")

        fake_root = self._fake_central_install(tmp_path, "v9.9.9")
        monkeypatch.setattr(dwi, "_CENTRAL_INSTALL_ROOT", fake_root)

        dispatch_id = "20260816-p8-fabric-drift"
        create_dispatch_worktree(dispatch_id, project_root=repo)

        # Simulate `vnx update`: the symlink now points at a NEW version.
        (fake_root / "versions" / "v9.9.10").mkdir(parents=True)
        (fake_root / "current").unlink()
        (fake_root / "current").symlink_to(fake_root / "versions" / "v9.9.10")

        mock_store = MagicMock()
        mock_store.append = MagicMock()
        with caplog.at_level(logging.WARNING, logger="dispatch_worktree_isolation"):
            with patch("event_store.EventStore", return_value=mock_store):
                remove_dispatch_worktree(dispatch_id, project_root=repo, terminal_id="T1")

        assert "FABRIC VERSION DRIFT" in caplog.text
        assert "v9.9.9" in caplog.text
        assert "v9.9.10" in caplog.text

        teardown_event = next(
            c[0][1] for c in mock_store.append.call_args_list
            if c[0][1]["type"] == "provider_teardown_worktree"
        )
        assert teardown_event["data"]["fabric_version_at_create"] == "v9.9.9"
        assert teardown_event["data"]["fabric_version_at_teardown"] == "v9.9.10"

    def test_no_drift_when_fabric_version_is_stable(self, tmp_path, monkeypatch, caplog):
        import dispatch_worktree_isolation as dwi
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")

        fake_root = self._fake_central_install(tmp_path, "v9.9.9")
        monkeypatch.setattr(dwi, "_CENTRAL_INSTALL_ROOT", fake_root)

        dispatch_id = "20260816-p8-fabric-stable"
        create_dispatch_worktree(dispatch_id, project_root=repo)

        with caplog.at_level(logging.WARNING, logger="dispatch_worktree_isolation"):
            remove_dispatch_worktree(dispatch_id, project_root=repo)

        assert "FABRIC VERSION DRIFT" not in caplog.text
