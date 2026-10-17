"""The automatic /clear inside tmux (loop step 7, the automatic path), as the hooks see it.

At Stop with a valid, current ready-marker, inside tmux (`TMUX` and `TMUX_PANE` in the
hook's environment, the pane the marker recorded) and with `auto_clear` on:

- a running entry in `background_tasks`: the user is told what still runs and that
  /clear is not typed for them (U); nothing is started;
- otherwise the hook writes a job file and starts scripts/tmux_clear.sh detached through
  `tmux run-shell -b`, and the turn ends. The job (tmux_clear) types /clear and one fixed
  line into the session's own pane after its checks. A failure to start it falls back to
  the manual message.

The line run-shell gets passes two expansions: tmux expands `#{...}`, `#(...)` and `##`
in it, then `/bin/sh -c` runs it. So it holds fixed words, the script path shell-quoted,
and ids checked against fixed patterns (the pane `%<n>`, the session id, the generation,
a random job id), with every `#` doubled. The project path is never on that line: it is
in the job file. The line sends its output to /dev/null and always exits 0, because tmux
shows a run-shell job that prints or fails in view mode over the pane (measured, tmux 3.7).

The job file lives in `~/.claude/context-rotation/jobs/`, directories the user owns with
mode 0700, and the job reads and removes it. The job's state per pane lives in the
project's `.claude/rotation/`, behind the storage gate: `clear-<n>.lock`, the directory that
lets one job at a time act in pane %<n>, and `clear-<n>.json`, the record of the last job
there. Every change of that record has its line in injections.jsonl first
(state_fields.JOURNAL_EVENTS). With it the next UserPromptSubmit of the same `claude`
process repeats a failed clear's message (U), and SessionStart tells a clear the job typed
from one the user typed. Standard library only.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import stat
import subprocess
import time
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

import regular_file
import rotation_state
import state_fields

TMUX_TIMEOUT_SECONDS = 5.0
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "tmux_clear.sh")
JOB_ID = re.compile(r"[0-9a-f]{32}")
JOB_FILE = re.compile(r"[0-9a-f]{32}\.json")
JOB_VERSION = 1
# hook_pid: the Stop hook that starts the job, which waits until it has exited.
JOB_FIELDS = ("version", "project_dir", "session_id", "generation", "pane", "process", "hook_pid")
JOB_MAX_BYTES = 64 * 1024
JOB_FILE_MAX_AGE_SECONDS = 600
MAX_PATH = 4096
MAX_PROCESS_START = 200
NOTED = "auto-clear-noted"
DISPLAY_MILLISECONDS = "15000"
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
START_FAILURES = (OSError, ValueError, subprocess.SubprocessError)

RECORD_VERSION = 1
RECORD_FIELDS = ("version", "pane", "origin_session_id", "generation", "process", "step", "at_step", "reason",
                 "successor", "at", "told", "left_in_line")
# A job goes started -> entered -> acknowledged -> continued; "aborted" records where it stopped (at_step),
# and left_in_line whether what it typed may still be in the input line, which it then did not touch.
STEPS = ("started", "entered", "acknowledged", "continued", "aborted")
REACHED = ("started", "entered", "acknowledged")
FINISHED_TASK = ("completed", "failed", "killed", "stopped")
TASK_WORDS = {"shell": ("background shell", "background shells"), "subagent": ("subagent", "subagents")}

MANUAL = "Handover saved. Type /clear, then any message, to continue in a fresh context."
STARTED = (
    "context-rotation: handover saved. /clear is typed in this pane for you once the session is idle; type anything "
    "in the input line to stop that, or type /clear yourself."
)
BACKGROUND = (
    "context-rotation: handover saved, but this session still runs %s, so /clear is not typed for you. Type /clear, "
    "then any message, once you want to continue in a fresh context."
)
UNLISTED = (
    "context-rotation: handover saved. This Claude Code does not list its background tasks at Stop, so /clear is not "
    "typed for you. Type /clear, then any message, to continue in a fresh context."
)
BEFORE = "context-rotation: no automatic /clear: %s. Type /clear, then any message, to continue in a fresh context."
NOT_CURRENT = "context-rotation: no automatic /clear: %s. The clear follows once the handover is written again."
ENTERED = (
    "context-rotation: /clear was typed, but %s. Type /context-rotation:rotate resume to continue from the handover."
)
ACKNOWLEDGED = (
    "context-rotation: this session continues from the handover of the previous one, but the line that starts it "
    "was not sent: %s. Type any message to continue."
)
REPEAT_BEFORE = (
    "context-rotation: the automatic /clear did not happen: %s. This prompt is new work after the handover, so the "
    "clear follows once the handover is written again."
)
REPEAT_ACKNOWLEDGED = (
    "context-rotation: this session continued from the handover of the previous one; the line that starts it was "
    "not sent: %s."
)
LEFT = " The text it typed was left in the input line as it is, with anything you typed there."
OFF_HINT = (
    "context-rotation typed this /clear for you. To type it yourself instead, set \"auto_clear\": false in "
    ".claude/context-rotation.json (this project) or ~/.claude/context-rotation.json (every project)."
)


# --- the one place tmux runs ------------------------------------------------------

def run_tmux(args: Sequence[str]) -> Tuple[int, str]:
    """Run `tmux <args>`: (exit status, stdout). OSError or subprocess.SubprocessError when tmux cannot run.

    tmux finds its server through TMUX in the environment. Its output is read as
    UTF-8, whatever the locale: a capture holds `❯` and a no-break space.
    """
    done = subprocess.run(["tmux"] + list(args), stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=TMUX_TIMEOUT_SECONDS)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def display(pane: Optional[str], text: str) -> None:
    """Show `text` in the status line of the clients that show `pane` (any client without one). Never raises.

    `text` is one of this module's fixed lines: display-message expands formats in
    it, and none holds a `#`. tmux older than 3.2 has no -d, so it is tried without.
    """
    target = ["-t", pane] if pane else []
    for args in (["display-message", "-d", DISPLAY_MILLISECONDS] + target + [text],
                 ["display-message"] + target + [text]):
        try:
            if run_tmux(args)[0] == 0:
                return
        except (OSError, subprocess.SubprocessError):
            return


# --- what the hook checks -----------------------------------------------------------

valid_pane = state_fields.is_pane_id


def valid_process(value: Any) -> bool:
    """The process identity a marker records: a positive pid and a start time (rotation_state.process_identity)."""
    return (isinstance(value, dict) and set(value) == {"pid", "start"} and state_fields.is_count(value["pid"])
            and value["pid"] > 0 and isinstance(value["start"], str) and 0 < len(value["start"]) <= MAX_PROCESS_START)


def wanted(env: Dict[str, str], settings: Dict[str, Any], marker: Dict[str, Any]) -> bool:
    """The automatic path applies: auto_clear on, inside tmux, in the pane and the process the marker recorded."""
    pane = env.get("TMUX_PANE")
    return (settings["auto_clear"] is True and bool(env.get("TMUX")) and valid_pane(pane)
            and marker.get("tmux_pane") == pane and valid_process(marker.get("process")))


def _counted(kind: str, count: int) -> str:
    one, many = TASK_WORDS.get(kind, ("other task", "other tasks"))
    return "%d %s" % (count, one if count == 1 else many)


def running(tasks: list) -> Optional[str]:
    """What still runs, in words ("1 background shell and 2 subagents"); None when nothing does.

    Only counts and fixed words, no text from the payload. A task whose status is not
    a finished one counts as running.
    """
    counts: Dict[str, int] = {}
    for task in tasks:
        status = task.get("status") if isinstance(task, dict) else None
        if status in FINISHED_TASK:
            continue
        kind = task.get("type") if isinstance(task, dict) and task.get("type") in TASK_WORDS else "task"
        counts[kind] = counts.get(kind, 0) + 1
    parts = [_counted(kind, counts[kind]) for kind in ("shell", "subagent", "task") if counts.get(kind)]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


# --- the job file ----------------------------------------------------------------------

def _is_version(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


def _lstat(directory: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(name, dir_fd=directory)
    except FileNotFoundError:
        return None


def _owned_below(parent: int, name: str, create: bool) -> int:
    """A descriptor of the directory `name` below `parent`: a real one of this user with mode 0700, else refused."""
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent)
    descriptor = os.open(name, _DIRECTORY, dir_fd=parent)
    found = os.fstat(descriptor)
    if found.st_uid != os.getuid() or found.st_mode & 0o077:
        os.close(descriptor)
        raise rotation_state.StorageRefused("%s is not a directory of this user with mode 0700" % name)
    return descriptor


@contextlib.contextmanager
def user_storage(*parts: str, create: bool = False) -> Iterator[int]:
    """A descriptor of `~/.claude/context-rotation/<parts>`, each directory below `~/.claude` checked (_owned_below)."""
    claude = os.path.join(os.path.expanduser("~"), ".claude")
    if create:
        os.makedirs(claude, exist_ok=True)
    descriptor = os.open(os.path.realpath(claude), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in ("context-rotation",) + parts:
            below = _owned_below(descriptor, name, create)
            os.close(descriptor)
            descriptor = below
        yield descriptor
    finally:
        os.close(descriptor)


def _sweep_jobs(directory: int) -> None:
    """Remove job files no job took: older than JOB_FILE_MAX_AGE_SECONDS."""
    for name in os.listdir(directory):
        found = _lstat(directory, name) if JOB_FILE.fullmatch(name) else None
        if found is not None and stat.S_ISREG(found.st_mode) and time.time() - found.st_mtime > JOB_FILE_MAX_AGE_SECONDS:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory)


def write_job(document: Dict[str, Any]) -> str:
    """A new job file holding `document`, complete before anything can start its job: its id."""
    job_id = os.urandom(16).hex()
    data = json.dumps(document, sort_keys=True).encode("utf-8")
    with user_storage("jobs", create=True) as directory:
        _sweep_jobs(directory)
        descriptor = os.open(job_id + ".json", _NEW_FILE, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    return job_id


def remove_job(job_id: str) -> None:
    with contextlib.suppress(FileNotFoundError), user_storage("jobs") as directory:
        os.unlink(job_id + ".json", dir_fd=directory)


def job_problem(document: Any) -> Optional[str]:
    """Why a job file is not one the Stop hook writes; None when it is."""
    if not isinstance(document, dict) or set(document) != set(JOB_FIELDS) or not _is_version(
            document["version"], JOB_VERSION):
        return "its fields are not those of a job file"
    project_dir = document["project_dir"]
    if not state_fields.is_plain_path(project_dir) or len(project_dir) > MAX_PATH:
        return "its project_dir is not a plain absolute path"
    if not (state_fields.is_safe_session_id(document["session_id"])
            and state_fields.is_generation(document["generation"]) and valid_pane(document["pane"])
            and valid_process(document["process"]) and state_fields.is_count(document["hook_pid"])
            and document["hook_pid"] > 0):
        return "its ids are not ones the plugin writes"
    return None


def take_job(job_id: str) -> Optional[Dict[str, Any]]:
    """The job file of `job_id`, read and removed; None when it is absent, unreadable or not one the hook writes.

    Only a regular file of this user is read. OSError when its directory cannot be had.
    """
    name = job_id + ".json"
    with user_storage("jobs") as directory:
        found = _lstat(directory, name)
        if found is None or not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid():
            return None
        raw = regular_file.read_regular(name, directory, JOB_MAX_BYTES)
        os.unlink(name, dir_fd=directory)
    try:
        document = json.loads(raw.decode("utf-8")) if raw is not None else None
    except ValueError:
        return None
    return document if job_problem(document) is None else None


def run_shell_line(words: Sequence[str]) -> str:
    """The line for `tmux run-shell`: unchanged through tmux's format expansion and `sh -c`, silent, exit 0."""
    line = "sh %s >/dev/null 2>&1 || true" % " ".join(shlex.quote(word) for word in words)
    return line.replace("#", "##")


