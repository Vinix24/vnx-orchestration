"""What the force and hard bands do in each hook (loop step 3), and how a session gets out.

  level  UserPromptSubmit           PreToolUse                              Stop
  force  M: write the handover now  nothing, or a denial of a tool that     held back once per
                                    force_deny_tools names                  crossing (M)
  hard   as force                   denies all but HARD_ALLOWED and a       as force
                                    Write or Edit of the handover (never
                                    through a symlink at its path)

Whatever the band, and also below warn, in plan mode, after a release and inside a
subagent: a Write, Edit, MultiEdit or NotebookEdit that lands on the plugin's own
files is denied (M), and that denial counts toward no cap (storage_denial). Those
are the project's `.claude/rotation`, other than the session's own handover, which
holds the state that decides what is denied; the `.claude` above it, where a file
makes the storage gate refuse the project; and either settings file
(context_budget.settings_paths), existing or not, whose `"enabled": false` switches
every hook off. Where a write lands is judged by the real directories against the
rotation directory that rotation_state.storage verifies, both as the kernel opens
the path (a symlink followed before a `..` after it) and with `..` collapsed by
name, so neither a symlink nor a `..` redirects the check. Each spelling is judged
on its own: one that lands on such a file denies, and only when none does and one
could not be judged is that failure logged and the call let through. A shell
command is not parsed: it runs with the user's rights, and this module does not
see what it changes, nor what an MCP tool writes.

M goes to the model (additionalContext, permissionDecisionReason, the Stop
reason), U to the user (systemMessage). Each crossing gets one user line, at the
first UserPromptSubmit, denial or held-back Stop in it. Every message names the
session's handover path as the plugin builds it; its other values are the
measured size and the band's threshold, both in tokens, counts from a checked
state and fixed names.

A session only informs, so nothing is denied or held back: in plan mode
(`permission_mode: plan`); after the user typed `/context-rotation:rotate
release`, which the UserPromptExpansion hook records and which only a typed
command fires; and after hard_max_denials denials without a handover write,
which the user is told once. No Stop is held back while stop_hook_active is set.
Apart from that denial nothing acts at an unknown size, and inside a subagent
(hook_runtime.open_project) a hook denies a write of the plugin's own files and does
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
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, Union

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
CHECK_STORAGE = "check write of the plugin's own files"
STORAGE_REASON = (
    "context-rotation denied this tool call: it writes into %s, which the context-rotation plugin keeps for its "
    "own files. No tool call writes there, in any band, except a write of this session's handover at %s."
)
SETTINGS_REASON = (
    "context-rotation denied this tool call: it writes %s, the context-rotation plugin's settings file. No tool "
    "call writes it, in any band: the user changes it themselves."
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


# --- what no band lets through: a write of the plugin's own files ------------------

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


Told = TypeVar("Told")


def _told(tell: Callable[[], Told]) -> Union[Told, OSError]:
    """What `tell` returns, or the OSError that says it cannot be told, kept to raise where it is needed."""
    try:
        return tell()
    except OSError as error:
        return error


def _known(told: Union[Told, OSError]) -> Told:
    """The value _told kept; its OSError raised."""
    if isinstance(told, OSError):
        raise told
    return told


def _lands(place: str, kept: Union[Place, OSError], inside: bool) -> bool:
    """`place` is the kept place, or with `inside` lies in it; OSError when either cannot be told.

    A file at the rotation directory or at the `.claude` it is made in makes the
    storage gate refuse the project (rotation_state.storage_problem), and then no
    hook enforces anything; so both are kept, and the rotation directory with
    everything inside it.
    """
    directory, names = _known(kept)
    found = _names_below(place, directory)
    wanted = tuple(name.casefold() for name in names)
    return found is not None and (found[:len(wanted)] == wanted if inside else found == wanted)


# A check of one place a write can land: the denial when it lands on what the check keeps, else None.
Check = Callable[[str], Optional[str]]


def _settings_check(path: str) -> Check:
    """Keeps the settings file at `path` (context_budget.settings_paths), whether it exists or not."""
    kept = _told(lambda: _where_made(os.path.realpath(path)))
    return lambda place: SETTINGS_REASON % path if _lands(place, kept, inside=False) else None


def _storage_checks(session: hook_runtime.Session) -> List[Check]:
    """Keep `.claude` and `.claude/rotation` with everything in it, but for the session's own handover.

    A place is that handover when it lands where one of the handover's own places
    lands, while the handover path holds a regular file or nothing yet (as for
    writes_handover). Each part is told on its own: one that fails raises only
    where it decides.
    """
    reason = STORAGE_REASON % (os.path.dirname(session.handover), session.handover)
    claude = _told(lambda: _claude_place(session.project_dir))
    storage = _told(lambda: _storage_place(session.project_dir))
    own =_told(lambda: {_where_made(place) for place in _places(session.handover)})
    handover_open = _told(lambda: _handover_open(session))

    def exempt(place: str) -> bool:
        return _where_made(place) in _known(own) and _known(handover_open)

    return [lambda place: reason if _lands(place, claude, inside=False) else None,
            lambda place: reason if _lands(place, storage, inside=True) and not exempt(place) else None]


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


def _spellings(target: str, failures: List[OSError]) -> List[str]:
    """The places of `target` (_places); none when they cannot be told, after that failure is added to `failures`."""
    try:
        return _places(target)
    except OSError as error:
        failures.append(error)
        return []


def writes_into_storage(tool_name: Any, tool_input: Any, session: hook_runtime.Session) -> Optional[str]:
    """The denial for a Write, Edit, MultiEdit or NotebookEdit of the plugin's own files; None for any other call.

    Those are either settings file, existing or not, the files load_settings reads
    (context_budget.settings_paths); `.claude/rotation` and everything in it, but the
    session's own handover; and the `.claude` above it. Where a write lands is judged
    by the real directories, not by the path as written (_places): a `..` after a
    symlink, a symlink elsewhere that leads in, a symlink inside that leads out, a `~`
    and a name in another case all count. A settings file is checked first, so the
    denial names what the write would change.

    Each spelling is judged on its own, against each kept place on its own: one that
    lands on a kept place denies, also when another cannot be judged. OSError, the
    first such failure, only when none lands on one and some could not be judged.
    """
    targets = _write_targets(tool_name, tool_input)
    if not targets:
        return None
    failures: List[OSError] = []
    places = [place for target in targets for place in _spellings(target, failures)]
    checks = [_settings_check(path) for path in context_budget.settings_paths(session.project_dir)]
    for check in checks + _storage_checks(session):
        for place in places:
            try:
                denial = check(place)
            except OSError as error:
                failures.append(error)
                continue
            if denial:
                return denial
    if failures:
        raise failures[0]
    return None


def storage_denial(tool_name: Any, tool_input: Any, session: hook_runtime.Session, run: hook_runtime.HookRun) -> Reply:
    """(M, None) for a write of the plugin's own files (writes_into_storage); None for any other call.

    None too when no spelling of the path lands on one and some could not be judged,
    after that failure is logged: a failed check never denies.
    """
    run.at(CHECK_STORAGE)
    try:
        denial = writes_into_storage(tool_name, tool_input, session)
    except OSError as error:
        rotation_state.log_failure(session.project_dir, CHECK_STORAGE, error)
        return None
    return (denial, None) if denial else None


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

def size_phrase(size: int, level: str, settings: Dict[str, Any]) -> str:
    """The size and the band's threshold, in tokens, for a message the model or the user sees."""
    threshold = context_budget.threshold_for(settings, level)
    return "at %s tokens, in the %s band (threshold %s tokens)" % (
        context_budget.token_text(size), level, context_budget.token_text(threshold))


