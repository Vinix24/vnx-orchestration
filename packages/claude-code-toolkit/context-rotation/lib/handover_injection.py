"""Handing a handover to a session: the order that makes a crash recoverable.

claim (atomic rename) -> copy to the archive -> one line in injections.jsonl ->
emit -> remove the original, then the claimed marker last. A crash at any point
leaves the claimed marker with the original or the archive copy, and `resume`
lists it. Automatic injection happens at most once; recovery is manual, and
reads the archive copy first: the original is used only while there is no copy
yet and it still holds the claimed bytes. The original is removed only while it
holds them, so a newer handover written meanwhile stays. Every marker here passed
rotation_state.marker_problem, and every file is read through the verified
storage directories. A storage failure before the emit (archive copy, log line)
is logged and nothing is emitted, so the claim stays for `resume`; after the emit
nothing raises. Standard library only.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

import rotation_state

INLINE_LIMIT = 9000
SKILL_COMMAND = "context-rotation:rotate"
RESUME_HINT = "Type /%s resume to see them and pick one." % SKILL_COMMAND
LISTED_AT_START = 5


def injection_context(text: str, archive: str, outdated_reason: Optional[str] = None) -> Dict[str, str]:
    """The additionalContext for a handover: inline up to INLINE_LIMIT, else a pointer."""
    warning = ""
    if outdated_reason:
        warning = (
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


def _record(via: str, origin: Optional[str], successor: str, handover: Optional[str], archive: str,
            data: bytes, mode: str) -> Dict[str, Any]:
    return {
        "at": rotation_state.utc_now(),
        "via": via,
        "origin_session_id": origin,
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

    `data` holds the claimed bytes (sha256 equal to the marker's claimed_sha256).
    None when the archive copy or the log line could not be written (OSError,
    LockTimeout): the failure goes to rotation_state.log_failure, nothing is
    emitted, and the claimed marker stays for `resume`.
    """
    step = "archive handover"
    try:
        at(step)
        archive = rotation_state.archive_handover(project_dir, marker["archive"], data)
        context = injection_context(data.decode("utf-8", "replace"), archive, marker.get("outdated_reason"))
        step = "log injection"
        at(step)
        rotation_state.log_injection(project_dir, _record(
            via, marker["session_id"], successor, marker["handover"], archive, data, context["mode"]))
    except (OSError, rotation_state.LockTimeout) as error:
        rotation_state.log_failure(project_dir, "%s for %s" % (step, claimed), error)
        return None
    at("emit handover")
    emit(context["text"], archive)
    rotation_state.finish_claim(project_dir, claimed, marker)
    return archive


def _resume_archived(project_dir: str, successor: str, entry: Dict[str, Any],
                     emit: Callable[[str, str], None]) -> Optional[str]:
    data = rotation_state.read_bytes(project_dir, entry["path"])
    if data is None:
        return "The archived handover %s cannot be read." % entry["path"]
    context = injection_context(data.decode("utf-8", "replace"), entry["path"])
    origin = (entry.get("injection") or {}).get("origin_session_id")
    origin = origin if rotation_state.is_safe_session_id(origin) else None
    try:
        rotation_state.log_injection(project_dir, _record(
            "resume", origin, successor, None, entry["path"], data, context["mode"]))
    except (OSError, rotation_state.LockTimeout) as error:
        rotation_state.log_failure(project_dir, "log injection for %s" % entry["path"], error)
        return _not_handed_over(project_dir)
    emit(context["text"], entry["path"])
    return None


def _not_handed_over(project_dir: str) -> str:
    return "The handover could not be handed over (see %s). %s" % (rotation_state.errors_path(project_dir), RESUME_HINT)


def resume_pick(project_dir: str, successor: str, entry: Dict[str, Any],
                emit: Callable[[str, str], None]) -> Optional[str]:
    """Continue from one listed handover. None once it is emitted, else why it cannot be."""
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
        taken = rotation_state.claim(project_dir, entry["marker_path"], successor)
        if taken is None:
            return "That handover was taken by another session in the meantime."
        claimed, marker, data = taken
    if deliver(project_dir, claimed, marker, data, successor, "resume", emit) is None:
        return _not_handed_over(project_dir)
    return None


# --- what is listed ------------------------------------------------------

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
    kind = entry["kind"]
    if kind == "archived":
        record = entry.get("injection") or {}
        when = "continued in session %s at %s" % (record.get("session_id"), record.get("at")) if record else (
            "no line in injections.jsonl")
        return "archived, %s: %s" % (when, entry["path"])
    marker = entry["marker"]
    if kind == "claimed":
        copy = marker["archive"]
        extra = " (archive copy: %s)" % copy if rotation_state.read_bytes(project_dir, copy) is not None else ""
        return "claim did not finish, taken at %s from %s: %s%s" % (
            marker.get("claimed_at"), _origin(marker, identity), marker["handover"], extra)
    state = "outdated (%s)" % marker.get("outdated_reason", "reason not recorded") if kind == "outdated" else "ready"
    return "%s, written at %s by %s: %s" % (
        state, marker.get("written_at"), _origin(marker, identity), marker["handover"])


def resume_entries(project_dir: str, session_id: str) -> List[Dict[str, Any]]:
    return rotation_state.pending_handovers(project_dir, session_id) + rotation_state.archived_handovers(project_dir)


def resume_list(project_dir: str, session_id: str, identity: Optional[Dict[str, Any]]) -> str:
    """The numbered list the `resume` action shows the user (to the model).

    Markers that cannot be resumed (they failed the path checks, or a claim whose
    bytes are no longer there) follow unnumbered, so no number can pick them.
    """
    pending, blocked = rotation_state.survey(project_dir, session_id)
    entries = pending + rotation_state.archived_handovers(project_dir)
    directory = os.path.abspath(rotation_state.rotation_dir(project_dir))
    lines = ["Handovers in %s, newest first within each group:" % directory] if entries else [
        "There are no handovers in %s to resume." % directory]
    lines += ["%d. %s" % (number, describe(project_dir, entry, identity)) for number, entry in enumerate(entries, 1)]
    lines += ["- not resumable, left on disk for the user to inspect: %s (%s)" % (path, why) for path, why in blocked]
    if entries:
        lines.append(
            "Show this list to the user. To continue from one, the user types /%s resume <number>. "
            "A handover the user no longer wants can be deleted; it then leaves this list." % SKILL_COMMAND
        )
    return "\n".join(lines)


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
