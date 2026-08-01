"""test_dispatch_worktree_identity_race.py — OI-861 dispatch-identity crossing.

Two dispatches fired back-to-back on the same lane must never share a worktree
or inherit each other's identity.  This exercises the dispatch-id-keyed atomic
claim in dispatch_worktree_isolation:

  1. Concurrent claims on the SAME worktree slot (two dispatch ids that
     sanitize to the same safe id): exactly one wins, the loser gets a clean
     WorktreeIdentityConflict — never a silent shared worktree.
  2. The claim REGISTRY lives under the canonical state root (ADR-026 SSOT),
     NOT under any repo root.  A repo-local pin forks the map exactly as far
     apart as the racing worktrees are, and the OI-861 crossing runs straight
     through it (PR #1274 review).  test_claim_dir_is_not_under_repo_root and
     test_claims_from_different_project_roots_share_one_map guard this.
  3. Hard refusal: a worker offered a worktree stamped for a different dispatch
     id gets a fail-loud WorktreeIdentityConflict via verify_worktree_identity.
  4. Teardown refusal: remove_dispatch_worktree refuses to reap a worktree it
     does not own.
  5. Same-dispatch re-entry is idempotent; remove clears the claim so the slot
     can be claimed again.

The concurrency is REAL: every iteration spawns two threads behind a barrier
that both fire create_dispatch_worktree at the same instant on the same slot.
A retry loop that re-ran the same call N times would re-hit the same serialized
window and prove nothing; this test exercises genuine simultaneous claims.

Every test pins the claim registry to a shared temp state root
(VNX_DATA_DIR_EXPLICIT=1 + VNX_DATA_DIR + VNX_STATE_DIR) so the ambient
central store (~/.vnx-data/...) is never written and two simulated worktrees
serialize on one map.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))


def _set_shared_claim_dir(monkeypatch, tmp_path: Path) -> Path:
    """Point the claim registry at a SHARED temp state root.

    ``_claim_dir`` resolves via ``vnx_paths.resolve_paths()["VNX_STATE_DIR"]``
    (the canonical state root), so the test pins both ``VNX_DATA_DIR``
    (explicit override) and ``VNX_STATE_DIR`` to one temp dir that every
    simulated "worktree" resolves to.  Without this, tests would either write
    to the ambient central store or fork the map per checkout — the exact
    defect OI-861 is about.
    """
    data_dir = tmp_path / "shared-data"
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    return data_dir


def _init_git_repo(tmp_path: Path) -> Path:
    """Create a real git repo on branch main with one committed seed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "race-test@vnx"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "VNX Race Test"], check=True
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True
    )
    return repo


def _colliding_dispatch_ids(index: int) -> "tuple[str, str]":
    """Two dispatch ids that sanitize to the SAME safe id.

    ``dispatch_worktree_isolation._sanitize_dispatch_id`` collapses ``.`` to
    ``-``, so ``-lock.reclaim.timeout`` and ``-lock-reclaim-timeout`` both land
    on ``-lock-reclaim-timeout`` → the same worktree path and branch.  This is
    the narrowest way to make two *different* dispatch identities contend for
    one slot, which is exactly the OI-861 crossing.
    """
    stem = f"20260730-race{index}"
    return f"{stem}-lock-reclaim-timeout", f"{stem}.lock.reclaim.timeout"


