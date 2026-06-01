#!/usr/bin/env python3
"""tmux_conversation_normalizer.py — normalize raw pipe-pane log to CanonicalEvents.

Reads the raw pipe-pane conversation log produced by the tmux-spawn lane,
strips ANSI/OSC/control sequences and TUI redraw noise, deduplicates redraw
frames, and appends CanonicalEvent records to the EventStore so the tmux-spawn
lane is observable in the dashboard live-stream and minable by the learning loop.

Called at dispatch close-out by TmuxInteractiveDispatch._run_capture_normalizer.
Best-effort: callers must wrap in try/except.

BILLING SAFETY: No Anthropic SDK imports. Local filesystem only.
"""

from __future__ import annotations

import fcntl
import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from event_store import EventStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ANSI / OSC stripping
# ---------------------------------------------------------------------------

# Comprehensive escape-sequence pattern covering:
# - CSI sequences: ESC [ <params> <final-byte>  (colours, cursor movement, etc.)
# - OSC sequences: ESC ] <body> ST (window titles, hyperlinks, etc.)
#   ST is either BEL (\x07) or ESC \
# - DCS / SOS / PM / APC: ESC [PX^_] <body> ST
# - Any other two-character ESC + one-byte sequence
# - Carriage returns (\r) and NUL bytes (\x00)
_ANSI_ESCAPE_RE = re.compile(
    r'(?:'
    r'\x1b\[[0-9;?]*[a-zA-Z@]'               # CSI: ESC [ params final
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'    # OSC: ESC ] body ST
    r'|\x1b[PX\^_][^\x1b]*\x1b\\'            # DCS/SOS/PM/APC
    r'|\x1b.'                                  # Any other ESC + 1 char
    r'|\r'                                     # CR
    r'|\x00'                                   # NUL
    r')'
)

# TUI "redraw frame" detection: cursor-absolute-position sequences (ESC [ row ; col H/f)
# and screen-clear sequences (ESC [ 2J, ESC [ J, ESC [ K).
# A line containing >= 3 absolute cursor-positioning sequences is a TUI redraw artefact.
_CURSOR_ABS_RE = re.compile(r'\x1b\[\d+;\d+[Hf]')
_SCREEN_CLEAR_RE = re.compile(r'\x1b\[(?:2J|[JK])')

_CURSOR_ABS_THRESHOLD = 3
_SCREEN_CLEAR_THRESHOLD = 2


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def strip_ansi_osc(text: str) -> str:
    """Strip ANSI escape, OSC, DCS, and common control sequences from terminal output."""
    return _ANSI_ESCAPE_RE.sub('', text)


def is_redraw_frame(raw_line: str) -> bool:
    """Return True if *raw_line* is dominated by TUI cursor/clear sequences.

    Lines with >= 3 absolute cursor-positioning hits OR >= 2 screen-clear hits
    are TUI redraw artefacts (full-screen redraws of the Claude Code TUI chrome)
    and do not contribute conversation content.
    """
    if len(_CURSOR_ABS_RE.findall(raw_line)) >= _CURSOR_ABS_THRESHOLD:
        return True
    if len(_SCREEN_CLEAR_RE.findall(raw_line)) >= _SCREEN_CLEAR_THRESHOLD:
        return True
    return False


def normalize_conversation(
    raw_log: Path,
    event_store: "EventStore",
    terminal_id: str,
    dispatch_id: str,
    model: str,
) -> int:
    """Read raw pipe-pane log, strip noise, append CanonicalEvents to EventStore.

    Strategy:
    1. Read the full raw log under LOCK_SH.
    2. Process line-by-line: skip TUI redraw-heavy lines, strip ANSI/OSC from
       the rest, drop empty lines, deduplicate identical lines.
    3. Emit one ``text`` CanonicalEvent with all unique cleaned lines joined.
    4. Emit one ``complete`` CanonicalEvent as a terminal marker.

    Returns the number of CanonicalEvents appended (0 if nothing to normalize).

    ``provider_meta`` fields:
    - ``lane``:     ``tmux_interactive``
    - ``source``:   ``tmux_pipe_pane``
    - ``raw_log``:  path to the raw log file (absolute string)
    """
    if not raw_log.exists():
        return 0

    try:
        if raw_log.stat().st_size == 0:
            return 0
    except OSError:
        return 0

    # Read the full raw log under a shared lock.
    try:
        with raw_log.open("r", encoding="utf-8", errors="replace") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                raw_content = fh.read()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        logger.debug("normalizer: read failed for %s: %s", raw_log, exc)
        return 0

    # Ensure lib dir is on sys.path for sibling imports.
    _lib_dir = str(Path(__file__).resolve().parent)
    if _lib_dir not in sys.path:
        sys.path.insert(0, _lib_dir)

    from canonical_event import CanonicalEvent  # noqa: PLC0415

    # Process line-by-line: skip redraw frames, strip, deduplicate.
    seen: set[str] = set()
    clean_lines: list[str] = []

    for raw_line in raw_content.split('\n'):
        # Skip lines dominated by TUI cursor/clear sequences (full-frame redraws).
        if is_redraw_frame(raw_line):
            continue
        # Strip all remaining ANSI/OSC/control sequences.
        clean = strip_ansi_osc(raw_line).strip()
        if not clean:
            continue
        # Deduplicate: identical lines that appear multiple times in the raw log
        # (e.g. repeated TUI chrome that survived redraw filtering) are emitted once.
        if clean in seen:
            continue
        seen.add(clean)
        clean_lines.append(clean)

    if not clean_lines:
        return 0

    provider_meta: dict = {
        "lane": "tmux_interactive",
        "source": "tmux_pipe_pane",
        "raw_log": str(raw_log),
    }

    events_appended = 0

    # Text event: all unique cleaned lines as a single joined string.
    text_event = CanonicalEvent(
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        provider="claude",
        sub_provider="anthropic",
        event_type="text",
        data={"text": "\n".join(clean_lines)},
        model=model,
        provider_meta=provider_meta,
    )
    event_store.append(terminal_id, text_event, dispatch_id=dispatch_id)
    events_appended += 1

    # Complete event: terminal marker for the learning loop and dashboard.
    complete_event = CanonicalEvent(
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        provider="claude",
        sub_provider="anthropic",
        event_type="complete",
        data={},
        model=model,
        provider_meta=provider_meta,
    )
    event_store.append(terminal_id, complete_event, dispatch_id=dispatch_id)
    events_appended += 1

    logger.debug(
        "normalizer: appended %d events for dispatch=%s terminal=%s",
        events_appended,
        dispatch_id,
        terminal_id,
    )
    return events_appended
