"""Per-session state of the force and hard bands (loop step 3).

It lives in the project's `.claude/rotation/`, beside the files of rotation_state,
behind the same storage gate:

  state/<session>.enforcement.json   the session's level, its crossings, the Stop
                                     block and the user line given per crossing,
                                     its denials, and whether it only informs
  enforcement.jsonl                  one line per change of that state, written
                                     before the change; cut to its newest
                                     JOURNAL_KEEP_LINES once it passes JOURNAL_MAX_BYTES

A session id never holds a dot, so `<session>.enforcement.json` is never the
guard's `state/<session>.json` of any session.

The level is the measured band as enforcement sees it: LOW for below and warn,
which hold nothing back, then FORCE and HARD. A crossing is a move of the level
into FORCE or HARD from a lower level; falling back (after a compaction) and
rising again is a new crossing. `blocked` and `told` hold the number of the
crossing the Stop block and the user line were last given for, so each is given
at most once per crossing.

Every change goes through `update`: under the project lock, which every marker
change holds too, the state is read, changed, every change gets its line in
enforcement.jsonl, and only then is the state file rewritten whole at its own
name through a temporary file of its own (rotation_state.write_json). Hooks of
one session that run at the same moment wait for the lock, so no count is lost.
Nothing is renamed or linked onto another name.

The state file is an input, not a fact: any process of the user can write one,
and a cloned repository can ship one. A state that is not exactly what this
module writes (state_problem) counts as a fresh one and is replaced at the next
change, with one errors.log line. A read that fails (an OSError of the lstat, the
open or the read) is not such a state: load and update raise it, so nothing is
decided or written on it (band_guard). Only values this module checked leave it:
counts, and names from the closed lists LEVELS and INFORM_REASONS. Every read,
write and append goes through a descriptor of the verified directory
(rotation_state.storage). Standard library only.
"""
from __future__ import annotations

import json
import os
import stat
from typing import Any, Callable, Dict, List, Optional, Tuple

import regular_file
import rotation_state
import state_fields

VERSION = 2
LOW, FORCE, HARD = "low", "force", "hard"
LEVELS = (LOW, FORCE, HARD)
# Why a session only informs: the user typed release, or the denial cap was reached.
RELEASE = "release"
DENIAL_CAP = "denial_cap"
INFORM_REASONS = (RELEASE, DENIAL_CAP)
FIELDS = ("version", "level", "crossing", "blocked", "told", "denials", "inform_only", "start_size", "loop_warned")
COUNTS = ("crossing", "blocked", "told", "denials")
SUFFIX = ".enforcement.json"
JOURNAL = "enforcement.jsonl"
JOURNAL_MAX_BYTES = 256 * 1024
JOURNAL_KEEP_LINES = 1000

Events = List[Tuple[str, Dict[str, Any]]]
Change = Callable[[Dict[str, Any]], Tuple[Events, Any]]


def fresh() -> Dict[str, Any]:
    return {"version": VERSION, "level": LOW, "crossing": 0, "blocked": 0, "told": 0, "denials": 0,
            "inform_only": None, "start_size": None, "loop_warned": False}


def state_path(project_dir: str, session_id: str) -> str:
    return os.path.join(os.path.abspath(rotation_state.rotation_dir(project_dir)), "state", session_id + SUFFIX)


def journal_path(project_dir: str) -> str:
    return os.path.join(os.path.abspath(rotation_state.rotation_dir(project_dir)), JOURNAL)


def level_of(band: Optional[str]) -> Optional[str]:
    """The level of a measured band (context_budget.band); None for an unknown size."""
    if band is None:
        return None
    return band if band in (FORCE, HARD) else LOW


def _one_of(value: Any, names: Tuple[str, ...]) -> bool:
    return isinstance(value, str) and value in names


# --- what a state may hold ------------------------------------------------------

def state_problem(state: Any) -> Optional[str]:
    """Why a state is not one this module writes; None when it is."""
    if not isinstance(state, dict) or set(state) != set(FIELDS):
        return "its fields are not those of an enforcement state"
    if not (state_fields.is_count(state["version"]) and state["version"] == VERSION):
        return "it is not a version %d enforcement state" % VERSION
    if not _one_of(state["level"], LEVELS):
        return "its level is not one the plugin writes"
    for field in COUNTS:
        if not state_fields.is_count(state[field]):
            return "its %s is not a count" % field
    if state["blocked"] > state["crossing"] or state["told"] > state["crossing"]:
        return "it gives a Stop block or a user line to a crossing that did not happen"
    if state["inform_only"] is not None and not _one_of(state["inform_only"], INFORM_REASONS):
        return "its inform_only is not one the plugin writes"
    if state["start_size"] is not None and not state_fields.is_count(state["start_size"]):
        return "its start_size is not a count"
    if not isinstance(state["loop_warned"], bool):
        return "its loop_warned is not a boolean"
    return None


