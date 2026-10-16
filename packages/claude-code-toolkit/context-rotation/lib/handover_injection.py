"""Handing a handover to a session: the order that makes a crash recoverable.

claim (atomic rename) -> copy to the archive -> one line in injections.jsonl ->
emit -> remove the original, then the claimed marker last. A crash at any point
leaves the claimed marker with the original or the archive copy, and `resume`
lists both. Automatic injection happens at most once; recovery is manual.
Standard library only.
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
            via: str, emit: Callable[[str, str], None], at: Callable[[str], None] = _no_step) -> str:
    """Archive, log, emit and clean up a claimed handover. Returns the archive path."""
    at("archive handover")
    target = marker.get("archive") or rotation_state.plan_archive(project_dir, marker["session_id"])
    archive = rotation_state.archive_handover(target, data)
    context = injection_context(data.decode("utf-8", "replace"), archive, marker.get("outdated_reason"))
    at("log injection")
    rotation_state.log_injection(project_dir, _record(
        via, marker["session_id"], successor, marker["handover"], archive, data, context["mode"]))
    at("emit handover")
    emit(context["text"], archive)
    rotation_state.finish_claim(project_dir, claimed, marker["handover"])
    return archive


def _resume_archived(project_dir: str, successor: str, entry: Dict[str, Any],
                     emit: Callable[[str, str], None]) -> Optional[str]:
    data = rotation_state.read_bytes(entry["path"])
    if data is None:
        return "The archived handover %s cannot be read." % entry["path"]
    context = injection_context(data.decode("utf-8", "replace"), entry["path"])
    origin = (entry.get("injection") or {}).get("origin_session_id")
    rotation_state.log_injection(project_dir, _record(
        "resume", origin, successor, None, entry["path"], data, context["mode"]))
    emit(context["text"], entry["path"])
    return None


def resume_pick(project_dir: str, successor: str, entry: Dict[str, Any],
                emit: Callable[[str, str], None]) -> Optional[str]:
    """Continue from one listed handover. None once it is emitted, else why it cannot be."""
    if entry["kind"] == "archived":
        return _resume_archived(project_dir, successor, entry, emit)
    marker = entry["marker"]
    if entry["kind"] == "claimed":
        claimed = entry["marker_path"]
        data = rotation_state.read_bytes(marker["handover"]) or rotation_state.read_bytes(marker.get("archive"))
        if data is None:
            return "Neither %s nor its archive copy can be read." % marker["handover"]
    else:
        data = rotation_state.read_bytes(marker["handover"])
        if data is None:
            return "The handover file %s is gone." % marker["handover"]
        taken = rotation_state.claim(project_dir, entry["marker_path"], successor)
        if taken is None:
            return "That handover was taken by another session in the meantime."
        claimed, marker = taken
    deliver(project_dir, claimed, marker, data, successor, "resume", emit)
    return None


# --- what is listed ------------------------------------------------------

def _origin(marker: Dict[str, Any]) -> str:
    return "session %s" % marker.get("session_id")


def describe(entry: Dict[str, Any]) -> str:
    """One line for a pending or archived handover."""
    kind = entry["kind"]
    if kind == "archived":
        record = entry.get("injection") or {}
        when = "continued in session %s at %s" % (record.get("session_id"), record.get("at")) if record else (
            "no line in injections.jsonl")
        return "archived, %s: %s" % (when, entry["path"])
    marker = entry["marker"]
    if kind == "claimed":
        copy = marker.get("archive")
        extra = " (archive copy: %s)" % copy if copy and os.path.exists(copy) else ""
        return "claim did not finish, taken at %s from %s: %s%s" % (
            marker.get("claimed_at"), _origin(marker), marker["handover"], extra)
    state = "outdated (%s)" % marker.get("outdated_reason", "reason not recorded") if kind == "outdated" else "ready"
    return "%s, written at %s by %s: %s" % (state, marker.get("written_at"), _origin(marker), marker["handover"])


def resume_entries(project_dir: str, session_id: str) -> List[Dict[str, Any]]:
    return rotation_state.pending_handovers(project_dir, session_id) + rotation_state.archived_handovers(project_dir)


def resume_list(project_dir: str, session_id: str) -> str:
    """The numbered list the `resume` action shows the user (to the model)."""
    entries = resume_entries(project_dir, session_id)
    if not entries:
        return "There are no handovers in %s to resume." % rotation_state.rotation_dir(project_dir)
    lines = ["Handovers in %s, newest first within each group:" % os.path.abspath(rotation_state.rotation_dir(project_dir))]
    lines += ["%d. %s" % (number, describe(entry)) for number, entry in enumerate(entries, 1)]
    lines.append(
        "Show this list to the user. To continue from one, the user types /%s resume <number>. "
        "A handover the user no longer wants can be deleted; it then leaves this list." % SKILL_COMMAND
    )
    return "\n".join(lines)


def start_notice(project_dir: str, session_id: str) -> Optional[str]:
    """The line a new session shows the user when handovers wait; None when none do."""
    pending = rotation_state.pending_handovers(project_dir, session_id)
    if not pending:
        return None
    lines = ["context-rotation: %d handover(s) were not continued automatically. %s" % (len(pending), RESUME_HINT)]
    lines += ["- " + describe(entry) for entry in pending[:LISTED_AT_START]]
    if len(pending) > LISTED_AT_START:
        lines.append("- and %d more" % (len(pending) - LISTED_AT_START))
    return "\n".join(lines)
