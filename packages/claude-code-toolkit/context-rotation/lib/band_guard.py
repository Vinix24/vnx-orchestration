"""What the force and hard bands do in each hook (loop step 3), and how a session gets out.

  level  UserPromptSubmit           PreToolUse                              Stop
  force  M: write the handover now  nothing, or a denial of a tool that     held back once per
                                    force_deny_tools names                  crossing (M)
  hard   as force                   denies all but HARD_ALLOWED and a       as force
                                    Write or Edit of the handover (never
                                    through a symlink at its path)

M goes to the model (additionalContext, permissionDecisionReason, the Stop
reason), U to the user (systemMessage). Each crossing gets one user line, at the
first UserPromptSubmit, denial or held-back Stop in it. Every message names the
session's handover path as the plugin builds it; its other values are the
measured size, the window from the settings, counts from a checked state and
fixed names.

A session only informs, so nothing is denied or held back: in plan mode
(`permission_mode: plan`); after the user typed `/context-rotation:rotate
release`, which the UserPromptExpansion hook records and which only a typed
command fires; and after hard_max_denials denials without a handover write,
which the user is told once. No Stop is held back while stop_hook_active is set.
An unknown size never acts, nor does a hook inside a subagent
(hook_runtime.open_project). A failure of the enforcement state (OSError,
LockTimeout, ValueError) is logged (errors.log, else stderr) and the call goes
ahead as in a session that only informs: a failure never denies or holds back.
Standard library only.
"""
from __future__ import annotations

import fnmatch
from typing import Any, Dict, Optional, Tuple

import context_budget
import enforcement_state
import hook_runtime
import rotation_state
import transcript_work

LOW, FORCE, HARD = enforcement_state.LOW, enforcement_state.FORCE, enforcement_state.HARD
HARD_ALLOWED = ("Read", "Glob", "Grep", "Skill", "AskUserQuestion", "TodoWrite")
PLAN = "plan"
FAILURES = (OSError, rotation_state.LockTimeout, ValueError)
RELEASE_COMMAND = "/context-rotation:rotate release"
ALLOWED_AT_HARD = "Read, Glob, Grep, Skill, AskUserQuestion, TodoWrite and a Write or Edit of %s"
INFORM_ONLY = {
    PLAN: "plan mode is on",
    enforcement_state.RELEASE: "the user released the guard for this session",
    enforcement_state.DENIAL_CAP: (
        "the guard already denied as many tool calls without a handover write as hard_max_denials allows"),
}
RELEASED = (
    "The user typed %s: the context-rotation guard is released for this session, so from now on no tool is "
    "denied and no stop is held back. Do nothing else for this command; tell the user in one line that the "
    "guard is released. A handover at %s, then /clear, is still the way to continue in a fresh context."
)
RELEASED_LINE = (
    "context-rotation: released for this session. Nothing is denied or held back from now on; the notices about "
    "the context size continue."
)
RELEASE_FAILED = (
    "The user typed %s, but the plugin could not record the release. Do nothing else for this command; tell the "
    "user in one line that it was not recorded and that .claude/rotation/errors.log says why."
)
RELEASE_FAILED_LINE = "context-rotation: the release could not be recorded; .claude/rotation/errors.log says why."

Reply = Optional[Tuple[str, Optional[str]]]


# --- what each level holds back ----------------------------------------------------

def writes_handover(tool_name: Any, tool_input: Any, session: hook_runtime.Session) -> bool:
    """A Write or Edit of the session's handover, while that path holds a regular file or nothing yet.

    transcript_work.is_handover_write compares real paths, so with a symlink at the
    handover path a write of the file it points to would match too, and so would the
    write of the handover path, which lands in that file. Neither is a handover write.
    """
    if not transcript_work.is_handover_write(tool_name, tool_input, session.handover):
        return False
    project_dir, handover = session.project_dir, session.handover
    return rotation_state.stored_regular(project_dir, handover) or not rotation_state.stored_exists(project_dir, handover)


