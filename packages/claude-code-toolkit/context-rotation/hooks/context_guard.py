"""Entry for UserPromptSubmit, UserPromptExpansion, PreToolUse and Stop.

- UserPromptSubmit: a prompt makes the session's ready-marker outdated; the warn
  band and above ask for a handover (M); an unmeasurable session is told once (U).
- UserPromptExpansion: when the user types /context-rotation:rotate, adds the
  session's handover path (write), the list of handovers (resume), or the chosen
  handover itself (resume <number>).
- PreToolUse: any call other than Read, Glob, Grep or a write of the handover
  makes the ready-marker outdated.
- Stop: re-checks the ready-marker against the transcript and its age, gives a
  valid handover written outside Write or Edit its marker, and tells the user
  once to type /clear (loop step 7, the manual path).

A hook that fires inside a subagent (agent_id set) does nothing, and so does one
whose project storage fails the storage gate (hook_runtime.open_project). Failures
go through hook_runtime: one stderr line, errors.log, exit 1, never 2.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import context_budget
import handover_contract
import handover_injection
import hook_runtime
import rotation_state
import transcript_work

UNKNOWN_NOTICE_AFTER = 3
UNKNOWN_NOTICE = (
    "context-rotation cannot measure this session's context, so it cannot warn "
    "before the context fills up. It will not repeat this notice."
)
MANUAL_COMPLETION = "Handover saved. Type /clear, then any message, to continue in a fresh context."


class Session:
    def __init__(self, project_dir, session_id, settings):
        self.project_dir = project_dir
        self.session_id = session_id
        self.settings = settings
        self.handover = rotation_state.handover_path(project_dir, session_id)

    def marker(self, kind):
        return rotation_state.marker_path(self.project_dir, kind, self.session_id)


def open_session(payload, env, run):
    """The session this hook acts for, or None when it must do nothing (hook_runtime.open_project)."""
    opened = hook_runtime.open_project(payload, env, run)
    return Session(*opened) if opened is not None else None


# --- outdated markers (loop step 6) --------------------------------------

def outdated_note(marker):
    return (
        "Note: the handover at %s no longer covers the latest work (%s). It will not be "
        "used for a clear until it is written again." % (marker["handover"], marker.get("outdated_reason"))
    )


def outdate(session, reason, run):
    """Make the ready-marker outdated, or pick up an untold one; the note for the model."""
    marker = None
    if rotation_state.stored_exists(session.project_dir, session.marker("ready")):
        run.at("mark outdated")
        marker = rotation_state.mark_outdated(session.project_dir, session.session_id, reason, notified=True)
    if marker is None:
        run.at("outdated notice")
        marker = rotation_state.take_outdated_notice(session.project_dir, session.session_id)
    return outdated_note(marker) if marker else None


# --- UserPromptSubmit ----------------------------------------------------

def warn_message(size, settings, project_dir, session_id):
    window = settings["window_tokens"]
    percent = size * 100 / window
    return (
        "Context is %d of %d tokens (%.0f%%). Wrap up the current work and prepare a "
        "handover: write the state of this session (goal, decisions, what is done, "
        "what is next) to %s so a fresh session can continue from it."
        % (size, window, percent, rotation_state.handover_path(project_dir, session_id))
    )


def handle_known(size, session, state, run):
    if state.get("unknown_count"):
        state["unknown_count"] = 0
        run.at("save state")
        rotation_state.save_state(session.project_dir, session.session_id, state)
    if context_budget.band(size, session.settings) in ("warn", "force", "hard"):
        # The handover path is named in the instruction below: protect it first.
        run.at("ignore protection")
        rotation_state.ensure_gitignore(session.project_dir)
        return warn_message(size, session.settings, session.project_dir, session.session_id)
    return None


def handle_unknown(session, state, run):
    state["unknown_count"] = int(state.get("unknown_count") or 0) + 1
    due = state["unknown_count"] >= UNKNOWN_NOTICE_AFTER and not state.get("notice_sent")
    if due:
        state["notice_sent"] = True
        state["notice_sent_at"] = rotation_state.utc_now()
        state["notice_sent_band"] = "unknown"
        state["notice_sent_size"] = None
        state["notice_sent_unknown_count"] = state["unknown_count"]
    run.at("save state")
    rotation_state.save_state(session.project_dir, session.session_id, state)
    return UNKNOWN_NOTICE if due else None


def on_prompt(payload, env, run, session):
    notes = []
    if rotation_state.prompt_outdates(payload.get("prompt")):
        notes.append(outdate(session, "a prompt after it", run))
    run.at("measure")
    size = context_budget.measure(payload.get("transcript_path"))
    run.at("load state")
    state = rotation_state.load_state(session.project_dir, session.session_id)
    notice = None
    if size is None:
        notice = handle_unknown(session, state, run)
    else:
        notes.append(handle_known(size, session, state, run))
    text = "\n\n".join(n for n in notes if n)
    document = hook_runtime.context_output("UserPromptSubmit", text, notice)
    if document:
        hook_runtime.emit(document)


# --- UserPromptExpansion -------------------------------------------------

def write_instruction(session):
    return (
        "Write this session's handover to %s with the Write tool, as the rotate skill "
        'describes: "## Goal", "## Current state", "## Running", "## Next steps" (numbered, '
        'starting at "1."), "## Files touched", "## Open risks", at most %d characters. '
        "Make that write your last action." % (session.handover, session.settings["handover_max_chars"])
    )


def expansion_says(text, user=None):
    """Context for the model and a line for the user; nothing at all when both are empty."""
    hook_runtime.emit(hook_runtime.context_output("UserPromptExpansion", text, user))


def continued(text, archive):
    expansion_says(text, "context-rotation: continued from %s." % archive)


def resume(session, words, env, run):
    """The resume action: the numbered list, or the chosen handover itself."""
    run.at("list handovers")
    if not words:
        identity = rotation_state.process_identity(env)
        expansion_says(handover_injection.resume_list(session.project_dir, session.session_id, identity))
        return
    if not (words[0].isascii() and words[0].isdigit()):
        expansion_says("Type /%s resume <number> with a number from the list." % handover_injection.SKILL_COMMAND)
        return
    entries = handover_injection.resume_entries(session.project_dir, session.session_id)
    number = int(words[0])
    if not 1 <= number <= len(entries):
        expansion_says("There is no handover number %d. %s" % (number, handover_injection.RESUME_HINT))
        return
    run.at("resume handover")
    problem = handover_injection.resume_pick(
        session.project_dir, session.session_id, entries[number - 1], continued)
    if problem:
        expansion_says(problem)


def on_expansion(payload, env, run, session):
    if payload.get("command_source") != "plugin" or payload.get("command_name") != handover_injection.SKILL_COMMAND:
        return
    args = payload.get("command_args")
    words = args.split() if isinstance(args, str) else []
    action = words[0].lower() if words else "write"
    if action == "write":
        run.at("ignore protection")
        rotation_state.ensure_gitignore(session.project_dir)
        expansion_says(write_instruction(session))
    elif action == "resume":
        resume(session, words[1:], env, run)


# --- PreToolUse ----------------------------------------------------------

def on_tool(payload, env, run, session):
    tool_name, tool_input = payload.get("tool_name"), payload.get("tool_input")
    if transcript_work.is_handover_write(tool_name, tool_input, session.handover):
        # The handover is about to be written: protect it before it exists.
        run.at("ignore protection")
        rotation_state.ensure_gitignore(session.project_dir)
        return
    if not rotation_state.tool_call_outdates(tool_name, tool_input, session.handover):
        return
    note = outdate(session, "a %s call after it" % tool_name, run)
    if note:
        hook_runtime.emit(hook_runtime.context_output("PreToolUse", note))


# --- Stop ----------------------------------------------------------------

def announce(session, sha, run):
    run.at("announce")
    if rotation_state.mark_announced(session.project_dir, session.session_id, sha):
        hook_runtime.emit({"systemMessage": MANUAL_COMPLETION})


def adopt(payload, env, run, session, data):
    """Stop fallback: a valid handover written outside Write or Edit still gets a marker."""
    outdated = rotation_state.load_marker(session.project_dir, session.marker("outdated"))
    if outdated is not None and outdated["sha256"] == rotation_state.sha256_hex(data):
        return
    if handover_contract.problems(data, session.settings["handover_max_chars"]):
        return
    written = rotation_state.stored_mtime(session.project_dir, session.handover)
    if written is None:
        return
    run.at("check transcript")
    if transcript_work.work_since(payload.get("transcript_path"), written, session.handover):
        return
    run.at("write ready marker")
    marker = rotation_state.new_marker(
        session.session_id, session.handover, data, env, payload.get("transcript_path"), None)
    rotation_state.mark_ready(session.project_dir, marker)
    announce(session, marker["sha256"], run)


def on_stop(payload, env, run, session):
    run.at("read handover")
    data = rotation_state.read_bytes(session.project_dir, session.handover)
    ready = rotation_state.load_marker(session.project_dir, session.marker("ready"))
    if ready is not None:
        run.at("check ready marker")
        reason = rotation_state.outdated_reason(ready, session.settings, data)
        if reason is None:
            announce(session, ready["sha256"], run)
            return
        run.at("mark outdated")
        rotation_state.mark_outdated(session.project_dir, session.session_id, reason, notified=False)
    if data is not None:
        adopt(payload, env, run, session, data)


HANDLERS = {
    "UserPromptSubmit": on_prompt,
    "UserPromptExpansion": on_expansion,
    "PreToolUse": on_tool,
    "Stop": on_stop,
}


def run(payload, env, run_state):
    handler = HANDLERS.get(payload.get("hook_event_name"))
    if handler is None:
        return
    session = open_session(payload, env, run_state)
    if session is not None:
        handler(payload, env, run_state, session)


if __name__ == "__main__":
    hook_runtime.main(run)
