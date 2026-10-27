#!/usr/bin/env python3
"""The ``vnx roadmap`` help names no retired plan artefact.

``RoadmapManager.load_feature`` stopped materialising a plan file with ADR-047
(track ``retire-feature-plan-second-planning-layer``), and ``vnx init`` /
``new-worktree`` write none. The command's own help is the first thing an
operator reads before running the verb, so it must not promise a file the verb
no longer writes — and it must keep documenting the two verbs it has.

Measured in C-last, see ``docs/investigations/2026-10-27-retire-feature-plan-nameting.md`` §7.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "commands" / "roadmap.sh"

RETIRED_PLAN_FILES = ("FEATURE_PLAN.md", "PR_QUEUE.md", "ROADMAP.md")


def _roadmap_help() -> str:
    """The help the command prints when it is called without a verb."""
    proc = subprocess.run(
        ["bash", "-c", 'set -u; source "$1"; cmd_roadmap', "_", str(SCRIPT)],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Usage: vnx roadmap" in proc.stdout, proc.stdout + proc.stderr
    return proc.stdout


def test_roadmap_help_promises_no_retired_plan_file():
    help_text = _roadmap_help()
    for name in RETIRED_PLAN_FILES:
        assert name not in help_text, (
            f"`vnx roadmap` help still names the retired plan artefact {name}; "
            "no verb writes or reads it (ADR-047)"
        )


def test_roadmap_help_still_documents_load_and_init():
    help_text = _roadmap_help()
    # `load` stages the branch and worktree; `init` imports one named file.
    assert "load <feature_id>" in help_text
    assert "init <ROADMAP.yaml>" in help_text
