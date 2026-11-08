"""Portability detector (skills-port SP-1): rules, lines, tree scan and two-tree parity."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import skill_portability as sp  # noqa: E402

UNSAFE = ["architect", "database-engineer", "debugger", "intelligence-engineer", "security-engineer"]


def _skill(tree: Path, name: str, body: str, extra: dict[str, str] | None = None) -> Path:
    d = tree / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    for rel, text in (extra or {}).items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return d


def test_home_claude_path_is_flagged_with_rule_and_line():
    text = "# Title\n\nline three\nConsult memory in ~/.claude/projects/x/MEMORY.md now\n"
    findings = sp.dead_claude_paths(text)
    assert [(f.rule, f.line, f.file) for f in findings] == [("claude_home_path", 4, "SKILL.md")]
    assert "~/.claude/projects" in findings[0].text


@pytest.mark.parametrize("line", ["read $HOME/.claude/x", "read ${HOME}/.claude/x", "see ~/.claude"])
def test_home_variants_are_flagged(line):
    assert [f.rule for f in sp.dead_claude_paths(line)] == ["claude_home_path"]


@pytest.mark.parametrize(
    "line",
    [
        "@.claude/skills/x/template.md",
        "see .claude/vnx-system/scripts/y.py",
        "https://code.claude.com/docs/en/goal",
    ],
)
def test_project_relative_and_unrelated_paths_are_not_flagged(line):
    assert sp.dead_claude_paths(line) == []


def test_tree_scan_names_exactly_the_unsafe_skills(tmp_path):
    tree = tmp_path / "skills"
    for name in UNSAFE:
        _skill(tree, name, "---\nname: x\n---\nuse ~/.claude/projects/p/MEMORY.md\n")
    _skill(tree, "backend-developer", "clean body\n@.claude/skills/backend-developer/template.md\n")
    _skill(tree, "reviewer", "clean body\n")
    assert sp.provider_unsafe_skills(tree) == UNSAFE


def test_a_path_in_a_reference_file_flags_the_skill_with_that_file(tmp_path):
    d = _skill(tmp_path, "debugger", "clean\n", {"references/notes.md": "a\nb\nsee ~/.claude/x\n"})
    findings = sp.scan_skill(d)
    assert [(f.file, f.line) for f in findings] == [("references/notes.md", 3)]


def test_unreadable_file_is_a_finding_not_a_pass(tmp_path):
    d = _skill(tmp_path, "weird", "clean\n")
    (d / "bad.md").write_bytes(b"\xff\xfe\x00\xc3\x28")
    findings = sp.scan_skill(d)
    assert [(f.rule, f.file) for f in findings] == [("unreadable_file", "bad.md")]
    assert sp.provider_unsafe_skills(tmp_path) == ["weird"]


def test_classify_identical_announcement_semantic_and_unreadable(tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("x\n\U0001F527 Skill actief: foo\ny\n", encoding="utf-8")
    b.write_text("x\n\U0001F527 Skill actief: foo\ny\n", encoding="utf-8")
    assert sp.classify_skill_pair(a, b) == "identical"
    b.write_text("x\nSkill actief: foo\ny\n", encoding="utf-8")
    assert sp.classify_skill_pair(a, b) == "announcement"
    b.write_text("x\nSkill actief: foo\nchanged\n", encoding="utf-8")
    assert sp.classify_skill_pair(a, b) == "semantic"
    b.write_text("x\nSkill actief: bar\ny\n", encoding="utf-8")
    assert sp.classify_skill_pair(a, b) == "semantic"
    assert sp.classify_skill_pair(a, tmp_path / "missing.md") == "semantic"


def test_tree_parity_counts_and_one_sided_names(tmp_path):
    shipped, project = tmp_path / "s", tmp_path / "p"
    _skill(shipped, "same", "body\n")
    _skill(project, "same", "body\n")
    _skill(shipped, "ann", "\U0001F527 Skill actief: ann\n")
    _skill(project, "ann", "Skill actief: ann\n")
    _skill(shipped, "sem", "one\n")
    _skill(project, "sem", "two\n")
    _skill(shipped, "ship-only", "x\n")
    _skill(project, "proj-only", "x\n")
    parity = sp.tree_parity(shipped, project)
    assert (parity["shared"], parity["identical"], parity["announcement_only"], parity["semantic"]) == (3, 1, 1, 1)
    assert parity["semantic_names"] == ["sem"]
    assert parity["only_shipped"] == ["ship-only"]
    assert parity["only_project"] == ["proj-only"]
