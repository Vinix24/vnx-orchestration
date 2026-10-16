"""SessionStart: injection after /clear, listing, recovery and the archive (loop step 8)."""
from __future__ import annotations

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
    path = rotation(project) / "injections.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


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
    assert "outdated (it is older than 60 minutes)" in message_of(proc)
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
    assert claimed.name == "claimed-sess-a-sess-b.json"
    assert s.handover.read_text() == VALID_HANDOVER
    assert len(archived(project)) == (0 if point == "after claim" else 1)
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


def test_a_failed_cleanup_after_the_emit_keeps_the_injection(tmp_path, project, monkeypatch, capsys):
    saved_session(tmp_path, project)

    def refuse(path):
        raise PermissionError("read-only")

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", refuse)
        run_in_process(project, "sess-b")
    out = json.loads(capsys.readouterr().out)
    assert VALID_HANDOVER in out["hookSpecificOutput"]["additionalContext"]
    assert "cleanup of" in (rotation(project) / "errors.log").read_text()


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
    assert VALID_HANDOVER in context_of(user.expand("resume 1"))
    assert markers(project) == []
    assert not other.handover.exists()
    assert injections(project)[0]["origin_session_id"] == "sess-other"
    # The origin's process can no longer inject it after a clear.
    assert context_of(start(project, "sess-d", pid=other_pid)) is None


def test_resume_of_an_archived_handover_injects_it_again(tmp_path, project):
    saved_session(tmp_path, project)
    start(project, "sess-b")
    proc = Session(tmp_path, project, "sess-c").expand("resume 1")
    assert VALID_HANDOVER in context_of(proc)
    assert [line["via"] for line in injections(project)] == ["clear", "resume"]
    assert len(archived(project)) == 1


def test_resume_of_an_outdated_handover_carries_a_warning(tmp_path, project, other_pid):
    s = saved_session(tmp_path, project, "sess-other", pid=other_pid)
    s.prompt("more")
    context = context_of(Session(tmp_path, project, "sess-c").expand("resume 1"))
    assert "marked outdated (a prompt after it)" in context
    assert VALID_HANDOVER in context


def test_a_deleted_handover_leaves_the_list(tmp_path, project, other_pid):
    s = saved_session(tmp_path, project, "sess-other", pid=other_pid)
    assert "sess-other" in message_of(start(project, "sess-b", source="startup"))
    s.handover.unlink()
    assert start(project, "sess-c", source="startup").stdout == ""
    assert "There are no handovers" in context_of(Session(tmp_path, project, "sess-d").expand("resume"))


@pytest.mark.parametrize("args,expected", [
    ("resume 7", "There is no handover number 7."),
    ("resume x", "with a number from the list"),
    ("resume", "There are no handovers in"),
])
def test_resume_answers_a_bad_pick(tmp_path, project, args, expected):
    assert expected in context_of(Session(tmp_path, project, "sess-c").expand(args))
