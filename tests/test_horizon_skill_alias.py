"""tests/test_horizon_skill_alias.py — pm -> horizon skill rename + alias (D2).

Verifies D2 of claudedocs/2026-07-05-horizon-planning-module-PLAN.md:

- `.claude/skills/horizon/SKILL.md` exists and its frontmatter `name:` is `horizon`
  (the Claude Code skill loader keys a skill's identity off this field, not just the
  directory name).
- `.claude/skills/pm/SKILL.md` still exists as a backward-compat alias (frontmatter
  `name: pm`) so `/pm` and any surviving `@pm` delegation reference keep resolving.
- The pm alias is a thin pointer, not a duplicate: the substantive skill body (the
  future-state lifecycle, the plan-gate section, etc.) lives ONLY under horizon/, and
  the pm stub redirects to it.
- `.claude/skills` lists both `horizon` and `pm` (whatever a caller scans for skills in
  this repo, both names are discoverable).
- No dangling `@pm` reference: every SKILL.md that still mentions `@pm` is safe only
  because the alias file exists — this test would fail the moment someone deletes the
  pm/ pointer while `@pm` references remain in prose.
- `planner`'s delegation prose was reconciled to name `@horizon` (not left as a bare,
  unexplained `@pm`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
HORIZON_SKILL_MD = SKILLS_DIR / "horizon" / "SKILL.md"
PM_SKILL_MD = SKILLS_DIR / "pm" / "SKILL.md"
PLANNER_SKILL_MD = SKILLS_DIR / "planner" / "SKILL.md"
FEATUREPLAN_KICKOFF_SKILL_MD = SKILLS_DIR / "featureplan-kickoff" / "SKILL.md"


def _read_frontmatter(path: Path) -> dict:
    """Parse the YAML frontmatter block of a SKILL.md file (best-effort)."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no leading YAML frontmatter block"
    _, _, rest = text.partition("---\n")
    fm_text, sep, _ = rest.partition("\n---")
    assert sep, f"{path} frontmatter block is not closed with a second '---'"
    return yaml.safe_load(fm_text) or {}


def _list_skill_dirs() -> list[str]:
    return sorted(p.name for p in SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))


# ---------------------------------------------------------------------------
# horizon exists, is the canonical skill
# ---------------------------------------------------------------------------

class TestHorizonSkillExists:
    def test_horizon_skill_md_exists(self):
        assert HORIZON_SKILL_MD.exists(), "Renamed skill .claude/skills/horizon/SKILL.md is missing"

    def test_horizon_frontmatter_name_is_horizon(self):
        fm = _read_frontmatter(HORIZON_SKILL_MD)
        assert fm.get("name") == "horizon", f"horizon/SKILL.md frontmatter name={fm.get('name')!r}, expected 'horizon'"

    def test_horizon_body_carries_the_substantive_lifecycle_content(self):
        text = HORIZON_SKILL_MD.read_text(encoding="utf-8")
        assert "The Horizon lifecycle you drive" in text
        assert "plan-first gate" in text.lower()
        assert "vnx horizon" in text, "horizon skill body should reference the vnx horizon command surface"

    def test_horizon_body_uses_vnx_horizon_not_bare_planning_cli(self):
        text = HORIZON_SKILL_MD.read_text(encoding="utf-8")
        # The shipped command surface is `vnx horizon` (D1, #1014); the skill body
        # should not still be telling operators to invoke the internal script directly.
        assert "planning_cli.py objective add" not in text
        assert "planning_cli.py deliverable add" not in text
        assert "planning_cli.py plan-gate run" not in text
        assert "planning_cli.py objective drift" not in text


# ---------------------------------------------------------------------------
# pm still resolves (alias), but as a thin pointer, not a duplicate
# ---------------------------------------------------------------------------

class TestPmAliasResolves:
    def test_pm_skill_md_still_exists(self):
        assert PM_SKILL_MD.exists(), "pm/SKILL.md must survive as a backward-compat alias"

    def test_pm_frontmatter_name_is_pm(self):
        fm = _read_frontmatter(PM_SKILL_MD)
        assert fm.get("name") == "pm", f"pm/SKILL.md frontmatter name={fm.get('name')!r}, expected 'pm'"

    def test_pm_is_a_pointer_not_a_duplicate(self):
        """The pm stub must redirect to @horizon, not carry its own copy of the lifecycle."""
        text = PM_SKILL_MD.read_text(encoding="utf-8")
        assert "@horizon" in text
        assert "renamed" in text.lower()
        # The substantive section headers from the original pm body must NOT be
        # duplicated here -- they live only under horizon/ now.
        assert "The Horizon lifecycle you drive" not in text
        assert "The future-state lifecycle you drive" not in text
        assert "## Tiered review gates" not in text

    def test_pm_and_horizon_are_not_byte_identical(self):
        """A real rename+alias, not two copies of the same file."""
        assert PM_SKILL_MD.read_text(encoding="utf-8") != HORIZON_SKILL_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Both names are discoverable in the skills directory
# ---------------------------------------------------------------------------

class TestSkillDirectoryListsBoth:
    def test_horizon_and_pm_both_present_on_disk(self):
        names = _list_skill_dirs()
        assert "horizon" in names, f"'horizon' missing from {SKILLS_DIR}: {names}"
        assert "pm" in names, f"'pm' missing from {SKILLS_DIR}: {names}"


# ---------------------------------------------------------------------------
# No dangling @pm references
# ---------------------------------------------------------------------------

class TestNoDanglingPmReferences:
    def test_every_at_pm_reference_is_covered_by_the_alias(self):
        """Any surviving `@pm` mention anywhere in .claude/skills is only safe because
        the pm/ alias file exists. If the alias were ever deleted, this test documents
        exactly why that would be a breaking change."""
        offenders = []
        for skill_md in SKILLS_DIR.glob("*/SKILL.md"):
            text = skill_md.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"@pm\b", text):
                offenders.append(skill_md)
        if offenders:
            assert PM_SKILL_MD.exists(), (
                f"@pm is referenced in {offenders} but .claude/skills/pm/SKILL.md (the alias) is missing"
            )

    def test_planner_delegation_prose_names_horizon(self):
        text = PLANNER_SKILL_MD.read_text(encoding="utf-8")
        assert "@horizon" in text, "planner/SKILL.md should name @horizon as its delegator, not a bare @pm"


# ---------------------------------------------------------------------------
# Terminology reconcile: planner + featureplan-kickoff prose
# ---------------------------------------------------------------------------

class TestTerminologyReconciled:
    @pytest.mark.parametrize("path", [PLANNER_SKILL_MD, FEATUREPLAN_KICKOFF_SKILL_MD])
    def test_vnx_horizon_command_surface_named(self, path):
        text = path.read_text(encoding="utf-8")
        assert "vnx horizon" in text, f"{path} should reference the vnx horizon command surface"

    @pytest.mark.parametrize("path", [PLANNER_SKILL_MD, FEATUREPLAN_KICKOFF_SKILL_MD])
    def test_objective_alias_still_documented(self, path):
        """objective/deliverable stay valid as aliases -- the reconcile documents them,
        it does not delete the backward-compat CLI surface."""
        text = path.read_text(encoding="utf-8")
        assert "vnx objective" in text, f"{path} should still document the vnx objective alias"
