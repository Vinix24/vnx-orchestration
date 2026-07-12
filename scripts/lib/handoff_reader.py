#!/usr/bin/env python3
"""Repo-level reader for the T0 context-rotation handoff.md contract.

Parses the SAME shape scripts/lib/context_rotation.write_t0_handoff writes:
frontmatter (context, project, date, branch) + `## Waar we middenin zitten` /
`## State` / `## Next steps` sections. This is the "missing repo-level
handoff reader" the rev-3 plan builds — `vnx handoff show` (vnx_cli/commands/
handoff.py) is the CLI entry point a freshly-respawned T0 runs to resume.

Full contract: docs/operations/CONTEXT_ROTATION.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class HandoffBriefing:
    context: str = ""
    project: str = ""
    date: str = ""
    branch: str = ""
    waar_we_middenin_zitten: str = ""
    state: str = ""
    next_steps: str = ""
    raw: str = ""


def _parse_frontmatter(text: str) -> Dict[str, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    fields: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _parse_sections(text: str) -> Dict[str, str]:
    body = _FRONTMATTER_RE.sub("", text, count=1)
    matches = list(_SECTION_RE.finditer(body))
    sections: Dict[str, str] = {}
    for i, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[title] = body[start:end].strip()
    return sections


def parse_handoff(text: str) -> HandoffBriefing:
    """Parse raw handoff.md text into a HandoffBriefing. Never raises — a
    malformed/partial document just yields empty fields."""
    frontmatter = _parse_frontmatter(text)
    sections = _parse_sections(text)
    return HandoffBriefing(
        context=frontmatter.get("context", ""),
        project=frontmatter.get("project", ""),
        date=frontmatter.get("date", ""),
        branch=frontmatter.get("branch", ""),
        waar_we_middenin_zitten=sections.get("Waar we middenin zitten", ""),
        state=sections.get("State", ""),
        next_steps=sections.get("Next steps", ""),
        raw=text,
    )


def read_handoff(path: Path) -> Optional[HandoffBriefing]:
    """Read + parse handoff.md at path. Returns None if missing/unreadable."""
    resolved = Path(path)
    if not resolved.is_file():
        return None
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_handoff(text)


def format_briefing(briefing: HandoffBriefing) -> str:
    """Render a HandoffBriefing as the resume text `vnx handoff show` prints."""
    lines = [
        f"Context rotation resume — project={briefing.project} branch={briefing.branch} date={briefing.date}",
        "",
        "## Waar we middenin zitten",
        briefing.waar_we_middenin_zitten or "(none recorded)",
        "",
        "## State",
        briefing.state or "(none recorded)",
        "",
        "## Next steps",
        briefing.next_steps or "(none recorded)",
    ]
    return "\n".join(lines)
