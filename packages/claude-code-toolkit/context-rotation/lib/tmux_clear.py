"""The detached job of the automatic /clear (loop step 7), run by scripts/tmux_clear.sh.

Usage: tmux_clear.py <pane> <session_id> <generation> <job_id>

The Stop hook starts it through `tmux run-shell -b` (auto_clear) with validated ids;
the project path is in the job file. In this order:

1. the job file is read and removed, then the pane's lock `clear-<n>.lock` is taken
   with mkdir under the project lock, its line first; a lock older than
   STALE_LOCK_SECONDS is broken, with its own line; while another job holds it,
   this one leaves without a word;
2. it waits for the turn to settle: the Stop hook that started it has exited, then
   two equal captures of the pane in a row;
3. the ready-marker is read again and must still be current, for this generation,
   this pane and this `claude` process;
4. the pane checks: the pane is the marker's, alive, in no mode and not
   synchronized; its screen shows the input box, empty, with no spinner above it
   (pane_screen); the session's `claude` (CLAUDE_PID and its start time from the
   marker) runs in the process tree of `#{pane_pid}` and owns the input of the
   pane's terminal: `#{pane_tty}` is its controlling terminal, its process group is
   that terminal's foreground group, and it is not stopped (ps);
5. `/clear` is typed with `send-keys -l` and read back from the input line, which
   must read exactly `/clear`; then the step's line and record are written, the
   ready-marker is read again as in step 3, and a fresh look decides Enter (enter);
6. it waits up to ack_timeout_seconds (at most MAX_ACK_SECONDS) for the successor's
   `delivered` line via `clear` for this generation in injections.jsonl, written once
   the handover's emit returned;
7. after the turn settles again and the pane checks pass, `Continue from the handover
   in your context, step 1.` is typed and read back, and Enter goes the same way.

Enter goes only on a look taken after the step's journal line, with nothing between
that look and the key: the session's `claude` still runs below the pane and owns its
terminal's input, then one tmux call shows the pane's state and its screen at one
moment (look), and the input line must read exactly the typed line, with no spinner
above it. The job types into the user's terminal, so when it is not certain it sends
nothing. It removes what it typed only from a line that a look shows holds exactly
that, with the cursor right after it (take_back): every character those BSpace remove
is its own. Any other line is left as it is, and the user's lines say so.

Nothing read from a file or a screen is ever typed: only those two fixed lines, BSpace
and Enter, into the pane the marker names. Any failed check, and any read that fails
(an OSError of the marker, the handover, the transcript, the lock, the pane, the
process table or the journal), stops the job: nothing more is typed, the reason gets
its line in injections.jsonl and goes into the pane's record, `tmux display-message`
shows the user what to do, and the next prompt of that `claude` process repeats it
(auto_clear.prompt_notice). The job never exits 2. Standard library only.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

import auto_clear
import context_budget
import pane_screen
import rotation_state
import state_fields
import transcript_work

CLEAR = "/clear"
CONTINUE = "Continue from the handover in your context, step 1."
STALE_LOCK_SECONDS = 300
# The lock must stay fresh for the whole job: its waits, each with its deadline, add up to well
# under STALE_LOCK_SECONDS (the hook 15, two settles of 15, two read-backs of 3, the ack 120).
MAX_ACK_SECONDS = 120
SETTLE_SECONDS = 15.0
SETTLE_POLL_SECONDS = 0.5
HOOK_POLL_SECONDS = 0.1
VERIFY_SECONDS = 3.0
VERIFY_POLL_SECONDS = 0.1
ACK_POLL_SECONDS = 0.25
PANE_FORMAT = ("#{pane_id} #{pane_pid} #{pane_dead} #{pane_in_mode} #{pane_synchronized} #{cursor_x} #{cursor_y} "
               "#{pane_tty}")
MAX_ANCESTORS = 256
DIGITS = re.compile(r"[0-9]+")
# ps -o pid=,pgid=,tpgid=,stat=,tty= for one process: BSD ps (macOS) and procps (Linux) print the same columns.
TERMINAL_LINE = re.compile(r"\s*([0-9]+)\s+([0-9]+)\s+(-?[0-9]+)\s+(\S+)\s+(\S+)\s*")
FAILURES = (OSError, rotation_state.LockTimeout, ValueError)
REFUSED = "context-rotation: no automatic /clear: its job was started with ids it does not accept."

# The clock, one seam for the tests.
sleep: Callable[[float], None] = time.sleep
monotonic: Callable[[], float] = time.monotonic


def parse_args(argv: Sequence[str]) -> Optional[Tuple[str, str, str, str]]:
    """(pane, session id, generation, job id) when each matches its fixed pattern; None otherwise."""
    if len(argv) != 4:
        return None
    pane, origin, generation, job_id = argv
    if (auto_clear.valid_pane(pane) and state_fields.is_safe_session_id(origin)
            and state_fields.is_generation(generation) and auto_clear.JOB_ID.fullmatch(job_id)):
        return pane, origin, generation, job_id
    return None


def run_ps(args: Sequence[str]) -> str:
    """What `ps <args>` prints. OSError when ps cannot tell."""
    try:
        done = subprocess.run(["ps"] + list(args), capture_output=True, env=dict(os.environ, LC_ALL="C"),
                              timeout=rotation_state.PS_TIMEOUT_SECONDS)
    except subprocess.SubprocessError as error:
        raise OSError("ps did not answer: %s" % error)
    if done.returncode != 0:
        raise OSError("ps exited with status %d" % done.returncode)
    return done.stdout.decode("ascii", "replace")


def process_table() -> Dict[int, int]:
    """{pid: parent pid} of every process, from ps. OSError when ps cannot tell."""
    table = {}
    for line in run_ps(["-A", "-o", "pid=,ppid="]).splitlines():
        fields = line.split()
        if len(fields) == 2 and all(DIGITS.fullmatch(field) for field in fields):
            table[int(fields[0])] = int(fields[1])
    if not table:
        raise OSError("ps listed no process")
    return table


class Terminal(NamedTuple):
    """A process at its controlling terminal, as ps tells it."""

    pgid: int
    tpgid: int  # the terminal's foreground process group; without a terminal 0 (macOS) or -1 (Linux)
    state: str  # ps `stat`: it starts with T when stopped (t on Linux: stopped by a tracer)
    tty: str  # the terminal without /dev/: ttys003 (macOS), pts/3 (Linux); ?? or ? without one


def terminal_of(pid: int) -> Terminal:
    """Where `pid` stands at its terminal, from ps. OSError when ps cannot tell, or tells of no such line."""
    found = TERMINAL_LINE.fullmatch(run_ps(["-o", "pid=,pgid=,tpgid=,stat=,tty=", "-p", str(pid)]))
    if found is None or int(found.group(1)) != pid:
        raise OSError("ps told no terminal of process %d" % pid)
    return Terminal(int(found.group(2)), int(found.group(3)), found.group(4), found.group(5))


def terminal_problem(terminal: Terminal, pane_tty: str) -> Optional[str]:
    """Why the process does not own the input of the terminal `pane_tty`; None when it does.

    It owns it when it is not stopped, the terminal is its controlling terminal, and
    its process group is the terminal's foreground group. A `claude` stopped (Ctrl-Z,
    SIGSTOP) or in a background group can run below the pane while another program
    reads the keys.
    """
    if terminal.state[:1] in ("T", "t"):
        return "process_stopped"
    if "/dev/" + terminal.tty != pane_tty or terminal.pgid != terminal.tpgid:
        return "process_background"
    return None


def alive(pid: int) -> bool:
    """A process with this pid exists (one of another user too)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_stamp(found: os.stat_result) -> Tuple[int, int]:
    """What tells one lock directory from a later one at the same name: an inode can be reused, with a new ctime."""
    return found.st_ino, found.st_ctime_ns


