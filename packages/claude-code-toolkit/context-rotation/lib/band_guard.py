"""What the force and hard bands do in each hook (loop step 3), and how a session gets out.

  level  UserPromptSubmit           PreToolUse                              Stop
  force  M: write the handover now  nothing, or a denial of a tool that     held back once per
                                    force_deny_tools names                  crossing (M)
  hard   as force                   denies all but HARD_ALLOWED and a       as force
                                    Write or Edit of the handover (never
                                    through a symlink at its path)

Whatever the band, and also below warn, in plan mode, after a release and inside a
subagent: a Write, Edit, MultiEdit or NotebookEdit that lands in the project's
`.claude/rotation`, other than of the session's own handover, is denied (M), and
that denial counts toward no cap (storage_denial). The directory holds the state
that decides what is denied, so a tool call of the model never writes it. A write
at that directory itself or at the `.claude` above it is denied the same way: a
file there makes the storage gate refuse the project. Where a write lands is
judged by the real directories against the rotation directory that
rotation_state.storage verifies, both as the kernel opens the path (a symlink
followed before a `..` after it) and with `..` collapsed by name, so neither a
symlink nor a `..` redirects the check, and a check that fails lets the call
through and is logged. A shell command is not parsed: it runs with the user's
rights, and this module does not see what it changes.

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
Apart from that denial nothing acts at an unknown size, and inside a subagent
(hook_runtime.open_project) a hook denies a write into `.claude/rotation` and does
nothing else. A failure of the enforcement state (OSError,
LockTimeout, ValueError) is logged (errors.log, else stderr) and the call goes
ahead as in a session that only informs: a failure never denies or holds back.
A read of that state that fails is such a failure, never a state to replace, so
nothing is written on it either; a handover path that cannot be checked lets the
call through and resets no denials. The band is recorded at every PreToolUse,
also for a call the hard band allows. Standard library only.
"""
from __future__ import annotations

import fnmatch
import os
import stat
from typing import Any, Dict, List, Optional, Tuple

import context_budget
import enforcement_state
import hook_runtime
import rotation_state
import transcript_work

