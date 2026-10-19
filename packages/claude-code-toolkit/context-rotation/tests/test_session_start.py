"""SessionStart: injection after /clear, listing, recovery and the archive (loop step 8)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from conftest import (
    HOOKS, PKG, VALID_HANDOVER, Session, b1_entry, context_of, event, message_of, rotation, settings_file, start,
)

# lib is on sys.path through conftest; the hook entry is imported to run it in-process.
sys.path.insert(0, str(PKG / "hooks"))

import context_guard
import hook_runtime
import rotation_state
import session_start


def saved_session(tmp_path, project, session_id="sess-a", pid=None, text=VALID_HANDOVER):
    s = Session(tmp_path, project, session_id, pid=pid)
    s.write_handover(text)
    s.finish_turn()
    assert s.marker("ready").exists()
    return s


def injections(project):
    """The injection lines of injections.jsonl; the other transitions have lines there too."""
    path = rotation(project) / "injections.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [line for line in lines if line["event"] == "injected"]


def archived(project):
    directory = rotation(project) / "archive"
    return sorted(directory.glob("*.md")) if directory.exists() else []


def markers(project):
    return sorted(p.name for p in rotation(project).glob("*.json"))


# --- injection after /clear ----------------------------------------------

def test_clear_injects_the_handover_of_the_same_process(tmp_path, project):
    s = saved_session(tmp_path, project)
    proc = start(project, "sess-b")
    doc = json.loads(proc.stdout)
    spec = doc["hookSpecificOutput"]
    assert spec["hookEventName"] == "SessionStart"
    assert VALID_HANDOVER in spec["additionalContext"]
    assert 'Continue from step 1 under "## Next steps"' in spec["additionalContext"]
    [copy] = archived(project)
    assert copy.read_text() == VALID_HANDOVER
    assert str(copy) in spec["additionalContext"]
    assert doc["systemMessage"] == (
        "context-rotation: continued from the handover of the previous session (%s)." % copy)
    [line] = injections(project)
    assert (line["via"], line["origin_session_id"], line["session_id"]) == ("clear", "sess-a", "sess-b")
    assert (line["archive"], line["handover"], line["mode"]) == (str(copy), str(s.handover), "inline")
    assert markers(project) == []
    assert not s.handover.exists()


def test_double_injection_does_not_happen(tmp_path, project):
    saved_session(tmp_path, project)
    assert context_of(start(project, "sess-b"))
    again = start(project, "sess-c")
    assert context_of(again) is None
    assert message_of(again) is None
    assert len(injections(project)) == 1


def test_two_sessions_one_handover_only_the_same_process_gets_it(tmp_path, project, other_pid):
    saved_session(tmp_path, project)
    elsewhere = start(project, "sess-x", pid=other_pid)
    assert context_of(elsewhere) is None
    notice = message_of(elsewhere)
    assert "1 handover(s) were not continued automatically" in notice
    assert "ready, written at" in notice and "by session sess-a" in notice
    assert "/context-rotation:rotate resume" in notice
    assert VALID_HANDOVER in context_of(start(project, "sess-b"))


def test_two_handovers_of_one_process_inject_nothing_and_list_both(tmp_path, project):
    saved_session(tmp_path, project, "sess-a1")
    saved_session(tmp_path, project, "sess-a2")
    proc = start(project, "sess-b")
    assert context_of(proc) is None
    assert "2 handover(s) were not continued automatically" in message_of(proc)
    assert markers(project) == ["ready-sess-a1.json", "ready-sess-a2.json"]
    assert injections(project) == []


def test_an_outdated_handover_is_listed_not_injected(tmp_path, project):
    s = saved_session(tmp_path, project)
    s.prompt("one more thing")
    proc = start(project, "sess-b")
    assert context_of(proc) is None
    assert "outdated (a prompt after it)" in message_of(proc)


def test_an_over_age_handover_is_outdated_and_listed(tmp_path, project):
    s = saved_session(tmp_path, project)
    marker = json.loads(s.marker("ready").read_text())
    marker["written_at"] = (datetime.now(timezone.utc) - timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    s.marker("ready").write_text(json.dumps(marker))
    proc = start(project, "sess-b")
    assert context_of(proc) is None
    assert "outdated (it is older than handover_max_age_minutes allows)" in message_of(proc)
    assert markers(project) == ["outdated-sess-a.json"]


def test_a_stale_handover_of_another_process_is_listed(tmp_path, project, other_pid):
    saved_session(tmp_path, project, "sess-old", pid=other_pid)
    assert "by session sess-old" in message_of(start(project, "sess-new", source="startup"))


def test_a_handover_changed_after_its_check_is_not_injected(tmp_path, project):
    s = saved_session(tmp_path, project)
    s.handover.write_text(VALID_HANDOVER + "\nEdited outside the session.\n")
    proc = start(project, "sess-b")
    assert context_of(proc) is None
    assert "the handover file changed after it was checked" in message_of(proc)


def test_work_in_the_old_transcript_after_the_marker_blocks_injection(tmp_path, project):
    s = saved_session(tmp_path, project)
    s.append(b1_entry("queued_command", prompt="wait"))
    assert context_of(start(project, "sess-b")) is None
    assert markers(project) == ["outdated-sess-a.json"]


def test_without_claude_pid_nothing_is_injected(tmp_path, project):
    saved_session(tmp_path, project)
    proc = start(project, "sess-b", drop=("CLAUDE_PID",))
    assert context_of(proc) is None
    assert "not continued automatically" in message_of(proc)


def test_compact_neither_injects_nor_lists(tmp_path, project):
    saved_session(tmp_path, project)
    proc = start(project, "sess-a", source="compact")
    assert (proc.returncode, proc.stdout) == (0, "")


def test_startup_lists_but_never_injects(tmp_path, project):
    saved_session(tmp_path, project)
    proc = start(project, "sess-b", source="startup")
    assert context_of(proc) is None
    assert "not continued automatically" in message_of(proc)


def test_a_session_does_not_list_its_own_handover(tmp_path, project):
    saved_session(tmp_path, project)
    assert start(project, "sess-a", source="resume").stdout == ""


def test_nothing_in_a_project_without_handovers(tmp_path, project):
    proc = start(project, "sess-b")
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not (project / ".claude").exists()


def test_enabled_false_does_nothing(tmp_path, project):
    saved_session(tmp_path, project)
    settings_file(project, enabled=False)
    assert start(project, "sess-b").stdout == ""
    assert markers(project) == ["ready-sess-a.json"]


def test_inside_a_subagent_nothing_happens(tmp_path, project):
    saved_session(tmp_path, project)
    payload = event("17-SessionStart-clear", project, project / "t.jsonl", "sess-b", agent_id="agent-1")
    proc = subprocess.run([sys.executable, str(HOOKS / "session_start.py")], input=json.dumps(payload),
                          capture_output=True, text=True, env=dict(os.environ, CLAUDE_PID=str(os.getpid())))
    assert proc.stdout == ""


def test_an_oversized_handover_is_injected_as_a_pointer(tmp_path, project):
    settings_file(project, handover_max_chars=20000)
    text = VALID_HANDOVER + "\n" + ("detail line\n" * 900)
    saved_session(tmp_path, project, text=text)
    context = context_of(start(project, "sess-b"))
    [copy] = archived(project)
    assert len(context) < 9000
    assert "Read this file first" in context and str(copy) in context
    assert text not in context
    assert copy.read_text() == text
    assert injections(project)[0]["mode"] == "pointer"


# --- the claim race --------------------------------------------------------

def test_concurrent_successors_inject_exactly_once(tmp_path, project):
    saved_session(tmp_path, project)
    env = dict(os.environ, CLAUDE_PID=str(os.getpid()))
    procs = []
    for number in range(8):
        payload = event("17-SessionStart-clear", project, project / "t.jsonl", "sess-b%d" % number)
        child = subprocess.Popen([sys.executable, str(HOOKS / "session_start.py")], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        procs.append((child, json.dumps(payload)))
    outputs = [child.communicate(data, timeout=60) for child, data in procs]
    assert [child.returncode for child, _ in procs] == [0] * 8, outputs
    injected = [out for out, _ in outputs if out and "hookSpecificOutput" in json.loads(out)]
    assert len(injected) == 1
    assert len(injections(project)) == 1
    assert len(archived(project)) == 1
    assert markers(project) == []


# --- crash after claim, after archive, before emit -------------------------

class Crash(Exception):
    pass


def crash(*args, **kwargs):
    raise Crash("simulated crash")


CRASH_POINTS = {
    "after claim": (rotation_state, "archive_handover"),
    "after archive": (rotation_state, "log_injection"),
    "before emit": (hook_runtime, "emit"),
}


def claim_name(path):
    """The name a claim has: its origin and the generation of the handover it took."""
    return "claimed-sess-a-%s.json" % json.loads(path.read_text())["generation"]


def run_in_process(project, session_id):
    payload = event("17-SessionStart-clear", project, project / "t.jsonl", session_id)
    env = dict(os.environ, CLAUDE_PID=str(os.getpid()))
    session_start.run(payload, env, hook_runtime.HookRun())


@pytest.mark.parametrize("point", sorted(CRASH_POINTS))
def test_a_crash_between_claim_and_emit_leaves_a_recoverable_handover(tmp_path, project, monkeypatch, point):
    s = saved_session(tmp_path, project)
    module, name = CRASH_POINTS[point]
    with monkeypatch.context() as patch:
        patch.setattr(module, name, crash)
        with pytest.raises(Crash):
            run_in_process(project, "sess-b")
    [claimed] = rotation(project).glob("claimed-*.json")
    assert claimed.name == claim_name(claimed)
    if point == "after claim":
        assert s.handover.read_text() == VALID_HANDOVER and archived(project) == []
    else:  # the original moved into the archive before the emit, as the claim's archive copy
        assert not s.handover.exists()
        assert [copy.read_text() for copy in archived(project)] == [VALID_HANDOVER]
        assert json.loads(claimed.read_text())["archive"] == str(archived(project)[0])
    assert len(injections(project)) == (1 if point == "before emit" else 0)

    # At most one automatic injection: a later clear injects nothing and lists the claim.
    later = start(project, "sess-c")
    assert context_of(later) is None
    assert "claim did not finish" in message_of(later)

    # Recovery is manual: resume lists it and injects it on request.
    user = Session(tmp_path, project, "sess-c")
    listing = context_of(user.expand("resume"))
    assert "1. claim did not finish" in listing and str(s.handover) in listing
    proc = user.expand("resume 1")
    assert VALID_HANDOVER in context_of(proc)
    assert message_of(proc).startswith("context-rotation: continued from ")
    assert not claimed.exists()
    assert not s.handover.exists()
    assert injections(project)[-1]["via"] == "resume"


NEWER = VALID_HANDOVER + "\nWritten again by the origin session.\n"


def sha_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_recovery_reads_the_archived_snapshot_not_a_changed_original(tmp_path, project, monkeypatch):
    s = saved_session(tmp_path, project)
    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "log_injection", crash)
        with pytest.raises(Crash):
            run_in_process(project, "sess-b")
    [copy] = archived(project)
    assert copy.read_text() == VALID_HANDOVER
    s.write_handover(NEWER)  # the original changes after the crash, with its own marker

    proc = Session(tmp_path, project, "sess-c").resume(1)
    assert VALID_HANDOVER in context_of(proc)
    assert "Written again by the origin session." not in context_of(proc)
    assert copy.read_text() == VALID_HANDOVER
    [line] = injections(project)
    assert (line["archive"], line["sha256"]) == (str(copy), sha_of(copy))
    # The changed original is a newer handover: it stays, with its own marker.
    assert s.handover.read_text() == NEWER
    assert markers(project) == ["ready-sess-a.json"]


def test_a_claims_archive_copy_survives_the_trim_and_resume_injects_it(tmp_path, project, monkeypatch):
    settings_file(project, archive_keep=2)
    s = saved_session(tmp_path, project)
    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "log_injection", crash)
        with pytest.raises(Crash):
            run_in_process(project, "sess-b")
    [copy] = archived(project)
    s.write_handover(NEWER)  # the original no longer matches claimed_sha256
    for day in range(1, 4):  # archive_keep + 1 newer archived handovers
        moment = datetime(2099, 1, day, tzinfo=timezone.utc)
        rotation_state.archive_handover(
            str(project), rotation_state.plan_archive(str(project), "sess-n%d" % day, moment), b"newer")
    start(project, "sess-last", source="compact")
    assert copy.exists() and copy.read_text() == VALID_HANDOVER
    assert len(archived(project)) == 3

    proc = Session(tmp_path, project, "sess-c").resume(1)
    assert context_of(proc).count(VALID_HANDOVER) == 1
    assert "Written again by the origin session." not in context_of(proc)
    last = injections(project)[-1]
    assert (last["via"], last["archive"], last["sha256"]) == ("resume", str(copy), sha_of(copy))


def test_a_claim_without_archive_is_not_recovered_from_a_changed_original(tmp_path, project, monkeypatch):
    s = saved_session(tmp_path, project)
    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "archive_handover", crash)
        with pytest.raises(Crash):
            run_in_process(project, "sess-b")
    s.handover.write_text(NEWER)  # changed outside the tools: no marker of its own

    proc = Session(tmp_path, project, "sess-c").resume(1)
    assert "Written again by the origin session." not in (context_of(proc) or "")
    assert s.handover.read_text() == NEWER
    assert archived(project) == []
    assert injections(project) == []


def test_a_changed_archive_copy_is_neither_used_nor_overwritten(tmp_path, project, monkeypatch):
    s = saved_session(tmp_path, project)
    with monkeypatch.context() as patch:
        patch.setattr(rotation_state, "log_injection", crash)
        with pytest.raises(Crash):
            run_in_process(project, "sess-b")
    [copy] = archived(project)
    copy.write_text("edited by hand\n")
    user = Session(tmp_path, project, "sess-c")
    [claimed] = rotation(project).glob("claimed-*.json")

    lines = context_of(user.expand("resume")).splitlines()
    assert any("not resumable" in line and claimed.name in line
               and "does not hold the claimed handover" in line for line in lines), "the claim is listed as resumable"
    assert lines[1].startswith("1. archived")
    # Number 1 is the edited archive file, offered as what it is; the claim is not touched.
    assert VALID_HANDOVER not in context_of(user.expand("resume 1"))
    assert copy.read_text() == "edited by hand\n"
    assert not s.handover.exists()  # the original became the archive copy before the crash
    assert [line["handover"] for line in injections(project)] == [None]
    assert markers(project) == [claimed.name]


def test_a_handover_rewritten_between_its_check_and_the_claim_is_not_injected(tmp_path, project, monkeypatch):
    s = saved_session(tmp_path, project)
    real_matches = session_start.current_matches

    def matches_then_the_origin_writes(*args):
        found = real_matches(*args)
        s.handover.write_text(NEWER)  # the Write tool writes without the project lock
        return found

    with monkeypatch.context() as patch:
        patch.setattr(session_start, "current_matches", matches_then_the_origin_writes)
        run_in_process(project, "sess-b")
    assert injections(project) == [] and archived(project) == []
    assert s.handover.read_text() == NEWER
    assert markers(project) == ["ready-sess-a.json"]


@pytest.mark.parametrize("via", ["clear", "resume"])
def test_a_newer_handover_written_during_delivery_survives_the_cleanup(tmp_path, project, monkeypatch, other_pid, via):
    pid = os.getpid() if via == "clear" else other_pid
    s = saved_session(tmp_path, project, pid=pid)
    real_emit = hook_runtime.emit

    def emit_while_the_origin_writes_again(document):
        real_emit(document)
        s.write_handover(NEWER)

    with monkeypatch.context() as patch:
        patch.setattr(hook_runtime, "emit", emit_while_the_origin_writes_again)
        if via == "clear":
            run_in_process(project, "sess-b")
        else:
            user = Session(tmp_path, project, "sess-b")
            ident = "ready-sess-a-%s" % json.loads(s.marker("ready").read_text())["generation"]
            context_guard.run(user.payload(
                "09-UserPromptExpansion-plugin-skill", command_name="context-rotation:rotate",
                command_args="resume " + ident), dict(os.environ, CLAUDE_PID=str(os.getpid())),
                hook_runtime.HookRun())
    [copy] = archived(project)
    assert copy.read_text() == VALID_HANDOVER
    assert s.handover.exists(), "the cleanup deleted the newer handover"
    assert s.handover.read_text() == NEWER
    assert json.loads(s.marker("ready").read_text())["sha256"] == sha_of(s.handover)
    assert not list(rotation(project).glob("claimed-*.json"))
    # The newer handover is injected by the next clear of the origin's process.
    assert "Written again by the origin session." in context_of(start(project, "sess-d", pid=pid))


def test_a_failed_cleanup_after_the_emit_keeps_the_injection(tmp_path, project, monkeypatch, capsys):
    s = saved_session(tmp_path, project)
    real_unlink = os.unlink

    def refuse(path, dir_fd=None):
        if os.path.basename(str(path)) == s.handover.name or os.path.basename(str(path)).startswith("claimed-"):
            raise PermissionError("read-only")
        real_unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", refuse)
        run_in_process(project, "sess-b")
    out = json.loads(capsys.readouterr().out)
    assert VALID_HANDOVER in out["hookSpecificOutput"]["additionalContext"]
    assert "cleanup of" in (rotation(project) / "errors.log").read_text()


def journal_events(project):
    path = rotation(project) / "injections.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_finished_delivery_has_its_line_after_the_injection(tmp_path, project):
    saved_session(tmp_path, project)
    assert VALID_HANDOVER in context_of(start(project, "sess-b"))
    lines = journal_events(project)
    assert [line["event"] for line in lines][-2:] == ["injected", "delivered"]
    injected, delivered = lines[-2:]
    assert (delivered["via"], delivered["origin_session_id"], delivered["session_id"]) == ("clear", "sess-a", "sess-b")
    assert delivered["generation"] == injected["generation"]
    assert set(delivered) == {"event", "at", "via", "origin_session_id", "generation", "session_id"}


def test_an_emit_that_fails_leaves_no_delivery_line_and_the_claim(tmp_path, project, monkeypatch):
    saved_session(tmp_path, project)

    def broken(document):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(hook_runtime, "emit", broken)
    with pytest.raises(BrokenPipeError):
        run_in_process(project, "sess-b")
    assert [line["event"] for line in journal_events(project)][-1] == "injected"
    assert list(rotation(project).glob("claimed-*.json"))


def test_a_delivery_line_that_cannot_be_written_keeps_the_injection(tmp_path, project, monkeypatch, capsys):
    saved_session(tmp_path, project)
    real = rotation_state.journal

    def journal(project_dir, event_name, **fields):
        if event_name == "delivered":
            raise PermissionError(13, "Permission denied")
        return real(project_dir, event_name, **fields)

    monkeypatch.setattr(rotation_state, "journal", journal)
    run_in_process(project, "sess-b")
    out = json.loads(capsys.readouterr().out)
    assert VALID_HANDOVER in out["hookSpecificOutput"]["additionalContext"]
    assert "delivered" not in [line["event"] for line in journal_events(project)]
    assert "log the delivery" in (rotation(project) / "errors.log").read_text()


# --- the archive ---------------------------------------------------------------

def test_the_archive_is_trimmed_at_every_start(tmp_path, project):
    settings_file(project, archive_keep=3)
    directory = rotation(project) / "archive"
    directory.mkdir(parents=True)
    names = ["2026010%dT120000000000Z-sess-%d.md" % (day, day) for day in range(1, 6)]
    for name in names:
        (directory / name).write_text("old")
    (directory / "notes.txt").write_text("not ours")
    proc = start(project, "sess-b", source="startup")
    assert proc.returncode == 0
    assert sorted(p.name for p in directory.iterdir()) == sorted(names[2:] + ["notes.txt"])


def fill_archive(directory, count):
    directory.mkdir(parents=True, exist_ok=True)
    names = ["2026010%dT120000000000Z-sess-%d.md" % (day, day) for day in range(1, count + 1)]
    for name in names:
        (directory / name).write_text("old")
    return names


def other_project_archive(tmp_path):
    other = tmp_path / "other"
    names = fill_archive(other / ".claude" / "rotation" / "archive", 6)
    return other, names


def test_a_symlinked_archive_directory_is_not_trimmed(tmp_path, project, capfd):
    settings_file(project, archive_keep=2)
    other, names = other_project_archive(tmp_path)
    rotation(project).mkdir(parents=True)
    (rotation(project) / "archive").symlink_to(other / ".claude" / "rotation" / "archive")
    proc = start(project, "sess-b", source="startup")
    assert proc.returncode == 0
    assert sorted(p.name for p in (other / ".claude" / "rotation" / "archive").iterdir()) == sorted(names)
    assert proc.stderr.count("%s is a symlink; nothing is read or changed" % (rotation(project) / "archive")) == 1
    assert not (rotation(project) / "errors.log").exists()


def test_a_symlinked_rotation_directory_is_not_trimmed(tmp_path, project):
    settings_file(project, archive_keep=2)
    other, names = other_project_archive(tmp_path)
    (project / ".claude").mkdir(exist_ok=True)
    rotation(project).symlink_to(other / ".claude" / "rotation")
    proc = start(project, "sess-b", source="startup")
    assert proc.returncode == 0
    assert sorted(p.name for p in (other / ".claude" / "rotation" / "archive").iterdir()) == sorted(names)
    assert proc.stderr.count("%s is a symlink; nothing is read or changed" % rotation(project)) == 1


def test_a_symlinked_dot_claude_still_trims_its_real_archive(tmp_path, project):
    real = tmp_path / "real-claude"
    names = fill_archive(real / "rotation" / "archive", 5)
    (real / "context-rotation.json").write_text(json.dumps({"archive_keep": 2}))
    (project / ".claude").symlink_to(real)
    assert start(project, "sess-b", source="startup").returncode == 0
    assert sorted(p.name for p in (real / "rotation" / "archive").iterdir()) == names[3:]


def test_injections_keep_only_the_newest_archive_copies(tmp_path, project):
    settings_file(project, archive_keep=2)
    for number in range(4):
        saved_session(tmp_path, project, "sess-%d" % number)
        assert context_of(start(project, "sess-next-%d" % number))
    start(project, "sess-last", source="compact")
    kept = archived(project)
    assert len(kept) == 2
    assert [p.name.split("Z-", 1)[1] for p in kept] == ["sess-2.md", "sess-3.md"]
    assert len(injections(project)) == 4


# --- resume via the typed command ------------------------------------------------

def test_resume_lists_pending_and_archived_handovers(tmp_path, project, other_pid):
    saved_session(tmp_path, project, "sess-a")
    start(project, "sess-b")
    saved_session(tmp_path, project, "sess-other", pid=other_pid)
    listing = context_of(Session(tmp_path, project, "sess-c").expand("resume"))
    lines = listing.splitlines()
    assert lines[1].startswith("1. ready, written at") and "sess-other" in lines[1]
    assert lines[2].startswith("2. archived, continued in session sess-b at")
    assert "/context-rotation:rotate resume <number>" in lines[-1]


def test_resume_of_a_pending_handover_claims_it(tmp_path, project, other_pid):
    other = saved_session(tmp_path, project, "sess-other", pid=other_pid)
    user = Session(tmp_path, project, "sess-c")
    assert VALID_HANDOVER in context_of(user.resume(1))
    assert markers(project) == []
    assert not other.handover.exists()
    assert injections(project)[0]["origin_session_id"] == "sess-other"
    # The origin's process can no longer inject it after a clear.
    assert context_of(start(project, "sess-d", pid=other_pid)) is None


def test_resume_of_an_archived_handover_injects_it_again(tmp_path, project):
    saved_session(tmp_path, project)
    start(project, "sess-b")
    proc = Session(tmp_path, project, "sess-c").resume(1)
    assert VALID_HANDOVER in context_of(proc)
    assert [line["via"] for line in injections(project)] == ["clear", "resume"]
    assert len(archived(project)) == 1


def test_resume_of_an_outdated_handover_carries_a_warning(tmp_path, project, other_pid):
    s = saved_session(tmp_path, project, "sess-other", pid=other_pid)
    s.prompt("more")
    context = context_of(Session(tmp_path, project, "sess-c").resume(1))
    assert "marked outdated (a prompt after it)" in context
    assert VALID_HANDOVER in context


def test_a_deleted_handover_leaves_the_list(tmp_path, project, other_pid):
    s = saved_session(tmp_path, project, "sess-other", pid=other_pid)
    assert "sess-other" in message_of(start(project, "sess-b", source="startup"))
    s.handover.unlink()
    assert start(project, "sess-c", source="startup").stdout == ""
    assert "There are no handovers" in context_of(Session(tmp_path, project, "sess-d").expand("resume"))


@pytest.mark.parametrize("args,expected", [
    ("resume 7", "There is no handover with that number."),
    ("resume x", "Type /context-rotation:rotate resume <number> or resume <id>"),
    ("resume", "There are no handovers in"),
])
def test_resume_answers_a_bad_pick(tmp_path, project, args, expected):
    assert expected in context_of(Session(tmp_path, project, "sess-c").expand(args))
