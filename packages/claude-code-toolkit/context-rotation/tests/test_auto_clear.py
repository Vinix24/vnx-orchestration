"""The automatic path as the hooks see it (auto_clear): Stop starts the job or says why not, and what comes after.

The hooks run as processes, inside pane %7 of a fake tmux binary (conftest.tmux_bin)
that records every call; no real tmux server is ever reached.
"""
from __future__ import annotations

import json
import os
import shlex
import stat

import pytest

from conftest import (
    PKG, VALID_HANDOVER, Session, clear_events, load_fixture, message_of, rotation, settings_file, start, started,
)

import auto_clear
import context_budget
import hook_runtime
import rotation_state
import tmux_clear

FIXTURE_TASKS = load_fixture("payloads", "13-Stop-with-background-tasks.json")["payload"]["background_tasks"]


def jobs_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "context-rotation", "jobs")


def job_files():
    return sorted(os.listdir(jobs_dir())) if os.path.isdir(jobs_dir()) else []


def saved(session):
    session.write_handover()
    return json.loads(session.marker("ready").read_text())


# --- Stop starts the job -------------------------------------------------------------

def test_stop_in_tmux_starts_the_job_and_tells_the_user(session, tmux_bin, project):
    marker = saved(session)
    proc = session.finish_turn()
    assert message_of(proc) == auto_clear.STARTED
    (call,) = tmux_bin.calls("run-shell")
    assert call[:4] == ["run-shell", "-b", "-t", "%7"]
    words = shlex.split(call[4].replace("##", "#"))
    job_id = words[5]
    assert words == ["sh", os.path.join(str(PKG), "scripts", "tmux_clear.sh"), "%7", "sess-a", marker["generation"],
                     job_id, ">/dev/null", "2>&1", "||", "true"]
    assert job_files() == [job_id + ".json"]
    with open(os.path.join(jobs_dir(), job_id + ".json")) as handle:
        document = json.load(handle)
    assert document["project_dir"] == str(project) and document["process"] == marker["process"]
    assert document["hook_pid"] > 0 and auto_clear.job_problem(document) is None


def test_the_job_directories_are_the_users_own(session, tmux_bin):
    saved(session)
    session.finish_turn()
    for path in (os.path.dirname(jobs_dir()), jobs_dir()):
        found = os.lstat(path)
        assert stat.S_ISDIR(found.st_mode) and found.st_uid == os.getuid() and found.st_mode & 0o077 == 0
    (name,) = job_files()
    assert os.lstat(os.path.join(jobs_dir(), name)).st_mode & 0o077 == 0


def test_two_stops_for_one_handover_start_one_job(session, tmux_bin):
    saved(session)
    assert message_of(session.finish_turn()) == auto_clear.STARTED
    assert session.stop().stdout == ""
    assert len(tmux_bin.calls("run-shell")) == 1
    assert len(job_files()) == 1


def test_a_new_handover_starts_a_new_job(session, tmux_bin):
    saved(session)
    session.finish_turn()
    session.write_handover(VALID_HANDOVER + "\nMore.\n")
    assert message_of(session.finish_turn()) == auto_clear.STARTED
    assert len(tmux_bin.calls("run-shell")) == 2


# --- when nothing is started -----------------------------------------------------------

def test_running_background_tasks_type_nothing_and_say_what_runs(session, tmux_bin):
    saved(session)
    proc = session.stop(background_tasks=FIXTURE_TASKS)
    assert message_of(proc) == auto_clear.BACKGROUND % "1 background shell and 1 subagent"
    assert tmux_bin.calls() == [] and job_files() == []


def test_finished_background_tasks_do_not_hold_the_clear_back(session, tmux_bin):
    saved(session)
    done = [dict(task, status="completed") for task in FIXTURE_TASKS]
    assert message_of(session.stop(background_tasks=done)) == auto_clear.STARTED


def test_a_stop_without_its_list_of_tasks_types_nothing(session, tmux_bin):
    saved(session)
    payload = session.payload("03-Stop-idle")
    del payload["background_tasks"]
    assert message_of(session.hook("context_guard", payload)) == auto_clear.UNLISTED
    assert tmux_bin.calls() == []


