"""Resolve the ``claude`` CLI binary without relying on an interactive shell PATH.

A launchd or cron job inherits no shell profile, so ``claude`` (installed by the
native installer under ``~/.local/bin``) is invisible to a bare
``subprocess.run(["claude", ...])`` even though it works in a terminal (OI-1258).

No Anthropic SDK: this only locates the binary the callers start via subprocess.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

# Fixed install locations of the native installer, relative to $HOME.
_NATIVE_INSTALL_RELPATHS = (
    os.path.join(".local", "bin", "claude"),
    os.path.join(".claude", "local", "claude"),
)


def _is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def resolve_claude_cli() -> Optional[str]:
    """Return the absolute path of the ``claude`` CLI, or ``None`` if not found.

    Order: ``shutil.which("claude")`` on the current PATH, then the native
    installer's fixed locations. Only an existing executable file counts.
    ``None`` means the caller must fail closed (``missing_cli``), not guess.
    """
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    if not home or home == "~":
        return None
    for rel in _NATIVE_INSTALL_RELPATHS:
        candidate = os.path.join(home, rel)
        if _is_executable_file(candidate):
            return candidate
    return None
