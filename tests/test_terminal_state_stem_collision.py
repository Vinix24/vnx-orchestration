#!/usr/bin/env python3
"""Stem collision between the terminal_state_shadow CLI wrapper and its library (OI-1976).

scripts/terminal_state_shadow.py (CLI wrapper, called by path from shell libs and
consumer hooks) used to share its stem with the library that now lives in
scripts/lib/terminal_state_core.py. With scripts/ ahead of scripts/lib/ on sys.path
a bare ``from terminal_state_shadow import SCHEMA_VERSION`` resolved to the wrapper
and the t0 state builder reported ``source: error`` for T1-T3.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
WRAPPER = SCRIPTS_DIR / "terminal_state_shadow.py"

LIBRARY_STEM = "terminal_state_core"
WRAPPER_STEM = "terminal_state_shadow"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_terminal_state(state_dir: Path) -> None:
    now = _now_iso()
    terminals = {
        tid: {
            "terminal_id": tid,
            "status": "idle",
            "claimed_by": None,
            "claimed_at": None,
            "lease_expires_at": None,
            "last_activity": now,
            "version": 1,
        }
        for tid in ("T1", "T2", "T3")
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "terminal_state.json").write_text(
        json.dumps({"schema_version": 1, "terminals": terminals}), encoding="utf-8"
    )


def _isolated_env(tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_") and k != "PYTHONPATH"}
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    return env


_BUILDER_SNIPPET = """
import json, sys
from pathlib import Path
sys.path.insert(0, {lib!r})
sys.path.insert(0, {scripts!r})   # scripts/ BEFORE scripts/lib/
assert sys.path.index({scripts!r}) < sys.path.index({lib!r})
import build_t0_state
print(json.dumps(build_t0_state._build_terminals(Path({state!r}))))
"""


def test_builder_terminals_resolve_with_scripts_before_lib(tmp_path):
    state_dir = tmp_path / "state"
    _seed_terminal_state(state_dir)

    snippet = _BUILDER_SNIPPET.format(lib=str(LIB_DIR), scripts=str(SCRIPTS_DIR), state=str(state_dir))
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        env=_isolated_env(tmp_path),
        cwd=str(tmp_path),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    terminals = json.loads(proc.stdout.strip().splitlines()[-1])

    for tid in ("T1", "T2", "T3"):
        entry = terminals[tid]
        assert entry["source"] != "error", entry
        assert "ImportError" not in str(entry.get("error", "")), entry
        assert entry["status"] == "idle", entry
        assert entry["source"] == "terminal_state", entry


def _stem_files(stem: str) -> list[Path]:
    return sorted(
        p
        for p in SCRIPTS_DIR.rglob(f"{stem}.py")
        if "__pycache__" not in p.parts
    )


@pytest.mark.parametrize("stem", [LIBRARY_STEM, WRAPPER_STEM])
def test_terminal_state_stems_are_unique_under_scripts(stem):
    found = _stem_files(stem)
    assert len(found) == 1, f"stem {stem!r} is shared by {[str(p.relative_to(REPO_ROOT)) for p in found]}"


def test_wrapper_and_library_live_where_the_callers_expect_them():
    assert _stem_files(WRAPPER_STEM) == [WRAPPER]
    assert _stem_files(LIBRARY_STEM) == [LIB_DIR / f"{LIBRARY_STEM}.py"]


def _run_wrapper(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = _isolated_env(tmp_path)
    env["VNX_DATA_DIR"] = str(tmp_path / "data")
    env["VNX_STATE_DIR"] = str(tmp_path / "data" / "state")
    return subprocess.run(
        [sys.executable, str(WRAPPER), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=60,
    )


def test_wrapper_set_and_get_worktree(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    wt = str(tmp_path / "worktree-T1")

    set_proc = _run_wrapper(tmp_path, "set-worktree", "T1", wt, "--state-dir", str(state_dir))
    assert set_proc.returncode == 0, set_proc.stderr
    assert json.loads(set_proc.stdout) == {"terminal_id": "T1", "worktree_path": wt}

    get_proc = _run_wrapper(tmp_path, "get-worktree", "T1", "--state-dir", str(state_dir))
    assert get_proc.returncode == 0, get_proc.stderr
    assert get_proc.stdout.strip() == wt


def test_wrapper_status_write_path(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    proc = _run_wrapper(
        tmp_path,
        "--terminal-id", "T2",
        "--status", "claimed",
        "--claimed-by", "dispatch-x",
        "--lease-seconds", "60",
        "--state-dir", str(state_dir),
    )
    assert proc.returncode == 0, proc.stderr
    record = json.loads(proc.stdout)
    assert record["terminal_id"] == "T2"
    assert record["status"] == "claimed"

    doc = json.loads((state_dir / "terminal_state.json").read_text(encoding="utf-8"))
    assert doc["terminals"]["T2"]["claimed_by"] == "dispatch-x"
    assert doc["terminals"]["T2"]["lease_expires_at"]


def test_wrapper_get_worktree_requires_terminal_id(tmp_path):
    proc = _run_wrapper(tmp_path, "get-worktree")
    assert proc.returncode == 1
    assert "Usage" in proc.stderr
