#!/usr/bin/env python3
"""Tests for skill_refinement.py — slice 2 of the rework->skill loop.

Coverage:
- rework-prone role (>0.3) yields a well-formed diff+rationale proposal targeting .claude/skills/
- below-threshold role yields no proposal
- proposals never touch .vnx/skills/
- application requires a separate operator step (all proposals land as status="pending")
"""

import json
import sqlite3
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from skill_refinement import (  # noqa: E402
    REWORK_THRESHOLD,
    _ATTRIBUTION_HEADING,
    _generate_diff,
    _insert_section,
    compute_rework_rates,
    find_rework_prone_roles,
    generate_all_proposals,
    generate_proposal,
    resolve_skill_path,
    write_proposals,
)

# ---- schema helpers ----

_QI_SCHEMA = """
CREATE TABLE dispatch_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    terminal TEXT NOT NULL DEFAULT 'T1',
    track    TEXT NOT NULL DEFAULT 'A',
    role TEXT,
    parent_dispatch TEXT,
    pattern_count INTEGER DEFAULT 0,
    prevention_rule_count INTEGER DEFAULT 0,
    instruction_char_count INTEGER DEFAULT 0,
    outcome_status TEXT,
    UNIQUE (project_id, dispatch_id)
);
CREATE VIEW dispatch_success_by_role AS
SELECT role,
       COUNT(*) AS total_dispatches,
       SUM(CASE WHEN outcome_status='success' THEN 1 ELSE 0 END) AS successes,
       ROUND(AVG(CASE WHEN outcome_status='success' THEN 1.0 ELSE 0.0 END), 3) AS success_rate,
       AVG(pattern_count) AS avg_patterns
FROM dispatch_metadata WHERE outcome_status IS NOT NULL
GROUP BY role ORDER BY total_dispatches DESC;
"""


