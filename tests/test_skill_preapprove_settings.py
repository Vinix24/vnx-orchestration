#!/usr/bin/env python3
"""Tests for worker-role skill-use pre-approval (dispatch 20260614-skill-preapprove).

Background
----------
Every tmux-interactive lane dispatch spawns a worker in a fresh git worktree.
The worker loads its role skill (e.g. ``database-engineer``) and Claude Code
raises a skill-USE permission prompt. In a DETACHED worker with no human to
answer, that prompt STALLS the dispatch. The fix pre-approves the worker-role
skills via the documented permission-rule grammar.

Mechanism (schema-valid, NOT a guessed no-op key)
-------------------------------------------------
A skill is pre-approved with a ``permissions.allow`` entry of the form
``Skill(<skill-name>)``. This is the exact rule grammar the Claude Code Agent
SDK emits for its ``skills`` option:

    skills:"all"           -> "Skill"          (blanket — intentionally NOT used)
    skills:[a, b, ...]      -> "Skill(a)", "Skill(b)", ...  appended to allowedTools

Verified against the installed Claude Code CLI binary (v2.1.177): the SDK
option handler maps ``h.map((a)=>`Skill(${a})`)`` into ``allowedTools``, which
shares the permission-rule grammar with ``permissions.allow`` in settings.json.

These tests cover three surfaces:
1. The repo ``.claude/settings.json`` (loaded by lane worktrees of this repo).
2. The ``vnx init`` default scaffold template.
3. The ``vnx init`` minimal scaffold template.

Templates are rendered through the REAL ``_render_template`` used by
``vnx init`` — the test does not reimplement Jinja rendering.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_LIB_DIR = REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from vnx_cli.commands.init_cmd import _render_template  # noqa: E402

# The worker roles the lanes use. Each MUST be pre-approved so a detached
# worker never hits the skill-use prompt.
WORKER_ROLES = [
    "database-engineer",
    "backend-developer",
    "frontend-developer",
    "quality-engineer",
    "security-engineer",
    "api-developer",
    "debugger",
]

EXPECTED_RULES = [f"Skill({role})" for role in WORKER_ROLES]

# Rendering context mirrors vnx_cli.commands.init_cmd.vnx_init (engine_root is
# the only token used by settings.json.j2; the Skill rules do not depend on it).
RENDER_CTX = {
    "project_name": "test-project",
    "project_id": "test-project-id",
    "vnx_version": "0.0.0-test",
    "engine_root": "/fake/engine/root",
}

REPO_SETTINGS = REPO_ROOT / ".claude" / "settings.json"
TEMPLATES = {
    "default": REPO_ROOT / "templates" / "init" / "default" / "settings.json.j2",
    "minimal": REPO_ROOT / "templates" / "init" / "minimal" / "settings.json.j2",
}


def _render(template_path: Path) -> dict:
    """Render a settings template via the real init renderer and parse JSON."""
    rendered = _render_template(template_path, RENDER_CTX)
    # Must parse as valid JSON — a stray Jinja artefact (e.g. an un-stripped
    # comment or a leading token) would raise here.
    return json.loads(rendered)


# ---------------------------------------------------------------------------
# Repo settings.json (lane worktree surface)
# ---------------------------------------------------------------------------

class TestRepoSettings:
    def test_repo_settings_is_valid_json(self):
        data = json.loads(REPO_SETTINGS.read_text())
        assert isinstance(data, dict)

    def test_repo_settings_preapproves_all_worker_roles(self):
        data = json.loads(REPO_SETTINGS.read_text())
        allow = data.get("permissions", {}).get("allow", [])
        for rule in EXPECTED_RULES:
            assert rule in allow, f"{rule} missing from repo .claude/settings.json"

    def test_repo_settings_no_blanket_skill_allow(self):
        """Discipline: pre-approve named roles only, never a blanket Skill allow."""
        data = json.loads(REPO_SETTINGS.read_text())
        allow = data.get("permissions", {}).get("allow", [])
        assert "Skill" not in allow
        assert "Skill(*)" not in allow


# ---------------------------------------------------------------------------
# Scaffold templates (fresh pip-install surface)
# ---------------------------------------------------------------------------

class TestScaffoldTemplates:
    @pytest.mark.parametrize("name", sorted(TEMPLATES))
    def test_template_renders_to_valid_json(self, name):
        data = _render(TEMPLATES[name])
        assert isinstance(data, dict)
        assert "permissions" in data

    @pytest.mark.parametrize("name", sorted(TEMPLATES))
    def test_template_preapproves_all_worker_roles(self, name):
        data = _render(TEMPLATES[name])
        allow = data.get("permissions", {}).get("allow", [])
        for rule in EXPECTED_RULES:
            assert rule in allow, f"{rule} missing from {name} scaffold template"

    @pytest.mark.parametrize("name", sorted(TEMPLATES))
    def test_template_preserves_existing_allows(self, name):
        """Pre-approval is additive — pre-existing Bash allows must remain."""
        data = _render(TEMPLATES[name])
        allow = data.get("permissions", {}).get("allow", [])
        assert "Bash(git *)" in allow
        assert "Bash(vnx *)" in allow

    @pytest.mark.parametrize("name", sorted(TEMPLATES))
    def test_template_no_blanket_skill_allow(self, name):
        data = _render(TEMPLATES[name])
        allow = data.get("permissions", {}).get("allow", [])
        assert "Skill" not in allow
        assert "Skill(*)" not in allow


# ---------------------------------------------------------------------------
# Cross-check: every pre-approved role maps to a real shipped skill
# ---------------------------------------------------------------------------

class TestPreapprovalMatchesShippedSkills:
    """A typo'd skill name would silently no-op (never matches an invocation).

    Guard against drift: each pre-approved ``Skill(<role>)`` must reference a
    skill that actually ships in ``.claude/skills/<role>/SKILL.md`` and whose
    frontmatter ``name:`` equals ``<role>`` (the identifier the rule matches).
    """

    @pytest.mark.parametrize("role", WORKER_ROLES)
    def test_skill_md_exists(self, role):
        skill_md = REPO_ROOT / ".claude" / "skills" / role / "SKILL.md"
        assert skill_md.is_file(), f"no SKILL.md for pre-approved role {role}"

    @pytest.mark.parametrize("role", WORKER_ROLES)
    def test_skill_frontmatter_name_matches_rule(self, role):
        skill_md = REPO_ROOT / ".claude" / "skills" / role / "SKILL.md"
        name = None
        for line in skill_md.read_text().splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip()
                break
        assert name == role, (
            f"frontmatter name '{name}' != dir '{role}'; "
            f"Skill({role}) rule would not match the skill's invocation name"
        )
