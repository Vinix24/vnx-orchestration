"""harness_config_dir.py — fabric-owned Claude config dir for the harness lanes.

glm-harness and deepseek-harness start a ``claude`` child against a redirected
endpoint (Z.ai via OpenRouter, DeepSeek). Without a ``CLAUDE_CONFIG_DIR`` of its
own the child inherits the operator's (``~/.claude`` or ``~/.claude-salesminds``)
and loads the user CLAUDE.md with its imports, the user memory and the personal
skills, all of which then go to the provider.

``ensure_harness_config_dir(lane)`` returns ``<VNX_DATA_DIR>/harness-config/<lane>/``
(mode 0700). The only file the fabric writes there is ``settings.json``, which
carries the destructive Bash ``ask`` rules plus ``cleanupPeriodDays: 365``. The
retention is pinned to a year because the session transcripts under
``<lane>/projects/`` are the only source for the cost backfill (K6); the Claude
Code default of 30 days would start deleting the oldest of them. It FAILS CLOSED
(raises ``HarnessConfigDirError``) rather than falling back to inheritance.

These lanes authenticate with ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_API_KEY``,
never the keychain item bound to a config-dir path, so a fresh dir does not
break their login. The ``claude_headless`` subscription lane must NOT use this
helper: its keychain login is bound to its config-dir path.

``plugins`` is a forbidden top-level entry, with one exception: a plain directory whose
entries are all plain files from ``PLUGIN_CACHE_ALLOWLIST``. On 2026-10-18 at 07:58Z,
during a long deepseek_gate run, claude 2.1.302 wrote ``plugins/plugin-catalog-cache.json``
and ``plugins/plugin-directory-cache-v2.json`` into the lane dir although nothing was
installed and ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`` was set; the next spawn then
refused and closed every deepseek and glm seat (OI-2163). Both files are the CLI's own
listing of the official marketplace (install counts, token costs), read only by the
``/plugin`` browser and the org-blocked-plugin lookup. They enable nothing and reach no
session context; a plugin needs ``installed_plugins.json`` or an enabled entry in
settings. No env variable stops the fetch (measured in the 2.1.302 binary), and a CLI
update can start writing more files, so the list stays closed: any other entry under
``plugins/`` keeps the refusal and is named in the message.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

HARNESS_CONFIG_SUBDIR = "harness-config"

# ``claude --bare`` skips CLAUDE.md auto-discovery. Without it the child walks from its
# cwd up to ``/`` and sends every ancestor CLAUDE.md / .claude/rules/*.md (the operator's
# ``$HOME/.claude``) to the provider, whatever CLAUDE_CONFIG_DIR says (measured 2026-10-02,
# claude 2.1.287). Every harness child that talks to a redirected endpoint carries it.
BARE_FLAG = "--bare"
APPEND_SYSTEM_PROMPT_FILE_FLAG = "--append-system-prompt-file"
PROJECT_INSTRUCTIONS_FILE = "CLAUDE.md"

# Fabric constant, never copied from ~/.claude/settings.json at runtime.
DESTRUCTIVE_BASH_ASK_RULES: List[str] = [
    "Bash(sudo:*)",
    "Bash(rm -rf:*)",
    "Bash(chown:*)",
    "Bash(dd:*)",
    "Bash(mkfs:*)",
]

# The Claude Code default transcript retention is 30 days; the operator's own
# config dirs carry ``cleanupPeriodDays: 365``. The harness transcripts under
# ``<lane>/projects/`` are the only source for the cost backfill (K6), so the
# lanes must keep them for a year too. Fabric constant, never read from the
# operator's settings at runtime.
HARNESS_TRANSCRIPT_RETENTION_DAYS: int = 365

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

# The CLI's own plugin listing caches, the only files allowed under ``plugins/``.
PLUGIN_CACHE_ALLOWLIST = (
    "plugin-catalog-cache.json",
    "plugin-directory-cache-v2.json",
)

# Env pair that every harness spawn overlays last.
AUTO_MEMORY_OFF_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


class HarnessConfigDirError(RuntimeError):
    """The harness config dir is missing, unsafe or contaminated: do not spawn."""


def _settings_payload() -> str:
    return json.dumps(
        {
            "permissions": {"ask": list(DESTRUCTIVE_BASH_ASK_RULES)},
            "cleanupPeriodDays": HARNESS_TRANSCRIPT_RETENTION_DAYS,
        },
        indent=2,
    ) + "\n"


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


def _check_plugins_entry(plugins_dir: Path, lane_dir: Path) -> None:
    """Allow ``plugins/`` only as a plain dir holding nothing but the CLI's cache files."""
    refusal = "harness config dir holds user-level context (plugins)"
    if plugins_dir.is_symlink() or not plugins_dir.is_dir():
        raise HarnessConfigDirError(f"{refusal}: {plugins_dir} is not a plain directory")
    try:
        children = list(plugins_dir.iterdir())
    except OSError as exc:
        raise HarnessConfigDirError(f"cannot read {plugins_dir}: {exc}") from exc
    for child in children:
        plain_file = child.is_file() and not child.is_symlink()
        if child.name not in PLUGIN_CACHE_ALLOWLIST or not plain_file:
            raise HarnessConfigDirError(
                f"{refusal}: plugins/{child.name} is not an allowed CLI cache file: {lane_dir}"
            )


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
        if entry.name == "plugins":
            _check_plugins_entry(entry, lane_dir)
        elif entry.name in FORBIDDEN_ENTRIES:
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
    try:
        atomic_write_text(settings_path, _settings_payload())
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
    base: Optional[Path] = None
    try:
        base = _resolve_data_dir() / HARNESS_CONFIG_SUBDIR
        return sorted(p for p in base.glob("*/projects") if p.is_dir())
    except (HarnessConfigDirError, OSError) as exc:
        logger.warning(
            "harness_projects_dirs: cannot list harness transcript dirs under %s: %s",
            base if base is not None else "<unresolved VNX_DATA_DIR>", exc,
        )
        return []


