#!/usr/bin/env python3
"""Tests for `vnx skills sync` — idempotent refresh of a project's skills from canonical.

Dispatch-ID: 20260627-vnx-skills-sync

Closes the bootstrap-skills copy-once gap (ADR-026): bootstrap-skills returns early when
.claude/skills/ already exists, so there is no propagation path for skill updates. `vnx skills
sync` refreshes an existing project's skills from the installed VNX, safely:
  - dry-run is the DEFAULT (preview); --apply is required to write
  - a timestamped backup is made before any overwrite (the preservation path)
  - project-only skills are preserved (rsync without --delete)

Isolation: bin/vnx re-resolves PROJECT_ROOT internally, so the project is pinned via the
VNX_PROJECT_ROOT env override — the repo's own .claude/skills/ is never touched.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VNX = REPO / "bin" / "vnx"
SHIPPED_SKILLS = REPO / "skills"


def _run_sync(project_root: Path, *args: str):
    env = dict(os.environ)
    env["VNX_PROJECT_ROOT"] = str(project_root)
    return subprocess.run(
        ["bash", str(VNX), "skills", "sync", *args],
        capture_output=True, text=True, env=env, cwd=str(project_root),
    )


@pytest.fixture
def project(tmp_path):
    """A temp project whose .claude/skills/ is a copy of shipped skills, plus a project-only
    file and one deliberately-outdated skill, so a sync has real work to do."""
    skills = tmp_path / ".claude" / "skills"
    skills.parent.mkdir(parents=True)
    shutil.copytree(SHIPPED_SKILLS, skills)
    (skills / "_project_only.md").write_text("PROJECT CUSTOM — must survive\n")
    # Outdate one shipped skill so sync must refresh it.
    some_skill = next(skills.glob("*/SKILL.md"))
    rel = some_skill.relative_to(skills)
    some_skill.write_text("OUTDATED PROJECT VERSION\n")
    return {"root": tmp_path, "skills": skills, "outdated_rel": rel}


def _backups(skills_dir: Path):
    return list(skills_dir.parent.glob("skills.bak.*"))


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required for skills sync")
class TestSkillsSync:
    def test_dry_run_is_default_and_writes_nothing(self, project):
        r = _run_sync(project["root"])
        assert r.returncode == 0, r.stderr
        assert "dry-run" in (r.stdout + r.stderr)
        # No backup, and the outdated skill is NOT refreshed in a dry-run.
        assert _backups(project["skills"]) == []
        assert "OUTDATED" in (project["skills"] / project["outdated_rel"]).read_text()

    def test_apply_backs_up_refreshes_and_preserves_project_only(self, project):
        r = _run_sync(project["root"], "--apply")
        assert r.returncode == 0, r.stderr
        # Backup made before overwrite.
        backups = _backups(project["skills"])
        assert len(backups) == 1, "exactly one timestamped backup expected"
        # The backup holds the pre-sync (outdated) content.
        assert "OUTDATED" in (backups[0] / project["outdated_rel"]).read_text()
        # The live skill is refreshed from shipped.
        assert "OUTDATED" not in (project["skills"] / project["outdated_rel"]).read_text()
        # Project-only skill survives (rsync has no --delete).
        assert (project["skills"] / "_project_only.md").read_text().startswith("PROJECT CUSTOM")

    def test_apply_when_already_current_is_noop(self, project):
        # First apply brings it current; a second sync should report no changes + no new backup.
        _run_sync(project["root"], "--apply")
        before = len(_backups(project["skills"]))
        r = _run_sync(project["root"])
        assert r.returncode == 0, r.stderr
        assert "no changes" in (r.stdout + r.stderr).lower()
        assert len(_backups(project["skills"])) == before

    def test_missing_project_skills_dir_errors(self, tmp_path):
        (tmp_path / ".claude").mkdir()  # no skills/ subdir
        r = _run_sync(tmp_path)
        assert r.returncode != 0
        assert "bootstrap-skills" in (r.stdout + r.stderr)

    def test_repo_skills_never_touched(self, project):
        """Regression guard: the sync must operate on the temp project, never the repo."""
        before = subprocess.run(
            ["git", "status", "--short", ".claude/skills/"],
            capture_output=True, text=True, cwd=str(REPO),
        ).stdout
        _run_sync(project["root"], "--apply")
        after = subprocess.run(
            ["git", "status", "--short", ".claude/skills/"],
            capture_output=True, text=True, cwd=str(REPO),
        ).stdout
        assert before == after, "vnx skills sync must not modify the repo's own .claude/skills/"
        assert not list(REPO.glob(".claude/skills.bak.*")), "no backup leak into the repo"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
