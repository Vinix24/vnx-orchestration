"""The job of the automatic /clear (tmux_clear), in-process: tmux is a recorder that plays an A0 capture.

Every job here was started by the real Stop hook, which wrote its job file and asked
the (fake) tmux binary to run it; the test then runs that job with the arguments of
that run-shell line.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

import pytest

from conftest import (
    HOOKS, PANE_EXPECTED, VALID_HANDOVER, FakeTmux, Session, b1_entry, clear_events, event, journal_lines, message_of,
    pane_text, rotation, settings_file, start, started,
)

import auto_clear
import rotation_state
import state_fields
import tmux_clear
import transcript_work

CLEAR = ["-l", "/clear"]
CONTINUE = ["-l", tmux_clear.CONTINUE]
SIX = ["BSpace"] * 6


@pytest.fixture(autouse=True)
def fast(clock):
    return clock


def successor(session, results, session_id="sess-b"):
    """On Enter after /clear: the SessionStart of the fresh session, in the same claude process and pane."""
    def on_enter(submitted):
        if submitted == "/clear":
            results.append(start(session.project, session_id, source="clear", pid=session.pid))
    return on_enter


def record(project):
    return json.loads((rotation(project) / "clear-7.json").read_text(encoding="utf-8"))


def stopped(project, fake, reason, reached="started"):
    """The job stopped for `reason` after `reached`: its line, its record and the user's line in the pane."""
    assert clear_events(project)[-1] == "clear_aborted"
    assert journal_lines(project)[-1]["abort_reason"] == reason
    found = record(project)
    assert (found["step"], found["at_step"], found["reason"], found["told"]) == ("aborted", reached, reason, False)
    assert fake.shown() == [auto_clear.display_text(reached, reason)]
    assert not (rotation(project) / "clear-7.lock").exists()


# --- the whole way ---------------------------------------------------------------

def test_the_job_clears_and_continues_in_the_fresh_session(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    results = []
    fake.on_enter.append(successor(session, results))
    assert tmux_clear.main(args) == 0
    assert fake.sent() == [CLEAR, ["Enter"], CONTINUE, ["Enter"]]
    assert fake.shown() == []
    assert clear_events(project) == ["clear_started", "clear_entered", "clear_acknowledged", "clear_continued"]
    events = [line["event"] for line in journal_lines(project)]
    assert (events.index("clear_entered") < events.index("injected") < events.index("delivered")
            < events.index("clear_acknowledged"))
    found = record(project)
    assert (found["step"], found["successor"], found["origin_session_id"]) == ("continued", "sess-b", "sess-a")
    assert not (rotation(project) / "clear-7.lock").exists()
    (proc,) = results
    assert VALID_HANDOVER in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_the_first_automatic_clear_says_how_to_turn_it_off_and_the_next_does_not(
        tmp_path, session, fake, tmux_bin, project):
    results = []
    fake.on_enter.append(successor(session, results))
    tmux_clear.main(started(session, tmux_bin))
    assert auto_clear.OFF_HINT in message_of(results[0])
    nxt = Session(tmp_path, project, "sess-b", pid=session.pid)
    fake.on_enter[:] = [successor(session, results, "sess-c")]
    tmux_clear.main(started(nxt, tmux_bin))
    assert message_of(results[1]).startswith("context-rotation: continued from")
    assert auto_clear.OFF_HINT not in message_of(results[1])


def test_a_clear_the_user_typed_gets_no_hint(session, project, tmux_bin):
    session.write_handover()
    session.finish_turn()
    proc = start(project, "sess-b", source="clear", pid=session.pid)
    assert auto_clear.OFF_HINT not in message_of(proc)


# --- every A0 capture: typed or not typed -------------------------------------------

@pytest.mark.parametrize("name", sorted(PANE_EXPECTED))
def test_each_capture_decides_whether_clear_is_typed(name, session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    fake.base = FakeTmux(screen=name).base
    tmux_clear.main(args)
    reason = PANE_EXPECTED[name]
    if reason is None:
        assert fake.sent()[:2] == [CLEAR, ["Enter"]]
    else:
        assert fake.sent() == []
        stopped(project, fake, reason)


# --- verify before Enter, and never remove what is not the job's own ------------------

def left_alone(session, project, fake, reason, reached="started"):
    """Stopped for `reason` with the input line left as it is: the record, its line and both user lines say so."""
    assert clear_events(project)[-1] == "clear_aborted"
    assert journal_lines(project)[-1]["left_in_line"] is True
    found = record(project)
    assert (found["step"], found["at_step"], found["reason"], found["left_in_line"]) == (
        "aborted", reached, reason, True)
    (shown,) = fake.shown()
    assert shown == auto_clear.display_text(reached, reason, True) and shown.endswith(auto_clear.LEFT)
    assert auto_clear.LEFT in message_of(session.prompt("my own prompt"))


def fail_journal_at(monkeypatch, wanted, then=None):
    """The journal line of event `wanted` fails; `then(fake)` runs first, while the project lock is held."""
    real = rotation_state.journal

    def journal(project_dir, event, **fields):
        if event == wanted:
            if then:
                then()
            raise PermissionError(13, "Permission denied")
        return real(project_dir, event, **fields)

    monkeypatch.setattr(rotation_state, "journal", journal)


def during_journal_of(monkeypatch, wanted, change):
    """`change()` runs right after the line of event `wanted` is written, with the project lock still held."""
    real = rotation_state.journal

    def journal(project_dir, event, **fields):
        real(project_dir, event, **fields)
        if event == wanted:
            change()

    monkeypatch.setattr(rotation_state, "journal", journal)


def test_text_the_user_adds_after_the_typed_clear_is_never_removed(session, fake, tmux_bin, project):
    """Codex on 450a7683: the user appends hello after /clear; the cleanup took hello and the r, leaving /clea."""
    args = started(session, tmux_bin)

    def user_appends(tmux):
        if tmux.typed == "/clear":
            tmux.typed = "/clearhello"

    fake.on_capture = user_appends
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR]
    assert fake.typed == "/clearhello"
    assert fake.submitted == []
    left_alone(session, project, fake, "typed")


def test_text_the_user_typed_before_the_clear_is_left_as_it_is(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)

    def user_types(tmux):
        if tmux.typed == "/clear":
            tmux.typed = "x/clear"

    fake.on_capture = user_types
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR]
    assert fake.typed == "x/clear"
    left_alone(session, project, fake, "typed")


