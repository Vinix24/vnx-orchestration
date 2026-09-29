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

Fix-forward round 4 (20260716-ff-1174-r4): the repo self-audit regression
guard below asserted a FIXED expected outcome (HOOK-NOT-WIRED) that was true
only until the operator wired `.claude/settings.json` — once that landed
(commit 67f0bf91), the guard itself started failing on the branch that fixed
the gap it exists to catch. It is now state-aware: it independently
JSON-parses `.claude/settings.json` (not via the script under test) to
determine whether the hook is actually wired, then asserts whichever outcome
that state implies — clean/exit 0 when wired, HOOK-NOT-WIRED/exit!=0 when
not. Both directions stay fixture-tested above regardless of this repo's own
state; this guard only checks the self-audit stays consistent with reality.

Also this round: finding 2 (basename-only hook matching let an unrelated
script sharing the name "sessionstart.sh" at a different path false-match —
fixed to compare the dir/file path suffix instead) and finding 3 (a minified
single-line settings.json skipped the very line carrying both the
"SessionStart" key and the hook command, false-negativing HOOK-NOT-WIRED —
fixed to scan that line too).

Isolation: every test targets a throwaway tmp_path project except the
self-audit guard, which deliberately targets this repo's own
`.claude/terminals/T0/` — never edited by these tests.
"""

import json
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

    def test_settings_reference_to_differently_located_same_basename_script_reports_not_wired(self, tmp_path):
        """Finding 2 (2026-07-16 r4): matching only the hook's BASENAME let
        an unrelated script that merely SHARES the name "sessionstart.sh" at
        a different path count as wiring the real hook. The needle must
        include the directory component (hooks/sessionstart.sh), not just
        the basename."""
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
            '            "command": "bash scripts/unrelated-tool/sessionstart.sh"\n'
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

    def test_minified_single_line_settings_json_wires_hook(self, tmp_path):
        """Finding 3 (2026-07-16 r4): the awk scan skipped the very line
        that matched "SessionStart": before checking it for the needle — a
        minified single-line settings.json puts key, brackets, and hook
        command all on that one line, so it was never found."""
        project = _make_project(tmp_path)
        self._write_hook(project, ".claude/hooks")
        settings_dir = project / ".claude"
        (settings_dir / "settings.json").write_text(
            '{"hooks":{"SessionStart":[{"matcher":"*","hooks":[{"type":"command",'
            '"command":"bash .claude/hooks/sessionstart.sh"}]}]}}\n'
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


def _repo_sessionstart_hook_is_wired() -> bool:
    """Independent (JSON-parse, not the awk logic under test) check of
    whether this repo's own .claude/settings.json actively references
    hooks/sessionstart.sh in its SessionStart config — decides which
    assertion direction the regression guard below expects, rather than the
    guard hardcoding one fixed outcome that goes stale the moment an
    operator edits settings.json (finding 1 self-test staleness, 2026-07-16
    r4: commit 67f0bf91 wired the hook and this guard's old fixed
    HOOK-NOT-WIRED assertion started failing on its own branch)."""
    settings_file = REPO / ".claude" / "settings.json"
    if not settings_file.is_file():
        return False
    try:
        data = json.loads(settings_file.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    for entry in data.get("hooks", {}).get("SessionStart", []):
        for hook in entry.get("hooks", []):
            if "hooks/sessionstart.sh" in hook.get("command", ""):
                return True
    return False


class TestRepoSelfAudit:
    def test_vnx_repo_itself_matches_its_actual_hook_wiring_state(self):
        """State-aware regression guard: whichever way this repo's own
        .claude/settings.json currently wires (or doesn't wire)
        hooks/sessionstart.sh, `--static` must report the matching outcome —
        clean/exit 0 once wired, HOOK-NOT-WIRED/exit!=0 while it isn't. Both
        directions stay independently fixture-tested above (TestSkillHookInjected);
        this guard only checks the self-audit stays consistent with the real
        state of this repo's own settings.json — never edited by these
        tests."""
        r = _run_static(REPO)
        out = r.stdout + r.stderr
        if _repo_sessionstart_hook_is_wired():
            assert r.returncode == 0, out
            assert "HOOK-NOT-WIRED" not in out
        else:
            assert r.returncode != 0, out
            assert "HOOK-NOT-WIRED" in out
            assert "t0-orchestrator" in out


# ---------------------------------------------------------------------------
# Sources the role names: scripts, subcommands, state files (fsh D10).
#
# Fixture fabric = a tmp project that carries bin/vnx, scripts/ and the writers
# manifest, so the audit judges the role against THAT tree. ADR-007 does not
# apply: the audit reads no central DB and creates no table.
# ---------------------------------------------------------------------------

FIXTURE_BIN_VNX = """\
#!/usr/bin/env bash
main() {
  case "$1" in
    dispatch)
      echo dispatch ;;
    objective)
      "$VNX_PYTHON" "$VNX_HOME/scripts/planning_cli.py" objective "$@" ;;
  esac
}
"""

FIXTURE_MANIFEST = """\
# fixture manifest
t0_state.json | scripts/build_t0_state.py | t0_state.json |
t0_state.json#open_items | scripts/build_t0_state.py | "open_items": | open items
digest.json | scripts/open_items_manager.py | digest.json |
"""

FIXTURE_ROLE = """\
# T0

