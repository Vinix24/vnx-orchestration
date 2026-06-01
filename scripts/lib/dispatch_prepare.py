"""dispatch_prepare — shared prepare() for both tmux and subprocess Claude lanes.

Composes the enriched instruction body by reusing existing injection seams from
subprocess_dispatch_internals. Both lanes wire this behind VNX_SHARED_PREPARE
(default "0" — safe ship; burn-in flips to "1").

Output order (top to bottom):
  [permission preamble]       _inject_permission_profile  (gap #2)
  ---
  [skill body + instruction]  _inject_skill_context       (+ repo-map prepended)
  [worker rules footer]       VNX_WORKER_RULES_FOOTER=1   (gap #3a, default on)
  [report contract directive] VNX_REPORT_CONTRACT_DIRECTIVE=1 (gap #3b, default on)
  <!-- VNX-END-OF-INSTRUCTION -->
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_lib_dir = str(Path(__file__).resolve().parent)
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

_WORKER_RULES_FOOTER_SENTINEL = "<!-- VNX-WORKER-RULES-FOOTER -->"
_TRAILER_SENTINEL = "<!-- VNX-END-OF-INSTRUCTION -->"


def prepare(
    *,
    terminal_id: "str | None" = None,
    instruction: str,
    role: "str | None" = None,
    dispatch_id: str,
    dispatch_paths: "list[str] | None" = None,
    pr_id: "str | None" = None,
    model: str = "sonnet",
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

    # 6. Trailer sentinel — paste-truncation guard
    body = body + f"\n\n{_TRAILER_SENTINEL}\n"

    return body