def start(session: Any, marker: Dict[str, Any]) -> None:
    """Write the job file and start the job detached. START_FAILURES when it does not start; the file then goes."""
    pane, origin, generation = marker["tmux_pane"], marker["session_id"], marker["generation"]
    job_id = write_job({
        "version": JOB_VERSION, "project_dir": os.path.abspath(session.project_dir), "session_id": origin,
        "generation": generation, "pane": pane, "process": marker["process"], "hook_pid": os.getpid(),
    })
    try:
        status, _ = run_tmux(["run-shell", "-b", "-t", pane, run_shell_line([SCRIPT, pane, origin, generation, job_id])])
        if status != 0:
            raise OSError("tmux run-shell exited with status %d" % status)
    except START_FAILURES:
        remove_job(job_id)
        raise


def at_stop(payload: Dict[str, Any], session: Any, marker: Dict[str, Any], env: Dict[str, str], run: Any) -> str:
    """The user's line at Stop for a current handover (U): the manual message, or why /clear is (not) typed."""
    if not wanted(env, session.settings, marker):
        return MANUAL
    tasks = payload.get("background_tasks")
    if not isinstance(tasks, list):
        return UNLISTED
    still = running(tasks)
    if still:
        return BACKGROUND % still
    run.at("start the automatic clear")
    try:
        start(session, marker)
    except START_FAILURES as error:
        rotation_state.log_failure(session.project_dir, "start the automatic clear", error)
        return MANUAL
    return STARTED


