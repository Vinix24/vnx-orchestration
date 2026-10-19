"""Entry for UserPromptSubmit, UserPromptExpansion, PreToolUse and Stop.

- UserPromptSubmit: a prompt makes the session's ready-marker outdated; the warn
  band asks for a handover (M), the force and hard bands ask for it now (M) and
  tell the user once per crossing (U, band_guard); an unmeasurable session is
  told once (U); an automatic /clear that stopped in this pane is repeated once to
  the user of its `claude` process (U, auto_clear).
- UserPromptExpansion: when the user types /context-rotation:rotate, adds the
  session's handover path (write), records the release of the force and hard
  bands for the session (release), adds the list of handovers (resume), or the
  chosen handover itself (resume <id>, or resume <number> for the entry shown
  under that number in this session's last list); any other action gets the
  usage line.
- PreToolUse: a write into `.claude/rotation` other than of the handover, or into
  either settings file, is denied in every band, also inside a subagent
  (band_guard.storage_denial).
  Any call other than Read, Glob, Grep or a write of the handover
  makes the ready-marker outdated; the force and hard bands may deny the call
  (band_guard), never a write of the handover.
- Stop: re-checks the ready-marker against the transcript and its age, gives a
  valid handover written outside Write or Edit its marker, and tells the user
  once per marker (loop step 7): to type /clear (the manual path), or, inside
  tmux with auto_clear on, that /clear is typed for them by a detached job, or
  why not (auto_clear). Without a current handover, the force and hard bands
  hold the Stop back once per crossing (band_guard).

A hook that fires inside a subagent (agent_id set) does nothing but deny a write
into `.claude/rotation` or either settings file (SUBAGENT_HANDLERS), and one whose project storage fails
the storage gate does nothing at all (hook_runtime.open_project). Failures
go through hook_runtime: one stderr line, errors.log, exit 1, never 2. A denial and
a held-back Stop are JSON with exit 0, decided last, and a failure of the
enforcement state never leads to either (band_guard).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import auto_clear
import band_guard
import context_budget
import handover_contract
import handover_injection
import hook_runtime
import rotation_state
import state_fields
import transcript_work

UNKNOWN_NOTICE_AFTER = 3
UNKNOWN_NOTICE = (
    "context-rotation cannot measure this session's context, so it cannot warn "
    "before the context fills up. It will not repeat this notice."
)
# The ids of the last list `resume` showed in this session, so a number picks what the user saw.
LISTING = "resume_listing"
MAX_LISTED = 1000
MAX_NUMBER_DIGITS = 6


# --- outdated markers (loop step 6) --------------------------------------

def outdated_note(marker):
    return (
        "Note: the handover at %s no longer covers the latest work (%s). It will not be "
        "used for a clear until it is written again."
        % (marker["handover"], marker.get("outdated_reason", state_fields.REASON_NOT_RECORDED))
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
    threshold = context_budget.threshold_for(settings, "warn")
    return (
        "Context is at %s tokens, at or above the warn threshold of %s tokens. Wrap up the current work and prepare a "
        "handover: write the state of this session (goal, decisions, what is done, "
        "what is next) to %s so a fresh session can continue from it."
        % (context_budget.token_text(size), context_budget.token_text(threshold),
           rotation_state.handover_path(project_dir, session_id))
    )


def handle_known(payload, size, session, state, run):
    """(M, U) for a measured size: the warn message, or the force and hard bands' (band_guard)."""
    if state.get("unknown_count"):
        state["unknown_count"] = 0
        run.at("save state")
        rotation_state.save_state(session.project_dir, session.session_id, state)
    band = context_budget.band(size, session.settings)
    if band in ("warn", "force", "hard"):
        # The handover path is named in the instruction below: protect it first.
        run.at("ignore protection")
        rotation_state.ensure_gitignore(session.project_dir)
    text, line = band_guard.on_prompt(payload, session, size, run)
    if band == "warn":
        text = warn_message(size, session.settings, session.project_dir, session.session_id)
    return text, line


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
        notes.append(outdate(session, state_fields.PROMPT_AFTER, run))
    repeat = auto_clear.prompt_notice(payload, env, run, session)
    run.at("measure")
    size = context_budget.measure(payload.get("transcript_path"))
    run.at("load state")
    state = rotation_state.load_state(session.project_dir, session.session_id)
    notice = None
    if size is None:
        notice = handle_unknown(session, state, run)
    else:
        text, notice = handle_known(payload, size, session, state, run)
        notes.append(text)
    text = "\n\n".join(n for n in notes if n)
    user = "\n".join(line for line in (repeat, notice) if line)
    document = hook_runtime.context_output("UserPromptSubmit", text, user)
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


