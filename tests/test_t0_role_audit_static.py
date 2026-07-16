#!/usr/bin/env python3
"""Tests for `t0_role_audit.sh --static` — role<->skill invocability audit.

Dispatch-ID: 20260716-f1-t0-startup-import

F1 root cause: role-orchestrator.md presupposed `t0-orchestrator` was
model-loadable via the Skill tool, but its frontmatter carried
`disable-model-invocation: true` (set by commit 3e2592f9, A-4 hardening) —
nothing cross-checked the two, so the drift went undetected for ~7 weeks.
`--static` makes that class of drift observable and CI-assertable.

Isolation: every test targets a throwaway tmp_path project; a regression
guard asserts the repo's own `.claude/terminals/T0/` audits clean after the
F1 fix (it must never need editing by these tests).
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AUDIT_SCRIPT = REPO / "scripts" / "commands" / "t0_role_audit.sh"

SKILL_DISABLED_FRONTMATTER = """\
---
name: t0-orchestrator
description: test skill
user-invocable: true
disable-model-invocation: true
allowed-tools: [Read]
---

# T0 Orchestrator

playbook body
"""

SKILL_INVOCABLE_FRONTMATTER = """\
---
name: t0-orchestrator
description: test skill
allowed-tools: [Read]
---

# T0 Orchestrator

playbook body
"""


def _run_static(root: Path):
    return subprocess.run(
        ["bash", str(AUDIT_SCRIPT), "--static", str(root)],
        capture_output=True, text=True,
    )


def _make_project(tmp_path: Path, name: str = "project") -> Path:
    root = tmp_path / name
    t0 = root / ".claude" / "terminals" / "T0"
    t0.mkdir(parents=True)
    return root


class TestHealthyProject:
    def test_import_present_and_resolves_exits_clean(self, tmp_path):
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
        (t0 / "role-orchestrator.md").write_text("# T0\n\nNo skill references here.\n")

        r = _run_static(project)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "clean" in (r.stdout + r.stderr).lower()


class TestImportMissing:
    def test_missing_import_target_reports_import_missing(self, tmp_path):
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n@does-not-exist.md\n")
        (t0 / "role-orchestrator.md").write_text("# T0\n\nNo skill references here.\n")

        r = _run_static(project)
        assert r.returncode != 0
        assert "IMPORT-MISSING" in r.stdout
        assert "does-not-exist.md" in r.stdout


class TestSkillUnloadable:
    def test_disabled_skill_not_imported_reports_skill_unloadable(self, tmp_path):
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
        (t0 / "role-orchestrator.md").write_text(
            "# T0\n\nBefore anything, invoke `@t0-orchestrator` via the Skill tool.\n"
        )
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_DISABLED_FRONTMATTER)

        r = _run_static(project)
        assert r.returncode != 0
        assert "SKILL-UNLOADABLE" in r.stdout
        assert "t0-orchestrator" in r.stdout

    def test_missing_skill_file_entirely_reports_skill_unloadable(self, tmp_path):
        """The sales-copilot case: role text references a skill that has no
        SKILL.md at all in this project."""
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
        (t0 / "role-orchestrator.md").write_text(
            "# T0\n\nBefore anything, invoke `@t0-orchestrator` via the Skill tool.\n"
        )

        r = _run_static(project)
        assert r.returncode != 0
        assert "SKILL-UNLOADABLE" in r.stdout
        assert "does not exist" in r.stdout

    def test_disabled_skill_but_invocable_variant_is_clean(self, tmp_path):
        """A skill without disable-model-invocation is a real Skill-tool call
        target — no finding, even without a CLAUDE.md import."""
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
        (t0 / "role-orchestrator.md").write_text(
            "# T0\n\nBefore anything, invoke `@t0-orchestrator` via the Skill tool.\n"
        )
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_INVOCABLE_FRONTMATTER)

        r = _run_static(project)
        assert r.returncode == 0, r.stdout + r.stderr


class TestSkillImportedIntoContext:
    def test_disabled_skill_imported_by_claude_md_is_clean(self, tmp_path):
        """Mirrors the actual F1 fix: a disabled skill referenced in
        role-orchestrator.md is fine when its SKILL.md is `@`-imported
        (in-context) by the T0 CLAUDE.md — invocability becomes irrelevant."""
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text(
            "@role-orchestrator.md\n@../../skills/t0-orchestrator/SKILL.md\n"
        )
        (t0 / "role-orchestrator.md").write_text(
            "# T0\n\nBefore anything, invoke `@t0-orchestrator` via the Skill tool.\n"
        )
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_DISABLED_FRONTMATTER)

        r = _run_static(project)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "clean" in (r.stdout + r.stderr).lower()


class TestDefaultRootIsGitRoot:
    def test_no_project_root_arg_defaults_to_cwd_git_root(self, tmp_path):
        project = _make_project(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=str(project), check=True)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
        (t0 / "role-orchestrator.md").write_text("# T0\n\nNo skill references here.\n")

        r = subprocess.run(
            ["bash", str(AUDIT_SCRIPT), "--static"],
            capture_output=True, text=True, cwd=str(project),
        )
        assert r.returncode == 0, r.stdout + r.stderr


class TestRepoSelfAudit:
    def test_vnx_repo_itself_passes_static_audit(self):
        """Regression guard: after the F1 fix, this repo's own T0 role must
        audit clean — never edited by these tests."""
        r = _run_static(REPO)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "clean" in (r.stdout + r.stderr).lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
