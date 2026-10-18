"""Every handover generation has its own identity, and nothing in .claude/rotation is overwritten.

A generation is one handover the plugin marked ready. Its claim, its entry in the
`resume` list and the archive copy it ends in all name it, so a later handover of
the same session never takes its place (codex and deepseek on #2080, round 7):

- a second claim of the same session never replaces an unfinished one;
- a move of a marker or of the original handover never replaces a file at its target;
- a number or id saved from a list never picks a later handover;
- a file that was ever at the handover path is never deleted by the cleanup, also
  when it is written through a descriptor opened before the cleanup moved it;
- an explicit pick says whether the file still holds the text that was checked.

The fix-forward (OI-2144, codex on #2080's last head) adds two rules:

- the archive copy a claim names is the one written from the claimed bytes and
  never changes; the original moved off the handover path is kept apart under its
  own name, and no claim and no delivery reads it;
- a pick claims only the generation it found: one published in between is refused.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import pytest

from conftest import PKG, VALID_HANDOVER, Session, context_of, event, rotation, settings_file, start

sys.path.insert(0, str(PKG / "hooks"))

import context_guard
import handover_injection
import hook_runtime
import rotation_state
import session_start

FIRST = VALID_HANDOVER + "\nThe first handover of this session.\n"
SECOND = VALID_HANDOVER + "\nThe second handover of this session.\n"
LATE = VALID_HANDOVER + "\nWritten through a descriptor opened before the cleanup.\n"
FOREIGN = "written by another process\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def saved_session(tmp_path, project, session_id="sess-a", pid=None, text=VALID_HANDOVER):
    s = Session(tmp_path, project, session_id, pid=pid)
    s.write_handover(text)
    s.finish_turn()
    assert s.marker("ready").exists()
    return s


def run_clear(project, session_id="sess-b"):
    payload = event("17-SessionStart-clear", project, project / "t.jsonl", session_id)
    session_start.run(payload, dict(os.environ, CLAUDE_PID=str(os.getpid())), hook_runtime.HookRun())


def ready_handover(project, data, session_id="sess-a"):
    handover = rotation_state.handover_path(str(project), session_id)
    os.makedirs(os.path.dirname(handover), exist_ok=True)
    with open(handover, "wb") as handle:
        handle.write(data)
    marker = rotation_state.new_marker(session_id, handover, data, {"CLAUDE_PID": str(os.getpid())}, None, "tool-1")
    rotation_state.mark_ready(str(project), marker)
    return rotation_state.marker_path(str(project), "ready", session_id)


def stored_texts(project):
    """Every handover text on disk: every file in the archive, and the handover files."""
    found = [p.read_bytes() for p in (rotation(project) / "archive").iterdir() if p.is_file()]
    return found + [p.read_bytes() for p in rotation(project).glob("handover-*.md")]


def injected_lines(project):
    lines = (rotation(project) / "injections.jsonl").read_text().splitlines()
    return [record for record in map(json.loads, lines) if record["event"] == "injected"]


def numbered(text):
    return [line for line in (text or "").splitlines() if line[:1].isdigit()]


# --- a claim per generation --------------------------------------------------------

def test_a_second_claim_never_overwrites_an_unfinished_one(project):
    """Delivery of the first claim stops after its archive copy; the session writes again and is claimed again."""
    first = rotation_state.claim(str(project), ready_handover(project, FIRST.encode()), "sess-b")
    rotation_state.archive_handover(str(project), first[1]["archive"], first[2])
    second = rotation_state.claim(str(project), ready_handover(project, SECOND.encode()), "sess-b")
    assert first is not None and second is not None
    claims = [e["marker"]["claimed_sha256"] for e in rotation_state.pending_handovers(str(project), None)
              if e["kind"] == "claimed"]
    assert sorted(claims) == sorted([sha(FIRST.encode()), sha(SECOND.encode())]), "the first claim was overwritten"
    for day in range(1, 4):
        moment = datetime(2099, 1, day, tzinfo=timezone.utc)
        rotation_state.archive_handover(
            str(project), rotation_state.plan_archive(str(project), "sess-n%d" % day, moment), b"newer")
    rotation_state.trim_archive(str(project), 1)
    assert FIRST.encode() in stored_texts(project), "the trim deleted the only copy of the first handover"


# --- no move replaces what is at its target ----------------------------------------

def test_making_a_marker_outdated_never_replaces_a_file_at_the_outdated_name(project):
    ready = ready_handover(project, VALID_HANDOVER.encode())
    left = rotation(project) / "outdated-sess-a.json"
    left.write_text("left on disk by hand\n")
    rotation_state.mark_outdated(str(project), "sess-a", "a prompt after it", notified=False)
    assert left.read_text() == "left on disk by hand\n", "the move to outdated replaced a file"
    assert os.path.exists(ready)
    assert any("nothing replaced" in line for line in rotation_state.error_lines(str(project)))


def test_moving_the_original_never_replaces_a_file_in_the_archive(tmp_path, project, monkeypatch, capsys):
    """The name the cleanup planned for the original is taken before its move (deepseek's residual)."""
    s = saved_session(tmp_path, project)
    planned, real_plan = [], rotation_state.plan_archive

    def plan_then_taken(project_dir, origin, now=None, suffix=".md"):
        path = real_plan(project_dir, origin, now, suffix)
        planned.append(path)
        if len(planned) == 2:  # the claim plans the archive copy, the cleanup the name for the original
            with open(path, "w") as handle:
                handle.write(FOREIGN)
        return path

    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "plan_archive", plan_then_taken)
        run_clear(project)
    assert len(planned) == 2
    assert os.path.exists(planned[1]) and open(planned[1]).read() == FOREIGN, "the move replaced an archive file"
    assert VALID_HANDOVER.encode() in stored_texts(project), "the handover itself was lost"


