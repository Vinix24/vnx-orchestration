"""The PostToolUse handover detector, run as the real script (loop step 5)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

from conftest import HOOKS, VALID_HANDOVER, Session, context_of, rotation, settings_file


def ready(session):
    return json.loads(session.marker("ready").read_text(encoding="utf-8"))


def test_valid_handover_writes_the_ready_marker(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    proc = s.write_handover()
    assert "valid and marked ready" in context_of(proc)
    assert str(s.handover) in context_of(proc)
    marker = ready(s)
    assert marker["version"] == 1
    assert marker["session_id"] == "sess-a"
    assert marker["handover"] == str(s.handover)
    assert marker["process"]["pid"] == os.getpid()
    assert marker["process"]["start"]
    assert marker["tool_use_id"] == "tool-sess-a-1"
    # The hook ran after the Write call entry and before its result and attachment.
    lines = s.transcript.read_bytes().splitlines(True)
    assert marker["transcript"]["path"] == str(s.transcript)
    assert marker["transcript"]["size"] == sum(len(line) for line in lines[:-2])
    assert marker["transcript"]["last_uuid"] == json.loads(lines[-3])["uuid"]
    assert marker["written_at"].endswith("Z")
    assert marker["announced"] is False
    assert marker["sha256"] == hashlib.sha256(VALID_HANDOVER.encode("utf-8")).hexdigest()


def test_first_write_in_a_project_creates_the_gitignore(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    s.write_handover()
    assert (rotation(project) / ".gitignore").read_text() == "*\n"


def test_missing_running_section_is_named(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    text = VALID_HANDOVER.replace("## Running\nnone\n", "")
    note = context_of(s.write_handover(text))
    assert "not ready yet" in note
    assert '"## Running" section is missing' in note
    assert "Next steps" not in note
    assert not s.marker("ready").exists()


def test_unnumbered_first_step_is_named(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    text = VALID_HANDOVER.replace("1. Run the parser", "- Run the parser")
    note = context_of(s.write_handover(text))
    assert 'not a step numbered "1."' in note
    assert "Running" not in note
    assert not s.marker("ready").exists()


def test_missing_next_steps_is_named(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    text = VALID_HANDOVER.replace("## Next steps\n", "")
    assert '"## Next steps" section is missing' in context_of(s.write_handover(text))


def test_oversized_handover_is_refused_with_its_length(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    text = VALID_HANDOVER + "x" * 8000
    note = context_of(s.write_handover(text))
    assert "%d characters long and the limit is 8000" % len(text) in note
    assert not s.marker("ready").exists()


def test_the_limit_is_a_setting(tmp_path, project):
    settings_file(project, handover_max_chars=200)
    s = Session(tmp_path, project, "sess-a")
    assert "limit is 200" in context_of(s.write_handover())


def test_an_invalid_rewrite_outdates_the_earlier_marker(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    s.write_handover()
    s.write_handover("nothing useful")
    assert not s.marker("ready").exists()
    outdated = json.loads(s.marker("outdated").read_text())
    assert outdated["outdated_reason"] == "the handover was rewritten and is not valid"


def test_a_valid_rewrite_replaces_an_outdated_marker(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    s.write_handover()
    s.prompt("one more thing")
    assert s.marker("outdated").exists()
    s.write_handover(VALID_HANDOVER + "\nUpdated.\n")
    assert s.marker("ready").exists()
    assert not s.marker("outdated").exists()


def test_edit_counts_like_write(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    assert "valid and marked ready" in context_of(s.write_handover(tool="Edit"))


def test_other_files_are_ignored(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    other = project / "notes.md"
    other.write_text(VALID_HANDOVER)
    proc = s.hook("handover_detector", s.payload(
        "07-PostToolUse-Write", tool_input={"file_path": str(other), "content": VALID_HANDOVER}))
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not rotation(project).exists()


def test_another_sessions_handover_is_ignored(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    foreign = rotation(project) / "handover-sess-b.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text(VALID_HANDOVER)
    proc = s.hook("handover_detector", s.payload(
        "07-PostToolUse-Write", tool_input={"file_path": str(foreign), "content": VALID_HANDOVER}))
    assert proc.stdout == ""
    assert list(rotation(project).glob("ready-*.json")) == []


def test_inside_a_subagent_nothing_happens(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    s.handover.parent.mkdir(parents=True)
    s.handover.write_text(VALID_HANDOVER)
    proc = s.hook("handover_detector", s.payload(
        "07-PostToolUse-Write", agent_id="agent-1", agent_type="general-purpose",
        tool_input={"file_path": str(s.handover), "content": VALID_HANDOVER}))
    assert proc.stdout == ""
    assert not s.marker("ready").exists()


def test_enabled_false_is_silent(tmp_path, project):
    settings_file(project, enabled=False)
    s = Session(tmp_path, project, "sess-a")
    proc = s.write_handover()
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not s.marker("ready").exists()


def test_without_claude_pid_the_marker_has_no_process(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    s.handover.parent.mkdir(parents=True)
    s.handover.write_text(VALID_HANDOVER)
    proc = s.hook("handover_detector", s.payload(
        "07-PostToolUse-Write", tool_input={"file_path": str(s.handover)}), drop=("CLAUDE_PID",))
    assert proc.returncode == 0
    assert ready(s)["process"] is None


def test_parallel_detectors_leave_one_valid_marker(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    s.handover.parent.mkdir(parents=True)
    s.handover.write_text(VALID_HANDOVER)
    payload = json.dumps(s.payload("07-PostToolUse-Write", tool_input={"file_path": str(s.handover)}))
    env = dict(os.environ, CLAUDE_PID=str(os.getpid()))
    procs = [
        subprocess.Popen([sys.executable, str(HOOKS / "handover_detector.py")], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(8)
    ]
    results = [p.communicate(payload, timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0] * 8, results
    assert ready(s)["session_id"] == "sess-a"
    assert list(rotation(project).glob("*.tmp")) == []
    assert sorted(p.name for p in rotation(project).glob("*.json")) == ["ready-sess-a.json"]


def test_unwritable_rotation_dir_fails_without_blocking(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    s.handover.parent.mkdir(parents=True)
    s.handover.write_text(VALID_HANDOVER)
    (rotation(project) / ".gitignore").write_text("*\n")
    (rotation(project) / ".lock").mkdir()
    proc = s.hook("handover_detector", s.payload("07-PostToolUse-Write", tool_input={"file_path": str(s.handover)}))
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "read handover failed" in proc.stderr
