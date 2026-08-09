"""role_application.py — deterministic role-applied verification for the provider lane.

Dispatch-20260801-w10: the provider/envelope lanes injected intelligence and a
repo map but NEVER the role's own CLAUDE.md context — a dispatch staged with
``role=quality-engineer`` received byte-identical context to one staged with
``role=system-architect``. This module wires the missing piece and, crucially,
makes the receipt truthful about whether the role context was actually USED.

Part A (wiring) reuses ``_inject_skill_context`` — the same injector the tmux
and subprocess lanes already use. Part B (this module) is the deterministic
control: after the final prompt is assembled, it verifies that the content of
the resolved role source actually occurs in the prompt and returns a verdict
the caller stamps onto the dispatch receipt.

Deterministic-first principle (agents/system-architect/CLAUDE.md, #1298): the
check is a string comparison, never a model call. An LLM "judging whether the
role looks used" is the wrong tool here — the prompt is bytes we assembled, so
we can verify containment directly.

Comparison shape (motivated):
  - Role content is embedded with surrounding formatting, so a naive exact
    string match is too brittle. Both sides are whitespace-normalized (every
    whitespace run collapses to one space) before comparison.
  - Primary check: the FULL normalized role content is a substring of the
    normalized prompt. This is the strongest proof — the whole role document
    reached the worker (the legacy injector prepends it verbatim).
  - Fallback: the LONGEST non-empty line of the role content, normalized, is a
    substring. Long sentences survive reflow/trim better than the full doc; the
    longest line acts as a stable marker. This keeps the check truthful when a
    future formatting change reflows the role body.

Candidate sources mirror what ``_inject_skill_context`` actually resolves:
  - ``prompt_assembler`` — ``scripts/lib/prompts/roles/<role>.md`` (PromptAssembler L2)
  - ``agents``          — ``agents/<role>/CLAUDE.md`` (project-level agent override)
  - ``skills``          — ``.claude/skills/<role>/CLAUDE.md`` (skill definition)
  - ``terminal``        — ``.claude/terminals/<terminal>/CLAUDE.md`` (terminal fallback)

The first EXISTING source in that order is the one the injector resolves; only
that resolved source's content may evidence role application. When its content
is absent from the prompt, the verdict is False even if a lower-priority
source's content happens to be present (round-3 gate fix: terminal fallback
content used to stamp role_applied=True while agents/<role>/CLAUDE.md was
absent). When no source exists at all it reports ``tier="none"`` with why.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")

# Tier labels stamped on the receipt. "none" = no role source resolved.
TIER_PROMPT_ASSEMBLER = "prompt_assembler"
TIER_AGENTS = "agents"
TIER_SKILLS = "skills"
TIER_TERMINAL = "terminal"
TIER_NONE = "none"

# Longest-line fallback threshold: a candidate "stable marker" line must be at
# least this many characters or it is too generic to prove anything.
_MIN_MARKER_CHARS = 16


@dataclass(frozen=True)
class RoleApplicationVerdict:
    """Deterministic outcome of the role-applied check, stamped on the receipt."""

    role_applied: bool
    tier: str                     # prompt_assembler | agents | skills | terminal | none
    reason: Optional[str] = None  # why not applied, when role_applied is False
    source_path: Optional[str] = None


def resolve_project_root() -> Path:
    """Resolve the project root the same way the injector's legacy path does.

    Mirrors ``_legacy_claude_md_resolution`` (subprocess_dispatch.__file__ →
    parents[2]) so the control verifies the same ``agents/`` tree the injector
    reads. Callers may pass an explicit ``project_root`` to override (tests).
    """
    import subprocess_dispatch as _sd  # noqa: PLC0415
    return _sd.Path(_sd.__file__).resolve().parents[2]


def _normalize_ws(text: str) -> str:
    """Collapse every whitespace run to a single space and strip the ends.

    Injection rendering interleaves newlines/indentation that the role source
    does not carry; normalizing both sides makes substring containment robust
    to that formatting without loosening what it proves.
    """
    return _WS_RE.sub(" ", text).strip()


def _validate_slug(name: str, context: str) -> None:
    """Raise ValueError if *name* contains '..' or path separators.

    Path traversal via ``role`` or ``terminal_id`` slugs would let a dispatch
    escape the intended directory trees (agents/, .claude/skills/,
    prompt_assembler prompts, .claude/terminals/) and read arbitrary files.
    This guard rejects such slugs before any path is constructed (OI-932).
    """
    if not name or not name.strip():
        raise ValueError(f"{context} must not be empty")
    stripped = name.strip()
    if ".." in stripped:
        raise ValueError(
            f"{context} contains path traversal '..': {stripped!r}"
        )
    if "/" in stripped or "\\" in stripped:
        raise ValueError(
            f"{context} contains path separator: {stripped!r}"
        )


def _candidate_sources(terminal_id: str, role: str, project_root: Path):
    """Yield (tier, path) candidates in the injector's resolution precedence.

    Slugs are validated against path traversal before path construction (OI-932).
    """
    _prompts_dir = Path(__file__).resolve().parent / "prompts" / "roles"
    if role:
        _validate_slug(role, "role")
        yield TIER_PROMPT_ASSEMBLER, _prompts_dir / f"{role}.md"
    if role:
        yield TIER_AGENTS, project_root / "agents" / role / "CLAUDE.md"
    if role:
        yield TIER_SKILLS, project_root / ".claude" / "skills" / role / "CLAUDE.md"
    _validate_slug(terminal_id, "terminal_id")
    yield TIER_TERMINAL, project_root / ".claude" / "terminals" / terminal_id / "CLAUDE.md"


def _longest_meaningful_line(content: str) -> str:
    """Return the longest non-empty line of *content* (stripped)."""
    best = ""
    for line in content.splitlines():
        stripped = line.strip()
        if len(stripped) > len(best):
            best = stripped
    return best


def _content_present(final_prompt: str, role_content: str) -> bool:
    """Whitespace-normalized containment: full doc, then longest-line marker.

    The full-content substring is the primary, strongest proof. When the role
    body was reflowed/trimmed by surrounding formatting, the longest line (a
    stable marker that survives reflow) is the robust fallback.
    """
    if not role_content or not role_content.strip():
        return False
    norm_prompt = _normalize_ws(final_prompt)
    norm_full = _normalize_ws(role_content)
    if norm_full and norm_full in norm_prompt:
        return True
    marker = _longest_meaningful_line(role_content)
    if len(marker) >= _MIN_MARKER_CHARS and _normalize_ws(marker) in norm_prompt:
        return True
    return False


def _lower_tier_present(
    final_prompt: str,
    candidates,
    resolved_tier: str,
) -> Optional[str]:
    """Return the tier of the first lower-priority source whose content is in
    *final_prompt*, or None.

    Only sources strictly below *resolved_tier* in priority order count; the
    resolved source itself is handled by the caller. Used to explain a
    role_applied=False verdict when lower-tier content would have fooled a naive
    "any tier present" check.
    """
    for tier, path in candidates:
        if tier == resolved_tier:
            continue
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="ignore")
            if content.strip() and _content_present(final_prompt, content):
                return tier
    return None


def verify_role_applied(
    final_prompt: str,
    terminal_id: str,
    role: Optional[str],
    *,
    project_root: Optional[Path] = None,
) -> RoleApplicationVerdict:
    """Deterministically verify the resolved role source reached *final_prompt*.

    Resolves the role source via the same candidate order ``_inject_skill_context``
    uses (PromptAssembler role prompt first, then the legacy 3-tier) and checks
    whitespace-normalized containment of the resolved source's content.

    Returns a ``RoleApplicationVerdict``; never raises (best-effort verification
    must never break a dispatch). A resolution/content error degrades to
    ``role_applied=False`` with the reason recorded.
    """
    try:
        if project_root is None:
            project_root = resolve_project_root()
        role_slug = (role or "").strip()
        candidates = list(_candidate_sources(terminal_id, role_slug, project_root))

        # Resolve the source the injector would actually use: the FIRST existing
        # candidate in priority order. Only that source's content may evidence
        # role application. Content from a lower-priority tier must never carry
        # the verdict when a higher-priority source exists but was not injected
        # (round-3 gate fix: terminal fallback content used to stamp
        # role_applied=True while agents/<role>/CLAUDE.md was absent).
        resolved = next(
            ((tier, path) for tier, path in candidates if path.is_file()),
            None,
        )
        if resolved is None:
            return RoleApplicationVerdict(
                role_applied=False,
                tier=TIER_NONE,
                reason="no role source resolved (prompt_assembler/agents/skills/terminal)",
                source_path=None,
            )

        resolved_tier, resolved_path = resolved
        content = resolved_path.read_text(encoding="utf-8", errors="ignore")
        if not content.strip():
            return RoleApplicationVerdict(
                role_applied=False,
                tier=resolved_tier,
                reason="role source resolved but its content is empty",
                source_path=str(resolved_path),
            )

        if _content_present(final_prompt, content):
            return RoleApplicationVerdict(
                role_applied=True,
                tier=resolved_tier,
                reason=None,
                source_path=str(resolved_path),
            )

        # Resolved source content is absent. Record whether a lower-priority
        # source's content IS present — that is the exact false-positive shape a
        # naive "any tier present" check would have matched, and the reader
        # should not have to guess which source it was. The boolean plus the
        # resolved tier carry the verdict; the note only explains the failure
        # mode, it is not a new status layer.
        lower_tier = _lower_tier_present(final_prompt, candidates, resolved_tier)
        reason = (
            "role source resolved but its content is absent from the final prompt"
        )
        if lower_tier is not None:
            reason += (
                f"; lower-priority {lower_tier} content IS present but cannot "
                "evidence role application when the resolved source is absent"
            )
        return RoleApplicationVerdict(
            role_applied=False,
            tier=resolved_tier,
            reason=reason,
            source_path=str(resolved_path),
        )
    except Exception as exc:  # noqa: BLE001 — verification must never break a dispatch
        logger.warning("role_application: verify_role_applied failed (non-fatal): %s", exc)
        return RoleApplicationVerdict(
            role_applied=False,
            tier=TIER_NONE,
            reason=f"role-applied verification error: {exc}",
            source_path=None,
        )