# --- a saved pick names one generation --------------------------------------------------

def test_a_saved_number_never_picks_a_later_handover_of_the_same_session(tmp_path, project, other_pid):
    origin = saved_session(tmp_path, project, pid=other_pid, text=FIRST)
    reader = Session(tmp_path, project, "sess-r")
    [listed] = numbered(context_of(reader.expand("resume")))
    assert listed.startswith("1. ready") and "sess-a" in listed
    assert "The first handover" in context_of(Session(tmp_path, project, "sess-t").resume(1))  # consumed elsewhere
    origin.write_handover(SECOND)
    origin.finish_turn()
    text = context_of(reader.expand("resume 1"))
    assert "The second handover" not in text, "a saved number picked a later handover"
    assert "is no longer there" in text
    assert numbered(text)[0].startswith("1. ready")


def test_a_pick_never_claims_a_generation_published_after_it_was_found(tmp_path, project, other_pid,
                                                                       monkeypatch, capsys):
    """The origin marks a new generation ready between the lookup of the pick and its claim (codex on #2080)."""
    origin = saved_session(tmp_path, project, pid=other_pid, text=FIRST)
    first = json.loads(origin.marker("ready").read_text())["generation"]
    reader = Session(tmp_path, project, "sess-r")
    real_find, published = handover_injection.find_entry, []

    def found_then_the_origin_writes_again(entries, ident):
        entry = real_find(entries, ident)
        origin.write_handover(SECOND)
        origin.finish_turn()
        published.append(json.loads(origin.marker("ready").read_text())["generation"])
        return entry

    with monkeypatch.context() as patch:
        patch.setattr(handover_injection, "find_entry", found_then_the_origin_writes_again)
        context_guard.run(reader.payload(
            "09-UserPromptExpansion-plugin-skill", command_name="context-rotation:rotate",
            command_args="resume ready-sess-a-" + first), dict(os.environ, CLAUDE_PID=str(os.getpid())),
            hook_runtime.HookRun())
    text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    [second] = published
    assert second != first
    assert "The second handover" not in text, "the pick claimed a generation published after it was found"
    assert "generation %s" % first in text and "generation %s" % second in text
    assert json.loads(origin.marker("ready").read_text())["generation"] == second
    assert not list(rotation(project).glob("claimed-*.json"))
    assert not injected_lines(project)


# --- the cleanup never deletes a file that was at the handover path ------------------

def test_a_write_through_a_descriptor_opened_before_the_cleanup_is_kept(tmp_path, project, monkeypatch, capsys):
    s = saved_session(tmp_path, project)
    writer = os.open(str(s.handover), os.O_WRONLY)  # opened before the cleanup moves the file, without O_TRUNC
    real_check = rotation_state._still_claimed

    def checked_then_written(*args):
        result = real_check(*args)
        os.write(writer, LATE.encode())
        os.ftruncate(writer, len(LATE.encode()))
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(rotation_state, "_still_claimed", checked_then_written)
            run_clear(project)
    finally:
        os.close(writer)
    assert LATE.encode() in stored_texts(project), "the cleanup deleted bytes written through an open descriptor"


