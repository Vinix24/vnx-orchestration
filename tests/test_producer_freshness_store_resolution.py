"""OI-2042: the producer-freshness monitor must resolve the project's central store.

The launchd job runs ``cd <repo> && python scripts/producer_freshness_monitor.py``
with no VNX environment and a stripped PATH. When ``git`` cannot be started the
resolver used to conclude "not a checkout" and walk to the parent directory,
which wrote the heartbeat into a stray ``<parent>/.vnx-data``.

The tests build a throwaway checkout under a tmp HOME (parent-directory
``.vnx-data`` next to the repo, plus the central store ``~/.vnx-data/<pid>``)
and run the monitor's own state-dir resolution in a subprocess.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PROJECT_ID = "vnx-dev"

_PROBE = (
    "import sys; sys.path.insert(0, 'scripts'); "
    "import producer_freshness_monitor as m; print(m._default_state_dir())"
)


def _build_checkout(home: Path) -> tuple[Path, Path, Path]:
    parent = home / "Development"
    repo = parent / "vnx-orchestration"
    (repo / "scripts").mkdir(parents=True)
    shutil.copytree(
        REPO / "scripts" / "lib", repo / "scripts" / "lib",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in ("producer_freshness_monitor.py", "cli_output.py"):
        shutil.copy(REPO / "scripts" / name, repo / "scripts" / name)
    (repo / ".git").mkdir()
    (repo / ".vnx-project-id").write_text(PROJECT_ID + "\n", encoding="utf-8")
    stray = parent / ".vnx-data"
    (stray / "state").mkdir(parents=True)
    central = home / ".vnx-data" / PROJECT_ID
    (central / "state").mkdir(parents=True)
    return repo, stray, central


def _run_probe(repo: Path, home: Path, path: str) -> Path:
    env = {"HOME": str(home), "PATH": path}
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=repo, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return Path(proc.stdout.strip().splitlines()[-1]).resolve()


def test_monitor_without_git_resolves_central_store_not_parent_store(tmp_path):
    home = tmp_path.resolve() / "home"
    repo, stray, central = _build_checkout(home)

    state_dir = _run_probe(repo, home, path=str(tmp_path / "no-bin"))

    assert state_dir == (central / "state").resolve()
    assert stray.resolve() not in state_dir.parents


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_monitor_with_git_on_path_resolves_central_store(tmp_path):
    home = tmp_path.resolve() / "home"
    repo, stray, central = _build_checkout(home)
    (repo / ".git").rmdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    state_dir = _run_probe(repo, home, path=os.path.dirname(shutil.which("git")))

    assert state_dir == (central / "state").resolve()


def test_checkout_root_returns_none_for_plain_directory_without_git(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO / "scripts" / "lib"))
    try:
        import vnx_paths
    finally:
        sys.path.pop(0)
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert vnx_paths._checkout_root(plain) is None
    (plain / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    assert vnx_paths._checkout_root(plain) == plain.resolve()
