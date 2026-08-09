#!/usr/bin/env python3
"""tests/test_path_parity_hook.py — path_parity_check.sh central-store resolution.

Defect A (OI-852/857-adjacent, "the hook splits the store"): the old hook
resolved ``STATE_DIR`` as ``${VNX_STATE_DIR:-$ROOT/.vnx-data/state}`` — a
repo-local fallback, not the central per-project store (ADR-026). In a normal
session VNX_STATE_DIR is unset, so the old hook silently wrote a second,
diverging copy of ``path_parity.json`` next to the repo instead of the one
every other reader expects at ``~/.vnx-data/<project>/state/``.

These tests run the real hook (bash + the real vnx_paths resolver) against
this real repo checkout — a synthetic fake repo would have no real project
identity to resolve against and would prove nothing about the actual bug.
Isolation from the REAL central store (``~/.vnx-data/vnx-dev``) is via
``VNX_DATA_HOME`` redirected at a tmp_path, not via VNX_DATA_DIR_EXPLICIT:
the whole point is exercising the same "VNX_STATE_DIR/VNX_DATA_DIR unset"
code path production hits, which VNX_DATA_DIR_EXPLICIT would short-circuit
before it ever ran.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "hooks" / "path_parity_check.sh"
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import vnx_paths  # noqa: E402

_HOOK_BUDGET_SECONDS = 5.0


def _run_hook(env: dict) -> subprocess.CompletedProcess:
    start = time.monotonic()
    result = subprocess.run(
        ["bash", str(HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=env,
        timeout=10,
    )
    elapsed = time.monotonic() - start
    assert elapsed < _HOOK_BUDGET_SECONDS, f"SessionStart budget exceeded: {elapsed:.2f}s"
    assert result.returncode == 0, (
        f"hook must always exit 0 (fail-soft contract): "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result


def test_hook_resolves_central_state_dir_without_vnx_state_dir_set(tmp_path, monkeypatch):
    """The exact defect: VNX_STATE_DIR unset (the normal-session condition)
    must resolve to the SAME central path the canonical Python resolver
    (vnx_paths.resolve_paths) returns — never the repo-local fallback."""
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    isolated_home = tmp_path / "vnx-data-home"
    monkeypatch.setenv("VNX_DATA_HOME", str(isolated_home))

    expected_state_dir = Path(vnx_paths.resolve_paths()["VNX_STATE_DIR"])
    repo_local_fallback = REPO / ".vnx-data" / "state"
    assert expected_state_dir != repo_local_fallback, (
        "central resolver coincides with the repo-local fallback on this "
        "machine -- this test cannot distinguish the fix from the old bug"
    )

    env = dict(os.environ)
    _run_hook(env)

    out_path = expected_state_dir / "path_parity.json"
    assert out_path.is_file(), f"expected the central write at {out_path}, found nothing there"
    assert not (repo_local_fallback / "path_parity.json").exists(), (
        "hook wrote the OLD repo-local split-brain path as well as (or instead "
        "of) the central one"
    )
    data = json.loads(out_path.read_text())
    assert "parity" in data
    assert "consumer_scan" in data


def test_hook_honors_explicit_vnx_state_dir(tmp_path, monkeypatch):
    """An explicit VNX_STATE_DIR override (e.g. a dispatch worker's own
    central per-project pin) must still be honored — the central-resolver
    fix must not steamroll a legitimate explicit override."""
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

    env = dict(os.environ)
    _run_hook(env)

    out_path = tmp_path / "path_parity.json"
    assert out_path.is_file()
    data = json.loads(out_path.read_text())
    assert "parity" in data


def test_hook_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
