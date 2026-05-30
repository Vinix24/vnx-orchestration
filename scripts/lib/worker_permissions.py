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
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Interim worker-capability scoping
# (WORKER-CAPABILITY-SCOPING-DESIGN.md §5 — pre-full-binding)
# ---------------------------------------------------------------------------

# Empty MCP server set. Paired with --strict-mcp-config this gives the default
# code worker ZERO ambient MCP reach (no Supabase / n8n / Gmail side-effects).
# This is the core security win of the interim.
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'

# Tool set a headless code worker needs to function: full file CRUD + Bash
# (incl. git) + search. Used when the dispatch role declares no profile so the
# interim never silently strips a code worker's ability to write and commit.
DEFAULT_CODE_WORKER_TOOLS = ["Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep"]


def default_code_worker_profile() -> PermissionProfile:
    """Interim fallback profile for a headless code worker (no role binding yet).

    Permits the full code-worker tool set. Zero-MCP isolation is enforced
    separately by the spawn flags (build_claude_scoping_args), not by this
    profile. The full role→capability binding lands in unified-layer Wave 2.
    """
    return PermissionProfile(role="code-worker", allowed_tools=list(DEFAULT_CODE_WORKER_TOOLS))


def worker_scoping_enabled() -> bool:
    """Whether the interim worker-capability scoping is active (default ON).

    Set ``VNX_WORKER_SCOPED=0`` (or false/no/off) to revert to the legacy
    ``--dangerously-skip-permissions`` posture. The flag makes the change
    reversible per the design's sequencing note.
    """
    raw = os.environ.get("VNX_WORKER_SCOPED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def build_claude_scoping_args(
    profile: PermissionProfile | None = None,
    *,
    permission_mode: str = "acceptEdits",
) -> list[str]:
    """Build the interim capability-scoping argv fragment for a headless claude worker.

    Replaces the ``--dangerously-skip-permissions`` blanket with a scoped-but-
    functional posture (WORKER-CAPABILITY-SCOPING-DESIGN.md §4.4 / §5):

      * ``--permission-mode <mode>`` — ``acceptEdits`` lets a no-TTY worker apply
        edits without prompting while Bash/MCP stay gated by the allow-list.
      * ``--strict-mcp-config`` + ``--mcp-config {"mcpServers":{}}`` — ignore all
        ambient MCP sources and expose ZERO MCP servers (the core security win).
      * ``--allowedTools <list>`` — materialized from ``generate_claude_settings``
        so the worker keeps Read/Write/Edit/MultiEdit/Bash/Glob/Grep and can
        still write code and commit.

    profile defaults to the code-worker profile when not supplied.
    """
    if profile is None:
        profile = default_code_worker_profile()
    allowed = generate_claude_settings(profile).get("allowedTools", [])
    args = [
        "--permission-mode", permission_mode,
        "--strict-mcp-config",
        "--mcp-config", EMPTY_MCP_CONFIG,
    ]
    if allowed:
        args += ["--allowedTools", ",".join(allowed)]
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