# --- the archive copy of a claim never changes ------------------------------------------

def test_a_write_after_the_claim_never_changes_the_archive_a_pointer_names(tmp_path, project):
    """The origin keeps the original open and writes after the claim (codex on #2080, OI-2144)."""
    settings_file(project, handover_max_chars=20000)
    text = FIRST + "detail line\n" * 900
    s = saved_session(tmp_path, project, text=text)
    writer = os.open(str(s.handover), os.O_WRONLY)  # opened before the claim, without O_TRUNC
    try:
        context = context_of(start(project, "sess-b"))
        os.write(writer, LATE.encode())
        os.ftruncate(writer, len(LATE.encode()))
    finally:
        os.close(writer)
    [line] = injected_lines(project)
    assert line["mode"] == "pointer" and "Read this file first" in context and line["archive"] in context
    assert "holds exactly the text that was checked" in context
    with open(line["archive"], "rb") as handle:
        assert handle.read() == text.encode(), "the archive copy the pointer names changed after the claim"
    assert LATE.encode() in stored_texts(project), "the write through the open descriptor was lost"


def test_a_crash_before_the_emit_leaves_the_claimed_bytes_to_resume(tmp_path, project):
    """The origin writes after the claim, and the delivery dies between its archive step and its emit."""
    source = ready_handover(project, FIRST.encode())
    writer = os.open(rotation_state.handover_path(str(project), "sess-a"), os.O_WRONLY)
    claimed, marker, data = rotation_state.claim(str(project), source, "sess-b", as_checked=True)

    def written_then_crash(step):
        if step == "emit handover":
            os.write(writer, LATE.encode())
            os.ftruncate(writer, len(LATE.encode()))
            raise Crash("simulated crash before the emit")

    try:
        with pytest.raises(Crash):
            handover_injection.deliver(str(project), claimed, marker, data, "sess-b", "clear",
                                       lambda text, archive: None, written_then_crash)
    finally:
        os.close(writer)
    left = rotation_state.load_marker(str(project), claimed)
    assert rotation_state.claimed_bytes(str(project), left) == (FIRST.encode(), None)
    text = context_of(Session(tmp_path, project, "sess-r").resume(1))
    assert "The first handover" in text
    assert "Written through a descriptor" not in text, "resume handed over bytes written after the claim"


def test_the_moved_original_is_never_listed_or_named_by_a_claim(tmp_path, project):
    s = saved_session(tmp_path, project)
    run_clear(project)
    archive = rotation(project) / "archive"
    [copy] = [p for p in archive.iterdir() if rotation_state.ARCHIVE_NAME.fullmatch(p.name)]
    [original] = [p for p in archive.iterdir() if p != copy]
    assert original.name.endswith(rotation_state.ORIGINAL_SUFFIX) and not s.handover.exists()
    assert copy.read_text() == original.read_text() == VALID_HANDOVER
    assert [line["archive"] for line in injected_lines(project)] == [str(copy)]
    assert [e["path"] for e in rotation_state.archived_handovers(str(project))] == [str(copy)]


def test_the_trim_keeps_the_newest_moved_originals_too(tmp_path, project):
    for day in range(1, 5):
        moment = datetime(2099, 1, day, tzinfo=timezone.utc)
        for suffix in (".md", rotation_state.ORIGINAL_SUFFIX):
            path = rotation_state.plan_archive(str(project), "sess-%d" % day, moment, suffix)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write("day %d\n" % day)
    assert rotation_state.trim_archive(str(project), 2) == 4
    left = sorted(p.name.split("Z-", 1)[1] for p in (rotation(project) / "archive").iterdir())
    assert left == sorted(["sess-3.md", "sess-4.md", "sess-3.original", "sess-4.original"])


# --- an explicit pick says what it injects --------------------------------------------------

@pytest.mark.parametrize("edited", [False, True], ids=["unchanged", "edited-after-the-check"])
def test_an_explicit_pick_says_whether_the_file_holds_what_was_checked(tmp_path, project, other_pid, edited):
    s = saved_session(tmp_path, project, pid=other_pid, text=FIRST)
    injected = FIRST + ("Edited by hand after the check.\n" if edited else "")
    s.handover.write_text(injected)
    text = context_of(Session(tmp_path, project, "sess-c").resume(1))
    assert "The first handover" in text
    if edited:
        assert "no longer holds the text that was checked" in text
        assert "Edited by hand after the check." in text
    else:
        assert "holds exactly the text that was checked" in text
    [line] = [r for r in map(json.loads, (rotation(project) / "injections.jsonl").read_text().splitlines())
              if r["event"] == "injected"]
    assert line["sha256"] == sha(injected.encode())


