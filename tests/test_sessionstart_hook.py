#!/usr/bin/env python3
"""Tests for `hooks/sessionstart.sh` — the fleet-wide SessionStart hook.

Dispatch-ID: 20260716-ff-1174-startup-import

Finding 0 (live smoke test, 2026-07-16): T0's Mandatory Startup step used to
reach the (intentionally non-model-invocable) t0-orchestrator SKILL.md body
via a CLAUDE.md `@`-import. That import resolves outside the T0 terminal
directory, and Claude Code classifies it as an "external CLAUDE.md file
import" — a fresh autonomous T0 spawn hits an interactive trust prompt with
nobody to answer it, and hangs.

The fix moves the delivery into this hook instead: for a T0 session, it reads
`.claude/skills/t0-orchestrator/SKILL.md` and returns its body as
`additionalContext`, before the model ever sees a prompt — no import, no
Skill-tool call, so no trust prompt is possible. This is deployed fleet-wide
via `vnx init`/`bootstrap_hooks` (`.claude/hooks/sessionstart.sh`), so the fix
lands everywhere that syncs, with no per-project hand-config.

Isolation: every test builds a throwaway tmp_path project and invokes the
hook as a subprocess with a faked `cwd`; nothing here touches this repo's own
`.claude/` state.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "sessionstart.sh"

SKILL_BODY = """\
---
name: t0-orchestrator
description: test skill
user-invocable: true
disable-model-invocation: true
allowed-tools: [Read]
---

# T0 Orchestrator

You are the orchestration authority for VNX. UNIQUE-PLAYBOOK-MARKER-4f8e1c.
"""


def _make_project(tmp_path: Path, name: str = "project") -> Path:
    root = tmp_path / name
    (root / ".vnx").mkdir(parents=True)
    for term in ("T0", "T1", "T2", "T3"):
        (root / ".claude" / "terminals" / term).mkdir(parents=True)
    return root


def _run_hook(cwd: Path):
    r = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, cwd=str(cwd),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def fabric(tmp_path_factory) -> Path:
    """A tmp copy of the fabric (hooks/ + scripts/), so step (a) of the playbook
    resolver points at <fabric>/skills and not at this checkout's own skills."""
    root = tmp_path_factory.mktemp("fabric") / "fab"
    shutil.copytree(
        REPO / "scripts", root / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv"),
    )
    (root / "hooks").mkdir()
    shutil.copy2(HOOK, root / "hooks" / "sessionstart.sh")
    return root


def _env(home: Path) -> dict:
    """Bare environment: a tmp HOME, no VNX_HOME, never the real ~/.vnx-system."""
    return {"PATH": os.environ["PATH"], "HOME": str(home)}


