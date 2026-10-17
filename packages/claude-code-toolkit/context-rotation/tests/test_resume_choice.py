"""What `resume` offers and what a pick takes (OI-2126 item 3, OI-2136 item 2, OI-2138 item 4).

The list the user saw is the list a number refers to: each entry carries a stable
id, the numbers are stored with the listing, and a handover that arrived after
the list was shown cannot slip under a number the user already read.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from conftest import VALID_HANDOVER, Session, context_of, message_of, rotation

import rotation_state as rs

HANDOVER_A = VALID_HANDOVER + "\nThis is handover A.\n"
HANDOVER_B = VALID_HANDOVER + "\nThis is handover B.\n"


def saved(tmp_path, project, session_id, pid, text):
    s = Session(tmp_path, project, session_id, pid=pid)
    s.write_handover(text)
    s.finish_turn()
    assert s.marker("ready").exists()
    return s


def ident(s, kind="ready"):
    """The id the list shows for a session's marker: <kind>-<session>-<generation>."""
    return "%s-%s-%s" % (kind, s.session_id, json.loads(s.marker(kind).read_text())["generation"])


def written_minutes_ago(path, minutes):
    marker = json.loads(path.read_text())
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    marker["written_at"] = moment.strftime(rs.TIME_FORMAT)
    path.write_text(json.dumps(marker))


def numbered(text):
    return [line for line in (text or "").splitlines() if line[:1].isdigit()]


def test_a_number_picks_the_handover_shown_under_it(tmp_path, project, other_pid):
    a = saved(tmp_path, project, "sess-a", other_pid, HANDOVER_A)
    written_minutes_ago(a.marker("ready"), 5)
    user = Session(tmp_path, project, "sess-c")
    [first] = numbered(context_of(user.expand("resume")))
    assert first.startswith("1. ") and "sess-a" in first
    saved(tmp_path, project, "sess-b", other_pid, HANDOVER_B)  # newer, so it now sorts first
    text = context_of(user.expand("resume 1"))
    assert "This is handover A." in text
    assert "This is handover B." not in text


def test_the_list_shows_a_stable_id_per_entry(tmp_path, project, other_pid):
    a = saved(tmp_path, project, "sess-a", other_pid, HANDOVER_A)
    Session(tmp_path, project, "sess-b").expand("resume " + ident(a))  # archives it
    a2 = saved(tmp_path, project, "sess-a2", other_pid, HANDOVER_B)
    lines = numbered(context_of(Session(tmp_path, project, "sess-c").expand("resume")))
    assert lines[0].endswith("[id %s]" % ident(a2))
    [archive] = (rotation(project) / "archive").glob("*.md")
    assert lines[1].endswith("[id %s]" % archive.name[:-3])


def test_an_id_picks_its_handover(tmp_path, project, other_pid):
    a = saved(tmp_path, project, "sess-a", other_pid, HANDOVER_A)
    saved(tmp_path, project, "sess-b", other_pid, HANDOVER_B)
    text = context_of(Session(tmp_path, project, "sess-c").expand("resume " + ident(a)))
    assert "This is handover A." in text and "This is handover B." not in text


def test_a_number_without_a_list_shown_is_refused_with_the_current_list(tmp_path, project, other_pid):
    saved(tmp_path, project, "sess-a", other_pid, HANDOVER_A)
    user = Session(tmp_path, project, "sess-c")
    refused = context_of(user.expand("resume 1"))
    assert "This is handover A." not in refused
    assert "No list was shown in this session yet" in refused
    assert numbered(refused)[0].startswith("1. ready")
    assert "This is handover A." in context_of(user.expand("resume 1"))


def test_a_number_whose_handover_is_gone_is_refused_with_the_current_list(tmp_path, project, other_pid):
    a = saved(tmp_path, project, "sess-a", other_pid, HANDOVER_A)
    taken = ident(a)
    user = Session(tmp_path, project, "sess-c")
    user.expand("resume")
    Session(tmp_path, project, "sess-d").expand("resume " + taken)  # another session takes it
    text = context_of(user.expand("resume 1"))
    assert "Handover %s is no longer there" % taken in text
    assert "This is handover A." not in text
    assert numbered(text)[0].startswith("1. archived")


@pytest.mark.parametrize("pick", ["resume 7", "resume " + "9" * 5000], ids=["seven", "5000-digits"])
def test_a_number_outside_the_list_is_refused_with_the_normal_message(tmp_path, project, pick):
    proc = Session(tmp_path, project, "sess-c").expand(pick)
    assert proc.returncode == 0, proc.stderr
    assert "There is no handover with that number." in context_of(proc)


@pytest.mark.parametrize("pick", ["resume x", "resume ../ready-sess-a", "resume ²"],
                         ids=["word", "path", "superscript-digit"])
def test_a_pick_that_is_neither_a_number_nor_an_id_gets_the_usage(tmp_path, project, pick):
    proc = Session(tmp_path, project, "sess-c").expand(pick)
    assert "Type /context-rotation:rotate resume <number> or resume <id>" in context_of(proc)


@pytest.mark.parametrize("args", ["wirte", "status", "Resume1"])
def test_an_unknown_action_answers_with_the_usage_line(tmp_path, project, args):
    proc = Session(tmp_path, project, "sess-c").expand(args)
    assert proc.returncode == 0, proc.stderr
    assert message_of(proc) == (
        "context-rotation: usage: /context-rotation:rotate [write | release | resume [<number> | <id>]]")


# --- the order of the list -------------------------------------------------------------

def make_ready(project, session_id, written_at):
    handover = rs.handover_path(str(project), session_id)
    os.makedirs(os.path.dirname(handover), exist_ok=True)
    with open(handover, "wb") as handle:
        handle.write(VALID_HANDOVER.encode())
    marker = rs.new_marker(session_id, handover, VALID_HANDOVER.encode(), {"CLAUDE_PID": str(os.getpid())},
                           None, "tool-1")
    marker["written_at"] = written_at
    rs.mark_ready(str(project), marker)


def test_claims_come_first_then_ready_then_outdated_each_newest_first(project):
    make_ready(project, "sess-ready-old", "2026-10-17T10:00:00Z")
    make_ready(project, "sess-ready-new", "2026-10-17T11:00:00Z")
    make_ready(project, "sess-outdated-newest", "2026-10-17T12:00:00Z")
    make_ready(project, "sess-outdated-old", "2026-10-17T09:00:00Z")
    for session_id in ("sess-outdated-newest", "sess-outdated-old"):
        rs.mark_outdated(str(project), session_id, "a prompt after it", notified=True)
    make_ready(project, "sess-taken", "2026-10-17T08:00:00Z")
    rs.claim(str(project), rs.marker_path(str(project), "ready", "sess-taken"), "sess-x")
    order = [(e["kind"], e["marker"]["session_id"]) for e in rs.pending_handovers(str(project), None)]
    assert order == [
        ("claimed", "sess-taken"),
        ("ready", "sess-ready-new"), ("ready", "sess-ready-old"),
        ("outdated", "sess-outdated-newest"), ("outdated", "sess-outdated-old"),
    ]