def handover_now(size: int, level: str, session: hook_runtime.Session, why: Optional[str]) -> str:
    """M at UserPromptSubmit in the force and hard bands."""
    text = ("Context is %s. Write the handover at %s now with the rotate skill "
            "(/context-rotation:rotate), and make that write your last action."
            % (size_phrase(size, level, session.settings), session.handover))
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
    head = "context-rotation: the context is %s; the handover goes to %s." % (
        size_phrase(size, level, session.settings), session.handover)
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
            % (size_phrase(size, level, session.settings), why, session.handover))


def stop_reason(size: int, level: str, session: hook_runtime.Session) -> str:
    """M for a held-back Stop."""
    return ("Context is %s. Write the handover at %s with the rotate skill as your last action, "
            "then stop." % (size_phrase(size, level, session.settings), session.handover))


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
              run: hook_runtime.HookRun) -> Tuple[Optional[int], bool]:
    run.at("measure")
    size = context_budget.measure(payload.get("transcript_path"))
    return size, rotation_state.injected_on_clear(session.project_dir, session.session_id)


def _record_and_level(state: Dict[str, Any], size: Optional[int], settings: Dict[str, Any],
                      injected: bool) -> Tuple[enforcement_state.Events, Optional[str]]:
    """(events, level) for a measured size: record an injected session's start size, then its level.

    The start size is the first known measurement and is never overwritten, so a
    compaction or a fall below it does not reset it. Unknown sizes stay unknown.
    """
    events: enforcement_state.Events = []
    if injected and state["start_size"] is None and size is not None:
        events += enforcement_state.record_start(state, size)
    return events, _loop_level(state, size, settings)


