"""The job of the automatic /clear (tmux_clear), in-process: tmux is a recorder that plays an A0 capture.

Every job here was started by the real Stop hook, which wrote its job file and asked
the (fake) tmux binary to run it; the test then runs that job with the arguments of
that run-shell line.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time

import pytest

from conftest import (
    PANE_EXPECTED, VALID_HANDOVER, FakeTmux, Session, b1_entry, clear_events, journal_lines, message_of, rotation,
    settings_file, start,
)

import auto_clear
import rotation_state
import state_fields
import tmux_clear
import transcript_work

CLEAR = ["-l", "/clear"]
CONTINUE = ["-l", tmux_clear.CONTINUE]
SIX = ["BSpace"] * 6


class Clock:
    """monotonic and sleep for the job: a sleep moves the time on, nothing waits."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    found = Clock()
    monkeypatch.setattr(tmux_clear, "monotonic", found.monotonic)
    monkeypatch.setattr(tmux_clear, "sleep", found.sleep)
    return found


@pytest.fixture
def fake(monkeypatch, tmux_bin):
    """The tmux the job talks to; the hooks of the test run in pane %7 of the fake binary (tmux_bin)."""
    recorder = FakeTmux()
    monkeypatch.setattr(auto_clear, "run_tmux", recorder)
    return recorder


@pytest.fixture
def session(tmp_path, project, tmux_bin):
    return Session(tmp_path, project, "sess-a")


def started(session, tmux_bin):
    """Write the handover and end the turn: the Stop hook starts the job. The job's arguments."""
    session.write_handover()
    assert message_of(session.finish_turn()) == auto_clear.STARTED
    line = tmux_bin.calls("run-shell")[-1][-1]
    return shlex.split(line.replace("##", "#"))[2:6]


def successor(project, results, session_id="sess-b"):
    """On Enter after /clear: the SessionStart of the fresh session, in the same claude process and pane."""
    def on_enter(submitted):
        if submitted == "/clear":
            results.append(start(project, session_id, source="clear"))
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
    fake.on_enter.append(successor(project, results))
    assert tmux_clear.main(args) == 0
    assert fake.sent() == [CLEAR, ["Enter"], CONTINUE, ["Enter"]]
    assert fake.shown() == []
    assert clear_events(project) == ["clear_started", "clear_entered", "clear_acknowledged", "clear_continued"]
    events = [line["event"] for line in journal_lines(project)]
    assert events.index("clear_entered") < events.index("injected") < events.index("clear_acknowledged")
    found = record(project)
    assert (found["step"], found["successor"], found["origin_session_id"]) == ("continued", "sess-b", "sess-a")
    assert not (rotation(project) / "clear-7.lock").exists()
    (proc,) = results
    assert VALID_HANDOVER in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_the_first_automatic_clear_says_how_to_turn_it_off_and_the_next_does_not(
        tmp_path, session, fake, tmux_bin, project):
    results = []
    fake.on_enter.append(successor(project, results))
    tmux_clear.main(started(session, tmux_bin))
    assert auto_clear.OFF_HINT in message_of(results[0])
    nxt = Session(tmp_path, project, "sess-b")
    fake.on_enter[:] = [successor(project, results, "sess-c")]
    tmux_clear.main(started(nxt, tmux_bin))
    assert message_of(results[1]).startswith("context-rotation: continued from")
    assert auto_clear.OFF_HINT not in message_of(results[1])


def test_a_clear_the_user_typed_gets_no_hint(session, project, tmux_bin):
    session.write_handover()
    session.finish_turn()
    proc = start(project, "sess-b", source="clear")
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


# --- verify before Enter ---------------------------------------------------------

def test_a_line_that_never_reads_clear_gets_six_bspace_and_nothing_else(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    fake.echo = False  # the keys land, but the input line never shows them
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, SIX]
    stopped(project, fake, "typed")
    assert "clear_entered" not in clear_events(project)


def test_a_key_of_the_user_between_check_and_typing_is_kept(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)

    def user_types(tmux):
        if tmux.typed == "/clear":
            tmux.typed = "x/clear"

    fake.on_capture = user_types
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, SIX]
    assert fake.typed == "x"
    stopped(project, fake, "typed")


def test_a_pane_that_cannot_be_read_after_typing_gets_six_bspace(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)

    def gone(tmux):
        if tmux.typed == "/clear":
            tmux.fail["capture-pane"] = OSError("the pane went away")

    fake.on_capture = gone
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, SIX]
    stopped(project, fake, "pane_unreadable")


def test_the_continuation_line_is_verified_too(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    results = []
    fake.on_enter.append(successor(project, results))

    def silent_after_clear(submitted):
        if submitted == "/clear":
            fake.echo = False

    fake.on_enter.append(silent_after_clear)
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, ["Enter"], CONTINUE, ["BSpace"] * len(tmux_clear.CONTINUE)]
    stopped(project, fake, "typed", "acknowledged")


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


def test_an_injection_of_another_generation_is_no_ack(session, fake, tmux_bin, project):
    args = started(session, tmux_bin)

    def other_generation(submitted):
        rotation_state.journal(str(project), "injected", via="clear", origin_session_id="sess-a",
                               generation="0" * 16, session_id="sess-b",
                               handover=str(session.handover), archive=str(rotation(project) / "archive" /
                                                                           "20261017T000000000000Z-sess-a.md"),
                               sha256="0" * 64, chars=1, mode="inline")

    fake.on_enter.append(other_generation)
    tmux_clear.main(args)
    assert fake.sent() == [CLEAR, ["Enter"]]
    stopped(project, fake, "no_ack", "entered")


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


# --- a read that fails decides nothing ---------------------------------------------------

def denied(*args, **kwargs):
    raise PermissionError(13, "Permission denied")


@pytest.mark.parametrize("target, reason", [
    ((rotation_state, "read_marker_checked"), "marker_unreadable"),
    ((rotation_state, "read_checked"), "marker_unreadable"),
    ((transcript_work, "position_holds"), "transcript_unreadable"),
    ((tmux_clear, "process_table"), "process_unreadable"),
    ((rotation_state, "process_start_checked"), "process_unreadable"),
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

@pytest.mark.parametrize("info, reason", [
    ("%7 {pid} 0 1 0", "pane_mode"),
    ("%7 {pid} 0 0 1", "pane_synchronized"),
    ("%7 {pid} 1 0 0", "pane_gone"),
    ("%8 {pid} 0 0 0", "pane_gone"),
    ("", "pane_gone"),
    ("%7 x 0 0 0", "pane_gone"),
])
def test_a_pane_that_is_not_plain_and_alive_types_nothing(info, reason, session, fake, tmux_bin, project):
    args = started(session, tmux_bin)
    fake.info = info.format(pid=os.getpid())
    tmux_clear.main(args)
    assert fake.sent() == []
    stopped(project, fake, reason)


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