@pytest.mark.parametrize("tasks, words", [
    ([{"type": "shell", "status": "running"}] * 2, "2 background shells"),
    ([{"type": "subagent"}], "1 subagent"),
    ([{"type": "monitor", "status": "running", "command": "rm -rf ~"}], "1 other task"),
    (["not a task"], "1 other task"),
    ([{"type": "shell", "status": "running"}, {"type": "subagent", "status": "running"}, {"type": "x"}],
     "1 background shell, 1 subagent and 1 other task"),
    ([{"type": "shell", "status": "killed"}], None),
    ([], None),
])
def test_what_still_runs_is_counted_in_fixed_words(tasks, words):
    assert auto_clear.running(tasks) == words


def test_auto_clear_off_keeps_the_manual_path(session, tmux_bin, project):
    settings_file(project, auto_clear=False)
    saved(session)
    assert message_of(session.finish_turn()) == auto_clear.MANUAL
    assert tmux_bin.calls() == []


def test_outside_tmux_the_manual_path_stays(session, tmux_bin, monkeypatch):
    saved(session)
    monkeypatch.delenv("TMUX")
    assert message_of(session.finish_turn()) == auto_clear.MANUAL
    assert tmux_bin.calls() == []


def test_a_pane_id_outside_its_pattern_starts_nothing(session, tmux_bin, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%7;touch x")
    marker = saved(session)
    assert marker["tmux_pane"] == "%7;touch x"
    assert message_of(session.finish_turn()) == auto_clear.MANUAL
    assert tmux_bin.calls() == []


def test_a_stop_in_another_pane_than_the_handover_starts_nothing(session, tmux_bin, monkeypatch):
    saved(session)
    monkeypatch.setenv("TMUX_PANE", "%8")
    assert message_of(session.finish_turn()) == auto_clear.MANUAL
    assert tmux_bin.calls() == []


def test_a_handover_without_its_claude_process_starts_nothing(session, tmux_bin):
    """The PostToolUse hook ran without CLAUDE_PID, so the marker records no process to find in the pane."""
    tool_id = session.tool_id()
    session.handover.parent.mkdir(parents=True, exist_ok=True)
    session.handover.write_text(VALID_HANDOVER, encoding="utf-8")
    payload = session.payload("07-PostToolUse-Write", tool_name="Write", tool_use_id=tool_id,
                              tool_input={"file_path": str(session.handover), "content": VALID_HANDOVER})
    session.hook("handover_detector", payload, drop=("CLAUDE_PID",))
    assert json.loads(session.marker("ready").read_text())["process"] is None
    assert message_of(session.finish_turn()) == auto_clear.MANUAL
    assert tmux_bin.calls() == []


# --- a start that fails falls back to the manual message --------------------------------------

def test_a_run_shell_that_fails_falls_back_and_leaves_no_job_file(session, tmux_bin, project):
    tmux_bin.set("status-run-shell", "1")
    saved(session)
    proc = session.finish_turn()
    assert message_of(proc) == auto_clear.MANUAL
    assert len(tmux_bin.calls("run-shell")) == 1 and job_files() == []
    assert any("start the automatic clear failed: OSError" in line for line in rotation_state.error_lines(str(project)))


def test_no_tmux_on_the_path_falls_back(session, tmux_bin, tmp_path, monkeypatch):
    saved(session)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    proc = session.hook("context_guard", session.payload("03-Stop-idle"), env_extra={"PATH": str(empty)})
    assert (proc.returncode, message_of(proc)) == (0, auto_clear.MANUAL)
    assert "Traceback" not in proc.stderr and job_files() == []


def test_a_job_directory_that_is_not_the_users_own_falls_back(session, tmux_bin):
    saved(session)
    os.makedirs(jobs_dir())
    os.chmod(jobs_dir(), 0o755)
    assert message_of(session.finish_turn()) == auto_clear.MANUAL
    assert tmux_bin.calls() == []


def test_a_symlinked_job_directory_falls_back(session, tmux_bin, tmp_path):
    saved(session)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    os.makedirs(os.path.dirname(jobs_dir()), mode=0o700)
    os.symlink(str(elsewhere), jobs_dir())
    assert message_of(session.finish_turn()) == auto_clear.MANUAL
    assert list(elsewhere.iterdir()) == []


def test_old_job_files_no_job_took_are_swept(session, tmux_bin):
    os.makedirs(os.path.dirname(jobs_dir()), mode=0o700)
    os.mkdir(jobs_dir(), 0o700)
    stale = os.path.join(jobs_dir(), "a" * 32 + ".json")
    other = os.path.join(jobs_dir(), "notes.txt")
    for path in (stale, other):
        with open(path, "w") as handle:
            handle.write("{}")
        old = os.path.getmtime(path) - auto_clear.JOB_FILE_MAX_AGE_SECONDS - 10
        os.utime(path, (old, old))
    saved(session)
    session.finish_turn()
    assert not os.path.exists(stale) and os.path.exists(other)
    assert len([n for n in job_files() if n.endswith(".json")]) == 1


# --- the run-shell line -------------------------------------------------------------------------

def tmux_expand(line):
    """What tmux makes of a run-shell argument with no format left in it: `##` is one `#`."""
    assert "#" not in line.replace("##", ""), "a single # would start a tmux format"
    return line.replace("##", "#")


@pytest.mark.parametrize("script", ["/opt/plug in/scripts/tmux_clear.sh", "/a b'c$(x)#{pane_id}/t.sh",
                                    "/x#(touch y)/t.sh", "/ends with;/t.sh"])
def test_the_run_shell_line_survives_tmux_and_sh(script):
    line = auto_clear.run_shell_line([script, "%7", "sess-a", "0" * 16, "a" * 32])
    expanded = tmux_expand(line)
    assert shlex.split(expanded) == ["sh", script, "%7", "sess-a", "0" * 16, "a" * 32, ">/dev/null", "2>&1", "||",
                                     "true"]
    assert not line.endswith(";")


# --- the next prompt repeats a job that stopped ------------------------------------------------

@pytest.fixture
def stopped_job(session, fake, tmux_bin, clock):
    """A job that found text in the input line and typed nothing."""
    args = started(session, tmux_bin)
    fake.typed = "half typed"
    tmux_clear.main(args)
    assert fake.sent() == []
    return session


def test_the_next_prompt_repeats_why_no_clear_happened(stopped_job, project):
    proc = stopped_job.prompt("half typed, and now sent")
    document = json.loads(proc.stdout)
    assert document["systemMessage"] == auto_clear.REPEAT_BEFORE % "there is text in the input line"
    assert "no longer covers the latest work" in document["hookSpecificOutput"]["additionalContext"]
    assert clear_events(project)[-1] == "clear_told"
    assert stopped_job.prompt("and another").stdout.count("automatic /clear") == 0


def test_a_typed_clear_takes_the_message_without_showing_it(stopped_job, project):
    proc = stopped_job.prompt("/clear")
    assert "automatic" not in proc.stdout
    assert clear_events(project)[-1] == "clear_told"


def test_another_claude_process_in_the_pane_gets_no_repeat(tmp_path, stopped_job, project, other_pid):
    other = Session(tmp_path, project, "sess-z", pid=other_pid)
    assert "automatic /clear" not in other.prompt("hello").stdout
    assert json.loads((rotation(project) / "clear-7.json").read_text())["told"] is False
    assert "automatic /clear" in stopped_job.prompt("hello").stdout


def test_the_fresh_session_hears_when_no_one_took_the_handover(tmp_path, session, fake, tmux_bin, project, clock):
    settings_file(project, ack_timeout_seconds=2)
    tmux_clear.main(started(session, tmux_bin))
    assert fake.sent() == [["-l", "/clear"], ["Enter"]]
    fresh = Session(tmp_path, project, "sess-b", pid=session.pid)
    message = message_of(fresh.prompt("hello"))
    assert message == auto_clear.ENTERED % (
        "the delivery of the handover to the fresh session was not confirmed in time")


def test_a_record_that_cannot_be_written_shows_nothing_and_is_logged(stopped_job, project, monkeypatch):
    def refused(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(rotation_state, "journal", refused)
    session = hook_runtime.Session(str(project), "sess-a", context_budget.load_settings(str(project)))
    env = {"CLAUDE_PID": str(stopped_job.pid), "TMUX_PANE": "%7"}
    assert auto_clear.prompt_notice({"prompt": "hello"}, env, hook_runtime.HookRun(), session) is None
    assert json.loads((rotation(project) / "clear-7.json").read_text())["told"] is False
    assert any("repeat the stopped clear failed: PermissionError" in line
               for line in rotation_state.error_lines(str(project)))


# --- the record of a pane --------------------------------------------------------------------------

def a_record(**changes):
    record = auto_clear.new_record({"pane": "%7", "session_id": "sess-a", "generation": "0" * 16,
                                    "process": {"pid": 12, "start": "ps:x"}})
    record.update(changes)
    return record


@pytest.mark.parametrize("changes", [
    {"version": True}, {"pane": "%8"}, {"origin_session_id": "a/b"}, {"generation": "x"},
    {"process": None}, {"step": "typed"}, {"step": "aborted"}, {"step": "aborted", "reason": "busy"},
    {"step": "aborted", "reason": "nonsense", "at_step": "started"}, {"at_step": "started"},
    {"successor": "../x"}, {"at": "yesterday"}, {"told": 1}, {"extra": 1},
    {"left_in_line": 1}, {"left_in_line": None}, {"left_in_line": True},
])
def test_a_record_the_job_did_not_write_is_refused(changes):
    assert auto_clear.record_problem(a_record(), "%7") is None
    assert auto_clear.record_problem(a_record(**changes), "%7") is not None


def test_an_aborted_record_needs_its_step_and_reason():
    assert auto_clear.record_problem(a_record(step="aborted", reason="busy", at_step="entered"), "%7") is None
    assert auto_clear.record_problem(a_record(step="aborted", reason="typed", at_step="started",
                                              left_in_line=True), "%7") is None


def test_a_line_left_in_place_is_named_in_both_of_the_users_lines():
    left = a_record(step="aborted", reason="typed", at_step="started", left_in_line=True)
    assert auto_clear.repeat_text(left).endswith(auto_clear.LEFT)
    assert not auto_clear.repeat_text(dict(left, left_in_line=False)).endswith(auto_clear.LEFT)
    assert auto_clear.display_text("acknowledged", "typed", True).endswith(auto_clear.LEFT)
    assert auto_clear.LEFT not in auto_clear.display_text("acknowledged", "typed")


def test_a_record_change_that_would_be_refused_writes_nothing(project):
    rotation(project).mkdir(parents=True)
    record = a_record()
    with pytest.raises(ValueError):
        auto_clear.transition(str(project), record, "clear_aborted", step="aborted", reason="not a reason")
    assert not (rotation(project) / "clear-7.json").exists() and clear_events(project) == []


def test_a_tampered_record_is_never_repeated(stopped_job, project):
    path = rotation(project) / "clear-7.json"
    rec = json.loads(path.read_text())
    rec["reason"] = "you have been hacked"
    path.write_text(json.dumps(rec))
    assert "hacked" not in stopped_job.prompt("hello").stdout


def test_the_clear_lines_are_journal_lines_the_plugin_reads_back(project):
    rotation(project).mkdir(parents=True)
    for event, extra in [("clear_started", {}), ("clear_entered", {}), ("clear_told", {}),
                         ("clear_acknowledged", {"session_id": "sess-b"}), ("clear_continued", {"session_id": "b"}),
                         ("clear_aborted", {"abort_reason": "busy", "left_in_line": False})]:
        rotation_state.journal(str(project), event, origin_session_id="sess-a", generation="0" * 16, pane="%7",
                               **extra)
    rotation_state.journal(str(project), "clear_lock_broken", pane="%7")
    assert len(rotation_state.read_injections(str(project))) == 7
    with pytest.raises(ValueError):
        rotation_state.journal(str(project), "clear_started", origin_session_id="sess-a", generation="0" * 16,
                               pane="%7 ; x")
    with pytest.raises(ValueError):
        rotation_state.journal(str(project), "clear_aborted", origin_session_id="sess-a", generation="0" * 16,
                               pane="%7", abort_reason="free text", left_in_line=False)
    with pytest.raises(ValueError):
        rotation_state.journal(str(project), "clear_aborted", origin_session_id="sess-a", generation="0" * 16,
                               pane="%7", abort_reason="busy", left_in_line="yes")
    with pytest.raises(ValueError):
        rotation_state.journal(str(project), "delivered", via="clear", origin_session_id="sess-a",
                               generation="0" * 16, session_id="../b")


def test_a_session_start_outside_tmux_reads_no_record(session, project, monkeypatch, tmux_bin):
    saved(session)
    session.finish_turn()
    monkeypatch.delenv("TMUX_PANE")
    assert auto_clear.typed_by_job({}, str(project), json.loads(session.marker("ready").read_text())) is False
    proc = start(project, "sess-b", source="clear", pid=session.pid)
    assert auto_clear.OFF_HINT not in message_of(proc)


def test_the_first_on_machine_note_is_kept_in_the_users_own_directory():
    assert auto_clear.first_on_machine() is True
    assert auto_clear.first_on_machine() is False
    noted = os.path.join(os.path.dirname(jobs_dir()), auto_clear.NOTED)
    assert stat.S_ISREG(os.lstat(noted).st_mode)
