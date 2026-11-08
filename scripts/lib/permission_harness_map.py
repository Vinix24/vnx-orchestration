"""Capability matrix per worker lane (track provider-agnostic-permissions, P3).

For each of the six lanes and each policy dimension: what the harness can
enforce, with the mechanism named. The values follow ADR-057 section 5:
``supported``, ``degraded`` (the lane can do it by another mechanism with other
evidence) and ``unsupported`` (the lane cannot express it).

A lane that is not in the table is ``unsupported`` for every dimension, never a
default of another lane. A dimension outside :data:`DIMENSIONS` raises.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

SUPPORTED = "supported"
DEGRADED = "degraded"
UNSUPPORTED = "unsupported"
LEVELS = frozenset({SUPPORTED, DEGRADED, UNSUPPORTED})

DIMENSIONS = (
    "filesystem_read", "filesystem_write", "filesystem_deny",
    "tools_allow", "tools_deny", "mcp", "network", "hooks",
)
LANES = (
    "claude_headless", "claude_subprocess", "deepseek_harness",
    "glm_harness", "kimi", "codex",
)

_CLAUDE_FLAGS = {
    "filesystem_read": (DEGRADED, "no read flag: the cwd worktree and the PreToolUse scope hook"),
    "filesystem_write": (SUPPORTED, "--permission-mode acceptEdits plus file_write_scope in the hook"),
    "filesystem_deny": (DEGRADED, "isolation denied_paths reach the hook only, no claude flag"),
    "tools_allow": (SUPPORTED, "--allowedTools"),
    "tools_deny": (SUPPORTED, "--disallowedTools"),
    "mcp": (SUPPORTED, "--mcp-config empty plus --strict-mcp-config plus the mcp__* deny"),
    "network": (SUPPORTED, "WebFetch and WebSearch in --disallowedTools"),
    "hooks": (SUPPORTED, "PreToolUse hook registered in .claude/settings.json"),
}
# The harness lanes run the same claude seam but their settings.json refuses
# hooks (harness_config_dir.py), so the hook-carried parts fall away.
_HARNESS_FLAGS = {
    **_CLAUDE_FLAGS,
    "filesystem_deny": (UNSUPPORTED, "denied_paths need the hook and the harness config refuses hooks"),
    "hooks": (UNSUPPORTED, "harness settings.json refuses a hooks block"),
    "filesystem_read": (DEGRADED, "no read flag: the cwd worktree only"),
    "filesystem_write": (DEGRADED, "--permission-mode acceptEdits, file_write_scope needs the absent hook"),
}
_KIMI = {
    "filesystem_read": (UNSUPPORTED, "kimi --yolo has no path scope"),
    "filesystem_write": (UNSUPPORTED, "kimi --yolo has no path scope"),
    "filesystem_deny": (UNSUPPORTED, "kimi --yolo has no path scope"),
    "tools_allow": (UNSUPPORTED, "no per-tool allow list; --yolo must stay"),
    "tools_deny": (UNSUPPORTED, "no per-tool deny; only a PreToolUse ^Shell$ hook (not measured as a tools-deny)"),
    "mcp": (UNSUPPORTED, "no MCP scope flag"),
    "network": (UNSUPPORTED, "no network switch"),
    "hooks": (DEGRADED, "one PreToolUse ^Shell$ hook in a 0600 config file"),
}
_CODEX = {
    "filesystem_read": (DEGRADED, "--sandbox <mode> from VNX_CODEX_SANDBOX; unsupported on the bypass route"),
    "filesystem_write": (DEGRADED, "--sandbox <mode> from VNX_CODEX_SANDBOX; unsupported on the bypass route"),
    "filesystem_deny": (UNSUPPORTED, "an OS sandbox mode has no per-path deny"),
    "tools_allow": (UNSUPPORTED, "no per-tool allow list"),
    "tools_deny": (UNSUPPORTED, "no per-tool deny"),
    "mcp": (UNSUPPORTED, "no MCP scope flag"),
    "network": (DEGRADED, "the sandbox mode decides network, not the policy"),
    "hooks": (UNSUPPORTED, "no hook mechanism"),
}
_TABLE: Dict[str, Mapping[str, tuple]] = {
    "claude_headless": _CLAUDE_FLAGS,
    "claude_subprocess": _CLAUDE_FLAGS,
    "deepseek_harness": _HARNESS_FLAGS,
    "glm_harness": _HARNESS_FLAGS,
    "kimi": _KIMI,
    "codex": _CODEX,
}


def capability_for(lane: Optional[str], dimension: str) -> Dict[str, str]:
    """``{"level", "mechanism"}`` for one (lane, dimension).

    An unknown or missing lane is ``unsupported``. An unknown dimension raises
    ``KeyError``: the enum is closed.
    """
    if dimension not in DIMENSIONS:
        raise KeyError(f"unknown permission dimension: {dimension!r}")
    row = _TABLE.get(lane) if isinstance(lane, str) else None
    if row is None:
        return {"level": UNSUPPORTED, "mechanism": "lane is not in the capability matrix"}
    level, mechanism = row[dimension]
    return {"level": level, "mechanism": mechanism}


def capability_row(lane: Optional[str]) -> Dict[str, Dict[str, str]]:
    """The full row of a lane: every dimension, never a partial map."""
    return {dim: capability_for(lane, dim) for dim in DIMENSIONS}


def unsupported_dimensions(lane: Optional[str]) -> list:
    """The dimensions the lane cannot express, for the record and the later refusal."""
    return [d for d in DIMENSIONS if capability_for(lane, d)["level"] == UNSUPPORTED]


def material_record(
    lane: Optional[str], policy_digest: Optional[str], flags: Any, dispatch_id: str,
) -> Dict[str, Any]:
    """One material record: digest, lane, the capability row and the flags passed.

    ``policy_digest`` None is recorded as ``policy_state: "unresolved"``: the
    caller states its own scope or the legacy path never resolved a policy.
    """
    return {
        "dispatch_id": dispatch_id,
        "lane": lane if isinstance(lane, str) else "unspecified",
        "lane_known": isinstance(lane, str) and lane in _TABLE,
        "policy_digest": policy_digest,
        "policy_state": "resolved" if policy_digest else "unresolved",
        "capabilities": capability_row(lane),
        "unsupported": unsupported_dimensions(lane),
        "flags": list(flags),
    }