def _run_concurrent_claim(
    repo_a: Path,
    repo_b: Path,
    id_a: str,
    id_b: str,
) -> "tuple[dict, dict]":
    """Fire two create_dispatch_worktree calls simultaneously.

    The two calls run against DIFFERENT project roots (``repo_a`` / ``repo_b``)
    — simulating the main checkout and a dispatch worktree — but must collide on
    the SAME shared claim map.  Both threads wait on a barrier so they enter at
    the same instant.  Returns (outcome_a, outcome_b) where each is
    {"status": "ok", "path": Path} or {"status": "err", "exc": Exception}.
    """
    from dispatch_worktree_isolation import create_dispatch_worktree

    barrier = threading.Barrier(2)
    outcomes: dict[str, dict] = {}

    def _claim(dispatch_id: str, repo: Path, key: str) -> None:
        try:
            barrier.wait(timeout=30)
            path = create_dispatch_worktree(dispatch_id, project_root=repo)
            outcomes[key] = {"status": "ok", "path": path}
        except Exception as exc:  # noqa: BLE001 — the test must observe the raw error
            outcomes[key] = {"status": "err", "exc": exc}

    threads = [
        threading.Thread(target=_claim, args=(id_a, repo_a, "a")),
        threading.Thread(target=_claim, args=(id_b, repo_b, "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    assert not any(t.is_alive() for t in threads), "claim thread hung"
    return outcomes["a"], outcomes["b"]


# ─── the claim registry lives in the canonical state root (PR #1274 review) ─

class TestClaimRegistryLocation:
    def test_claim_dir_is_not_under_repo_root(self, tmp_path, monkeypatch):
        """The claim map must NEVER be built under a repo root.

        This is the PR #1274 rejection point: the repo-local
        ``<repo>/.vnx-data/state/dispatch_worktree_claims`` fork serializes
        nothing between two racing checkouts.  If someone regresses to a
        repo-local pin, this assertion fails red.
        """
        data_dir = _set_shared_claim_dir(monkeypatch, tmp_path)
        from dispatch_worktree_isolation import _claim_dir

        repo = tmp_path / "repo"
        repo.mkdir()
        claim = _claim_dir(repo).resolve()

        assert not claim.is_relative_to(repo.resolve()), (
            f"claim registry {claim} must not live under the repo root {repo}"
        )
        assert claim == (data_dir / "state" / "dispatch_worktree_claims"), (
            f"claim registry {claim} must live under the canonical state root "
            f"(VNX_STATE_DIR), got a different path"
        )

    def test_claims_from_different_project_roots_share_one_map(
        self, tmp_path, monkeypatch
    ):
        """Two simultaneous claims from DIFFERENT worktrees see each other.

        Two project roots (main checkout vs dispatch worktree) resolve to the
        SAME shared claim map and serialize on it — exactly the property the
        repo-local pin broke.  On a repo-local implementation both threads
        would write distinct per-checkout claim files and BOTH would succeed;
        this test requires exactly one winner.
        """
        _set_shared_claim_dir(monkeypatch, tmp_path)
        from dispatch_worktree_isolation import (
            WorktreeClaimError,
            _write_claim_atomic,
        )

        # Two distinct project roots simulating main checkout + worktree.
        root_a = tmp_path / "wt_a"
        root_b = tmp_path / "wt_b"
        wt_path_a = root_a / ".vnx-data" / "worktrees" / "dispatch-same"
        wt_path_b = root_b / ".vnx-data" / "worktrees" / "dispatch-same"
        wt_path_a.parent.mkdir(parents=True)
        wt_path_b.parent.mkdir(parents=True)

        id_a, id_b = _colliding_dispatch_ids(99)  # same safe id, different identity
        barrier = threading.Barrier(2)
        outcomes: dict[str, dict] = {}

        def _claim(dispatch_id: str, wt_path: Path, root: Path, key: str) -> None:
            try:
                barrier.wait(timeout=30)
                _write_claim_atomic(
                    dispatch_id, worktree_path=wt_path, project_root=root
                )
                outcomes[key] = {"status": "ok"}
            except Exception as exc:  # noqa: BLE001
                outcomes[key] = {"status": "err", "exc": exc}

        threads = [
            threading.Thread(target=_claim, args=(id_a, wt_path_a, root_a, "a")),
            threading.Thread(target=_claim, args=(id_b, wt_path_b, root_b, "b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)
        assert not any(t.is_alive() for t in threads), "claim thread hung"

        ok = [k for k in ("a", "b") if outcomes[k]["status"] == "ok"]
        err = [k for k in ("a", "b") if outcomes[k]["status"] == "err"]
        assert len(ok) == 1 and len(err) == 1, (
            f"expected exactly one winner across two worktrees, got "
            f"ok={len(ok)} err={len(err)} "
            f"(a={outcomes['a']['status']}, b={outcomes['b']['status']})"
        )
        loser_exc = outcomes[err[0]]["exc"]
        assert isinstance(loser_exc, WorktreeClaimError), (
            f"loser must fail with the claim error family, got "
            f"{type(loser_exc).__name__}: {loser_exc}"
        )
        # The winner's claim must be readable from the OTHER worktree's context.
        from dispatch_worktree_isolation import _read_claim

        winner_id = id_a if ok[0] == "a" else id_b
        winner_root = root_a if ok[0] == "a" else root_b
        claim = _read_claim(winner_id, winner_root)
        assert claim is not None
        assert claim["dispatch_id"] == winner_id


# ─── the race: two concurrent claims on one slot ────────────────────────────

class TestConcurrentClaimRace:
    def test_exactly_one_winner_per_slot(self, tmp_path, monkeypatch):
        """Concurrent same-slot claims: one winner, one clean identity conflict.

        Repeated across 12 fresh slots so the outcome is shown to be stable,
        not a one-off — every iteration is a genuine barrier-synchronized pair
        of claims fired from two different project roots.
        """
        from dispatch_worktree_isolation import (
            WorktreeIdentityConflict,
            verify_worktree_identity,
        )

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")

        for i in range(12):
            id_a, id_b = _colliding_dispatch_ids(i)
            outcome_a, outcome_b = _run_concurrent_claim(repo, repo, id_a, id_b)

            ok = [o for o in (outcome_a, outcome_b) if o["status"] == "ok"]
            err = [o for o in (outcome_a, outcome_b) if o["status"] == "err"]
            assert len(ok) == 1, (
                f"iteration {i}: expected exactly one winner, got {len(ok)} "
                f"(a={outcome_a['status']}, b={outcome_b['status']})"
            )
            assert len(err) == 1, (
                f"iteration {i}: expected exactly one loser, got {len(err)}"
            )

            loser_exc = err[0]["exc"]
            assert isinstance(loser_exc, WorktreeIdentityConflict), (
                f"iteration {i}: loser must fail with WorktreeIdentityConflict, "
                f"got {type(loser_exc).__name__}: {loser_exc}"
            )
            assert "claimed by dispatch" in str(loser_exc), (
                f"iteration {i}: loser error must name the conflict, got: {loser_exc}"
            )

            winner_id = id_a if outcome_a["status"] == "ok" else id_b
            loser_id = id_b if outcome_a["status"] == "ok" else id_a
            winner_path = ok[0]["path"]

            # The winner's worktree is provably the winner's...
            claim = verify_worktree_identity(winner_id, winner_path)
            assert claim["dispatch_id"] == winner_id
            # ...and the loser is hard-refused on the very same worktree.
            with pytest.raises(WorktreeIdentityConflict, match="stamped for dispatch"):
                verify_worktree_identity(loser_id, winner_path)

    def test_same_dispatch_second_create_is_idempotent(self, tmp_path, monkeypatch):
        """Double-fire of the SAME dispatch id reuses its own claimed worktree."""
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            verify_worktree_identity,
        )

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")
        dispatch_id = "20260730-sfp1b-lock-reclaim-timeout"

        first = create_dispatch_worktree(dispatch_id, project_root=repo)
        second = create_dispatch_worktree(dispatch_id, project_root=repo)

        assert first == second
        assert first.exists()
        verify_worktree_identity(dispatch_id, first)


# ─── hard refusal: worker offered the wrong worktree ────────────────────────

class TestVerifyWorktreeIdentity:
    def test_owner_verifies(self, tmp_path, monkeypatch):
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            verify_worktree_identity,
        )

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")
        dispatch_id = "20260730-owner-dispatch"
        wt = create_dispatch_worktree(dispatch_id, project_root=repo)

        claim = verify_worktree_identity(dispatch_id, wt)
        assert claim["dispatch_id"] == dispatch_id
        assert claim["worktree_path"] == str(wt.resolve())

    def test_other_dispatch_is_rejected(self, tmp_path, monkeypatch):
        """A worker offered a worktree stamped for another dispatch fails loud.

        This is the literal OI-861 measurement: the sfp1b session was handed
        sfp2b's worktree.  With the stamp in place the sfp1b worker's identity
        check must reject it before doing any work.
        """
        from dispatch_worktree_isolation import (
            WorktreeIdentityConflict,
            create_dispatch_worktree,
            verify_worktree_identity,
        )

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")
        owner = "20260730-sfp2b-failclosed-tenant"
        intruder = "20260730-sfp1b-lock-reclaim-timeout"

        wt = create_dispatch_worktree(owner, project_root=repo)

        with pytest.raises(WorktreeIdentityConflict) as excinfo:
            verify_worktree_identity(intruder, wt)
        message = str(excinfo.value)
        assert owner in message
        assert intruder in message
        assert "stamped for dispatch" in message

    def test_unclaimed_worktree_is_rejected(self, tmp_path, monkeypatch):
        """No stamp → identity cannot be verified → hard refusal."""
        from dispatch_worktree_isolation import (
            WorktreeIdentityMissing,
            verify_worktree_identity,
        )

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        worktrees_dir = repo / ".vnx-data" / "worktrees"
        unclaimed = worktrees_dir / "dispatch-20260730-no-claim"
        unclaimed.mkdir(parents=True)

        with pytest.raises(WorktreeIdentityMissing, match="no dispatch-id claim"):
            verify_worktree_identity("20260730-no-claim", unclaimed, project_root=repo)


# ─── teardown refusal: never reap a worktree you do not own ─────────────────

class TestRemoveRefusal:
    def test_remove_refuses_other_dispatch_worktree(self, tmp_path, monkeypatch):
        """remove() must not reap a worktree stamped for a different dispatch.

        This is the "one's worktree reaped mid-flight by the other's
        completion" half of the OI-861 race.
        """
        from dispatch_worktree_isolation import (
            WorktreeIdentityConflict,
            _dispatch_worktree_dir,
            create_dispatch_worktree,
            remove_dispatch_worktree,
            verify_worktree_identity,
        )

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")
        owner = "20260730-owner-remove"
        intruder = "20260730.owner.remove"  # sanitizes identically to owner

        wt = create_dispatch_worktree(owner, project_root=repo)

        with pytest.raises(WorktreeIdentityConflict, match="already claimed by dispatch"):
            remove_dispatch_worktree(intruder, project_root=repo)

        # The worktree survives and is still verifiable by its owner.
        assert _dispatch_worktree_dir(repo, owner).exists()
        verify_worktree_identity(owner, wt)

    def test_remove_clears_claim_then_recreate(self, tmp_path, monkeypatch):
        """After a legit remove, the slot is free for a fresh claim."""
        from dispatch_worktree_isolation import (
            WorktreeIdentityMissing,
            create_dispatch_worktree,
            remove_dispatch_worktree,
            verify_worktree_identity,
        )

        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")
        dispatch_id = "20260730-recreate-cycle"

        first = create_dispatch_worktree(dispatch_id, project_root=repo)
        remove_dispatch_worktree(dispatch_id, project_root=repo)
        assert not first.exists()

        # Claim is gone → the stale path no longer verifies for anyone.
        with pytest.raises(WorktreeIdentityMissing):
            verify_worktree_identity(dispatch_id, first)

        second = create_dispatch_worktree(dispatch_id, project_root=repo)
        assert second.exists()
        verify_worktree_identity(dispatch_id, second)