def _read_in(directory: int, name: str) -> Optional[bytes]:
    """At most MAX_READ_BYTES + 1 bytes of the regular file `name` in `directory`; None when something else is there.

    FileNotFoundError when nothing is there, and any other OSError of the lstat,
    the open or the read as it comes: a failed read says nothing about the file.
    """
    if not stat.S_ISREG(os.lstat(name, dir_fd=directory).st_mode):
        return None
    with regular_file.open_regular(name, directory) as handle:
        return handle.read(regular_file.MAX_READ_BYTES + 1)


def _read(project_dir: str, session_id: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """(stored state, None); (fresh state, why the stored one is not used); (fresh state, None) when there is none.

    OSError when the state cannot be read: a read that fails is no reason to replace what is stored.
    """
    try:
        with rotation_state.storage(project_dir, "state") as directory:
            raw = _read_in(directory, session_id + SUFFIX)
    except FileNotFoundError:
        return fresh(), None
    if raw is None or len(raw) > regular_file.MAX_READ_BYTES:
        return fresh(), "it is not a regular file of at most %d bytes" % regular_file.MAX_READ_BYTES
    try:
        state = json.loads(raw.decode("utf-8"))
    except ValueError:
        return fresh(), "it is not JSON"
    problem = state_problem(state)
    return (fresh(), problem) if problem else (state, None)


def load(project_dir: str, session_id: str) -> Dict[str, Any]:
    """The session's state as stored, or a fresh one when there is none or it is not one this module writes.

    OSError when it cannot be read: the caller decides nothing on a read that failed.
    """
    return _read(project_dir, session_id)[0]


# --- the journal: enforcement.jsonl ------------------------------------------------

# One line per change of a session's state, written before the change.
JOURNAL_EVENTS: Dict[str, Tuple[str, ...]] = {
    "crossing": ("session_id", "level", "crossing"),
    "fall": ("session_id", "level", "crossing"),
    "told": ("session_id", "crossing"),
    "stop_block": ("session_id", "crossing"),
    "denial": ("session_id", "level", "denials"),
    "inform_only": ("session_id", "reason", "denials"),
    "reset": ("session_id", "denials"),
    "start": ("session_id", "start_size"),
    "loop_warned": ("session_id",),
}
_RECORD_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "at": state_fields.is_plugin_time,
    "session_id": state_fields.is_safe_session_id,
    "level": lambda value: _one_of(value, LEVELS),
    "crossing": state_fields.is_count,
    "denials": state_fields.is_count,
    "reason": lambda value: _one_of(value, INFORM_REASONS),
    "start_size": state_fields.is_count,
}


def record_problem(record: Any) -> Optional[str]:
    """Why a line of enforcement.jsonl is not one the plugin writes; None when it is."""
    if not isinstance(record, dict):
        return "it is not an object"
    event = record.get("event")
    fields = JOURNAL_EVENTS.get(event) if isinstance(event, str) else None
    if fields is None:
        return "its event is not one the plugin writes"
    if set(record) != {"event", "at"} | set(fields):
        return "its fields are not those of a %s line" % event
    for field in ("at",) + fields:
        if not _RECORD_CHECKS[field](record[field]):
            return "its %s is not one the plugin writes" % field
    return None


def _journal(project_dir: str, records: List[Dict[str, Any]]) -> None:
    """Append the lines in one write, in the verified directory; the caller holds the project lock."""
    for record in records:
        problem = record_problem(record)
        if problem:
            raise ValueError("enforcement journal line refused: %s" % problem)
    data = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records).encode("utf-8")
    with rotation_state.storage(project_dir, create=True) as directory:
        rotation_state.append_lines(directory, JOURNAL, data, ".enforcement-")
        if os.lstat(JOURNAL, dir_fd=directory).st_size > JOURNAL_MAX_BYTES:
            rotation_state.keep_last_lines(directory, JOURNAL, JOURNAL_KEEP_LINES, ".enforcement-")