def _run_in_fabric(fabric: Path, cwd: Path, home: Path) -> dict:
    r = subprocess.run(
        ["bash", str(fabric / "hooks" / "sessionstart.sh")],
        capture_output=True, text=True, cwd=str(cwd), env=_env(home), timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def _put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _body_of(ctx: str) -> str:
    # The hook's framing adds a trailing newline; $(cat) already stripped the file's.
    return ctx.split("\n---\n", 1)[1].rstrip("\n")


@pytest.fixture
def world(fabric, tmp_path):
    """project + empty HOME + a fabric whose skills/ is cleaned before each test."""
    shutil.rmtree(fabric / "skills", ignore_errors=True)
    home = tmp_path / "home"
    home.mkdir()
    project = _make_project(tmp_path)
    return {"fabric": fabric, "home": home, "project": project,
            "cwd": project / ".claude" / "terminals" / "T0"}


def _fabric_skill(w) -> Path:
    return w["fabric"] / "skills" / "t0-orchestrator" / "SKILL.md"


def _installed_skill(w) -> Path:
    return w["home"] / ".vnx-system" / "current" / "skills" / "t0-orchestrator" / "SKILL.md"


def _legacy_skill(w) -> Path:
    return w["project"] / ".claude" / "skills" / "t0-orchestrator" / "SKILL.md"


class TestT0SkillBodyInjection:
    def test_injects_fabric_source_over_stale_legacy_copy(self, world):
        fabric_md = _put(_fabric_skill(world), SKILL_BODY + "FABRIC-SOURCE\n")
        _put(_legacy_skill(world), SKILL_BODY + "STALE-LEGACY-COPY\n")

        ctx = _run_in_fabric(world["fabric"], world["cwd"], world["home"])
        assert _body_of(ctx) == fabric_md.read_text().rstrip("\n")
        assert "STALE-LEGACY-COPY" not in ctx
        assert "T0 Master Orchestrator Active" in ctx
        assert f"(source: fabric)" in ctx
        assert "DEGRADED" not in ctx

    def test_installed_only_is_not_degraded(self, world):
        installed = _put(_installed_skill(world), SKILL_BODY + "INSTALLED\n")

        ctx = _run_in_fabric(world["fabric"], world["cwd"], world["home"])
        assert _body_of(ctx) == installed.read_text().rstrip("\n")
        assert f"{installed.resolve()} (source: installed)" in ctx
        assert "DEGRADED" not in ctx

    def test_project_only_is_degraded(self, world):
        legacy = _put(_legacy_skill(world), SKILL_BODY + "LEGACY\n")

        ctx = _run_in_fabric(world["fabric"], world["cwd"], world["home"])
        assert _body_of(ctx) == legacy.read_text().rstrip("\n")
        assert f"{legacy.resolve()} (source: legacy)" in ctx
        assert "DEGRADED" in ctx

    def test_everything_missing_is_loud_and_not_fatal(self, world):
        ctx = _run_in_fabric(world["fabric"], world["cwd"], world["home"])
        assert "UNIQUE-PLAYBOOK-MARKER-4f8e1c" not in ctx
        assert "\n---\n" not in ctx
        line = next(l for l in ctx.splitlines() if "T0 PLAYBOOK SOURCE MISSING" in l)
        for step in ("fabric:", "vnx_home: not set", "installed:", "project:", "legacy:"):
            assert step in line
        assert "T0 Master Orchestrator Active" in ctx

    def test_hook_path_equals_resolver_path(self, world):
        _put(_installed_skill(world), SKILL_BODY)
        _put(_legacy_skill(world), SKILL_BODY + "LEGACY\n")
        sys.path.insert(0, str(world["fabric"] / "scripts" / "lib"))
        try:
            import t0_playbook
            expected = t0_playbook.resolve(
                world["project"], scripts_root=world["fabric"] / "scripts",
                vnx_home="", home=str(world["home"]),
            )
        finally:
            sys.path.remove(str(world["fabric"] / "scripts" / "lib"))
            sys.modules.pop("t0_playbook", None)
        ctx = _run_in_fabric(world["fabric"], world["cwd"], world["home"])
        assert expected.state == "ok"
        assert f"{expected.path} (source: {expected.source_id})" in ctx

    def test_output_is_reasonably_sized(self, world):
        _put(_fabric_skill(world), SKILL_BODY)
        ctx = _run_in_fabric(world["fabric"], world["cwd"], world["home"])
        # Sanity bound: catches self-referential injection growing without limit.
        assert len(ctx.encode("utf-8")) < 100_000

    def test_idempotent_across_repeated_invocations(self, world):
        _put(_fabric_skill(world), SKILL_BODY)
        first = _run_in_fabric(world["fabric"], world["cwd"], world["home"])
        second = _run_in_fabric(world["fabric"], world["cwd"], world["home"])
        assert first == second

    def test_repo_hook_injects_repo_skill_not_legacy_copy(self, tmp_path):
        """The real hook in this checkout: its fabric step is <repo>/skills."""
        project = _make_project(tmp_path)
        _put(project / ".claude" / "skills" / "t0-orchestrator" / "SKILL.md",
             SKILL_BODY + "STALE-LEGACY-COPY\n")
        r = subprocess.run(
            ["bash", str(HOOK)], capture_output=True, text=True, timeout=60,
            cwd=str(project / ".claude" / "terminals" / "T0"), env=_env(tmp_path),
        )
        assert r.returncode == 0, r.stdout + r.stderr
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        repo_md = (REPO / "skills" / "t0-orchestrator" / "SKILL.md").read_text()
        assert _body_of(ctx) == repo_md.rstrip("\n")


class TestOtherTerminalsUnaffected:
    def test_t1_terminal_has_no_skill_body_injection(self, tmp_path):
        project = _make_project(tmp_path)
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_BODY)

        out = _run_hook(project / ".claude" / "terminals" / "T1")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "T1 Worker (Track A) Active" in ctx
        assert "UNIQUE-PLAYBOOK-MARKER-4f8e1c" not in ctx


