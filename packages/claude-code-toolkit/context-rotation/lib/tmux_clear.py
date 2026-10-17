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
   marker) runs in the process tree of `#{pane_pid}`;
5. `/clear` is typed with `send-keys -l` and read back from the input line. Enter
   goes as a separate call only when the line reads exactly `/clear`; otherwise six
   BSpace remove it and the job stops;
6. it waits up to ack_timeout_seconds (at most MAX_ACK_SECONDS) for the successor's
   `injected` line via `clear` for this generation in injections.jsonl;
7. after the turn settles again and the pane checks pass, `Continue from the handover
   in your context, step 1.` is typed, read back, and Enter sent separately.

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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import auto_clear
import context_budget
import pane_screen
import rotation_state
import state_fields
import transcript_work

CLEAR = "/clear"
CONTINUE = "Continue from the handover in your context, step 1."
STALE_LOCK_SECONDS = 300
# The lock must stay fresh for the whole job: the waits below add up to well under STALE_LOCK_SECONDS.
MAX_ACK_SECONDS = 200
SETTLE_SECONDS = 15.0
SETTLE_POLL_SECONDS = 0.5
HOOK_POLL_SECONDS = 0.1
VERIFY_SECONDS = 3.0
VERIFY_POLL_SECONDS = 0.1
ACK_POLL_SECONDS = 0.25
PANE_FORMAT = "#{pane_id} #{pane_pid} #{pane_dead} #{pane_in_mode} #{pane_synchronized}"
MAX_ANCESTORS = 256
DIGITS = re.compile(r"[0-9]+")
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


def process_table() -> Dict[int, int]:
    """{pid: parent pid} of every process, from ps. OSError when ps cannot tell."""
    try:
        done = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True,
                              env=dict(os.environ, LC_ALL="C"), timeout=rotation_state.PS_TIMEOUT_SECONDS)
    except subprocess.SubprocessError as error:
        raise OSError("ps did not answer: %s" % error)
    if done.returncode != 0:
        raise OSError("ps exited with status %d" % done.returncode)
    table = {}
    for line in done.stdout.decode("ascii", "replace").splitlines():
        fields = line.split()
        if len(fields) == 2 and all(DIGITS.fullmatch(field) for field in fields):
            table[int(fields[0])] = int(fields[1])
    if not table:
        raise OSError("ps listed no process")
    return table


def alive(pid: int) -> bool:
    """A process with this pid exists (one of another user too)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
        return True

    def release_lock(self) -> None:
        try:
            with rotation_state.project_lock(self.project_dir), rotation_state.storage(self.project_dir) as directory:
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

    def check_pane(self) -> Optional[str]:
        """The pane is the marker's and idle, and the session's `claude` runs in it."""
        status, out = self.tmux("display-message", "-p", "-t", self.pane, PANE_FORMAT)
        if status < 0:
            return "pane_unreadable"
        fields = out.split()
        if status != 0 or len(fields) != 5 or fields[0] != self.pane or not DIGITS.fullmatch(fields[1]) \
                or fields[2] != "0":
            return "pane_gone"
        if fields[3] != "0":
            return "pane_mode"
        if fields[4] != "0":
            return "pane_synchronized"
        text = self.capture()
        if text is None:
            return "pane_unreadable"
        return pane_screen.idle_problem(pane_screen.read(text, self.settings)) or self.check_process(int(fields[1]))

    def check_process(self, pane_pid: int) -> Optional[str]:
        pid = self.process["pid"]
        try:
            if rotation_state.process_start_checked(pid) != self.process["start"]:
                return "process"
            table = process_table()
        except OSError:
            return "process_unreadable"
        return None if descends(table, pid, pane_pid) else "process"

    def type_line(self, literal: str) -> Optional[str]:
        """Type `literal` and read it back from the input line; on any mismatch remove it again.

        Enter is never sent here. The line is removed with one BSpace per character
        typed, never with a key that clears more than that.
        """
        if not self.keys("-l", literal):
            return "send"
        deadline = monotonic() + VERIFY_SECONDS
        while True:
            text = self.capture()
            typed = pane_screen.read(text, self.settings).typed if text is not None else None
            if typed == literal:
                return None
            wrong = typed is not None and not literal.startswith(typed)
            if text is None or wrong or monotonic() >= deadline:
                self.keys(*["BSpace"] * len(literal))
                return "pane_unreadable" if text is None else "typed"
            sleep(VERIFY_POLL_SECONDS)

    def type_clear(self) -> Optional[str]:
        return self.type_line(CLEAR)

    def type_continue(self) -> Optional[str]:
        return self.type_line(CONTINUE)

    def enter(self, literal: str, event: str, step: str) -> Optional[str]:
        """The step's line and record first, then Enter as a call of its own."""
        try:
            auto_clear.transition(self.project_dir, self.record, event, step=step)
        except FAILURES as error:
            rotation_state.log_failure(self.project_dir, "record %s in pane %s" % (step, self.pane), error)
            self.keys(*["BSpace"] * len(literal))
            return "journal"
        return None if self.keys("Enter") else "send"

    def enter_clear(self) -> Optional[str]:
        reason = self.enter(CLEAR, "clear_entered", "entered")
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
        """Stop: the reason's line and record, then the user's line in the pane. Nothing more is typed."""
        try:
            auto_clear.transition(self.project_dir, self.record, "clear_aborted", step="aborted",
                                  at_step=self.reached, reason=reason)
        except FAILURES as error:
            rotation_state.log_failure(self.project_dir, "record the stopped clear in pane %s" % self.pane, error)
        auto_clear.display(self.pane, auto_clear.display_text(self.reached, reason))


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
