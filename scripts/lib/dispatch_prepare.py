"""dispatch_prepare — shared prepare() for both tmux and subprocess Claude lanes.

Composes the enriched instruction body by reusing existing injection seams from
subprocess_dispatch_internals. Both lanes wire this behind VNX_SHARED_PREPARE
(default "0" — safe ship; burn-in flips to "1").

Output order (top to bottom):
  [permission preamble]       _inject_permission_profile  (gap #2)
  ---
  [skill body + instruction]  _inject_skill_context       (+ repo-map prepended)
  [scope guard]               dispatch_paths, when non-empty
  [worker rules footer]       VNX_WORKER_RULES_FOOTER=1   (gap #3a, default on)
  [report contract directive] VNX_REPORT_CONTRACT_DIRECTIVE=1 (gap #3b, default on)

The trailer sentinel (<!-- VNX-END-OF-INSTRUCTION -->) is NOT appended by
prepare() — each lane appends it as the absolute last step after any
lane-specific content (e.g. the tmux completion-protocol).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_lib_dir = str(Path(__file__).resolve().parent)
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

_WORKER_RULES_FOOTER_SENTINEL = "<!-- VNX-WORKER-RULES-FOOTER -->"

# Public constant — lanes append this as the ABSOLUTE LAST line of the
# delivered body.  prepare() intentionally does NOT include it so each
# lane can append lane-specific content (e.g. completion-protocol) before it.
END_OF_INSTRUCTION_SENTINEL = "<!-- VNX-END-OF-INSTRUCTION -->"

# Private alias kept for backward compatibility with existing imports.
_TRAILER_SENTINEL = END_OF_INSTRUCTION_SENTINEL


def _scope_note_block(dispatch_paths: "list[str]") -> str:
    """Scope guard block forwarded from dispatch_paths — identical to tmux lane text."""
    paths_str = "\n".join(f"  - `{p}`" for p in dispatch_paths)
    return (
        "\n\n---\n\n## Scope Guard\n\n"
        "**Edit ONLY within these paths.** Do not touch files outside this scope:\n\n"
        f"{paths_str}\n"
    )


def prepare(
    *,
    terminal_id: "str | None" = None,
    instruction: str,
    role: "str | None" = None,
    dispatch_id: str,
    dispatch_paths: "list[str] | None" = None,
    pr_id: "str | None" = None,
    model: str = "kimi-k3",
    repo_map: "str | None" = None,
) -> str:
    """Compose the shared enriched instruction body for both Claude lanes.

    Reuses existing injection seams from subprocess_dispatch_internals; does not
    duplicate enrichment logic. Both lanes call this when VNX_SHARED_PREPARE=1.
    """
    from subprocess_dispatch_internals.skill_injection import (
        _inject_permission_profile,
        _inject_skill_context,
    )
    import worker_rules_footer as _wrf
    import report_body_contract as _rbc

    # 1. Append repo-map to raw instruction before skill injection (mirrors subprocess delivery)
    raw = instruction
    if repo_map:
        raw = raw + f"\n\n{repo_map}"

    # 2. Skill context: skill body + intelligence wrapping
    body = _inject_skill_context(
        terminal_id or "",
        raw,
        role,
        {
            "dispatch_id": dispatch_id,
            "model": model,
            "dispatch_paths": dispatch_paths or [],
            "pr_id": pr_id,
            "pr": pr_id,
        },
    )

    # 3. Permission preamble prepended to full body (closes gap #2)
    body = _inject_permission_profile(terminal_id or "", role, body)

    # 3b. Scope guard — only when dispatch_paths is non-empty
    if dispatch_paths:
        body = body + _scope_note_block(dispatch_paths)

    # 4. Worker rules footer — gated by VNX_WORKER_RULES_FOOTER (default on, gap #3a)
    if os.environ.get("VNX_WORKER_RULES_FOOTER", "1").strip().lower() not in (
        "0", "false", "no", "off"
    ):
        if _WORKER_RULES_FOOTER_SENTINEL not in body:
            body = body + "\n\n" + _wrf.build(role, dispatch_id)

    # 5. Report contract directive — gated by VNX_REPORT_CONTRACT_DIRECTIVE (default on, gap #3b)
    if os.environ.get("VNX_REPORT_CONTRACT_DIRECTIVE", "1").strip().lower() not in (
        "0", "false", "no", "off"
    ):
        body = body + "\n\n" + _rbc.build_directive(dispatch_id, pr_id=pr_id)

    # Trailer sentinel is intentionally NOT appended here.
    # Each lane (tmux, subprocess) appends END_OF_INSTRUCTION_SENTINEL as its
    # absolute last step, after any lane-specific content.

    return body
