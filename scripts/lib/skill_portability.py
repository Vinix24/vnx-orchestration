"""Skill portability detector and two-tree parity (skills-port SP-1).

One owner for the rule that decides whether a skill can be ported to a provider
other than Claude Code. ``validate_skill.py --inventory`` and ``vnx skills sync``
read these functions, so no second copy of the rule exists.

The rules are derived from the five skills that were measured as unsafe on
origin/main (architect, database-engineer, debugger, intelligence-engineer,
security-engineer). The only construct that singles out exactly those five is a
path into the Claude Code user directory (``~/.claude/...``): it points at a
directory that exists only for a Claude Code login, so a port copies a dead path.
The project-relative ``.claude/skills`` and ``.claude/vnx-system`` references are
not a rule: far more skills carry them, and they resolve inside the project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from vnx_skills import iter_skill_dirs

# rule name -> pattern. A rule that matches nothing in the real tree is not added.
RULES: dict[str, re.Pattern[str]] = {
    "claude_home_path": re.compile(r"(?:~|\$HOME|\$\{HOME\})/\.claude(?![A-Za-z0-9_-])"),
}

# Files inside a skill directory that are read as text by the detector.
_TEXT_SUFFIXES = {".md", ".sh", ".py", ".yaml", ".yml", ".txt", ".json", ".toml"}

# "<emoji> Skill actief: name" and "Skill actief: name" are the same announcement.
_ANNOUNCEMENT = re.compile(r"^\W*(Skill actief:.*)$")


@dataclass(frozen=True)
class Finding:
    """One non-portable construct: the rule that matched, its file and line."""

    rule: str
    file: str
    line: int
    text: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "file": self.file, "line": self.line, "text": self.text}


def dead_claude_paths(text: str, file: str = "SKILL.md") -> list[Finding]:
    """Return every non-portable construct in *text*, one finding per rule and line."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in RULES.items():
            if pattern.search(line):
                findings.append(Finding(rule, file, number, line.strip()))
    return findings


def _text_files(skill_dir: Path) -> Iterable[Path]:
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file() and path.suffix in _TEXT_SUFFIXES:
            yield path


def scan_skill(skill_dir: Path) -> list[Finding]:
    """Scan SKILL.md and every text file below *skill_dir*."""
    findings: list[Finding] = []
    for path in _text_files(skill_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # An unreadable file cannot be shown portable: surface it as a finding.
            findings.append(Finding("unreadable_file", str(path.relative_to(skill_dir)), 0, ""))
            continue
        findings.extend(dead_claude_paths(text, str(path.relative_to(skill_dir))))
    return findings


def provider_unsafe_skills(tree: Path) -> list[str]:
    """Sorted names of the skills under *tree* that use a non-portable construct."""
    return sorted(d.name for d in iter_skill_dirs(tree) if scan_skill(d))


def _normalize(text: str) -> str:
    return "\n".join(_ANNOUNCEMENT.sub(r"\1", line) for line in text.splitlines())


def classify_skill_pair(left: Path, right: Path) -> str:
    """Compare two SKILL.md files: ``identical``, ``announcement`` or ``semantic``.

    An unreadable file is ``semantic``: a pair that cannot be read is never shown equal.
    """
    try:
        a = left.read_text(encoding="utf-8")
        b = right.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "semantic"
    if a == b:
        return "identical"
    return "announcement" if _normalize(a) == _normalize(b) else "semantic"


def tree_parity(shipped: Path, project: Path) -> dict:
    """Count and name the skills present in both trees by how far they differ."""
    shipped_names = {d.name: d for d in iter_skill_dirs(shipped)}
    project_names = {d.name: d for d in iter_skill_dirs(project)}
    groups: dict[str, list[str]] = {"identical": [], "announcement": [], "semantic": []}
    for name in sorted(set(shipped_names) & set(project_names)):
        kind = classify_skill_pair(shipped_names[name] / "SKILL.md", project_names[name] / "SKILL.md")
        groups[kind].append(name)
    return {
        "shared": sum(len(v) for v in groups.values()),
        "identical": len(groups["identical"]),
        "announcement_only": len(groups["announcement"]),
        "semantic": len(groups["semantic"]),
        "semantic_names": groups["semantic"],
        "only_shipped": sorted(set(shipped_names) - set(project_names)),
        "only_project": sorted(set(project_names) - set(shipped_names)),
    }
