"""Handing a handover to a session: the order that makes a crash recoverable.

the claim's line in injections.jsonl -> claim (the marker moves to the claim name
of its generation) -> copy to the archive -> the original moves into the archive
(rotation_state.retire_original) -> the injection's line -> emit -> the delivery's
line, once the emit returned -> the claimed marker removed last, only once that
line is written. A crash at any
point leaves the claimed marker naming an archive copy that holds the claimed
bytes, or the original, and `resume` lists it. Automatic injection happens at
most once; recovery is manual, and reads the archive copy first: the original is
used only while there is no copy yet and it still holds the claimed bytes. No
file that was ever at the handover path is deleted, so a handover written
meanwhile stays. Every marker here passed rotation_state.marker_problem, and
every file is read through the verified storage directories. A storage failure
before the emit (archive copy, log line) is logged and nothing is emitted, so the
claim stays for `resume`; after the emit nothing raises.

What is shown comes from the plugin, never as text from disk: the paths a marker
gives are the ones rotation_state.read_marker builds, times and outdated reasons
passed state_fields, an entry id names a generation or an archive file name the
plugin writes, and a journal line is shown only when state_fields.record_problem
accepts it. The handover body is the one intended exception, and the context
around it says whether it still holds the text its marker was checked against.
Standard library only.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import rotation_state
import state_fields

INLINE_LIMIT = 9000
SKILL_COMMAND = "context-rotation:rotate"
RESUME_HINT = "Type /%s resume to see them and pick one." % SKILL_COMMAND
PICK_HINT = "Type /%s resume <number> or resume <id> with a number or an id from the list." % SKILL_COMMAND
USAGE = "usage: /%s [write | release | resume [<number> | <id>]]" % SKILL_COMMAND
LISTED_AT_START = 5
# <kind>-<session>-<generation> for a marker, or an archive file name without ".md".
ENTRY_ID = re.compile(
    r"(?:ready|outdated|claimed)-[A-Za-z0-9_-]{1,%d}-[0-9a-f]{%d}|[0-9]{8}T[0-9]{12}Z-[A-Za-z0-9_-]{1,%d}"
    % (state_fields.MAX_SESSION_ID, state_fields.GENERATION_HEX, state_fields.MAX_SESSION_ID + 8))
CHECKED = " It holds exactly the text that was checked when it was marked ready."
NOT_CHECKED = (
    " It no longer holds the text that was checked when it was marked ready: the file was changed "
    "afterwards and the change was not checked, so check it against the current state before acting on it."
)


def injection_context(text: str, archive: str, outdated_reason: Optional[str] = None,
                      checked: Optional[bool] = None) -> Dict[str, str]:
    """The additionalContext for a handover: inline up to INLINE_LIMIT, else a pointer.

    `checked` says whether the text is the one its marker's sha256 was taken of
    (None for an archive copy resumed without a marker), and the context says so.
    """
    warning = "" if checked is None else (CHECKED if checked else NOT_CHECKED)
    if outdated_reason:
        warning += (
            " It was marked outdated (%s), so check the current state of the work before "
            "acting on it." % outdated_reason
        )
    head = (
        "This session continues from a handover written by an earlier session. A copy is "
        'at %s.%s Continue from step 1 under "## Next steps".' % (archive, warning)
    )
    inline = head + "\n\n" + text
    if len(inline) <= INLINE_LIMIT:
        return {"text": inline, "mode": "inline"}
    pointer = (
        "A handover from an earlier session is at %s. Read this file first, then continue "
        'from step 1 under "## Next steps".%s' % (archive, warning)
    )
    return {"text": pointer, "mode": "pointer"}


def _record(via: str, origin: Optional[str], generation: Optional[str], successor: str, handover: Optional[str],
            archive: str, data: bytes, mode: str) -> Dict[str, Any]:
    return {
        "event": "injected",
        "at": rotation_state.utc_now(),
        "via": via,
        "origin_session_id": origin,
        "generation": generation,
        "session_id": successor,
        "handover": handover,
        "archive": archive,
        "sha256": rotation_state.sha256_hex(data),
        "chars": len(data.decode("utf-8", "replace")),
        "mode": mode,
    }


def _no_step(name: str) -> None:
    return None


def deliver(project_dir: str, claimed: str, marker: Dict[str, Any], data: bytes, successor: str,
            via: str, emit: Callable[[str, str], None], at: Callable[[str], None] = _no_step) -> Optional[str]:
    """Archive, log, emit and clean up a claimed handover. Returns the archive path.

    `data` holds the claimed bytes (sha256 equal to the marker's claimed_sha256),
    and the context says whether they are the bytes the marker was checked against.
    The context and the log line name the archive copy written from `data`, which
    nothing writes to afterwards. The original moves into the archive before the
    emit under a name of its own, and is never what they name.
    None when the archive copy or the log line could not be written (OSError,
    LockTimeout): the failure goes to rotation_state.log_failure, nothing is
    emitted, and the claimed marker stays for `resume`. An emit that raises
    leaves the claim too, and no `delivered` line: only an emit that returned
    gets one (rotation_state.log_delivery), which the automatic /clear waits for.
    The claimed marker is removed only once that line is written, so the claim
    never ends without its line: when the line cannot be written, the claim stays
    for `resume` as well.
    """
    step = "archive handover"
    try:
        at(step)
        rotation_state.archive_handover(project_dir, marker["archive"], data)
        at("retire original")
        rotation_state.retire_original(project_dir, marker)
        archive = marker["archive"]
        context = injection_context(data.decode("utf-8", "replace"), archive, marker.get("outdated_reason"),
                                    marker["claimed_sha256"] == marker["sha256"])
        step = "log injection"
        at(step)
        rotation_state.log_injection(project_dir, _record(
            via, marker["session_id"], marker["generation"], successor, marker["handover"], archive, data,
            context["mode"]))
    except (OSError, rotation_state.LockTimeout) as error:
        rotation_state.log_failure(project_dir, "%s for %s" % (step, claimed), error)
        return None
    at("emit handover")
    emit(context["text"], archive)
    if rotation_state.log_delivery(project_dir, via, marker["session_id"], marker["generation"], successor):
        rotation_state.finish_claim(project_dir, claimed)
    return archive


def _resume_archived(project_dir: str, successor: str, entry: Dict[str, Any],
                     emit: Callable[[str, str], None]) -> Optional[str]:
    data = rotation_state.read_bytes(project_dir, entry["path"])
    if data is None:
        return "The archived handover %s cannot be read." % entry["path"]
    context = injection_context(data.decode("utf-8", "replace"), entry["path"])
    line = entry.get("injection") or {}
    try:
        rotation_state.log_injection(project_dir, _record(
            "resume", line.get("origin_session_id"), line.get("generation"), successor, None, entry["path"], data,
            context["mode"]))
    except (OSError, rotation_state.LockTimeout) as error:
        rotation_state.log_failure(project_dir, "log injection for %s" % entry["path"], error)
        return _not_handed_over(project_dir)
    emit(context["text"], entry["path"])
    return None


def _not_handed_over(project_dir: str) -> str:
    return "The handover could not be handed over (see %s). %s" % (rotation_state.errors_path(project_dir), RESUME_HINT)


def resume_pick(project_dir: str, successor: str, entry: Dict[str, Any],
                emit: Callable[[str, str], None]) -> Optional[str]:
    """Continue from one listed handover. None once it is emitted, else why it cannot be.

    The user picked it, so a ready or outdated handover is claimed as the file is
    now, also when it changed after its marker was written: refusing it would leave
    the user only the file itself. The context then says that it no longer holds
    the text that was checked (deliver), and the claim records the bytes it handed
    over (claimed_sha256). The claim takes only the generation the entry was found
    with: one the origin published at the same marker path since is refused.
    """
    if entry["kind"] == "archived":
        return _resume_archived(project_dir, successor, entry, emit)
    marker = entry["marker"]
    if entry["kind"] == "claimed":
        claimed = entry["marker_path"]
        data, problem = rotation_state.claimed_bytes(project_dir, marker)
        if data is None:
            return "The claim %s cannot be resumed: %s." % (claimed, problem)
    else:
        if rotation_state.read_bytes(project_dir, marker["handover"]) is None:
            return "The handover file %s is gone." % marker["handover"]
        try:
            taken = rotation_state.claim(project_dir, entry["marker_path"], successor,
                                         generation=marker["generation"])
        except rotation_state.GenerationChanged as changed:
            return "That handover was replaced before it could be claimed: %s. %s" % (changed, RESUME_HINT)
        if taken is None:
            return "That handover was taken by another session in the meantime."
        claimed, marker, data = taken
    if deliver(project_dir, claimed, marker, data, successor, "resume", emit) is None:
        return _not_handed_over(project_dir)
    return None


# --- what is listed ------------------------------------------------------

def entry_id(entry: Dict[str, Any]) -> str:
    """The id of a listed handover: <kind>-<session>-<generation> for a marker, its archive file name without ".md".

    An id names one generation in one state: a later handover of the same session,
    a claim taken meanwhile or a trim never gives another entry this id, so a
    number or id saved from a list picks what the user saw, or nothing.
    """
    if entry["kind"] == "archived":
        return os.path.basename(entry["path"])[:-len(".md")]
    return "%s-%s-%s" % (entry["kind"], entry["marker"]["session_id"], entry["marker"]["generation"])


def is_entry_id(word: Any) -> bool:
    return isinstance(word, str) and ENTRY_ID.fullmatch(word) is not None


def find_entry(entries: List[Dict[str, Any]], ident: str) -> Optional[Dict[str, Any]]:
    return next((entry for entry in entries if entry_id(entry) == ident), None)


def _origin(marker: Dict[str, Any], identity: Optional[Dict[str, Any]]) -> str:
    """Who wrote it, whose `claude` process that was, and that its paths passed the checks."""
    recorded = marker.get("process")
    if not recorded:
        process = "claude process not recorded"
    elif identity is None:
        process = "claude process of this session unknown"
    else:
        process = "this claude process" if recorded == identity else "another claude process"
    return "session %s (%s, paths checked)" % (marker.get("session_id"), process)


def describe(project_dir: str, entry: Dict[str, Any], identity: Optional[Dict[str, Any]] = None) -> str:
    """One line for a pending or archived handover; `identity` is this session's process."""
    return "%s [id %s]" % (_description(project_dir, entry, identity), entry_id(entry))


def _description(project_dir: str, entry: Dict[str, Any], identity: Optional[Dict[str, Any]]) -> str:
    kind = entry["kind"]
    if kind == "archived":
        record = entry.get("injection") or {}
        when = "continued in session %s at %s" % (record["session_id"], record["at"]) if record else (
            "no line in injections.jsonl")
        return "archived, %s: %s" % (when, entry["path"])
    marker = entry["marker"]
    if kind == "claimed":
        copy = marker["archive"]
        extra = " (archive copy: %s)" % copy if rotation_state.read_bytes(project_dir, copy) is not None else ""
        return "claim did not finish, taken at %s from %s: %s%s" % (
            marker["claimed_at"], _origin(marker, identity), marker["handover"], extra)
    reason = marker.get("outdated_reason", state_fields.REASON_NOT_RECORDED)
    state = "outdated (%s)" % reason if kind == "outdated" else "ready"
    return "%s, written at %s by %s: %s" % (
        state, marker["written_at"], _origin(marker, identity), marker["handover"])


def resume_entries(project_dir: str, session_id: str) -> List[Dict[str, Any]]:
    return rotation_state.pending_handovers(project_dir, session_id) + rotation_state.archived_handovers(project_dir)


def resume_list(project_dir: str, session_id: str, identity: Optional[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """(the numbered list the `resume` action shows the user (to the model), the id under each number).

    The groups: unfinished claims, ready, outdated, then archived handovers, each
    newest first. Markers that cannot be resumed (they failed the checks, or a
    claim whose bytes are no longer there) follow unnumbered, so no number can
    pick them; a file whose name the plugin does not write is not named.
    """
    pending, blocked = rotation_state.survey(project_dir, session_id)
    entries = pending + rotation_state.archived_handovers(project_dir)
    directory = os.path.abspath(rotation_state.rotation_dir(project_dir))
    lines = ["Handovers in %s, newest first within each group:" % directory] if entries else [
        "There are no handovers in %s to resume." % directory]
    lines += ["%d. %s" % (number, describe(project_dir, entry, identity)) for number, entry in enumerate(entries, 1)]
    lines += ["- not resumable, left on disk for the user to inspect: %s (%s)" % (
        rotation_state.shown_marker_path(path), why) for path, why in blocked]
    if entries:
        lines.append(
            "Show this list to the user. To continue from one, the user types /%s resume <number> or "
            "resume <id>; a number means the entry shown under it here. A handover the user no longer "
            "wants can be deleted; it then leaves this list." % SKILL_COMMAND
        )
    return "\n".join(lines), [entry_id(entry) for entry in entries]


def start_notice(project_dir: str, session_id: str, env: Dict[str, str]) -> Optional[str]:
    """The line a new session shows the user when handovers wait; None when none do."""
    pending = rotation_state.pending_handovers(project_dir, session_id)
    if not pending:
        return None
    identity = rotation_state.process_identity(env)
    lines = ["context-rotation: %d handover(s) were not continued automatically. %s" % (len(pending), RESUME_HINT)]
    lines += ["- " + describe(project_dir, entry, identity) for entry in pending[:LISTED_AT_START]]
    if len(pending) > LISTED_AT_START:
        lines.append("- and %d more" % (len(pending) - LISTED_AT_START))
    return "\n".join(lines)
