"""test_checkout_lag_gate.py — OI-1214: the door refuses a post-merge-verification
dispatch while the local main checkout lags origin/main, and logs the lag as a
number at every dispatch.

Tests the pure lag helper, the pure verdict builder, and the door integration via
build_runtime_snapshot + compile_plan. The git fixture builds a KNOWN lag entirely
with local operations (side-branch commits + ``update-ref`` of the remote-tracking
ref) — no network fetch, so every assertion is deterministic offline.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_cli import (
    _post_merge_verification_lag_verdict,
    build_runtime_snapshot,
    load_spec,
    main_checkout_lag,
)
from dispatch_plan import compile_plan
from dispatch_spec import Reject, validate


_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Local git fixtures — a real, deterministic lag with no network fetch
# ---------------------------------------------------------------------------

def _init_repo_with_origin(tmp_path: Path) -> Path:
    """Bare origin + local clone with an initial commit on ``main``.

    Returns the local clone path. Mirrors
    ``test_provider_dispatch_worktree_isolation._init_git_repo_with_origin``.
    """
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
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.email", "test@test.local"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (local / "README.md").write_text("init\n", encoding="utf-8")
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


def _init_repo_with_lag(tmp_path: Path, behind: int) -> Path:
    """Local clone whose ``origin/main`` remote-tracking ref is *behind* commits
    ahead of the checked-out ``main``.

    The new commits are created on a side branch in the SAME clone, then the
    remote-tracking ref is moved to their tip with ``update-ref`` — HEAD never
    leaves ``main``, and nothing is fetched. ``git rev-list --count HEAD..origin/main``
    then reports exactly *behind*.
    """
    local = _init_repo_with_origin(tmp_path)
    if behind <= 0:
        return local
    subprocess.run(
        ["git", "-C", str(local), "checkout", "-b", "ahead"],
        check=True, capture_output=True,
    )
    for i in range(behind):
        (local / f"ahead-{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(local), "add", f"ahead-{i}.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(local), "commit", "-m", f"ahead {i}"],
            check=True, capture_output=True,
        )
    tip = subprocess.check_output(
        ["git", "-C", str(local), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    subprocess.run(
        ["git", "-C", str(local), "update-ref", "refs/remotes/origin/main", tip],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "checkout", "main"],
        check=True, capture_output=True,
    )
    return local


def _init_repo_without_origin(tmp_path: Path) -> Path:
    """A local repo with a commit and no ``origin`` remote (lag is unresolvable)."""
    repo = tmp_path / "no-origin"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.local"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "f.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    return repo


# ---------------------------------------------------------------------------
# Bundle + snapshot helpers
# ---------------------------------------------------------------------------

def _make_bundle(
    tmp_path: Path,
    *,
    staging_id: str = "20260815-staging-lag",
    dispatch_id: str = "20260815-lag-dispatch",
    post_merge_verification: bool = False,
) -> "tuple[Path, Path]":
    """A promoted bundle (under pending/) so staging-binding passes and only the
    lag edge under test can reject. Returns (data_dir, spec_file)."""
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True)
    inst = bundle_dir / "instruction.md"
    inst.write_text(
        "# Lag gate probe\n\nRole: backend-developer\n\nVerify the merged code is live.\n",
        encoding="utf-8",
    )
    spec = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(inst),
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": "codex_gate",
        "dispatch_paths": [],
        "provider": "claude",
        "model": None,
        "deadline_seconds": 3600,
        "isolation": "worktree",
        "post_merge_verification": post_merge_verification,
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _snapshot_for_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    *,
    post_merge_verification: bool = False,
    staging_id: str = "20260815-staging-lag",
    dispatch_id: str = "20260815-lag-dispatch",
):
    """Point VNX_PROJECT_ROOT at the git fixture and build the door's snapshot."""
    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id=staging_id,
        dispatch_id=dispatch_id,
        post_merge_verification=post_merge_verification,
    )
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(project_root))
    spec = load_spec(spec_file)
    vspec = validate(spec, project_id="vnx-dev", repo_root=_REPO_ROOT)
    assert not isinstance(vspec, Reject)
    return build_runtime_snapshot(vspec, data_dir=data_dir, spec_file=spec_file)


def _lag_verdicts(snapshot):
    return [
        v for v in snapshot.constraint_verdicts
        if v.code == "post-merge-verification-stale-checkout"
    ]


# ---------------------------------------------------------------------------
# Pure lag helper
# ---------------------------------------------------------------------------

def test_main_checkout_lag_zero_when_current(tmp_path):
    """A fresh clone where origin/main == HEAD reports 0, not None."""
    local = _init_repo_with_lag(tmp_path, behind=0)
    assert main_checkout_lag(local) == 0