def hard_allows(tool_name: Any, tool_input: Any, session: hook_runtime.Session) -> bool:
    """The calls the hard band lets through: reading, asking, the skill, todos, and writing the handover."""
    return tool_name in HARD_ALLOWED or writes_handover(tool_name, tool_input, session)


def force_denies(tool_name: Any, settings: Dict[str, Any]) -> bool:
    """A tool the project's force_deny_tools names, as a tool name or an fnmatch pattern of one."""
    if not isinstance(tool_name, str):
        return False
    return any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in settings["force_deny_tools"])


def denies(level: str, tool_name: Any, settings: Dict[str, Any]) -> bool:
    """The level holds back a call the hard band does not allow (on_tool lets those through first)."""
    return level == HARD or (level == FORCE and force_denies(tool_name, settings))


def inform_reason(state: Dict[str, Any], payload: Dict[str, Any]) -> Optional[str]:
    """Why this call only informs (a key of INFORM_ONLY); None while the bands are enforced."""
    if payload.get("permission_mode") == PLAN:
        return PLAN
    return state["inform_only"]


# --- the messages ---------------------------------------------------------------------

def size_text(size: int, settings: Dict[str, Any]) -> str:
    window = settings["window_tokens"]
    return "%d of %d tokens (%.0f%%)" % (size, window, size * 100 / window)


def handover_now(size: int, level: str, session: hook_runtime.Session, why: Optional[str]) -> str:
    """M at UserPromptSubmit in the force and hard bands."""
    text = ("Context is %s, in the %s band. Write the handover at %s now with the rotate skill "
            "(/context-rotation:rotate), and make that write your last action."
            % (size_text(size, session.settings), level, session.handover))
    if why == PLAN:
        return text + " Plan mode is on, so nothing is denied; write it as soon as plan mode ends."
    if why:
        return text + " Nothing is denied or held back in this session: %s." % INFORM_ONLY[why]
    if level == HARD:
        return text + " Until it is written, every tool except %s is denied." % (ALLOWED_AT_HARD % session.handover)
    return text


def crossing_line(size: int, level: str, session: hook_runtime.Session, why: Optional[str]) -> str:
    """U, once per crossing."""
    head = "context-rotation: the context is at %.0f%% of the window (%s band); the handover goes to %s." % (
        size * 100 / session.settings["window_tokens"], level, session.handover)
    if why:
        return head + " Nothing is denied or held back: %s." % INFORM_ONLY[why]
    if level == HARD:
        return head + (" Until the model writes it, it may only read and write that handover. "
                       "Type %s to lift this for this session." % RELEASE_COMMAND)
    return head + " The model is asked to write it now; then type /clear to continue in a fresh context."


def cap_line(state: Dict[str, Any]) -> str:
    """U, once: the denial that reached hard_max_denials."""
    return ("context-rotation: %d tool calls were denied in this session without a handover write, so the guard "
            "only informs from now on: nothing is denied or held back for the rest of this session."
            % state["denials"])


def deny_reason(size: int, level: str, session: hook_runtime.Session) -> str:
    """M for a denied call: why, and the path."""
    if level == HARD:
        why = "in the hard band only %s are allowed until it is written" % (ALLOWED_AT_HARD % session.handover)
    else:
        why = "in the force band this project's force_deny_tools setting holds this tool back until it is written"
    return ("context-rotation denied this tool call: the context is %s, and %s. Write the handover at %s now "
            "with the rotate skill, as your last action, then end your turn."
            % (size_text(size, session.settings), why, session.handover))


def stop_reason(size: int, level: str, session: hook_runtime.Session) -> str:
    """M for a held-back Stop."""
    return ("Context is %s, in the %s band. Write the handover at %s with the rotate skill as your last action, "
            "then stop." % (size_text(size, session.settings), level, session.handover))


# --- the hooks ----------------------------------------------------------------------

def _settle(session: hook_runtime.Session, run: hook_runtime.HookRun, step: str,
            change: enforcement_state.Change) -> Any:
    """`change` on the session's state (enforcement_state.apply); None after a failure, which is logged."""
    run.at(step)
    try:
        return enforcement_state.apply(session.project_dir, session.session_id, change)
    except FAILURES as error:
        rotation_state.log_failure(session.project_dir, step, error)
        return None