class TestHookContextContent:
    """The hook's own text (not the injected skill body) carries references
    that must stay current. These tests verify the corrected references
    without coupling to the exact skill body content."""

    def test_skills_registry_path_is_correct(self, tmp_path):
        """Line 142 + line 150: both references point to skills/skills.yaml,
        not the non-existent .vnx/skills/skills.yaml."""
        project = _make_project(tmp_path)

        out = _run_hook(project / ".claude" / "terminals" / "T0")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "skills/skills.yaml" in ctx
        assert ".vnx/skills/skills.yaml" not in ctx

    def test_core_skills_list_includes_horizon_panel_fabric_reference(self, tmp_path):
        """Line 141: the core skills list includes the skills T0 actually
        routes to: horizon, panel, fabric-reference (added 2026-07-31)."""
        project = _make_project(tmp_path)

        out = _run_hook(project / ".claude" / "terminals" / "T0")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "@horizon" in ctx
        assert "@panel" in ctx
        assert "@fabric-reference" in ctx

    def test_operator_only_skills_are_not_advertised_as_invocable(self, tmp_path):
        """OI-656: @architect (and @t0-orchestrator) carry
        disable-model-invocation: true — advertising them in the same flat
        'Core skills' list as invocable skills invites an autonomous T0 to
        invoke them and hit 'Unknown skill'. The hook must keep the two
        groups apart: architect/t0-orchestrator only under an
        operator-only marker, never in the model-invocable list."""
        project = _make_project(tmp_path)

        out = _run_hook(project / ".claude" / "terminals" / "T0")
        ctx = out["hookSpecificOutput"]["additionalContext"]

        invocable_marker = "Model-invocable skills:"
        operator_marker = "Operator-only skills"
        assert invocable_marker in ctx, "invocable-skills group must be present"
        assert operator_marker in ctx, "operator-only group must be present"

        invocable_line = next(
            l for l in ctx.splitlines() if l.startswith("Model-invocable skills:")
        )
        # @architect must not be presented as model-invocable.
        assert "@architect" not in invocable_line
        assert "@t0-orchestrator" not in invocable_line
        # Both must still be present somewhere (so T0 knows they exist).
        operator_line = next(
            l for l in ctx.splitlines() if l.startswith("Operator-only skills")
        )
        assert "@architect" in operator_line
        assert "@t0-orchestrator" in operator_line

    def test_old_pin_language_is_absent(self):
        """Sonnet-pinned and VNX_OVERRIDE_WORKERS_KIMI_PINNED must not appear
        in the T0 role or skill files after the free-per-dispatch
        modernisation (PR #1257). Conversely, pin_semantics and the free
        per-dispatch choice wording must be present.

        This test reads the real repo files, not a mock fixture — it fails
        against origin/main where the old pin language still lives."""
        skill_md = REPO / ".claude" / "skills" / "t0-orchestrator" / "SKILL.md"
        role_md = REPO / ".claude" / "terminals" / "T0" / "role-orchestrator.md"

        skill_text = skill_md.read_text()
        role_text = role_md.read_text()
        combined = skill_text + role_text

        # Old pin language must be gone from both files.
        assert "Sonnet-pinned" not in combined
        assert "VNX_OVERRIDE_WORKERS_KIMI_PINNED" not in combined

        # New pin semantics must be present.
        assert "pin_semantics" in combined
        assert "per-dispatch" in combined


class TestNonVnxDirectory:
    def test_exits_silently_outside_a_terminal_directory(self, tmp_path):
        r = subprocess.run(
            ["bash", str(HOOK)],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert r.returncode == 0
        assert r.stdout.strip() == "{}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
