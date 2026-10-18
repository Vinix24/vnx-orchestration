"""The values the plugin writes into its files, and the check that a value read back is one of them.

Anything under .claude/rotation can be written by another process of the user, or
shipped in a cloned repository, and some of its values reach the model's context
(additionalContext) or the user's line (systemMessage). So a value read back is
used only when it has exactly the form the plugin writes: a time in TIME_FORMAT,
an outdated reason from the closed list OUTDATED_REASONS, a session id of plain
file-name characters, a digest of 64 hex digits, a generation of GENERATION_HEX hex
digits. Paths are checked here for their form only; readers show the path the
plugin builds itself, never the one read.

The handover body is the one intended exception: it is injected as the handover.
After /clear it is injected only while it matches the marker's sha256. When the
user picks a handover from the `resume` list, the file is injected as it is, and
the injected context says whether it still holds the text the marker's sha256 was
taken of (handover_injection.injection_context). Standard library only.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

import transcript_work

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# One handover the plugin marked ready: its claim, its resume id and its journal lines name it.
GENERATION_HEX = 16
_GENERATION = re.compile(r"[0-9a-f]{%d}" % GENERATION_HEX)
# A session id and a generation share one file name in claimed-<origin>-<generation>.json.
MAX_SESSION_ID = 120
ARCHIVE_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-[A-Za-z0-9_-]+\.md")
HANDOVER_NAME = re.compile(r"handover-[A-Za-z0-9_-]+\.md")
# A tmux pane id as tmux gives it in TMUX_PANE: "%" and digits, nothing else.
_PANE = re.compile(r"%[0-9]{1,9}")

# --- the outdated reasons: a closed list --------------------------------------

PROMPT_AFTER = "a prompt after it"
TOOL_CALL_AFTER = "a tool call after it"
REWRITTEN_INVALID = "the handover was rewritten and is not valid"
FILE_GONE = "the handover file is gone"
FILE_CHANGED = "the handover file changed after it was checked"
OVER_AGE = "it is older than handover_max_age_minutes allows"
OUTDATED_REASONS = (
    PROMPT_AFTER, TOOL_CALL_AFTER, REWRITTEN_INVALID, FILE_GONE, FILE_CHANGED, OVER_AGE,
) + transcript_work.STALE_REASONS
REASON_NOT_RECORDED = "reason not recorded"

# --- why an automatic /clear stopped: a closed list (tmux_clear) ----------------------

CLEAR_ABORTS: Dict[str, str] = {
    "job": "its job file could not be read",
    "storage": "the project's .claude/rotation is a symlink or not a directory",
    "lock": "the lock of the pane could not be read or taken",
    "settle": "the session did not become idle",
    "not_current": "the handover no longer covers the latest work",
    "replaced": "a newer handover replaced it",
    "marker_unreadable": "its marker or the handover could not be read",
    "transcript_unreadable": "the transcript could not be read",
    "pane_unreadable": "the pane could not be read",
    "pane_gone": "the pane is gone",
    "pane_mode": "the pane is in copy or view mode",
    "pane_synchronized": "synchronize-panes is on in its window",
    "no_input_box": "no input box is on the screen, so a dialog or a menu may be open",
    "busy": "the session is busy",
    "input": "there is text in the input line",
    "process": "the claude process of the session is not running in that pane",
    "process_background": "the claude process of the session is not in the foreground of the pane's terminal",
    "process_stopped": "the claude process of the session is stopped",
    "process_unreadable": "the process table could not be read",
    "typed": "the typed text did not read back as typed",
    "send": "tmux could not send the keys",
    "journal": "its line in injections.jsonl could not be written",
    "no_ack": "the delivery of the handover to the fresh session was not confirmed in time",
    "ack_unreadable": "injections.jsonl could not be read",
    "error": "an unexpected error stopped it (errors.log says which)",
}


# --- single values ----------------------------------------------------------------

def is_safe_session_id(value: Any) -> bool:
    """One plain file name: letters, digits, "_" and "-" only, so no separator, dot or NUL."""
    return isinstance(value, str) and len(value) <= MAX_SESSION_ID and _SAFE_ID.fullmatch(value) is not None


def is_plugin_time(value: Any) -> bool:
    """A UTC time exactly as the plugin writes it (TIME_FORMAT), and a real one."""
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, TIME_FORMAT)
    except ValueError:
        return False
    return True


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def is_generation(value: Any) -> bool:
    return isinstance(value, str) and _GENERATION.fullmatch(value) is not None


def is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_reason(value: Any) -> bool:
    return isinstance(value, str) and value in OUTDATED_REASONS


def is_pane_id(value: Any) -> bool:
    return isinstance(value, str) and _PANE.fullmatch(value) is not None


def is_clear_abort(value: Any) -> bool:
    return isinstance(value, str) and value in CLEAR_ABORTS


def is_plain_path(value: Any) -> bool:
    """An absolute path in the normal form the plugin writes: no ".", "..", "//" or trailing separator."""
    return (isinstance(value, str) and os.path.isabs(value) and "\0" not in value
            and os.path.normpath(value) == value)


def _named(pattern: "re.Pattern[str]") -> Callable[[Any], bool]:
    return lambda value: is_plain_path(value) and pattern.fullmatch(os.path.basename(value)) is not None


is_archive_path = _named(ARCHIVE_NAME)
is_handover_path = _named(HANDOVER_NAME)


def is_stamp(value: Any) -> bool:
    """The inode, size and modification time a claim records of the handover it read."""
    return (isinstance(value, dict) and set(value) == {"ino", "size", "mtime_ns"}
            and all(is_count(item) for item in value.values()))


# --- what a marker may show ----------------------------------------------------

def marker_value_problem(kind: str, marker: Dict[str, Any]) -> Optional[str]:
    """Why a marker value that is shown or relied on is not one the plugin writes; None when all are.

    written_at always, claimed_at and claimed_file for a claim, outdated_at and
    outdated_reason wherever they are present. An outdated marker may lack the
    reason: a crash between its move and its fields leaves it so, and it is then
    shown as REASON_NOT_RECORDED.
    """
    times = ("written_at", "claimed_at") if kind == "claimed" else ("written_at",)
    times += ("outdated_at",) if "outdated_at" in marker else ()
    for field in times:
        if not is_plugin_time(marker.get(field)):
            return "its %s is not a time the plugin writes" % field
    if "outdated_reason" in marker and not is_reason(marker["outdated_reason"]):
        return "its outdated_reason is not one the plugin writes"
    if kind == "claimed" and not is_stamp(marker.get("claimed_file")):
        return "its claimed_file is not the inode, size and time the plugin records"
    return None


# --- the lines of injections.jsonl ------------------------------------------------

# One line per transition of a handover, written before the transition itself.
# A "kept" line names a file the cleanup found at the handover path that no marker described: no generation.
# A "delivered" line is the one line written after its step rather than before: the emit of a
# claimed handover returned (handover_injection.deliver); "injected" only says it is about to go out.
# The clear_* lines are the steps of an automatic /clear in a tmux pane (tmux_clear): the pane's lock
# taken (clear_started) or a stale one broken (clear_lock_broken), Enter after /clear (clear_entered),
# the successor's delivery seen (clear_acknowledged), Enter after the line that continues
# (clear_continued), a stop (clear_aborted, which says whether the job left what it typed in the
# input line) and its message repeated to the user (clear_told).
_CLEAR = ("origin_session_id", "generation", "pane")
JOURNAL_EVENTS: Dict[str, Tuple[str, ...]] = {
    "ready": ("origin_session_id", "generation", "sha256"),
    "outdated": ("origin_session_id", "generation", "sha256", "reason"),
    "claimed": ("origin_session_id", "generation", "session_id", "sha256", "archive"),
    "injected": ("via", "origin_session_id", "generation", "session_id", "handover", "archive", "sha256", "chars",
                 "mode"),
    "delivered": ("via", "origin_session_id", "generation", "session_id"),
    "kept": ("origin_session_id", "sha256", "archive"),
    "clear_lock_broken": ("pane",),
    "clear_started": _CLEAR,
    "clear_entered": _CLEAR,
    "clear_acknowledged": _CLEAR + ("session_id",),
    "clear_continued": _CLEAR + ("session_id",),
    "clear_aborted": _CLEAR + ("abort_reason", "left_in_line"),
    "clear_told": _CLEAR,
}
INJECTION_VIAS = ("clear", "resume")
INJECTION_MODES = ("inline", "pointer")

_RECORD_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "at": is_plugin_time,
    "origin_session_id": is_safe_session_id,
    "generation": is_generation,
    "session_id": is_safe_session_id,
    "sha256": is_sha256,
    "reason": is_reason,
    "archive": is_archive_path,
    "handover": lambda value: value is None or is_handover_path(value),
    "via": lambda value: value in INJECTION_VIAS,
    "chars": is_count,
    "mode": lambda value: value in INJECTION_MODES,
    "pane": is_pane_id,
    "abort_reason": is_clear_abort,
    "left_in_line": lambda value: isinstance(value, bool),
}


def _record_value_ok(event: str, field: str, value: Any) -> bool:
    if event == "injected" and field in ("origin_session_id", "generation") and value is None:
        return True  # an archive copy resumed without a line that names its origin
    return _RECORD_CHECKS[field](value)


def record_problem(record: Any) -> Optional[str]:
    """Why a line of injections.jsonl is not one the plugin writes; None when it is.

    Exactly the fields of its event, each in the form the plugin writes it.
    """
    if not isinstance(record, dict):
        return "it is not an object"
    fields = JOURNAL_EVENTS.get(record.get("event")) if isinstance(record.get("event"), str) else None
    if fields is None:
        return "its event is not one the plugin writes"
    if set(record) != {"event", "at"} | set(fields):
        return "its fields are not those of a %s line" % record["event"]
    for field in ("at",) + fields:
        if not _record_value_ok(record["event"], field, record[field]):
            return "its %s is not one the plugin writes" % field
    return None
