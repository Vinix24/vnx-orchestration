#!/usr/bin/env python3
"""worker_permissions.py — Per-terminal permission profiles for headless workers.

Loads role-based permission profiles from .vnx/worker_permissions.yaml and
generates CLAUDE.md permission instructions that are injected into dispatch
instructions before subprocess launch.

Path resolution priority (prevents template leak during rc-cutover):
  1. $VNX_PROJECT_ROOT/.vnx/worker_permissions.yaml  — project override file
  2. $PROJECT_ROOT/.vnx/worker_permissions.yaml       — same, alternate env var
  3. Sibling resolution: Path(__file__).parents[2]/.vnx/worker_permissions.yaml
     Works in both dev-mode (repo root/.vnx/) and central install (VNX_HOME/.vnx/).
  4. $VNX_HOME/.vnx/worker_permissions.yaml           — fallback to shipped template

In central-install mode the shipped template (VNX_HOME) is immutable and may
not contain project-specific file_write_scope paths.  Priority 1/2 ensures the
project copy wins; priority 3/4 are fallback-only.

BILLING SAFETY: No Anthropic SDK. No api.anthropic.com calls.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Essential tools a headless code worker needs to read, write, and commit code.
# Used as the fallback allow-list when no role-specific profile is available so
# capability scoping never strips a worker of its ability to do backend work.
DEFAULT_CODE_WORKER_TOOLS = ["Read", "Write", "Edit", "MultiEdit", "Bash", "Grep", "Glob"]

# Empty ambient-MCP config string handed to `claude --mcp-config`. Paired with
# `--strict-mcp-config` this makes a worker reach ZERO MCP servers (no Supabase,
# n8n, Gmail, etc.) — the core security win of the interim capability scoping.
EMPTY_MCP_CONFIG = json.dumps({"mcpServers": {}}, separators=(",", ":"))


def _resolve_permissions_yaml() -> Path:
    """Resolve worker_permissions.yaml with project-override-first priority.

    Returns the first existing path from the priority list above.
    Falls back to the sibling path even if it does not yet exist — callers
    handle missing files gracefully via _load_yaml's empty-dict return.
    """
    # Priority 1: explicit project root override (set by central-install shim)
    for env_var in ("VNX_PROJECT_ROOT", "PROJECT_ROOT"):
        proj = os.environ.get(env_var)
        if proj:
            candidate = Path(proj) / ".vnx" / "worker_permissions.yaml"
            if candidate.exists():
                return candidate

    # Priority 2: sibling resolution (works in dev-mode and embedded installs)
    sibling = Path(__file__).resolve().parents[2] / ".vnx" / "worker_permissions.yaml"
    if sibling.exists():
        return sibling

    # Priority 3: VNX_HOME env var (central install fallback)
    vnx_home = os.environ.get("VNX_HOME")
    if vnx_home:
        candidate = Path(vnx_home) / "worker_permissions.yaml"
        if candidate.exists():
            return candidate

    # Return the sibling path as default (may not exist; callers handle gracefully)
    return sibling


_PERMISSIONS_YAML_PATH = _resolve_permissions_yaml()


@dataclass
class PermissionProfile:
    role: str
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    bash_allow_patterns: list[str] = field(default_factory=list)
    bash_deny_patterns: list[str] = field(default_factory=list)
    file_write_scope: list[str] = field(default_factory=list)


def _load_yaml(path: Path) -> dict:
    """Load YAML file, returning empty dict on failure."""
    try:
        import yaml
        return yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        logger.warning("worker_permissions: failed to load %s: %s", path, exc)
        return {}


def load_permissions(role: str, yaml_path: Path | None = None) -> PermissionProfile:
    """Load PermissionProfile for role from worker_permissions.yaml.

    yaml_path defaults to None which triggers project-override-first resolution
    on every call (important in central-install mode where PROJECT_ROOT may be
    set after module import).  Pass an explicit path in tests.

    Returns a profile with empty lists (no restrictions) when the role is not
    found or the YAML cannot be loaded — callers remain functional.
    """
    if yaml_path is None:
        yaml_path = _resolve_permissions_yaml()
    data = _load_yaml(yaml_path)
    profiles = data.get("profiles", {})
    raw = profiles.get(role)
    if raw is None:
        logger.warning("worker_permissions: no profile found for role '%s', using empty profile", role)
        return PermissionProfile(role=role)

    return PermissionProfile(
        role=role,
        allowed_tools=raw.get("allowed_tools", []),
        denied_tools=raw.get("denied_tools", []),
        bash_allow_patterns=raw.get("bash_allow_patterns", []),
        bash_deny_patterns=raw.get("bash_deny_patterns", []),
        file_write_scope=raw.get("file_write_scope", []),
    )


def generate_claude_settings(profile: PermissionProfile) -> dict:
    """Generate Claude Code compatible settings.json content.

    Produces an allowedTools list from the profile. Denied tools are excluded.
    Returns a minimal settings dict suitable for writing as settings.json.
    """
    allowed = [t for t in profile.allowed_tools if t not in profile.denied_tools]
    return {
        "allowedTools": allowed,
    }


def worker_scoped_enabled() -> bool:
    """Whether headless workers spawn with scoped capabilities (default OFF).

    Tmux-spawn workers run in an isolated per-dispatch worktree, so the scoped
    allow-list only stalls autonomous builds on prompts for un-allow-listed ops
    (skills/ writes, mkdir, rm) without adding real blast-radius protection.
    Returns False (blanket ``--dangerously-skip-permissions``) unless
    ``VNX_WORKER_SCOPED`` is explicitly set to a truthy value (``1`` / ``true`` /
    ``yes`` / ``on``), which opts back into the scoped posture (role allow-list +
    empty ambient MCP).
    """
    return os.environ.get("VNX_WORKER_SCOPED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def worker_permission_enforcement_enabled() -> bool:
    """Whether role-scoped worker permission enforcement is active (default OFF).

    This is the ADR-012 enforcement flag: when ON, detached workers launch with
    role-derived ``--allowedTools`` / ``--disallowedTools`` /
    ``--permission-mode`` instead of the blanket
    ``--dangerously-skip-permissions``. Default OFF preserves the historical
    skip-permissions behavior exactly.

    Truthy values: ``1``, ``true``, ``yes``, ``on``.
    """
    return os.environ.get("VNX_ENFORCE_WORKER_PERMISSIONS", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def default_code_worker_profile() -> PermissionProfile:
    """Functional least-privilege profile for a headless code worker.

    Used when no role-specific profile is available (unknown role, or a role whose
    YAML profile declares no allowed_tools) so the scoped spawn still permits the
    essential code-worker tools — never MCP, never skip-permissions.
    """
    return PermissionProfile(
        role="code-worker",
        allowed_tools=list(DEFAULT_CODE_WORKER_TOOLS),
        denied_tools=["WebSearch", "WebFetch"],
    )


def resolve_worker_profile(
    role: Optional[str],
    yaml_path: Path | None = None,
) -> PermissionProfile:
    """Resolve a PermissionProfile for *role*, falling back to the code-worker default.

    A missing/unknown role — or a role whose profile declares no allowed_tools —
    yields :func:`default_code_worker_profile` so capability scoping never strips a
    headless worker of the tools it needs to write code and commit.
    """
    if role:
        profile = load_permissions(role, yaml_path)
        if profile.allowed_tools:
            return profile
    return default_code_worker_profile()


def build_claude_scope_args(
    profile: PermissionProfile,
    *,
    permission_mode: str = "acceptEdits",
    requires_mcp: bool = False,
    working_tree_only: bool = False,
) -> list[str]:
    """Materialize a PermissionProfile into scoping CLI args for a headless ``claude``.

    Replaces the blanket ``--dangerously-skip-permissions`` with a scoped-but-
    functional posture:

      - ``--permission-mode <mode>`` — ``acceptEdits`` auto-approves edits so a
        no-TTY worker proceeds without prompts, while Bash/MCP stay gated.
      - ``--strict-mcp-config --mcp-config {"mcpServers":{}}`` — ignore every
        ambient MCP source; the default worker reaches ZERO MCP servers.
        Skipped when ``requires_mcp=True`` so the dispatch can use the project's
        normal MCP config (e.g. Supabase, n8n) without being force-emptied.
      - ``--allowedTools`` / ``--disallowedTools`` — the profile's tool allow/deny
        lists (the previously-dead :func:`generate_claude_settings`, now live).

    ``requires_mcp``: when True, the ``--strict-mcp-config --mcp-config {}`` pair
    is omitted so the worker's normal ambient MCP config is used instead.
    """
    settings = generate_claude_settings(profile)
    allowed = settings.get("allowedTools", [])
    args: list[str] = ["--permission-mode", permission_mode]
    if not requires_mcp:
        args += ["--strict-mcp-config", "--mcp-config", EMPTY_MCP_CONFIG]
    if allowed:
        args += ["--allowedTools", ",".join(allowed)]
    disallowed = list(profile.denied_tools)
    if working_tree_only:
        # Working-tree-only dispatches (plan-review / plan-write) must not mutate
        # git history. Deny commit/push at the tool-permission layer — the SLOT —
        # not just the instruction preamble (the gids). The bare and `:*` forms
        # cover both `git commit` and `git commit -m ...`.
        disallowed += [
            "Bash(git commit)", "Bash(git commit:*)",
            "Bash(git push)", "Bash(git push:*)",
        ]
    if disallowed:
        args += ["--disallowedTools", ",".join(disallowed)]
    return args


def generate_permission_preamble(profile: PermissionProfile) -> str:
    """Generate a CLAUDE.md-style permission block for injection into instructions.

    The preamble is prepended to dispatch instructions so the headless agent
    is explicitly aware of its tool and bash constraints for this execution.
    """
    lines = [
        f"## Permission Profile: {profile.role}",
        "",
        "You are operating under scoped permissions for this dispatch.",
        "",
    ]

    if profile.allowed_tools:
        lines.append(f"**Allowed tools:** {', '.join(profile.allowed_tools)}")
    if profile.denied_tools:
        lines.append(f"**Denied tools (do NOT use):** {', '.join(profile.denied_tools)}")

    if profile.bash_allow_patterns:
        lines.append("")
        lines.append("**Bash commands you may run (pattern match):**")
        for pat in profile.bash_allow_patterns:
            lines.append(f"  - `{pat}`")

    if profile.bash_deny_patterns:
        lines.append("")
        lines.append("**Bash commands you must NOT run:**")
        for pat in profile.bash_deny_patterns:
            lines.append(f"  - `{pat}`")

    if profile.file_write_scope:
        lines.append("")
        lines.append("**File write scope (only write within these paths):**")
        for scope in profile.file_write_scope:
            lines.append(f"  - `{scope}`")

    lines.append("")
    return "\n".join(lines)


def validate_dispatch_permissions(
    dispatch_metadata: dict,
    yaml_path: Path | None = None,
) -> list[str]:
    """Check if dispatch role matches terminal assignment.

    Returns a list of warning strings. Empty list means no issues.

    dispatch_metadata keys used:
      - "terminal" or "terminal_id": e.g. "T1"
      - "role": e.g. "backend-developer"
    """
    warnings: list[str] = []
    terminal = dispatch_metadata.get("terminal") or dispatch_metadata.get("terminal_id", "")
    role = dispatch_metadata.get("role", "")

    if not terminal or not role:
        return warnings

    if yaml_path is None:
        yaml_path = _resolve_permissions_yaml()
    data = _load_yaml(yaml_path)
    assignments = data.get("terminal_assignments", {})
    expected_role = assignments.get(terminal)

    if expected_role is None:
        warnings.append(
            f"worker_permissions: terminal '{terminal}' has no assignment in worker_permissions.yaml"
        )
    elif expected_role != role:
        warnings.append(
            f"worker_permissions: terminal '{terminal}' is assigned role '{expected_role}' "
            f"but dispatch specifies role '{role}'"
        )

    profiles = data.get("profiles", {})
    if role and role not in profiles:
        warnings.append(
            f"worker_permissions: role '{role}' has no profile in worker_permissions.yaml"
        )

    return warnings


def match_bash_deny(command: str, profile: PermissionProfile) -> Optional[str]:
    """Return the first deny pattern that matches command, or None.

    Uses fnmatch glob matching (shell-style wildcards).
    """
    for pattern in profile.bash_deny_patterns:
        if fnmatch.fnmatch(command, pattern):
            return pattern
    return None


def match_file_write_scope(file_path: str, profile: PermissionProfile) -> bool:
    """Return True if file_path is within any of the profile's file_write_scope globs."""
    if not profile.file_write_scope:
        return True
    for scope in profile.file_write_scope:
        if fnmatch.fnmatch(file_path, scope):
            return True
    return False
