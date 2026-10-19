"""One storage gate at every entry, and no SessionStart path that raises.

`.claude/rotation`, its archive and its state directory must each be a real
directory. When one of them is a symlink, every hook entry does nothing in that
project for that call: one line on stderr, exit 0, no errors.log, and nothing in
the directory the symlink points to is read, renamed, written or deleted. A
storage failure inside the SessionStart hook is logged and the hook still exits
0, also after the handover was emitted; a claim that could not be finished stays
for `resume`.
"""
from __future__ import annotations

import errno
import io
import json
import os
import sys
from pathlib import Path

import pytest

from conftest import (PKG, VALID_HANDOVER, Session, context_of, event, gate_lines, refused_once, rotation,
                      settings_file, snapshot, start)

sys.path.insert(0, str(PKG / "hooks"))

import hook_runtime
import rotation_state
import session_start

SECRET = "the handover of another project"
OLD_COPIES = ["2026010%dT120000000000Z-sess-%d.md" % (day, day) for day in range(1, 4)]


def saved(tmp_path, project, session_id="sess-a"):
    """A session of this `claude` process with a current handover in `project`."""
    origin = Session(tmp_path, project, session_id)
    origin.write_handover()
    origin.finish_turn()
    assert origin.marker("ready").exists()
    return origin


# --- .claude/rotation is a symlink to another project's -------------------------

@pytest.fixture
def linked(tmp_path, project):
    """Project B holds a current handover of this process; project A's .claude/rotation points to B's."""
    other = tmp_path / "other"
    other.mkdir()
    origin = saved(tmp_path, other)
    (project / ".claude").mkdir()
    rotation(project).symlink_to(rotation(other))
    return other, origin, snapshot(rotation(other))


def test_a_clear_does_not_take_the_handover_a_symlinked_rotation_points_to(project, linked):
    other, origin, before = linked
    proc = start(project, "sess-b")
    assert proc.stdout == "", "the handover of the other project was injected"
    assert snapshot(rotation(other)) == before
    assert origin.marker("ready").exists() and origin.handover.read_text() == VALID_HANDOVER
    assert not (rotation(other) / "archive").exists()
    assert not (rotation(other) / "errors.log").exists()
    refused_once(proc, rotation(project))


def resume_in(tmp_path, project, args):
    return Session(tmp_path, project, "sess-c").expand(args)


def prompt_in(tmp_path, project, _args):
    """A prompt whose size cannot be measured, so the guard writes its counters."""
    payload = event("02-UserPromptSubmit", project, tmp_path / "no-transcript.jsonl", "sess-c", prompt="go on")
    return Session(tmp_path, project, "sess-c").hook("context_guard", payload)


def startup_in(tmp_path, project, _args):
    return start(project, "sess-c", source="startup")


@pytest.mark.parametrize("entry,args", [
    (startup_in, None), (resume_in, "resume"), (resume_in, "resume 1"), (prompt_in, None),
], ids=["startup-list", "resume-list", "resume-pick", "prompt-counters"])
def test_no_entry_reads_or_changes_what_a_symlinked_rotation_points_to(tmp_path, project, linked, entry, args):
    other, origin, before = linked
    proc = entry(tmp_path, project, args)
    assert proc.stdout == "", "the other project's storage was used: %s" % proc.stdout
    assert snapshot(rotation(other)) == before
    refused_once(proc, rotation(project))


# --- the archive or the state directory is a symlink ---------------------------------

def test_a_clear_writes_no_copy_through_a_symlinked_archive(tmp_path, project):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    origin = saved(tmp_path, project)
    (rotation(project) / "archive").symlink_to(elsewhere)
    proc = start(project, "sess-b")
    assert proc.stdout == "", "the handover was delivered through the symlinked archive"
    assert list(elsewhere.iterdir()) == []
    assert origin.marker("ready").exists() and origin.handover.read_text() == VALID_HANDOVER
    assert not list(rotation(project).glob("claimed-*.json"))
    assert not (rotation(project) / "errors.log").exists()
    refused_once(proc, rotation(project) / "archive")


def test_resume_does_not_read_a_symlinked_archive(tmp_path, project):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "20260101T120000000000Z-sess-p.md").write_text(SECRET)
    rotation(project).mkdir(parents=True)
    (rotation(project) / "archive").symlink_to(elsewhere)
    proc = resume_in(tmp_path, project, "resume 1")
    assert SECRET not in proc.stdout
    assert not (rotation(project) / "injections.jsonl").exists()
    refused_once(proc, rotation(project) / "archive")


def test_a_prompt_writes_no_counters_through_a_symlinked_state_directory(tmp_path, project):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    rotation(project).mkdir(parents=True)
    (rotation(project) / "state").symlink_to(elsewhere)
    proc = prompt_in(tmp_path, project, None)
    assert list(elsewhere.iterdir()) == []
    refused_once(proc, rotation(project) / "state")


def test_a_symlinked_dot_claude_is_followed_once(tmp_path, project):
    real = tmp_path / "real-claude"
    real.mkdir()
    (project / ".claude").symlink_to(real)
    saved(tmp_path, project)
    proc = start(project, "sess-b")
    assert VALID_HANDOVER in context_of(proc)
    assert gate_lines(proc) == []
    # The archive copy, and the original moved off the handover path under a name of its own.
    assert sorted(p.suffix for p in (real / "rotation" / "archive").iterdir()) == [".md", ".original"]


# --- no SessionStart path raises ------------------------------------------------------