class View(NamedTuple):
    """One look at the pane: the pid of its first process, its terminal, its screen, and the cursor as (column, row)."""

    pane_pid: int
    tty: str
    screen: pane_screen.Screen
    cursor: Tuple[int, int]


def enter_problem(screen: pane_screen.Screen, literal: str) -> Optional[str]:
    """Why Enter must not go on this screen; None for an input box that holds exactly `literal`, nothing busy."""
    if not screen.box:
        return "no_input_box"
    if screen.busy:
        return "busy"
    return None if screen.typed == literal else "typed"


def descends(table: Dict[int, int], pid: int, ancestor: int) -> bool:
    """`pid` is `ancestor` or runs below it in the process tree."""
    current = pid
    for _ in range(MAX_ANCESTORS):
        if current == ancestor:
            return True
        if current not in table or table[current] == current:
            return False
        current = table[current]
    return False


class Job:
    """One automatic /clear in one pane, for one generation of a handover."""

    def __init__(self, project_dir: str, settings: Dict[str, Any], document: Dict[str, Any]) -> None:
        self.project_dir = project_dir
        self.settings = settings
        self.pane = document["pane"]
        self.origin = document["session_id"]
        self.generation = document["generation"]
        self.process = document["process"]
        self.hook_pid = document["hook_pid"]
        self.record = auto_clear.new_record(document)
        self.reached = "started"
        self.lock = auto_clear.pane_name(self.pane, ".lock")
        self.lock_made: Optional[Tuple[int, int]] = None
        # The pane's first process and its terminal as check_pane saw them, and the line the job typed that
        # may still be in the input line: set before its keys go out, cleared once Enter or take_back sent it away.
        self.pane_pid: Optional[int] = None
        self.pane_tty: Optional[str] = None
        self.typing: Optional[str] = None

    # --- tmux --------------------------------------------------------------------

    def tmux(self, *args: str) -> Tuple[int, str]:
        """(status, stdout); status -1 when tmux could not run at all."""
        try:
            return auto_clear.run_tmux(list(args))
        except (OSError, subprocess.SubprocessError):
            return -1, ""

    def capture(self) -> Optional[str]:
        status, text = self.tmux("capture-pane", "-p", "-e", "-t", self.pane)
        return text if status == 0 else None

    def keys(self, *keys: str) -> bool:
        return self.tmux("send-keys", "-t", self.pane, *keys)[0] == 0

    # --- the lock ------------------------------------------------------------------

    def take_lock(self) -> bool:
        """Take the pane's lock, its line first; False while another job holds it. OSError when it cannot be had."""
        with rotation_state.project_lock(self.project_dir), rotation_state.storage(
                self.project_dir, create=True) as directory:
            try:
                found: Optional[os.stat_result] = os.lstat(self.lock, dir_fd=directory)
            except FileNotFoundError:
                found = None
            if found is not None:
                if not stat.S_ISDIR(found.st_mode):
                    raise OSError("%s is not a lock directory" % self.lock)
                if time.time() - found.st_mtime < STALE_LOCK_SECONDS:
                    return False
                rotation_state.journal(self.project_dir, "clear_lock_broken", pane=self.pane)
                os.rmdir(self.lock, dir_fd=directory)
            auto_clear.transition(self.project_dir, self.record, "clear_started")
            os.mkdir(self.lock, 0o700, dir_fd=directory)
            self.lock_made = lock_stamp(os.lstat(self.lock, dir_fd=directory))
        return True

    def release_lock(self) -> None:
        """Remove the lock this job made; a lock another job made at that name after it is left alone."""
        try:
            with rotation_state.project_lock(self.project_dir), rotation_state.storage(self.project_dir) as directory:
                if lock_stamp(os.lstat(self.lock, dir_fd=directory)) == self.lock_made:
                    os.rmdir(self.lock, dir_fd=directory)
        except FileNotFoundError:
            return
        except (OSError, rotation_state.LockTimeout) as error:
            rotation_state.log_failure(self.project_dir, "release the lock of pane %s" % self.pane, error)

    # --- the steps -----------------------------------------------------------------

    def run(self) -> None:
        steps: List[Callable[[], Optional[str]]] = [
            self.wait_hook, self.settle, self.recheck, self.check_pane, self.type_clear, self.enter_clear,
            self.wait_ack, self.settle, self.check_pane, self.type_continue, self.enter_continue,
        ]
        for step in steps:
            reason = step()
            if reason:
                self.abort(reason)
                return

    def wait_hook(self) -> Optional[str]:
        """Wait until the Stop hook that started this job has exited: its turn is ending."""
        deadline = monotonic() + SETTLE_SECONDS
        while alive(self.hook_pid):
            if monotonic() >= deadline:
                return "settle"
            sleep(HOOK_POLL_SECONDS)
        return None

    def settle(self) -> Optional[str]:
        """Wait until two captures in a row are the same."""
        deadline = monotonic() + SETTLE_SECONDS
        previous = None
        while True:
            text = self.capture()
            if text is None:
                return "pane_unreadable"
            if text == previous:
                return None
            if monotonic() >= deadline:
                return "settle"
            previous = text
            sleep(SETTLE_POLL_SECONDS)

    def recheck(self) -> Optional[str]:
        """The ready-marker still describes all work of the session, for this generation, pane and process."""
        try:
            marker = rotation_state.read_marker_checked(
                self.project_dir, rotation_state.marker_path(self.project_dir, "ready", self.origin))
            if marker is None:
                return "not_current"
            if marker["generation"] != self.generation:
                return "replaced"
            if marker.get("tmux_pane") != self.pane or marker.get("process") != self.process:
                return "not_current"
            data = rotation_state.read_checked(self.project_dir, marker["handover"])
        except OSError:
            return "marker_unreadable"
        reason = rotation_state.outdated_reason(marker, self.settings, data)
        if reason == transcript_work.UNREADABLE:
            return "transcript_unreadable"
        return "not_current" if reason else None

    def look(self) -> Tuple[Optional[str], Optional[View]]:
        """The pane's state and its screen from one tmux call: (None, view), or (why not, None).

        tmux runs the commands of one call one after the other, without reading the
        pane or a key in between, so the cursor and the screen are of the same moment.
        Measured on tmux 3.7: for a pane that is gone display-message prints an empty
        line, then capture-pane fails with status 1 and ends the call. The line holds
        the eight fields of PANE_FORMAT; the last, `#{pane_tty}`, is the pane's terminal
        as a path below /dev/. Why not: the pane is not the marker's, alive (all eight
        fields, the numbers in digits and the terminal a /dev/ path), in no mode and not
        synchronized.
        """
        status, out = self.tmux("display-message", "-p", "-t", self.pane, PANE_FORMAT, ";",
                                "capture-pane", "-p", "-e", "-t", self.pane)
        if status < 0:
            return "pane_unreadable", None
        info, _, text = out.partition("\n")
        fields = info.split()
        if status != 0 or len(fields) != 8 or fields[0] != self.pane or fields[2] != "0" \
                or not all(DIGITS.fullmatch(field) for field in fields[1:2] + fields[5:7]) \
                or not fields[7].startswith("/dev/"):
            return "pane_gone", None
        if fields[3] != "0":
            return "pane_mode", None
        if fields[4] != "0":
            return "pane_synchronized", None
        return None, View(int(fields[1]), fields[7], pane_screen.read(text, self.settings),
                          (int(fields[5]), int(fields[6])))

    def check_pane(self) -> Optional[str]:
        """The pane is the marker's and idle, and the session's `claude` runs in it and owns its terminal's input."""
        problem, view = self.look()
        if view is None:
            return problem
        self.pane_pid, self.pane_tty = view.pane_pid, view.tty
        return pane_screen.idle_problem(view.screen) or self.check_process(view.pane_pid, view.tty)

    def fresh_look(self) -> Tuple[Optional[str], Optional[View]]:
        """The read a key is decided on: the session's `claude` still runs below the pane and owns its input; one look.

        The look is the last read: what follows is decided on it alone and sent at
        once. (why not, None) when the process or the pane fails its check, also when
        the pane's first process or its terminal is no longer the one check_pane saw.
        """
        problem = self.check_process(self.pane_pid, self.pane_tty)
        if problem:
            return problem, None
        problem, view = self.look()
        if view is not None and (view.pane_pid, view.tty) != (self.pane_pid, self.pane_tty):
            return "process", None
        return problem, view

    def check_process(self, pane_pid: int, pane_tty: str) -> Optional[str]:
        """The session's `claude` runs below the pane's first process and owns the input of the pane's terminal.

        Running below the pane is not enough: stopped, or in a background group, it
        leaves the keys to another program in that terminal (terminal_problem).
        """
        pid = self.process["pid"]
        try:
            if rotation_state.process_start_checked(pid) != self.process["start"]:
                return "process"
            if not descends(process_table(), pid, pane_pid):
                return "process"
            terminal = terminal_of(pid)
        except OSError:
            return "process_unreadable"
        return terminal_problem(terminal, pane_tty)

    def type_line(self, literal: str) -> Optional[str]:
        """Type `literal` and read it back until the input line holds exactly that. No other key goes here.

        A line that reads otherwise (more text, other text, part of it, no line at
        all) may hold what the user typed, so nothing removes it: the job stops and
        leaves it as it is.
        """
        self.typing = literal
        if not self.keys("-l", literal):
            return "send"
        deadline = monotonic() + VERIFY_SECONDS
        while True:
            text = self.capture()
            if text is None:
                return "pane_unreadable"
            typed = pane_screen.read(text, self.settings).typed
            if typed == literal:
                return None
            if (typed is not None and not literal.startswith(typed)) or monotonic() >= deadline:
                return "typed"
            sleep(VERIFY_POLL_SECONDS)

    def type_clear(self) -> Optional[str]:
        return self.type_line(CLEAR)

    def type_continue(self) -> Optional[str]:
        return self.type_line(CONTINUE)

    def enter(self, literal: str, event: str, step: str, handover: bool = False) -> Optional[str]:
        """The step's line and record, for /clear the marker again, then a fresh look, then Enter as a call of its own.

        The line and the record come before the key because the fresh session's
        SessionStart runs as soon as /clear is entered and reads the record to tell a
        clear the job typed from one the user typed (auto_clear.typed_by_job): it must
        already say "entered". That write can wait up to rotation_state.LOCK_TIMEOUT_SECONDS
        for the project lock, and a prompt in that wait can make the handover outdated:
        new work after the marker, or a newer handover with a generation of its own.
        SessionStart would then refuse it, and /clear would empty the context with no
        successor to take the handover. So for /clear (`handover`) the marker is read
        again after the write, as recheck reads it; when it fails, no Enter goes and
        take_back decides on a fresh look whether the typed line is removed. The
        continuation needs no such read: the successor has taken the generation by then.
        The look Enter is decided on comes last, and nothing comes between that look and
        the key: no lock, no sleep, no other read.
        """
        try:
            auto_clear.transition(self.project_dir, self.record, event, step=step)
        except FAILURES as error:
            rotation_state.log_failure(self.project_dir, "record %s in pane %s" % (step, self.pane), error)
            return self.take_back(literal, "journal", self.fresh_look()[1])
        reason = self.recheck() if handover else None
        if reason:
            return self.take_back(literal, reason, self.fresh_look()[1])
        problem, view = self.fresh_look()
        if view is None:
            return problem
        problem = enter_problem(view.screen, literal)
        if problem:
            return self.take_back(literal, problem, view)
        if not self.keys("Enter"):
            return "send"
        self.typing = None
        return None

    def take_back(self, literal: str, reason: str, view: Optional[View]) -> str:
        """Remove `literal` from the input line when the line is certainly the job's own; `reason` either way.

        `view` is the look just taken (fresh_look), None when the process or the pane
        failed it. One BSpace per character goes, at once, only when that look shows
        exactly `literal` in the input line with the cursor right after it: then every
        character they remove is one the job typed. Any other line (more text, other
        text, the cursor elsewhere, no line that could be read) gets no key at all and
        stays as it is, which the user's lines then say (abort).
        """
        screen = view.screen if view is not None else None
        if screen is not None and screen.typed == literal and view.cursor == (screen.end, screen.row):
            if self.keys(*["BSpace"] * len(literal)):
                self.typing = None
        return reason

    def enter_clear(self) -> Optional[str]:
        reason = self.enter(CLEAR, "clear_entered", "entered", handover=True)
        if reason is None:
            self.reached = "entered"
        return reason

    def enter_continue(self) -> Optional[str]:
        return self.enter(CONTINUE, "clear_continued", "continued")

    def wait_ack(self) -> Optional[str]:
        """Wait for the successor's line: the session that took this generation after /clear."""
        deadline = monotonic() + min(self.settings["ack_timeout_seconds"], MAX_ACK_SECONDS)
        while True:
            try:
                successor = auto_clear.find_ack(self.project_dir, self.origin, self.generation)
            except OSError:
                return "ack_unreadable"
            if successor is not None:
                return self.acknowledge(successor)
            if monotonic() >= deadline:
                return "no_ack"
            sleep(ACK_POLL_SECONDS)

    def acknowledge(self, successor: str) -> Optional[str]:
        try:
            auto_clear.transition(self.project_dir, self.record, "clear_acknowledged", step="acknowledged",
                                  successor=successor)
        except FAILURES as error:
            rotation_state.log_failure(self.project_dir, "record the successor in pane %s" % self.pane, error)
            return "journal"
        self.reached = "acknowledged"
        return None

    def abort(self, reason: str) -> None:
        """Stop: the reason's line and record, then the user's line in the pane. Nothing more is typed.

        Both say whether what the job typed may still be in the input line (left_in_line).
        """
        left = self.typing is not None
        try:
            auto_clear.transition(self.project_dir, self.record, "clear_aborted", step="aborted",
                                  at_step=self.reached, reason=reason, left_in_line=left)
        except FAILURES as error:
            rotation_state.log_failure(self.project_dir, "record the stopped clear in pane %s" % self.pane, error)
        auto_clear.display(self.pane, auto_clear.display_text(self.reached, reason, left))


