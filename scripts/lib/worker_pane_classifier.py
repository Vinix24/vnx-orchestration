"""worker_pane_classifier.py — classify a detached worker's tmux pane (OI-863).

A DETACHED tmux worker has no human to answer a Claude Code permission prompt.
When it hits one, the pane shows the permission box ("Permission rule
Bash(...) requires confirmation ... Do you want to proceed?") and the worker
silently waits: no event, no receipt, no exit — only the pane betrays it.  The
dispatch deadline (hours) is the only thing that would otherwise fire.

This module classifies a SINGLE pane capture into a small set of worker
states.  The load-bearing distinction (OI-863): ``awaiting_permission`` is a
RECOVERABLE state — one keystroke (a relay send-keys answer) saves the worker —
and must never be conflated with a dead/stalled worker, which a monitor would
be justified in killing.  Classification is a pure function of the pane text so
it is trivially testable and lane-agnostic.

The prompt markers mirror ``worker_permission_relay.PROMPT_MARKERS`` (the
active-responder twin).  A drift here means the detector stops seeing a prompt
the relay would answer — or vice versa; a parity test guards against it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Keep in parity with worker_permission_relay.PROMPT_MARKERS.  Matched
# case-insensitively and kept tolerant: never pinned to one Claude Code
# version's exact wording (the lane has been burned by version-specific TUI
# scraping before).
PROMPT_MARKERS = (
    "do you want to proceed",
    "do you want to make this edit",
    "do you want to create",
    "requires confirmation",
    "requires your permission",
)

# Version-robust "Claude is actively working" indicators (mirrors
# TmuxInteractiveDispatch._looks_working): the structural token-counter shape
# the TUI prints during a turn ("(18s · ↓ 739 tokens)"), plus tolerant legacy
# literals ("esc to interrupt").
_WORKING_TOKEN_RE = re.compile(r"\(\s*\d+\s*s\b[^)\n]*tokens?\b", re.IGNORECASE)
_WORKING_LITERALS = ("esc to interrupt", "to interrupt")

# Idle-at-input-prompt markers (bottom of the pane when Claude is ready for
# input).  The glyph/tail is checked AFTER the working indicators so a pane
# that shows both ("? for shortcuts" plus a running token counter) classifies
# as working, not idle.
_IDLE_GLYPHS = ("❯",)
_IDLE_MARKERS = ("for shortcuts", "welcome to claude")

# State labels — deliberately a small, closed set so consumers can switch on
# them without string drift.
STATE_AWAITING_PERMISSION = "awaiting_permission"
STATE_WORKING = "working"
STATE_AWAITING_INPUT = "awaiting_input"
STATE_STALLED = "stalled"
STATE_DEAD = "dead"
STATE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorkerPaneState:
    """The classification of one pane capture."""

    state: str
    matched_marker: Optional[str] = None
    detail: str = ""

    @property
    def is_awaiting_permission(self) -> bool:
        return self.state == STATE_AWAITING_PERMISSION


def classify_worker_pane(content: str) -> WorkerPaneState:
    """Classify a single tmux pane capture into a worker state.

    Priority order (a pane can satisfy several; the first wins):

      1. ``awaiting_permission`` — a permission prompt marker is visible.
         Highest priority because it is the RECOVERABLE state: the worker is
         alive and waiting on exactly one input; killing it would discard a
         rescueable dispatch.
      2. ``working`` — active-work indicators (token counter / legacy literal).
      3. ``awaiting_input`` — idle at the Claude input prompt (ready for a new
         prompt, not blocked).
      4. ``dead`` — the capture is empty/whitespace (session or process gone).
      5. ``stalled`` — content present but none of the above signals.

    Returns ``WorkerPaneState`` — never raises on malformed input.
    """
    if content is None:
        return WorkerPaneState(STATE_UNKNOWN, detail="no pane content provided")
    if not content.strip():
        return WorkerPaneState(STATE_DEAD, detail="empty pane capture")

    lowered = content.lower()
    for marker in PROMPT_MARKERS:
        if marker in lowered:
            return WorkerPaneState(
                STATE_AWAITING_PERMISSION,
                matched_marker=marker,
                detail="permission prompt visible on pane",
            )

    if _WORKING_TOKEN_RE.search(content):
        return WorkerPaneState(STATE_WORKING, detail="token-counter working indicator")
    if any(lit in lowered for lit in _WORKING_LITERALS):
        return WorkerPaneState(STATE_WORKING, detail="working literal present")

    lines = [ln for ln in content.splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    tail_low = tail.lower()
    if any(glyph in tail for glyph in _IDLE_GLYPHS) or any(
        m in tail_low for m in _IDLE_MARKERS
    ):
        return WorkerPaneState(STATE_AWAITING_INPUT, detail="idle at input prompt")

    return WorkerPaneState(STATE_STALLED, detail="content but no prompt/working/idle signal")