def project_instructions_args(cwd: Optional[Any]) -> List[str]:
    """``--append-system-prompt-file`` for exactly ``<cwd>/CLAUDE.md``, when it exists.

    ``--bare`` drops auto-discovery, so the worker's project instructions (the report
    contract) are passed explicitly. Only the file at the worktree root counts, and a
    symlink that resolves outside that root is refused: nothing from outside the
    worktree may reach the provider. ``@import`` lines inside the file stay literal text.
    """
    if cwd is None:
        return []
    root = Path(os.path.realpath(cwd))
    candidate = root / PROJECT_INSTRUCTIONS_FILE
    if not candidate.is_file():
        return []
    resolved = Path(os.path.realpath(candidate))
    if resolved.parent != root:
        logger.warning(
            "project_instructions_args: %s resolves outside the worktree (%s); not passed",
            candidate, resolved,
        )
        return []
    return [APPEND_SYSTEM_PROMPT_FILE_FLAG, str(resolved)]


def bare_harness_cli_args(cwd: Optional[Any] = None) -> List[str]:
    """Flags every harness child starts with: ``--bare`` plus the worktree instructions."""
    return [BARE_FLAG, *project_instructions_args(cwd)]


def require_harness_credential(env: Dict[str, str]) -> None:
    """Refuse a spawn whose child env carries no credential ``--bare`` can use.

    Under ``--bare`` the keychain and OAuth are never read, so an env without
    ``ANTHROPIC_AUTH_TOKEN`` or ``ANTHROPIC_API_KEY`` would only fail later and opaquely.
    Both forms were measured to arrive as a header under ``--bare`` (claude 2.1.287).
    """
    if not any((env.get(key) or "").strip() for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")):
        raise HarnessConfigDirError("harness child env has no ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY")