def open_job(pane: str, origin: str, generation: str, job_id: str) -> Optional[Job]:
    """The job its file describes, for the ids it was started with; None (the user told why) when there is none."""
    try:
        document = auto_clear.take_job(job_id)
    except OSError:
        document = None
    if document is None or (document["pane"], document["session_id"], document["generation"]) != (
            pane, origin, generation):
        auto_clear.display(pane, auto_clear.display_text("started", "job"))
        return None
    project_dir = document["project_dir"]
    settings = context_budget.load_settings(project_dir)
    if not (settings["enabled"] and settings["auto_clear"]):
        return None
    if rotation_state.storage_problem(project_dir):
        auto_clear.display(pane, auto_clear.display_text("started", "storage"))
        return None
    return Job(project_dir, settings, document)


def run_job(job: Job) -> None:
    """Lock, run, unlock. An unexpected error stops the job with its own reason: never a half-typed pane in silence."""
    try:
        taken = job.take_lock()
    except FAILURES as error:
        rotation_state.log_failure(job.project_dir, "lock pane %s" % job.pane, error)
        auto_clear.display(job.pane, auto_clear.display_text("started", "lock"))
        return
    if not taken:
        return
    try:
        job.run()
    except Exception as error:
        rotation_state.log_failure(job.project_dir, "automatic clear in pane %s" % job.pane, error)
        job.abort("error")
    finally:
        job.release_lock()


def main(argv: Sequence[str]) -> int:
    parsed = parse_args(argv)
    if parsed is None:
        auto_clear.display(None, REFUSED)
        return 1
    job = open_job(*parsed)
    if job is not None:
        run_job(job)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
