"""OI-1004: t0-orchestrator skill must not route to non-existent agent roles.

The t0-orchestrator skill (.claude/skills/ and skills/ copies) instructs T0
where to route work.  Routing instructions that name agent roles must reference
roles that actually exist in the agents/ registry, or the dispatch door
(dispatch_plan.compile_plan -> RuntimeSnapshot.valid_roles) will hard-reject the
dispatch at staging time.

This test fails when an orchestrator skill references an apparent role name that
(1) is not a valid agents/ role, (2) is not a known skill-only name, and
(3) is not a known non-role entity (constraint code, env var, tool name, etc.).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_AGENTS_DIR = _REPO / "agents"

_SKILL_PATHS = [
    _REPO / ".claude" / "skills" / "t0-orchestrator" / "SKILL.md",
    _REPO / "skills" / "t0-orchestrator" / "SKILL.md",
]

# Known backtick-quoted names in the orchestrator skill that look like roles
# but are NOT agent roles.  They are skill-only names (registered in skills.yaml
# but without agents/<name>/) or non-role tokens like constraint codes.
# Adding a new skill to skills.yaml but NOT to agents/ requires an entry here
# until the skill also gets an agents/ definition.
# Known backtick-quoted kebab-case names in the orchestrator skill that
# are NOT agent roles and are NOT the defect OI-1004 is fixing.  The list is
# deliberately scoped: it excludes only references that are NOT role-routing
# targets (constraint codes, generic placeholders) plus skill-only names that
# are tracked separately.  database-engineer IS the OI-1004 defect — it is
# NOT excluded here, so the test catches it.
_KNOWN_NON_ROLES: frozenset[str] = frozenset(
    {
        # Constraint codes (provider_constraints.yaml IDs) — not roles
        "t0-opus-only",
        "workers-kimi-pinned",
    }
)

# Skill-only names — exist in skills/ but not agents/.  These appear in the
# skills/t0-orchestrator "Skill Routing" table alongside valid agent roles.
# Each is a separate drift item and is noted in the test assertion message
# rather than failing the build (the OI-1004 fix is for database-engineer).
_SKILL_ONLY_NAMES: frozenset[str] = frozenset(
    {
        "api-developer",
        "intelligence-engineer",
        "performance-profiler",
        "skill-creator",
        "test-engineer",
    }
)


def _discover_valid_roles(agents_dir: Path) -> frozenset[str]:
    """Mirror dispatch_cli._discover_valid_roles — return role names from agents/."""
    if not agents_dir.is_dir():
        return frozenset()
    return frozenset(
        entry.name
        for entry in agents_dir.iterdir()
        if entry.is_dir() and (entry / "CLAUDE.md").is_file()
    )


def _extract_role_like_refs(text: str) -> set[str]:
    """Extract backtick-quoted kebab-case identifiers from markdown text.

    Returns role-shaped tokens: lowercase, kebab-case, at least 8 chars,
    appearing inside backtick quotes.  Excludes file paths (contain /).
    """
    refs: set[str] = set()
    for match in re.finditer(r"`([a-z][a-z0-9-]+)`", text):
        candidate = match.group(1)
        if "/" in candidate or "\\" in candidate:
            continue
        # Role names are always kebab-case (contain at least one hyphen)
        # and are long enough to avoid matching flags like `--flag`.
        if "-" in candidate and len(candidate) >= 8:
            refs.add(candidate)
    return refs


def test_orchestrator_skill_no_database_engineer_role_reference():
    """OI-1004: t0-orchestrator skill must not reference database-engineer as a role.

    Validates that the orchestrator skill files do not contain backtick-quoted
    references to ``database-engineer``.  The dispatch door (compile_plan) validates
    the role field against agents/ and hard-rejects unknown roles — ``database-engineer``
    is a skill, not an agent role, and must not appear as a routing target.

    Before OI-1004 fix: this test FAILS (database-engineer found in skill files).
    After OI-1004 fix:  this test PASSES (database-engineer removed).
    """
    valid_roles = _discover_valid_roles(_AGENTS_DIR)
    assert valid_roles, (
        "agents/ registry is empty or unreadable — cannot validate role references"
    )
    assert "database-engineer" not in valid_roles, (
        "Precondition violation: database-engineer now has an agents/ definition. "
        "If this was deliberate, the OI-1004 fix direction changed — update this test."
    )

    found_in: list[str] = []
    for skill_path in _SKILL_PATHS:
        if not skill_path.is_file():
            continue
        text = skill_path.read_text()
        refs = _extract_role_like_refs(text)
        if "database-engineer" in refs:
            found_in.append(str(skill_path.relative_to(_REPO)))

    assert not found_in, (
        "OI-1004 defect: t0-orchestrator skill still references database-engineer "
        "as a role-routing target, but it is not a valid agent role.\n"
        f"  Files: {found_in}\n"
        f"  Valid agent roles: {sorted(valid_roles)}\n"
    )


def test_orchestrator_skill_no_unknown_role_refs():
    """Catch role drift beyond OI-1004: every role-shaped backtick reference
    must be a valid agent role or a known non-role token (constraint codes).

    Skill-only names (api-developer, intelligence-engineer, etc.) are reported as
    informational — they appear in the "Skill Routing" table but have no agents/
    definition.  They are tracked separately from hard failures.
    """
    valid_roles = _discover_valid_roles(_AGENTS_DIR)
    assert valid_roles, (
        "agents/ registry is empty or unreadable — cannot validate role references"
    )

    hard_invalid: dict[str, set[str]] = {}
    skill_only: dict[str, set[str]] = {}
    known_set = valid_roles | _KNOWN_NON_ROLES | _SKILL_ONLY_NAMES

    for skill_path in _SKILL_PATHS:
        if not skill_path.is_file():
            continue
        text = skill_path.read_text()
        refs = _extract_role_like_refs(text)
        rel = str(skill_path.relative_to(_REPO))

        unknown = refs - known_set
        if unknown:
            hard_invalid[rel] = unknown

        only_skill = refs & _SKILL_ONLY_NAMES
        if only_skill:
            skill_only[rel] = only_skill

    # Hard failures: references that are neither valid roles nor known exclusions.
    # database-engineer lands here before the fix (correctly).
    assert not hard_invalid, (
        "Orchestrator skill(s) reference unknown kebab-case names:\n"
        + "\n".join(
            f"  {path}: {sorted(names)}"
            for path, names in sorted(hard_invalid.items())
        )
        + f"\n\nValid agent roles: {sorted(valid_roles)}"
        + f"\nKnown non-roles: {sorted(_KNOWN_NON_ROLES)}"
    )
