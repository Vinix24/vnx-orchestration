"""What happens to a handover between its write and its removal, and what is recorded.

- The cleanup moves the original handover off its path only while it is still
  the file the claim read, and never deletes it: Claude Code's Write tool does
  not take the project lock, so a write can land at any moment of the cleanup
  (codex on #2080, OI-2126 item 1, round 7 item 3).
- The transcript cursor of a ready-marker sits at the handover write itself, so
  work that lands while the hook runs is after it (OI-2126 item 2).
- Every transition has its line in injections.jsonl before the marker changes
  (OI-2126 item 4), and that file is kept to a bound tied to archive_keep.
- errors.log loses no line when hooks fail at the same moment (OI-2083).
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys

import pytest

from conftest import (PKG, VALID_HANDOVER, Session, b1_entry, context_of, event, message_of, rotation, settings_file,
                      start)

sys.path.insert(0, str(PKG / "hooks"))

import handover_detector
import hook_runtime
import regular_file
import rotation_state
import session_start

NEWER = VALID_HANDOVER + "\nWritten again by the origin session.\n"
MIDDLE = VALID_HANDOVER + "\nAn edit in between.\n"
MANUAL = "Handover saved. Type /clear, then any message, to continue in a fresh context."


def saved_session(tmp_path, project, session_id="sess-a", pid=None):
    s = Session(tmp_path, project, session_id, pid=pid)
    s.write_handover()
    s.finish_turn()
    assert s.marker("ready").exists()
    return s


def run_clear(project, session_id="sess-b"):
    payload = event("17-SessionStart-clear", project, project / "t.jsonl", session_id)
    session_start.run(payload, dict(os.environ, CLAUDE_PID=str(os.getpid())), hook_runtime.HookRun())


def journal(project):
    path = rotation(project) / "injections.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def archived_texts(project):
    directory = rotation(project) / "archive"
    return sorted(p.read_text() for p in directory.glob("*.md")) if directory.exists() else []


class Crash(Exception):
    pass


def crash(*args, **kwargs):
    raise Crash("simulated crash")


# --- the original moves off its path only while it is the file the claim read ------

def test_a_handover_written_right_after_the_cleanups_last_read_is_never_deleted(tmp_path, project, monkeypatch, capsys):
    s = saved_session(tmp_path, project)
    real_check = rotation_state._still_claimed

    def checked_then_the_origin_writes(*args):
        result = real_check(*args)
        s.handover.write_text(NEWER)  # the Write tool, right after the plugin's last read of the moved file
        return result

    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "_still_claimed", checked_then_the_origin_writes)
        run_clear(project)
    assert s.handover.exists(), "the cleanup deleted a handover written after its last read"
    assert s.handover.read_text() == NEWER
    assert archived_texts(project) == [VALID_HANDOVER]


def after_the_archive_copy(patch, action):
    """Run `action` right after the claim's archive copy is written, before the original is moved."""
    real_archive = rotation_state.archive_handover

    def archive_then_act(*args):
        result = real_archive(*args)
        action()
        return result

    patch.setattr(rotation_state, "archive_handover", archive_then_act)


def test_a_handover_rewritten_after_the_claim_is_kept_even_with_the_same_bytes(tmp_path, project, monkeypatch, capsys):
    s = saved_session(tmp_path, project)

    def the_origin_writes_the_same_text():
        s.handover.write_text(VALID_HANDOVER)
        found = os.stat(str(s.handover))
        os.utime(str(s.handover), ns=(found.st_atime_ns, found.st_mtime_ns + 1000000))

    with monkeypatch.context() as patch:
        after_the_archive_copy(patch, the_origin_writes_the_same_text)
        run_clear(project)
    assert s.handover.exists(), "a handover changed after it was read was deleted"
    assert s.handover.read_text() == VALID_HANDOVER
    # Kept without a marker of its own: listed as an unclaimed handover, never dropped.
    assert s.marker("outdated").exists()
    notice = message_of(start(project, "sess-c", source="startup"))
    assert "outdated (the handover file changed after it was checked)" in notice and "by session sess-a" in notice


