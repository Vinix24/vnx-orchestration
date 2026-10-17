"""rotation_state: process identity, the project lock, markers, claim, archive and listing."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import rotation_state as rs
from conftest import PKG, VALID_HANDOVER, rotation

DEFAULT_SETTINGS = {"handover_max_age_minutes": 60}


# --- process identity --------------------------------------------------------

def test_identity_of_a_live_process_is_pid_and_start():
    identity = rs.process_identity({"CLAUDE_PID": str(os.getpid())})
    assert identity["pid"] == os.getpid()
    assert identity["start"].startswith(("proc:", "ps:"))
    assert rs.process_identity({"CLAUDE_PID": str(os.getpid())}) == identity


def test_another_process_has_another_identity(other_pid):
    assert rs.process_identity({"CLAUDE_PID": str(other_pid)}) != rs.process_identity({"CLAUDE_PID": str(os.getpid())})


@pytest.mark.parametrize("raw", [None, "", "abc", "-5", "0", "12 3", "²"])
def test_no_identity_without_a_usable_pid(raw):
    env = {} if raw is None else {"CLAUDE_PID": raw}
    assert rs.process_identity(env) is None


def test_no_identity_for_a_process_that_is_gone():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert rs.process_start(child.pid) is None


# --- the project lock ----------------------------------------------------------

def test_the_lock_is_reentrant_and_writes_the_gitignore(project):
    with rs.project_lock(str(project)):
        with rs.project_lock(str(project)):
            assert (rotation(project) / ".gitignore").read_text() == "*\n"
    with rs.project_lock(str(project), timeout=0.2):
        pass


def test_a_held_lock_times_out_another_process(project):
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, %r); import rotation_state as rs\n"
         "with rs.project_lock(%r):\n    print('held', flush=True)\n    time.sleep(30)\n"
         % (str(PKG / "lib"), str(project))],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(rs.LockTimeout):
            with rs.project_lock(str(project), timeout=0.3):
                pass
    finally:
        holder.kill()
        holder.wait()
    with rs.project_lock(str(project), timeout=2):
        pass


# --- markers -----------------------------------------------------------------------

def make_marker(project, session_id="sess-a", data=VALID_HANDOVER.encode()):
    handover = rs.handover_path(str(project), session_id)
    os.makedirs(os.path.dirname(handover), exist_ok=True)
    with open(handover, "wb") as handle:
        handle.write(data)
    marker = rs.new_marker(session_id, handover, data, {"CLAUDE_PID": str(os.getpid())}, None, "tool-1")
    rs.mark_ready(str(project), marker)
    return marker


def test_ready_then_outdated_then_ready(project):
    make_marker(project)
    outdated = rs.mark_outdated(str(project), "sess-a", "a prompt after it", notified=False)
    assert outdated["outdated_reason"] == "a prompt after it"
    assert sorted(p.name for p in rotation(project).glob("*.json")) == ["outdated-sess-a.json"]
    assert rs.mark_outdated(str(project), "sess-a", "again", notified=True) is None
    make_marker(project)
    assert sorted(p.name for p in rotation(project).glob("*.json")) == ["ready-sess-a.json"]


def test_an_outdated_notice_is_taken_once(project):
    make_marker(project)
    rs.mark_outdated(str(project), "sess-a", "work after it (a Bash call)", notified=False)
    assert rs.take_outdated_notice(str(project), "sess-a")["outdated_reason"] == "work after it (a Bash call)"
    assert rs.take_outdated_notice(str(project), "sess-a") is None


def test_announced_once_and_only_for_the_same_content(project):
    marker = make_marker(project)
    assert rs.mark_announced(str(project), "sess-a", "other-sha") is False
    assert rs.mark_announced(str(project), "sess-a", marker["sha256"]) is True
    assert rs.mark_announced(str(project), "sess-a", marker["sha256"]) is False


@pytest.mark.parametrize("change", [
    {"version": 2}, {"session_id": "../x"}, {"sha256": None}, {"handover": 5},
])
def test_a_marker_of_another_version_or_shape_is_not_loaded(project, change):
    make_marker(project)
    path = rotation(project) / "ready-sess-a.json"
    data = json.loads(path.read_text())
    data.update(change)
    path.write_text(json.dumps(data))
    assert rs.load_marker(str(project), str(path)) is None
    assert path.exists()


def test_a_corrupt_marker_is_not_loaded(project):
    make_marker(project)
    path = rotation(project) / "ready-sess-a.json"
    path.write_text("{not json")
    assert rs.load_marker(str(project), str(path)) is None


# --- outdated rules ------------------------------------------------------------------

@pytest.mark.parametrize("prompt,outdates", [
    ("/clear", False), ("  /clear  ", False), ("/clear now", False), ("/clearer", True),
    ("go on", True), ("", True), (None, True),
])
def test_prompt_rule(prompt, outdates):
    assert rs.prompt_outdates(prompt) is outdates


@pytest.mark.parametrize("tool,tool_input,outdates", [
    ("Read", {"file_path": "/x"}, False),
    ("Glob", {"pattern": "*"}, False),
    ("Grep", {"pattern": "x"}, False),
    ("Write", {"file_path": "HANDOVER"}, False),
    ("Edit", {"file_path": "HANDOVER"}, False),
    ("Write", {"file_path": "/elsewhere.md"}, True),
    ("Bash", {"command": "ls"}, True),
    ("TodoWrite", {"todos": []}, True),
    ("Skill", {"skill": "x"}, True),
    (None, None, True),
])
def test_tool_call_rule(tmp_path, tool, tool_input, outdates):
    handover = str(tmp_path / "handover-s.md")
    if tool_input and tool_input.get("file_path") == "HANDOVER":
        tool_input = {"file_path": handover}
    assert rs.tool_call_outdates(tool, tool_input, handover) is outdates


def test_age_rule():
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    marker = {"written_at": (now - timedelta(minutes=59)).strftime(rs.TIME_FORMAT)}
    assert rs.over_age(marker, DEFAULT_SETTINGS, now) is False
    marker["written_at"] = (now - timedelta(minutes=61)).strftime(rs.TIME_FORMAT)
    assert rs.over_age(marker, DEFAULT_SETTINGS, now) is True
    assert rs.over_age({"written_at": "yesterday"}, DEFAULT_SETTINGS, now) is True


def test_outdated_reason_checks_the_file_first(project):
    marker = make_marker(project)
    assert rs.outdated_reason(marker, DEFAULT_SETTINGS, None) == "the handover file is gone"
    assert rs.outdated_reason(marker, DEFAULT_SETTINGS, b"other") == "the handover file changed after it was checked"
    assert rs.outdated_reason(marker, DEFAULT_SETTINGS, VALID_HANDOVER.encode()) == "its transcript position is unknown"


# --- claim, archive, log -----------------------------------------------------------------

def test_a_marker_is_claimed_once(project):
    make_marker(project)
    source = rs.marker_path(str(project), "ready", "sess-a")
    first = rs.claim(str(project), source, "sess-b")
    assert first is not None
    path, marker, data = first
    assert os.path.basename(path) == "claimed-sess-a-sess-b.json"
    assert marker["claimed_by"] == "sess-b"
    assert data == VALID_HANDOVER.encode()
    assert marker["claimed_sha256"] == marker["sha256"] == rs.sha256_hex(data)
    assert marker["archive"].startswith(rs.archive_dir(str(project)))
    assert json.loads(open(path).read())["archive"] == marker["archive"]
    assert rs.claim(str(project), source, "sess-c") is None


def test_archive_paths_never_collide(project):
    moment = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    first = rs.archive_handover(rs.plan_archive(str(project), "sess-a", moment), b"one")
    second = rs.plan_archive(str(project), "sess-a", moment)
    assert second != first and second.endswith("-sess-a-1.md")
    assert rs.ARCHIVE_NAME.match(os.path.basename(first))
    assert rs.ARCHIVE_NAME.match(os.path.basename(second))


def test_trim_keeps_the_newest(project):
    for day in range(1, 6):
        moment = datetime(2026, 10, day, tzinfo=timezone.utc)
        rs.archive_handover(rs.plan_archive(str(project), "sess-%d" % day, moment), b"x")
    assert rs.trim_archive(str(project), 2) == 3
    names = sorted(os.listdir(rs.archive_dir(str(project))))
    assert [n.split("Z-")[1] for n in names] == ["sess-4.md", "sess-5.md"]
    assert rs.trim_archive(str(project), 2) == 0


def archive_n(project, count, first_day=1):
    return [rs.archive_handover(rs.plan_archive(str(project), "sess-%d" % day, datetime(2026, 9, day, tzinfo=timezone.utc)), b"x")
            for day in range(first_day, first_day + count)]


def claim_naming(project, copy, session_id="sess-old"):
    """A claimed marker that names `copy` as its archive."""
    make_marker(project, session_id)
    path, marker, _ = rs.claim(str(project), rs.marker_path(str(project), "ready", session_id), "sess-new")
    marker["archive"] = copy
    rs.write_json(path, marker)
    return path


def test_trim_keeps_every_copy_a_marker_names(project):
    old = archive_n(project, 3)
    for number, copy in enumerate(old):
        claim_naming(project, copy, "sess-claim-%d" % number)
    newer = archive_n(project, 2, first_day=10)
    unnamed = archive_n(project, 2, first_day=5)
    assert rs.trim_archive(str(project), 2) == 2
    left = sorted(os.listdir(rs.archive_dir(str(project))))
    assert left == sorted(os.path.basename(p) for p in old + newer)
    assert not any(os.path.exists(p) for p in unnamed)


def test_trim_keeps_a_copy_named_by_a_marker_that_fails_marker_problem(project):
    old = archive_n(project, 1)[0]
    path = claim_naming(project, old)
    data = json.loads(open(path).read())
    data["claimed_sha256"] = "not a digest"
    rs.write_json(path, data)
    assert rs.read_marker(str(project), path)[0] is None
    archive_n(project, 3, first_day=10)
    assert rs.trim_archive(str(project), 2) == 1
    assert os.path.exists(old)


def test_trim_ignores_a_marker_that_is_not_json(project):
    old = archive_n(project, 3)
    (rotation(project) / "claimed-sess-a-sess-b.json").write_text("{not json")
    assert rs.trim_archive(str(project), 1) == 2
    assert [os.path.exists(p) for p in old] == [False, False, True]


def test_trim_survives_a_marker_whose_archive_field_holds_a_nul(project):
    named = archive_n(project, 2)
    claim_naming(project, named[0])
    unnamed = archive_n(project, 3, first_day=5)
    newest = archive_n(project, 2, first_day=10)
    rs.write_json(str(rotation(project) / "claimed-sess-x-sess-y.json"), {"archive": "x\u0000y"})
    assert rs.trim_archive(str(project), 2) == 4
    assert os.path.exists(named[0]) and all(os.path.exists(p) for p in newest)
    assert not any(os.path.exists(p) for p in unnamed + named[1:])


@pytest.mark.parametrize("field", [None, 7, ["a"], "", "relative.md", "/nowhere/20260101T000000000000Z-s.md"])
def test_trim_survives_any_archive_field(project, field):
    old = archive_n(project, 3)
    rs.write_json(str(rotation(project) / "claimed-sess-x-sess-y.json"), {"archive": field})
    assert rs.trim_archive(str(project), 1) == 2
    assert [os.path.exists(p) for p in old] == [False, False, True]


def test_trim_without_an_archive_creates_nothing(project):
    assert rs.trim_archive(str(project), 3) == 0
    assert not (project / ".claude").exists()


def test_injection_log_appends_and_skips_bad_lines(project):
    rs.log_injection(str(project), {"archive": "a"})
    with open(rs.injections_path(str(project)), "a") as handle:
        handle.write("not json\n[1]\n")
    rs.log_injection(str(project), {"archive": "b"})
    assert [r["archive"] for r in rs.read_injections(str(project))] == ["a", "b"]


def test_pending_lists_claims_first_then_newest_markers(project):
    make_marker(project, "sess-old")
    path = rs.marker_path(str(project), "ready", "sess-old")
    data = json.loads(open(path).read())
    data["written_at"] = "2026-01-01T00:00:00Z"
    rs.write_json(path, data)
    make_marker(project, "sess-new")
    make_marker(project, "sess-taken")
    rs.claim(str(project), rs.marker_path(str(project), "ready", "sess-taken"), "sess-x")
    kinds = [(e["kind"], e["marker"]["session_id"]) for e in rs.pending_handovers(str(project), None)]
    assert kinds == [("claimed", "sess-taken"), ("ready", "sess-new"), ("ready", "sess-old")]
    assert all(e["marker"]["session_id"] != "sess-new" for e in rs.pending_handovers(str(project), "sess-new"))