# --- the record of a pane's job: clear-<n>.json --------------------------------------

def pane_name(pane: str, suffix: str) -> str:
    return "clear-%s%s" % (pane[1:], suffix)


def record_path(project_dir: str, pane: str) -> str:
    return os.path.join(os.path.abspath(rotation_state.rotation_dir(project_dir)), pane_name(pane, ".json"))


def new_record(document: Dict[str, Any]) -> Dict[str, Any]:
    """The record of a job that starts, from its job file."""
    return {"version": RECORD_VERSION, "pane": document["pane"], "origin_session_id": document["session_id"],
            "generation": document["generation"], "process": document["process"], "step": "started",
            "at_step": None, "reason": None, "successor": None, "at": rotation_state.utc_now(), "told": False,
            "left_in_line": False}


def record_problem(record: Any, pane: str) -> Optional[str]:
    """Why a record is not one a job of `pane` writes; None when it is."""
    if not isinstance(record, dict) or set(record) != set(RECORD_FIELDS) or not _is_version(
            record["version"], RECORD_VERSION):
        return "its fields are not those of a clear record"
    if record["pane"] != pane or not valid_pane(pane):
        return "it is not the record of pane %s" % pane
    if not (state_fields.is_safe_session_id(record["origin_session_id"])
            and state_fields.is_generation(record["generation"]) and valid_process(record["process"])):
        return "its ids are not ones the plugin writes"
    aborted = record["step"] == "aborted"
    if record["step"] not in STEPS or aborted != state_fields.is_clear_abort(record["reason"]):
        return "its step and reason are not ones the plugin writes"
    if (record["at_step"] in REACHED) != aborted or (not aborted and record["at_step"] is not None):
        return "its at_step is not one the plugin writes"
    if not isinstance(record["left_in_line"], bool) or (record["left_in_line"] and not aborted):
        return "its left_in_line is not one the plugin writes"
    successor = record["successor"]
    if successor is not None and not state_fields.is_safe_session_id(successor):
        return "its successor is not a plain file name"
    if not state_fields.is_plugin_time(record["at"]) or not isinstance(record["told"], bool):
        return "its time or told flag is not one the plugin writes"
    return None