def test_a_generation_set_aside_while_a_newer_one_appeared_stays_in_the_archive(tmp_path, project, monkeypatch, capsys):
    s = saved_session(tmp_path, project)
    planned, real_plan, real_check = [], rotation_state.plan_archive, rotation_state._still_claimed

    def plan_then_an_edit(project_dir, origin, now=None, suffix=".md"):
        planned.append(origin)
        if len(planned) == 2:  # the cleanup planned the original's archive name, after it checked the file
            with open(str(s.handover), "a") as handle:
                handle.write(MIDDLE[len(VALID_HANDOVER):])
        return real_plan(project_dir, origin, now, suffix)

    def checked_then_a_newer_write(*args):
        result = real_check(*args)
        s.handover.write_text(NEWER)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "plan_archive", plan_then_an_edit)
        patch.setattr(rotation_state, "_still_claimed", checked_then_a_newer_write)
        run_clear(project)
    assert s.handover.read_text() == NEWER
    assert archived_texts(project) == sorted([VALID_HANDOVER, MIDDLE])
    [kept] = [r for r in journal(project) if r.get("event") == "kept"]
    assert open(kept["archive"]).read() == MIDDLE


def test_a_handover_moved_back_to_its_path_also_keeps_its_archive_name(tmp_path, project, monkeypatch, capsys):
    """Changed between the check and the move, nothing newer at the path: it goes back, and nothing is deleted."""
    s = saved_session(tmp_path, project)
    planned, real_plan = [], rotation_state.plan_archive

    def plan_then_an_edit(project_dir, origin, now=None, suffix=".md"):
        planned.append(origin)
        if len(planned) == 2:
            with open(str(s.handover), "a") as handle:
                handle.write(MIDDLE[len(VALID_HANDOVER):])
        return real_plan(project_dir, origin, now, suffix)

    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "plan_archive", plan_then_an_edit)
        run_clear(project)
    assert s.handover.read_text() == MIDDLE
    assert archived_texts(project) == sorted([VALID_HANDOVER, MIDDLE])
    assert s.marker("outdated").exists()
    assert json.loads(s.marker("outdated").read_text())["sha256"] == rotation_state.sha256_hex(MIDDLE.encode())


def test_an_unchanged_original_is_still_removed(tmp_path, project, capsys):
    s = saved_session(tmp_path, project)
    run_clear(project)
    assert not s.handover.exists()
    assert archived_texts(project) == [VALID_HANDOVER]
    assert not list(rotation(project).glob("*.json"))


# --- the transcript cursor of a ready-marker ---------------------------------------

def detect_in_process(s, tool_id):
    payload = s.payload("07-PostToolUse-Write", tool_name="Write", tool_use_id=tool_id,
                        tool_input={"file_path": str(s.handover), "content": VALID_HANDOVER})
    handover_detector.run(payload, dict(os.environ, CLAUDE_PID=str(os.getpid())), hook_runtime.HookRun())
    s.append(b1_entry("tool_result", tool_use_id=tool_id, file_path=s.handover, tool_output="written"),
             b1_entry("hook_additional_context", tool_use_id=tool_id, hook_text="checked"))


def write_entry(s, tool_id):
    s.append(b1_entry("tool_use_write", tool_use_id=tool_id, handover_path=s.handover, handover_text="text"))
    s.handover.parent.mkdir(parents=True, exist_ok=True)
    s.handover.write_text(VALID_HANDOVER)


def test_a_prompt_that_lands_while_the_detector_runs_is_after_the_cursor(tmp_path, project, monkeypatch, capsys):
    s = Session(tmp_path, project, "sess-a")
    tool_id = s.tool_id()
    write_entry(s, tool_id)
    real_identity = rotation_state.process_identity

    def identity_while_a_prompt_arrives(env):
        s.append(b1_entry("user_prompt", prompt="one more thing"))
        return real_identity(env)

    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "process_identity", identity_while_a_prompt_arrives)
        detect_in_process(s, tool_id)
    assert message_of(s.finish_turn()) != MANUAL
    assert s.marker("outdated").exists()
    assert json.loads(s.marker("outdated").read_text())["outdated_reason"] == "work after it (a prompt)"