def test_a_line_that_never_reads_back_gets_no_key(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    fake.echo = False  # the keys may land later, but the input line never shows them
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR]
    assert "clear_entered" not in clear_events(project)
    left_alone(session, project, fake, "typed")


def test_a_line_that_reads_back_only_in_part_gets_no_key(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)

    def in_part(tmux):
        if CLEAR in tmux.sent() and tmux.typed == "":
            tmux.typed = "/cl"

    fake.echo = False
    fake.on_capture = in_part
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR]
    assert fake.typed == "/cl"
    left_alone(session, project, fake, "typed")


def test_a_pane_that_cannot_be_read_after_typing_gets_no_key(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)

    def gone(tmux):
        if tmux.typed == "/clear":
            tmux.fail["capture-pane"] = OSError("the pane went away")

    fake.on_capture = gone
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR]
    assert fake.typed == "/clear"
    left_alone(session, project, fake, "pane_unreadable")


def test_a_cursor_moved_off_the_end_gets_no_key(session, fake, tmux_bin, project, monkeypatch):
    """The line reads exactly /clear, but the cursor is not after it: BSpace would leave part of it behind."""
    args = started(session, tmux_bin)
    fail_journal_at(monkeypatch, "clear_entered", lambda: setattr(fake, "back", 2))
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR]
    assert fake.typed == "/clear"
    left_alone(session, project, fake, "journal")


def test_text_added_while_the_journal_line_failed_is_left_as_it_is(session, fake, tmux_bin, project, monkeypatch):
    args = started(session, tmux_bin)
    fail_journal_at(monkeypatch, "clear_entered", lambda: setattr(fake, "typed", fake.typed + " and more"))
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR]
    assert fake.typed == "/clear and more"
    left_alone(session, project, fake, "journal")


def test_a_journal_line_that_cannot_be_written_sends_no_enter(session, fake, tmux_bin, project, monkeypatch):
    """The line is exactly the job's /clear with the cursor after it: six BSpace remove only what it typed."""
    args = started(session, tmux_bin)
    fail_journal_at(monkeypatch, "clear_entered")
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, SIX]
    assert fake.typed == "" and fake.submitted == []
    stopped(project, fake, "journal")
    assert record(project)["left_in_line"] is False and auto_clear.LEFT not in fake.shown()[0]


def test_the_continuation_line_is_verified_too(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    results = []
    fake.on_enter.append(successor(session, results))

    def silent_after_clear(submitted):
        if submitted == "/clear":
            fake.echo = False

    fake.on_enter.append(silent_after_clear)
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, ["Enter"], CONTINUE]
    left_alone(session, project, fake, "typed", "acknowledged")


# --- Enter goes on a look taken after the journal line ------------------------------------

def test_a_line_replaced_during_the_journal_write_gets_no_enter(session, fake, tmux_bin, project, monkeypatch):
    """Codex on 450a7683: the input is replaced while the line is written; Enter submitted the user's draft."""
    args = started(session, tmux_bin)
    during_journal_of(monkeypatch, "clear_entered", lambda: setattr(fake, "typed", "my draft"))
    tmux_clear.main(args)
    assert fake.submitted == []
    assert fake.sent() == [CLEAR]
    assert fake.typed == "my draft"
    left_alone(session, project, fake, "typed")


def test_a_session_busy_after_the_journal_write_gets_no_enter(session, fake, tmux_bin, project, monkeypatch):
    args = started(session, tmux_bin)
    during_journal_of(monkeypatch, "clear_entered", lambda: setattr(fake, "base", pane_text("03-busy-auto.esc.txt")))
    tmux_clear.main(args)
    assert fake.submitted == []
    assert fake.sent() == [CLEAR, SIX], "the line was exactly the job's /clear, so it was taken back"
    stopped(project, fake, "busy")


