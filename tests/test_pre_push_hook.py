"""Tests for D2.1 — the worker->main pre-push block (hooks/git/pre-push).

Runs the hook script as a subprocess with simulated git pre-push stdin
(`<local-ref> <local-sha> <remote-ref> <remote-sha>`) and env, asserting:
  - worker context (VNX_DISPATCH_ID) + push to main  -> blocked (exit 1)
  - worker context + push to a dispatch/* branch       -> allowed (exit 0)
  - NON-worker context + push to main                  -> allowed (Vincent unaffected)
  - VNX_OVERRIDE_WORKER_PUSH_MAIN=1 + worker + main     -> allowed (override)
  - worktree-path detection (.vnx-data/worktrees/dispatch-*) -> worker -> blocked
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "hooks" / "git" / "pre-push"

_PUSH_MAIN = "refs/heads/main abc123 refs/heads/main def456\n"
_PUSH_DISPATCH = "refs/heads/dispatch/x abc123 refs/heads/dispatch/x def456\n"


def _run(stdin: str, *, env_extra: dict | None = None, cwd: Path | None = None):
    """Run the pre-push hook with an isolated env; return the CompletedProcess."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(HOOK), "origin", "git@github.com:Vinix24/vnx-orchestration.git"],
        input=stdin,
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_hook_is_executable():
    assert HOOK.is_file()
    assert os.access(HOOK, os.X_OK), "pre-push hook must be executable"


def test_worker_push_to_main_blocked():
    r = _run(_PUSH_MAIN, env_extra={"VNX_DISPATCH_ID": "20260622-test"})
    assert r.returncode == 1
    assert "BLOCKED" in r.stderr


def test_worker_push_to_dispatch_branch_allowed():
    r = _run(_PUSH_DISPATCH, env_extra={"VNX_DISPATCH_ID": "20260622-test"})
    assert r.returncode == 0


def test_non_worker_push_to_main_allowed(tmp_path):
    # No VNX_DISPATCH_ID, cwd is not a dispatch worktree -> not a worker -> allow.
    r = _run(_PUSH_MAIN, cwd=tmp_path)
    assert r.returncode == 0


def test_override_flag_allows_worker_push_to_main():
    r = _run(
        _PUSH_MAIN,
        env_extra={"VNX_DISPATCH_ID": "20260622-test", "VNX_OVERRIDE_WORKER_PUSH_MAIN": "1"},
    )
    assert r.returncode == 0


def test_worktree_path_detection_blocks_main(tmp_path):
    # A git repo whose toplevel matches .vnx-data/worktrees/dispatch-* is a worker
    # context even without VNX_DISPATCH_ID.
    wt = tmp_path / ".vnx-data" / "worktrees" / "dispatch-20260622-abc"
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(wt), check=True)
    r = _run(_PUSH_MAIN, cwd=wt)
    assert r.returncode == 1
    assert "BLOCKED" in r.stderr


def test_worktree_path_detection_allows_non_main(tmp_path):
    wt = tmp_path / ".vnx-data" / "worktrees" / "dispatch-20260622-abc"
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(wt), check=True)
    r = _run(_PUSH_DISPATCH, cwd=wt)
    assert r.returncode == 0


def test_empty_stdin_allows():
    # No refs being pushed -> nothing to block.
    r = _run("", env_extra={"VNX_DISPATCH_ID": "20260622-test"})
    assert r.returncode == 0


def test_head_colon_main_refspec_blocked():
    # `git push origin HEAD:main` resolves to remote_ref refs/heads/main on stdin.
    r = _run(
        "refs/heads/dispatch/x abc123 refs/heads/main def456\n",
        env_extra={"VNX_DISPATCH_ID": "20260622-test"},
    )
    assert r.returncode == 1