def load_record(project_dir: str, pane: str) -> Optional[Dict[str, Any]]:
    """The record of the pane's last job; None when absent, unreadable or not one a job writes."""
    raw = rotation_state.read_bytes(project_dir, record_path(project_dir, pane))
    try:
        record = json.loads(raw.decode("utf-8")) if raw is not None else None
    except ValueError:
        return None
    return record if record_problem(record, pane) is None else None


def _line_of(event: str, record: Dict[str, Any]) -> Dict[str, Any]:
    line = {"origin_session_id": record["origin_session_id"], "generation": record["generation"],
            "pane": record["pane"]}
    if event in ("clear_acknowledged", "clear_continued"):
        line["session_id"] = record["successor"]
    if event == "clear_aborted":
        line.update(abort_reason=record["reason"], left_in_line=record["left_in_line"])
    return line


def transition(project_dir: str, record: Dict[str, Any], event: str, **changes: Any) -> None:
    """One step of a pane's job: its line in injections.jsonl, then the record, under the project lock.

    `record` is changed in place once both are written. ValueError for a change that
    makes a record or a line the plugin would refuse to read back; nothing is written.
    """
    updated = dict(record, at=rotation_state.utc_now(), **changes)
    problem = record_problem(updated, record["pane"])
    if problem:
        raise ValueError("clear record refused: %s" % problem)
    with rotation_state.project_lock(project_dir):
        rotation_state.journal(project_dir, event, **_line_of(event, updated))
        rotation_state.write_json(project_dir, record_path(project_dir, updated["pane"]), updated)
    record.clear()
    record.update(updated)