def test_main_checkout_lag_returns_exact_distance(tmp_path):
    """The reported number is the ACTUAL commit distance, not a flag."""
    local = _init_repo_with_lag(tmp_path, behind=3)
    assert main_checkout_lag(local) == 3


def test_main_checkout_lag_none_without_origin(tmp_path):
    """An unresolvable origin/main returns None (unknown), never a false 0."""
    repo = _init_repo_without_origin(tmp_path)
    assert main_checkout_lag(repo) is None


def test_main_checkout_lag_none_for_non_git_dir(tmp_path):
    """A non-git directory returns None rather than raising."""
    assert main_checkout_lag(tmp_path) is None


# ---------------------------------------------------------------------------
# Pure verdict builder
# ---------------------------------------------------------------------------

def test_post_merge_verification_verdict_names_consequence_and_fix():
    """The refusal names the consequence AND the fixing command, not just the fact."""
    v = _post_merge_verification_lag_verdict(3)
    assert v.code == "post-merge-verification-stale-checkout"
    assert v.severity == "blocking"
    assert "measure the old code" in v.message
    assert "false negative" in v.message
    assert "git pull --ff-only" in v.message
    assert "3 commit(s) behind origin/main" in v.message


# ---------------------------------------------------------------------------
# Door integration: the four dispatch-specified behaviors
# ---------------------------------------------------------------------------

def test_zero_lag_lets_post_merge_verification_through(tmp_path, monkeypatch):
    """Zero lag passes a verification dispatch (no blocking verdict)."""
    local = _init_repo_with_lag(tmp_path, behind=0)
    snapshot = _snapshot_for_fixture(
        tmp_path, monkeypatch, local, post_merge_verification=True,
    )
    assert _lag_verdicts(snapshot) == []


def test_lag_blocks_post_merge_verification_dispatch(tmp_path, monkeypatch):
    """A known lag > 0 emits a BLOCKING verdict for a verification dispatch."""
    local = _init_repo_with_lag(tmp_path, behind=3)
    snapshot = _snapshot_for_fixture(
        tmp_path, monkeypatch, local, post_merge_verification=True,
    )
    blocks = _lag_verdicts(snapshot)
    assert blocks and blocks[0].severity == "blocking"


def test_lag_blocks_verification_reaches_compile_plan_reject(tmp_path, monkeypatch):
    """The blocking verdict flows through compile_plan D3 into a Reject."""
    local = _init_repo_with_lag(tmp_path, behind=2)
    data_dir, spec_file = _make_bundle(tmp_path, post_merge_verification=True)
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(local))
    spec = load_spec(spec_file)
    vspec = validate(spec, project_id="vnx-dev", repo_root=_REPO_ROOT)
    assert not isinstance(vspec, Reject)
    snapshot = build_runtime_snapshot(vspec, data_dir=data_dir, spec_file=spec_file)
    plan = compile_plan(vspec, snapshot)
    assert isinstance(plan, Reject)
    assert plan.code == "post-merge-verification-stale-checkout"


def test_lag_allows_normal_build_dispatch(tmp_path, monkeypatch):
    """A normal build dispatch is NOT refused on lag (no burden — the marker is off)."""
    local = _init_repo_with_lag(tmp_path, behind=3)
    snapshot = _snapshot_for_fixture(
        tmp_path, monkeypatch, local, post_merge_verification=False,
    )
    assert _lag_verdicts(snapshot) == []


# ---------------------------------------------------------------------------
# Logging: the number, not a flag
# ---------------------------------------------------------------------------

def test_logged_number_matches_distance(tmp_path, monkeypatch, caplog):
    """The door logs the ACTUAL count ('3 commits behind'), never a boolean."""
    local = _init_repo_with_lag(tmp_path, behind=3)
    with caplog.at_level(logging.WARNING, logger="dispatch_cli"):
        _snapshot_for_fixture(
            tmp_path, monkeypatch, local, post_merge_verification=True,
        )
    assert "3 commits behind origin/main" in caplog.text
    assert "1 commits behind origin/main" not in caplog.text


def test_logged_zero_when_current(tmp_path, monkeypatch, caplog):
    """A current checkout logs '0 commits behind' — the number is always present."""
    local = _init_repo_with_lag(tmp_path, behind=0)
    with caplog.at_level(logging.INFO, logger="dispatch_cli"):
        _snapshot_for_fixture(
            tmp_path, monkeypatch, local, post_merge_verification=False,
        )
    assert "0 commits behind origin/main" in caplog.text