def run_main(monkeypatch, payload):
    """The SessionStart entry in this process, as Claude Code runs it: its exit status."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    with pytest.raises(SystemExit) as done:
        hook_runtime.main(session_start.run)
    return done.value.code


def clear_payload(project):
    return event("17-SessionStart-clear", project, project / "t.jsonl", "sess-b", source="clear")


def emitted(capsys):
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else None


def errors_log(project):
    path = rotation(project) / "errors.log"
    return path.read_text() if path.exists() else ""


def after(monkeypatch, moment, action):
    """Run `action` right after the handover is emitted, or right after its `delivered` line is written."""
    owner, name = (hook_runtime, "emit") if moment == "emit" else (rotation_state, "log_delivery")
    real = getattr(owner, name)

    def then_act(*args):
        result = real(*args)
        action()
        return result

    monkeypatch.setattr(owner, name, then_act)


# After the emit the `delivered` line fails first, and the claim then stays without a cleanup;
# after that line the cleanup of the claim is what fails.
MOMENTS = pytest.mark.parametrize("moment,logged", [
    ("emit", "log the delivery of generation"), ("delivered", "cleanup of"),
], ids=["after-the-emit", "after-the-delivered-line"])


@MOMENTS
def test_a_cleanup_that_cannot_open_the_lock_after_the_emit_still_exits_0(tmp_path, project, monkeypatch, capsys,
                                                                        moment, logged):
    origin = saved(tmp_path, project)
    lock = rotation(project) / ".lock"

    def the_lock_becomes_a_directory():
        lock.unlink()
        lock.mkdir()

    after(monkeypatch, moment, the_lock_becomes_a_directory)
    code = run_main(monkeypatch, clear_payload(project))
    assert code == 0, "the hook failed after it emitted the handover"
    assert VALID_HANDOVER in emitted(capsys)
    [claimed] = rotation(project).glob("claimed-sess-a-*.json")  # the unfinished claim stays
    assert not origin.handover.exists()  # it moved into the archive before the emit
    assert Path(json.loads(claimed.read_text())["archive"]).read_text() == VALID_HANDOVER
    assert logged in errors_log(project)


@MOMENTS
def test_a_cleanup_that_times_out_on_the_lock_after_the_emit_still_exits_0(tmp_path, project, monkeypatch, capsys,
                                                                         moment, logged):
    saved(tmp_path, project)
    real_acquire = rotation_state._acquire

    def held(descriptor, path, timeout):
        if os.path.basename(path) != ".lock":  # another process holds the project lock, not the log's
            return real_acquire(descriptor, path, timeout)
        raise rotation_state.LockTimeout("%s was held" % path)

    after(monkeypatch, moment, lambda: monkeypatch.setattr(rotation_state, "_acquire", held))
    assert run_main(monkeypatch, clear_payload(project)) == 0
    assert VALID_HANDOVER in emitted(capsys)
    assert list(rotation(project).glob("claimed-sess-a-*.json"))
    assert logged in errors_log(project)


def fill_old_copies(project):
    directory = rotation(project) / "archive"
    directory.mkdir(parents=True, exist_ok=True)
    for name in OLD_COPIES:
        (directory / name).write_text("old")
    return directory


def refuse_archive_unlinks(monkeypatch):
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        if dir_fd is not None and rotation_state.ARCHIVE_NAME.fullmatch(os.path.basename(str(path))):
            raise PermissionError(errno.EACCES, "read-only archive", str(path))
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)


def lock_times_out_once(monkeypatch):
    real_acquire = rotation_state._acquire
    calls = []

    def acquire(descriptor, path, timeout):
        if os.path.basename(path) == ".lock":
            calls.append(path)
        if len(calls) == 1 and os.path.basename(path) == ".lock":
            raise rotation_state.LockTimeout("%s was held" % path)
        return real_acquire(descriptor, path, timeout)

    monkeypatch.setattr(rotation_state, "_acquire", acquire)


@pytest.mark.parametrize("failure", [refuse_archive_unlinks, lock_times_out_once], ids=["unlink-refused", "lock-held"])
def test_a_trim_that_fails_does_not_fail_the_start(tmp_path, project, monkeypatch, capsys, failure):
    settings_file(project, archive_keep=1)
    directory = fill_old_copies(project)
    saved(tmp_path, project)
    failure(monkeypatch)
    assert run_main(monkeypatch, clear_payload(project)) == 0
    assert VALID_HANDOVER in emitted(capsys), "the trim failure skipped the injection"
    assert all((directory / name).exists() for name in OLD_COPIES)
    assert "trim" in errors_log(project)


def test_an_archive_that_cannot_be_written_leaves_the_claim_and_lists_it(tmp_path, project, monkeypatch, capsys):
    origin = saved(tmp_path, project)
    real_link = os.link

    def link(source, target, **kwargs):
        if rotation_state.ARCHIVE_NAME.fullmatch(os.path.basename(str(target))):
            raise OSError(errno.ENOSPC, "no space left", str(target))
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(os, "link", link)
    assert run_main(monkeypatch, clear_payload(project)) == 0
    monkeypatch.setattr(os, "link", real_link)
    out = json.loads(capsys.readouterr().out)
    assert "hookSpecificOutput" not in out, "a handover without its archive copy was emitted"
    assert "claim did not finish" in out["systemMessage"]
    assert list(rotation(project).glob("claimed-sess-a-*.json"))
    assert origin.handover.read_text() == VALID_HANDOVER
    assert "no space left" in errors_log(project)