def stored_listing(session):
    """The ids of the last list shown in this session; None when none was shown (or the state holds no such list)."""
    listing = rotation_state.load_state(session.project_dir, session.session_id).get(LISTING)
    if isinstance(listing, list) and len(listing) <= MAX_LISTED and all(map(handover_injection.is_entry_id, listing)):
        return listing
    return None


def show_list(session, env, run, lead=None):
    """Show the current list, and remember its ids for a later `resume <number>`."""
    identity = rotation_state.process_identity(env)
    text, ids = handover_injection.resume_list(session.project_dir, session.session_id, identity)
    state = rotation_state.load_state(session.project_dir, session.session_id)
    if ids or LISTING in state:  # a project without handovers gets no state file for an empty list
        state[LISTING] = ids
        run.at("save state")
        rotation_state.save_state(session.project_dir, session.session_id, state)
    expansion_says(lead + "\n\n" + text if lead else text)


def picked(session, word):
    """(entry id, None) for a pick, (None, lead) when it is refused with the current list, (None, None) for neither."""
    if word.isascii() and word.isdigit():
        listing = stored_listing(session)
        number = int(word) if len(word) <= MAX_NUMBER_DIGITS else 0
        if listing and 1 <= number <= len(listing):
            return listing[number - 1], None
        shown = "No list was shown in this session yet; this is the current one:" if listing is None else (
            "This is the current list:")
        return None, "There is no handover with that number. " + shown
    if handover_injection.is_entry_id(word):
        return word, None
    return None, None


def resume(session, words, env, run):
    """The resume action: the numbered list, or the chosen handover itself."""
    run.at("list handovers")
    if not words:
        show_list(session, env, run)
        return
    ident, lead = picked(session, words[0])
    if lead:
        show_list(session, env, run, lead)
        return
    if ident is None:
        expansion_says(handover_injection.PICK_HINT)
        return
    entry = handover_injection.find_entry(
        handover_injection.resume_entries(session.project_dir, session.session_id), ident)
    if entry is None:
        show_list(session, env, run, "Handover %s is no longer there. This is the current list:" % ident)
        return
    run.at("resume handover")
    problem = handover_injection.resume_pick(session.project_dir, session.session_id, entry, continued)
    if problem:
        expansion_says(problem)


def unknown_action():
    expansion_says(
        "The user typed /%s with an action it does not have. Do nothing else; show them this line: %s"
        % (handover_injection.SKILL_COMMAND, handover_injection.USAGE),
        "context-rotation: " + handover_injection.USAGE)


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
    elif action == "release":
        expansion_says(*band_guard.on_release(session, run))
    elif action == "resume":
        resume(session, words[1:], env, run)
    else:
        unknown_action()


# --- PreToolUse ----------------------------------------------------------

def on_tool(payload, env, run, session):
    tool_name, tool_input = payload.get("tool_name"), payload.get("tool_input")
    if transcript_work.is_handover_write(tool_name, tool_input, session.handover):
        # The handover is about to be written: protect it before it exists.
        run.at("ignore protection")
        rotation_state.ensure_gitignore(session.project_dir)
    note = None
    if rotation_state.tool_call_outdates(tool_name, tool_input, session.handover):
        note = outdate(session, state_fields.TOOL_CALL_AFTER, run)
    # Decided last, so nothing after it can fail the hook; a write of the handover goes ahead
    # unless a symlink at its path would take it elsewhere (band_guard.writes_handover).
    denial = band_guard.on_tool(payload, session, run)
    if denial:
        hook_runtime.emit(hook_runtime.deny_output(denial[0], note, denial[1]))
    elif note:
        hook_runtime.emit(hook_runtime.context_output("PreToolUse", note))