def _make_qi(tmp_path, rows: List[Tuple]) -> sqlite3.Connection:
    """Create quality_intelligence.db with dispatch_metadata rows.

    Each row: (dispatch_id, project_id, role, outcome_status, parent_dispatch)
    """
    qi = sqlite3.connect(tmp_path / "quality_intelligence.db")
    qi.executescript(_QI_SCHEMA)
    qi.executemany(
        "INSERT INTO dispatch_metadata "
        "(dispatch_id, project_id, role, outcome_status, parent_dispatch) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    qi.commit()
    return qi


def _make_skill(skills_dir: Path, role: str, content: str = None) -> Path:
    """Create .claude/skills/<role>/SKILL.md."""
    skill_dir = skills_dir / role
    skill_dir.mkdir(parents=True, exist_ok=True)
    if content is None:
        content = (
            f"# {role.title()}\n\nA skill description.\n\n"
            f"## Guidelines\n\nDo good work.\n\n"
            f"{_insert_section.__module__}\n\n"
            f"## Skill Activation Announcement\n\nSkill actief: {role}\n"
        )
        # Use a simple, realistic skill file without the module name
        content = (
            f"# {role.title()}\n\nA skill.\n\n"
            f"## Guidelines\n\nDo good work.\n\n"
            f"## Skill Activation Announcement\n\n```\nSkill actief: {role}\n```\n"
        )
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir / "SKILL.md"


_TS = "2026-07-05T12:00:00Z"


# ---- unit: compute_rework_rates ----

def test_compute_rework_rates_basic(tmp_path):
    qi = _make_qi(tmp_path, [
        ("d0", "vnx-dev", "debugger", "success", None),
        ("d1", "vnx-dev", "debugger", "success", None),
        ("d2", "vnx-dev", "debugger", "success", None),
        ("d3", "vnx-dev", "debugger", "success", None),
        ("r0", "vnx-dev", "backend-developer", "success", "d0"),
        ("r1", "vnx-dev", "backend-developer", "success", "d1"),
    ])
    try:
        rates = compute_rework_rates(qi, "vnx-dev")
        assert "debugger" in rates
        assert rates["debugger"]["reworked"] == 2
        assert rates["debugger"]["total"] == 4
        assert rates["debugger"]["rework_rate"] == 0.5
        # backend-developer has 2 dispatches, 0 reworked
        assert rates["backend-developer"]["reworked"] == 0
        assert rates["backend-developer"]["rework_rate"] == 0.0
    finally:
        qi.close()


def test_compute_rework_rates_empty(tmp_path):
    qi = sqlite3.connect(tmp_path / "quality_intelligence.db")
    qi.executescript(_QI_SCHEMA)
    qi.commit()
    try:
        rates = compute_rework_rates(qi, "vnx-dev")
        assert rates == {}
    finally:
        qi.close()


# ---- unit: find_rework_prone_roles ----

def test_find_rework_prone_roles_boundary(tmp_path):
    # debugger exactly AT threshold (3/10 = 0.3): NOT > 0.3, excluded
    # security-engineer above threshold (2/5 = 0.4): included
    rows = (
        [("d%d" % i, "vnx-dev", "debugger", "success", None) for i in range(10)]
        + [("r%d" % i, "vnx-dev", "backend-developer", "success", "d%d" % i) for i in range(3)]
        + [("s%d" % i, "vnx-dev", "security-engineer", "success", None) for i in range(5)]
        + [("sr%d" % i, "vnx-dev", "backend-developer", "success", "s%d" % i) for i in range(2)]
    )
    qi = _make_qi(tmp_path, rows)
    try:
        prone = find_rework_prone_roles(qi, "vnx-dev", threshold=0.3)
        role_names = [r for r, _ in prone]
        assert "debugger" not in role_names      # 0.3 is not > 0.3
        assert "security-engineer" in role_names  # 0.4 > 0.3
    finally:
        qi.close()


def test_find_rework_prone_roles_sorted_by_rate(tmp_path):
    rows = (
        [("a%d" % i, "vnx-dev", "role-a", "success", None) for i in range(5)]
        + [("ar%d" % i, "vnx-dev", "role-b", "success", "a%d" % i) for i in range(4)]  # 4/5=0.8
        + [("b%d" % i, "vnx-dev", "role-c", "success", None) for i in range(10)]
        + [("br%d" % i, "vnx-dev", "role-b", "success", "b%d" % i) for i in range(6)]  # 6/10=0.6
    )
    qi = _make_qi(tmp_path, rows)
    try:
        prone = find_rework_prone_roles(qi, "vnx-dev", threshold=0.3)
        rates = [data["rework_rate"] for _, data in prone]
        assert rates == sorted(rates, reverse=True)
    finally:
        qi.close()


# ---- unit: resolve_skill_path ----

def test_resolve_skill_path_finds_claude_skills(tmp_path):
    _make_skill(tmp_path / ".claude" / "skills", "debugger")
    path = resolve_skill_path(tmp_path, "debugger")
    assert path is not None
    assert ".claude" in path.parts
    # must NOT be under .vnx/skills/
    parts_str = "/".join(path.parts)
    assert ".vnx/skills" not in parts_str


def test_resolve_skill_path_returns_none_if_absent(tmp_path):
    assert resolve_skill_path(tmp_path, "nonexistent-role") is None


def test_resolve_skill_path_ignores_vnx_skills(tmp_path):
    # .vnx/skills/ exists but .claude/skills/ does not
    vnx_dir = tmp_path / ".vnx" / "skills" / "debugger"
    vnx_dir.mkdir(parents=True)
    (vnx_dir / "SKILL.md").write_text("# vnx shipped template\n", encoding="utf-8")

    path = resolve_skill_path(tmp_path, "debugger")
    assert path is None  # .claude/skills/ absent -> None, never returns .vnx/skills/


# ---- unit: generate_proposal ----

def test_generate_proposal_well_formed(tmp_path):
    skill_path = _make_skill(tmp_path / ".claude" / "skills", "debugger")
    proposal = generate_proposal(
        role="debugger",
        rework_rate=0.449,
        reworked_count=5,
        total_dispatches=11,
        skill_path=skill_path,
        project_root=tmp_path,
        generated_at=_TS,
    )
    assert proposal is not None

    # Required fields present and non-empty
    assert proposal["diff"]
    assert proposal["rationale"]
    assert proposal["operator_test"]
    assert proposal["role"] == "debugger"
    assert proposal["rework_rate"] == 0.449
    assert proposal["reworked_count"] == 5
    assert proposal["total_dispatches"] == 11
    assert proposal["status"] == "pending"
    assert proposal["generated_at"] == _TS

    # skill_path targets .claude/skills/ only
    assert ".claude/skills/" in proposal["skill_path"]
    assert ".vnx/skills/" not in proposal["skill_path"]

    # diff is valid unified diff: has ---, +++, @@ lines and added lines
    diff_lines = proposal["diff"].splitlines()
    assert any(l.startswith("---") for l in diff_lines)
    assert any(l.startswith("+++") for l in diff_lines)
    assert any(l.startswith("@@") for l in diff_lines)
    added = [l for l in diff_lines if l.startswith("+") and not l.startswith("+++")]
    assert len(added) > 0

    # rationale references the role and threshold
    assert "debugger" in proposal["rationale"]
    assert "30%" in proposal["rationale"] or "0.3" in proposal["rationale"] or "44%" in proposal["rationale"]

    # operator_test references the apply command
    assert "vnx learning skill-refine" in proposal["operator_test"]


def test_generate_proposal_idempotent(tmp_path):
    # Skill already has the attribution heading — do not re-propose
    content = f"# Debugger\n\n{_ATTRIBUTION_HEADING}\n\n## Skill Activation Announcement\n"
    skill_path = _make_skill(tmp_path / ".claude" / "skills", "debugger", content=content)
    proposal = generate_proposal(
        role="debugger",
        rework_rate=0.449,
        reworked_count=5,
        total_dispatches=11,
        skill_path=skill_path,
        project_root=tmp_path,
        generated_at=_TS,
    )
    assert proposal is None


def test_generate_proposal_missing_skill_returns_none(tmp_path):
    missing = tmp_path / ".claude" / "skills" / "ghost" / "SKILL.md"
    proposal = generate_proposal(
        role="ghost",
        rework_rate=0.5,
        reworked_count=5,
        total_dispatches=10,
        skill_path=missing,
        project_root=tmp_path,
        generated_at=_TS,
    )
    assert proposal is None


# ---- integration: generate_all_proposals ----

def test_rework_prone_role_yields_proposal(tmp_path):
    """debugger at 4/7 = 0.571 is above threshold -> proposal generated targeting .claude/skills/."""
    _make_skill(tmp_path / ".claude" / "skills", "debugger")
    rows = (
        [("d%d" % i, "vnx-dev", "debugger", "success", None) for i in range(7)]
        + [("r%d" % i, "vnx-dev", "backend-developer", "success", "d%d" % i) for i in range(4)]
    )
    qi = _make_qi(tmp_path, rows)
    try:
        proposals = generate_all_proposals(qi, "vnx-dev", tmp_path, threshold=0.3, generated_at=_TS)
    finally:
        qi.close()

    assert len(proposals) >= 1
    proposal = next(p for p in proposals if p["role"] == "debugger")
    assert proposal["diff"] != ""
    assert ".claude/skills/" in proposal["skill_path"]
    assert proposal["rationale"] != ""
    assert proposal["operator_test"] != ""
    assert proposal["status"] == "pending"


def test_below_threshold_role_yields_no_proposal(tmp_path):
    """security-engineer at 1/5 = 0.2 is below threshold -> no proposal."""
    _make_skill(tmp_path / ".claude" / "skills", "security-engineer")
    rows = (
        [("s%d" % i, "vnx-dev", "security-engineer", "success", None) for i in range(5)]
        + [("sr0", "vnx-dev", "backend-developer", "success", "s0")]
    )
    qi = _make_qi(tmp_path, rows)
    try:
        proposals = generate_all_proposals(qi, "vnx-dev", tmp_path, threshold=0.3, generated_at=_TS)
    finally:
        qi.close()

    roles = [p["role"] for p in proposals]
    assert "security-engineer" not in roles


def test_proposals_never_touch_vnx_skills(tmp_path):
    """Even with a .vnx/skills/ sibling present, proposals only target .claude/skills/."""
    _make_skill(tmp_path / ".claude" / "skills", "debugger")

    vnx_dir = tmp_path / ".vnx" / "skills" / "debugger"
    vnx_dir.mkdir(parents=True)
    (vnx_dir / "SKILL.md").write_text("# debugger (shipped template)\n", encoding="utf-8")

    rows = (
        [("d%d" % i, "vnx-dev", "debugger", "success", None) for i in range(5)]
        + [("r%d" % i, "vnx-dev", "backend-developer", "success", "d%d" % i) for i in range(3)]
    )
    qi = _make_qi(tmp_path, rows)
    try:
        proposals = generate_all_proposals(qi, "vnx-dev", tmp_path, threshold=0.3, generated_at=_TS)
    finally:
        qi.close()

    for p in proposals:
        assert ".vnx/skills/" not in p["skill_path"], (
            f"proposal for {p['role']} targeted .vnx/skills/: {p['skill_path']}"
        )
        assert ".vnx" not in p["skill_path"].split("/")[0]


def test_application_requires_operator_step(tmp_path):
    """All proposals have status='pending' and the skill file is NOT mutated."""
    _make_skill(tmp_path / ".claude" / "skills", "debugger")
    rows = (
        [("d%d" % i, "vnx-dev", "debugger", "success", None) for i in range(5)]
        + [("r%d" % i, "vnx-dev", "backend-developer", "success", "d%d" % i) for i in range(3)]
    )
    qi = _make_qi(tmp_path, rows)
    try:
        proposals = generate_all_proposals(qi, "vnx-dev", tmp_path, threshold=0.3, generated_at=_TS)
    finally:
        qi.close()

    assert proposals, "expected at least one proposal for debugger at 0.6 rework rate"

    for p in proposals:
        assert p["status"] == "pending", f"proposal for {p['role']} was auto-applied (status != pending)"

    # Verify the skill file is NOT mutated by generate_all_proposals
    skill_path = tmp_path / ".claude" / "skills" / "debugger" / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")
    assert _ATTRIBUTION_HEADING not in content, "generate_all_proposals must NOT modify skill files"


def test_no_skill_file_skips_role(tmp_path):
    """A rework-prone role with no .claude/skills/ entry is silently skipped."""
    # debugger has no skill file
    rows = (
        [("d%d" % i, "vnx-dev", "debugger", "success", None) for i in range(5)]
        + [("r%d" % i, "vnx-dev", "backend-developer", "success", "d%d" % i) for i in range(3)]
    )
    qi = _make_qi(tmp_path, rows)
    try:
        proposals = generate_all_proposals(qi, "vnx-dev", tmp_path, threshold=0.3, generated_at=_TS)
    finally:
        qi.close()

    # No crash, and no proposals (the skill file didn't exist)
    assert isinstance(proposals, list)
    roles = [p["role"] for p in proposals]
    assert "debugger" not in roles


# ---- unit: write_proposals ----

def test_write_proposals_atomic(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    proposals = [{"id": "test-1", "role": "debugger", "status": "pending"}]
    output_path = state_dir / "pending_skill_refinements.json"

    write_proposals(proposals, output_path, threshold=0.3, generated_at=_TS)

    assert output_path.exists()
    # Atomic write must not leave .tmp behind
    assert not (state_dir / "pending_skill_refinements.json.tmp").exists()

    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["threshold"] == 0.3
    assert data["generated_at"] == _TS
    assert len(data["proposals"]) == 1
    assert data["proposals"][0]["id"] == "test-1"


def test_write_proposals_empty_list(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    output_path = state_dir / "pending_skill_refinements.json"

    write_proposals([], output_path, threshold=0.3, generated_at=_TS)

    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["proposals"] == []


def test_write_proposals_overwrites(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    output_path = state_dir / "pending_skill_refinements.json"

    write_proposals([{"id": "v1"}], output_path, threshold=0.3, generated_at=_TS)
    write_proposals([{"id": "v2"}, {"id": "v3"}], output_path, threshold=0.3, generated_at=_TS)

    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(data["proposals"]) == 2
    assert data["proposals"][0]["id"] == "v2"


# ---- unit: _insert_section ----

def test_insert_section_before_activation(tmp_path):
    original = "# Title\n\nContent.\n\n## Skill Activation Announcement\n\nActivation text.\n"
    result = _insert_section(original, "## NEW SECTION\n\nNew content.\n")
    assert "## NEW SECTION" in result
    assert result.index("## NEW SECTION") < result.index("## Skill Activation Announcement")


def test_insert_section_at_end_when_no_activation(tmp_path):
    original = "# Title\n\nContent.\n"
    result = _insert_section(original, "## NEW SECTION\n\nNew content.\n")
    assert result.endswith("## NEW SECTION\n\nNew content.\n")


# ---- unit: _generate_diff ----

def test_generate_diff_valid_unified_diff(tmp_path):
    original = "line1\nline2\nline3\n"
    new = "line1\nline2\nNEW LINE\nline3\n"
    diff = _generate_diff(original, new, ".claude/skills/role/SKILL.md")
    lines = diff.splitlines()
    assert any(l.startswith("---") for l in lines)
    assert any(l.startswith("+++") for l in lines)
    assert any(l.startswith("@@") for l in lines)
    assert any(l.startswith("+NEW LINE") for l in lines)


def test_generate_diff_identical_content_is_empty(tmp_path):
    content = "same\ncontent\n"
    diff = _generate_diff(content, content, ".claude/skills/role/SKILL.md")
    assert diff == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