# --- the user's lines after a job stopped ------------------------------------------------

def display_text(reached: str, reason: str, left: bool = False) -> str:
    """The line display-message shows when a job stops after `reached` for `reason`; `left`: LEFT follows."""
    why = state_fields.CLEAR_ABORTS[reason]
    if reached == "entered":
        text = ENTERED % why
    elif reached == "acknowledged":
        text = ACKNOWLEDGED % why
    else:
        text = (NOT_CURRENT if reason == "not_current" else BEFORE) % why
    return text + (LEFT if left else "")


def repeat_text(record: Dict[str, Any]) -> str:
    """The same message at the next prompt of the session's `claude` process (U)."""
    why = state_fields.CLEAR_ABORTS[record["reason"]]
    if record["at_step"] == "entered":
        text = ENTERED % why
    elif record["at_step"] == "acknowledged":
        text = REPEAT_ACKNOWLEDGED % why
    else:
        text = REPEAT_BEFORE % why
    return text + (LEFT if record["left_in_line"] else "")


def _take_notice(project_dir: str, pane: str, identity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    with rotation_state.project_lock(project_dir):
        record = load_record(project_dir, pane)
        if record is None or record["step"] != "aborted" or record["told"] or record["process"] != identity:
            return None
        transition(project_dir, record, "clear_told", told=True)
    return record


def prompt_notice(payload: Dict[str, Any], env: Dict[str, str], run: Any, session: Any) -> Optional[str]:
    """The message of a job that stopped in this pane, once, at the next prompt of its `claude` process (U).

    A prompt that is /clear itself takes the message without showing it. A failure of
    the record is logged and shows nothing.
    """
    pane = env.get("TMUX_PANE")
    if not valid_pane(pane) or not rotation_state.stored_exists(session.project_dir,
                                                                 record_path(session.project_dir, pane)):
        return None
    run.at("repeat the stopped clear")
    identity = rotation_state.process_identity(env)
    if identity is None:
        return None
    try:
        record = _take_notice(session.project_dir, pane, identity)
    except (OSError, rotation_state.LockTimeout, ValueError) as error:
        rotation_state.log_failure(session.project_dir, "repeat the stopped clear", error)
        return None
    if record is None or not rotation_state.prompt_outdates(payload.get("prompt")):
        return None
    return repeat_text(record)


# --- SessionStart: was this clear typed by the job? ---------------------------------------

def typed_by_job(env: Dict[str, str], project_dir: str, marker: Dict[str, Any]) -> bool:
    """The pane's job entered /clear for this marker's generation (its record says so). Never raises."""
    pane = env.get("TMUX_PANE")
    if not valid_pane(pane):
        return False
    record = load_record(project_dir, pane)
    return (record is not None and record["step"] == "entered"
            and record["origin_session_id"] == marker["session_id"] and record["generation"] == marker["generation"])


def first_on_machine() -> bool:
    """True for the first automatic clear of this user, and whenever that cannot be recorded. Never raises."""
    try:
        with user_storage(create=True) as directory:
            os.close(os.open(NOTED, _NEW_FILE, 0o600, dir_fd=directory))
    except FileExistsError:
        return False
    except OSError:
        return True
    return True


def find_ack(project_dir: str, origin: str, generation: str) -> Optional[str]:
    """The session this generation was delivered to after /clear: a `delivered` line via `clear` for it.

    Only that line counts: it is written once the emit returned, while `injected`
    comes before the emit, which can still fail and leave the claim unfinished
    (handover_injection.deliver). OSError when injections.jsonl cannot be read.
    """
    for raw in reversed(rotation_state.journal_checked(project_dir).splitlines()):
        try:
            line = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if (state_fields.record_problem(line) is None and line["event"] == "delivered" and line["via"] == "clear"
                and line["origin_session_id"] == origin and line["generation"] == generation):
            return line["session_id"]
    return None

