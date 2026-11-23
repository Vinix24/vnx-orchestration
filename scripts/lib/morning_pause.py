#!/usr/bin/env python3
"""The morning pause as a function (ADR-095 decisions 1 and 2).

Pure core: no clock, no I/O. ``now`` is injected and must be timezone-aware.
``is_paused`` answers from the window, the clock and the decision status, and
names the reason from a closed set. After the deadline it is never paused,
whatever ``decided`` says, so a dead job or a sleeping Mac cannot hold the
fabric past the deadline. An unknown decision status is never "decided"
(ADR-063): before the deadline it pauses with ``decision_unknown``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KEY_ENABLED = "VNX_MORNING_PAUSE_ENABLED"
KEY_START = "VNX_MORNING_PAUSE_START"
KEY_DEADLINE = "VNX_MORNING_PAUSE_DEADLINE"
KEY_TZ = "VNX_MORNING_PAUSE_TZ"

REASON_DISABLED = "disabled"
REASON_BEFORE_START = "before_start"
REASON_AFTER_DEADLINE = "after_deadline"
REASON_DECIDED = "decided"
REASON_OPEN_POINTS = "open_points"
REASON_DECISION_UNKNOWN = "decision_unknown"
REASON_NOT_STARTED = "not_started"  # no snapshot today: no round started, so nothing to hold for
REASONS = frozenset((
    REASON_DISABLED, REASON_BEFORE_START, REASON_AFTER_DEADLINE,
    REASON_DECIDED, REASON_OPEN_POINTS, REASON_DECISION_UNKNOWN, REASON_NOT_STARTED,
))

_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_TRUE = frozenset(("1", "true", "yes", "on"))
_FALSE = frozenset(("0", "false", "no", "off"))


@dataclass(frozen=True)
class PauseWindow:
    enabled: bool
    start: str
    deadline: str
    tz: str


@dataclass(frozen=True)
class PauseVerdict:
    paused: bool
    reason: str


def _parse_hhmm(key: str, value: object) -> time:
    match = _TIME_RE.match(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"{key}: expected HH:MM (00:00-23:59), got {value!r}")
    return time(int(match.group(1)), int(match.group(2)))


def _parse_bool(key: str, value: object) -> bool:
    text = str(value).strip().lower() if value is not None else ""
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"{key}: expected a boolean (0/1/true/false), got {value!r}")


def _zone(tz: object) -> ZoneInfo:
    try:
        return ZoneInfo(tz)  # type: ignore[arg-type]
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError) as exc:
        raise ValueError(f"{KEY_TZ}: unknown timezone {tz!r}") from exc


def window_from_config(get: Callable[[str], object]) -> PauseWindow:
    """Build the window from a getter for the four keys; a bad value raises ``ValueError``."""
    enabled = _parse_bool(KEY_ENABLED, get(KEY_ENABLED))
    start, deadline, tz = get(KEY_START), get(KEY_DEADLINE), get(KEY_TZ)
    start_t = _parse_hhmm(KEY_START, start)
    deadline_t = _parse_hhmm(KEY_DEADLINE, deadline)
    _zone(tz)
    if not start_t < deadline_t:
        raise ValueError(f"{KEY_START}: start {start!r} must be before {KEY_DEADLINE} {deadline!r}")
    return PauseWindow(enabled, start, deadline, tz)  # type: ignore[arg-type]


def in_window(window: PauseWindow, now: datetime) -> bool:
    """True from the start time up to, not including, the deadline in the window's zone."""
    local = now.astimezone(_zone(window.tz)).time().replace(tzinfo=None)
    return _parse_hhmm(KEY_START, window.start) <= local < _parse_hhmm(KEY_DEADLINE, window.deadline)


def is_paused(window: PauseWindow, now: datetime, *, decided: Optional[bool]) -> PauseVerdict:
    """Is the fabric paused at ``now``? ``decided`` is True/False, or None when unknown."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not window.enabled:
        return PauseVerdict(False, REASON_DISABLED)
    local = now.astimezone(_zone(window.tz)).time().replace(tzinfo=None)
    if local < _parse_hhmm(KEY_START, window.start):
        return PauseVerdict(False, REASON_BEFORE_START)
    if local >= _parse_hhmm(KEY_DEADLINE, window.deadline):
        return PauseVerdict(False, REASON_AFTER_DEADLINE)
    if decided is True:
        return PauseVerdict(False, REASON_DECIDED)
    if decided is False:
        return PauseVerdict(True, REASON_OPEN_POINTS)
    return PauseVerdict(True, REASON_DECISION_UNKNOWN)