def test_claude_gone_after_the_journal_write_gets_no_enter(session, fake, tmux_bin, project, monkeypatch):
    args = started(session, tmux_bin)
    during_journal_of(monkeypatch, "clear_entered",
                      lambda: monkeypatch.setattr(rotation_state, "process_start_checked", lambda pid: None))
    tmux_clear.main(args)
    assert fake.submitted == []
    assert fake.sent() == [CLEAR]
    found = record(project)
    assert (found["reason"], found["left_in_line"]) == ("process", True)
    assert fake.shown() == [auto_clear.display_text("started", "process", True)]


def test_a_continuation_replaced_during_its_journal_write_gets_no_enter(session, fake, tmux_bin, project,
                                                                         monkeypatch):
    args = started(session, tmux_bin)
    results = []
    fake.on_enter.append(successor(session, results))
    during_journal_of(monkeypatch, "clear_continued", lambda: setattr(fake, "typed", "my draft"))
    tmux_clear.main(args)
    assert fake.submitted == ["/clear"]
    assert fake.sent() == [CLEAR, ["Enter"], CONTINUE]
    assert fake.typed == "my draft"
    found = record(project)
    assert (found["at_step"], found["reason"], found["left_in_line"]) == ("acknowledged", "typed", True)


def test_each_enter_follows_its_line_and_a_look_with_nothing_in_between(session, fake, tmux_bin, project,
                                                                         monkeypatch):
    """The last call before each Enter is a capture of the pane, taken after the step's journal line."""
    args = started(session, tmux_bin)
    results = []
    fake.on_enter.append(successor(session, results))

    def noted(name, real):
        def call(*args, **kwargs):
            fake.calls.append([name] + [arg for arg in args[1:2] if name == "journal"])
            return real(*args, **kwargs)
        return call

    monkeypatch.setattr(rotation_state, "journal", noted("journal", rotation_state.journal))
    monkeypatch.setattr(tmux_clear, "sleep", noted("sleep", tmux_clear.sleep))
    monkeypatch.setattr(tmux_clear, "process_table", noted("ps", tmux_clear.process_table))
    monkeypatch.setattr(rotation_state, "process_start_checked", noted("ps", rotation_state.process_start_checked))
    tmux_clear.main(args)
    enters = [number for number, call in enumerate(fake.calls) if call == ["send-keys", "-t", "%7", "Enter"]]
    assert len(enters) == 2 and fake.submitted == ["/clear", tmux_clear.CONTINUE]
    for number, event in zip(enters, ("clear_entered", "clear_continued")):
        before = fake.calls[:number]
        assert "capture-pane" in before[-1], "the call right before Enter is not a look at the pane"
        assert before.index(["journal", event]) < number - 1, "the look is older than the journal line"


# --- the successor's acknowledgement ---------------------------------------------------

def test_no_ack_within_the_timeout_types_nothing_more(session, fake, tmux_bin, project, clock):
    settings_file(project, ack_timeout_seconds=5)
    args = started(session, tmux_bin)
    begin = clock.now
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, ["Enter"]]
    stopped(project, fake, "no_ack", "entered")
    assert 5 <= clock.now - begin < 30


def test_the_ack_wait_is_capped_below_the_lock_age(session, fake, tmux_bin, project, clock):
    settings_file(project, ack_timeout_seconds=100000)
    args = started(session, tmux_bin)
    begin = clock.now
    tmux_clear.main(args)
    assert tmux_clear.MAX_ACK_SECONDS <= clock.now - begin < tmux_clear.STALE_LOCK_SECONDS


def injected_line(session, project, generation):
    rotation_state.journal(str(project), "injected", via="clear", origin_session_id="sess-a",
                           generation=generation, session_id="sess-b", handover=str(session.handover),
                           archive=str(rotation(project) / "archive" / "20261017T000000000000Z-sess-a.md"),
                           sha256="0" * 64, chars=1, mode="inline")


def test_a_delivery_of_another_generation_is_no_ack(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)

    def other_generation(submitted):
        injected_line(session, project, "0" * 16)
        rotation_state.journal(str(project), "delivered", via="clear", origin_session_id="sess-a",
                               generation="0" * 16, session_id="sess-b")

    fake.on_enter.append(other_generation)
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, ["Enter"]]
    stopped(project, fake, "no_ack", "entered")


def test_an_injected_line_without_its_delivery_is_no_ack(session, fake, tmux_bin, project):
    """The `injected` line is written before the emit: it says a delivery began, not that it finished."""
    args = started(session, tmux_bin)
    generation = json.loads(session.marker("ready").read_text())["generation"]
    fake.on_enter.append(lambda submitted: injected_line(session, project, generation))
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, ["Enter"]]
    stopped(project, fake, "no_ack", "entered")


def broken_successor(session, results, session_id="sess-b"):
    """On Enter after /clear: the fresh session's SessionStart, with nobody reading its output any more.

    Its stdout is a pipe whose read end is closed before the hook starts, so the emit
    of the handover fails with EPIPE (BrokenPipeError) after the `injected` line.
    """
    def on_enter(submitted):
        if submitted != "/clear":
            return
        project = session.project
        payload = event("17-SessionStart-clear", project, project / "none.jsonl", session_id, source="clear")
        proc = subprocess.Popen([sys.executable, str(HOOKS / "session_start.py")], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(os.environ, CLAUDE_PID=str(session.pid)))
        proc.stdout.close()
        proc.stdout = None
        _, err = proc.communicate(json.dumps(payload), timeout=60)
        results.append((proc.returncode, err))
    return on_enter


