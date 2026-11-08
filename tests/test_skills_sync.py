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

sys.path.insert(0, str(REPO / "scripts" / "lib"))
from vnx_skills import resolve_sync_set, SKILL_OPT_OUT_MARKER  # noqa: E402


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


class TestDirectoryBasedSyncSet:
    """Directory-based discovery: the sync set is derived from skill directories,
    not from a manual manifest, with an explicit opt-out marker."""

    def test_shipped_skills_include_horizon_and_fabric_reference(self):
        sync_set = resolve_sync_set(SHIPPED_SKILLS)
        assert "horizon" in sync_set, "horizon must be in the shipped sync set"
        assert "fabric-reference" in sync_set, "fabric-reference must be in the shipped sync set"

    def test_new_fixture_skill_is_included(self, tmp_path):
        source = tmp_path / "skills"
        source.mkdir()
        (source / "fixture-new-skill").mkdir()
        (source / "fixture-new-skill" / "SKILL.md").write_text("# Fixture\n")
        assert "fixture-new-skill" in resolve_sync_set(source)

    def test_opt_out_marker_excludes_skill_from_sync_set(self, tmp_path):
        source = tmp_path / "skills"
        source.mkdir()
        (source / "local-only").mkdir()
        (source / "local-only" / "SKILL.md").write_text("# Local\n")
        (source / "local-only" / SKILL_OPT_OUT_MARKER).write_text("project-local\n")
        (source / "fixture-shipped").mkdir()
        (source / "fixture-shipped" / "SKILL.md").write_text("# Shipped\n")
        sync_set = resolve_sync_set(source)
        assert "local-only" not in sync_set
        assert "fixture-shipped" in sync_set  # sanity: non-opted-out still included


def _run_for(project_root: Path, tmp_path: Path, *args: str, extra_env=None):
    """Run the sync with codex/gemini absent from PATH and HOME/VNX_HOME isolated."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for tool in ("bash", "rsync", "date", "cp", "mkdir", "grep", "git", "dirname", "basename",
                 "cat", "sed", "tr", "uname", "env", "python3", "sort", "head", "wc", "readlink"):
        found = shutil.which(tool)
        if found and not (bindir / tool).exists():
            (bindir / tool).symlink_to(found)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": str(bindir), "HOME": str(home),
        "VNX_PROJECT_ROOT": str(project_root),
        "VNX_HOME": str(REPO), "VNX_STATE_DIR": str(tmp_path / "state"),
    }
    env.update(extra_env or {})
    return subprocess.run(
        [shutil.which("bash"), str(VNX), "skills", "sync", *args],
        capture_output=True, text=True, env=env, cwd=str(project_root),
    )


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required for skills sync")
class TestSkillsSyncForProvider:
    def test_codex_mirrors_without_cli(self, project, tmp_path):
        assert shutil.which("codex", path=str(tmp_path / "bin")) is None
        r = _run_for(project["root"], tmp_path, "--for", "codex", "--apply")
        mirrored = project["root"] / ".agents/skills/horizon/SKILL.md"
        assert mirrored.is_file(), f"codex mirror absent without the CLI: {r.stderr.strip()}"
        assert r.returncode == 0, r.stderr

    def test_gemini_mirrors_without_cli(self, project, tmp_path):
        r = _run_for(project["root"], tmp_path, "--for", "gemini", "--apply")
        assert r.returncode == 0, r.stderr
        assert (project["root"] / ".gemini/skills/horizon/SKILL.md").is_file()
        assert not (project["root"] / ".agents/skills").exists()

    def test_mirrors_even_when_claude_skills_already_current(self, project, tmp_path):
        _run_for(project["root"], tmp_path, "--apply")
        r = _run_for(project["root"], tmp_path, "--for", "codex", "--apply")
        assert r.returncode == 0, r.stderr
        assert (project["root"] / ".agents/skills/horizon/SKILL.md").is_file()

    def test_dry_run_with_for_writes_nothing(self, project, tmp_path):
        r = _run_for(project["root"], tmp_path, "--for", "codex")
        assert r.returncode == 0, r.stderr
        assert "dry-run" in (r.stdout + r.stderr)
        assert not (project["root"] / ".agents").exists()
        assert _backups(project["skills"]) == []

    def test_unknown_provider_is_refused(self, project, tmp_path):
        r = _run_for(project["root"], tmp_path, "--for", "kimi", "--apply")
        assert r.returncode != 0
        assert "--for" in (r.stdout + r.stderr)
        assert not (project["root"] / ".agents").exists()

    def test_for_without_value_is_refused(self, project, tmp_path):
        r = _run_for(project["root"], tmp_path, "--for")
        assert r.returncode != 0

    def test_auto_detect_without_for_keeps_cli_probe(self, project, tmp_path):
        (project["root"] / ".agents/skills").mkdir(parents=True)
        r = _run_for(project["root"], tmp_path, "--apply")
        assert r.returncode == 0, r.stderr
        # codex is not on PATH: today's probe leaves the existing mirror untouched.
        assert not (project["root"] / ".agents/skills/horizon/SKILL.md").exists()

    def test_auto_detect_mirrors_when_cli_present(self, project, tmp_path):
        (project["root"] / ".agents/skills").mkdir(parents=True)
        fake = tmp_path / "bin"
        fake.mkdir(exist_ok=True)
        (fake / "codex").write_text("#!/bin/sh\nexit 0\n")
        (fake / "codex").chmod(0o755)
        r = _run_for(project["root"], tmp_path, "--apply")
        assert r.returncode == 0, r.stderr
        assert (project["root"] / ".agents/skills/horizon/SKILL.md").is_file()

    def test_detector_is_called_and_unsafe_names_reported(self, project, tmp_path):
        shim = tmp_path / "shim"
        shim.mkdir()
        log = tmp_path / "detector.log"
        (shim / "sitecustomize.py").write_text(f"import sys\nsys.path.insert(0, {str(shim)!r})\n")
        (shim / "skill_portability.py").write_text(
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('_real_sp', {str(REPO / 'scripts/lib/skill_portability.py')!r})\n"
            "real = importlib.util.module_from_spec(spec)\n"
            "sys.modules['_real_sp'] = real\n"
            f"sys.path.insert(0, {str(REPO / 'scripts/lib')!r})\n"
            "spec.loader.exec_module(real)\n"
            "def provider_unsafe_skills(tree):\n"
            f"    open({str(log)!r}, 'a').write(str(tree) + '\\n')\n"
            "    return ['zz-fake-unsafe'] + real.provider_unsafe_skills(tree)\n"
        )
        r = _run_for(project["root"], tmp_path, "--for", "codex", "--apply",
                     extra_env={"PYTHONPATH": str(shim)})
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert log.exists(), out
        assert log.read_text().strip() == str(REPO / "skills")
        lines = [ln for ln in out.splitlines() if "zz-fake-unsafe" in ln]
        assert len(lines) == 1 and "codex" in lines[0]
        # Warning, not a gate: the unsafe skills are still mirrored.
        assert (project["root"] / ".agents/skills/architect/SKILL.md").is_file()

    def test_real_detector_reports_each_unsafe_skill_by_name(self, project, tmp_path):
        sys.path.insert(0, str(REPO / "scripts" / "lib"))
        from skill_portability import provider_unsafe_skills  # noqa: E402
        names = provider_unsafe_skills(SHIPPED_SKILLS)
        r = _run_for(project["root"], tmp_path, "--for", "gemini")
        out = r.stdout + r.stderr
        for name in names:
            assert sum(1 for ln in out.splitlines() if f"{name}" in ln and "WARN" in ln) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
