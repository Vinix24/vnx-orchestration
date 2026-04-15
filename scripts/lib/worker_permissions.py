#!/usr/bin/env python3
"""worker_permissions.py — Per-terminal permission profiles for headless workers.

Loads role-based permission profiles from .vnx/worker_permissions.yaml and
generates CLAUDE.md permission instructions that are injected into dispatch
instructions before subprocess launch.

BILLING SAFETY: No Anthropic SDK. No api.anthropic.com calls.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PERMISSIONS_YAML_PATH = Path(__file__).resolve().parents[2] / ".vnx" / "worker_permissions.yaml"


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


def load_permissions(role: str, yaml_path: Path = _PERMISSIONS_YAML_PATH) -> PermissionProfile:
    """Load PermissionProfile for role from worker_permissions.yaml.

    Returns a profile with empty lists (no restrictions) when the role is not
    found or the YAML cannot be loaded — callers remain functional.
    """
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
    yaml_path: Path = _PERMISSIONS_YAML_PATH,
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
