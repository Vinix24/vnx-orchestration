#!/usr/bin/env python3
"""Tests for `t0_role_audit.sh --static` — role<->skill invocability audit.

Dispatch-ID: 20260716-f1-t0-startup-import / 20260716-ff-1174-startup-import

F1 root cause: role-orchestrator.md presupposed `t0-orchestrator` was
model-loadable via the Skill tool, but its frontmatter carried
`disable-model-invocation: true` (set by commit 3e2592f9, A-4 hardening) —
nothing cross-checked the two, so the drift went undetected for ~7 weeks.
`--static` makes that class of drift observable and CI-assertable.

Fix-forward (20260716-ff-1174): the F1 fix's own CLAUDE.md `@`-import of the
skill body turned out to trip Claude Code's external-CLAUDE.md-import trust
prompt on a fresh autonomous spawn (live smoke test) — replaced with
SessionStart-hook injection (`hooks/sessionstart.sh`). This file's coverage
grew three ways: (1) hook-injection now satisfies the in-context check the
same way a CLAUDE.md import used to; (2) AGENTS.md/GEMINI.md tri-file
surfaces are audited too (PLAYBOOK-MECHANISM-GAP, a reported-not-silent
finding, since codex/gemini have no hook equivalent); (3) the frontmatter
`disable-model-invocation: true` grep is anchored to the key's own line-start
and skips comments, so a `description:` field merely mentioning that string
no longer false-positives as SKILL-UNLOADABLE.

Fix-forward round 3 (20260716-ff-1174-r3): codex round 2 found that
`_t0_static_hook_injects_skill` accepted a hook FILE's content as sufficient
proof of in-context delivery without checking whether Claude Code is actually
configured to run that hook at all (finding 1) — exactly the gap in this
repo's own fabric source: `hooks/sessionstart.sh` correctly injects the skill
body, but `.claude/settings.json`'s SessionStart config never calls it. The
check now also requires an active (uncommented) reference to the hook script
in the SessionStart config of `.claude/settings.json` (or, absent that file,
the settings template — a consumer-check fallback for projects that haven't
run `vnx regen-settings` yet). A hook-file-only match without settings wiring
now reports the new HOOK-NOT-WIRED finding instead of the generic
SKILL-UNLOADABLE.

Isolation: every test targets a throwaway tmp_path project; a regression
guard asserts the repo's own `.claude/terminals/T0/` audits honestly — it
currently reports HOOK-NOT-WIRED, since wiring `.claude/settings.json` is an
explicitly-authorized separate operator action, not part of this fix. That
guard must flip to clean (not be re-loosened) once the operator makes that
edit — never edited by these tests.
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

# Finding 3 (codex, 2026-07-16): the frontmatter grep matched
# `disable-model-invocation: true` ANYWHERE in the frontmatter block,
# including inside a description or a comment — these two fixtures name the
# literal string without setting the real key, and must NOT be treated as
# disabled.
SKILL_DESCRIPTION_MENTIONS_STRING_FRONTMATTER = """\
---
name: t0-orchestrator
description: "Docs note: disable-model-invocation: true is an example flag, not set here."
user-invocable: true
allowed-tools: [Read]
---

# T0 Orchestrator

playbook body
"""

SKILL_COMMENTED_OUT_DISABLE_FRONTMATTER = """\
---
name: t0-orchestrator
description: test skill
# disable-model-invocation: true  (left here as documentation, not active)
user-invocable: true
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


def _wire_sessionstart_hook(project: Path, hook_relpath: str, settings_path: Path = None):
    """Write a minimal settings.json (default: project/.claude/settings.json)
    that actually registers `hook_relpath` (e.g. ".claude/hooks/sessionstart.sh"
    or "hooks/sessionstart.sh") in the SessionStart hooks config — the piece a
    hook FILE existing does not by itself prove (finding 1, codex round 2,
    2026-07-16)."""
    if settings_path is None:
        settings_dir = project / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = settings_dir / "settings.json"
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        '{\n'
        '  "hooks": {\n'
        '    "SessionStart": [\n'
        '      {\n'
        '        "matcher": "*",\n'
        '        "hooks": [\n'
        '          {\n'
        '            "type": "command",\n'
        f'            "command": "bash /abs/project/root/{hook_relpath}"\n'
        '          }\n'
        '        ]\n'
        '      }\n'
        '    ]\n'
        '  }\n'
        '}\n'
    )


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


class TestFrontmatterAnchorIgnoresMentionsAndComments:
    """Finding 3 (codex): unanchored grep false-positived SKILL-UNLOADABLE
    when the literal string `disable-model-invocation: true` appeared inside
    a description or a comment rather than as the real key."""

    def test_description_mentioning_the_string_is_not_treated_as_disabled(self, tmp_path):
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
        (t0 / "role-orchestrator.md").write_text(
            "# T0\n\nBefore anything, invoke `@t0-orchestrator` via the Skill tool.\n"
        )
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_DESCRIPTION_MENTIONS_STRING_FRONTMATTER)

        r = _run_static(project)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SKILL-UNLOADABLE" not in r.stdout

    def test_commented_out_disable_line_is_not_treated_as_disabled(self, tmp_path):
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
        (t0 / "role-orchestrator.md").write_text(
            "# T0\n\nBefore anything, invoke `@t0-orchestrator` via the Skill tool.\n"
        )
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_COMMENTED_OUT_DISABLE_FRONTMATTER)

        r = _run_static(project)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SKILL-UNLOADABLE" not in r.stdout

    def test_real_disable_key_still_detected(self, tmp_path):
        """Regression guard: the anchor fix must not blind the check to a
        genuinely disabled skill."""
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


