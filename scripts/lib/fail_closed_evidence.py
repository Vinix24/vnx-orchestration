"""fail_closed_evidence.py — one shared fail-closed block for every build worker.

Measured 2026-10-23 (dispatch dlv-48b4e15d7ebc): close to half of 140 review
blockers were fail-open reads of evidence, and the role prompts taught the
opposite (``(value or {}).get(...)`` without a limit). The rule is written once,
in ``prompts/fail_closed_evidence.md``, and reaches a worker through the two
places a role's text is assembled:

  - ``PromptAssembler.assemble`` (base_worker + ``prompts/roles/<role>.md``)
  - ``skill_context._legacy_claude_md_resolution`` (``agents/<role>/CLAUDE.md``,
    the route quality-engineer and system-architect take; they never see
    base_worker.md)

Both call :func:`with_block`, which is idempotent: a text that already carries
the block is returned unchanged, so a worker sees it exactly once whichever
route produced the text. Every lane (headless envelope, provider lane, the
terminal-pinned subprocess lane) assembles through one of those two places.

Reviewer roles are not build roles and never receive it (D1 keeps them
unenriched, see ``reviewer_roles``).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_BLOCK_PATH = Path(__file__).resolve().parent / "prompts" / "fail_closed_evidence.md"

BLOCK_HEADING = "## Fail-closed on evidence"

# Roles whose dispatches write code. A positive list: an unknown role gets no
# block, so a new reviewer-like role can never receive it by accident.
BUILD_ROLES = frozenset({
    "backend-developer",
    "frontend-developer",
    "quality-engineer",
    "security-engineer",
    "database-engineer",
    "test-engineer",
    "system-architect",
    "architect",
    "intelligence-engineer",
    "api-developer",
    "debugger",
    "python-optimizer",
    "supabase-expert",
    "performance-profiler",
})


def is_build_role(role: "str | None") -> bool:
    return (role or "").strip() in BUILD_ROLES


def load_block() -> str:
    """Return the shared block text; raise when the file is missing.

    A missing file is a broken install, not a reason to ship a build worker
    without the rule, so this does not degrade to an empty string.
    """
    return _BLOCK_PATH.read_text().strip()


def with_block(text: str, role: "str | None") -> str:
    """Append the shared block to *text* for a build role, exactly once."""
    if not is_build_role(role) or BLOCK_HEADING in text:
        return text
    return f"{text}\n\n---\n\n{load_block()}"
