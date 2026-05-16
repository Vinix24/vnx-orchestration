"""env_loader.py — Lightweight .env file loader for VNX provider keys.

Search order (first match wins per key):
  1. <repo-root>/vnx.env
  2. ~/.vnx/vnx.env

Shell env always wins over file values. File values fill in missing keys only.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_VALID_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _parse_env_file(path: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not path.is_file():
        return result
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            logger.warning("env_loader: %s:%d malformed (no =), skipped", path, lineno)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not _VALID_KEY.match(key):
            logger.warning("env_loader: %s:%d invalid key %r, skipped", path, lineno, key)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def load_env(repo_root: Optional[Path] = None, user_home: Optional[Path] = None) -> List[str]:
    """Populate os.environ from vnx.env files. Returns list of loaded paths."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[2]
    if user_home is None:
        user_home = Path.home()
    candidates = [repo_root / "vnx.env", user_home / ".vnx" / "vnx.env"]
    loaded: List[str] = []
    for path in candidates:
        values = _parse_env_file(path)
        if not values:
            continue
        for key, value in values.items():
            if key not in os.environ:  # shell env wins
                os.environ[key] = value
        loaded.append(str(path))
    return loaded
