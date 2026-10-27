#!/usr/bin/env python3
"""No scaffolder writes a plan file any more (ADR-047, B5).

``new-worktree`` scaffolded ``FEATURE_PLAN.md`` and ``PR_QUEUE.md`` into every
new worktree and ``vnx init`` wrote ``FEATURE_PLAN.md`` into a new project.
The tracks DB is the only plan source (ADR-047 §2), so both paths now write
nothing and a fresh tree holds only what git tracks.

Both tests drive the real code: the shell test sources
``scripts/commands/new_worktree.sh`` and calls ``cmd_new_worktree`` with the
``bin/vnx`` command functions stubbed (worktree-start, bootstraps, doctor) so
no real bootstrap or dispatch runs; the init test calls ``vnx_init`` itself.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "commands" / "new_worktree.sh"

sys.path.insert(0, str(REPO_ROOT))

from vnx_cli.commands import init_cmd  # noqa: E402

PLAN_FILES = ("FEATURE_PLAN.md", "PR_QUEUE.md")

# Drives the real sourced script. Only the bin/vnx command functions are
# stubbed: everything under test (argument parsing, `git worktree add`, the
# worktree layout) runs as written.
_HARNESS = r"""
set -u
log() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }
cmd_worktree_start() { mkdir -p "$VNX_DATA_DIR/state"; return 0; }
cmd_bootstrap_skills() { return 0; }
cmd_bootstrap_terminals() { return 0; }
cmd_bootstrap_hooks() { return 0; }
cmd_regen_settings() { return 0; }
cmd_doctor() { return 0; }
_load_command() { return 1; }
# shellcheck disable=SC1090
source "$NEW_WORKTREE_SCRIPT"
cmd_new_worktree "$1" --skip-doctor
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _shell_env(repo: Path, tmp_path: Path) -> dict:
    """A clean VNX_* environment: no ambient state dirs, VNX_HOME at the repo."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("VNX_")}
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update(
        HOME=str(home),
        PROJECT_ROOT=str(repo),
        VNX_HOME=str(REPO_ROOT),
        VNX_DATA_DIR=str(repo / ".vnx-data"),
        VNX_DATA_DIR_GUARD="off",
        NEW_WORKTREE_SCRIPT=str(SCRIPT),
    )
    return env


def test_new_worktree_scaffolds_no_plan_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")

    proc = subprocess.run(
        ["bash", "-c", _HARNESS, "new-worktree-test", "b5-no-plan"],
        cwd=str(repo),
        env=_shell_env(repo, tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    worktree = Path(f"{repo}-wt-b5-no-plan")
    assert worktree.is_dir(), proc.stdout + proc.stderr
    assert (worktree / "README.md").is_file()
    for name in PLAN_FILES:
        assert not (worktree / name).exists(), (
            f"new-worktree wrote {name} into the fresh worktree\n{proc.stdout}"
        )


def _init_args(project_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_path=None,
        project_dir=str(project_dir),
        project_id=None,
        template="default",
        force=False,
        non_interactive=False,
        set_version=None,
    )


def test_vnx_init_writes_no_feature_plan(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_GUARD", "off")
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    # launchd agents are a host side effect, never the thing under test.
    for name in (
        "_install_gate_obligation_runner",
        "_install_ledger_health_runner",
        "_install_receipt_processor_runner",
        "_install_cleanup_reviewed_worktrees_runner",
    ):
        monkeypatch.setattr(init_cmd, name, lambda *a, **k: False)

    project = tmp_path / "proj"
    project.mkdir()

    assert init_cmd.vnx_init(_init_args(project)) == 0

    # Positive evidence that init actually ran to completion.
    assert (project / ".vnx-version").is_file()
    assert (project / ".claude" / "settings.json").is_file()
    for name in PLAN_FILES:
        assert not (project / name).exists(), f"vnx init wrote {name}"