Read `.vnx-data/state/t0_state.json` for terminals and open items.

```bash
python3 scripts/receipt_query.py pull --json
python3 scripts/open_items_manager.py digest
vnx dispatch
vnx objective list
```

Also `.vnx-data/state/digest.json`.
"""


def _make_fabric(tmp_path: Path, role: str = FIXTURE_ROLE, name: str = "fabric") -> Path:
    root = _make_project(tmp_path, name)
    (root / "bin").mkdir()
    (root / "bin" / "vnx").write_text(FIXTURE_BIN_VNX)
    scripts = root / "scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "planning_cli.py").write_text('sub.add_parser("list")\nsub.add_parser("show")\n')
    (scripts / "receipt_query.py").write_text('sub.add_parser("pull")\nsub.add_parser("digest")\n')
    (scripts / "open_items_manager.py").write_text('OUT = "digest.json"\nsub.add_parser("digest")\n')
    (scripts / "build_t0_state.py").write_text('OUT = "t0_state.json"\nstate = {"open_items": 1}\n"open_items": 1,\n')
    (scripts / "lib" / "t0_role_state_writers.txt").write_text(FIXTURE_MANIFEST)
    # The audit script under test judges a fixture through its own copy of the auditor.
    (scripts / "lib" / "t0_role_sources_audit.py").write_text(
        (REPO / "scripts" / "lib" / "t0_role_sources_audit.py").read_text()
    )
    t0 = root / ".claude" / "terminals" / "T0"
    (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
    (t0 / "role-orchestrator.md").write_text(role)
    return root


class TestRoleSourcesHealthy:
    def test_fixture_role_naming_only_real_sources_is_clean(self, tmp_path):
        root = _make_fabric(tmp_path)
        r = _run_static(root)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "clean" in r.stdout.lower()

    def test_repo_role_names_only_sources_the_repo_has(self):
        """The shipped role is green at merge of D10."""
        r = _run_static(REPO)
        out = r.stdout + r.stderr
        for code in ("SCRIPT-MISSING", "SUBCOMMAND-MISSING", "STATE-UNWRITTEN", "STATE-WRITER-GONE"):
            assert code not in out, out


class TestScriptMissing:
    def test_role_naming_nonexistent_script_is_flagged(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nRun `python3 scripts/ghost_tool.py --now`.\n")
        r = _run_static(root)
        assert r.returncode != 0
        assert "SCRIPT-MISSING" in r.stdout
        assert "scripts/ghost_tool.py" in r.stdout

    def test_bare_script_name_resolves_anywhere_under_scripts(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nSee `t0_role_sources_audit.py`.\n")
        r = _run_static(root)
        assert r.returncode == 0, r.stdout

    def test_deleting_a_script_the_role_names_turns_the_audit_red(self, tmp_path):
        root = _make_fabric(tmp_path)
        assert _run_static(root).returncode == 0
        (root / "scripts" / "receipt_query.py").unlink()
        r = _run_static(root)
        assert r.returncode != 0
        assert "SCRIPT-MISSING" in r.stdout and "receipt_query.py" in r.stdout

    def test_docs_source_is_audited_too(self, tmp_path):
        root = _make_fabric(tmp_path)
        docs = root / "docs" / "core"
        docs.mkdir(parents=True)
        (docs / "DISPATCH_RULES.md").write_text("Run `python3 scripts/gone_script.py pull`.\n")
        r = _run_static(root)
        assert r.returncode != 0
        assert "SCRIPT-MISSING" in r.stdout and "gone_script.py" in r.stdout
        assert "DISPATCH_RULES.md" in r.stdout


class TestSubcommandMissing:
    def test_removed_script_subcommand_is_flagged(self, tmp_path):
        root = _make_fabric(tmp_path)
        (root / "scripts" / "receipt_query.py").write_text('sub.add_parser("digest")\n')
        r = _run_static(root)
        assert r.returncode != 0
        assert "SUBCOMMAND-MISSING" in r.stdout
        assert "receipt_query.py pull" in r.stdout

    def test_unknown_vnx_command_is_flagged(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nThen `vnx teleport`.\n")
        r = _run_static(root)
        assert r.returncode != 0
        assert "SUBCOMMAND-MISSING" in r.stdout and "vnx teleport" in r.stdout

    def test_unknown_group_subcommand_is_flagged(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nThen `vnx objective vaporize`.\n")
        r = _run_static(root)
        assert r.returncode != 0
        assert "vnx objective vaporize" in r.stdout

    def test_second_word_of_plain_command_is_prose_not_a_subcommand(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nUse `vnx dispatch` to send work.\n")
        r = _run_static(root)
        assert r.returncode == 0, r.stdout

    def test_command_file_under_scripts_commands_counts(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nThen `vnx snapshot`.\n")
        (root / "scripts" / "commands").mkdir()
        (root / "scripts" / "commands" / "snapshot.sh").write_text("cmd_snapshot() { :; }\n")
        assert _run_static(root).returncode == 0


class TestStateSources:
    def test_state_file_absent_from_manifest_is_unwritten(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nRead `.vnx-data/state/ghost_state.json`.\n")
        r = _run_static(root)
        assert r.returncode != 0
        assert "STATE-UNWRITTEN" in r.stdout and "ghost_state.json" in r.stdout

    def test_builder_dropping_a_t0_state_section_turns_the_audit_red(self, tmp_path):
        root = _make_fabric(tmp_path)
        assert _run_static(root).returncode == 0
        (root / "scripts" / "build_t0_state.py").write_text('OUT = "t0_state.json"\n')
        r = _run_static(root)
        assert r.returncode != 0
        assert "STATE-WRITER-GONE" in r.stdout and "open_items" in r.stdout

    def test_role_dropping_a_section_phrase_is_flagged(self, tmp_path):
        role = FIXTURE_ROLE.replace("terminals and open items", "terminals")
        root = _make_fabric(tmp_path, role)
        r = _run_static(root)
        assert r.returncode != 0
        assert "STATE-WRITER-GONE" in r.stdout and "open items" in r.stdout

    def test_writer_script_deleted_is_flagged(self, tmp_path):
        root = _make_fabric(tmp_path)
        (root / "scripts" / "open_items_manager.py").unlink()
        r = _run_static(root)
        assert r.returncode != 0
        assert "STATE-WRITER-GONE" in r.stdout and "digest.json" in r.stdout

    def test_malformed_manifest_line_is_flagged(self, tmp_path):
        root = _make_fabric(tmp_path)
        manifest = root / "scripts" / "lib" / "t0_role_state_writers.txt"
        manifest.write_text(manifest.read_text() + "only-one-column\n")
        r = _run_static(root)
        assert r.returncode != 0
        assert "MANIFEST-BAD-LINE" in r.stdout


class TestSourcesAuditNeverSilent:
    """An audit that could not measure is red, not clean (codex ff1, point 1)."""

    def test_auditor_exiting_nonzero_without_output_is_a_finding(self, tmp_path):
        root = _make_fabric(tmp_path)
        assert _run_static(root).returncode == 0
        (root / "scripts" / "lib" / "t0_role_sources_audit.py").write_text("import sys\nsys.exit(3)\n")
        r = _run_static(root)
        assert r.returncode != 0, r.stdout
        assert "SOURCES-AUDIT-FAILED: exit 3" in r.stdout

    def test_auditor_crashing_with_traceback_is_a_finding(self, tmp_path):
        root = _make_fabric(tmp_path)
        (root / "scripts" / "lib" / "t0_role_sources_audit.py").write_text("raise RuntimeError('boom')\n")
        r = _run_static(root)
        assert r.returncode != 0
        assert "SOURCES-AUDIT-FAILED: exit 1" in r.stdout

    def test_missing_auditor_script_is_a_finding(self, tmp_path):
        root = _make_fabric(tmp_path)
        (root / "scripts" / "lib" / "t0_role_sources_audit.py").unlink()
        r = _run_static(root)
        assert r.returncode != 0
        assert "SOURCES-AUDIT-FAILED" in r.stdout and "does not exist" in r.stdout

    def test_normal_findings_do_not_add_a_failure_line(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nThen `vnx teleport`.\n")
        r = _run_static(root)
        assert r.returncode != 0
        assert "SOURCES-AUDIT-FAILED" not in r.stdout


class TestSubcommandScopedPerGroup:
    """`vnx <group> <sub>` is checked against the file that handles the group."""

    def test_word_defined_by_another_command_file_does_not_satisfy_the_group(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nThen `vnx objective vaporize`.\n")
        (root / "scripts" / "commands").mkdir()
        (root / "scripts" / "commands" / "other.sh").write_text('case "$1" in vaporize) echo x ;; esac\n')
        (root / "scripts" / "elsewhere_cli.py").write_text('sub.add_parser("vaporize")\n')
        # A second project whose command file also defines it must not leak in.
        other = _make_fabric(tmp_path, name="other")
        (other / "scripts" / "planning_cli.py").write_text('sub.add_parser("vaporize")\n')
        r = _run_static(root)
        assert r.returncode != 0, r.stdout
        assert "vnx objective vaporize" in r.stdout

    def test_word_defined_by_the_group_handler_is_clean(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nThen `vnx objective show`.\n")
        assert _run_static(root).returncode == 0

    def test_shell_function_group_reads_its_own_function_body(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nThen `vnx role sync` and `vnx role explode`.\n")
        (root / "bin" / "vnx").write_text(
            FIXTURE_BIN_VNX.replace("  esac\n}", "    role)\n      cmd_role \"$@\" ;;\n  esac\n}")
            + 'cmd_role() {\n  if [ "$1" = "sync" ]; then echo sync; fi\n}\n'
        )
        r = _run_static(root)
        assert r.returncode != 0
        assert "vnx role explode" in r.stdout and "vnx role sync" not in r.stdout


class TestDocStateChecked:
    """DISPATCH_RULES state paths meet the writers manifest too (point 3)."""

    def _with_doc(self, tmp_path, text):
        root = _make_fabric(tmp_path)
        doc = root / "docs" / "core"
        doc.mkdir(parents=True)
        (doc / "DISPATCH_RULES.md").write_text(text)
        return root

    def test_doc_state_path_without_manifest_line_is_unwritten(self, tmp_path):
        root = self._with_doc(tmp_path, "Read `.vnx-data/state/bestaat_niet.json`.\n")
        r = _run_static(root)
        assert r.returncode != 0
        assert "STATE-UNWRITTEN" in r.stdout and "bestaat_niet.json" in r.stdout
        assert "DISPATCH_RULES.md" in r.stdout

    def test_doc_state_path_with_manifest_line_is_clean(self, tmp_path):
        root = self._with_doc(tmp_path, "Read `.vnx-data/state/digest.json`.\n")
        assert _run_static(root).returncode == 0

    def test_doc_state_writer_gone_is_flagged_once_when_role_names_it_too(self, tmp_path):
        root = self._with_doc(tmp_path, "Read `.vnx-data/state/digest.json`.\n")
        (root / "scripts" / "open_items_manager.py").unlink()
        r = _run_static(root)
        assert r.returncode != 0
        assert r.stdout.count("STATE-WRITER-GONE") == 1


class TestFencedCommands:
    """Command lines inside fenced blocks are audited; prose is not (point 4)."""

    def test_fenced_unknown_command_is_flagged(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\n```bash\nvnx bestaatniet\n```\n")
        r = _run_static(root)
        assert r.returncode != 0
        assert "vnx bestaatniet" in r.stdout

    def test_fenced_unknown_subcommand_after_prompt_marker_is_flagged(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\n```\n  $ vnx objective vaporize\n```\n")
        r = _run_static(root)
        assert r.returncode != 0
        assert "vnx objective vaporize" in r.stdout

    def test_fenced_known_command_is_clean(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\n```bash\nvnx objective show\nvnx dispatch\n```\n")
        assert _run_static(root).returncode == 0

    def test_prose_line_starting_with_vnx_outside_a_fence_is_ignored(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\nvnx bestaatniet is prose here.\n")
        assert _run_static(root).returncode == 0

    def test_fence_closes_so_later_prose_is_ignored(self, tmp_path):
        root = _make_fabric(tmp_path, FIXTURE_ROLE + "\n```\nvnx dispatch\n```\nvnx bestaatniet\n")
        assert _run_static(root).returncode == 0


class TestConsumerProject:
    def test_project_without_scripts_is_judged_against_the_fabric(self, tmp_path):
        """A consumer carries the synced role but no scripts/. It is checked
        against the fabric the audit belongs to, so a role naming a real fabric
        script is clean and one naming a ghost is flagged."""
        consumer = _make_project(tmp_path, "consumer")
        t0 = consumer / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
        (t0 / "role-orchestrator.md").write_text("Run `python3 scripts/build_t0_state.py`.\n")
        assert _run_static(consumer).returncode == 0
        (t0 / "role-orchestrator.md").write_text("Run `python3 scripts/ghost_tool.py`.\n")
        r = _run_static(consumer)
        assert r.returncode != 0 and "SCRIPT-MISSING" in r.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
