"""Directory-based VNX skill discovery.

Skills are discovered from the canonical ``skills/`` tree by directory, not by a
manual manifest. A directory containing a ``SKILL.md`` file is considered a
shipped skill unless it carries the opt-out marker ``.vnx-skip-sync``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

# Marker file inside a skill directory that excludes it from sync/discovery.
# Use this for project-local-only skills that live under .claude/skills/ but must
# NOT propagate to consumer projects via vnx skills sync.
SKILL_OPT_OUT_MARKER = ".vnx-skip-sync"


def is_skill_dir(path: Path) -> bool:
    """Return True if *path* is a directory that looks like a shipped skill."""
    return path.is_dir() and not path.name.startswith(".") and (path / "SKILL.md").is_file()


def is_opted_out(path: Path) -> bool:
    """Return True if the skill directory carries the opt-out marker."""
    return (path / SKILL_OPT_OUT_MARKER).is_file()


def iter_skill_dirs(skills_dir: Path) -> Iterable[Path]:
    """Yield skill directories under *skills_dir* that should sync/discover."""
    if not skills_dir.is_dir():
        return
    for child in sorted(skills_dir.iterdir()):
        if is_skill_dir(child) and not is_opted_out(child):
            yield child


def resolve_sync_set(skills_dir: Path) -> list[str]:
    """Return the sorted list of skill names that would be propagated by sync."""
    return sorted(p.name for p in iter_skill_dirs(skills_dir))
