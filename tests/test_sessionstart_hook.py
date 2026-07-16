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


class TestT0SkillBodyInjection:
    def test_injects_skill_body_when_present(self, tmp_path):
        project = _make_project(tmp_path)
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_BODY)

        out = _run_hook(project / ".claude" / "terminals" / "T0")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "UNIQUE-PLAYBOOK-MARKER-4f8e1c" in ctx
        assert "T0 Master Orchestrator Active" in ctx

    def test_fail_soft_when_skill_missing(self, tmp_path):
        """No `.claude/skills/t0-orchestrator/SKILL.md` at all — the hook
        must still succeed and return valid JSON, just without the body."""
        project = _make_project(tmp_path)

        out = _run_hook(project / ".claude" / "terminals" / "T0")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "T0 Master Orchestrator Active" in ctx
        assert "UNIQUE-PLAYBOOK-MARKER-4f8e1c" not in ctx

    def test_output_is_reasonably_sized(self, tmp_path):
        project = _make_project(tmp_path)
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_BODY)

        out = _run_hook(project / ".claude" / "terminals" / "T0")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        # Sanity bound, not a real product limit: catches an accidental
        # infinite-growth bug (e.g. self-referential injection) long before
        # it could approach any real hook-output ceiling.
        assert len(ctx.encode("utf-8")) < 100_000

    def test_idempotent_across_repeated_invocations(self, tmp_path):
        """Each SessionStart fire (a fresh session, a `/clear`, ...) must
        recompute the same output from disk — nothing accumulates."""
        project = _make_project(tmp_path)
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_BODY)

        t0_dir = project / ".claude" / "terminals" / "T0"
        first = _run_hook(t0_dir)
        second = _run_hook(t0_dir)
        assert first == second


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