def test_a_handover_whose_emit_failed_gets_no_continuation(session, fake, tmux_bin, project):
    """Codex on 450a7683: the emit raises BrokenPipeError, the claim stays, and the job still typed the line."""
    args = started(session, tmux_bin)
    results = []
    fake.on_enter.append(broken_successor(session, results))
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, ["Enter"]]
    assert fake.submitted == ["/clear"]
    ((status, err),) = results
    # 1 from the hook, or 120 when Python cannot flush stdout at exit either; never 2, which would block.
    assert status in (1, 120) and "emit handover failed: BrokenPipeError" in err
    events = [line["event"] for line in journal_lines(project)]
    assert "injected" in events and "delivered" not in events
    assert list(rotation(project).glob("claimed-sess-a-*.json")), "the claim that did not finish stays for resume"
    stopped(project, fake, "no_ack", "entered")
    assert "/context-rotation:rotate resume" in fake.shown()[0]


# --- the marker is read again ---------------------------------------------------------

def test_a_prompt_after_the_stop_makes_the_job_type_nothing(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    session.prompt("One more thing.")
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "not_current")


def test_work_in_the_transcript_after_the_stop_makes_the_job_type_nothing(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    tool_id = session.tool_id()
    session.append(b1_entry("tool_use_bash", tool_use_id=tool_id, command="make test"))
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "not_current")


def test_a_changed_handover_makes_the_job_type_nothing(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    session.handover.write_text(VALID_HANDOVER + "\nchanged\n", encoding="utf-8")
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "not_current")


def test_a_newer_generation_makes_the_older_job_type_nothing(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    session.write_handover(VALID_HANDOVER + "\nNewer.\n")
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "replaced")


def test_a_marker_of_another_pane_makes_the_job_type_nothing(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    marker = json.loads(session.marker("ready").read_text())
    marker["tmux_pane"] = "%8"
    session.marker("ready").write_text(json.dumps(marker))
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "not_current")


# --- and once more right before Enter ----------------------------------------------------------

def after_first_recheck(monkeypatch, change):
    """`change()` runs once, right after the job's first read of the marker (Job.recheck) found it current."""
    real = tmux_clear.Job.recheck
    found = []

    def recheck(job):
        reason = real(job)
        if not found:
            found.append(reason)
            change()
        return reason

    monkeypatch.setattr(tmux_clear.Job, "recheck", recheck)
    return found


def after_record_of(monkeypatch, wanted, change):
    """`change()` runs right after the line and record of event `wanted` are written, the project lock released."""
    real = auto_clear.transition

    def transition(project_dir, record, event, **changes):
        real(project_dir, record, event, **changes)
        if event == wanted:
            change()

    monkeypatch.setattr(auto_clear, "transition", transition)


def outdate(session, change):
    if change == "prompt":
        assert session.prompt("One more thing after the handover.").returncode == 0
        assert not session.marker("ready").exists()
    else:
        session.write_handover(VALID_HANDOVER + "\nNewer.\n")


@pytest.mark.parametrize("change, reason", [("prompt", "not_current"), ("newer handover", "replaced")])
@pytest.mark.parametrize("window", ["after the first read of the marker", "after the journal line of Enter"])
def test_a_handover_outdated_before_enter_gets_no_enter(window, change, reason, session, fake, tmux_bin, project,
                                                       monkeypatch):
    """Codex on e51a3929: a prompt made the handover outdated after the job read the marker; /clear still went.

    SessionStart then refuses the handover, and the context is gone without a successor.
    """
    args = started(session, tmux_bin)
    first = [None]
    if window == "after the first read of the marker":
        first = after_first_recheck(monkeypatch, lambda: outdate(session, change))
    else:
        after_record_of(monkeypatch, "clear_entered", lambda: outdate(session, change))
    tmux_clear.main(args)
    assert first == [None], "the first read of the marker found it current"
    assert fake.submitted == []
    assert fake.sent() == [CLEAR, SIX], "the line was exactly the job's /clear, so it was taken back"
    stopped(project, fake, reason)
    assert record(project)["left_in_line"] is False


def test_a_handover_outdated_while_the_line_holds_more_is_left_as_it_is(session, fake, tmux_bin, project,
                                                                         monkeypatch):
    """The recheck fails and the user has added text: no Enter, and no BSpace either."""
    args = started(session, tmux_bin)

    def prompt_and_type():
        outdate(session, "prompt")
        fake.typed += " and more"

    after_record_of(monkeypatch, "clear_entered", prompt_and_type)
    tmux_clear.main(args)
    assert fake.submitted == [] and fake.sent() == [CLEAR]
    assert fake.typed == "/clear and more"
    left_alone(session, project, fake, "not_current")