LOW, FORCE, HARD = enforcement_state.LOW, enforcement_state.FORCE, enforcement_state.HARD
HARD_ALLOWED = ("Read", "Glob", "Grep", "Skill", "AskUserQuestion", "TodoWrite")
PLAN = "plan"
FAILURES = (OSError, rotation_state.LockTimeout, ValueError)
CHECK_HANDOVER = "check handover path"
# The tools that write the file at a path in their input, and the input fields that hold it.
FILE_WRITERS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
PATH_FIELDS = ("file_path", "notebook_path")
CHECK_STORAGE = "check write into the plugin directory"
STORAGE_REASON = (
    "context-rotation denied this tool call: it writes into %s, which the context-rotation plugin keeps for its "
    "own files. No tool call writes there, in any band, except a write of this session's handover at %s."
)
# A prompt whose state could not be read: its message says nothing about what is denied.
UNREAD = "unread"
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
    OSError when what is at that path cannot be checked: a failed lstat tells neither.
    """
    return transcript_work.is_handover_write(tool_name, tool_input, session.handover) and _handover_open(session)


def _handover_open(session: hook_runtime.Session) -> bool:
    """The handover path holds a regular file or nothing yet; OSError when a failed lstat tells neither."""
    try:
        with rotation_state.storage(session.project_dir) as directory:
            found = os.lstat(os.path.basename(session.handover), dir_fd=directory)
    except FileNotFoundError:
        return True
    return stat.S_ISREG(found.st_mode)


# --- what no band lets through: a write into the plugin's directory -----------------

Identity = Tuple[int, int]


def _identity(path: str) -> Optional[Identity]:
    """(device, inode) of what is at `path`, symlinks followed; None when nothing is.

    OSError for any other failure of the stat: it tells neither.
    """
    try:
        found = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return found.st_dev, found.st_ino


# (an existing directory's identity, the names below it): a path, perhaps not made yet.
Place = Tuple[Identity, Tuple[str, ...]]


def _storage_place(project_dir: str) -> Place:
    """Where `.claude/rotation` is, or where a write would make it.

    The rotation directory itself when it exists, through the descriptor
    rotation_state.storage verifies: a symlink there is refused (StorageRefused, an
    OSError), never followed. Otherwise its nearest existing ancestor, and the names
    a write would create below that one.
    """
    try:
        with rotation_state.storage(project_dir) as directory:
            found = os.fstat(directory)
    except FileNotFoundError:
        return _where_made(os.path.realpath(rotation_state.rotation_dir(project_dir)))
    return (found.st_dev, found.st_ino), ()


def _claude_place(project_dir: str) -> Place:
    """Where `.claude` is, as the storage gate resolves it, or where a write would make it."""
    return _where_made(os.path.realpath(os.path.join(project_dir, ".claude")))


def _where_made(path: str) -> Place:
    """The nearest existing ancestor of `path` (`path` itself when it exists), and the names below it down to `path`."""
    names: Tuple[str, ...] = ()
    identity = _identity(path)
    while identity is None:
        path, name = os.path.split(path)
        names = (name,) + names
        identity = _identity(path)
    return identity, names


def _names_below(place: str, directory: Identity) -> Optional[Tuple[str, ...]]:
    """The names from `directory` down to `place`, without case; None when `place` is neither it nor below it.

    Every ancestor is compared by identity, so a path that reaches the directory
    another way still matches. The names compare without case: on a
    case-insensitive file system, the macOS default, `.claude/Rotation` becomes the
    plugin's directory once a write makes it.
    """
    path, names = place, ()
    while _identity(path) != directory:
        parent, name = os.path.split(path)
        if parent == path:
            return None
        path, names = parent, (name,) + names
    return tuple(name.casefold() for name in names)


def _guarded(place: str, storage: Place, claude: Place) -> bool:
    """`place` is the rotation directory, lies in it, or is the `.claude` directory it is made in.

    A file at either directory makes the storage gate refuse the project
    (rotation_state.storage_problem), and then no hook enforces anything.
    """
    (directory, below), (above, name) = storage, claude
    found = _names_below(place, directory)
    if found is not None and found[:len(below)] == tuple(part.casefold() for part in below):
        return True
    return _names_below(place, above) == tuple(part.casefold() for part in name)


def _write_targets(tool_name: Any, tool_input: Any) -> List[str]:
    """The paths a Write, Edit, MultiEdit or NotebookEdit writes; none for any other call."""
    if tool_name not in FILE_WRITERS or not isinstance(tool_input, dict):
        return []
    found = (tool_input.get(field) for field in PATH_FIELDS)
    return [path for path in found if isinstance(path, str) and path and "\0" not in path]


def _places(target: str) -> List[str]:
    """The paths that say where a write of `target` can land.

    Two forms of the path: as the kernel opens it, where a symlink is followed before
    a `..` after it applies, and with `..` collapsed by name first, as a caller that
    normalises a path before it opens it does (Node's path.resolve). Of each form:
    the path as written, whose directories _names_below compares by identity, so a
    symlink inside the directory that leads out still passes through it; the name
    below its real directory; and where a symlink at the name leads. A leading `~`
    is the home directory, as Claude Code's file tools read it; a relative path
    starts at the working directory.
    """
    path = os.path.join(os.getcwd(), os.path.expanduser(target))
    places = []
    for form in (path, os.path.normpath(path)):
        parent, name = os.path.split(form)
        if name in ("", os.curdir, os.pardir):  # the path names a directory: the place is where it resolves
            named = os.path.realpath(form)
        else:
            named = os.path.join(os.path.realpath(parent), name)
        places += [form, named, os.path.realpath(form)]
    return places


def writes_into_storage(tool_name: Any, tool_input: Any, session: hook_runtime.Session) -> bool:
    """A Write, Edit, MultiEdit or NotebookEdit that lands in `.claude/rotation`, or at it or `.claude`, but not the handover.

    Where it lands is judged by the real directories, not by the path as written
    (_places): a `..` after a symlink, a symlink elsewhere that leads in, a symlink
    inside that leads out, a `~` and a name in another case all count. A place that
    is the handover is exempt while the handover path holds a regular file or
    nothing yet (as for writes_handover); the handover's path is judged as the
    target's, by identity where the file exists. OSError when where a write lands
    cannot be checked.
    """
    targets = _write_targets(tool_name, tool_input)
    if not targets:
        return False
    storage, claude = _storage_place(session.project_dir), _claude_place(session.project_dir)
    guarded = [place for target in targets for place in _places(target) if _guarded(place, storage, claude)]
    if not guarded:
        return False
    own = {_where_made(place) for place in _places(session.handover)} if _handover_open(session) else set()
    return any(_where_made(place) not in own for place in guarded)


def storage_denial(tool_name: Any, tool_input: Any, session: hook_runtime.Session, run: hook_runtime.HookRun) -> Reply:
    """(M, None) for a write into the plugin's directory (writes_into_storage); None for any other call.

    None too when where the write lands cannot be checked, after that failure is
    logged: a failed check never denies.
    """
    run.at(CHECK_STORAGE)
    try:
        into = writes_into_storage(tool_name, tool_input, session)
    except OSError as error:
        rotation_state.log_failure(session.project_dir, CHECK_STORAGE, error)
        return None
    return (STORAGE_REASON % (os.path.dirname(session.handover), session.handover), None) if into else None


def hard_allows(tool_name: Any, tool_input: Any, session: hook_runtime.Session) -> bool:
    """The calls the hard band lets through: reading, asking, the skill, todos, and writing the handover.

    OSError when the handover path cannot be checked (writes_handover).
    """
    return tool_name in HARD_ALLOWED or writes_handover(tool_name, tool_input, session)


def _allowed(tool_name: Any, tool_input: Any, session: hook_runtime.Session, run: hook_runtime.HookRun) -> bool:
    """hard_allows; True when the handover path cannot be checked, after that failure is logged."""
    run.at(CHECK_HANDOVER)
    try:
        return hard_allows(tool_name, tool_input, session)
    except OSError as error:
        rotation_state.log_failure(session.project_dir, CHECK_HANDOVER, error)
        return True


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
    if why == UNREAD:
        return text
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
        settled = _stored_reason(payload, session), None
    if level == LOW:
        return None, None
    why, line = settled
    return handover_now(size, level, session, why), line


def _stored_reason(payload: Dict[str, Any], session: hook_runtime.Session) -> Optional[str]:
    """inform_reason of the stored state; UNREAD, after that failure is logged, when it cannot be read."""
    try:
        return inform_reason(enforcement_state.load(session.project_dir, session.session_id), payload)
    except FAILURES as error:
        rotation_state.log_failure(session.project_dir, "read enforcement state at UserPromptSubmit", error)
        return UNREAD


def on_tool(payload: Dict[str, Any], session: hook_runtime.Session, run: hook_runtime.HookRun) -> Reply:
    """(M reason, U line or None) when the band denies this call; None when it goes ahead.

    A write into the plugin's directory is denied in every band, also for an unknown
    size, and is counted as no denial (storage_denial). The band is recorded for
    every call, also one the hard band allows: only the denial depends on that.
    """
    tool_name, tool_input = payload.get("tool_name"), payload.get("tool_input")
    kept = storage_denial(tool_name, tool_input, session, run)
    size, level = _measured(payload, session, run)
    if level is None:
        return kept
    allowed = bool(kept) or _allowed(tool_name, tool_input, session, run)

    def change(state):
        events = enforcement_state.observe(state, level)
        if allowed or inform_reason(state, payload) or not denies(level, tool_name, session.settings):
            return events, None
        events += enforcement_state.deny(state, session.settings["hard_max_denials"])
        told = enforcement_state.tell(state)
        lines = [crossing_line(size, level, session, None)] if told else []
        if state["inform_only"] == enforcement_state.DENIAL_CAP:
            lines.append(cap_line(state))
        return events + told, (deny_reason(size, level, session), " ".join(lines) or None)

    settled = _settle(session, run, "enforcement state at PreToolUse", change)
    return kept or settled


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


def on_handover_write(payload: Dict[str, Any], session: hook_runtime.Session, run: hook_runtime.HookRun) -> None:
    """A write of the session's handover: the denials so far no longer count toward hard_max_denials.

    Not for a write a symlink at the handover path took elsewhere (writes_handover),
    nor when that path cannot be checked: then the failure is logged and nothing is written.
    """
    run.at(CHECK_HANDOVER)
    try:
        written = writes_handover(payload.get("tool_name"), payload.get("tool_input"), session)
    except OSError as error:
        rotation_state.log_failure(session.project_dir, CHECK_HANDOVER, error)
        return
    if written:
        _settle(session, run, "reset denials", _reset)


def _reset(state: Dict[str, Any]) -> Tuple[enforcement_state.Events, None]:
    return enforcement_state.reset_denials(state), None