def test_a_parallel_result_that_lands_before_the_detector_runs_is_after_the_cursor(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    bash_id, write_id = s.tool_id(), s.tool_id()
    s.append(b1_entry("tool_use_bash", tool_use_id=bash_id, command="make", description="build"))
    write_entry(s, write_id)
    s.append(b1_entry("tool_result", tool_use_id=bash_id, file_path="x", tool_output="built"))
    s.hook("handover_detector", s.payload(
        "07-PostToolUse-Write", tool_name="Write", tool_use_id=write_id,
        tool_input={"file_path": str(s.handover), "content": VALID_HANDOVER}))
    s.append(b1_entry("tool_result", tool_use_id=write_id, file_path=s.handover, tool_output="written"))
    assert message_of(s.finish_turn()) != MANUAL
    assert json.loads(s.marker("outdated").read_text())["outdated_reason"] == "work after it (the result of a tool call)"


# --- a line in injections.jsonl before every transition --------------------------

def test_every_transition_has_its_line(tmp_path, project, other_pid):
    s = saved_session(tmp_path, project, pid=other_pid)
    s.prompt("one more thing")
    user = Session(tmp_path, project, "sess-c")
    user.expand("resume")
    user.expand("resume 1")
    lines = journal(project)
    assert [line.get("event") for line in lines] == ["ready", "outdated", "claimed", "injected", "delivered"]
    assert all(line.get("origin_session_id") == "sess-a" for line in lines)
    assert len({line.get("generation") for line in lines}) == 1, "one handover, one generation in every line"
    assert lines[1].get("reason") == "a prompt after it"
    assert [line.get("session_id") for line in lines[2:]] == ["sess-c"] * 3
    assert lines[4]["via"] == "resume"
    assert open(lines[3]["archive"]).read() == VALID_HANDOVER


def test_a_crash_right_after_the_claim_leaves_the_claims_line(tmp_path, project, monkeypatch):
    saved_session(tmp_path, project)
    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "archive_handover", crash)
        with pytest.raises(Crash):
            run_clear(project)
    [claimed] = rotation(project).glob("claimed-*.json")
    [*_, last] = journal(project) or [{}]
    assert (last.get("event"), last.get("origin_session_id"), last.get("session_id")) == ("claimed", "sess-a", "sess-b")
    assert last.get("archive") == json.loads(claimed.read_text())["archive"]


def without_the_delivered_line(patch):
    """The journal refuses the `delivered` line only: the emit before it returned (OI-2126 item 4)."""
    real_append = rotation_state.append_journal

    def append(project_dir, record):
        if record.get("event") == "delivered":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_append(project_dir, record)

    patch.setattr(rotation_state, "append_journal", append)


def test_a_claim_leaves_only_after_its_delivered_line(tmp_path, project, monkeypatch, capsys):
    saved_session(tmp_path, project)
    with monkeypatch.context() as patch:
        without_the_delivered_line(patch)
        run_clear(project)
    assert VALID_HANDOVER in json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert [line.get("event") for line in journal(project)] == ["ready", "claimed", "injected"]
    claims = list(rotation(project).glob("claimed-*.json"))
    assert len(claims) == 1, "the claim was removed without a line in injections.jsonl"
    assert "claim did not finish" in message_of(start(project, "sess-c", source="startup"))
    assert any("log the delivery of generation" in line
               for line in (rotation(project) / "errors.log").read_text().splitlines())


def test_a_claim_resumed_once_its_delivered_line_can_be_written_leaves(tmp_path, project, monkeypatch, capsys):
    saved_session(tmp_path, project)
    with monkeypatch.context() as patch:
        without_the_delivered_line(patch)
        run_clear(project)
    user = Session(tmp_path, project, "sess-c")
    user.expand("resume")
    assert VALID_HANDOVER in context_of(user.expand("resume 1"))
    assert [line.get("event") for line in journal(project)][-2:] == ["injected", "delivered"]
    assert not list(rotation(project).glob("claimed-*.json"))


def make_ready(project, session_id="sess-a"):
    handover = rotation_state.handover_path(str(project), session_id)
    os.makedirs(os.path.dirname(handover), exist_ok=True)
    with open(handover, "wb") as handle:
        handle.write(VALID_HANDOVER.encode())
    marker = rotation_state.new_marker(session_id, handover, VALID_HANDOVER.encode(),
                                       {"CLAUDE_PID": str(os.getpid())}, None, "tool-1")
    rotation_state.mark_ready(str(project), marker)