def test_the_handover_after_a_clear_says_it_holds_what_was_checked(tmp_path, project, capsys):
    saved_session(tmp_path, project)
    run_clear(project)
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert VALID_HANDOVER in context and "holds exactly the text that was checked" in context


# --- a move that stopped halfway settles on the next attempt -----------------------------

class Crash(Exception):
    pass


def crash_on_unlink_of(monkeypatch, prefix):
    """The process dies between a move's link and its unlink of the old name."""
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        if os.path.basename(str(path)).startswith(prefix):
            raise Crash("simulated crash")
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)


def test_a_generation_is_claimed_once_also_after_a_claim_stopped_halfway(project, monkeypatch):
    source = ready_handover(project, FIRST.encode())
    generation = json.loads(open(source).read())["generation"]
    with monkeypatch.context() as patch:
        crash_on_unlink_of(patch, "ready-")
        with pytest.raises(Crash):
            rotation_state.claim(str(project), source, "sess-b")
    claimed = rotation(project) / ("claimed-sess-a-%s.json" % generation)
    assert os.path.samefile(source, str(claimed)), "the claim did not stop between its link and its unlink"
    assert rotation_state.claim(str(project), source, "sess-c") is None
    assert not os.path.exists(source)
    assert json.loads(claimed.read_text())["claimed_by"] == "sess-b"
    assert [e["kind"] for e in rotation_state.pending_handovers(str(project), None)] == ["claimed"]


def test_a_move_to_outdated_that_stopped_halfway_is_completed(project, monkeypatch):
    source = ready_handover(project, VALID_HANDOVER.encode())
    with monkeypatch.context() as patch:
        crash_on_unlink_of(patch, "ready-")
        with pytest.raises(Crash):
            rotation_state.mark_outdated(str(project), "sess-a", "a prompt after it", notified=False)
    target = rotation(project) / "outdated-sess-a.json"
    assert os.path.samefile(source, str(target))
    assert rotation_state.mark_outdated(str(project), "sess-a", "a tool call after it", notified=False) is not None
    assert not os.path.exists(source)
    assert json.loads(target.read_text())["outdated_reason"] == "a tool call after it"


def test_an_outdated_marker_left_by_a_crash_in_mark_ready_is_superseded(project):
    ready_handover(project, FIRST.encode())
    rotation_state.mark_outdated(str(project), "sess-a", "a prompt after it", notified=True)
    newer = rotation_state.new_marker("sess-a", rotation_state.handover_path(str(project), "sess-a"),
                                      SECOND.encode(), {}, None, None)
    rotation_state.write_json(str(project), rotation_state.marker_path(str(project), "ready", "sess-a"), newer)
    assert rotation_state.mark_outdated(str(project), "sess-a", "a tool call after it", notified=False) is not None
    left = json.loads((rotation(project) / "outdated-sess-a.json").read_text())
    assert (left["generation"], left["outdated_reason"]) == (newer["generation"], "a tool call after it")
    assert not (rotation(project) / "ready-sess-a.json").exists()


def test_a_claim_name_that_holds_something_else_refuses_the_claim(project):
    source = ready_handover(project, VALID_HANDOVER.encode())
    generation = json.loads(open(source).read())["generation"]
    taken = rotation(project) / ("claimed-sess-a-%s.json" % generation)
    taken.write_text("{not a marker")
    assert rotation_state.claim(str(project), source, "sess-b") is None
    assert taken.read_text() == "{not a marker"
    assert os.path.exists(source)
    assert any("holds no valid claim" in line for line in rotation_state.error_lines(str(project)))


def test_a_typed_id_of_a_later_handover_of_the_same_session_is_refused(tmp_path, project, other_pid):
    origin = saved_session(tmp_path, project, pid=other_pid, text=FIRST)
    first_id = "ready-sess-a-%s" % json.loads(origin.marker("ready").read_text())["generation"]
    assert "The first handover" in context_of(Session(tmp_path, project, "sess-t").expand("resume " + first_id))
    origin.write_handover(SECOND)
    origin.finish_turn()
    text = context_of(Session(tmp_path, project, "sess-r").expand("resume " + first_id))
    assert "The second handover" not in text
    assert "Handover %s is no longer there" % first_id in text
