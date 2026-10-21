"""The shipped T0 skill and DISPATCH_RULES must not present a claude tmux lane as live.

The tmux-spawn lane was removed on 2026-09-18. Every claude dispatch runs on
`claude_headless`. A sentence may name the old lane only to say it was removed.
Also: every `DISPATCH_RULES §N` the skill cites must land on an existing section.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
RULES_MD = REPO_ROOT / "docs" / "core" / "DISPATCH_RULES.md"
SKILL_MD = REPO_ROOT / "skills" / "t0-orchestrator" / "SKILL.md"

STALE = re.compile(r"claude-tmux|tmux-spawn\s+lane", re.IGNORECASE)


def _sentences(text: str):
    flat = re.sub(r"[ \t]*\n(?!\s*\n)[ \t]*", " ", text)
    for block in re.split(r"\n\s*\n", flat):
        yield from re.split(r"(?<=[.;])\s+", block)


@pytest.mark.parametrize("path", [RULES_MD, SKILL_MD], ids=lambda p: p.name)
def test_no_sentence_presents_a_claude_tmux_lane_as_current(path):
    offenders = [
        s.strip()[:140]
        for s in _sentences(path.read_text(encoding="utf-8"))
        if STALE.search(s) and "removed" not in s.lower()
    ]
    assert offenders == [], f"{path.name} names a claude tmux lane as current: {offenders}"


def test_rules_headings_do_not_name_the_removed_lane():
    headings = [
        line for line in RULES_MD.read_text(encoding="utf-8").splitlines()
        if line.startswith("#") and STALE.search(line)
    ]
    assert headings == []


def test_skill_section_references_land_on_existing_rules_sections():
    sections = set(re.findall(r"^## (\d+)\.", RULES_MD.read_text(encoding="utf-8"), re.M))
    skill = SKILL_MD.read_text(encoding="utf-8")
    cited = set()
    for m in re.finditer(r"DISPATCH_RULES(?:\.md)?`?\s+§(\d+)(?:[–-](\d+))?", skill):
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        cited.update(str(n) for n in range(lo, hi + 1))
    assert cited, "skill no longer cites DISPATCH_RULES sections"
    assert cited <= sections, f"dangling section refs: {sorted(cited - sections)}"


def test_concurrency_section_names_the_headless_lane():
    text = RULES_MD.read_text(encoding="utf-8")
    body = text.split("## 6.", 1)[1].split("## 7.", 1)[0]
    assert "claude_headless" in body
    assert "2026-09-18" in body
