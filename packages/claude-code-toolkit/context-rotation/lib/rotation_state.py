"""Per-session state for the context-rotation plugin.

Files live under <project>/.claude/rotation/. The directory carries a
.gitignore with `*`: handovers hold a session's working state and must never
be committed. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Dict, Optional

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def is_safe_session_id(session_id: Any) -> bool:
    return isinstance(session_id, str) and _SAFE_ID.match(session_id) is not None


def rotation_dir(project_dir: str) -> str:
    return os.path.join(project_dir, ".claude", "rotation")


def state_path(project_dir: str, session_id: str) -> str:
    return os.path.join(rotation_dir(project_dir), "state", session_id + ".json")


def handover_path(project_dir: str, session_id: str) -> str:
    return os.path.join(os.path.abspath(rotation_dir(project_dir)), "handover-%s.md" % session_id)


def ensure_gitignore(project_dir: str) -> None:
    """Create rotation/.gitignore once. An existing file is never touched."""
    directory = rotation_dir(project_dir)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, ".gitignore")
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("*\n")


def load_state(project_dir: str, session_id: str) -> Dict[str, Any]:
    """The session's state, or an empty dict when absent or unreadable."""
    if not is_safe_session_id(session_id):
        return {}
    try:
        with open(state_path(project_dir, session_id), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(project_dir: str, session_id: str, state: Dict[str, Any]) -> Optional[str]:
    """Write the state atomically. Raises ValueError for an unsafe session id."""
    if not is_safe_session_id(session_id):
        raise ValueError("unsafe session id")
    ensure_gitignore(project_dir)
    target = state_path(project_dir, session_id)
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temp, target)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise
    return target
