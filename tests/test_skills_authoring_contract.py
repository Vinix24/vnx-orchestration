"""Authoring contract of skills (skilldisc R2).

The authored source of a skill is ``skills/<slug>/SKILL.md``. The Claude
destination ``.claude/skills/<slug>/`` is generated and ignored by git, so a
materialised copy can never land in a commit. Tracked files stay tracked: an
ignore rule never untracks them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MIN_TRACKED_CLAUDE = 27
MIN_TRACKED_SOURCE = 25
CLAUDE_ONLY_SLUGS = ("control-centre", "panel")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def _tracked(rev: str | None, pattern: str) -> set[str]:
    if rev is None:
        result = _git("ls-files", pattern)
    else:
        result = _git("ls-tree", "-r", "--name-only", rev)
    assert result.returncode == 0, result.stderr
    names = set(result.stdout.splitlines())
    if rev is None:
        return names
    prefix, suffix = pattern.split("*", 1)
    return {n for n in names if n.startswith(prefix) and n.endswith(suffix)}


def _merge_base() -> str | None:
    result = _git("merge-base", "origin/main", "HEAD")
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _ignored(path: str) -> bool:
    result = _git("check-ignore", "-q", path)
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


def test_new_claude_skill_md_is_ignored() -> None:
    assert _ignored(".claude/skills/zzz-probe/SKILL.md")


def test_authored_source_is_not_ignored() -> None:
    assert not _ignored("skills/refactor-safe/SKILL.md")
    assert not _ignored("skills/zzz-probe/SKILL.md")


@pytest.mark.parametrize(
    "pattern,minimum",
    [
        (".claude/skills/*/SKILL.md", MIN_TRACKED_CLAUDE),
        ("skills/*/SKILL.md", MIN_TRACKED_SOURCE),
    ],
)
def test_tracked_skill_files_stay_tracked(pattern: str, minimum: int) -> None:
    now = _tracked(None, pattern)
    assert len(now) >= minimum
    base = _merge_base()
    if base is not None:
        before = _tracked(base, pattern)
        assert before <= now, f"untracked since merge base: {sorted(before - now)}"


def test_claude_only_slugs_are_still_tracked() -> None:
    tracked = _tracked(None, ".claude/skills/*/SKILL.md")
    source = _tracked(None, "skills/*/SKILL.md")
    for slug in CLAUDE_ONLY_SLUGS:
        assert f".claude/skills/{slug}/SKILL.md" in tracked
        assert f"skills/{slug}/SKILL.md" not in source