# --- changing the state ---------------------------------------------------------------

def update(project_dir: str, session_id: str, change: Change) -> Any:
    """Change the session's state under the project lock; `change`'s result.

    `change` gets the state as stored (a fresh one when there is none or it is not
    one this module writes), changes it in place and returns (events, result),
    events being (event, fields) pairs of JOURNAL_EVENTS. Every event gets its line
    in enforcement.jsonl, then the state file is rewritten; with no events nothing
    is written. Raises OSError or rotation_state.LockTimeout when the lock, a line
    or the state cannot be had, read or written, and ValueError when `change` makes a
    state or a line this module would refuse to read back; the state on disk is then
    the one before, possibly with its lines already in the journal.
    """
    with rotation_state.project_lock(project_dir):
        state, rejected = _read(project_dir, session_id)
        events, result = change(state)
        if not events:
            return result
        problem = state_problem(state)
        if problem:
            raise ValueError("enforcement state refused: %s" % problem)
        at = rotation_state.utc_now()
        _journal(project_dir, [dict(fields, event=event, at=at, session_id=session_id) for event, fields in events])
        if rejected:
            rotation_state.log_error(project_dir, "%s context-rotation: rejected the enforcement state of %s: %s; "
                                     "replaced by a fresh one" % (at, session_id, rejected))
        rotation_state.write_json(project_dir, state_path(project_dir, session_id), state)
    return result


def apply(project_dir: str, session_id: str, change: Change) -> Any:
    """`change`'s result: a dry run on the state as read, and `update` only when that run changes something.

    `change` must depend on the state alone, so the run under the lock decides
    again on the state it reads there. Raises what load and update raise.
    """
    events, result = change(load(project_dir, session_id))
    if not events:
        return result
    return update(project_dir, session_id, change)


# --- the changes ---------------------------------------------------------------------

def observe(state: Dict[str, Any], level: str) -> Events:
    """Record a measured level: a rise is a new crossing, a fall is recorded so the next rise is one too."""
    old = state["level"]
    if level == old:
        return []
    state["level"] = level
    if LEVELS.index(level) > LEVELS.index(old):
        state["crossing"] += 1
        return [("crossing", {"level": level, "crossing": state["crossing"]})]
    return [("fall", {"level": level, "crossing": state["crossing"]})]


def tell(state: Dict[str, Any]) -> Events:
    """The crossing's user line, at most once per crossing: [] when it was given already."""
    if state["crossing"] == 0 or state["told"] == state["crossing"]:
        return []
    state["told"] = state["crossing"]
    return [("told", {"crossing": state["crossing"]})]


def block_stop(state: Dict[str, Any]) -> Events:
    """The crossing's Stop block, at most once per crossing: [] when it was given already."""
    if state["crossing"] == 0 or state["blocked"] == state["crossing"]:
        return []
    state["blocked"] = state["crossing"]
    return [("stop_block", {"crossing": state["crossing"]})]


def deny(state: Dict[str, Any], cap: int) -> Events:
    """Count a denial; the one that reaches `cap` makes the session inform-only."""
    state["denials"] += 1
    events = [("denial", {"level": state["level"], "denials": state["denials"]})]
    if state["denials"] >= cap and state["inform_only"] is None:
        state["inform_only"] = DENIAL_CAP
        events.append(("inform_only", {"reason": DENIAL_CAP, "denials": state["denials"]}))
    return events


def release(state: Dict[str, Any]) -> Events:
    """The user's release: the session only informs from now on."""
    if state["inform_only"] == RELEASE:
        return []
    state["inform_only"] = RELEASE
    return [("inform_only", {"reason": RELEASE, "denials": state["denials"]})]


def reset_denials(state: Dict[str, Any]) -> Events:
    """A handover write: the denials counted so far no longer count toward the cap."""
    if state["denials"] == 0:
        return []
    cleared, state["denials"] = state["denials"], 0
    return [("reset", {"denials": cleared})]


def record_start(state: Dict[str, Any], size: int) -> Events:
    """Record the start size of an injected session: the first known measurement, never overwritten."""
    if state["start_size"] is not None:
        return []
    state["start_size"] = size
    return [("start", {"start_size": size})]


def loop_warn(state: Dict[str, Any]) -> Events:
    """The loop line, at most once per session: [] when it was given already."""
    if state["loop_warned"]:
        return []
    state["loop_warned"] = True
    return [("loop_warned", {})]