def _measured(payload: Dict[str, Any], session: hook_runtime.Session,
              run: hook_runtime.HookRun) -> Tuple[Optional[int], Optional[str]]:
    run.at("measure")
    size = context_budget.measure(payload.get("transcript_path"))
    return size, enforcement_state.level_of(context_budget.band(size, session.settings))


def on_prompt(payload: Dict[str, Any], session: hook_runtime.Session, size: int,
              run: hook_runtime.HookRun) -> Tuple[Optional[str], Optional[str]]:
    """(M, U) for a measured prompt, both None below force; a fall below force is recorded."""
    level = enforcement_state.level_of(context_budget.band(size, session.settings))

    def change(state):
        events = enforcement_state.observe(state, level)
        why = inform_reason(state, payload)
        told = enforcement_state.tell(state) if level != LOW else []
        return events + told, (why, crossing_line(size, level, session, why) if told else None)

    settled = _settle(session, run, "enforcement state at UserPromptSubmit", change)
    if settled is None:  # the state failed: inform as the stored state says, without the user line
        settled = inform_reason(enforcement_state.load(session.project_dir, session.session_id), payload), None
    if level == LOW:
        return None, None
    why, line = settled
    return handover_now(size, level, session, why), line


def on_tool(payload: Dict[str, Any], session: hook_runtime.Session, run: hook_runtime.HookRun) -> Reply:
    """(M reason, U line or None) when the band denies this call; None when it goes ahead."""
    tool_name, tool_input = payload.get("tool_name"), payload.get("tool_input")
    if hard_allows(tool_name, tool_input, session):
        return None
    size, level = _measured(payload, session, run)
    if level is None:
        return None

    def change(state):
        events = enforcement_state.observe(state, level)
        if inform_reason(state, payload) or not denies(level, tool_name, session.settings):
            return events, None
        events += enforcement_state.deny(state, session.settings["hard_max_denials"])
        told = enforcement_state.tell(state)
        lines = [crossing_line(size, level, session, None)] if told else []
        if state["inform_only"] == enforcement_state.DENIAL_CAP:
            lines.append(cap_line(state))
        return events + told, (deny_reason(size, level, session), " ".join(lines) or None)

    return _settle(session, run, "enforcement state at PreToolUse", change)


def on_stop(payload: Dict[str, Any], session: hook_runtime.Session, run: hook_runtime.HookRun) -> Reply:
    """(M reason, U line or None) when this Stop is held back for the handover; None when it goes ahead."""
    size, level = _measured(payload, session, run)
    if level is None:
        return None
    active = bool(payload.get("stop_hook_active"))

    def change(state):
        events = enforcement_state.observe(state, level)
        if level == LOW or active or inform_reason(state, payload):
            return events, None
        blocked = enforcement_state.block_stop(state)
        if not blocked:
            return events, None
        told = enforcement_state.tell(state)
        line = crossing_line(size, level, session, None) if told else None
        return events + blocked + told, (stop_reason(size, level, session), line)

    return _settle(session, run, "enforcement state at Stop", change)


def on_release(session: hook_runtime.Session, run: hook_runtime.HookRun) -> Tuple[str, str]:
    """(M, U) after the user typed /context-rotation:rotate release: the session only informs from now on."""
    if _settle(session, run, "record release", _released):
        return RELEASED % (RELEASE_COMMAND, session.handover), RELEASED_LINE
    return RELEASE_FAILED % RELEASE_COMMAND, RELEASE_FAILED_LINE


def _released(state: Dict[str, Any]) -> Tuple[enforcement_state.Events, bool]:
    return enforcement_state.release(state), True


def on_handover_write(session: hook_runtime.Session, run: hook_runtime.HookRun) -> None:
    """A write of the session's handover: the denials so far no longer count toward hard_max_denials."""
    _settle(session, run, "reset denials", _reset)


def _reset(state: Dict[str, Any]) -> Tuple[enforcement_state.Events, None]:
    return enforcement_state.reset_denials(state), None