class TestSkillHookInjected:
    """Finding 0 mechanism change: the t0-orchestrator playbook body now
    reaches T0 via the SessionStart hook, not a CLAUDE.md `@`-import. The
    hook-detection path must satisfy the in-context condition the same way
    an import used to — but ONLY when the hook is both referencing the
    skill AND actually wired into SessionStart config (finding 1, codex
    round 2, 2026-07-16): a hook file existing was previously accepted as
    sufficient proof on its own, without checking whether Claude Code is
    configured to run it at all."""

    def _write_hook(self, project: Path, hooks_dir_name: str):
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
        (t0 / "role-orchestrator.md").write_text(
            "# T0\n\nBefore anything, invoke `@t0-orchestrator` via the Skill tool.\n"
        )
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_DISABLED_FRONTMATTER)

        hooks_dir = project / hooks_dir_name
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "sessionstart.sh").write_text(
            "#!/usr/bin/env bash\ncat \"$PROJECT_ROOT/.claude/skills/t0-orchestrator/SKILL.md\"\n"
        )

    def test_disabled_skill_hook_injected_by_deployed_copy_is_clean(self, tmp_path):
        project = _make_project(tmp_path)
        self._write_hook(project, ".claude/hooks")
        _wire_sessionstart_hook(project, ".claude/hooks/sessionstart.sh")

        r = _run_static(project)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "clean" in (r.stdout + r.stderr).lower()

    def test_disabled_skill_hook_injected_by_fabric_source_template_is_clean(self, tmp_path):
        """Mirrors this repo's own shape: no `.claude/hooks/` deployed copy,
        only the fabric-source `hooks/sessionstart.sh` template — WIRED into
        .claude/settings.json's SessionStart config."""
        project = _make_project(tmp_path)
        self._write_hook(project, "hooks")
        _wire_sessionstart_hook(project, "hooks/sessionstart.sh")

        r = _run_static(project)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "clean" in (r.stdout + r.stderr).lower()

    def test_hook_present_but_not_referencing_the_skill_still_reports_unloadable(self, tmp_path):
        """A hook file existing is not enough — it must actually reference
        this skill's SKILL.md path."""
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
        (t0 / "role-orchestrator.md").write_text(
            "# T0\n\nBefore anything, invoke `@t0-orchestrator` via the Skill tool.\n"
        )
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_DISABLED_FRONTMATTER)

        hooks_dir = project / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "sessionstart.sh").write_text("#!/usr/bin/env bash\necho '{}'\n")
        _wire_sessionstart_hook(project, ".claude/hooks/sessionstart.sh")

        r = _run_static(project)
        assert r.returncode != 0
        assert "SKILL-UNLOADABLE" in r.stdout

    def test_hook_references_skill_but_settings_do_not_wire_it_reports_hook_not_wired(self, tmp_path):
        """The exact gap finding 1 found in this repo's own fabric source:
        hooks/sessionstart.sh correctly injects the skill body, but no
        .claude/settings.json (nor a settings template) exists to actually
        invoke it. A hook file being present and correctly written is not
        proof Claude Code runs it — this must report a finding, not clean."""
        project = _make_project(tmp_path)
        self._write_hook(project, "hooks")

        r = _run_static(project)
        assert r.returncode != 0
        assert "HOOK-NOT-WIRED" in r.stdout
        assert "t0-orchestrator" in r.stdout

    def test_deployed_hook_references_skill_but_settings_json_omits_it_reports_hook_not_wired(self, tmp_path):
        """Same gap, deployed-consumer shape: .claude/hooks/sessionstart.sh
        references the skill, but .claude/settings.json exists and simply
        never wires it into SessionStart (e.g. only unrelated hooks are
        registered there, mirroring this repo's real settings.json)."""
        project = _make_project(tmp_path)
        self._write_hook(project, ".claude/hooks")
        settings_dir = project / ".claude"
        (settings_dir / "settings.json").write_text(
            '{\n'
            '  "hooks": {\n'
            '    "SessionStart": [\n'
            '      {\n'
            '        "matcher": "",\n'
            '        "hooks": [\n'
            '          {\n'
            '            "type": "command",\n'
            '            "command": "bash -c \'exec bash scripts/hooks/unrelated.sh\'"\n'
            '          }\n'
            '        ]\n'
            '      }\n'
            '    ]\n'
            '  }\n'
            '}\n'
        )

        r = _run_static(project)
        assert r.returncode != 0
        assert "HOOK-NOT-WIRED" in r.stdout

    def test_commented_out_settings_reference_to_hook_does_not_count_as_wired(self, tmp_path):
        """A `#`-commented shell reference to the hook path inside the
        command string must not be treated as an active invocation."""
        project = _make_project(tmp_path)
        self._write_hook(project, ".claude/hooks")
        settings_dir = project / ".claude"
        (settings_dir / "settings.json").write_text(
            '{\n'
            '  "hooks": {\n'
            '    "SessionStart": [\n'
            '      {\n'
            '        "matcher": "*",\n'
            '        "hooks": [\n'
            '          {\n'
            '            "type": "command",\n'
            '            "command": "bash -c \'# exec bash .claude/hooks/sessionstart.sh\'"\n'
            '          }\n'
            '        ]\n'
            '      }\n'
            '    ]\n'
            '  }\n'
            '}\n'
        )

        r = _run_static(project)
        assert r.returncode != 0
        assert "HOOK-NOT-WIRED" in r.stdout

    def test_settings_template_wires_hook_when_no_deployed_settings_json_is_clean(self, tmp_path):
        """Consumer-check fallback: a project that hasn't run `vnx init`/
        `vnx regen-settings` yet has no .claude/settings.json at all — the
        settings TEMPLATE it will get is accepted as evidence instead, so it
        isn't reported unloadable purely for not having generated its
        settings.json yet."""
        project = _make_project(tmp_path)
        self._write_hook(project, ".claude/hooks")
        _wire_sessionstart_hook(
            project, ".claude/hooks/sessionstart.sh",
            settings_path=project / "templates" / "settings_vnx_keys.json.tmpl",
        )

        r = _run_static(project)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "clean" in (r.stdout + r.stderr).lower()


