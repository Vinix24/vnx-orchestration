"""harness_config_dir.py — fabric-owned Claude config dir for the harness lanes.

glm-harness and deepseek-harness start a ``claude`` child against a redirected
endpoint (Z.ai via OpenRouter, DeepSeek). Without a ``CLAUDE_CONFIG_DIR`` of its
own the child inherits the operator's (``~/.claude`` or ``~/.claude-salesminds``)
and loads the user CLAUDE.md with its imports, the user memory and the personal
skills, all of which then go to the provider.

``ensure_harness_config_dir(lane)`` returns ``<VNX_DATA_DIR>/harness-config/<lane>/``
(mode 0700). The only file the fabric writes there is ``settings.json`` with the
destructive Bash ``ask`` rules. It FAILS CLOSED (raises ``HarnessConfigDirError``)
rather than falling back to inheritance.

These lanes authenticate with ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_API_KEY``,
never the keychain item bound to a config-dir path, so a fresh dir does not
break their login. The ``claude_headless`` subscription lane must NOT use this
helper: its keychain login is bound to its config-dir path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List

_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

HARNESS_CONFIG_SUBDIR = "harness-config"

# Fabric constant, never copied from ~/.claude/settings.json at runtime.
DESTRUCTIVE_BASH_ASK_RULES: List[str] = [
    "Bash(sudo:*)",
    "Bash(rm -rf:*)",
    "Bash(chown:*)",
    "Bash(dd:*)",
    "Bash(mkfs:*)",
]

# Top-level entries that would feed user-level context back into the child.
FORBIDDEN_ENTRIES = (
    "CLAUDE.md",
    "CLAUDE.local.md",
    "skills",
    "agents",
    "commands",
    "hooks",
    "plugins",
)

# Env pair that every harness spawn overlays last.
AUTO_MEMORY_OFF_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


class HarnessConfigDirError(RuntimeError):
    """The harness config dir is missing, unsafe or contaminated: do not spawn."""


def _settings_payload() -> str:
    return json.dumps({"permissions": {"ask": list(DESTRUCTIVE_BASH_ASK_RULES)}}, indent=2) + "\n"


def _resolve_data_dir() -> Path:
    try:
        from vnx_paths import resolve_paths
        raw = (resolve_paths().get("VNX_DATA_DIR") or "").strip()
    except Exception as exc:  # vnx-silent-except: re-raised as the fail-closed error
        raise HarnessConfigDirError(f"VNX_DATA_DIR cannot be resolved: {exc}") from exc
    if not raw:
        raise HarnessConfigDirError("VNX_DATA_DIR resolved to an empty value")
    return Path(raw).expanduser()


def _personal_config_roots() -> List[Path]:
    home = Path.home()
    roots = [home / ".claude"]
    try:
        roots.extend(p for p in home.glob(".claude-*"))
    except OSError:
        pass
    return [Path(os.path.realpath(r)) for r in roots]


def _is_inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _safe_lane_name(lane: str) -> str:
    name = (lane or "").strip()
    if not name or "/" in name or name in (".", "..") or os.sep in name:
        raise HarnessConfigDirError(f"invalid harness lane name: {lane!r}")
    return name


def _has_symlink_component(path: Path, stop: Path) -> bool:
    """True when ``path`` or any component between it and ``stop`` is a symlink."""
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


def ensure_harness_config_dir(lane: str) -> Path:
    """Create (if needed) and validate the config dir for ``lane``; return its path."""
    name = _safe_lane_name(lane)
    data_dir = _resolve_data_dir()
    lane_dir = data_dir / HARNESS_CONFIG_SUBDIR / name

    resolved = Path(os.path.realpath(lane_dir))
    for root in _personal_config_roots():
        if _is_inside(resolved, root) or _is_inside(Path(os.path.abspath(lane_dir)), root):
            raise HarnessConfigDirError(
                f"harness config dir {lane_dir} lies inside a personal config dir ({root})"
            )

    if _has_symlink_component(lane_dir, data_dir):
        raise HarnessConfigDirError(f"harness config dir {lane_dir} is or sits behind a symlink")

    try:
        lane_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(lane_dir, 0o700)
    except OSError as exc:
        raise HarnessConfigDirError(f"cannot create harness config dir {lane_dir}: {exc}") from exc

    if lane_dir.is_symlink() or not lane_dir.is_dir():
        raise HarnessConfigDirError(f"harness config dir {lane_dir} is not a plain directory")

    try:
        entries = list(lane_dir.iterdir())
    except OSError as exc:
        raise HarnessConfigDirError(f"cannot read harness config dir {lane_dir}: {exc}") from exc
    for entry in entries:
        if entry.is_symlink():
            raise HarnessConfigDirError(f"harness config dir holds a symlink: {entry}")
        if entry.name in FORBIDDEN_ENTRIES:
            raise HarnessConfigDirError(
                f"harness config dir holds user-level context ({entry.name}): {lane_dir}"
            )

    settings_path = lane_dir / "settings.json"
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HarnessConfigDirError(f"unreadable {settings_path}: {exc}") from exc
        if not isinstance(existing, dict) or "hooks" in existing:
            raise HarnessConfigDirError(f"{settings_path} carries hooks or is not an object")
    tmp = lane_dir / "settings.json.tmp"
    try:
        tmp.write_text(_settings_payload(), encoding="utf-8")
        os.replace(tmp, settings_path)  # vnx-atomic-write: tmp + os.replace
    except OSError as exc:
        raise HarnessConfigDirError(f"cannot write {settings_path}: {exc}") from exc
    return lane_dir


def harness_config_env(lane: str) -> Dict[str, str]:
    """Env overlay (applied last) that pins the child to the lane's config dir."""
    return {
        "CLAUDE_CONFIG_DIR": str(ensure_harness_config_dir(lane)),
        AUTO_MEMORY_OFF_ENV: "1",
    }


def harness_projects_dirs() -> List[Path]:
    """``<VNX_DATA_DIR>/harness-config/*/projects`` dirs that exist (read-only, never raises)."""
    try:
        base = _resolve_data_dir() / HARNESS_CONFIG_SUBDIR
        return sorted(p for p in base.glob("*/projects") if p.is_dir())
    except Exception:  # vnx-silent-except: transcript lookup is best-effort
        return []
