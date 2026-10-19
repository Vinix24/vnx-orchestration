"""The loop guard for sessions that start heavy from a handover injection (deliverable 2).

An injected session (a handover via the SessionStart /clear path) records its
start size and is held below force and hard until it has done min_work_tokens of
real work, so a rotation cannot restart it near the force band and loop. The one
loop U-line warns when the start is already so close to force that rotation will
not help.
"""
from __future__ import annotations

import json
import os

import pytest

import rotation_state
from conftest import Session, at, compacted, context_of, goes_ahead, journal, message_of, output, rotation, settings_file, state_of

ABSOLUTE = {"warn_tokens": 55000, "force_tokens": 65000, "hard_tokens": 85000}
LOOP_LINE = "rotating will not make the next session smaller"


@pytest.fixture
def s(tmp_path, project):
    return Session(tmp_path, project, "sess-a")


def inject(session, project):
    """The injections.jsonl line of a handover injected into `session` via /clear."""
    rotation_state.journal(
        str(project), "injected", via="clear", origin_session_id="sess-origin",
        generation="0123456789abcdef", session_id=session.session_id,
        handover=rotation_state.handover_path(str(project), "sess-origin"),
        archive=os.path.join(rotation_state.archive_dir(str(project)), "20261017T090000000000Z-sess-origin.md"),
        sha256="0" * 64, chars=1, mode="inline")


def test_injected_session_is_not_forced_before_start_plus_min_work(s):
    settings_file(s.project, **ABSOLUTE)
    inject(s, s.project)
    at(s, 60000)  # the start size: warn band, so only the loop line is new
    first = s.prompt("x")
    assert LOOP_LINE in message_of(first)
    assert state_of(s)["start_size"] == 60000
    assert state_of(s)["level"] == "low"
    at(s, 62000)  # grew toward force, but still in the warn band: warned, not forced
    warned = context_of(s.prompt("x"))
    assert "warn threshold" in warned and "in the force band" not in warned
    at(s, 70000)  # at the raw force threshold but less than min_work_tokens above start
    assert context_of(s.prompt("x")) is None
    assert state_of(s)["level"] == "low"
    at(s, 80000)  # start + min_work_tokens: forced now
    proc = s.prompt("x")
    assert "in the force band" in context_of(proc)
    assert state_of(s)["level"] == "force"


def test_the_loop_line_is_given_once(s):
    settings_file(s.project, **ABSOLUTE)
    inject(s, s.project)
    at(s, 60000)
    assert LOOP_LINE in message_of(s.prompt("x"))
    assert state_of(s)["loop_warned"] is True
    at(s, 61000)
    assert "systemMessage" not in output(s.prompt("x"))
    assert [line["event"] for line in journal(s.project)].count("loop_warned") == 1


def test_a_non_injected_session_is_forced_as_today(s):
    settings_file(s.project, **ABSOLUTE)
    at(s, 60000)
    first = s.prompt("x")
    assert "systemMessage" not in output(first)
    assert state_of(s) is None  # a warn-band prompt below force changes nothing
    at(s, 70000)
    proc = s.prompt("x")
    assert "in the force band" in context_of(proc)
    assert state_of(s)["level"] == "force"
    assert state_of(s)["start_size"] is None


def test_a_compaction_below_start_does_not_reset_the_start_size(s):
    settings_file(s.project, **ABSOLUTE)
    inject(s, s.project)
    at(s, 60000)
    s.prompt("x")
    at(s, 80000)
    s.prompt("x")
    assert state_of(s)["start_size"] == 60000
    assert state_of(s)["level"] == "force"
    compacted(s, 50000)  # a compaction below the start size: warn, but the start stays
    assert goes_ahead(s.stop())
    assert state_of(s)["start_size"] == 60000
    assert state_of(s)["level"] == "low"
    at(s, 80000)  # a fresh rise to force uses the same start size, so it is forced
    assert "in the force band" in context_of(s.prompt("x"))


def test_an_old_version_state_is_replaced_as_fresh(s):
    settings_file(s.project, **ABSOLUTE)
    inject(s, s.project)
    state_dir = rotation(s.project) / "state"
    state_dir.mkdir(parents=True)
    (state_dir / ("%s.enforcement.json" % s.session_id)).write_text(json.dumps(
        {"version": 1, "level": "hard", "crossing": 2, "blocked": 2, "told": 2,
         "denials": 3, "inform_only": None}))
    at(s, 60000)
    proc = s.prompt("x")
    assert state_of(s)["version"] == 2
    assert state_of(s)["start_size"] == 60000
    assert LOOP_LINE in message_of(proc)