class TestTriFilePlaybookMechanismGap:
    """Finding 2 (codex): `vnx role sync` mirrors the same Mandatory Startup
    role text into AGENTS.md/GEMINI.md, but Claude Code's SessionStart-hook
    injection has no codex/gemini equivalent — the audit must report that
    gap instead of auditing clean on CLAUDE.md alone."""

    ROLE_MARKER_BEGIN = "<!-- VNX:BEGIN T0-ROLE -->"
    ROLE_MARKER_END = "<!-- VNX:END T0-ROLE -->"

    def _healthy_claude_side(self, project: Path):
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
        (t0 / "role-orchestrator.md").write_text("# T0\n\nNo skill references here.\n")
        return t0

    def test_agents_md_with_role_marker_reports_gap(self, tmp_path):
        project = _make_project(tmp_path)
        t0 = self._healthy_claude_side(project)
        (t0 / "AGENTS.md").write_text(
            f"{self.ROLE_MARKER_BEGIN}\n# T0 - VNX Master Orchestrator\n{self.ROLE_MARKER_END}\n"
        )

        r = _run_static(project)
        assert r.returncode != 0
        assert "PLAYBOOK-MECHANISM-GAP" in r.stdout
        assert "AGENTS.md" in r.stdout

    def test_gemini_md_with_role_marker_reports_gap(self, tmp_path):
        project = _make_project(tmp_path)
        t0 = self._healthy_claude_side(project)
        (t0 / "GEMINI.md").write_text(
            f"{self.ROLE_MARKER_BEGIN}\n# T0 - VNX Master Orchestrator\n{self.ROLE_MARKER_END}\n"
        )

        r = _run_static(project)
        assert r.returncode != 0
        assert "PLAYBOOK-MECHANISM-GAP" in r.stdout
        assert "GEMINI.md" in r.stdout

    def test_agents_md_without_role_marker_is_not_flagged(self, tmp_path):
        """A hand-authored AGENTS.md that hasn't been synced with `vnx role
        sync` yet carries no marker — nothing to report."""
        project = _make_project(tmp_path)
        t0 = self._healthy_claude_side(project)
        (t0 / "AGENTS.md").write_text("# Project Codex notes\n\nSome custom guidance.\n")

        r = _run_static(project)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "PLAYBOOK-MECHANISM-GAP" not in r.stdout

    def test_no_provider_files_is_not_flagged(self, tmp_path):
        project = _make_project(tmp_path)
        self._healthy_claude_side(project)

        r = _run_static(project)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "PLAYBOOK-MECHANISM-GAP" not in r.stdout


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
    def test_vnx_repo_itself_reports_the_honest_hook_not_wired_gap(self):
        """Finding 1 (codex round 2, 2026-07-16): this repo's own
        hooks/sessionstart.sh correctly injects the t0-orchestrator skill
        body, but .claude/settings.json's SessionStart config does not
        invoke that hook — that line is intentionally left for a separate,
        explicitly-authorized operator action (an operator-config change,
        not something this fix-forward makes on its own). Until that
        settings.json edit lands, `--static` must report this gap honestly
        rather than claim clean — this regression guard asserts the honest
        state, and must flip back to clean (not be re-loosened) once the
        operator wires the hook. Never edited by these tests."""
        r = _run_static(REPO)
        assert r.returncode != 0, r.stdout + r.stderr
        out = r.stdout + r.stderr
        assert "HOOK-NOT-WIRED" in out
        assert "t0-orchestrator" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