def test_the_marker_is_read_again_between_the_journal_line_and_the_look(session, fake, tmux_bin, project,
                                                                        monkeypatch):
    """For /clear: its journal line, the marker read again, then the look Enter goes on. The continuation reads none."""
    args = started(session, tmux_bin)
    results = []
    fake.on_enter.append(successor(session, results))
    real_journal, real_marker = rotation_state.journal, rotation_state.read_marker_checked

    def journal(project_dir, event, **fields):
        fake.calls.append(["journal", event])
        return real_journal(project_dir, event, **fields)

    def read_marker(*args, **kwargs):
        fake.calls.append(["marker"])
        return real_marker(*args, **kwargs)

    monkeypatch.setattr(rotation_state, "journal", journal)
    monkeypatch.setattr(rotation_state, "read_marker_checked", read_marker)
    tmux_clear.main(args)
    assert fake.submitted == ["/clear", tmux_clear.CONTINUE]
    calls = fake.calls
    clear_enter, continue_enter = [number for number, call in enumerate(calls)
                                   if call == ["send-keys", "-t", "%7", "Enter"]]
    last_read = max(number for number, call in enumerate(calls[:clear_enter]) if call == ["marker"])
    assert calls.index(["journal", "clear_entered"]) < last_read < clear_enter - 1
    assert "capture-pane" in calls[clear_enter - 1]
    assert ["marker"] not in calls[calls.index(["journal", "clear_continued"]):continue_enter]


# --- a read that fails decides nothing ---------------------------------------------------

def denied(*args, **kwargs):
    raise PermissionError(13, "Permission denied")


@pytest.mark.parametrize("target, reason", [
    ((rotation_state, "read_marker_checked"), "marker_unreadable"),
    ((rotation_state, "read_checked"), "marker_unreadable"),
    ((transcript_work, "position_holds"), "transcript_unreadable"),
    ((tmux_clear, "process_table"), "process_unreadable"),
    ((rotation_state, "process_start_checked"), "process_unreadable"),
    ((tmux_clear, "terminal_of"), "process_unreadable"),
])
def test_a_failed_read_before_typing_types_nothing(target, reason, session, fake, tmux_bin, project, monkeypatch):
    args = started(session, tmux_bin)
    monkeypatch.setattr(*target, denied)
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, reason)


@pytest.mark.parametrize("command", ["capture-pane", "pane-info"])
def test_a_pane_that_cannot_be_read_types_nothing(command, session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    fake.fail[command] = OSError("no server")
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "pane_unreadable")


def test_a_lock_that_cannot_be_read_types_nothing(session, fake, tmux_bin, project, monkeypatch):
    args = started(session, tmux_bin)
    real = os.lstat

    def lstat(path, *rest, **kwargs):
        if path == "clear-7.lock":
            raise PermissionError(13, "Permission denied")
        return real(path, *rest, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    tmux_clear.main(args)
    assert fake.sent() == []
    assert fake.shown() == [auto_clear.display_text("started", "lock")]
    assert "clear_started" not in clear_events(project)


def test_an_ack_that_cannot_be_read_types_nothing_more(session, fake, tmux_bin, project, monkeypatch):
    args = started(session, tmux_bin)
    monkeypatch.setattr(rotation_state, "journal_checked", denied)
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, ["Enter"]]
    stopped(project, fake, "ack_unreadable", "entered")


def test_a_journal_line_that_cannot_be_written_sends_no_enter(session, fake, tmux_bin, project, monkeypatch):
    args = started(session, tmux_bin)
    real = rotation_state.journal

    def journal(project_dir, event, **fields):
        if event == "clear_entered":
            raise PermissionError(13, "Permission denied")
        return real(project_dir, event, **fields)

    monkeypatch.setattr(rotation_state, "journal", journal)
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, SIX]
    stopped(project, fake, "journal")


def test_an_unexpected_error_stops_the_job_with_its_reason(session, fake, tmux_bin, project, monkeypatch):
    args = started(session, tmux_bin)

    def broken(*args, **kwargs):
        raise KeyError("broken")

    monkeypatch.setattr(tmux_clear.pane_screen, "read", broken)
    assert tmux_clear.main(args) == 0
    assert fake.sent() == []
    stopped(project, fake, "error")
    assert any("automatic clear in pane %7 failed: KeyError" in line for line in rotation_state.error_lines(str(project)))


# --- the pane and the process ---------------------------------------------------------------

@pytest.mark.parametrize("fields, reason", [
    ({"pane_in_mode": "1"}, "pane_mode"),
    ({"pane_synchronized": "1"}, "pane_synchronized"),
    ({"pane_dead": "1"}, "pane_gone"),
    ({"pane_id": "%8"}, "pane_gone"),
    ({"pane_id": ""}, "pane_gone"),  # what tmux prints for a pane that is gone (measured, tmux 3.7)
    ({"pane_pid": "x"}, "pane_gone"),
    ({"cursor_y": ""}, "pane_gone"),
])
def test_a_pane_that_is_not_plain_and_alive_types_nothing(fields, reason, session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    fake.fields = fields
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, reason)


def test_a_pane_that_prints_no_state_types_nothing(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    fake.info = ""
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "pane_gone")


def test_a_pane_tmux_cannot_find_types_nothing(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    fake.fail["pane-info"] = 1
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "pane_gone")