# --- Stop ----------------------------------------------------------------

def announce(payload, env, run, session, marker):
    """The user's line for a current handover, once per marker: the manual path or the automatic one (auto_clear)."""
    run.at("announce")
    if rotation_state.mark_announced(session.project_dir, session.session_id, marker["sha256"]):
        hook_runtime.emit({"systemMessage": auto_clear.at_stop(payload, session, marker, env, run)})


def adopt(payload, env, run, session, data, cursor):
    """Stop fallback: a valid handover written outside Write or Edit still gets a marker, which it returns.

    `cursor` is the transcript position taken before `data` was read: work after
    it is judged later, work stamped after the file's write is judged here.
    """
    outdated = rotation_state.load_marker(session.project_dir, session.marker("outdated"))
    if outdated is not None and outdated["sha256"] == rotation_state.sha256_hex(data):
        return None
    if handover_contract.problems(data, session.settings["handover_max_chars"]):
        return None
    written = rotation_state.stored_mtime(session.project_dir, session.handover)
    if written is None:
        return None
    run.at("check transcript")
    if transcript_work.work_since(payload.get("transcript_path"), written, session.handover):
        return None
    run.at("write ready marker")
    marker = rotation_state.new_marker(session.session_id, session.handover, data, env, cursor, None)
    rotation_state.mark_ready(session.project_dir, marker)
    return marker


def current_handover(payload, env, run, session):
    """The session's ready-marker when its handover is current, after the Stop fallback; None otherwise."""
    run.at("read handover")
    cursor = None
    if rotation_state.stored_exists(session.project_dir, session.handover):
        cursor = transcript_work.position(payload.get("transcript_path"))  # before the read, for adopt
    data = rotation_state.read_bytes(session.project_dir, session.handover)
    ready = rotation_state.load_marker(session.project_dir, session.marker("ready"))
    if ready is not None:
        run.at("check ready marker")
        reason = rotation_state.outdated_reason(ready, session.settings, data)
        if reason is None:
            return ready
        run.at("mark outdated")
        rotation_state.mark_outdated(session.project_dir, session.session_id, reason, notified=False)
    return adopt(payload, env, run, session, data, cursor) if data is not None else None


def on_stop(payload, env, run, session):
    marker = current_handover(payload, env, run, session)
    if marker is not None:
        announce(payload, env, run, session, marker)
        return
    block = band_guard.on_stop(payload, session, run)
    if block:
        hook_runtime.emit(hook_runtime.block_output(*block))


HANDLERS = {
    "UserPromptSubmit": on_prompt,
    "UserPromptExpansion": on_expansion,
    "PreToolUse": on_tool,
    "Stop": on_stop,
}


def on_subagent_tool(payload, env, run, session):
    """PreToolUse inside a subagent: only a write into `.claude/rotation` or a settings file is denied; no band, marker or state."""
    denial = band_guard.storage_denial(payload.get("tool_name"), payload.get("tool_input"), session, run)
    if denial:
        hook_runtime.emit(hook_runtime.deny_output(denial[0]))


SUBAGENT_HANDLERS = {"PreToolUse": on_subagent_tool}


def run(payload, env, run_state):
    handlers = SUBAGENT_HANDLERS if payload.get("agent_id") else HANDLERS
    handler = handlers.get(payload.get("hook_event_name"))
    if handler is None:
        return
    session = hook_runtime.open_session(payload, env, run_state, subagents=True)
    if session is not None:
        handler(payload, env, run_state, session)


if __name__ == "__main__":
    hook_runtime.main(run)
