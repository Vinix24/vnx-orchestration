"""Closed reading list for suggested edits (morning round D3b): CLAUDE.md, agents/<role>/CLAUDE.md,
skills/<name>/SKILL.md, scripts/lib/prompts/base_worker.md. Generator and apply handler share it."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

REASON_NOT_ON_LIST = "target_not_on_reading_list"
REASON_MISSING = "target_missing"

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_READING_LIST = (
    re.compile(r"^CLAUDE\.md$"),
    re.compile(rf"^agents/{_SEGMENT}/CLAUDE\.md$"),
    re.compile(rf"^skills/{_SEGMENT}/SKILL\.md$"),
    re.compile(r"^scripts/lib/prompts/base_worker\.md$"),
)
_ROLE_RE = re.compile(rf"^{_SEGMENT}$")


def role_target(role: object) -> Optional[str]:
    """``agents/<role>/CLAUDE.md`` for a plain role name, else None."""
    if not isinstance(role, str) or not _ROLE_RE.match(role) or ".." in role:
        return None
    return f"agents/{role}/CLAUDE.md"


def guard_target(target: object, root: Path) -> Tuple[Optional[Path], Optional[str]]:
    """``(resolved_path, None)`` or ``(None, reason)``; refuses off-list, ``..``, absolute, symlink escape."""
    if not isinstance(target, str) or not target:
        return None, REASON_NOT_ON_LIST
    if not any(p.match(target) for p in _READING_LIST):
        return None, REASON_NOT_ON_LIST
    real_root = Path(root).resolve()
    candidate = real_root / target
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None, REASON_NOT_ON_LIST
    if real_root != resolved and real_root not in resolved.parents:
        return None, REASON_NOT_ON_LIST
    if not resolved.is_file():
        return None, REASON_MISSING
    return resolved, None