def _loop_level(state: Dict[str, Any], size: Optional[int], settings: Dict[str, Any]) -> Optional[str]:
    """The level of a measured size, holding an injected session below force and hard until it did real work.

    A session that started from a handover injection reaches force or hard only when
    the size is at or above the threshold AND at least min_work_tokens above its start
    size. The warn band is unchanged, and every other session keeps the plain level.
    """
    if size is None:
        return None
    band = context_budget.band(size, settings)
    start = state["start_size"]
    if start is not None and band in (FORCE, HARD) and size - start < settings["min_work_tokens"]:
        return LOW
    return enforcement_state.level_of(band)


def _loop_line_due(state: Dict[str, Any], settings: Dict[str, Any]) -> bool:
    """True when the one-time loop line is due: the start size and min work already reach the force threshold."""
    if state["loop_warned"] or state["start_size"] is None:
        return False
    return state["start_size"] + settings["min_work_tokens"] >= context_budget.threshold_for(settings, "force")


def loop_line(start: int, settings: Dict[str, Any]) -> str:
    """U, once per session: the start is so close to the force threshold that rotating will not help."""
    force = context_budget.threshold_for(settings, "force")
    return ("context-rotation: this session started at %s tokens, and its force threshold is %s tokens; %s work "
            "tokens bring it there, so rotating will not make the next session smaller. Raise the force "
            "threshold, or start with a smaller context (fewer CLAUDE.md imports), to stop the loop."
            % (context_budget.token_text(start), context_budget.token_text(force),
               context_budget.token_text(settings["min_work_tokens"])))


def on_prompt(payload: Dict[str, Any], session: hook_runtime.Session, size: int,
              run: hook_runtime.HookRun) -> Tuple[Optional[str], Optional[str]]:
    """(M, U) for a measured prompt, both None below force but for the loop line; a fall below force is recorded."""
    injected = rotation_state.injected_on_clear(session.project_dir, session.session_id)
    raw_level = enforcement_state.level_of(context_budget.band(size, session.settings))

    def change(state):
        events, level = _record_and_level(state, size, session.settings, injected)
        events += enforcement_state.observe(state, level)
        why = inform_reason(state, payload)
        told = enforcement_state.tell(state) if level != LOW else []
        lines = [crossing_line(size, level, session, why)] if told else []
        if _loop_line_due(state, session.settings):
            events += enforcement_state.loop_warn(state)
            lines.append(loop_line(state["start_size"], session.settings))
        text = handover_now(size, level, session, why) if level != LOW else None
        return events + told, (why, text, "\n".join(lines) or None)

    settled = _settle(session, run, "enforcement state at UserPromptSubmit", change)
    if settled is None:  # the state failed: inform as the stored state says, without the user line
        why = _stored_reason(payload, session)
        text = handover_now(size, raw_level, session, why) if raw_level != LOW else None
        return text, None
    why, text, line = settled
    return text, line


def _stored_reason(payload: Dict[str, Any], session: hook_runtime.Session) -> Optional[str]:
    """inform_reason of the stored state; UNREAD, after that failure is logged, when it cannot be read."""
    try:
        return inform_reason(enforcement_state.load(session.project_dir, session.session_id), payload)
    except FAILURES as error:
        rotation_state.log_failure(session.project_dir, "read enforcement state at UserPromptSubmit", error)
        return UNREAD


def on_tool(payload: Dict[str, Any], session: hook_runtime.Session, run: hook_runtime.HookRun) -> Reply:
    """(M reason, U line or None) when the band denies this call; None when it goes ahead.

    A write of the plugin's own files is denied in every band, also for an unknown
    size, and is counted as no denial (storage_denial). The band is recorded for
    every call, also one the hard band allows: only the denial depends on that.
    """
    tool_name, tool_input = payload.get("tool_name"), payload.get("tool_input")
    kept = storage_denial(tool_name, tool_input, session, run)
    size, injected = _measured(payload, session, run)
    if size is None:
        return kept
    allowed = bool(kept) or _allowed(tool_name, tool_input, session, run)

    def change(state):
        events, level = _record_and_level(state, size, session.settings, injected)
        events += enforcement_state.observe(state, level)
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
    size, injected = _measured(payload, session, run)
    if size is None:
        return None
    active = bool(payload.get("stop_hook_active"))

    def change(state):
        events, level = _record_and_level(state, size, session.settings, injected)
        events += enforcement_state.observe(state, level)
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
