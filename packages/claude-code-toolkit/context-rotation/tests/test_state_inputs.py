"""The files under .claude/rotation are inputs, not facts.

Any process of the user can write a marker, a handover or an archive entry. Every
path and name taken from them is checked before it is read, written, renamed or
deleted; what fails a check is reported and left on disk. Each test drives the
real hook entries, the way such a file would meet them.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest

from conftest import HOOKS, VALID_HANDOVER, Session, context_of, event, message_of, rotation, settings_file, start

import hook_runtime
import rotation_state

QUICK = 10  # seconds: a hook that blocks on a FIFO runs into this instead of hanging the suite
SECRET = "secret text of an unrelated file"
GENERATION = "0123456789abcdef"
CLAIMED = "claimed-sess-p-%s.json" % GENERATION


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def handover_of(project, session_id="sess-p", text=VALID_HANDOVER):
    path = rotation(project) / ("handover-%s.md" % session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def plant(project, name, **fields):
    """A marker written by hand, as any process of the user can."""
    marker = {
        "version": 1, "session_id": "sess-p", "generation": GENERATION,
        "handover": str(rotation(project) / "handover-sess-p.md"),
        "sha256": sha(VALID_HANDOVER), "chars": len(VALID_HANDOVER), "process": None, "tmux_pane": None,
        "transcript": None, "tool_use_id": None, "written_at": rotation_state.utc_now(), "announced": False,
    }
    marker.update(fields)
    path = rotation(project) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(marker), encoding="utf-8")
    return path


@pytest.fixture
def victim(tmp_path):
    path = tmp_path / "elsewhere" / "victim.txt"
    path.parent.mkdir()
    path.write_text(SECRET, encoding="utf-8")
    return path


def quick(entry, payload, pid=None):
    """Run a hook entry with a short timeout, so a blocking hook fails the test instead of hanging it."""
    env = dict(os.environ, CLAUDE_PID=str(pid or os.getpid()))
    return subprocess.run([sys.executable, str(HOOKS / (entry + ".py"))], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=QUICK)


def resume(tmp_path, project, args, session_id="sess-c"):
    """The resume action; a number is picked from the list, shown first as the user would see it."""
    user = Session(tmp_path, project, session_id)

    def expand(words):
        return quick("context_guard", user.payload(
            "09-UserPromptExpansion-plugin-skill", command_name="context-rotation:rotate",
            command_args=words, prompt="/context-rotation:rotate " + words))

    if args.split()[1:2] and args.split()[1].isdigit():
        expand("resume")
    return expand(args)


def text_of(proc):
    assert proc.returncode == 0, proc.stderr
    return context_of(proc) or ""


def error_lines(project):
    path = rotation(project) / "errors.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# --- paths named by a marker ----------------------------------------------

def test_a_claimed_marker_cannot_aim_the_archive_copy_outside_the_project(tmp_path, project, victim):
    handover = handover_of(project)
    marker = plant(project, CLAIMED, claimed_by="sess-x", claimed_sha256=sha(VALID_HANDOVER),
                   claimed_at=rotation_state.utc_now(), archive=str(victim))
    proc = resume(tmp_path, project, "resume 1")
    assert victim.read_text() == SECRET
    assert VALID_HANDOVER not in text_of(proc)
    assert marker.exists() and handover.exists()
    assert any("its archive is not named like an archive copy" in line for line in error_lines(project))


@pytest.mark.parametrize("name,fields", [
    ("ready-plant.json", {}),
    ("ready-sess-p.json", {"handover": "VICTIM"}),
    ("outdated-sess-p.json", {"handover": "VICTIM", "outdated_reason": "a prompt after it"}),
], ids=["file-name-and-session-disagree", "ready-handover-elsewhere", "outdated-handover-elsewhere"])
def test_a_planted_marker_is_neither_injected_nor_cleaned_up(tmp_path, project, victim, name, fields):
    handover = handover_of(project)
    fields = {key: (str(victim) if value == "VICTIM" else value) for key, value in fields.items()}
    marker = plant(project, name, **fields)
    text = text_of(resume(tmp_path, project, "resume 1"))
    assert SECRET not in text and VALID_HANDOVER not in text
    assert victim.read_text() == SECRET
    assert handover.read_text() == VALID_HANDOVER
    assert marker.exists()


def test_a_handover_that_is_a_fifo_does_not_block_the_hook(tmp_path, project):
    fifo = rotation(project) / "handover-sess-p.md"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(str(fifo))
    marker = plant(project, "ready-sess-p.json")
    proc = resume(tmp_path, project, "resume 1")
    assert proc.returncode == 0, proc.stderr
    assert fifo.exists() and marker.exists()


def test_a_handover_that_is_a_symlink_is_not_followed(tmp_path, project, victim):
    link = rotation(project) / "handover-sess-p.md"
    link.parent.mkdir(parents=True)
    os.symlink(str(victim), str(link))
    plant(project, "ready-sess-p.json", sha256=sha(SECRET))
    assert SECRET not in text_of(resume(tmp_path, project, "resume 1"))
    assert victim.read_text() == SECRET


def test_a_transcript_that_is_a_fifo_does_not_block_the_clear(tmp_path, project):
    handover_of(project)
    fifo = tmp_path / "sess-p.jsonl"  # the session's own transcript, so the marker passes its checks
    os.mkfifo(str(fifo))
    identity = rotation_state.process_identity({"CLAUDE_PID": str(os.getpid())})
    plant(project, "ready-sess-p.json", process=identity,
          transcript={"path": str(fifo), "size": 0, "last_uuid": None})
    payload = event("17-SessionStart-clear", project, project / "none.jsonl", "sess-b", source="clear")
    proc = quick("session_start", payload)
    assert proc.returncode == 0, proc.stderr
    assert VALID_HANDOVER not in text_of(proc)


@pytest.mark.parametrize("name,fields", [
    ("ready-sess-p.json", {"handover": "VICTIM"}),
    ("ready-dots.json", {"session_id": "../dots"}),
    (CLAIMED, {"claimed_by": "sess-x", "claimed_sha256": sha(VALID_HANDOVER),
                                    "archive": "VICTIM"}),
], ids=["handover-elsewhere", "session-id-with-separator", "archive-elsewhere"])
def test_a_rejected_marker_is_reported_once_and_left_on_disk(tmp_path, project, victim, name, fields):
    handover_of(project)
    fields = {key: (str(victim) if value == "VICTIM" else value) for key, value in fields.items()}
    marker = plant(project, name, **fields)
    first = start(project, "sess-b", source="startup")
    second = start(project, "sess-c", source="startup")
    assert (first.returncode, second.returncode) == (0, 0)
    assert name in first.stderr
    assert name not in second.stderr
    assert len([line for line in error_lines(project) if name in line]) == 1
    assert marker.exists() and victim.read_text() == SECRET


def test_the_list_says_whose_process_and_whether_the_paths_passed(tmp_path, project, victim, other_pid):
    mine = Session(tmp_path, project, "sess-mine")
    mine.write_handover()
    mine.finish_turn()
    theirs = Session(tmp_path, project, "sess-theirs", pid=other_pid)
    theirs.write_handover()
    theirs.finish_turn()
    plant(project, "ready-sess-p.json", handover=str(victim))
    lines = text_of(resume(tmp_path, project, "resume")).splitlines()
    numbered = [line for line in lines if line[:1].isdigit()]
    assert len(numbered) == 2
    assert any("sess-mine" in line and "this claude process" in line for line in numbered)
    assert any("sess-theirs" in line and "another claude process" in line for line in numbered)
    assert all("paths checked" in line for line in numbered)
    [rejected] = [line for line in lines if "ready-sess-p.json" in line]
    assert "not resumable" in rejected


ARCHIVE = "ARCHIVE/20261016T120000000000Z-sess-p.md"
CLAIM = {"claimed_by": "sess-x", "claimed_sha256": sha(VALID_HANDOVER), "archive": ARCHIVE}


@pytest.mark.parametrize("name,fields,why", [
    ("ready-sess-p.json", {"version": 2}, "it is not a version 1 marker"),
    ("ready-sess-p.json", {"session_id": "sess-p\n"}, "its session_id is not a plain file name"),
    ("ready-sess-p.json", {"session_id": "x" * 121}, "its session_id is not a plain file name"),
    ("ready-sess-p.json", {"generation": "0123456789ABCDEF"}, "its generation is not one the plugin gives"),
    ("ready-sess-p.json", {"generation": None}, "its generation is not one the plugin gives"),
    ("ready-sess-p.json", {"sha256": "not-a-digest"}, "its sha256 is not a sha256 digest"),
    ("ready-other.json", {}, "its file name does not match its session_id and generation"),
    ("ready-sess-p.json", {"handover": "handover-sess-p.md"}, "its handover is not"),
    ("ready-sess-p.json", {"handover": "ROTATION/../handover-sess-p.md"}, "its handover is not"),
    ("ready-sess-p.json", {"tool_use_id": ["a"]}, "its tool_use_id is not a string"),
    ("ready-sess-p.json", {"transcript": {"path": "t.jsonl", "size": 0, "last_uuid": None}},
     "its transcript position is not"),
    ("ready-sess-p.json", {"transcript": {"path": "/t.jsonl", "size": True, "last_uuid": None}},
     "its transcript position is not"),
    (CLAIMED, dict(CLAIM, claimed_by="../x"), "its claimed_by is not a plain file name"),
    ("claimed-sess-p-sess-x.json", CLAIM, "its file name does not match its session_id and generation"),
    ("claimed-sess-p-fedcba9876543210.json", CLAIM, "its file name does not match its session_id and generation"),
    (CLAIMED, dict(CLAIM, claimed_sha256=None), "its claimed_sha256 is not a sha256 digest"),
    (CLAIMED, dict(CLAIM, archive=None), "its archive is not a plain absolute path"),
    (CLAIMED, dict(CLAIM, archive="ARCHIVE/notes.txt"), "its archive is not named like"),
    (CLAIMED, dict(CLAIM, archive="ROTATION/20261016T120000000000Z-sess-p.md"),
     "its archive is not in"),
    (CLAIMED, dict(CLAIM, archive="LINK"), "its archive is a symlink"),
])
def test_each_check_rejects_its_marker(tmp_path, project, victim, name, fields, why):
    handover_of(project)
    archive = rotation(project) / "archive"
    archive.mkdir(parents=True)
    os.symlink(str(victim), str(archive / "20261016T130000000000Z-sess-p.md"))
    places = {"ARCHIVE": str(archive), "ROTATION": str(rotation(project)),
              "LINK": str(archive / "20261016T130000000000Z-sess-p.md")}
    fields = dict(fields)
    for key, value in list(fields.items()):
        if isinstance(value, str):
            for placeholder, place in places.items():
                value = value.replace(placeholder, place) if value.startswith(placeholder) else value
            fields[key] = value
    marker = plant(project, name, **fields)
    proc = start(project, "sess-b", source="startup")
    assert proc.returncode == 0, proc.stderr
    assert VALID_HANDOVER not in (message_of(proc) or "")
    reports = [line for line in error_lines(project) if name in line]
    assert len(reports) == 1, "the marker was not reported as rejected"
    assert why in reports[0]
    assert marker.exists() and victim.read_text() == SECRET


def test_an_injection_log_that_is_a_fifo_does_not_block_the_list(tmp_path, project):
    directory = rotation(project) / "archive"
    directory.mkdir(parents=True)
    (directory / "20261016T120000000000Z-sess-p.md").write_text(VALID_HANDOVER)
    os.mkfifo(str(rotation(project) / "injections.jsonl"))
    proc = resume(tmp_path, project, "resume")
    assert "1. archived" in text_of(proc)


def test_an_errors_log_that_is_a_fifo_does_not_block_a_failure_report(tmp_path, project):
    (rotation(project) / "state" / "sess-c.json").mkdir(parents=True)  # a directory where the state file belongs
    os.mkfifo(str(rotation(project) / "errors.log"))
    payload = event("02-UserPromptSubmit", project, tmp_path / "no-transcript.jsonl", "sess-c", prompt="go on")
    proc = quick("context_guard", payload)
    assert proc.returncode == 1
    assert "save state failed" in proc.stderr
    assert "save state failed" in (rotation(project) / "errors.log").read_text()


# --- entries of the archive directory ---------------------------------------

def test_an_archive_entry_that_is_a_symlink_is_not_resumed(tmp_path, project, victim):
    directory = rotation(project) / "archive"
    directory.mkdir(parents=True)
    os.symlink(str(victim), str(directory / "20260101T120000000000Z-sess-p.md"))
    assert SECRET not in text_of(resume(tmp_path, project, "resume 1"))


def test_trimming_removes_only_regular_files(tmp_path, project, victim):
    settings_file(project, archive_keep=1)
    directory = rotation(project) / "archive"
    (directory / "20260101T120000000000Z-sess-1.md").mkdir(parents=True)
    os.symlink(str(victim), str(directory / "20260102T120000000000Z-sess-2.md"))
    (directory / "20260103T120000000000Z-sess-3.md").write_text("old")
    (directory / "20260104T120000000000Z-sess-4.md").write_text("new")
    proc = start(project, "sess-b", source="startup")
    assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in directory.iterdir()) == [
        "20260101T120000000000Z-sess-1.md", "20260102T120000000000Z-sess-2.md", "20260104T120000000000Z-sess-4.md"]
    assert victim.read_text() == SECRET


# --- the hook output ------------------------------------------------------------

def test_emit_writes_nothing_for_no_document(capsys):
    hook_runtime.emit(None)
    assert capsys.readouterr().out == ""
