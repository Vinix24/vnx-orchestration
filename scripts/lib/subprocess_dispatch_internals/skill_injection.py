"""skill_injection — compatibility re-export (canonical home: scripts/lib/skill_context.py).

The worker prompt-context injection implementation moved to the lane-neutral
``skill_context`` module so no dispatch lane imports from another lane's
internal package (dispatch-20260801-w11-1313-layering). This module is a thin
compat shim re-exporting the same names; existing imports and test patches at
``subprocess_dispatch_internals.skill_injection`` keep resolving unchanged.

New code should import from ``skill_context``.
"""

from __future__ import annotations

from skill_context import (
    _build_intelligence_section,
    _has_legacy_role_source,
    _has_prompt_assembler_role,
    _inject_permission_profile,
    _inject_skill_context,
    _legacy_claude_md_resolution,
    _load_agent_profile,
    _resolve_agent_cwd,
    _resolve_effective_role,
    _try_prompt_assembler,
)

__all__ = [
    "_build_intelligence_section",
    "_has_legacy_role_source",
    "_has_prompt_assembler_role",
    "_inject_permission_profile",
    "_inject_skill_context",
    "_legacy_claude_md_resolution",
    "_load_agent_profile",
    "_resolve_agent_cwd",
    "_resolve_effective_role",
    "_try_prompt_assembler",
]