def test_claude_outside_the_pane_tree_types_nothing(session, fake, tmux_bin, project, other_pid):
    args = started(session, tmux_bin)
    fake.pane_pid = other_pid
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "process")


@pytest.mark.parametrize("now", [None, "ps:another start"])
def test_claude_gone_or_restarted_types_nothing(now, session, fake, tmux_bin, project, monkeypatch):
    args = started(session, tmux_bin)
    monkeypatch.setattr(rotation_state, "process_start_checked", lambda pid: now)
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "process")


def test_the_pane_pid_of_an_ancestor_counts(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    fake.pane_pid = os.getppid()
    tmux_clear.main(args)
    assert fake.sent()[:2] == [CLEAR, ["Enter"]]


# --- the session's claude owns the input of the pane's terminal ---------------------------------------

@pytest.fixture
def no_terminal():
    """A process in a session of its own, with no controlling terminal."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()


def stop(pid):
    """SIGSTOP a child of this process and wait until it is stopped."""
    os.kill(pid, signal.SIGSTOP)
    _, status = os.waitpid(pid, os.WUNTRACED)
    assert os.WIFSTOPPED(status)


@pytest.mark.parametrize("place", ["on the pane's terminal", "without a terminal"])
def test_a_stopped_claude_gets_no_key(place, tmp_path, project, fake, tmux_bin, claude, no_terminal):
    """Codex on e51a3929: the recorded claude was SIGSTOPed (as by Ctrl-Z), and /clear still went into the pane."""
    pid = claude.pid if place == "on the pane's terminal" else no_terminal
    args = started(Session(tmp_path, project, "sess-a", pid=pid), tmux_bin)
    stop(pid)
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "process_stopped")


def test_a_claude_stopped_after_typing_gets_no_enter(session, fake, tmux_bin, project, monkeypatch, claude):
    """The check before Enter reads the terminal again: a claude stopped since /clear was typed gets no key."""
    args = started(session, tmux_bin)
    after_record_of(monkeypatch, "clear_entered", lambda: stop(claude.pid))
    tmux_clear.main(args)
    assert fake.submitted == [] and fake.sent() == [CLEAR]
    left_alone(session, project, fake, "process_stopped")


@pytest.mark.parametrize("place", ["in a background group", "on another terminal", "without a terminal"])
def test_a_claude_that_does_not_own_the_terminal_input_gets_no_key(place, tmp_path, project, fake, tmux_bin, claude,
                                                                   stand_in, no_terminal):
    """It runs below the pane, but another program reads the keys: they would go to that program."""
    if place == "in a background group":
        background = stand_in("background")
        pid, fake.pane_tty = background.pid, background.tty
    elif place == "on another terminal":
        pid, fake.pane_tty = claude.pid, stand_in().tty
    else:
        pid = no_terminal
    tmux_clear.main(started(Session(tmp_path, project, "sess-a", pid=pid), tmux_bin))
    assert fake.sent() == []
    stopped(project, fake, "process_background")


def ps_answering(tmp_path, monkeypatch, answer):
    """`ps` on PATH that runs the real one, except for the read of a process's terminal (tpgid): `answer` there."""
    folder = tmp_path / "ps-bin"
    folder.mkdir()
    script = folder / "ps"
    script.write_text('#!/bin/sh\ncase "$*" in\n  *tpgid*)\n    %s\n    exit ;;\nesac\nexec %s "$@"\n' % (
        answer, shlex.quote(shutil.which("ps"))), encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ["PATH"])


# What ps prints for the terminal read, the pane's tty, and why no key goes (None: the keys go). The lines
# are those of BSD ps (macOS: tpgid 0 and tty ?? without a terminal) and procps (Linux: -1 and ?), so
# both systems are decided here, whichever one runs the test.
PS_ANSWERS = [
    pytest.param("echo '{p} {p} {p} Ss+  ttys035 '", "/dev/ttys035", None, id="macos-foreground"),
    pytest.param("echo '  {p}  {p}  {p} Ss+  pts/3'", "/dev/pts/3", None, id="linux-foreground"),
    pytest.param("echo '{p} {p} {p} R+ pts/3'", "/dev/pts/3", None, id="linux-foreground-running"),
    pytest.param("echo '{p} {p} 0 Ss ??'", "/dev/ttys035", "process_background", id="macos-no-terminal"),
    pytest.param("echo '{p} {p} -1 Ss ?'", "/dev/pts/3", "process_background", id="linux-no-terminal"),
    pytest.param("echo '{p} {p} 4242 S ttys035'", "/dev/ttys035", "process_background", id="background-group"),
    pytest.param("echo '{p} {p} {p} Ss+ ttys036'", "/dev/ttys035", "process_background", id="other-terminal"),
    pytest.param("echo '{p} {p} {p} T+ ttys035'", "/dev/ttys035", "process_stopped", id="macos-stopped"),
    pytest.param("echo '{p} {p} {p} t+ pts/3'", "/dev/pts/3", "process_stopped", id="linux-tracing-stop"),
    pytest.param("exit 1", "/dev/pts/3", "process_unreadable", id="ps-fails"),
    pytest.param("exit 0", "/dev/pts/3", "process_unreadable", id="ps-prints-nothing"),
    pytest.param("echo garbage", "/dev/pts/3", "process_unreadable", id="ps-prints-garbage"),
    pytest.param("echo '1 1 1 Ss+ pts/3'", "/dev/pts/3", "process_unreadable", id="ps-tells-of-another-process"),
    pytest.param("echo '{p} {p} {p} Ss+ pts/3'; echo '{p} {p} {p} Ss+ pts/3'", "/dev/pts/3", "process_unreadable",
                 id="ps-prints-two-lines"),
]


@pytest.mark.parametrize("answer, tty, reason", PS_ANSWERS)
def test_what_ps_tells_of_the_terminal_decides_the_keys(answer, tty, reason, session, fake, tmux_bin, project,
                                                        tmp_path, monkeypatch):
    args = started(session, tmux_bin)
    ps_answering(tmp_path, monkeypatch, answer.format(p=session.pid))
    fake.pane_tty = tty
    tmux_clear.main(args)
    if reason is None:
        assert fake.sent()[:2] == [CLEAR, ["Enter"]]
    else:
        assert fake.sent() == []
        stopped(project, fake, reason)


def test_ps_tells_the_terminal_of_each_stand_in(stand_in, no_terminal):
    owner = stand_in()
    facts = tmux_clear.terminal_of(owner.pid)
    assert ("/dev/" + facts.tty, facts.pgid, facts.tpgid) == (owner.tty, owner.pid, owner.pid)
    assert tmux_clear.terminal_problem(facts, owner.tty) is None
    background = stand_in("background")
    facts = tmux_clear.terminal_of(background.pid)
    assert ("/dev/" + facts.tty, facts.pgid) == (background.tty, background.pid) and facts.tpgid != background.pid
    assert tmux_clear.terminal_problem(facts, background.tty) == "process_background"
    facts = tmux_clear.terminal_of(no_terminal)
    assert facts.tty in ("?", "??") and facts.tpgid in (0, -1)
    stop(owner.pid)
    assert tmux_clear.terminal_problem(tmux_clear.terminal_of(owner.pid), owner.tty) == "process_stopped"


def test_descends_walks_up_only():
    table = {10: 5, 5: 1, 1: 0, 20: 1}
    assert tmux_clear.descends(table, 10, 5) and tmux_clear.descends(table, 10, 10)
    assert not tmux_clear.descends(table, 5, 10) and not tmux_clear.descends(table, 20, 5)
    assert not tmux_clear.descends({7: 7}, 7, 3)


def test_the_process_table_holds_this_process():
    table = tmux_clear.process_table()
    assert table[os.getpid()] == os.getppid()


def test_process_start_checked_agrees_with_the_marker_identity(other_pid):
    assert rotation_state.process_start_checked(other_pid) == rotation_state.process_start(other_pid)
    assert rotation_state.process_start_checked(os.getpid()) == rotation_state.process_start(os.getpid())


# --- the settle -------------------------------------------------------------------------------

def test_a_screen_that_keeps_changing_never_settles(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    fake.on_capture = lambda tmux: setattr(tmux, "base", tmux.base + " ")  # every capture differs
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, "settle")


def test_the_job_waits_until_the_stop_hook_has_exited(session, fake, tmux_bin, project, monkeypatch, clock):
    args = started(session, tmux_bin)
    seen = []

    def alive(pid):
        seen.append(clock.now)
        return len(seen) < 4

    monkeypatch.setattr(tmux_clear, "alive", alive)
    tmux_clear.main(args)
    assert len(seen) == 4
    assert fake.calls[0][0] == "capture-pane" and fake.sent()[:1] == [CLEAR]


def test_a_stop_hook_that_never_exits_types_nothing(session, fake, tmux_bin, project, monkeypatch):
    args = started(session, tmux_bin)
    monkeypatch.setattr(tmux_clear, "alive", lambda pid: True)
    tmux_clear.main(args)
    assert fake.calls == [["display-message", "-d", auto_clear.DISPLAY_MILLISECONDS, "-t", "%7",
                           auto_clear.display_text("started", "settle")]]
    stopped(project, fake, "settle")


def test_alive_tells_a_finished_process():
    assert tmux_clear.alive(os.getpid())
    proc = subprocess.Popen(["true"])
    proc.wait()
    assert not tmux_clear.alive(proc.pid)


# --- one job per pane: the lock --------------------------------------------------------------

def test_a_second_job_for_the_pane_waits_out_nothing_and_types_nothing(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    marker = json.loads(session.marker("ready").read_text())
    second = auto_clear.write_job({
        "version": 1, "project_dir": str(project), "session_id": "sess-a", "generation": marker["generation"],
        "pane": "%7", "process": marker["process"], "hook_pid": 1,
    })
    ran = []

    def second_job(tmux):
        if not ran:
            ran.append(tmux_clear.main(args[:3] + [second]))

    fake.on_capture = second_job
    tmux_clear.main(args)
    assert ran == [0]
    assert fake.sent().count(CLEAR) == 1
    assert clear_events(project).count("clear_started") == 1


def test_a_fresh_lock_of_another_job_means_this_one_leaves_silently(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    (rotation(project) / "clear-7.lock").mkdir()
    tmux_clear.main(args)
    assert fake.calls == []
    assert clear_events(project) == []
    assert (rotation(project) / "clear-7.lock").is_dir()


def test_a_stale_lock_is_broken_with_its_line(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    lock = rotation(project) / "clear-7.lock"
    lock.mkdir()
    old = time.time() - tmux_clear.STALE_LOCK_SECONDS - 5
    os.utime(str(lock), (old, old))
    tmux_clear.main(args)
    assert clear_events(project)[:2] == ["clear_lock_broken", "clear_started"]
    assert fake.sent()[:1] == [CLEAR]
    assert not lock.exists()


def test_the_job_leaves_a_lock_another_job_took_after_its_own(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    lock = rotation(project) / "clear-7.lock"
    other = rotation(project) / "other-lock"
    taken = []

    def another_job_takes_over(tmux):
        if tmux.typed == "/clear" and not taken:
            other.mkdir()  # made while the first lock still exists, so it cannot get its inode
            lock.rmdir()
            other.rename(lock)
            taken.append(True)

    fake.on_capture = another_job_takes_over
    tmux_clear.main(args)
    assert taken and lock.is_dir()


def test_a_file_at_the_lock_name_is_no_lock_and_is_left(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    (rotation(project) / "clear-7.lock").write_text("not a lock")
    tmux_clear.main(args)
    assert fake.sent() == []
    assert fake.shown() == [auto_clear.display_text("started", "lock")]
    assert (rotation(project) / "clear-7.lock").read_text() == "not a lock"


# --- the arguments and the job file ---------------------------------------------------------

@pytest.mark.parametrize("bad", [
    ["%7;x", "sess-a", "0" * 16, "a" * 32],
    ["7", "sess-a", "0" * 16, "a" * 32],
    ["%7", "sess a", "0" * 16, "a" * 32],
    ["%7", "sess-a", "0" * 15, "a" * 32],
    ["%7", "sess-a", "0" * 16, "../" + "a" * 29],
    ["%7", "sess-a", "0" * 16],
])
def test_ids_outside_their_patterns_are_refused(bad, fake):
    assert tmux_clear.main(bad) == 1
    assert fake.calls == [["display-message", "-d", auto_clear.DISPLAY_MILLISECONDS, tmux_clear.REFUSED]]


def test_a_missing_job_file_types_nothing(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    auto_clear.remove_job(args[3])
    tmux_clear.main(args)
    assert fake.sent() == []
    assert fake.shown() == [auto_clear.display_text("started", "job")]
    assert clear_events(project) == []


def test_a_job_file_is_taken_once(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    tmux_clear.main(args)
    fake.calls.clear()
    tmux_clear.main(args)
    assert fake.sent() == []
    assert fake.shown() == [auto_clear.display_text("started", "job")]


def test_a_job_file_for_other_ids_types_nothing(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    tmux_clear.main(["%8"] + args[1:])
    assert fake.sent() == []
    assert fake.shown() == [auto_clear.display_text("started", "job")]


@pytest.mark.parametrize("change", [
    {"version": True}, {"project_dir": "relative/dir"}, {"project_dir": "/a/../b"}, {"pane": "%7 "},
    {"process": {"pid": 0, "start": "x"}}, {"hook_pid": "1"}, {"extra": 1},
])
def test_a_job_file_the_hook_did_not_write_types_nothing(change, session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    path = os.path.join(os.path.expanduser("~"), ".claude", "context-rotation", "jobs", args[3] + ".json")
    document = json.loads(open(path).read())
    document.update(change)
    with open(path, "w") as handle:
        json.dump(document, handle)
    tmux_clear.main(args)
    assert fake.sent() == []
    assert fake.shown() == [auto_clear.display_text("started", "job")]


def test_storage_that_fails_the_gate_types_nothing(session, fake, tmux_bin, project, tmp_path):
    args = started(session, tmux_bin)
    elsewhere = tmp_path / "elsewhere"
    (rotation(project)).rename(elsewhere)
    rotation(project).symlink_to(elsewhere)
    tmux_clear.main(args)
    assert fake.sent() == []
    assert fake.shown() == [auto_clear.display_text("started", "storage")]


def test_auto_clear_turned_off_before_the_job_ran_types_nothing(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    settings_file(project, auto_clear=False)
    tmux_clear.main(args)
    assert fake.calls == []


# --- the user's lines -------------------------------------------------------------------------

def test_every_reason_has_a_line_for_every_step_and_none_holds_a_format():
    assert set(state_fields.CLEAR_ABORTS) >= {"busy", "input", "no_input_box", "typed", "no_ack"}
    for reason in state_fields.CLEAR_ABORTS:
        for reached in auto_clear.REACHED:
            text = auto_clear.display_text(reached, reason)
            assert state_fields.CLEAR_ABORTS[reason] in text and "#" not in text
    fixed = [auto_clear.MANUAL, auto_clear.STARTED, auto_clear.BACKGROUND, auto_clear.UNLISTED, tmux_clear.REFUSED]
    assert all("#" not in text for text in fixed)


def test_the_job_never_exits_two(session, fake, tmux_bin, project):
    assert tmux_clear.main([]) == 1
    assert tmux_clear.main(started(session, tmux_bin)) == 0
