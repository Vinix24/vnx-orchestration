#!/usr/bin/env python3
"""dispatch_footer.py — Load and append T0 action-request footer to dispatch instructions.

Footers live in ``templates/footers/`` and tell the receiving T0 what orchestration
action to take after reading a worker receipt.  This module resolves the correct
template by mode, strips YAML frontmatter, and appends it to dispatch instructions
that don't already carry a footer.

Mode selection (in priority order):
1. ``VNX_DISPATCH_FOOTER_MODE`` env var (``normal`` | ``autonomous`` | ``enhanced``)
2. Caller-supplied ``mode`` argument
3. Default: ``"normal"``
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Sentinel that marks an instruction already carrying a footer.
# This tag is injected by append_dispatch_footer (not present in raw templates)
# so it works as a mode-agnostic double-injection guard regardless of which
# footer variant (normal/enhanced/autonomous) was injected.
_FOOTER_SENTINEL = "<!-- VNX-T0-ACTION-FOOTER -->"

# Map mode name -> template filename (under templates/footers/)
_FOOTER_FILES: dict[str, str] = {
    "normal": "t0_action_request.md",
    "enhanced": "t0_action_request_enhanced.md",
    "autonomous": "t0_action_request_autonomous.md",
}
_DEFAULT_MODE = "normal"


def _resolve_templates_dir() -> Path:
    """Resolve path to templates/footers/ relative to project root.

    This file lives at scripts/lib/dispatch_footer.py so project root is
    three levels up: scripts/lib -> scripts -> project_root.
    """
    here = Path(__file__).resolve()
    return here.parent.parent.parent / "templates" / "footers"


def _strip_frontmatter(content: str) -> str:
    """Strip YAML frontmatter (the leading --- ... --- block) from template content."""
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return content
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[i + 1:]).lstrip("\n")
    # No closing --- found — return as-is.
    return content


def load_footer_template(mode: str = _DEFAULT_MODE) -> str:
    """Load footer template content for the given mode.

    Resolves mode from ``VNX_DISPATCH_FOOTER_MODE`` env var first, then the
    caller-supplied ``mode`` argument.  Strips YAML frontmatter before returning.

    Returns empty string when the template file is missing or unreadable
    (best-effort, non-fatal).
    """
    effective_mode = os.environ.get("VNX_DISPATCH_FOOTER_MODE") or mode or _DEFAULT_MODE
    filename = _FOOTER_FILES.get(effective_mode) or _FOOTER_FILES[_DEFAULT_MODE]
    template_path = _resolve_templates_dir() / filename
    try:
        raw = template_path.read_text(encoding="utf-8")
        return _strip_frontmatter(raw).strip()
    except Exception as exc:
        logger.warning(
            "dispatch_footer: could not load %s (%s) — skipping footer injection",
            template_path,
            exc,
        )
        return ""


def append_dispatch_footer(instruction: str, mode: str = _DEFAULT_MODE) -> str:
    """Append T0 action-request footer to a dispatch instruction.

    Idempotent: if the instruction already contains the footer sentinel
    (``## ACTION REQUIRED: T0 Orchestrator Response``), returns unchanged.

    Returns the instruction unchanged on any failure (best-effort, non-fatal).
    """
    if _FOOTER_SENTINEL in instruction:
        logger.debug("dispatch_footer: footer already present — skipping")
        return instruction

    footer = load_footer_template(mode)
    if not footer:
        return instruction

    return instruction + "\n\n---\n\n" + _FOOTER_SENTINEL + "\n" + footer