def no_space(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


TRANSITIONS = {
    "outdated": lambda p: rotation_state.mark_outdated(p, "sess-a", "a prompt after it", notified=False),
    "claim": lambda p: rotation_state.claim(p, rotation_state.marker_path(p, "ready", "sess-a"), "sess-b"),
}


@pytest.mark.parametrize("name", sorted(TRANSITIONS))
def test_no_transition_happens_without_its_line(project, monkeypatch, name):
    make_ready(project)
    with monkeypatch.context() as patch:
        patch.setattr(regular_file, "append_regular", no_space)
        with pytest.raises(OSError):
            TRANSITIONS[name](str(project))
    assert sorted(p.name for p in rotation(project).glob("*.json")) == ["ready-sess-a.json"]


def valid_line(number):
    return json.dumps({
        "event": "ready", "at": "2026-10-17T09:00:%02dZ" % (number % 60),
        "origin_session_id": "sess-%d" % number, "sha256": "0" * 64,
    }, sort_keys=True)


def test_the_journal_is_kept_to_a_bound_tied_to_archive_keep(tmp_path, project):
    settings_file(project, archive_keep=2)
    lines = [valid_line(number) for number in range(100)]
    rotation(project).mkdir(parents=True)
    (rotation(project) / "injections.jsonl").write_text("\n".join(lines) + "\n")
    assert start(project, "sess-b", source="startup").returncode == 0
    kept = rotation_state.JOURNAL_LINES_PER_ARCHIVE_COPY * 2
    assert (rotation(project) / "injections.jsonl").read_text().splitlines() == lines[-kept:]


# --- errors.log under concurrent failures ----------------------------------------------

def test_an_error_logged_while_another_is_written_is_kept(project, monkeypatch):
    rotation(project).mkdir(parents=True)
    nested = []
    real_write, real_append = rotation_state._atomic_write, regular_file.append_regular

    def another_error_first():
        if not nested:
            nested.append(True)
            rotation_state.log_error(str(project), "the second error")

    def write(project_dir, target, data, prefix):
        if target.endswith("errors.log"):
            another_error_first()
        return real_write(project_dir, target, data, prefix)

    def append(path, data, dir_fd=None):
        if str(path).endswith("errors.log"):
            another_error_first()
        return real_append(path, data, dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "_atomic_write", write)
        patch.setattr(regular_file, "append_regular", append)
        assert rotation_state.log_error(str(project), "the first error")
    lines = (rotation(project) / "errors.log").read_text().splitlines()
    assert sorted(lines) == ["the first error", "the second error"]


WRITER = """
import sys
sys.path.insert(0, sys.argv[1])
import rotation_state
for number in range(8):
    assert rotation_state.log_error(sys.argv[2], "writer %s line %d" % (sys.argv[3], number))
"""


def test_concurrent_errors_lose_no_line(project):
    rotation(project).mkdir(parents=True)
    writers = [subprocess.Popen([sys.executable, "-c", WRITER, str(PKG / "lib"), str(project), str(number)])
               for number in range(6)]
    assert [writer.wait(timeout=60) for writer in writers] == [0] * 6
    lines = (rotation(project) / "errors.log").read_text().splitlines()
    assert sorted(lines) == sorted("writer %d line %d" % (w, n) for w in range(6) for n in range(8))


def test_errors_log_keeps_the_newest_lines(project):
    for number in range(rotation_state.ERROR_LOG_MAX_LINES + 7):
        rotation_state.log_error(str(project), "line %d" % number)
    lines = (rotation(project) / "errors.log").read_text().splitlines()
    assert lines == ["line %d" % n for n in range(7, rotation_state.ERROR_LOG_MAX_LINES + 7)]


def test_a_line_without_its_transition_changes_nothing(tmp_path, project, monkeypatch, capsys):
    """A crash between the line and the rename: the marker on disk is the truth, the line is history."""
    saved_session(tmp_path, project)
    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "_move_in_rotation", crash)
        with pytest.raises(Crash):
            rotation_state.mark_outdated(str(project), "sess-a", "a prompt after it", notified=False)
    assert [r["event"] for r in journal(project)] == ["ready", "outdated"]
    run_clear(project)
    assert VALID_HANDOVER in json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]


def test_a_journal_above_the_read_limit_is_trimmed_from_its_end(tmp_path, project):
    settings_file(project, archive_keep=1)
    rotation(project).mkdir(parents=True)
    filler = "x" * 1000
    with open(str(rotation(project) / "injections.jsonl"), "w") as handle:
        for number in range(1200):  # more than regular_file.MAX_READ_BYTES
            handle.write(json.dumps({"filler": filler, "n": number}) + "\n")
        last = [valid_line(number) for number in range(3)]
        handle.write("\n".join(last) + "\n")
    assert os.path.getsize(str(rotation(project) / "injections.jsonl")) > 1 << 20  # regular_file.MAX_READ_BYTES
    assert start(project, "sess-b", source="startup").returncode == 0
    kept = (rotation(project) / "injections.jsonl").read_text().splitlines()
    assert len(kept) == rotation_state.JOURNAL_LINES_PER_ARCHIVE_COPY and kept[-3:] == last  # archive_keep 1


def test_the_cleanup_leaves_a_handover_path_that_is_no_regular_file_alone(tmp_path, project, monkeypatch, capsys):
    s = saved_session(tmp_path, project)
    target = tmp_path / "elsewhere.md"
    target.write_text("not the handover")

    def a_symlink_takes_the_path():
        s.handover.unlink()
        s.handover.symlink_to(target)

    with monkeypatch.context() as patch:
        after_the_archive_copy(patch, a_symlink_takes_the_path)
        run_clear(project)
    assert s.handover.is_symlink() and target.read_text() == "not the handover"
    assert archived_texts(project) == [VALID_HANDOVER]
    assert not list(rotation(project).glob("claimed-*.json"))
